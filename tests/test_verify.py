from __future__ import annotations

import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jobcrawler.fakes import FakeChatClient
from jobcrawler.llm import LLMClient
from jobcrawler.pipeline import verify
from jobcrawler.pipeline.verify import CompanyContext, check_rules, employer_score, verify_jobs
from jobcrawler.schemas import ExtractionMethod

from .jobs_fixtures import NOW, board_job, normalized, raw_job

JD = raw_job().description or ""

ACME = CompanyContext(
    company_id=1,
    name="Công ty Cổ phần ACME",
    names=["ACME", "ACME Việt Nam JSC"],
    domains=["acme.vn"],
)


def no_llm() -> LLMClient:
    return LLMClient(FakeChatClient(["{}"]), model=None)


def llm_reply(**fields: Any) -> LLMClient:
    value = {
        "is_job_posting": True,
        "belongs_to_company": True,
        "is_agency": False,
        "scam_risk": 0.0,
        "confidence": 0.9,
        "reasons": ["Tin đăng trên trang careers của ACME"],
    } | fields
    return LLMClient(FakeChatClient([json.dumps(value, ensure_ascii=False)]), model="qwen")


def rules(job_fields: dict[str, Any] | None = None, ctx: CompanyContext = ACME, **kw: Any):
    return check_rules(normalized(raw_job(**(job_fields or {}))), ctx, now=NOW, **kw)


def test_clean_careers_job_is_accepted_with_reasons() -> None:
    v = rules()
    assert v.accepted
    assert v.confidence >= 0.8
    assert v.reasons == ["SOURCE_CAREERS", "EMPLOYER_MATCH"]
    assert v.code is None


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"expires_at": NOW - timedelta(days=1)}, verify.EXPIRED),
        ({"description": "<p>Tin tuyển dụng này đã hết hạn.</p>" + "<p>x</p>" * 3}, verify.EXPIRED),
        ({"description": "<p>Kế toán, lương tốt.</p>", "requirements": None}, verify.EMPTY_JD),
        ({"title": "Tuyển dụng"}, verify.NOT_A_JOB),
        ({"title": "Careers at ACME"}, verify.NOT_A_JOB),
        ({"title": "Tuyển dụng tháng 10/2026"}, verify.NOT_A_JOB),
        # Trang danh sách / lọc theo nơi làm của site tuyển dụng (Điện Máy Xanh).
        ({"title": "1 CƠ HỘI NGHỀ NGHIỆP ĐANG CHỜ BẠN"}, verify.NOT_A_JOB),
        ({"title": "11 VIỆC LÀM ĐANG CHỜ BẠN"}, verify.NOT_A_JOB),
        ({"title": "TẤT CẢ VIỆC LÀM"}, verify.NOT_A_JOB),
        ({"title": "View all jobs"}, verify.NOT_A_JOB),
        ({"title": "Tỉnh Vĩnh Long"}, verify.NOT_A_JOB),
        ({"title": "Hà Nội"}, verify.NOT_A_JOB),
        ({"title": "tìm việc làm ở Thanh Hóa"}, verify.NOT_A_JOB),
        ({"title": "Tìm thấy 4 công việc Thiết kế/ UX-UI/ Phát triển sản phẩm"}, verify.NOT_A_JOB),
        ({"title": "Thế giới việc làm, 39192 công việc đang chờ bạn"}, verify.NOT_A_JOB),
        ({"title": "Tuyển dụng nhân sự"}, verify.NOT_A_JOB),
        (
            {"expires_at": None, "url": "https://worklink.vn/tuyen-dung-ke-toan-update-11-2019/"},
            verify.EXPIRED,
        ),
        ({"apply_url": "https://tuyendung-gap.xyz/apply"}, verify.EMPLOYER_MISMATCH),
        (
            {"description": JD + "<p>Khách hàng của chúng tôi là tập đoàn FDI.</p>"},
            verify.AGENCY_POSTING,
        ),
        (
            {"description": JD + "<p>Our client is a leading bank.</p>"},
            verify.AGENCY_POSTING,
        ),
        ({"title": "Kế toán trưởng (Confidential)"}, verify.AGENCY_POSTING),
        (
            {"description": JD + "<p>Ứng viên phải đặt cọc đồng phục 500k.</p>"},
            verify.SCAM_SIGNALS,
        ),
        (
            {"description": JD + "<p>Việc nhẹ lương cao, làm tại nhà.</p>"},
            verify.SCAM_SIGNALS,
        ),
    ],
)
def test_reject_rules(fields: dict[str, Any], code: str) -> None:
    v = rules(fields)
    assert not v.accepted
    assert v.code == code
    assert any(r.startswith(code) for r in v.reasons)


