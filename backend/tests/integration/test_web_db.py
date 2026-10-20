"""API của giao diện web trên Postgres + Redis thật."""

from __future__ import annotations

import csv as csvlib
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from jobcrawler.config import Settings
from jobcrawler.db.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    FailedPage,
    Job,
    JobPosting,
    Log,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.queues import drop_queue, make_queue
from jobcrawler.services import maintenance
from jobcrawler.web.app import create_app

pytestmark = pytest.mark.integration


@dataclass
class Seed:
    client: httpx.AsyncClient
    settings: Settings
    tag: str
    # Id dạng chuỗi như API trả về (UUID trong JSON).
    company_id: str
    run_id: str
    job_id: str
    expired_job_id: str
    failed_page_id: str


async def seed(app_state: object, tag: str) -> tuple[UUID, str, UUID, UUID, UUID]:
    res = app_state.res  # pyright: ignore[reportAttributeAccessIssue]
    now = datetime.now(UTC)
    async with res.sessionmaker.begin() as s:
        company = Company(
            input_name=f"Web {tag}",
            legal_name=f"Công ty Cổ phần Web {tag}",
            primary_domain=f"web{tag}.vn",
            kyc_status="verified",
            kyc_confidence=0.95,
            kyc_at=now,
            kyc_evidence={
                "reason": "website ghi đúng MST",
                "signals": ["tax_code"],
                "evidence": [
                    {"url": f"https://web{tag}.vn", "kind": "footer", "snippet": "MST ..."}
                ],
            },
        )
        s.add(company)
        await s.flush()
        s.add(
            CompanyAlias(
                company_id=company.id, alias=f"Web {tag}", alias_norm=f"web {tag}", kind="input"
            )
        )
        s.add(
            SourceProfile(
                source="topcv",
                external_id=f"web-{tag}",
                url=f"https://www.topcv.vn/cong-ty/web-{tag}",
                name_on_source=f"Công ty Web {tag}",
                company_id=company.id,
                match_confidence=0.9,
                match_evidence={"signal": "name"},
            )
        )
        run_id = uuid4()
        s.add(
            CompanyRun(
                run_id=run_id,
                company_id=company.id,
                started_at=now - timedelta(seconds=2),
                finished_at=now - timedelta(seconds=1),
                duration_ms=61_000,
                status="partial",
                jobs_found=3,
                jobs_accepted=1,
                incomplete_sources=["careers:web.vn"],
            )
        )
        await s.flush()
        s.add_all(
            [
                SourceFetch(
                    run_id=run_id,
                    company_id=company.id,
                    source="careers:web.vn",
                    url=f"https://web{tag}.vn/tuyen-dung",
                    http_status=200,
                    outcome="ok",
                    duration_ms=300,
                    items=2,
                ),
                SourceFetch(
                    run_id=run_id,
                    company_id=company.id,
                    source="careers:web.vn",
                    url=f"https://web{tag}.vn/jobs/9",
                    outcome="timeout",
                    error="deadline",
                ),
                Log(
                    level="info",
                    event="company.start",
                    run_id=run_id,
                    company_id=company.id,
                    context={"x": 1},
                ),
            ]
        )
        page = FailedPage(
            url=f"https://web{tag}.vn/jobs/3",
            source="careers:web.vn",
            reason="no_job",
            run_id=run_id,
            http_status=200,
            html="<html><script>alert(1)</script>Bố cục lạ</html>",
        )
        s.add(page)
        common = {
            "company_id": company.id,
            "company_name": company.legal_name,
            "canonical_source": "careers:web.vn",
            "confidence": 0.92,
        }
        job = Job(
            cluster_key=f"{tag}-1",
            title=f"Kỹ sư Python {tag}",
            title_normalized="ky su python",
            locations=[{"province": "Hà Nội", "address_raw": "Cầu Giấy, Hà Nội"}],
            salary_min=15_000_000,
            salary_max=20_000_000,
            salary_currency="VND",
            salary_period="month",
            level="senior",
            description_md="## Mô tả\n- Viết API\n\n<script>alert('x')</script>",
            requirements_md="- 3 năm Python",
            source_urls=[f"https://web{tag}.vn/jobs/1"],
            apply_url=f"https://web{tag}.vn/jobs/1",
            **common,
        )
        expired = Job(
            cluster_key=f"{tag}-2",
            title=f"Kế toán {tag}",
            title_normalized="ke toan",
            description_md="Kế toán tổng hợp",
            status="expired",
            **common,
        )
        s.add_all([job, expired])
        await s.flush()
        s.add_all(
            [
                JobPosting(
                    source="careers:web.vn",
                    external_id=f"{tag}-1",
                    url=f"https://web{tag}.vn/jobs/1",
                    raw={"title": f"Kỹ sư Python {tag}", "url": f"https://web{tag}.vn/jobs/1"},
                    job_id=job.id,
                    company_id=company.id,
                    accepted=True,
                    confidence=0.92,
                    verification_reasons=["SOURCE_CAREERS", "EMPLOYER_MATCH"],
                    verified_at=now,
                ),
                JobPosting(
                    source="topcv",
                    external_id=f"{tag}-x",
                    url=f"https://www.topcv.vn/viec-lam/{tag}",
                    raw={"title": "Tin cũ"},
                    company_id=company.id,
                    accepted=False,
                    verification_reasons=["SOURCE_BOARD", "EXPIRED: hết hạn 2026-01-01"],
                    verified_at=now,
                ),
            ]
        )
        await s.flush()
        # Chèn thẳng vào bảng (không qua pipeline): tính các cột tóm tắt như lượt bảo trì.
        await maintenance.refresh_company_summaries(s)
        return company.id, str(run_id), job.id, expired.id, page.id


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Seed]:
    tag = uuid4().hex[:6]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jc-test-{tag}",
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "ui_password": SecretStr("mat-khau"),
        }
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            ids = await seed(app.state, tag)
            # Đăng nhập: JWT ký bằng khoá lifespan vừa đọc (như POST /api/auth/login).
            token = app.state.token_auth.issue().token
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ui.test",
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                yield Seed(client, settings, tag, *map(str, ids))
    finally:
        for name in settings.queue_names:
            await drop_queue(settings.redis_url, name)


