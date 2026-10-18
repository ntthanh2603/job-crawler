"""Tin tuyển dụng trong dữ liệu JSON của trang careers dựng bằng JavaScript (SPA).

Nhiều trang careers (Ahamove, Shopee, GHN, VNG...) nạp danh sách tin bằng API JSON hoặc nhúng
sẵn trong `__NEXT_DATA__`; HTML chỉ có tiêu đề, không link, không mô tả. Module này (thuần, không
gọi mạng):

1. Tìm mảng tin trong JSON: phần tử có tên vị trí và thuộc tính của tin (nơi làm, lương, hạn
   nộp, loại hình, phòng ban...) kèm mô tả, ≥60% phần tử như vậy, đường dẫn API / khoá JSON có
   từ về việc làm (job, career, recruit, tuyen...). Danh mục `{code, name}`, bài blog
   `{title, content}` không qua.
2. Đổi mỗi phần tử thành `RawJob` (có mô tả) hoặc link tới trang tin (có URL / slug khớp link
   trên trang, chưa có mô tả).
3. Nhận ra cách phân trang của API (page/pageSize, offset/limit, trong query hoặc body JSON) để
   đọc tiếp các trang sau.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from jobcrawler.net.urls import normalize_url
from jobcrawler.pages import parse
from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.base import parse_datetime
from jobcrawler.sources.jsonld import external_id_for_url

KIND = "careers"
# Đường dẫn API / khoá JSON của dữ liệu việc làm.
_JOBISH = re.compile(
    r"job|career|recruit|tuyen|vacanc|position|opening|ats|viec|posting|hiring|requisition", re.I
)
_MIN_DESCRIPTION = 100  # ký tự: ngắn hơn thì chưa phải mô tả tin
_MIN_SHARE = 0.6  # tỉ lệ phần tử phải giống tin
_MAX_DEPTH = 7
# Ngày trước mốc này là giá trị rỗng của API ("0001-01-01T00:00:00Z"), không phải hạn nộp.
_MIN_YEAR = 2000

# Khoá (đã bỏ ký tự không phải chữ/số, viết thường) theo vai trò.
_TITLE_KEYS = (
    "title",
    "jobtitle",
    "jobname",
    "positionname",
    "positiontitle",
    "position",
    "vacancyname",
    "tieude",
    "tenvitri",
    "name",
)
_URL_KEYS = (
    "url",
    "joburl",
    "detailurl",
    "jobdetailurl",
    "link",
    "joblink",
    "hostedurl",
    "absoluteurl",
    "permalink",
    "shareurl",
    "href",
    "urlrecruit",
)
_SLUG_KEYS = ("slug", "alias", "urlslug", "seourl")
_ID_KEYS = ("id", "jobid", "jobcode", "code", "uuid", "requisitionid", "reqid", "key")
_DESCRIPTION = ("description", "content", "detail", "body", "mota", "overview", "summary", "jd")
_REQUIREMENTS = ("requirement", "qualification", "yeucau")
_BENEFITS = ("benefit", "welfare", "perk", "quyenloi", "phucloi")
_LOCATION = ("location", "address", "city", "province", "region", "workplace", "diadiem", "area")
_SALARY = ("salary", "luong", "compensation")
_DEADLINE = ("endtime", "deadline", "expire", "closingdate", "validthrough", "hannop", "enddate")
_POSTED = ("postingtime", "postedat", "publishedat", "approvedon", "createdat", "createddate")
_EMPLOYMENT = ("employment", "workingtype", "worktype", "contracttype", "typeofwork", "hinhthuc")
_ORG = ("department", "team", "jobfield", "jobfamily", "level", "seniority", "capbac", "openings")
_ATTRIBUTE_GROUPS = (_LOCATION, _SALARY, _DEADLINE, _EMPLOYMENT, _ORG)

# Tham số phân trang của API.
_PAGE_KEYS = ("page", "pageindex", "pagenumber", "pageno", "currentpage", "p")
_OFFSET_KEYS = ("offset", "start", "skip", "from")
_SIZE_KEYS = ("pagesize", "limit", "size", "perpage", "rows", "hitsperpage")
_TOTAL_KEYS = ("total", "totalcount", "totalitems", "totalrecords", "recordstotal", "totalelements")
_PAGES_KEYS = ("totalpages", "pages", "lastpage", "nbpages", "pagecount")


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _text(value: Any) -> str | None:
    """Giá trị thành chữ: chuỗi, số, object `{label|name|title: ...}`, danh sách các thứ đó."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("label", "name", "title", "text", "display", "value"):
            if isinstance(found := value.get(key), str) and found.strip():
                return found.strip()
        return None
    if isinstance(value, list):
        parts = [t for x in value if (t := _text(x))]
        return ", ".join(dict.fromkeys(parts)) or None
    return None