def test_rules_do_not_fire_on_ordinary_wording() -> None:
    text = JD + (
        "<p>Chăm sóc khách hàng của chúng tôi tận tình. Không thu phí hồ sơ. Công ty không phải "
        "đa cấp. Nhân viên thu phí tại địa bàn. Xử lý thông tin confidential theo quy định.</p>"
    )
    v = rules({"description": text})
    assert v.accepted, v.reasons
    # "không phải đa cấp" vẫn là điểm cần LLM xem lại.
    assert v.concerns == ["dấu hiệu lừa đảo: đa cấp"]
    assert v.confidence < 0.8


def test_board_job_employer_must_match_company_or_mapped_profile() -> None:
    job = normalized(board_job("topcv", "job_2.html"))  # FPT Software
    fpt = CompanyContext(company_id=2, name="Công ty TNHH Phần mềm FPT", names=["FPT Software"])
    assert check_rules(job, fpt, now=NOW).accepted

    other = check_rules(job, ACME, now=NOW)
    assert other.code == verify.EMPLOYER_MISMATCH
    assert "FPT Software" in other.reasons[-1]

    # Profile đã map bằng MST/website dù tên khác: tên profile được tính là tên doanh nghiệp.
    viettel = CompanyContext(
        company_id=3,
        name="Tập đoàn Công nghiệp - Viễn thông Quân đội",
        profile_names=["Viettel Cyber Security"],
    )
    glints = check_rules(normalized(board_job("glints", "job_1.html")), viettel, now=NOW)
    assert glints.accepted
    assert "EMPLOYER_MATCH" in glints.reasons


