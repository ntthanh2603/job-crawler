"""Số liệu trang tổng quan, chịu được dữ liệu lớn.

Hai phần, cache khác nhau:

- **Phân bố** trên toàn bộ job đang mở / doanh nghiệp (theo nguồn, tỉnh/thành, cấp bậc, loại hình,
  hình thức, khoảng lương, KYC, doanh nghiệp nhiều việc nhất, job mới theo ngày): quét nhiều dòng
  nên tính sẵn — cron `overview_cron` làm mới mỗi vài phút, kết quả nằm trong Redis cache
  (`CHARTS_KEY`, CACHE_REDIS_URL); người xem chỉ đọc. Chưa có (Redis mới, khoá bị đẩy ra) thì
  tính một lần rồi lưu.
- **Theo cửa sổ thời gian** (lượt chạy, request theo giờ, thống kê `stats.collect_stats`): chỉ
  đọc các dòng trong cửa sổ qua index `started_at` / BRIN `fetched_at`; cache `VIEW_TTL` giây
  theo cửa sổ để nhiều người xem / tự làm mới không chạy lại truy vấn.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.database.models import Company, CompanyRun, Job, SourceFetch
from jobcrawler.modules.jobs.status_service import open_jobs
from jobcrawler.modules.overview import stats_service
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

# Khoá Redis có tiền tố tên queue: môi trường / test khác nhau không đọc nhầm số liệu của nhau.
CHARTS_KEY = "{ns}:overview:charts"
CHARTS_TTL = 30 * 60  # cron làm mới mỗi 5 phút; quá hạn này thì người xem tự tính lại
VIEW_KEY = "{ns}:overview:view:{hours}:{storage}"
VIEW_TTL = 60
DAYS = 30
TOP = 12

SOURCE_KIND = func.split_part(Job.canonical_source, ":", 1)
# Khoảng lương (triệu VND / tháng, theo mức cao nhất ghi trong tin).
SALARY_BANDS = [
    ("< 10 triệu", 0, 10),
    ("10–15 triệu", 10, 15),
    ("15–20 triệu", 15, 20),
    ("20–30 triệu", 20, 30),
    ("30–50 triệu", 30, 50),
    ("≥ 50 triệu", 50, None),
]


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _pairs(session: AsyncSession, stmt: Any) -> list[tuple[Any, int]]:
    rows: list[Any] = list(await session.execute(stmt))
    return [(row[0], int(row[1])) for row in rows]


async def compute_charts(
    session: AsyncSession, *, tz: str, now: datetime | None = None
) -> dict[str, Any]:
    """Phân bố trên job đang mở (`status_service.open_jobs`) và doanh nghiệp; job mới theo ngày
    của múi giờ `tz` (CRAWL_TIMEZONE). Mỗi truy vấn đọc theo index trạng thái / BRIN."""
    now = now or datetime.now(UTC)
    active = open_jobs(now)
    by_source = await _pairs(
        session,
        select(SOURCE_KIND, func.count())
        .where(active)
        .group_by(SOURCE_KIND)
        .order_by(func.count().desc()),
    )
    provinces = await _pairs(
        session,
        text(
            """
            SELECT p, count(*) FROM (
                SELECT DISTINCT j.id, loc->>'province' AS p
                FROM jobs j, jsonb_array_elements(j.locations) AS loc
                WHERE j.status = 'active' AND (j.expires_at IS NULL OR j.expires_at >= :now)) t
            WHERE p IS NOT NULL GROUP BY p ORDER BY count(*) DESC LIMIT :top
            """
        ).bindparams(top=TOP, now=now),
    )
    no_place = int(
        await session.scalar(
            select(func.count()).where(active, func.jsonb_array_length(Job.locations) == 0)
        )
        or 0
    )

    async def group(column: Any) -> list[tuple[str, int]]:
        return await _pairs(
            session,
            select(column, func.count())
            .where(active)
            .group_by(column)
            .order_by(func.count().desc()),
        )

    top = func.coalesce(Job.salary_max, Job.salary_min) / 1_000_000
    vnd_month = (Job.salary_currency == "VND") & (Job.salary_period == "month")
    band = case(
        *[
            (
                (vnd_month & (top >= low) & (top < high)) if high else (vnd_month & (top >= low)),
                name,
            )
            for name, low, high in SALARY_BANDS
        ],
        (top.is_not(None), "Tiền tệ / kỳ khác"),
        (Job.salary_negotiable, "Thoả thuận"),
        else_="Không ghi",
    )
    salary = dict(await _pairs(session, select(band, func.count()).where(active).group_by(band)))
    order = [b[0] for b in SALARY_BANDS] + ["Tiền tệ / kỳ khác", "Thoả thuận", "Không ghi"]

    # `timezone(tz, t)` = `t AT TIME ZONE tz`: giờ địa phương, rồi lấy ngày.
    local_day = func.date(func.timezone(tz, Job.first_seen_at))
    own = SOURCE_KIND == "careers"
    since = now - timedelta(days=DAYS)
    days: dict[date, dict[str, int]] = {}
    for day, is_own, n in await session.execute(
        select(local_day, own, func.count())
        .where(Job.first_seen_at >= since)
        .group_by(local_day, own)
    ):
        days.setdefault(day, {})["careers" if is_own else "boards"] = int(n)
    today = now.astimezone(ZoneInfo(tz)).date()
    jobs_by_day = [
        {
            "day": (d := today - timedelta(days=DAYS - 1 - i)).isoformat(),
            "careers": days.get(d, {}).get("careers", 0),
            "boards": days.get(d, {}).get("boards", 0),
        }
        for i in range(DAYS)
    ]

    # Cùng một biểu thức cho SELECT và GROUP BY (cùng tham số bind).
    kyc_status = func.coalesce(Company.kyc_status, "none")
    kyc = await _pairs(session, select(kyc_status, func.count()).group_by(kyc_status))
    top_companies = [
        {"id": cid, "name": brand or legal or name, "active_jobs": n}
        for cid, brand, legal, name, n in await session.execute(
            select(
                Company.id,
                Company.brand_name,
                Company.legal_name,
                Company.input_name,
                Company.active_jobs,
            )
            .where(Company.active_jobs > 0)
            .order_by(Company.active_jobs.desc())
            .limit(10)
        )
    ]
    companies = int(await session.scalar(select(func.count()).select_from(Company)) or 0)
    with_jobs = int(await session.scalar(select(func.count()).where(Company.active_jobs > 0)) or 0)
    active_jobs = sum(n for _, n in by_source)
    return {
        "generated_at": now,
        "totals": {
            "active_jobs": active_jobs,
            "companies": companies,
            "companies_with_jobs": with_jobs,
            "new_jobs_7d": sum(d["careers"] + d["boards"] for d in jobs_by_day[-7:]),
            "with_salary": sum(salary.get(name, 0) for name, _, _ in SALARY_BANDS)
            + salary.get("Tiền tệ / kỳ khác", 0),
        },
        "jobs_by_day": jobs_by_day,
        "jobs_by_source": by_source,
        "jobs_by_province": provinces + ([("Không rõ nơi làm", no_place)] if no_place else []),
        "jobs_by_level": await group(Job.level),
        "jobs_by_employment": await group(Job.employment_type),
        "jobs_by_work_mode": await group(Job.work_mode),
        "salary_bands": [(name, salary.get(name, 0)) for name in order if salary.get(name)],
        "kyc": sorted(kyc, key=lambda kv: -kv[1]),
        "top_companies": top_companies,
    }


async def refresh_charts(res: Resources) -> dict[str, Any]:
    async with res.sessionmaker() as session:
        charts = await compute_charts(session, tz=res.settings.crawl_timezone)
    await res.cache_redis.set(
        CHARTS_KEY.format(ns=res.settings.redis_prefix), _dump(charts), ex=CHARTS_TTL
    )
    log.info("overview.charts_refreshed", active_jobs=charts["totals"]["active_jobs"])
    return json.loads(_dump(charts))


async def get_charts(res: Resources) -> dict[str, Any]:
    cached = await res.cache_redis.get(CHARTS_KEY.format(ns=res.settings.redis_prefix))
    if cached:
        return json.loads(cached)
    return await refresh_charts(res)


async def fetches_per_hour(session: AsyncSession, *, hours: int = 24) -> list[dict[str, Any]]:
    """Request theo giờ (thành công / bị chặn / lỗi khác) — BRIN `fetched_at`."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    hour = func.date_trunc("hour", SourceFetch.fetched_at)
    kind = case(
        (SourceFetch.outcome == FetchOutcome.OK.value, "ok"),
        (SourceFetch.outcome == FetchOutcome.BLOCKED.value, "blocked"),
        else_="error",
    )
    buckets = {
        start + timedelta(hours=i): {"ok": 0, "blocked": 0, "error": 0} for i in range(hours)
    }
    for h, k, n in await session.execute(
        select(hour, kind, func.count()).where(SourceFetch.fetched_at >= start).group_by(hour, kind)
    ):
        key = h.astimezone(UTC)
        if key in buckets:
            buckets[key][k] = int(n)
    return [{"hour": h, **counts} for h, counts in buckets.items()]