def _fields(item: Mapping[str, Any], words: Sequence[str]) -> Iterator[tuple[str, Any]]:
    """(khoá chuẩn hoá, giá trị) của các trường có tên chứa một trong `words` (bỏ trường mã số
    `*_id`, giá trị rỗng)."""
    for key, value in item.items():
        norm = _norm(key)
        if norm.endswith("id") and norm != "id":
            continue
        if value in (None, "", [], {}):
            continue
        if any(w in norm for w in words):
            yield norm, value


def _has(item: Mapping[str, Any], words: Sequence[str]) -> bool:
    """Có trường thuộc nhóm (kể cả trường mã số `city_id`: vẫn là thuộc tính của tin)."""
    return any(
        any(w in _norm(key) for w in words) and value not in (None, "", [], {})
        for key, value in item.items()
    )


def _title(item: Mapping[str, Any]) -> str | None:
    by_key = {_norm(k): v for k, v in item.items()}
    for key in _TITLE_KEYS:
        if isinstance(value := by_key.get(key), str) and 2 <= len(value.strip()) <= 200:
            return " ".join(value.split())
    # Tên vị trí trong object lồng: GHN `selectedListRecruit: {value, label}`.
    for key, value in item.items():
        if isinstance(value, dict) and re.search(r"recruit|job|position|title", key, re.I):
            text = _text(value)
            if text and not text.isdigit() and 2 <= len(text) <= 200:
                return " ".join(text.split())
    return None


def _longest(item: Mapping[str, Any], words: Sequence[str]) -> str | None:
    texts = [t for _, v in _fields(item, words) if isinstance(v, str) and (t := v.strip())]
    return max(texts, key=len) if texts else None


def _description(item: Mapping[str, Any]) -> str | None:
    """Mô tả: trường mô tả dài nhất (không tính trường yêu cầu / quyền lợi)."""
    texts = [
        t
        for key, v in _fields(item, _DESCRIPTION)
        if isinstance(v, str)
        and not any(w in key for w in (*_REQUIREMENTS, *_BENEFITS))
        and (t := v.strip())
    ]
    return max(texts, key=len) if texts else None


def _job_like(item: Mapping[str, Any]) -> bool:
    if _title(item) is None:
        return False
    groups = sum(_has(item, words) for words in _ATTRIBUTE_GROUPS)
    described = len(_description(item) or "") >= _MIN_DESCRIPTION
    # Bài blog / câu chuyện nhân viên có tiêu đề + nội dung nhưng không có thuộc tính tin nào.
    return groups >= 1 and (described or groups >= 2)


@dataclass(frozen=True, slots=True)
class JsonDoc:
    """JSON thấy trên trang: phản hồi API bắt được khi render, hoặc nhúng trong HTML."""

    url: str  # URL API; JSON nhúng: URL trang
    data: Any
    method: str = "GET"
    request_json: Any = None
    embedded: bool = False


@dataclass(frozen=True, slots=True)
class JobArray:
    path: str  # $.data.list
    items: list[dict[str, Any]]


def job_arrays(doc: JsonDoc) -> list[JobArray]:
    """Mảng tin trong JSON (nhiều phần tử giống tin nhất trước)."""
    api = urlsplit(doc.url).path
    found: list[JobArray] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}", depth + 1)
            return
        if not isinstance(node, list) or not node:
            return
        items = [x for x in node if isinstance(x, dict)]
        if len(items) == len(node) and _JOBISH.search(f"{api} {path}"):
            good = [x for x in items if _job_like(x)]
            if good and len(good) >= _MIN_SHARE * len(items):
                found.append(JobArray(path, items))
                return
        for i, value in enumerate(node[:3]):
            walk(value, f"{path}[{i}]", depth + 1)

    walk(doc.data, "$", 0)
    return sorted(found, key=lambda a: -len(a.items))