async def test_requires_login(web: Seed) -> None:
    r = await web.client.get("/api/me", headers={"Authorization": ""})
    assert r.status_code == 401
    me = (await web.client.get("/api/me")).json()
    assert me == {"user": "admin", "auth": "jwt", "logout_url": None}
    # Đăng nhập thật bằng tài khoản trong cài đặt: cookie + token dùng được ngay.
    login = await web.client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "mat-khau"},
        headers={"Authorization": ""},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    assert (await web.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})).json()[
        "user"
    ] == "admin"
    bad = await web.client.post("/api/auth/login", json={"username": "admin", "password": "sai"})
    assert bad.status_code == 401
    meta = (await web.client.get("/api/meta")).json()
    assert "Hà Nội" in meta["provinces"]
    assert "senior" in meta["levels"]
    assert "careers" in meta["sources"]
    assert "topcv" in meta["boards"]


async def test_overview_and_stats(web: Seed) -> None:
    data = (await web.client.get("/api/overview", params={"hours": 1, "storage": True})).json()
    st = data["stats"]
    assert st["runs"]["runs"] >= 1
    assert {q["name"] for q in st["queues"]} == set(web.settings.queue_names)
    assert {p["kind"] for p in st["storage"]} == {"posting", "job_text", "html"}
    assert len(data["chart"]) == 24
    assert sum(b["total"] for b in data["chart"]) >= 1
    assert any(r["run_id"] == web.run_id for r in data["recent_runs"])
    assert len(data["fetch_chart"]) == 24
    charts = data["charts"]
    assert charts["totals"]["active_jobs"] >= 1
    assert len(charts["jobs_by_day"]) == 30
    assert "careers" in dict(charts["jobs_by_source"])
    assert "Hà Nội" in dict(charts["jobs_by_province"])
    top = charts["top_companies"]
    counts = [c["active_jobs"] for c in top]
    assert top
    assert counts == sorted(counts, reverse=True)
    if len(top) < 10:  # DB test dùng chung: đủ 10 thì doanh nghiệp seed (1 việc) có thể bị đẩy ra
        assert any(c["id"] == web.company_id for c in top)
    assert (await web.client.get("/api/stats", params={"hours": 0})).status_code == 200