def test_staffing_agency_on_board_is_agency_posting() -> None:
    # WORKLINK ("Kết nối nhân lực") đăng tuyển cho đơn vị khác ở Đồng Nai.
    job = normalized(board_job("glints", "job_2.html"))
    ctx = CompanyContext(
        company_id=4, name="Công ty khách hàng", profile_names=[job.raw.company_name or ""]
    )
    v = check_rules(job, ctx, now=NOW)
    assert v.code == verify.EXPIRED  # đăng từ 2025-10-30, không có hạn nộp
    later = check_rules(job, ctx, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert later.code == verify.AGENCY_POSTING


def test_topdev_jd_from_page_when_jsonld_has_only_headings() -> None:
    # JSON-LD chỉ có tiêu đề mục + quyền lợi; mô tả/yêu cầu đọc từ HTML trang nên tin được nhận.
    job = normalized(board_job("topdev", "job_2.html"))
    assert len(job.jd_text) >= 200
    ctx = CompanyContext(company_id=5, name="F88", names=["F88 Business JSC"])
    v = check_rules(job, ctx, now=NOW)
    assert v.accepted, v.reasons
    assert v.code is None


def test_employer_score() -> None:
    assert employer_score("FPT Software", ["fpt"]) >= 88
    assert employer_score("CÔNG TY CỔ PHẦN CÔNG NGHỆ KIOTVIET", ["kiotviet"]) >= 88
    assert employer_score("FE CREDIT", ["fecredit"]) >= 88
    assert employer_score("Công ty TNHH Tietex Việt Nam", ["acme"]) < 60
    # "Việt Nam" không làm hai tên khác nhau trở nên giống.
    assert employer_score("Công ty TNHH ABC Việt Nam", ["xyz viet nam"]) < 60


async def test_spam_reposts_keep_newest() -> None:
    old = normalized(raw_job(external_id="1", posted_at=NOW - timedelta(days=9)))
    new = normalized(raw_job(external_id="2", url="https://careers.acme.vn/jobs/2", posted_at=NOW))
    other_city = normalized(
        raw_job(external_id="3", url="https://careers.acme.vn/jobs/3", locations=["Đà Nẵng"])
    )
    verdicts = await verify_jobs([old, new, other_city], ACME, no_llm(), now=NOW, until=0)
    assert [v.code for v in verdicts] == [verify.SPAM_REPOST, None, None]
    assert "https://careers.acme.vn/jobs/2" in verdicts[0].reasons[-1]


async def test_suspicious_job_needs_llm() -> None:
    job = normalized(raw_job(method=ExtractionMethod.LLM))
    until = time.monotonic() + 10

    [rejected] = await verify_jobs([job], ACME, no_llm(), now=NOW, until=until)
    assert rejected.code == verify.LOW_CONFIDENCE
    assert "LLM chưa cấu hình" in rejected.reasons[-1]

    llm = llm_reply()
    [accepted] = await verify_jobs([job], ACME, llm, now=NOW, until=until)
    assert accepted.accepted
    assert accepted.llm
    assert accepted.confidence == 0.9
    assert accepted.reasons[-1] == "LLM_CONFIRMED: Tin đăng trên trang careers của ACME"
    chat = llm._client
    assert isinstance(chat, FakeChatClient)
    prompt = chat.calls[0]["messages"][1]["content"]
    assert "Công ty Cổ phần ACME" in prompt
    assert "Website: acme.vn" in prompt
    assert "tin do LLM bóc" in prompt

    for fields, code in [
        ({"belongs_to_company": False}, verify.EMPLOYER_MISMATCH),
        ({"is_agency": True}, verify.AGENCY_POSTING),
        ({"is_job_posting": False}, verify.NOT_A_JOB),
        ({"scam_risk": 0.7}, verify.SCAM_SIGNALS),
        ({"confidence": 0.6}, verify.LOW_CONFIDENCE),
    ]:
        [v] = await verify_jobs([job], ACME, llm_reply(**fields), now=NOW, until=until)
        assert v.code == code, fields
        assert v.llm


async def test_clean_jobs_do_not_call_llm_and_cap_is_respected() -> None:
    llm = llm_reply()
    clean = normalized(raw_job())
    suspects = [
        normalized(raw_job(external_id=f"s{i}", title=title, method=ExtractionMethod.LLM))
        for i, title in enumerate(["Kỹ sư cầu đường", "Nhân viên kho", "Lập trình viên Java"])
    ]
    verdicts = await verify_jobs(
        [clean, *suspects], ACME, llm, now=NOW, until=time.monotonic() + 10, max_llm=2
    )
    chat = llm._client
    assert isinstance(chat, FakeChatClient)
    assert len(chat.calls) == 2
    assert [v.accepted for v in verdicts] == [True, True, True, False]
    assert "vượt giới hạn LLM" in verdicts[3].reasons[-1]


async def test_every_decision_has_reasons_on_real_board_jobs() -> None:
    ctx = CompanyContext(company_id=9, name="Ngân hàng TMCP Quân Đội", names=["MB Bank"])
    jobs = [normalized(board_job("careerviet", n)) for n in ("job_1.html", "job_2.html")]
    verdicts = await verify_jobs(jobs, ctx, no_llm(), now=NOW, until=0)
    assert all(v.reasons for v in verdicts)
    assert verdicts[0].accepted  # MB Bank
    assert verdicts[1].code == verify.EMPLOYER_MISMATCH  # Tietex


async def test_llm_answers_are_cached_per_company_and_content() -> None:
    from jobcrawler.net.kv import MemoryKV

    kv = MemoryKV()
    job = normalized(raw_job(method=ExtractionMethod.LLM))
    llm = llm_reply()
    chat = llm._client
    assert isinstance(chat, FakeChatClient)
    until = time.monotonic() + 10

    [first] = await verify_jobs([job], ACME, llm, now=NOW, until=until, kv=kv)
    [again] = await verify_jobs([job], ACME, llm, now=NOW, until=until, kv=kv)
    assert first.accepted
    assert again.accepted
    assert again.reasons == first.reasons
    assert len(chat.calls) == 1  # lần hai lấy từ cache
    # Không còn LLM vẫn dùng được câu trả lời đã cache.
    [offline] = await verify_jobs([job], ACME, no_llm(), now=NOW, until=until, kv=kv)
    assert offline.accepted
    # Doanh nghiệp khác / nội dung khác: hỏi lại.
    other = dataclasses.replace(ACME, company_id=99)
    await verify_jobs([job], other, llm, now=NOW, until=until, kv=kv)
    changed = normalized(raw_job(method=ExtractionMethod.LLM, title="Kế toán thuế"))
    await verify_jobs([changed], ACME, llm, now=NOW, until=until, kv=kv)
    assert len(chat.calls) == 3


def test_old_recruitment_date_in_title_or_url_is_expired() -> None:
    old = rules(
        {
            "title": "Tuyển Dụng 11/ 2019: Tìm việc làm công nhân Hà Nội – Thái Nguyên",
            "url": "https://careers.acme.vn/tuyen-dung-11-2019-tim-viec-lam-cong-nhan/",
            "expires_at": None,
            "posted_at": None,
        }
    )
    assert old.code == verify.EXPIRED
    assert any("11/2019" in r for r in old.reasons)
    in_url = rules(
        {"url": "https://careers.acme.vn/tuyen-dung-3-2025-ke-toan/", "expires_at": None}
    )
    assert in_url.code == verify.EXPIRED
    recent = rules({"title": f"Kế toán đợt {NOW.month}/{NOW.year}", "expires_at": None})
    assert recent.code is None
