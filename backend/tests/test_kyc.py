from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.llm import LLMClient
from jobcrawler.net.http import FetchResult
from jobcrawler.pipeline import kyc
from jobcrawler.pipeline.company_facts import SiteFacts
from jobcrawler.prompts import kyc_pick
from jobcrawler.schemas import CompanyIn, KycStatus
from jobcrawler.serp import SERPER_URL, SerpClient
from jobcrawler.sources.base import FetchRecord

FIXTURES = Path(__file__).parent / "fixtures"
KYC = FIXTURES / "kyc"
CAREERS = FIXTURES / "careers"


def html(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def llm(*replies: str) -> LLMClient:
    return LLMClient(FakeChatClient(list(replies)), model="qwen")


NO_LLM = LLMClient(FakeChatClient(["{}"]), model=None)


def pick_json(**fields: Any) -> str:
    base: dict[str, Any] = {
        "evidence": [],
        "website": None,
        "registry": None,
        "legal_name": None,
        "tax_code": None,
        "address": None,
        "short_name": None,
        "brand_name": None,
        "english_name": None,
        "several_companies": False,
        "confidence": 0.9,
        "reason": "test",
    }
    return json.dumps(base | fields, ensure_ascii=False)


def serper(answers: dict[str, dict[str, Any]], calls: list[str]) -> Any:
    """Route Serper giả: trả payload theo truy vấn, ghi lại các truy vấn đã gọi."""

    def handler(kwargs: dict[str, Any]) -> FetchResult:
        query = kwargs["json"]["q"]
        calls.append(query)
        payload = answers.get(query, {"organic": []})
        return fetch_result(SERPER_URL, json.dumps(payload), content_type="application/json")

    return handler


def organic(rank: int, url: str, title: str, snippet: str = "") -> dict[str, Any]:
    return {"position": rank, "link": url, "title": title, "snippet": snippet}


# --- chạy KYC ---------------------------------------------------------------------------------

VINAMILK_SERP = {
    "knowledgeGraph": {
        "title": "Vinamilk",
        "type": "Công ty sữa",
        "website": "https://www.vinamilk.com.vn/",
        "descriptionLink": "https://vi.wikipedia.org/wiki/Vinamilk",
    },
    "organic": [
        organic(1, "https://www.vinamilk.com.vn/", "Vinamilk - Thương hiệu sữa hàng đầu Việt Nam"),
        organic(2, "https://vi.wikipedia.org/wiki/Vinamilk", "Vinamilk – Wikipedia tiếng Việt"),
        organic(
            3,
            "https://masothue.com/0300588569-cong-ty-co-phan-sua-viet-nam",
            "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
            "Tên quốc tế VIET NAM DAIRY PRODUCTS JOINT STOCK COMPANY. Người đại diện MAI KIỀU LIÊN",
        ),
        organic(4, "https://www.facebook.com/vinamilkofficial", "Vinamilk | Facebook"),
        organic(5, "https://daily-sua-vinamilk.vn/", "Đại lý sữa Vinamilk giá sỉ"),
    ],
}


def vinamilk_fetcher(calls: list[str]) -> FakeFetcher:
    fetcher = FakeFetcher()
    fetcher.add_route(SERPER_URL, serper({"Vinamilk": VINAMILK_SERP}, calls))
    fetcher.add_file("https://www.vinamilk.com.vn/", KYC / "vinamilk_home.html")
    return fetcher


async def test_verified_via_serp_footer_and_llm() -> None:
    calls: list[str] = []
    fetcher = vinamilk_fetcher(calls)
    answer = pick_json(
        website="W1",
        registry="R1",
        legal_name="CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
        tax_code="0300588569",
        brand_name="Vinamilk",
        english_name="Vietnam Dairy Products JSC",  # không có trong ứng viên → bỏ
        evidence=[
            {"candidate": "W1", "quote": "Số giấy chứng nhận đăng ký doanh nghiệp: 0300588569"},
            {"candidate": "R1", "quote": "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM"},
            {"candidate": "W1", "quote": "Vinamilk là công ty sữa lớn nhất thế giới"},  # bịa
        ],
        confidence=0.95,
    )
    client = llm(answer)
    records: list[FetchRecord] = []
    result = await kyc.run_kyc(
        CompanyIn(name="Vinamilk"),
        fetcher=fetcher,
        llm=client,
        serp=SerpClient(fetcher, api_key="k"),
        records=records,
    )

    assert result.status is KycStatus.VERIFIED
    assert result.error is None
    assert result.primary_domain == "vinamilk.com.vn"
    assert result.strong_domains == ["vinamilk.com.vn"]
    assert (result.legal_name, result.tax_code, result.brand_name) == (
        "CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
        "0300588569",
        "Vinamilk",
    )
    assert result.english_name is None
    assert {"site_name", "panel", "registry_tax_code", "domain_name"} <= set(result.signals)
    llm_evidence = [(e.url, e.snippet) for e in result.evidence if e.kind == "llm"]
    assert llm_evidence == [
        ("https://www.vinamilk.com.vn/", "Số giấy chứng nhận đăng ký doanh nghiệp: 0300588569"),
        (
            "https://masothue.com/0300588569-cong-ty-co-phan-sua-viet-nam",
            "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
        ),
    ]
    assert result.aliases() == [("legal", "CÔNG TY CỔ PHẦN SỮA VIỆT NAM"), ("brand", "Vinamilk")]
    assert (result.serp, result.llm) == ("serper", "kyc_pick@" + kyc_pick.PROMPT.version)

    # Một truy vấn đã có trang tra cứu MST: không tốn thêm truy vấn thứ hai.
    assert calls == ["Vinamilk"]
    kinds = {c.id: c.kind for c in result.candidates}
    assert kinds == {
        "W1": "website",
        "W2": "website",
        "R1": "registry",
        "C1": "other",
        "C2": "other",
    }
    registry = next(c for c in result.candidates if c.id == "R1")
    assert registry.tax_codes == ["0300588569"]
    assert registry.snippet is not None
    assert "MAI KIỀU LIÊN" not in registry.snippet
    assert {r.source for r in records} == {
        "serp:serper",
        "kyc:vinamilk.com.vn",
        "kyc:daily-sua-vinamilk.vn",
    }

    # Prompt chỉ chứa ứng viên đã đánh mã; website đại lý được liệt kê nhưng không được chọn.
    [call] = client._client.calls  # pyright: ignore[reportAttributeAccessIssue,reportOptionalMemberAccess]
    prompt = call["messages"][1]["content"]
    assert "[W1] website vinamilk.com.vn" in prompt
    assert "[R1] tra cứu doanh nghiệp masothue.com" in prompt
    assert "MST ghi trên trang: 0300588569" in prompt


async def test_several_companies_is_ambiguous() -> None:
    calls: list[str] = []
    fetcher = FakeFetcher()
    fetcher.add_route(
        SERPER_URL,
        serper(
            {
                "Sao Mai": {
                    "organic": [
                        organic(1, "https://saomai-a.vn/", "Sao Mai A - Vật liệu xây dựng"),
                        organic(2, "https://saomaigroup.com.vn/", "Tập đoàn Sao Mai"),
                    ]
                },
                "Sao Mai mã số thuế": {
                    "organic": [
                        organic(
                            1,
                            "https://masothue.com/1600000001-a",
                            "1600000001 - CÔNG TY TNHH SAO MAI",
                        ),
                        organic(
                            2,
                            "https://masothue.com/0100000002-b",
                            "0100000002 - CÔNG TY CP SAO MAI",
                        ),
                    ]
                },
            },
            calls,
        ),
    )
    fetcher.add(
        "https://saomai-a.vn/", "<html><body><footer>Công ty TNHH Sao Mai A</footer></body></html>"
    )
    fetcher.add(
        "https://saomaigroup.com.vn/",
        "<html><body><footer>Công ty CP Tập đoàn Sao Mai · MST: 1400000003</footer></body></html>",
    )
    answer = pick_json(
        website="W2",
        evidence=[{"candidate": "W2", "quote": "Công ty CP Tập đoàn Sao Mai"}],
        several_companies=True,
        confidence=0.6,
    )
    result = await kyc.run_kyc(
        CompanyIn(name="Sao Mai"),
        fetcher=fetcher,
        llm=llm(answer),
        serp=SerpClient(fetcher, api_key="k"),
    )
    assert calls == ["Sao Mai", "Sao Mai mã số thuế"]  # truy vấn 1 không có trang tra cứu MST
    assert result.status is KycStatus.AMBIGUOUS
    assert result.primary_domain is None  # không thu job từ domain chưa chắc
    assert result.strong_domains == []
    assert result.aliases() == []
    assert [c.id for c in result.candidates] == ["W1", "W2", "R1", "R2"]
    assert result.picked == "W2"


async def test_input_tax_code_conflict_keeps_only_input_website() -> None:
    fetcher = FakeFetcher()
    fetcher.add_file("https://vinamilk.com.vn/", KYC / "vinamilk_home.html")
    answer = pick_json(
        website="W1",
        legal_name="CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
        tax_code="0300588569",
        evidence=[{"candidate": "W1", "quote": "CÔNG TY CỔ PHẦN SỮA VIỆT NAM"}],
        confidence=0.95,
    )
    serp_fetcher = FakeFetcher()
    result = await kyc.run_kyc(
        CompanyIn(name="Vinamilk", tax_code="0101234567", website="vinamilk.com.vn"),
        fetcher=fetcher,
        llm=llm(answer),
        serp=SerpClient(serp_fetcher, api_key="k"),
    )
    # Website ghi MST khác MST đầu vào: tra SERP để đối chiếu (ở đây SERP lỗi, bỏ qua).
    assert [c[2]["json"]["q"] for c in serp_fetcher.calls] == ["Vinamilk", "0101234567"]
    assert result.status is KycStatus.AMBIGUOUS
    assert {"input_domain", "site_tax_code_mismatch", "tax_code_mismatch"} <= set(result.signals)
    assert result.primary_domain == "vinamilk.com.vn"  # website người dùng đưa vẫn được thu
    assert result.aliases() == []


async def test_matching_tax_code_makes_domain_strong_even_if_ambiguous() -> None:
    calls: list[str] = []
    fetcher = FakeFetcher()
    fetcher.add_route(
        SERPER_URL,
        serper(
            {"Tiki": {"organic": [organic(1, "https://tiki.vn/", "Tiki - Mua hàng online")]}}, calls
        ),
    )
    fetcher.add_file("https://tiki.vn/", KYC / "tiki_home.html")
    answer = pick_json(website="W1", confidence=0.5)
    result = await kyc.run_kyc(
        CompanyIn(name="Tiki", tax_code="0309532909"),
        fetcher=fetcher,
        llm=llm(answer),
        serp=SerpClient(fetcher, api_key="k"),
    )
    assert calls == ["Tiki", "0309532909"]  # MST đầu vào chưa xuất hiện → tra thêm theo MST
    assert result.status is KycStatus.AMBIGUOUS  # LLM không chắc, không có trích đoạn
    assert "tax_code" in result.signals
    assert result.strong_domains == ["tiki.vn"]
    assert result.primary_domain == "tiki.vn"


async def test_rules_without_llm() -> None:
    fetcher = FakeFetcher()
    fetcher.add_file("https://momo.vn/", CAREERS / "momo/home.html")
    result = await kyc.run_kyc(
        CompanyIn(name="MoMo", website="momo.vn"),
        fetcher=fetcher,
        llm=NO_LLM,
        serp=SerpClient(None),
    )
    assert result.status is KycStatus.VERIFIED
    assert result.llm is None
    assert {"input_domain", "site_name"} <= set(result.signals)
    assert result.primary_domain == "momo.vn"
    assert result.legal_name is not None
    assert result.legal_name.startswith("Công ty Cổ phần Dịch vụ Di động Trực tuyến")
    assert result.confidence == pytest.approx(0.85)

    # Không website, không SERP: không có gì để tìm.
    empty = FakeFetcher()
    nothing = await kyc.run_kyc(
        CompanyIn(name="Công ty TNHH Không Ai Biết"),
        fetcher=empty,
        llm=llm(pick_json()),
        serp=SerpClient(empty, api_key=None),
    )
    assert nothing.status is KycStatus.NOT_FOUND
    assert nothing.serp is None
    assert nothing.candidates == []
    assert empty.calls == []


async def test_ats_board_url_is_not_a_company_website() -> None:
    result = await kyc.run_kyc(
        CompanyIn(name="GitLab", website="https://job-boards.greenhouse.io/gitlab"),
        fetcher=FakeFetcher(),
        llm=NO_LLM,
        serp=SerpClient(None),
    )
    assert result.status is KycStatus.NOT_FOUND
    assert result.primary_domain is None
    assert result.strong_domains == []


def test_validate_pick_drops_unsupported_values() -> None:
    cands = kyc.CandidateSet("ABC")
    site = cands.website("abc.vn", "https://abc.vn/")
    site.add_doc(
        kyc.Doc(url="https://abc.vn/", label="Trang chủ", text="Công ty TNHH ABC · MST: 0101234567")
    )
    pick = kyc_pick.KycPick.model_validate_json(
        pick_json(
            website="W9",
            tax_code="0999999999",
            legal_name="Công ty TNHH ABC",
            brand_name="ABC Group",  # không có trong ứng viên
            english_name="ABC Company Limited",  # tự dịch
            address="1 Đường Không Có, Hà Nội",
            evidence=[
                {"candidate": "W1", "quote": "công ty tnhh abc"},  # khác hoa thường: vẫn nhận
                {"candidate": "R1", "quote": "Công ty TNHH ABC"},  # mã không tồn tại
            ],
            confidence=0.97,
        )
    )
    out = kyc.validate_pick(pick, cands)
    assert out.website is None
    assert out.tax_code is None
    assert out.legal_name == "Công ty TNHH ABC"
    assert (out.brand_name, out.english_name, out.address) == (None, None, None)
    assert [e.url for e in out.evidence] == ["https://abc.vn/"]
    assert set(out.dropped) >= {"website=W9", "tax_code=0999999999", "brand_name=ABC Group"}


async def test_timeout_returns_partial_result() -> None:
    fetcher = FakeFetcher(delay=0.5)
    fetcher.add_file("https://momo.vn/", CAREERS / "momo/home.html")
    result = await kyc.run_kyc(
        CompanyIn(name="MoMo", website="momo.vn"),
        fetcher=fetcher,
        llm=NO_LLM,
        serp=SerpClient(None),
        timeout=0.2,
    )
    assert result.error == kyc.TIMEOUT
    assert result.primary_domain == "momo.vn"  # domain đầu vào vẫn dùng được


def test_registry_of_namesake_is_not_attached_to_website() -> None:
    """Website tự ghi đúng tên nhưng bản ghi MST LLM chọn không gắn được với website (khác MST,
    khác tên pháp lý): giữ website, bỏ MST/tên pháp lý để không gộp nhầm theo MST."""
    cands = kyc.CandidateSet("ABC")
    site = cands.website("abc.vn", "https://abc.vn/")
    site.facts.append(SiteFacts(url="https://abc.vn/", names=["ABC"]))
    site.add_doc(kyc.Doc(url="https://abc.vn/", label="Trang chủ", text="ABC · Phần mềm quản lý"))
    reg = kyc.Candidate(
        id="R1",
        kind="registry",
        domain="masothue.com",
        url="https://masothue.com/0109999999-abc",
        tax_codes=["0109999999"],
    )
    reg.add_doc(
        kyc.Doc(
            url=reg.url, label="Kết quả tìm kiếm #2", text="0109999999 - CÔNG TY TNHH ABC SỐ MỘT"
        )
    )
    cands.registry[reg.url] = reg
    pick = kyc.validate_pick(
        kyc_pick.KycPick.model_validate_json(
            pick_json(
                website="W1",
                registry="R1",
                tax_code="0109999999",
                legal_name="CÔNG TY TNHH ABC SỐ MỘT",
                evidence=[{"candidate": "W1", "quote": "ABC · Phần mềm quản lý"}],
            )
        ),
        cands,
    )
    result = kyc.decide(CompanyIn(name="ABC"), cands, pick)
    assert result.status is KycStatus.VERIFIED
    assert result.primary_domain == "abc.vn"
    assert (result.tax_code, result.legal_name) == (None, None)
    assert {"tax_code_unlinked", "legal_name_unlinked"} <= set(result.signals)


async def test_www_fallback_and_brand_is_not_a_legal_name() -> None:
    fetcher = FakeFetcher()  # vinamilk.com.vn (không www) trả lỗi, chỉ www mở được
    fetcher.add_file("https://www.vinamilk.com.vn/", KYC / "vinamilk_home.html")
    answer = pick_json(
        website="W1",
        legal_name="Vinamilk",  # không có loại hình pháp lý → thương hiệu
        evidence=[{"candidate": "W1", "quote": "Bản quyền thuộc về Vinamilk © 2025"}],
    )
    result = await kyc.run_kyc(
        CompanyIn(name="Vinamilk", website="vinamilk.com.vn"),
        fetcher=fetcher,
        llm=llm(answer),
        serp=SerpClient(None),
    )
    assert [c[1] for c in fetcher.calls if c[0] == "fetch"][:2] == [
        "https://vinamilk.com.vn/",
        "https://www.vinamilk.com.vn/",
    ]
    assert result.status is KycStatus.VERIFIED
    assert result.tax_code == "0300588569"  # MST duy nhất ghi trên website
    assert (result.legal_name, result.brand_name) == (None, "Vinamilk")


async def test_no_page_evidence_is_not_verified() -> None:
    """Website đầu vào không mở được: LLM vẫn "chắc" nhưng không có trích đoạn nào từ trang."""
    answer = pick_json(website="W1", confidence=0.95)
    result = await kyc.run_kyc(
        CompanyIn(name="Vinamilk", website="vinamilk.com.vn"),
        fetcher=FakeFetcher(),
        llm=llm(answer),
        serp=SerpClient(None),
    )
    assert result.status is KycStatus.AMBIGUOUS
    assert result.primary_domain == "vinamilk.com.vn"  # vẫn là domain người dùng đưa
