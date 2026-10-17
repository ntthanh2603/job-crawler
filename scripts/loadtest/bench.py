"""Đo các truy vấn của giao diện trên DB jobcrawler_load (dữ liệu lớn giả lập).

uv run python scripts/loadtest/bench.py [--prepare] [--explain]
--prepare: tính cột tóm tắt doanh nghiệp + VACUUM ANALYZE (chạy một lần sau seed.py).
"""

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import make_url, select, text

from jobcrawler.config import get_settings
from jobcrawler.db.compress import load_dictionaries
from jobcrawler.db.models import CompanyRun
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.services import maintenance, overview, stats
from jobcrawler.web import queries

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
            await maintenance.refresh_company_summaries(s)
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
        if isinstance(out, queries.Page):
            more = "" if out.exact else "+"
            info = f"total={out.total}{more} items={len(out.items)} page={out.page}"
        results.append((name, ms, info))
        print(f"{ms:8.1f} ms  {name}  {info}", flush=True)

    J = queries.JobFilters
    await bench("jobs: mặc định (đang mở, mới nhất)", queries.list_jobs, J())
    await bench("jobs: trang 150", queries.list_jobs, J(page=150))
    await bench("jobs: tìm 'kế toán'", queries.list_jobs, J(q="kế toán"))
    await bench("jobs: tìm tên DN 'Mẫu 4242'", queries.list_jobs, J(q="Mẫu 4242"))
    await bench("jobs: tỉnh Đà Nẵng", queries.list_jobs, J(province="Đà Nẵng"))
    await bench(
        "jobs: lương ≥ 30tr, sắp theo lương", queries.list_jobs, J(salary_from=30, sort="salary")
    )
    await bench("jobs: nguồn topcv + senior", queries.list_jobs, J(source="topcv", level="senior"))
    await bench("jobs: sắp hết hạn", queries.list_jobs, J(sort="expires"))
    await bench("jobs: một doanh nghiệp", queries.list_jobs, J(company_id=4242, status=""))
    C = queries.CompanyFilters
    await bench("companies: mặc định", queries.list_companies, C())
    await bench("companies: tìm 'Mẫu 4242'", queries.list_companies, C(q="Mẫu 4242"))
    await bench("companies: tìm MST", queries.list_companies, C(q="0000004242"))
    await bench("companies: lượt cuối failed", queries.list_companies, C(run="failed"))
    await bench(
        "companies: có việc + verified", queries.list_companies, C(jobs="with", kyc="verified")
    )
    await bench("company: chi tiết", queries.company_detail, 4242)
    await bench("company: việc đang mở", queries.company_jobs, 4242)
    await bench("company: lượt chạy", queries.company_runs, 4242)
    await bench("company: tin bị loại", queries.company_rejected, 4242)
    async with sm() as s:
        run_id = await s.scalar(
            select(CompanyRun.run_id).where(CompanyRun.company_id == 4242).limit(1)
        )
    await bench("run: chi tiết", queries.run_detail, run_id)
    await bench("run: request", queries.run_fetches, run_id)
    await bench("boards: lịch sử index", queries.board_runs)
    await bench("overview: phân bố (cron)", overview.compute_charts)
    await bench("overview: lượt chạy theo giờ", overview.runs_per_hour)
    await bench("overview: request theo giờ", overview.fetches_per_hour)
    since = datetime.now(UTC) - timedelta(hours=24)
    await bench("stats: lượt chạy 24h", stats.run_stats, since)
    await bench("stats: nguồn 24h", stats.source_stats, since)
    await bench("stats: trang tuyển dụng", stats.board_stats)

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


asyncio.run(main())