def embedded_json(html: str) -> list[Any]:
    """JSON nhúng trong HTML: `<script id="__NEXT_DATA__">`, `<script type="application/json">`."""
    found: list[Any] = []
    texts: set[str] = set()
    for node in parse(html).css('script#__NEXT_DATA__, script[type="application/json"]'):
        text = node.text().strip()
        if len(text) < 50 or text in texts:
            continue
        texts.add(text)
        try:
            found.append(json.loads(text))
        except ValueError:
            continue
    return found


def _date(value: Any, *, end_of_day: bool) -> datetime | None:
    parsed = parse_datetime(value, end_of_day=end_of_day) if value is not None else None
    return parsed if parsed is not None and parsed.year >= _MIN_YEAR else None


def _first(item: Mapping[str, Any], words: Sequence[str]) -> Any:
    return next((v for _, v in _fields(item, words)), None)


def _named(item: Mapping[str, Any], words: Sequence[str]) -> str | None:
    """Chữ của trường thuộc nhóm, ưu tiên bản tên hiển thị (`departmentName` hơn `jobField`)."""
    found = list(_fields(item, words))
    shown = [v for k, v in found if k.endswith(("name", "label", "text"))]
    return _text(shown[0] if shown else found[0][1]) if found else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        number = float(value)
        return number if number > 0 else None
    return None


def _item_id(item: Mapping[str, Any]) -> str | None:
    by_key = {_norm(k): v for k, v in item.items()}
    for key in _ID_KEYS:
        value = by_key.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return None


def _item_url(item: Mapping[str, Any], *, page_url: str, links: Mapping[str, str]) -> str | None:
    """URL trang tin: trường URL của phần tử, không thì link trên trang có đúng slug của nó."""
    by_key = {_norm(k): v for k, v in item.items()}
    for key in _URL_KEYS:
        value = by_key.get(key)
        if isinstance(value, str) and value.strip() and not value.startswith(("mailto:", "#")):
            url = normalize_url(urljoin(page_url, value.strip()))
            if url and url.startswith("http"):
                return url
    for key in _SLUG_KEYS:
        slug = by_key.get(key)
        if isinstance(slug, str) and len(slug.strip()) >= 3:
            want = slug.strip().strip("/")
            for url in links:
                if urlsplit(url).path.rstrip("/").endswith("/" + want):
                    return url
    return None


IdNames = Mapping[tuple[str, str], str]


def id_names(docs: Sequence[JsonDoc]) -> dict[tuple[str, str], str]:
    """Tên theo mã từ các danh mục trang tải kèm: `{region_id: 32, region_name: "Vietnam",
    city_id: 33, city_name: "Hanoi"}` → {("region", "32"): "Vietnam", ("city", "33"): "Hanoi"}.
    Dùng để đọc `city_id` của tin thành tên nơi làm."""
    found: dict[tuple[str, str], str] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
        elif isinstance(node, dict):
            keys = {_norm(k): v for k, v in node.items()}
            for key, value in keys.items():
                if not key.endswith("id") or len(key) <= 2:
                    continue
                name = keys.get(key[:-2] + "name")
                if isinstance(name, str) and name.strip() and isinstance(value, str | int):
                    found.setdefault((key[:-2], str(value)), name.strip())
            for value in node.values():
                if isinstance(value, dict | list):
                    walk(value, depth + 1)

    for doc in docs:
        walk(doc.data, 0)
    return found


def _id_locations(item: Mapping[str, Any], names: IdNames) -> list[str]:
    """Nơi làm ghi bằng mã (`city_id`, `region_id`) → tên theo danh mục."""
    found: list[str] = []
    for key, value in item.items():
        norm = _norm(key)
        if not norm.endswith("id") or not any(w in norm for w in _LOCATION):
            continue
        if isinstance(value, str | int) and (name := names.get((norm[:-2], str(value)))):
            found.append(name)
    return found


@dataclass(slots=True)
class Extracted:
    jobs: list[RawJob] = field(default_factory=list)  # có mô tả: không cần trang chi tiết
    links: list[tuple[str, str]] = field(default_factory=list)  # (URL trang tin, tên vị trí)


