"""Tạo DB jobcrawler_load với dữ liệu lớn giả lập để đo truy vấn.

uv run python scripts/loadtest/seed.py   (trong backend/; xoá và tạo lại DB jobcrawler_load)
"""

import asyncio
import time

import asyncpg
from sqlalchemy import make_url

from jobcrawler.common.config import get_settings
from jobcrawler.database import migrate
from jobcrawler.database.compress import CODEC, Kind, dumps

_url = make_url(get_settings().database_url)
ADMIN = _url.set(drivername="postgresql").render_as_string(hide_password=False)
LOAD = _url.set(drivername="postgresql", database="jobcrawler_load").render_as_string(
    hide_password=False
)
LOAD_SA = _url.set(database="jobcrawler_load").render_as_string(hide_password=False)

COMPANIES = 100_000
JOBS = 1_000_000
RUNS = 1_000_000
POSTINGS = 1_000_000

DESCRIPTION = (
    "## Mô tả công việc\n\n- Tư vấn, chăm sóc khách hàng doanh nghiệp.\n- Lập báo cáo doanh số "
    "hằng tuần, phối hợp phòng kế toán đối chiếu công nợ.\n\n## Yêu cầu\n\n- Tốt nghiệp đại học, "
    "ít nhất 2 năm kinh nghiệm.\n- Giao tiếp tốt, chủ động.\n"
) * 3


async def step(conn: asyncpg.Connection, name: str, sql: str, *args: object) -> None:
    t = time.perf_counter()
    await conn.execute(sql, *args)
    print(f"  {name}: {time.perf_counter() - t:.1f} s", flush=True)


SEED_ID = """
CREATE FUNCTION seed_id(tag int, n bigint) RETURNS uuid LANGUAGE sql IMMUTABLE AS $$
  SELECT (lpad(to_hex(1767225600000 + n), 12, '0') || '7000' || '8'
          || lpad(to_hex(tag), 3, '0') || '000000000000')::uuid
$$
"""


