from __future__ import annotations

from jobcrawler.fakes import FakeChatClient, FakeFetcher
from jobcrawler.llm import LLMClient
from jobcrawler.sources.base import SourceLimits, SourceSession, failed_page_rows


async def test_fail_page_keeps_html_up_to_the_limit() -> None:
    fetcher = FakeFetcher()
    for i in range(4):
        fetcher.add(f"https://x.vn/{i}", f"<html>trang {i}</html>")
    session = SourceSession(
        "board:x",
        fetcher,
        LLMClient(FakeChatClient(["{}"]), model=None),
        SourceLimits(max_failed_pages=2),
    )
    for i in range(4):
        result = await session.fetch(f"https://x.vn/{i}")
        session.fail_page(result, f"no_job {i}")
    assert [r.failure for r in session.records] == ["no_job 0", "no_job 1", None, None]

    rows = failed_page_rows(session.records, limit=5, run_id="r1")
    assert rows == [
        {
            "url": "https://x.vn/0",
            "source": "board:x",
            "reason": "no_job 0",
            "run_id": "r1",
            "http_status": 200,
            "html": "<html>trang 0</html>",
        },
        {
            "url": "https://x.vn/1",
            "source": "board:x",
            "reason": "no_job 1",
            "run_id": "r1",
            "http_status": 200,
            "html": "<html>trang 1</html>",
        },
    ]
    assert len(failed_page_rows(session.records, limit=1)) == 1