def to_job(
    item: Mapping[str, Any],
    *,
    doc: JsonDoc,
    page_url: str,
    links: Mapping[str, str],
    names: IdNames | None = None,
) -> RawJob | tuple[str, str] | None:
    """Một phần tử → `RawJob` (đủ mô tả), (URL, tên vị trí) (chỉ có link), hoặc None."""
    title = _title(item)
    if title is None:
        return None
    url = _item_url(item, page_url=page_url, links=links)
    description = _description(item)
    requirements = _longest(item, _REQUIREMENTS)
    benefits = _longest(item, _BENEFITS)
    jd_length = len(description or "") + len(requirements or "")
    if jd_length < _MIN_DESCRIPTION:
        return (url, title) if url else None
    if url:
        # Cùng mã với tin bóc từ trang chi tiết: hai đường gặp nhau thì là một tin.
        external_id = external_id_for_url(url)
    else:
        api = urlunsplit(urlsplit(doc.url)._replace(query="", fragment=""))
        external_id = external_id_for_url(api, f"id:{_item_id(item) or title}")
    salaries = list(_fields(item, _SALARY))
    # Lương dạng chữ ("13 - 18 triệu/tháng"); số tách riêng thì vào min/max.
    salary_raw = next(
        (t for _, v in salaries if (t := _text(v)) and not re.fullmatch(r"[\d.]+", t)), None
    )
    low = next((_number(v) for k, v in salaries if "from" in k or "min" in k), None)
    high = next((_number(v) for k, v in salaries if k.startswith("to") or "max" in k), None)
    locations = [t for _, v in _fields(item, _LOCATION) if (t := _text(v)) and not t.isdigit()]
    # Chỉ có mã nơi làm: tên theo danh mục, thành phố trước, quốc gia sau ("Hanoi, Vietnam").
    if not locations and names and (coded := _id_locations(item, names)):
        locations = [", ".join(reversed(coded))]
    deadline = _first(item, _DEADLINE)
    posted = _first(item, _POSTED)
    employment = _first(item, _EMPLOYMENT)
    if employment is None and isinstance(item.get("type"), dict):
        employment = item["type"]  # GHN: type = {value: fulltime, label: Toàn thời gian}
    return RawJob(
        source=KIND,
        external_id=external_id,
        url=url or page_url,
        title=title,
        description=description,
        description_format="html",
        requirements=requirements,
        benefits=benefits,
        locations=list(dict.fromkeys(locations))[:10],
        employment_type=_named(item, _EMPLOYMENT) or _text(employment),
        department=_named(item, ("department", "jobfield", "jobfamily", "team")),
        level=_named(item, ("level", "seniority", "capbac")),
        salary_raw=salary_raw,
        salary_min=low,
        salary_max=high,
        posted_at=_date(posted, end_of_day=False),
        posted_raw=_text(posted),
        expires_at=_date(deadline, end_of_day=True),
        expires_raw=_text(deadline) if _date(deadline, end_of_day=True) else None,
        apply_url=url,
        method=ExtractionMethod.PARSER,
        # Không có trang riêng: nhiều tin chung URL trang danh sách (dedup không coi là cùng
        # nơi nộp hồ sơ).
        extra={"json": doc.url, **({} if url else {"shared_url": True})},
    )


def extract(
    doc: JsonDoc, *, page_url: str, links: Mapping[str, str], names: IdNames | None = None
) -> Extracted:
    """Tin và link tin trong một tài liệu JSON (`names`: tên theo mã, xem `id_names`)."""
    out = Extracted()
    seen: set[str] = set()
    for array in job_arrays(doc):
        for item in array.items:
            found = to_job(item, doc=doc, page_url=page_url, links=links, names=names)
            if isinstance(found, RawJob):
                if found.external_id not in seen:
                    seen.add(found.external_id)
                    out.jobs.append(found)
            elif found is not None and found[0] not in seen:
                seen.add(found[0])
                out.links.append(found)
    return out


