"""Gộp các tin trùng của cùng một doanh nghiệp thành một job.

Hai tin cùng cụm khi thoả cả ba điều kiện:

1. `title_normalized` giống nhau (rapidfuzz `token_set_ratio` ≥ 90) và không lệch nhau ở từ chỉ
   cấp bậc ("Java Developer" / "Senior Java Developer" là hai việc);
2. có chung ít nhất một tỉnh/thành (tin không rõ địa điểm thì không chặn);
3. JD giống nhau (MinHash Jaccard ≥ 0.8 trên 3-gram từ) hoặc cùng `apply_url` (nguồn không ghi
   thì lấy URL tin: hai bản bóc khác nhau của cùng một trang luôn gộp).

Cùng nguồn mà cả hai không ghi nơi làm (doanh nghiệp đăng lại tin cũ với mã mới, vd. MISA
/tuyen-dung/5883 và /5920): gộp khi tiêu đề gần như y hệt, không nêu hai nơi khác nhau
("[Hải Dương] …" / "[Hải Phòng] …"), và JD gần như trùng (≥ 0.95).

`fingerprint` là dấu vân tay nội dung (tiêu đề + nơi làm + JD đã chuẩn hoá) — cột `jobs.fingerprint`
có index duy nhất trong các job đang mở của một doanh nghiệp, nên hai job y hệt không thể cùng
tồn tại; `store` dùng nó (và `same_existing`) để gắn tin mới vào job đã có thay vì tạo job mới.

Bản chuẩn: ATS / trang careers của doanh nghiệp → trang tuyển dụng có JD đầy đủ nhất → tin mới
nhất. Cụm giữ mọi URL nguồn.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from array import array
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from itertools import combinations
from typing import Any, Final
from uuid import UUID

from datasketch import LeanMinHash, MinHash
from rapidfuzz import fuzz

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import (
    EducationLevel,
    EmploymentType,
    ExtractionMethod,
    JobLevel,
    Location,
    WorkMode,
)
from jobcrawler.common.text import fold
from jobcrawler.common.urls import normalize_url
from jobcrawler.pipeline import provinces as province_names
from jobcrawler.pipeline.categories import merge_categories
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.services.net.kv import KeyValueStore

log = get_logger(__name__)

TITLE_THRESHOLD = 90.0
JD_THRESHOLD = 0.8
# Tin đăng lại (cùng nguồn, không rõ nơi làm): đòi gần như y hệt.
REPOST_TITLE_THRESHOLD = 97.0
REPOST_JD_THRESHOLD = 0.95
# Một tiêu đề hẹp hơn tiêu đề kia (`narrower_title`): đòi JD gần như trùng.
NARROWER_JD_THRESHOLD = 0.95
SHINGLE = 3
NUM_PERM = 128
# Băng LSH để tìm cặp ứng viên (`band_pairs`): 32 băng × 4 hàm băm của chữ ký.
LSH_BANDS = 32
# Chữ ký JD trên Redis cache: lượt đủ đêm sau dùng lại; JD không còn gặp thì tự hết hạn.
SIGNATURE_TTL = 3 * 86_400

_METHOD_RANK = {
    ExtractionMethod.ATS_API: 0,
    ExtractionMethod.JSONLD: 1,
    ExtractionMethod.PARSER: 2,
    ExtractionMethod.LLM: 3,
    ExtractionMethod.HEURISTIC: 4,
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# Hàm hoán vị của MinHash sinh một lần cho mọi chữ ký (sinh lại mỗi tin tốn ~0,4 ms).
_SCHEME: Final = "affine32"
_TEMPLATE = MinHash(num_perm=NUM_PERM, scheme=_SCHEME)
_CACHE_PREFIX = f"minhash:{SHINGLE}:{NUM_PERM}:{_SCHEME}:"


def jd_minhash(text: str) -> MinHash:
    words = fold(text).split()
    if len(words) < SHINGLE:
        grams = {" ".join(words)} if words else set()
    else:
        grams = {" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}
    m = MinHash(num_perm=NUM_PERM, permutations=_TEMPLATE.permutations, scheme=_SCHEME)
    m.update_batch([g.encode() for g in grams])
    return m


def encode_signature(sig: MinHash) -> bytes:
    return sig.hashvalues.tobytes()


def decode_signature(raw: bytes) -> MinHash | None:
    """Chữ ký đọc từ cache; sai kích thước (đổi tham số MinHash) thì None, tính lại."""
    values = array("I")
    if len(raw) != NUM_PERM * values.itemsize:
        return None
    values.frombytes(raw)
    return LeanMinHash(seed=_TEMPLATE.seed, hashvalues=values, scheme=_SCHEME)


def text_key(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def signature_cache_key(key: str) -> str:
    """Khoá Redis cache của chữ ký: `text_key` của JD, hoặc `job:<dấu vân tay>` của job đã lưu."""
    return _CACHE_PREFIX + key


def jd_similarity(a: MinHash, b: MinHash) -> float:
    if a.is_empty() or b.is_empty():
        return 0.0
    return float(a.jaccard(b))


# Từ chỉ cấp bậc / vai trò (đã bỏ dấu): hai tiêu đề lệch nhau ở các từ này là hai vị trí khác
# nhau dù JD dùng chung mẫu ("Java Developer" / "Senior Java Developer", "Kế toán" / "Kế toán
# trưởng"). `token_set_ratio` cho 100 khi từ của tiêu đề này nằm trọn trong tiêu đề kia.
_RANK_TOKENS = frozenset(
    {
        "senior", "sr", "junior", "jr", "lead", "leader", "principal", "head", "chief",
        "manager", "director", "intern", "internship", "fresher", "trainee", "assistant",
        "deputy", "truong", "pho", "i", "ii", "iii", "iv",
    }
)  # fmt: skip
_RANK_PHRASES = ("thuc tap", "tro ly", "giam doc")
# Bỏ dấu, "trường" (trường học, thị trường, hiện trường...), "phố" / "phổ" (thành phố, phổ thông)
# cũng là truong / pho: không phải cấp bậc. "Phó" là cấp bậc khi đứng trước chức vụ ("phó phòng").
_NOT_RANK = re.compile(
    r"\b(?:thi|moi|hien|quang|chien|nha|tang) truong\b"
    r"|\btruong(?= (?:hoc|hop|thanh|quoc te|dai hoc|cao dang|trung cap|trung hoc|tieu hoc|"
    r"mam non|mau giao|thcs|thpt|pho thong|lien cap|song ngu|nghe)\b)"
    r"|\b(?:thanh|khu|duong) pho\b"
    r"|\bpho\b(?! (?:phong|giam|tong|ban|truong|quan|chu|chanh|hieu|khoa|bep|nhom|ca|bo|chi|doi|"
    r"xuong|vien)\b)"
)


def _ranks(folded: str) -> set[str]:
    return set(_NOT_RANK.sub(" ", folded).split()) & _RANK_TOKENS


@lru_cache(maxsize=1 << 16)
def rank_conflict(a: str, b: str) -> bool:
    """Hai tiêu đề khác nhau ở từ chỉ cấp bậc."""
    fa, fb = fold(a), fold(b)
    if _ranks(fa) ^ _ranks(fb):
        return True
    return any((f" {p} " in f" {fa} ") != (f" {p} " in f" {fb} ") for p in _RANK_PHRASES)


# Một doanh nghiệp lặp lại vài chục tiêu đề trên hàng nghìn tin: nhớ kết quả theo cặp tiêu đề.
@lru_cache(maxsize=1 << 16)
def title_similarity(a: str, b: str) -> float:
    if not a or not b or rank_conflict(a, b):
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


# Từ đệm không đổi vị trí ("Nhân viên kinh doanh" = "Kinh doanh"): bỏ khi xét tiêu đề chứa nhau.
_FILLER = re.compile(r"\b(?:nhan vien|chuyen vien|nv|cv|staff|officer|executive|specialist)\b")


@lru_cache(maxsize=1 << 16)
def narrower_title(a: str, b: str) -> bool:
    """Từ của tiêu đề này nằm trọn trong tiêu đề kia, bên kia thêm từ chuyên môn ("Kế toán" /
    "Kế toán tổng hợp", "Java Developer" / "Java Backend Developer"). `token_set_ratio` cho 100
    nhưng thường là hai vị trí dùng chung mẫu JD, nên JD phải gần như trùng mới gộp."""
    words_a = set(_FILLER.sub(" ", fold(a)).split())
    words_b = set(_FILLER.sub(" ", fold(b)).split())
    return bool(words_a and words_b) and (words_a < words_b or words_b < words_a)


def jd_threshold_for(title_a: str, title_b: str, threshold: float = JD_THRESHOLD) -> float:
    """Ngưỡng JD để hai tin (tiêu đề đã chuẩn hoá) là một việc."""
    if narrower_title(title_a, title_b):
        return max(threshold, NARROWER_JD_THRESHOLD)
    return threshold


def compatible_provinces(pa: set[str], pb: set[str]) -> bool:
    return not pa or not pb or bool(pa & pb)


def same_places(a: NormalizedJob, b: NormalizedJob) -> bool:
    """Có chung nơi làm. Khác nguồn: chung một tỉnh/thành (tin không rõ địa điểm thì không chặn).
    Cùng nguồn: hai tin khác nhau thường là hai nơi tuyển khác nhau ("[Hải Dương] Kinh doanh",
    "[Hải Phòng] Kinh doanh" — cùng Hải Phòng sau sáp nhập), nên nơi làm phải biết rõ và trùng ở
    mức tỉnh cũ."""
    if a.source == b.source:
        return bool(a.places & b.places)
    return compatible_provinces(a.provinces, b.provinces)


def title_places(title: str) -> frozenset[str]:
    """Nơi làm nêu trong tiêu đề, ở mức tỉnh cũ ("[Hải Dương] Kinh doanh" → {"Hải Dương"})."""
    return frozenset(province_names.find_units(title))


def fingerprint(title: str, places: frozenset[str] | set[str], jd_text: str) -> str:
    """Dấu vân tay nội dung một job: tiêu đề, nơi làm (tỉnh cũ, hoặc tỉnh) và JD sau khi bỏ dấu,
    viết thường, gộp khoảng trắng. Hai tin y hệt (đăng lại với mã/URL khác) cho cùng giá trị."""
    jd = hashlib.sha1(fold(jd_text).encode()).hexdigest()
    key = "|".join([fold(title), ",".join(sorted(places)), jd])
    return hashlib.sha1(key.encode()).hexdigest()[:32]


def location_places(
    title: str, locations: Sequence[tuple[str | None, str | None]]
) -> frozenset[str]:
    """Nơi làm để so trùng: tỉnh cũ từ địa chỉ + tiêu đề (bỏ tỉnh bị một quận/huyện cụ thể hơn
    bao lấy); không có thì tỉnh/thành. `locations`: các cặp (tỉnh, địa chỉ gốc)."""
    units = {u for _, address in locations if (u := province_names.unit_of(address))}
    places = province_names.most_specific(units | title_places(title))
    return places or frozenset(p for p, _ in locations if p)


def job_fingerprint(job: NormalizedJob) -> str:
    """Dấu vân tay của job đã gộp (`merge`), cùng cách tính với `stored_fingerprint`."""
    pairs = [(loc.province, loc.address_raw) for loc in job.locations]
    return fingerprint(job.title, location_places(job.title, pairs), job.jd_text)


def stored_fingerprint(
    title: str, locations: Sequence[dict[str, Any]], description: str, requirements: str | None
) -> str:
    """Dấu vân tay từ một dòng `jobs` (locations JSON, mô tả, yêu cầu)."""
    pairs = [(loc.get("province"), loc.get("address_raw")) for loc in locations]
    jd = "\n\n".join(p for p in (description, requirements) if p)
    return fingerprint(title, location_places(title, pairs), jd)


def _apply_key(job: NormalizedJob) -> str | None:
    """Nơi nộp hồ sơ; nguồn không ghi thì là chính trang tin (ứng tuyển trên trang đó). Tin không
    có trang riêng (chung URL trang danh sách: tin trong JSON của trang SPA, tin LLM đọc từ trang
    danh sách) thì URL đó không nói gì về nơi nộp."""
    extra = job.raw.extra
    if extra.get("shared_url") or extra.get("inline"):
        return normalize_url(job.apply_url) if job.apply_url else None
    return normalize_url(job.apply_url or job.url)


class Signatures:
    """MinHash của JD, tính một lần cho mỗi nội dung JD. `load` / `save` đọc sẵn / ghi lại chữ ký
    trên Redis cache (dữ liệu mất được): lượt sau không tính lại JD không đổi. Gọi trong thread
    được (gom cụm), nhưng không song song với `load` / `save`."""

    def __init__(self) -> None:
        self._sigs: dict[str, MinHash] = {}
        self._fresh: dict[str, MinHash] = {}  # tính ở lượt này, chưa ghi cache
        self._jobs: dict[int, MinHash] = {}  # theo tin (id), khỏi băm lại JD mỗi lần so

    def __call__(self, job: NormalizedJob) -> MinHash:
        sig = self._jobs.get(id(job))
        if sig is None:
            sig = self._jobs[id(job)] = self.of_text(job.jd_text)
        return sig

    def of_text(self, text: str) -> MinHash:
        key = text_key(text)
        sig = self._sigs.get(key)
        if sig is None:
            sig = self._sigs[key] = self._fresh[key] = jd_minhash(text)
        return sig

    async def load(self, kv: KeyValueStore | None, texts: Iterable[str]) -> int:
        """Nạp chữ ký đã có trên cache của các JD `texts`; trả số chữ ký nạp được."""
        if kv is None:
            return 0
        keys = [k for k in dict.fromkeys(map(text_key, texts)) if k not in self._sigs]
        if not keys:
            return 0
        try:
            raws = await kv.mget([signature_cache_key(k) for k in keys])
        except Exception as exc:  # cache lỗi: tính lại, không làm hỏng lượt
            log.warning("dedup.signatures_load_failed", error=str(exc))
            return 0
        found = 0
        for key, raw in zip(keys, raws, strict=True):
            if raw and (sig := decode_signature(raw)) is not None:
                self._sigs[key] = sig
                found += 1
        return found

    async def save(self, kv: KeyValueStore | None, extra: Mapping[str, MinHash] = {}) -> None:
        """Ghi lên cache các chữ ký mới tính (và `extra`: khoá → chữ ký, vd. theo job đã lưu)."""
        items = {signature_cache_key(k): encode_signature(s) for k, s in self._fresh.items()}
        items |= {signature_cache_key(k): encode_signature(s) for k, s in extra.items()}
        self._fresh.clear()
        if kv is None or not items:
            return
        try:
            await kv.mset(items, ttl=SIGNATURE_TTL)
        except Exception as exc:
            log.warning("dedup.signatures_save_failed", error=str(exc))


def bands_exact(threshold: float) -> bool:
    """Hai JD có Jaccard (ước lượng MinHash) ≥ `threshold` lệch nhau ở tối đa ⌊128·(1 − t)⌋ hàm
    băm; ít hơn số băng thì có ít nhất một băng trùng trọn (chuồng bồ câu): `band_pairs` không bỏ
    sót cặp nào mà so từng cặp thấy giống."""
    return int(NUM_PERM * (1 - threshold) + 1e-9) < LSH_BANDS


def band_pairs(
    sigs: Sequence[MinHash], groups: Sequence[Hashable] | None = None
) -> set[tuple[int, int]]:
    """Cặp (i < j) có chữ ký trùng trọn ít nhất một băng LSH — gần tuyến tính thay vì so mọi cặp.
    Chữ ký rỗng (JD rỗng) không ghép với gì. `groups`: chỉ ghép phần tử cùng nhóm (vd. nguồn)."""
    same: dict[tuple[Hashable, bytes], list[int]] = {}
    for i, sig in enumerate(sigs):
        if not sig.is_empty():
            group = groups[i] if groups is not None else None
            same.setdefault((group, encode_signature(sig)), []).append(i)
    reps = list(same.items())  # các chữ ký khác nhau, kèm phần tử có chữ ký đó (tăng dần)
    pairs: set[tuple[int, int]] = set()
    buckets: dict[tuple[Hashable, int, bytes], list[int]] = {}
    for r, ((group, whole), members) in enumerate(reps):
        pairs.update(combinations(members, 2))
        step = len(whole) // LSH_BANDS
        for b in range(LSH_BANDS):
            buckets.setdefault((group, b, whole[b * step : (b + 1) * step]), []).append(r)
    linked: set[tuple[int, int]] = set()
    for members in buckets.values():
        linked.update(combinations(members, 2))
    for a, b in linked:
        for i in reps[a][1]:
            for j in reps[b][1]:
                pairs.add((i, j) if i < j else (j, i))
    return pairs


def candidate_pairs(
    jobs: Sequence[NormalizedJob], sigs: Signatures, *, jd_threshold: float = JD_THRESHOLD
) -> list[tuple[int, int]] | None:
    """Cặp tin có thể `same_job` (tăng dần, như vòng so mọi cặp): cùng mã tin, cùng nơi nộp, hoặc
    trùng một băng LSH của JD. None: ngưỡng JD quá thấp để băng LSH chắc chắn đủ, so mọi cặp."""
    if not bands_exact(min(jd_threshold, REPOST_JD_THRESHOLD)):
        return None
    pairs = band_pairs([sigs(job) for job in jobs])
    same: dict[tuple[str, str], list[int]] = {}
    for i, job in enumerate(jobs):
        same.setdefault(("id", f"{job.source}:{job.external_id}"), []).append(i)
        if (apply := _apply_key(job)) is not None:
            same.setdefault(("apply", apply), []).append(i)
    for members in same.values():
        pairs.update(combinations(members, 2))
    return sorted(pairs)


def same_job(
    a: NormalizedJob,
    b: NormalizedJob,
    sigs: Signatures,
    *,
    title_threshold: float = TITLE_THRESHOLD,
    jd_threshold: float = JD_THRESHOLD,
) -> bool:
    if a.source == b.source and a.external_id == b.external_id:
        return True  # cùng một tin, bóc hai lần
    if title_similarity(a.title_normalized, b.title_normalized) < title_threshold:
        return False
    if a.source == b.source and not a.places and not b.places:
        return is_repost(a.title, b.title, jd_similarity(sigs(a), sigs(b)))
    if not same_places(a, b):
        return False
    apply_a, apply_b = _apply_key(a), _apply_key(b)
    if apply_a and apply_a == apply_b:
        return True
    threshold = jd_threshold_for(a.title_normalized, b.title_normalized, jd_threshold)
    return jd_similarity(sigs(a), sigs(b)) >= threshold


def is_repost(title_a: str, title_b: str, jd: float) -> bool:
    """Hai tin cùng nguồn không ghi nơi làm là một tin đăng lại: tiêu đề gần như y hệt, không nêu
    hai nơi khác nhau, JD gần như trùng."""
    if title_places(title_a) != title_places(title_b):
        return False
    return title_similarity(fold(title_a), fold(title_b)) >= REPOST_TITLE_THRESHOLD and (
        jd >= REPOST_JD_THRESHOLD
    )


@dataclass(frozen=True, slots=True)
class ExistingJob:
    """Một job đã lưu, đủ trường để so với tin mới (xem `same_existing`)."""

    id: UUID
    cluster_key: str
    title: str
    title_normalized: str
    source: str
    places: frozenset[str]
    provinces: frozenset[str]


def same_existing(
    job: NormalizedJob, existing: ExistingJob, signature: MinHash, existing_signature: MinHash
) -> bool:
    """Tin mới là job đã lưu (đăng lại / đổi URL): cùng luật với `same_job`."""
    if title_similarity(job.title_normalized, existing.title_normalized) < TITLE_THRESHOLD:
        return False
    jd = jd_similarity(signature, existing_signature)
    if job.source == existing.source and not job.places and not existing.places:
        return is_repost(job.title, existing.title, jd)
    if job.source == existing.source:
        if not job.places & existing.places:
            return False
    elif not compatible_provinces(job.provinces, set(existing.provinces)):
        return False
    return jd >= jd_threshold_for(job.title_normalized, existing.title_normalized)


def canonical_rank(job: NormalizedJob) -> tuple[int, int, int, float]:
    """Nhỏ hơn = ưu tiên hơn: nguồn của doanh nghiệp, cách bóc tin cậy, JD dài, tin mới."""
    posted = (job.posted_at or _EPOCH).timestamp()
    return (1 if job.board else 0, _METHOD_RANK.get(job.raw.method, 9), -len(job.jd_text), -posted)


@dataclass(slots=True)
class Cluster:
    members: list[NormalizedJob]  # bản chuẩn đứng đầu

    @property
    def canonical(self) -> NormalizedJob:
        return self.members[0]

    @property
    def source_urls(self) -> list[str]:
        return list(dict.fromkeys(m.url for m in self.members))


def connected_groups(
    n: int,
    linked: Callable[[int, int], bool],
    can_join: Callable[[list[int], list[int]], bool] | None = None,
    *,
    pairs: Iterable[tuple[int, int]] | None = None,
) -> list[list[int]]:
    """Gom n phần tử theo quan hệ `linked` (union-find), giữ thứ tự. `can_join(nhóm a, nhóm b)`
    chặn việc nối hai nhóm đã có (tránh gộp bắc cầu qua một phần tử "khớp với mọi thứ").

    `pairs`: chỉ xét các cặp (i < j) này, tăng dần (`linked` chắc chắn sai với mọi cặp khác);
    None thì xét mọi cặp. Cùng thứ tự xét nên cùng kết quả."""
    parent = list(range(n))
    members = {i: [i] for i in range(n)}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if pairs is None:
        pairs = ((i, j) for i in range(n) for j in range(i + 1, n))
    for i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj or not linked(i, j):
            continue
        if can_join is not None and not can_join(members[ri], members[rj]):
            continue
        parent[rj] = ri
        members[ri].extend(members.pop(rj))
    return [sorted(g) for g in sorted(members.values(), key=min)]


def cluster_jobs(
    jobs: Sequence[NormalizedJob],
    *,
    sigs: Signatures | None = None,
    title_threshold: float = TITLE_THRESHOLD,
    jd_threshold: float = JD_THRESHOLD,
) -> list[Cluster]:
    """Gom các tin (đã nhận, cùng doanh nghiệp) thành cụm; thứ tự cụm theo tin đầu tiên. Chỉ so
    các cặp ứng viên (`candidate_pairs`), không so mọi cặp."""
    sigs = sigs or Signatures()

    def provinces(group: list[int]) -> set[str]:
        return set().union(*(jobs[i].provinces for i in group))

    groups = connected_groups(
        len(jobs),
        lambda i, j: same_job(
            jobs[i], jobs[j], sigs, title_threshold=title_threshold, jd_threshold=jd_threshold
        ),
        # Tin không rõ địa điểm khớp được với mọi nơi: không để nó nối tin Hà Nội với tin Đà Nẵng.
        lambda a, b: compatible_provinces(provinces(a), provinces(b)),
        pairs=candidate_pairs(jobs, sigs, jd_threshold=jd_threshold),
    )
    return [Cluster(sorted((jobs[i] for i in g), key=canonical_rank)) for g in groups]


def merge(cluster: Cluster) -> NormalizedJob:
    """Bản chuẩn, bổ sung trường còn thiếu từ các tin khác trong cụm."""
    best = cluster.canonical
    others = cluster.members[1:]
    if not others:
        return best

    def first[T](values: Sequence[T | None]) -> T | None:
        return next((v for v in values if v is not None), None)

    locations: list[Location] = []
    seen: set[tuple[str | None, str | None]] = set()
    for m in cluster.members:
        for loc in m.locations:
            key = (loc.province, fold(loc.address_raw or ""))
            if key not in seen:
                seen.add(key)
                locations.append(loc)
    skills = list(dict.fromkeys(s for m in cluster.members for s in m.skills))[:30]
    postings = [m.posted_at for m in cluster.members if m.posted_at]
    salary = best.salary
    if salary is None or (salary.min is None and salary.max is None):
        with_amount = [
            m.salary
            for m in others
            if m.salary is not None and (m.salary.min is not None or m.salary.max is not None)
        ]
        salary = first(with_amount) or salary
    return dataclasses.replace(
        best,
        level=best.level
        if best.level is not JobLevel.UNKNOWN
        else first([m.level for m in others if m.level is not JobLevel.UNKNOWN]) or best.level,
        employment_type=best.employment_type
        if best.employment_type is not EmploymentType.UNKNOWN
        else first(
            [m.employment_type for m in others if m.employment_type is not EmploymentType.UNKNOWN]
        )
        or best.employment_type,
        work_mode=best.work_mode
        if best.work_mode is not WorkMode.UNKNOWN
        else first([m.work_mode for m in others if m.work_mode is not WorkMode.UNKNOWN])
        or best.work_mode,
        categories=merge_categories(m.categories for m in cluster.members),
        education=best.education
        if best.education is not EducationLevel.UNKNOWN
        else first([m.education for m in others if m.education is not EducationLevel.UNKNOWN])
        or best.education,
        openings=best.openings or first([m.openings for m in others]),
        locations=locations,
        salary=salary,
        experience_years=best.experience_years or first([m.experience_years for m in others]),
        requirements_md=best.requirements_md or first([m.requirements_md for m in others]),
        benefits_md=best.benefits_md or first([m.benefits_md for m in others]),
        skills=skills,
        posted_at=min(postings) if postings else None,
        expires_at=best.expires_at or first([m.expires_at for m in others]),
        apply_url=best.apply_url or first([m.apply_url for m in others]),
    )