async def runs_per_hour(session: AsyncSession, *, hours: int = 24) -> list[dict[str, Any]]:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    hour = func.date_trunc("hour", CompanyRun.started_at)
    buckets: dict[datetime, dict[str, int]] = {start + timedelta(hours=i): {} for i in range(hours)}
    for h, status, n in await session.execute(
        select(hour, CompanyRun.status, func.count())
        .where(CompanyRun.started_at >= start)
        .group_by(hour, CompanyRun.status)
    ):
        key = h.astimezone(UTC)
        if key in buckets:
            buckets[key][status] = int(n)
    return [{"hour": h, "counts": c, "total": sum(c.values())} for h, c in buckets.items()]


async def build_overview(res: Resources, *, hours: float | None, storage: bool) -> dict[str, Any]:
    """Toàn bộ dữ liệu trang tổng quan (cache VIEW_TTL giây theo cửa sổ)."""
    key = VIEW_KEY.format(ns=res.settings.redis_prefix, hours=hours or 0, storage=int(storage))
    cached = await res.cache_redis.get(key)
    if cached:
        return json.loads(cached)
    st = await stats_service.collect_stats(res, hours=hours, storage=storage)
    async with res.sessionmaker() as session:
        runs_chart = await runs_per_hour(session)
        fetch_chart = await fetches_per_hour(session)
        recent = list(
            await session.execute(
                select(CompanyRun, Company.input_name)
                .join(Company, Company.id == CompanyRun.company_id)
                .order_by(CompanyRun.started_at.desc())
                .limit(10)
            )
        )
    payload = {
        "stats": dataclasses.asdict(st),
        "chart": runs_chart,
        "fetch_chart": fetch_chart,
        "charts": await get_charts(res),
        "recent_runs": [
            {
                "run_id": str(run.run_id),
                "company_id": run.company_id,
                "company_name": name,
                "kind": run.kind,
                "trigger": run.trigger,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_ms": run.duration_ms,
                "status": run.status,
                "jobs_found": run.jobs_found,
                "jobs_accepted": run.jobs_accepted,
                "jobs_new": run.jobs_new,
                "jobs_closed": run.jobs_closed,
                "incomplete_sources": list(run.incomplete_sources or []),
                "error": run.error,
            }
            for run, name in recent
        ],
    }
    await res.cache_redis.set(key, _dump(payload), ex=VIEW_TTL)
    return json.loads(_dump(payload))