async def main() -> None:

    blob = CODEC.encode(Kind.JOB_TEXT.value, DESCRIPTION.encode(), dict_id=0)
    raw = CODEC.encode(Kind.POSTING.value, dumps({"title": "Nhân viên kinh doanh"}), dict_id=0)
    conn = await asyncpg.connect(LOAD)
    await conn.execute("SET synchronous_commit = off")
    # Id UUIDv7 tất định theo số thứ tự (cùng dạng migration 0007 đổi dữ liệu cũ): khoá ngoại tính
    # được bằng cùng hàm, không cần join khi sinh dữ liệu.
    await conn.execute(SEED_ID)
    await step(
        conn,
        "companies",
        """
        INSERT INTO companies (id, input_name, legal_name, primary_domain, kyc_status, tax_code)
        SELECT seed_id(1, i), 'Công ty Mẫu ' || i,
               CASE WHEN i % 3 = 0 THEN 'CÔNG TY CỔ PHẦN MẪU SỐ ' || i END,
               'cty' || i || '.vn',
               (ARRAY['verified', 'not_found', 'ambiguous', NULL])[1 + i % 4],
               CASE WHEN i % 2 = 0 THEN lpad(i::text, 10, '0') END
        FROM generate_series(1, $1) i
        """,
        COMPANIES,
    )
    await step(
        conn,
        "jobs",
        """
        INSERT INTO jobs (id, company_id, cluster_key, fingerprint, title, title_normalized,
                          company_name, level, employment_type, work_mode, locations,
                          salary_min, salary_max, salary_currency, salary_period,
                          salary_negotiable, description_md, canonical_source, status, confidence,
                          first_seen_at, last_seen_at, posted_at, expires_at, source_urls)
        SELECT seed_id(6, i), seed_id(1, 1 + i % $2),
               'k' || i,
               md5(i::text),
               t.title || ' ' || (i % 977),
               lower(t.title),
               'Công ty Mẫu ' || (1 + i % $2),
               (ARRAY['intern','fresher','junior','middle','senior','manager',
                      'unknown'])[1 + i % 7],
               (ARRAY['full_time','part_time','internship','contract','unknown'])[1 + i % 5],
               (ARRAY['onsite','hybrid','remote','unknown'])[1 + i % 4],
               jsonb_build_array(jsonb_build_object('province',
                 (ARRAY['Hà Nội','Hồ Chí Minh','Đà Nẵng','Hải Phòng','Cần Thơ','Đồng Nai',
                        'Bắc Ninh','Khánh Hoà','Nghệ An','Thanh Hoá'])[1 + i % 10])),
               CASE WHEN i % 3 <> 0 THEN (5 + i % 20) * 1000000 END,
               CASE WHEN i % 3 <> 0 THEN (10 + i % 40) * 1000000 END,
               CASE WHEN i % 3 <> 0 THEN 'VND' END,
               CASE WHEN i % 3 <> 0 THEN 'month' END,
               i % 3 = 0,
               $3,
               (ARRAY['careers','careers','topcv','vieclam24h','glints','jobsgo'])[1 + i % 6],
               CASE WHEN i % 10 < 7 THEN 'active' WHEN i % 10 < 9 THEN 'removed' ELSE 'expired' END,
               0.9,
               now() - (i % 60) * interval '1 day' - (i % 1440) * interval '1 minute',
               now() - (i % 5) * interval '1 day',
               now() - (i % 70) * interval '1 day',
               now() + (i % 40 - 5) * interval '1 day',
               ARRAY['https://cty.vn/jobs/' || i]
        FROM generate_series(1, $1) i
        CROSS JOIN LATERAL (SELECT (ARRAY['Nhân viên kinh doanh','Kế toán tổng hợp',
             'Lập trình viên Java','Chuyên viên tuyển dụng','Kỹ sư cầu nối','Nhân viên bán hàng',
             'Trưởng phòng marketing','Tester','Data Engineer','Chăm sóc khách hàng'])
             [1 + i % 10] AS title) t
        """,
        JOBS,
        COMPANIES,
        blob,
    )
    await step(
        conn,
        "company_runs",
        """
        INSERT INTO company_runs (run_id, company_id, started_at, finished_at, duration_ms, status,
                                  jobs_found, jobs_accepted, incomplete_sources)
        SELECT gen_random_uuid(), seed_id(1, 1 + i % $2),
               now() - (i % 43200) * interval '1 minute',
               now() - (i % 43200) * interval '1 minute' + interval '90 seconds',
               20000 + i % 160000,
               (ARRAY['ok','ok','ok','partial','ambiguous','failed'])[1 + i % 6],
               i % 30, i % 25,
               CASE WHEN i % 6 = 3 THEN ARRAY['careers:cty.vn'] ELSE ARRAY[]::text[] END
        FROM generate_series(1, $1) i
        """,
        RUNS,
        COMPANIES,
    )
    await step(
        conn,
        "source_fetches",
        """
        INSERT INTO source_fetches (run_id, company_id, source, url, http_status, duration_ms,
                                    items, outcome, fetched_at)
        SELECT r.run_id, r.company_id,
               (ARRAY['careers:cty.vn','discovery','kyc','topcv'])[1 + g],
               'https://cty.vn/p/' || g, 200, 300 + g * 100, g,
               (ARRAY['ok','ok','ok','blocked','error','timeout'])[1 + (g + r.duration_ms) % 6],
               r.started_at + g * interval '5 seconds'
        FROM company_runs r CROSS JOIN generate_series(0, 1) g
        """,
    )
    await step(
        conn,
        "job_postings",
        """
        INSERT INTO job_postings (source, external_id, url, raw, company_id, accepted,
                                  verification_reasons, verified_at, status, job_id)
        SELECT (ARRAY['careers','topcv','vieclam24h'])[1 + i % 3], 'e' || i,
               'https://cty.vn/jobs/' || i, $3, seed_id(1, 1 + i % $2), i % 9 <> 0,
               CASE WHEN i % 9 = 0 THEN ARRAY['EXPIRED: hết hạn'] ELSE ARRAY['SOURCE_CAREERS'] END,
               now() - (i % 30) * interval '1 day',
               'active', CASE WHEN i % 9 <> 0 THEN seed_id(6, i) END
        FROM generate_series(1, $1) i
        """,
        POSTINGS,
        COMPANIES,
        raw,
    )
    await step(
        conn,
        "logs",
        """
        INSERT INTO logs (ts, level, event, run_id, context)
        SELECT now() - (i % 43200) * interval '1 minute', 'info', 'company.start',
               (SELECT run_id FROM company_runs LIMIT 1), '{}'::jsonb
        FROM generate_series(1, 200) i
        """,
    )
    await conn.close()
    print("seeded", flush=True)


async def create() -> None:
    admin = await asyncpg.connect(ADMIN)
    await admin.execute("DROP DATABASE IF EXISTS jobcrawler_load")
    await admin.execute("CREATE DATABASE jobcrawler_load")
    await admin.close()


asyncio.run(create())
migrate.upgrade("head", database_url=LOAD_SA)
print("migrated", flush=True)
asyncio.run(main())