async def test_companies_list_detail_and_run(web: Seed) -> None:
    page = (await web.client.get("/api/companies", params={"q": f"Web {web.tag}"})).json()
    assert page["total"] == 1
    [item] = page["items"]
    assert item["company"]["id"] == web.company_id
    assert (item["last_status"], item["active_jobs"]) == ("partial", 1)
    assert item["last_incomplete"] == ["careers:web.vn"]
    filtered = await web.client.get("/api/companies", params={"q": web.tag, "run": "ok"})
    assert filtered.json()["total"] == 0

    d = (await web.client.get(f"/api/companies/{web.company_id}")).json()
    assert d["kyc_evidence"]["reason"] == "website ghi đúng MST"
    assert [p["match_signal"] for p in d["profiles"]] == ["name"]
    assert (d["active_jobs"], d["inactive_jobs"], d["runs_total"], d["rejected_total"]) == (
        1,
        1,
        1,
        1,
    )
    assert [f["id"] for f in d["failed_pages"]] == [web.failed_page_id]
    assert (await web.client.get(f"/api/companies/{uuid4()}")).status_code == 404
    old = await web.client.get("/api/companies/123")  # link cũ dùng id số
    assert old.status_code == 404
    assert old.json()["detail"].startswith("Không tìm thấy")
    assert (await web.client.get("/api/jobs", params={"company_id": "x"})).status_code == 422
    # Danh sách con lấy theo trang.
    base = f"/api/companies/{web.company_id}"
    jobs = (await web.client.get(f"{base}/jobs")).json()
    assert [j["id"] for j in jobs["items"]] == [web.job_id]
    assert (jobs["total"], jobs["page"], jobs["pages"], jobs["exact"]) == (1, 1, 1, True)
    inactive = (await web.client.get(f"{base}/jobs", params={"status": "inactive"})).json()
    assert [j["id"] for j in inactive["items"]] == [web.expired_job_id]
    runs = (await web.client.get(f"{base}/runs")).json()
    assert [r["run_id"] for r in runs["items"]] == [web.run_id]
    rejected = (await web.client.get(f"{base}/rejected", params={"page": 9})).json()
    assert [p["title"] for p in rejected["items"]] == ["Tin cũ"]
    assert rejected["page"] == 1  # trang vượt quá → trang cuối

    run = (await web.client.get(f"/api/runs/{web.run_id}")).json()
    assert run["company_name"] == f"Công ty Cổ phần Web {web.tag}"
    assert run["by_source"] == [
        {"source": "careers:web.vn", "total": 2, "outcomes": {"ok": 1, "timeout": 1}}
    ]
    assert (run["fetches_total"], run["logs_total"]) == (2, 1)
    logs = (await web.client.get(f"/api/runs/{web.run_id}/logs")).json()
    assert [log["event"] for log in logs["items"]] == ["company.start"]
    fetches = (await web.client.get(f"/api/runs/{web.run_id}/fetches")).json()
    assert fetches["total"] == 2

    page_ = (await web.client.get(f"/api/failed-pages/{web.failed_page_id}")).json()
    assert page_["html"].startswith("<html><script>")
    raw = await web.client.get(f"/api/failed-pages/{web.failed_page_id}/raw")
    assert raw.headers["content-type"].startswith("text/plain")
    assert "attachment" in raw.headers["content-disposition"]


async def test_jobs_filters_detail_export(web: Seed) -> None:
    params = {"company_id": web.company_id}
    active = (await web.client.get("/api/jobs", params=params)).json()
    assert [j["id"] for j in active["items"]] == [web.job_id]
    every = (await web.client.get("/api/jobs", params={**params, "status": "all"})).json()
    assert {j["id"] for j in every["items"]} == {web.job_id, web.expired_job_id}
    for extra, expected in [
        ({"province": "Hà Nội"}, 1),
        ({"province": "Đà Nẵng"}, 0),
        ({"salary_from": 18}, 1),
        ({"salary_from": 25}, 0),
        ({"level": "senior"}, 1),
        ({"source": "careers"}, 1),
        ({"q": "python"}, 1),
        ({"reviewed": "yes"}, 0),
    ]:
        r = (await web.client.get("/api/jobs", params={**params, **extra})).json()
        assert r["total"] == expected, extra

    d = (await web.client.get(f"/api/jobs/{web.job_id}")).json()
    assert d["job"]["salary"]["min"] == 15_000_000
    assert "<h2>Mô tả</h2>" in d["html"]["description"]
    assert "<script>" not in d["html"]["description"]
    assert "<li>3 năm Python</li>" in d["html"]["requirements"]
    assert [p["accepted"] for p in d["postings"]] == [True]
    assert d["review"] is None

    csv = await web.client.get("/api/jobs/export", params={**params, "fmt": "csv"})
    assert csv.headers["content-type"].startswith("text/csv")
    rows_csv = list(csvlib.DictReader(io.StringIO(csv.text.lstrip("﻿"))))
    assert [r["title"] for r in rows_csv] == [f"Kỹ sư Python {web.tag}"]
    assert rows_csv[0]["provinces"] == "Hà Nội"
    jsonl = await web.client.get(
        "/api/jobs/export", params={**params, "fmt": "jsonl", "status": "all"}
    )
    rows = [json.loads(line) for line in jsonl.text.splitlines()]
    assert {r["title"] for r in rows} == {f"Kỹ sư Python {web.tag}", f"Kế toán {web.tag}"}
    bad = await web.client.get("/api/jobs/export", params={"fmt": "xml"})
    assert bad.status_code == 422


