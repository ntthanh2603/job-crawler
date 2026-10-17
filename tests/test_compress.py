from __future__ import annotations

import json
import random

import pytest
from sqlalchemy.dialects import postgresql

from jobcrawler.db import compress
from jobcrawler.db.compress import (
    HEADER_SIZE,
    Codec,
    CorruptValueError,
    Dictionary,
    Kind,
    UnknownDictionaryError,
    ZstdJSON,
    ZstdText,
)

DIALECT = postgresql.dialect()  # pyright: ignore[reportAttributeAccessIssue]

TITLES = ["Kế toán tổng hợp", "Lập trình viên Python", "Nhân viên kinh doanh", "Chuyên viên HR"]
CITIES = ["Hà Nội", "Hồ Chí Minh", "Đà Nẵng", "Hải Phòng", "Cần Thơ"]
WORDS = ["dữ liệu", "báo cáo", "khách hàng", "hệ thống"]


def job_text(rng: random.Random) -> bytes:
    """Tin giả có khung chung (như tin cùng một trang tuyển dụng) và phần ngẫu nhiên."""
    words = " ".join(rng.choice(WORDS) for _ in range(30))
    description = (
        f"<h2>Mô tả công việc</h2><ul><li>Tiếp nhận và xử lý {words}</li>"
        "<li>Phối hợp với các phòng ban liên quan</li></ul><h2>Yêu cầu ứng viên</h2><ul>"
        "<li>Tốt nghiệp Cao đẳng/Đại học chuyên ngành liên quan</li>"
        f"<li>Có ít nhất {rng.randint(1, 5)} năm kinh nghiệm</li></ul><h2>Quyền lợi</h2><ul>"
        "<li>Lương tháng 13, thưởng theo hiệu quả</li>"
        "<li>Đóng BHXH, BHYT đầy đủ theo quy định</li></ul>"
    )
    job = {
        "source": "topcv",
        "title": rng.choice(TITLES),
        "company_name": f"Công ty TNHH {rng.randint(1, 10_000)}",
        "locations": [rng.choice(CITIES)],
        "description": description,
        "salary_raw": f"{rng.randint(8, 15)} - {rng.randint(16, 30)} triệu",
    }
    return json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode()


def samples(n: int, seed: int = 1) -> list[bytes]:
    rng = random.Random(seed)
    return [job_text(rng) for _ in range(n)]


def test_roundtrip_without_dictionary() -> None:
    codec = Codec(level=3)
    data = "Tuyển kế toán — lương 10-15 triệu".encode()
    blob = codec.encode(Kind.JOB_TEXT, data)
    assert blob[:HEADER_SIZE] == compress.header(0) == b"\x01\x00\x00\x00\x00"
    assert compress.dict_id_of(blob) == 0
    assert compress.content_size(blob) == len(data)
    assert compress.content_size(blob[:24]) == len(data)  # chỉ cần phần đầu
    assert codec.decode(blob) == data
    assert codec.decode(memoryview(blob)) == data
    assert codec.decode(codec.encode(Kind.POSTING, b"")) == b""


def test_bad_values_are_reported() -> None:
    codec = Codec()
    with pytest.raises(CorruptValueError):
        codec.decode(b'{"title": "x"}')
    with pytest.raises(CorruptValueError):
        codec.decode(b"\x01\x00")
    with pytest.raises(CorruptValueError):
        codec.decode(compress.header(0) + b"not zstd at all")
    with pytest.raises(UnknownDictionaryError):
        codec.decode(compress.header(12345) + b"\x28\xb5\x2f\xfd")


def trained(seed: int = 1, n: int = 400) -> Dictionary:
    dictionary, result = compress.train(
        Kind.POSTING,
        samples(n, seed),
        dict_size=8192,
        level=3,
        current=None,
        taken=set(),
        seed=seed,
    )
    assert dictionary is not None, result.reason
    return dictionary


def test_train_adopts_dictionary_that_compresses_better() -> None:
    dictionary, result = compress.train(
        Kind.POSTING, samples(400), dict_size=8192, level=3, current=None, taken=set(), seed=1
    )
    assert dictionary is not None
    assert result.adopted
    assert result.dict_id == dictionary.id != 0
    assert result.ratio_before is not None
    assert result.ratio_after is not None
    assert result.ratio_after > result.ratio_before * 1.5

    codec = Codec(level=3)
    codec.add(Dictionary(dictionary.id, Kind.POSTING, dictionary.data, usable_at=0))
    data = samples(1, seed=99)[0]
    blob = codec.encode(Kind.POSTING, data)
    assert compress.dict_id_of(blob) == dictionary.id
    assert codec.decode(blob) == data
    assert len(blob) < len(Codec(level=3).encode(Kind.POSTING, data)) * 0.7
    # Loại khác không dùng từ điển này.
    assert compress.dict_id_of(codec.encode(Kind.JOB_TEXT, data)) == 0

    # Từ điển mới không hơn từ điển đang dùng thì bỏ.
    again, second = compress.train(
        Kind.POSTING,
        samples(400, seed=2),
        dict_size=8192,
        level=3,
        current=dictionary.data,
        taken={dictionary.id},
        seed=2,
    )
    assert again is None
    assert not second.adopted
    assert second.reason == "không tốt hơn từ điển đang dùng"


def test_train_needs_enough_samples() -> None:
    dictionary, result = compress.train(
        Kind.POSTING, samples(5), dict_size=65536, level=3, current=None, taken=set()
    )
    assert dictionary is None
    assert result.reason == "mẫu quá ít so với cỡ từ điển"


def test_active_dictionary_waits_for_usable_at() -> None:
    first, second = trained(1), trained(2)
    codec = Codec(level=3)
    codec.add(Dictionary(first.id, Kind.POSTING, first.data, usable_at=100))
    codec.add(Dictionary(second.id, Kind.POSTING, second.data, usable_at=200))
    assert codec.active(Kind.POSTING, now=50) is None
    active = codec.active(Kind.POSTING, now=150)
    assert active is not None
    assert active.id == first.id
    active = codec.active(Kind.POSTING, now=250)
    assert active is not None
    assert active.id == second.id
    assert codec.ids == {first.id, second.id}
    # Giá trị nén bằng từ điển cũ vẫn đọc được.
    data = samples(1, seed=7)[0]
    old = codec.encode(Kind.POSTING, data, dict_id=first.id)
    assert codec.decode(old) == data
    with pytest.raises(ValueError, match="dành cho"):
        codec.add(Dictionary(0, Kind.POSTING, first.data, usable_at=0))


def test_column_types_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compress, "CODEC", Codec(level=3))
    as_json = ZstdJSON(Kind.POSTING)
    value = {"title": "Kỹ sư cầu nối tiếng Nhật", "locations": ["Hà Nội"], "salary": None}
    blob = as_json.process_bind_param(value, DIALECT)
    assert isinstance(blob, bytes)
    assert blob.startswith(b"\x01")
    assert as_json.process_result_value(blob, DIALECT) == value
    assert as_json.process_bind_param(None, DIALECT) is None
    assert as_json.process_result_value(None, DIALECT) is None

    as_text = ZstdText(Kind.JOB_TEXT)
    md = "## Mô tả\n- Lập trình **Python**\n" * 20
    blob = as_text.process_bind_param(md, DIALECT)
    assert isinstance(blob, bytes)
    assert len(blob) < len(md.encode()) / 5
    assert as_text.process_result_value(blob, DIALECT) == md
    assert as_text.process_result_value(None, DIALECT) is None
