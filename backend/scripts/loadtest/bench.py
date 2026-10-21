"""Đo các truy vấn của giao diện trên DB jobcrawler_load (dữ liệu lớn giả lập).

uv run python scripts/loadtest/bench.py [--prepare] [--explain]
--prepare: tính cột tóm tắt doanh nghiệp + VACUUM ANALYZE (chạy một lần sau seed.py).
"""

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import make_url, select, text

from jobcrawler.common.config import get_settings
from jobcrawler.common.pagination import Page
from jobcrawler.database.compress import load_dictionaries
from jobcrawler.database.models import Company, CompanyRun
from jobcrawler.database.session import create_engine, create_sessionmaker
from jobcrawler.modules.boards.boards_service import board_runs
from jobcrawler.modules.companies.companies_service import (
    CompanyListQuery,
    company_detail,
    company_jobs,
    company_rejected,
    company_runs,
    list_companies,
)
from jobcrawler.modules.jobs.jobs_service import JobListQuery, list_jobs
from jobcrawler.modules.maintenance import maintenance_service
from jobcrawler.modules.overview import overview_service, stats_service
from jobcrawler.modules.runs.runs_service import run_detail, run_fetches

URL = make_url(get_settings().database_url).set(database="jobcrawler_load")
EXPLAIN = "--explain" in sys.argv


async def main() -> None:
    engine = create_engine(
        get_settings().model_copy(
            update={"database_url": URL.render_as_string(hide_password=False)}
        )
    )
    async with engine.connect() as c:
        await load_dictionaries(c)
    sm = create_sessionmaker(engine)
    if "--prepare" in sys.argv:
        t = time.perf_counter()
        async with sm.begin() as s:
            await maintenance_service.refresh_company_summaries(s)
        print(f"refresh_company_summaries: {time.perf_counter() - t:.1f} s")
        async with engine.connect() as c:
            await c.execution_options(isolation_level="AUTOCOMMIT")
            await c.exec_driver_sql("VACUUM (PARALLEL 0, ANALYZE)")
        print("analyzed")

    results: list[tuple[str, float, str]] = []

    async def bench(name: str, fn, *args, **kw) -> None:
        async with sm() as s:
            await fn(s, *args, **kw)  # nóng cache
            t = time.perf_counter()
            out = await fn(s, *args, **kw)
            ms = (time.perf_counter() - t) * 1000
        info = ""
        if isinstance(out, Page):
            more = "" if out.exact else "+"
            info = f"total={out.total}{more} data={len(out.items)} page={out.page}"
        results.append((name, ms, info))
        print(f"{ms:8.1f} ms  {name}  {info}", flush=True)

    async with sm() as s:
        cid = await s.scalar(select(Company.id).where(Company.input_name == "Công ty Mẫu 4242"))
    assert cid is not None, "chưa seed: chạy scripts/loadtest/seed.py"

    # Số dòng mỗi trang như giao diện (50).
    def J(**kw: object) -> JobListQuery:
        return JobListQuery.model_validate({"limit": 50, **kw})

    def C(**kw: object) -> CompanyListQuery:
        return CompanyListQuery.model_validate({"limit": 50, **kw})

    await bench("jobs: mặc định (đang mở, mới nhất)", list_jobs, J())
    await bench("jobs: trang 150", list_jobs, J(page=150))
    await bench("jobs: cũ nhất trước (quét ngược index)", list_jobs, J(sortOrder="ASC"))
    await bench("jobs: tìm 'kế toán'", list_jobs, J(search="kế toán"))
    await bench("jobs: tìm tên DN 'Mẫu 4242'", list_jobs, J(search="Mẫu 4242"))
    await bench("jobs: tỉnh Đà Nẵng", list_jobs, J(province="Đà Nẵng"))
    await bench("jobs: lương ≥ 30tr, sắp theo lương", list_jobs, J(salary_from=30, sortBy="salary"))
    await bench("jobs: nguồn topcv + senior", list_jobs, J(source="topcv", level="senior"))
    await bench("jobs: sắp hết hạn", list_jobs, J(sortBy="expires_at", sortOrder="ASC"))
    await bench("jobs: một doanh nghiệp", list_jobs, J(company_id=cid, status="all"))
    await bench("companies: mặc định", list_companies, C())
    await bench("companies: tìm 'Mẫu 4242'", list_companies, C(search="Mẫu 4242"))
    await bench("companies: tìm MST", list_companies, C(search="0000004242"))
    await bench("companies: lượt cuối failed", list_companies, C(run="failed"))
    await bench("companies: có việc + verified", list_companies, C(jobs="with", kyc="verified"))
    await bench("companies: theo tên", list_companies, C(sortBy="name", sortOrder="ASC"))
    await bench("company: chi tiết", company_detail, cid)
    await bench("company: việc đang mở", company_jobs, cid)
    await bench("company: lượt chạy", company_runs, cid)
    await bench("company: tin bị loại", company_rejected, cid)
    async with sm() as s:
        run_id = await s.scalar(
            select(CompanyRun.run_id).where(CompanyRun.company_id == cid).limit(1)
        )
    await bench("run: chi tiết", run_detail, run_id)
    await bench("run: request", run_fetches, run_id)
    await bench("boards: lịch sử index", board_runs)
    await bench("overview: phân bố (cron)", overview_service.compute_charts)
    await bench("overview: lượt chạy theo giờ", overview_service.runs_per_hour)
    await bench("overview: request theo giờ", overview_service.fetches_per_hour)
    since = datetime.now(UTC) - timedelta(hours=24)
    await bench("stats: lượt chạy 24h", stats_service.run_stats, since)
    await bench("stats: nguồn 24h", stats_service.source_stats, since)
    await bench("stats: trang tuyển dụng", stats_service.board_stats)

    if EXPLAIN:
        async with sm() as s:
            for name, sql in [
                (
                    "jobs default",
                    "EXPLAIN ANALYZE SELECT id FROM jobs WHERE status='active'"
                    " ORDER BY first_seen_at DESC, id DESC LIMIT 50",
                ),
                (
                    "jobs q",
                    "EXPLAIN ANALYZE SELECT id FROM jobs WHERE (title ILIKE '%kế toán%'"
                    " OR company_name ILIKE '%kế toán%') AND status='active'"
                    " ORDER BY first_seen_at DESC, id DESC LIMIT 50",
                ),
            ]:
                plan = "\n".join(r[0] for r in await s.execute(text(sql)))
                print(f"--- {name}\n{plan}")
    slow = [r for r in results if r[1] > 300]
    print(f"\n{len(results)} truy vấn · chậm (> 300 ms): {len(slow)}")
    for name, ms, _ in slow:
        print(f"  {ms:.0f} ms  {name}")


sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]  # tên tiếng Việt khi in ra pipe
asyncio.run(main())