async def test_review_flow(web: Seed) -> None:
    r = await web.client.post(
        f"/api/jobs/{web.job_id}/review", json={"correct": False, "note": "tin cũ"}
    )
    assert r.json() == {"ok": True}
    d = (await web.client.get(f"/api/jobs/{web.job_id}")).json()
    assert (d["review"]["correct"], d["review"]["note"], d["review"]["reviewer"]) == (
        False,
        "tin cũ",
        "admin",
    )
    # Sửa đánh giá: vẫn một dòng.
    await web.client.post(f"/api/jobs/{web.job_id}/review", json={"correct": True})
    page = (await web.client.get("/api/review")).json()
    assert page["reviewed"] >= 1
    assert any(x["job_id"] == web.job_id and x["correct"] for x in page["recent"])
    assert (
        page["next"] is None or page["next"]["job"]["id"] != web.job_id
    )  # đã kiểm thì không gợi ý lại
    reviewed = (
        await web.client.get(
            "/api/jobs", params={"company_id": web.company_id, "reviewed": "correct"}
        )
    ).json()
    assert [j["reviewed"] for j in reviewed["items"]] == [True]
    assert (await web.client.delete(f"/api/jobs/{web.job_id}/review")).json() == {"ok": True}
    assert (await web.client.get(f"/api/jobs/{web.job_id}")).json()["review"] is None
    missing = await web.client.post(f"/api/jobs/{uuid4()}/review", json={"correct": True})
    assert missing.status_code == 404


