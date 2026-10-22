"""`store.content_hash`: chỉ đổi khi nội dung việc đổi, không đổi theo mốc thấy / ngày đăng tính lại
mỗi lượt (sự kiện `updated` cho hệ thống ngoài)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from jobcrawler.pipeline import store

from .jobs_fixtures import NOW, normalized, raw_job


def values(**fields: object) -> dict[str, object]:
    job = normalized(raw_job(**fields), board=False)
    return store.job_values(
        job,
        company_id=uuid4(),
        company_name="ACME",
        source_urls=[job.url],
        confidence=0.9,
        reasons=["TEST"],
    )


def test_hash_ignores_seen_times_posting_date_and_skill_order() -> None:
    base = values()
    same = values(posted_at=datetime(2026, 10, 12, tzinfo=UTC)) | {
        "confidence": 0.5,
        "verification_reasons": ["KHÁC"],
        "first_seen_at": NOW,
        "last_seen_at": NOW + timedelta(hours=3),
    }
    assert store.content_hash(base) == store.content_hash(same)
    assert store.content_hash(base | {"skills": ["SQL", "Excel"]}) == store.content_hash(
        base | {"skills": ["Excel", "SQL"]}
    )
    # Hạn nộp chỉ tính theo ngày: "còn 15 ngày" đọc lại lúc khác giờ không đổi hash.
    expires = datetime(2026, 11, 30, 1, tzinfo=UTC)
    assert store.content_hash(base | {"expires_at": expires}) == store.content_hash(
        base | {"expires_at": expires + timedelta(hours=5)}
    )


def test_hash_changes_with_content() -> None:
    base = store.content_hash(values())
    assert store.content_hash(values(salary_raw="25 - 30 triệu")) != base
    assert store.content_hash(values(title="Kế toán trưởng")) != base
    assert store.content_hash(values(expires_at=datetime(2026, 12, 15, tzinfo=UTC))) != base
