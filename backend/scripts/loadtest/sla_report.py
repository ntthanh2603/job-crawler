"""Báo cáo độ trễ phát hiện tin mới đầu-cuối: với mỗi tin `sla_site.py` / `fake_sites.py` đã đăng,
lúc nó vào DB (`job_postings.first_seen_at`) trừ lúc đăng.

    uv run python scripts/loadtest/sla_report.py sla.jsonl [--quiet]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from jobcrawler.common.config import get_settings
from jobcrawler.database.models import JobPosting
from jobcrawler.database.session import create_engine, create_sessionmaker


def tail_key(url: str) -> str:
    """Khoá so khớp: host (không cổng) + đường dẫn tin."""
    rest = url.split("://", 1)[-1]
    host, _, path = rest.partition("/")
    return host.split(":")[0] + "/" + path


async def report(rows: list[dict[str, str]], *, quiet: bool) -> int:
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as session:
            found: dict[str, datetime] = {}
            for url, first_seen in await session.execute(
                select(JobPosting.url, JobPosting.first_seen_at).where(
                    JobPosting.url.like("%/tuyen-dung/vi-tri-%")
                )
            ):
                found[tail_key(url)] = first_seen
                found[url.split("/tuyen-dung/")[-1]] = first_seen
    finally:
        await engine.dispose()
    latencies: list[float] = []
    for row in rows:
        published = datetime.fromisoformat(row["published_at"])
        key = tail_key(row["url"]) if "url" in row else row["path"].split("/tuyen-dung/")[-1]
        seen = found.get(key)
        delay = (seen - published).total_seconds() if seen else None
        if delay is not None:
            latencies.append(delay)
        if not quiet:
            shown = f"{delay:6.0f} s" if delay is not None else "  chưa thấy"
            print(f"{row['published_at'][11:19]}  {shown}  {row.get('title') or row.get('url')}")
    if not latencies:
        print("chưa có tin nào được thấy")
        return 1
    latencies.sort()

    def q(p: float) -> float:
        return latencies[min(len(latencies) - 1, round(p * (len(latencies) - 1)))]

    median = statistics.median(latencies)
    within = sum(d <= 180 for d in latencies)
    print(
        f"{len(latencies)}/{len(rows)} tin đã thấy · trung vị {median:.0f} s · P95 {q(0.95):.0f} s"
        f" · P99 {q(0.99):.0f} s · lớn nhất {latencies[-1]:.0f} s · ≤ 180 s: {within}/"
        f"{len(latencies)} ({within / len(latencies):.1%})"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("published", type=Path)
    parser.add_argument("--quiet", action="store_true", help="chỉ in tổng kết")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    text = args.published.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line]
    raise SystemExit(asyncio.run(report(rows, quiet=args.quiet)))


if __name__ == "__main__":
    main()