async def test_import_enqueue_boards_queue(web: Seed) -> None:
    csv = f"name,website\nWeb mới {web.tag},webmoi{web.tag}.vn\n;;\n".encode()
    r = await web.client.post(
        "/api/companies/import",
        files={"file": ("companies.csv", csv, "text/csv")},
        data={"enqueue": "true"},
    )
    body = r.json()
    assert (body["rows"], body["created"], body["enqueued"]) == (1, 1, 1)
    bad = await web.client.post(
        "/api/companies/import", files={"file": ("x.csv", b"ten_khac\nabc\n", "text/csv")}
    )
    assert bad.status_code == 400
    assert "thiếu cột `name`" in bad.json()["detail"]

    assert (await web.client.post("/api/enqueue", json={})).status_code == 400
    first = (await web.client.post("/api/enqueue", json={"company_ids": [web.company_id]})).json()
    assert first == {"selected": 1, "enqueued": 1, "already_queued": 0}
    again = (await web.client.post("/api/enqueue", json={"company_ids": [web.company_id]})).json()
    assert again["already_queued"] == 1

    queue = (await web.client.get("/api/queue")).json()["queues"]
    build_q = next(q for q in queue if q["name"] == web.settings.build_queue_name)
    assert build_q["queued"] == 2
    # Job build chỉ mang id doanh nghiệp: tên hiện là tên pháp lý (nếu có) đọc từ DB.
    assert {j["label"] for j in build_q["jobs"]} == {
        f"Web mới {web.tag}",
        f"Công ty Cổ phần Web {web.tag}",
    }
    assert {j["function"] for j in build_q["jobs"]} == {"build_company"}
    assert {j["trigger"] for j in build_q["jobs"]} == {"manual"}
    assert {j["continuation"] for j in build_q["jobs"]} == {0}
    # Poll ngay và build lại từ trang doanh nghiệp.
    assert (await web.client.post(f"/api/companies/{web.company_id}/poll")).json() == {
        "queued": True
    }
    queue = (await web.client.get("/api/queue")).json()["queues"]
    poll_q = next(q for q in queue if q["name"] == web.settings.poll_queue_name)
    assert [(j["function"], j["label"]) for j in poll_q["jobs"]] == [
        ("poll_company", f"Công ty Cổ phần Web {web.tag}")
    ]
    crawl = (await web.client.get(f"/api/companies/{web.company_id}/crawl")).json()
    assert crawl["plans"] == []
    assert crawl["poll_interval_seconds"] == web.settings.poll_interval_seconds
    status = (await web.client.get("/api/crawl/status")).json()
    assert status["target_seconds"] == 180
    assert {"polls", "freshness", "windows"} <= set(status)

    boards = (await web.client.get("/api/boards")).json()
    assert "topcv" in {b["source"] for b in boards["boards"]}
    runs = (await web.client.get("/api/boards/runs")).json()
    assert {"items", "total", "page", "pages", "exact"} <= set(runs)
    assert boards["queue"]["name"] == web.settings.board_queue_name
    indexed = (await web.client.post("/api/boards/topcv/index")).json()
    assert indexed == {"queued": True, "workers": 0}
    assert (await web.client.post("/api/boards/topcv/index")).json()["queued"] is False
    assert (await web.client.post("/api/boards/khong-co/index")).status_code == 404
    board_queue = make_queue(web.settings, web.settings.board_queue_name)
    await board_queue.connect()
    try:
        job = await board_queue.job("board:topcv")
    finally:
        await board_queue.disconnect()
    assert job is not None
    assert job.function == "index_board"

    assert (await web.client.post("/api/maintenance")).json() == {"queued": True}
    # Request ghi từ site khác bị chặn dù đã đăng nhập.
    evil = await web.client.post("/api/maintenance", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


async def test_eval_upload(web: Seed) -> None:
    companies = f"name\nWeb {web.tag}\n".encode()
    jobs = (
        f"company,title,url\nWeb {web.tag},,https://web{web.tag}.vn/jobs/1\n"
        f"Web {web.tag},Trưởng phòng nhân sự,\n"
    ).encode()
    r = await web.client.post(
        "/api/eval",
        files={
            "companies": ("companies.csv", companies, "text/csv"),
            "jobs": ("jobs.csv", jobs, "text/csv"),
        },
    )
    report = r.json()
    assert (report["golden"], report["predicted"], report["matched"]) == (2, 1, 1)
    assert report["precision"] == 1.0
    assert report["recall"] == 0.5
    assert report["companies"][0]["missing"] == ["Trưởng phòng nhân sự"]
    assert "Precision: **100.0%**" in report["markdown"]
    broken = await web.client.post(
        "/api/eval",
        files={
            "companies": ("c.csv", b"name\nX\n", "text/csv"),
            "jobs": ("j.csv", b"a,b\n1,2\n", "text/csv"),
        },
    )
    assert broken.status_code == 400
    assert (await web.client.get("/api/eval/server")).json()["path"] == "golden"


async def test_create_company_by_hand(web: Seed) -> None:
    name = f"Thêm tay {web.tag}"
    r = await web.client.post(
        "/api/companies",
        json={"name": name, "website": f"themtay{web.tag}.vn", "tax_code": " ", "enqueue": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["queued"], body["warnings"]) == (True, True, [])
    detail = (await web.client.get(f"/api/companies/{body['id']}")).json()
    assert detail["company"]["input_name"] == name
    assert detail["input_website"] == f"themtay{web.tag}.vn"
    # Cùng tên lần nữa (khác khoảng trắng): không tạo trùng, chỉ bổ sung ghi chú.
    again = (
        await web.client.post("/api/companies", json={"name": f"  {name} ", "note": "ghi chú"})
    ).json()
    assert (again["id"], again["created"], again["queued"]) == (body["id"], False, None)
    assert (await web.client.get(f"/api/companies/{body['id']}")).json()["note"] == "ghi chú"
    # Trường sai thì báo lỗi, không lặng lẽ bỏ qua như nạp CSV.
    for bad, message in (
        ({"name": "x"}, "quá ngắn"),
        ({"name": name, "tax_code": "12ab"}, "Mã số thuế"),
        ({"name": name, "website": "không phải web"}, "Website"),
    ):
        r = await web.client.post("/api/companies", json=bad)
        assert r.status_code == 400
        assert message in r.json()["detail"]
    assert (await web.client.post("/api/companies", json={"name": ""})).status_code == 422
    events = (
        await web.client.get(
            "/api/audit", params={"action": "company.create", "resource_id": body["id"]}
        )
    ).json()["items"]
    assert [e["metadata"]["created"] for e in events] == [False, True]


async def test_import_sample_csv(web: Seed) -> None:
    from jobcrawler.services import batch

    r = await web.client.get("/api/companies/import/sample.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "doanh-nghiep-mau.csv" in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")  # BOM: Excel đọc đúng UTF-8
    # Chính là định dạng nạp danh sách: đọc lại đủ 50 doanh nghiệp, không lỗi / cảnh báo.
    report = batch.parse_companies_csv(r.content.decode("utf-8-sig"))
    assert len(report.rows) == 50
    assert (report.errors, report.warnings, report.duplicates) == ([], [], 0)
