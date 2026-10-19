"""Vieclam24h (vieclam24h.vn).

- Sitemap ngừng cập nhật (7/2026) nên dùng danh sách sắp xếp mới nhất
  `/tim-kiem-viec-lam-nhanh?sort_q=actived_at_by_box,desc&page=N` (30 tin/trang). Trang chỉ cho
  xem ~34 trang đầu: danh sách không bao giờ đủ → không đánh dấu tin vắng mặt; tin hết hạn nhận
  biết qua `validThrough`.
- Lượt đuôi: các trang đầu của danh sách đó (xếp theo giờ kích hoạt: tin cũ được đẩy lên lẫn với
  tin mới), dừng khi nửa trang đã có trong index: thường 1 request (~950 KB), tối đa 5.
- URL tin `/<ngành>/<slug>-c<ngành>p<tỉnh>id<mã>.html` (mã tỉnh đổi sau sáp nhập 2025 nên khoá
  theo mã tin). `identifier` là mã nhà tuyển dụng.
- Profile `/danh-sach-tin-tuyen-dung-<slug>-ntd<mã>p<tỉnh>.html`. Website và MST của nhà tuyển
  dụng có trong dữ liệu trang chi tiết tin (`__NEXT_DATA__`): đọc luôn, không tốn request.
Mẫu: tests/fixtures/boards/vieclam24h/.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobcrawler.net.urls import normalize_url
from jobcrawler.pages import parse
from jobcrawler.pipeline.company_facts import valid_tax_code
from jobcrawler.sources.boards.base import BoardJob, ListingBoard, ProfileRef


def next_data(html: str) -> dict[str, Any]:
    node = parse(html).css_first("script#__NEXT_DATA__")
    if node is None:
        return {}
    try:
        data = json.loads(node.text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _employer(data: dict[str, Any]) -> dict[str, Any]:
    api = ((data.get("props") or {}).get("initialState") or {}).get("api") or {}
    for key in ("employerDetailHiddenContact", "employerDetail"):
        found = (api.get(key) or {}).get("data")
        if isinstance(found, dict):
            return found
    return {}


class Vieclam24h(ListingBoard):
    name = "vieclam24h"
    home = "https://vieclam24h.vn/"
    listing_url = (
        "https://vieclam24h.vn/tim-kiem-viec-lam-nhanh?sort_q=actived_at_by_box%2Cdesc&page={page}"
    )
    max_listing_pages = 40
    listing_complete = False
    note = "chỉ ~1 nghìn tin mới nhất; không đánh dấu tin gỡ"
    job_url = re.compile(r"^https://vieclam24h\.vn/[^/]+/[^/]+-c\d+p\d+id(?P<id>\d+)\.html$")
    profile_url = re.compile(
        r"^https://vieclam24h\.vn/danh-sach-tin-tuyen-dung-[^/]*-ntd(?P<id>\d+)p\d+\.html$"
    )
    profile_selector = 'a[title][href*="/danh-sach-tin-tuyen-dung-"]'
    # Trang profile chia trang `?page=N` (link `rel=next`).
    profile_listing_complete = True

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        parsed = super().parse_job(html, url)
        if parsed is None or parsed.profile is None:
            return parsed
        employer = _employer(next_data(html))
        website = normalize_url(employer.get("website") or "") if employer.get("website") else None
        tax_code = valid_tax_code(str(employer.get("tax_code") or "")) or None
        profile = parsed.profile.model_copy(
            update={
                "website": parsed.profile.website or website,
                "tax_code": tax_code,
                "info": {
                    **parsed.profile.info,
                    **({"address": employer["address"]} if employer.get("address") else {}),
                },
            }
        )
        return BoardJob(job=parsed.job, profile=profile)

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        profile = super().parse_profile(html, url)
        if profile is None:
            return None
        employer = _employer(next_data(html))
        updates: dict[str, Any] = {}
        if employer.get("name"):
            updates["name"] = employer["name"]
        if employer.get("website"):
            updates["website"] = normalize_url(employer["website"])
        if tax := valid_tax_code(str(employer.get("tax_code") or "")):
            updates["tax_code"] = tax
        if employer.get("address"):
            updates["info"] = {**profile.info, "address": employer["address"]}
        return profile.model_copy(update=updates)