# --- phân trang -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pager:
    """Cách lấy các trang sau của một API (hoặc trang HTML có JSON nhúng)."""

    url: str
    method: str
    body: Any
    key: str  # tên tham số, đúng như trong request
    kind: Literal["page", "offset"]
    current: int  # trang hiện tại / offset hiện tại
    size: int  # số tin mỗi trang
    total: int | None  # tổng số tin nếu API cho biết
    in_body: bool = False
    embedded: bool = False

    def request(self, step: int) -> tuple[str, Any]:
        """(URL, body) của trang thứ `step` sau trang đã đọc."""
        value = self.current + step if self.kind == "page" else self.current + step * self.size
        if self.in_body and isinstance(self.body, dict):
            return self.url, _set_in_body(self.body, self.key, value)
        parts = urlsplit(self.url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != self.key]
        query.append((self.key, str(value)))
        return urlunsplit(parts._replace(query=urlencode(query))), self.body

    def done(self, read: int) -> bool:
        """Đã đọc đủ `read` tin (theo tổng API báo)."""
        return self.total is not None and read >= self.total


def _set_in_body(body: dict[str, Any], key: str, value: int) -> dict[str, Any]:
    if key in body:
        return {**body, key: value}
    for name, nested in body.items():
        if isinstance(nested, dict) and key in nested:
            return {**body, name: {**nested, key: value}}
    return body


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _find_param(params: Mapping[str, Any], names: Sequence[str]) -> tuple[str, int] | None:
    by_norm = {_norm(k): k for k in params}
    for name in names:
        if (key := by_norm.get(name)) is not None and (value := _int(params[key])) is not None:
            return key, value
    return None


def _body_params(body: Any) -> dict[str, Any]:
    """Tham số trong body JSON: tầng ngoài và một tầng lồng."""
    if not isinstance(body, dict):
        return {}
    params = {k: v for k, v in body.items() if not isinstance(v, dict | list)}
    for value in body.values():
        if isinstance(value, dict):
            params |= {k: v for k, v in value.items() if not isinstance(v, dict | list)}
    return params


def _layers(data: Any, depth: int = 3) -> list[dict[str, Any]]:
    """Các object từ ngoài vào trong, tối đa `depth` tầng (`$`, `$.data`, `$.data.meta`...)."""
    layers: list[dict[str, Any]] = []
    current = [data] if isinstance(data, dict) else []
    for _ in range(depth):
        layers += current
        current = [v for layer in current for v in layer.values() if isinstance(v, dict)]
    return layers


def _total(data: Any, size: int) -> int | None:
    """Tổng số tin API báo (`total`, `total_count`...; hoặc số trang × cỡ trang)."""
    layers = _layers(data)
    for layer in layers:
        if (found := _find_param(layer, _TOTAL_KEYS)) is not None:
            return found[1]
    for layer in layers:
        if (found := _find_param(layer, _PAGES_KEYS)) is not None:
            return found[1] * size
    return None


def pager_for(doc: JsonDoc, array: JobArray) -> Pager | None:
    """Cách đọc trang sau của `doc` (None: không nhận ra tham số phân trang)."""
    size = len(array.items)
    if doc.embedded:
        # Trang HTML có JSON nhúng (Next.js): `?page=N` trên chính trang, nếu dữ liệu báo còn.
        total = _total(doc.data, size)
        query = dict(parse_qsl(urlsplit(doc.url).query))
        current = _find_param(query, _PAGE_KEYS)
        if total is None or total <= size:
            return None
        key, page = current or ("page", 1)
        return Pager(doc.url, "GET", None, key, "page", page, size, total, embedded=True)
    query = dict(parse_qsl(urlsplit(doc.url).query))
    body = _body_params(doc.request_json)
    for params, in_body in ((query, False), (body, True)):
        size_param = _find_param(params, _SIZE_KEYS)
        page_size = size_param[1] if size_param else size
        if (found := _find_param(params, _PAGE_KEYS)) is not None:
            key, page = found
            total = _total(doc.data, page_size)
            return Pager(
                doc.url, doc.method, doc.request_json, key, "page", page, page_size, total, in_body
            )
        if (found := _find_param(params, _OFFSET_KEYS)) is not None:
            key, offset = found
            total = _total(doc.data, page_size)
            return Pager(
                doc.url,
                doc.method,
                doc.request_json,
                key,
                "offset",
                offset,
                page_size,
                total,
                in_body,
            )
    return None


def with_doc(pager: Pager, doc: JsonDoc) -> Pager:
    """Pager cập nhật tổng số tin từ trang mới đọc (API có thể chỉ báo ở trang đầu)."""
    total = _total(doc.data, pager.size)
    return replace(pager, total=total if total is not None else pager.total)
