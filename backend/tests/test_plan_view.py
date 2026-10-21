"""Bộ khung cho giao diện (`modules/crawl/crawl_service.py`): trang / API poll đọc lại, lượt poll
gần
nhất đi đường nào."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jobcrawler.common.schemas import JobStatus, PlanStatus
from jobcrawler.database.models import Company, CrawlPlan
from jobcrawler.modules.crawl.crawl_schema import PlanPollResult, PollLimitsOut
from jobcrawler.modules.crawl.crawl_service import (
    PLAN_LIST_LIMIT,
    PostingRef,
    last_poll,
    plan_detail_view,
    plan_view,
)
from jobcrawler.sources.plan import ApiSpec, PlanSpec

BUILT = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
LIST = "https://tuyendung.acme.vn/viec-lam"


def _plan(spec: PlanSpec | dict[str, Any] | None = None, **values: Any) -> CrawlPlan:
    data = spec.model_dump(mode="json") if isinstance(spec, PlanSpec) else spec
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "source": "careers:tuyendung.acme.vn",
        "kind": "careers",
        "url": LIST,
        "spec": data or {},
        "status": PlanStatus.ACTIVE.value,
        "jobs": 12,
        "job_keys": [],
        "built_at": BUILT,
        "polled_at": None,
        "changed_at": None,
        "failures": 0,
        "last_error": None,
    }
    return CrawlPlan(**(fields | values))


def test_plan_view_lists_pages_and_apis_in_poll_order() -> None:
    hub = "https://tuyendung.acme.vn/ha-noi"
    spec = PlanSpec(
        listings=[LIST, f"{LIST}?page=2"],
        hubs=[hub, LIST],
        rendered=[LIST],
        api_only=[LIST],
        apis=[
            ApiSpec(
                url="https://api.acme.vn/jobs?page=1", key="page", size=20, total=45, listing=LIST
            ),
            ApiSpec(url="https://api.acme.vn/meta", method="POST", paged=False),
            ApiSpec(url=LIST, embedded=True, key="offset", kind="offset", size=10, total=30),
        ],
        ignored=["https://tuyendung.acme.vn/gioi-thieu"],
        indirect=["a1", "a2"],
        llm_listing=True,
        pages=6,
    )
    out = plan_view(_plan(spec))

    assert out.readable
    assert out.ats is None
    assert [(p.url, p.hub, p.rendered, p.api_only) for p in out.listings] == [
        (LIST, False, True, True),
        (f"{LIST}?page=2", False, False, False),
        (hub, True, False, False),
    ]
    assert out.listings_total == 3
    assert [(a.method, a.paged, a.pages, a.by_offset, a.embedded) for a in out.apis] == [
        ("GET", True, 3, False, False),
        ("POST", False, 1, False, False),
        ("GET", True, 3, True, True),
    ]
    assert out.apis[0].page_param == "page"
    assert out.apis[0].listing == LIST
    assert out.apis_total == 3
    assert (out.ignored, out.indirect, out.llm_listing) == (1, 2, True)
    # Trang SPA có API thay không tính; API nhúng tính trừ trang đầu (đọc cùng trang danh sách).
    assert (out.pages, out.pages_needed) == (6, spec.demand())


def test_plan_view_caps_long_listing_and_reads_ats_kind() -> None:
    many = PlanSpec(listings=[f"{LIST}?page={n}" for n in range(1, PLAN_LIST_LIMIT + 6)])
    out = plan_view(_plan(many))
    assert len(out.listings) == PLAN_LIST_LIMIT
    assert out.listings_total == PLAN_LIST_LIMIT + 5

    ats = PlanSpec(ats={"kind": "workday", "key": "acme/External", "url": LIST}, jobs=45)
    out = plan_view(_plan(ats, source="workday:acme/External", kind="workday"))
    assert out.ats == "workday"
    assert (out.listings, out.apis) == ([], [])
    assert out.pages_needed == 3


@pytest.mark.parametrize(
    "spec", [{}, {"version": 0, "listings": [LIST]}, {"version": 1, "apis": 5}]
)
def test_plan_view_unreadable_spec(spec: dict[str, Any]) -> None:
    out = plan_view(_plan(spec))
    assert not out.readable
    assert (out.listings, out.apis, out.pages_needed) == ([], [], 0)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"status": PlanStatus.RETIRED.value}, PlanPollResult.RETIRED),
        ({"status": PlanStatus.BROKEN.value, "polled_at": BUILT}, PlanPollResult.BROKEN),
        ({}, PlanPollResult.NEVER),
        # Lượt poll cuối thuộc bộ khung trước: lượt build sau đó ghi lại bộ khung.
        ({"polled_at": BUILT - timedelta(hours=8)}, PlanPollResult.NEVER),
        ({"polled_at": BUILT + timedelta(hours=7), "failures": 1}, PlanPollResult.RETRYING),
        (
            {
                "polled_at": BUILT + timedelta(hours=7),
                "changed_at": BUILT + timedelta(hours=7, seconds=4),
            },
            PlanPollResult.CHANGED,
        ),
        (
            {"polled_at": BUILT + timedelta(hours=7), "changed_at": BUILT + timedelta(hours=3)},
            PlanPollResult.UNCHANGED,
        ),
        ({"polled_at": BUILT + timedelta(hours=7)}, PlanPollResult.UNCHANGED),
    ],
)
def test_last_poll(values: dict[str, Any], expected: PlanPollResult) -> None:
    assert last_poll(_plan(**values)) is expected


def test_plan_detail_view_joins_job_keys_with_saved_postings() -> None:
    spec = PlanSpec(
        listings=[LIST],
        ignored=["https://tuyendung.acme.vn/gioi-thieu"],
        indirect=["k3"],
        names=[["city", "33", "Hà Nội"], ["city", "79", "Hồ Chí Minh"]],
    )
    plan = _plan(spec, job_keys=["k1", "k2", "k3", "k1"])
    company = Company(
        id=plan.company_id, input_name="acme", legal_name="Công ty ACME", brand_name=None
    )
    job_id = uuid.uuid4()
    postings = {
        "k1": PostingRef(f"{LIST}/k1", JobStatus.ACTIVE.value, True, job_id, "Kỹ sư dữ liệu"),
        "k3": PostingRef(f"{LIST}/k3", JobStatus.REMOVED.value, False, None, None),
    }
    limits = PollLimitsOut(max_failures=3, max_detail_pages=10, max_pages=10)
    out = plan_detail_view(plan, company, postings, limits)

    assert out.company_name == "Công ty ACME"
    assert out.plan.id == plan.id
    assert out.limits == limits
    assert out.jobs_total == 3
    assert [(j.key, j.title, j.status, j.indirect, j.job_id) for j in out.jobs] == [
        ("k1", "Kỹ sư dữ liệu", JobStatus.ACTIVE, False, job_id),
        ("k2", None, None, False, None),
        ("k3", None, JobStatus.REMOVED, True, None),
    ]
    assert out.jobs[2].accepted is False
    assert out.ignored_links == ["https://tuyendung.acme.vn/gioi-thieu"]
    assert [(n.kind, n.id, n.name) for n in out.names] == [
        ("city", "33", "Hà Nội"),
        ("city", "79", "Hồ Chí Minh"),
    ]
    assert out.names_total == 2
    assert out.spec == plan.spec
