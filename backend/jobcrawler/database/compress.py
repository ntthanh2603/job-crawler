"""Nén zstd cho các cột lớn: `job_postings.raw`, mô tả job, HTML trang parse lỗi.

Postgres tự nén (pglz) giá trị trên ~2 KB nhưng với tin tuyển dụng chỉ được ~1,8 lần, còn giá
trị nhỏ hơn thì để nguyên. zstd kèm từ điển huấn luyện trên chính dữ liệu (tin cùng trang có
chung khung HTML, nhãn, câu chữ quen thuộc) nén được 4–6 lần, kể cả giá trị vài trăm byte.

Một giá trị đã nén: 1 byte phiên bản (0x01) + 4 byte id từ điển (big-endian, 0 = không từ điển;
là `dict_id` ngẫu nhiên gán lúc huấn luyện) + một frame zstd (có kích thước gốc, không ghi dict id
và checksum). Cột để `STORAGE EXTERNAL` để Postgres không nén lại lần nữa và đọc được 5 byte đầu
mà không phải đọc cả giá trị (tìm dòng cần nén lại bằng từ điển mới).

Từ điển nằm ở bảng `compression_dicts`, được nạp vào `CODEC` khi mở tài nguyên và làm mới mỗi
COMPRESSION_DICT_REFRESH_SECONDS. Từ điển mới chỉ được dùng để nén sau `usable_at` (= lúc tạo +
COMPRESSION_DICT_DELAY_SECONDS, dài hơn chu kỳ làm mới), nên mọi process đang chạy đã có nó trước
khi gặp giá trị nén bằng nó. Từ điển cũ giữ lại mãi (mỗi cái vài chục KB) để đọc dữ liệu cũ.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import zstandard as zstd
from sqlalchemy import LargeBinary, bindparam, text
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.types import TypeDecorator

from jobcrawler.common.logging import get_logger
from jobcrawler.database.ids import NIL

log = get_logger(__name__)

VERSION = 1
HEADER_SIZE = 5


class Kind(StrEnum):
    """Loại dữ liệu; mỗi loại một bộ từ điển riêng."""

    POSTING = "posting"  # job_postings.raw (JSON RawJob)
    JOB_TEXT = "job_text"  # jobs.description_md / requirements_md / benefits_md
    HTML = "html"  # failed_pages.html_zstd


# Bảng và các cột nén của mỗi loại.
COLUMNS: dict[Kind, tuple[str, tuple[str, ...]]] = {
    Kind.POSTING: ("job_postings", ("raw",)),
    Kind.JOB_TEXT: ("jobs", ("description_md", "requirements_md", "benefits_md")),
    Kind.HTML: ("failed_pages", ("html_zstd",)),
}


class CorruptValueError(ValueError):
    """Giá trị không đúng định dạng nén."""


class UnknownDictionaryError(LookupError):
    """Giá trị nén bằng từ điển chưa nạp (process chưa làm mới từ điển?)."""


@dataclass(frozen=True, slots=True)
class Dictionary:
    id: int
    kind: str
    data: bytes
    usable_at: float  # epoch giây: từ lúc này mới dùng để nén


def header(dict_id: int) -> bytes:
    return bytes([VERSION]) + dict_id.to_bytes(4, "big")


def dict_id_of(blob: bytes | memoryview) -> int:
    if len(blob) < HEADER_SIZE or blob[0] != VERSION:
        raise CorruptValueError("giá trị không phải định dạng nén v1")
    return int.from_bytes(blob[1:HEADER_SIZE], "big")


def content_size(blob: bytes | memoryview) -> int | None:
    """Kích thước gốc ghi trong frame (chỉ cần phần đầu giá trị, khoảng 20 byte)."""
    try:
        size = zstd.frame_content_size(bytes(blob[HEADER_SIZE:]))
    except zstd.ZstdError:
        return None
    return size if size >= 0 else None


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class Codec:
    """Nén/giải nén theo loại dữ liệu với từ điển đã nạp.

    Không thread-safe (compressor zstd giữ trạng thái): chỉ dùng trong event loop; việc nặng ở
    thread khác (huấn luyện) tự tạo compressor riêng."""

    def __init__(self, level: int = 9) -> None:
        self.level = level
        self._dicts: dict[int, Dictionary] = {}
        self._zdicts: dict[int, zstd.ZstdCompressionDict] = {}
        self._compressors: dict[int, zstd.ZstdCompressor] = {}
        self._decompressors: dict[int, zstd.ZstdDecompressor] = {}

    def set_level(self, level: int) -> None:
        if level != self.level:
            self.level = level
            self._compressors.clear()

    @property
    def ids(self) -> set[int]:
        return set(self._dicts)

    @property
    def dictionaries(self) -> list[Dictionary]:
        return sorted(self._dicts.values(), key=lambda d: (d.kind, d.usable_at, d.id))

    def add(self, dictionary: Dictionary) -> None:
        if dictionary.id == 0:
            raise ValueError("id từ điển 0 dành cho 'không từ điển'")
        if dictionary.id in self._dicts:
            return
        self._dicts[dictionary.id] = dictionary
        self._zdicts[dictionary.id] = zstd.ZstdCompressionDict(dictionary.data)

    def clear(self) -> None:
        self._dicts.clear()
        self._zdicts.clear()
        self._compressors.clear()
        self._decompressors.clear()

    def active(self, kind: str, now: float | None = None) -> Dictionary | None:
        """Từ điển dùng để nén `kind` lúc `now`: cái dùng được mới nhất."""
        now = time.time() if now is None else now
        usable = [d for d in self._dicts.values() if d.kind == kind and d.usable_at <= now]
        return max(usable, key=lambda d: (d.usable_at, d.id), default=None)

    def _compressor(self, dict_id: int) -> zstd.ZstdCompressor:
        compressor = self._compressors.get(dict_id)
        if compressor is None:
            compressor = new_compressor(self.level, self._zdicts[dict_id] if dict_id else None)
            self._compressors[dict_id] = compressor
        return compressor

    def encode(self, kind: str, data: bytes, *, dict_id: int | None = None) -> bytes:
        if dict_id is None:
            active = self.active(kind)
            dict_id = active.id if active else 0
        return header(dict_id) + self._compressor(dict_id).compress(data)

    def decode(self, blob: bytes | memoryview) -> bytes:
        dict_id = dict_id_of(blob)
        decompressor = self._decompressors.get(dict_id)
        if decompressor is None:
            if dict_id and dict_id not in self._zdicts:
                raise UnknownDictionaryError(
                    f"chưa nạp từ điển nén {dict_id} (bảng compression_dicts)"
                )
            decompressor = (
                zstd.ZstdDecompressor(dict_data=self._zdicts[dict_id])
                if dict_id
                else zstd.ZstdDecompressor()
            )
            self._decompressors[dict_id] = decompressor
        try:
            return decompressor.decompress(memoryview(blob)[HEADER_SIZE:])
        except zstd.ZstdError as exc:
            raise CorruptValueError(f"không giải nén được: {exc}") from exc


def new_compressor(level: int, zdict: zstd.ZstdCompressionDict | None) -> zstd.ZstdCompressor:
    params: dict[str, Any] = {
        "level": level,
        "write_checksum": False,
        "write_content_size": True,
        "write_dict_id": False,
    }
    if zdict is not None:
        params["dict_data"] = zdict
    return zstd.ZstdCompressor(**params)


# Codec dùng chung của process (các TypeDecorator bên dưới gọi vào đây).
CODEC = Codec()


# --- kiểu cột SQLAlchemy -----------------------------------------------------------------------


class ZstdJSON(TypeDecorator[Any]):
    """JSON (dict/list) lưu thành bytea đã nén."""

    impl = LargeBinary
    cache_ok = True

    def __init__(self, kind: Kind) -> None:
        super().__init__()
        self.kind = kind

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        return None if value is None else CODEC.encode(self.kind, dumps(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else json.loads(CODEC.decode(value))


class ZstdText(TypeDecorator[str]):
    """Chuỗi lưu thành bytea đã nén."""

    impl = LargeBinary
    cache_ok = True

    def __init__(self, kind: Kind) -> None:
        super().__init__()
        self.kind = kind

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        return None if value is None else CODEC.encode(self.kind, str(value).encode())

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else CODEC.decode(value).decode()


# --- nạp từ điển ---------------------------------------------------------------------------------

_SELECT_DICTS = text(
    "SELECT id, kind, data, extract(epoch FROM usable_at) FROM compression_dicts"
    " WHERE NOT (id = ANY(:known))"
)


def _add_rows(codec: Codec, rows: Iterable[Sequence[Any]]) -> int:
    added = 0
    for dict_id, kind, data, usable_at in rows:
        codec.add(Dictionary(int(dict_id), str(kind), bytes(data), float(usable_at)))
        added += 1
    return added


def load_dictionaries_sync(conn: Connection, codec: Codec = CODEC) -> int:
    """Như `load_dictionaries`, cho kết nối đồng bộ (migration)."""
    rows = conn.execute(_SELECT_DICTS, {"known": sorted(codec.ids)}).all()
    return _add_rows(codec, rows)


async def load_dictionaries(conn: AsyncConnection, codec: Codec = CODEC) -> int:
    """Nạp từ điển chưa có vào `codec`; trả số từ điển mới."""
    rows = (await conn.execute(_SELECT_DICTS, {"known": sorted(codec.ids)})).all()
    return _add_rows(codec, rows)


class DictionaryRefresher:
    """Nạp từ điển lúc khởi động rồi làm mới định kỳ (task nền của process)."""

    def __init__(self, engine: AsyncEngine, *, interval: float, codec: Codec = CODEC) -> None:
        self._engine = engine
        self._interval = interval
        self._codec = codec
        self._task: asyncio.Task[None] | None = None

    async def refresh(self) -> int:
        async with self._engine.connect() as conn:
            try:
                added = await load_dictionaries(conn, self._codec)
            except ProgrammingError as exc:
                if "compression_dicts" not in str(exc):
                    raise
                log.warning("compress.no_table", reason="chưa `jobcrawler db upgrade`")
                return 0
        if added:
            log.info("compress.dicts_loaded", added=added, total=len(self._codec.ids))
        return added

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="compress-dict-refresh")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.refresh()
            except Exception as exc:  # DB chập chờn: thử lại lượt sau
                log.warning("compress.refresh_failed", error=str(exc))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


# --- huấn luyện từ điển --------------------------------------------------------------------------


@dataclass(slots=True)
class TrainResult:
    kind: str
    samples: int = 0
    dict_id: int | None = None
    # Tổng kích thước gốc / tổng kích thước nén trên phần mẫu để kiểm tra.
    ratio_before: float | None = None
    ratio_after: float | None = None
    adopted: bool = False
    reason: str = ""


def _ratio(samples: Sequence[bytes], compressor: zstd.ZstdCompressor) -> float:
    original = sum(len(s) for s in samples)
    packed = sum(len(compressor.compress(s)) + HEADER_SIZE for s in samples)
    return original / packed if packed else 1.0


def train(
    kind: str,
    samples: Sequence[bytes],
    *,
    dict_size: int,
    level: int,
    current: bytes | None,
    taken: set[int],
    min_gain: float = 0.05,
    seed: int | None = None,
) -> tuple[Dictionary | None, TrainResult]:
    """Huấn luyện từ điển trên 80% mẫu, so tỉ lệ nén trên 20% còn lại với từ điển đang dùng
    (`current`, None = không từ điển). Chỉ nhận khi tốt hơn ít nhất `min_gain`.

    Hàm thuần CPU (chạy ở thread riêng được): tự tạo compressor, không đụng `CODEC`."""
    result = TrainResult(kind=kind, samples=len(samples))
    pool = [s for s in samples if s]
    random.Random(seed).shuffle(pool)
    cut = max(1, len(pool) * 4 // 5)
    train_set, test_set = pool[:cut], pool[cut:] or pool[:cut]
    if sum(len(s) for s in train_set) < dict_size * 10:
        result.reason = "mẫu quá ít so với cỡ từ điển"
        return None, result
    dict_id = 0
    while dict_id == 0 or dict_id in taken:
        dict_id = secrets.randbits(31)
    try:
        zdict = zstd.train_dictionary(dict_size, list[Any](train_set), dict_id=dict_id, level=level)
    except zstd.ZstdError as exc:
        result.reason = f"huấn luyện lỗi: {exc}"
        return None, result
    before = new_compressor(level, zstd.ZstdCompressionDict(current) if current else None)
    result.ratio_before = round(_ratio(test_set, before), 2)
    result.ratio_after = round(_ratio(test_set, new_compressor(level, zdict)), 2)
    if result.ratio_after < result.ratio_before * (1 + min_gain):
        result.reason = "không tốt hơn từ điển đang dùng"
        return None, result
    result.dict_id = dict_id
    result.adopted = True
    return Dictionary(dict_id, kind, zdict.as_bytes(), usable_at=0.0), result


async def sample_values(session: AsyncSession, kind: Kind, limit: int) -> list[bytes]:
    """Lấy ngẫu nhiên tối đa `limit` giá trị (đã giải nén) của một loại."""
    table, columns = COLUMNS[kind]
    picked = ", ".join(columns)
    rows = await session.execute(
        text(
            f"SELECT {picked} FROM {table} WHERE id IN "
            f"(SELECT id FROM {table} ORDER BY random() LIMIT :n)"
        ),
        {"n": max(1, limit // len(columns))},
    )
    out: list[bytes] = []
    for row in rows:
        for blob in row:
            if blob is not None:
                try:
                    out.append(CODEC.decode(blob))
                except (CorruptValueError, UnknownDictionaryError):
                    continue
    return out[:limit]


async def count_values(session: AsyncSession, kind: Kind) -> int:
    table, columns = COLUMNS[kind]
    nonnull = " + ".join(f"count({c})" for c in columns)
    return int(await session.scalar(text(f"SELECT {nonnull} FROM {table}")) or 0)


@dataclass(frozen=True, slots=True)
class TrainOptions:
    dict_size: int = 64 * 1024
    level: int = 9
    min_samples: int = 1000
    max_samples: int = 5000
    delay_seconds: float = 900
    retrain_days: float = 30


async def train_dictionaries(
    sessionmaker: async_sessionmaker[AsyncSession],
    options: TrainOptions,
    *,
    now: datetime,
    kinds: Iterable[Kind] = tuple(Kind),
    codec: Codec = CODEC,
) -> list[TrainResult]:
    """Huấn luyện từ điển cho loại chưa có (hoặc có nhưng đã cũ) và đủ dữ liệu."""
    results: list[TrainResult] = []
    for kind in kinds:
        async with sessionmaker() as session:
            await load_dictionaries(await session.connection(), codec)
            newest = await session.scalar(
                text("SELECT max(created_at) FROM compression_dicts WHERE kind = :k"),
                {"k": kind.value},
            )
            if newest is not None and now - newest < timedelta(days=options.retrain_days):
                results.append(TrainResult(kind=kind, reason="từ điển còn mới"))
                continue
            total = await count_values(session, kind)
            if total < options.min_samples:
                results.append(
                    TrainResult(kind=kind, samples=total, reason="chưa đủ dữ liệu để huấn luyện")
                )
                continue
            samples = await sample_values(session, kind, options.max_samples)
        current = codec.active(kind.value)
        dictionary, result = await asyncio.to_thread(
            train,
            kind.value,
            samples,
            dict_size=options.dict_size,
            level=options.level,
            current=current.data if current else None,
            taken=codec.ids,
        )
        results.append(result)
        if dictionary is None:
            log.info("compress.train_skipped", kind=kind.value, reason=result.reason)
            continue
        usable_at = now + timedelta(seconds=options.delay_seconds)
        async with sessionmaker.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO compression_dicts (id, kind, data, samples, created_at, usable_at)"
                    " VALUES (:id, :kind, :data, :samples, :created_at, :usable_at)"
                ),
                {
                    "id": dictionary.id,
                    "kind": kind.value,
                    "data": dictionary.data,
                    "samples": len(samples),
                    "created_at": now,
                    "usable_at": usable_at,
                },
            )
        codec.add(Dictionary(dictionary.id, kind.value, dictionary.data, usable_at.timestamp()))
        log.info(
            "compress.trained",
            kind=kind.value,
            dict_id=dictionary.id,
            samples=len(samples),
            ratio_before=result.ratio_before,
            ratio_after=result.ratio_after,
            usable_at=usable_at.isoformat(),
        )
    return results


# --- nén lại bằng từ điển mới -------------------------------------------------------------------


async def recompress(
    sessionmaker: async_sessionmaker[AsyncSession],
    kind: Kind,
    *,
    limit: int,
    batch: int = 500,
    codec: Codec = CODEC,
) -> int:
    """Nén lại tối đa `limit` dòng chưa dùng từ điển hiện hành của `kind`; trả số dòng đã ghi.

    Chỉ ghi khi giá trị trong DB vẫn là giá trị đã đọc (không đè dữ liệu worker vừa cập nhật)."""
    active = codec.active(kind.value)
    if active is None or limit <= 0:
        return 0
    target = header(active.id)
    table, columns = COLUMNS[kind]
    stale = " OR ".join(f"substring({c} FROM 1 FOR {HEADER_SIZE}) <> :hdr" for c in columns)
    select_sql = text(
        f"SELECT id, {', '.join(columns)} FROM {table}"
        f" WHERE id > :last AND ({stale}) ORDER BY id LIMIT :n"
    )
    sets = ", ".join(f"{c} = :new_{c}" for c in columns)
    guards = " AND ".join(f"{c} IS NOT DISTINCT FROM :old_{c}" for c in columns)
    update_sql = text(f"UPDATE {table} SET {sets} WHERE id = :id AND {guards}").bindparams(
        *(bindparam(f"new_{c}", type_=LargeBinary) for c in columns),
        *(bindparam(f"old_{c}", type_=LargeBinary) for c in columns),
    )
    written = 0
    last = NIL
    while written < limit:
        async with sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select_sql, {"last": last, "hdr": target, "n": min(batch, limit - written)}
                )
            ).all()
            if not rows:
                break
            params: list[dict[str, Any]] = []
            for row in rows:
                item: dict[str, Any] = {"id": row[0]}
                for column, blob in zip(columns, row[1:], strict=True):
                    item[f"old_{column}"] = blob
                    if blob is None or bytes(blob[:HEADER_SIZE]) == target:
                        item[f"new_{column}"] = blob
                    else:
                        item[f"new_{column}"] = codec.encode(
                            kind.value, codec.decode(blob), dict_id=active.id
                        )
                params.append(item)
            await session.execute(update_sql, params)
            written += len(rows)
            last = rows[-1][0]
    if written:
        log.info("compress.recompressed", kind=kind.value, rows=written, dict_id=active.id)
    return written


# --- thống kê -------------------------------------------------------------------------------------


@dataclass(slots=True)
class StorageStats:
    kind: str
    table: str
    values: int = 0
    stored_bytes: int = 0
    # Ước lượng từ kích thước gốc ghi trong frame zstd của một mẫu.
    original_bytes: int = 0
    by_dict: dict[int, int] | None = None

    @property
    def ratio(self) -> float | None:
        return round(self.original_bytes / self.stored_bytes, 2) if self.stored_bytes else None


async def storage_stats(session: AsyncSession, kind: Kind, *, sample: int = 2000) -> StorageStats:
    table, columns = COLUMNS[kind]
    st = StorageStats(kind=kind.value, table=table, by_dict={})
    for column in columns:
        totals = f"count({column}), coalesce(sum(octet_length({column})), 0)"
        values, stored = (await session.execute(text(f"SELECT {totals} FROM {table}"))).one()
        st.values += int(values)
        st.stored_bytes += int(stored)
        if not values:
            continue
        rows = await session.execute(
            text(
                f"SELECT substring({column} FROM 1 FOR 32), octet_length({column}) FROM {table}"
                f" WHERE {column} IS NOT NULL ORDER BY random() LIMIT :n"
            ),
            {"n": sample},
        )
        sample_stored = sample_original = 0
        for head, length in rows:
            try:
                dict_id = dict_id_of(head)
            except CorruptValueError:
                continue
            assert st.by_dict is not None
            st.by_dict[dict_id] = st.by_dict.get(dict_id, 0) + 1
            size = content_size(head)
            if size is not None:
                sample_original += size
                sample_stored += int(length)
        if sample_stored:
            st.original_bytes += int(int(stored) * sample_original / sample_stored)
    return st
