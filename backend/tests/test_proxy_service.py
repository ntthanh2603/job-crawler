"""`services.proxies`: danh sách proxy chung và kiểm tra theo id."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.config import Settings
from jobcrawler.net.proxy_pool import parse_proxy_line
from jobcrawler.services import health
from jobcrawler.services import proxies as svc

pytestmark = pytest.mark.usefixtures("clean_env")


def settings(tmp_path: Path, lines: list[str], file_lines: Sequence[str] = ()) -> Settings:
    path = tmp_path / "proxies.txt"
    path.write_text("\n".join(file_lines), encoding="utf-8")
    return Settings(_env_file=None, proxies=lines, proxies_file=path)  # pyright: ignore[reportCallIssue]


def test_listed_merges_settings_then_file_without_duplicates(tmp_path: Path) -> None:
    s = settings(
        tmp_path,
        ["http://u:p@1.1.1.1:80", "1.1.1.2:81"],
        ["http://u:p@1.1.1.1:80", "socks5://1.1.1.3:1080", "khong-hop-le"],
    )
    items = svc.listed(s)
    assert [(i.source, i.line) for i in items] == [
        ("settings", "http://u:•••@1.1.1.1:80"),
        ("settings", "1.1.1.2:81"),
        ("file", "socks5://1.1.1.3:1080"),
    ]


async def test_check_only_selected_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(tmp_path, ["1.1.1.1:80", "1.1.1.2:81"])
    wanted = parse_proxy_line("1.1.1.2:81")
    assert wanted is not None
    seen: dict[str, Any] = {}

    async def fake_check(res: Any, *, pool: Any, url: str, concurrency: int, timeout: float):
        seen.update(ids=[p.id for p in pool.proxies], url=url, timeout=timeout)
        return []

    class FakeRes:
        redis = None

    monkeypatch.setattr(health, "check_proxies", fake_check)
    assert await svc.check(FakeRes(), s, [wanted.id, "khong-co"]) == []  # pyright: ignore[reportArgumentType]
    assert seen == {
        "ids": [wanted.id],
        "url": s.proxy_check_url,
        "timeout": svc.CHECK_TIMEOUT_SECONDS,
    }
    seen.clear()
    assert await svc.check(FakeRes(), s, ["khong-co"]) == []  # pyright: ignore[reportArgumentType]
    assert seen == {}
