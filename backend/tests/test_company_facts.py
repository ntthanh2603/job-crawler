from __future__ import annotations

from pathlib import Path

from jobcrawler.pipeline import company_facts as cf

FIXTURES = Path(__file__).parent / "fixtures"
KYC = FIXTURES / "kyc"
CAREERS = FIXTURES / "careers"


def html(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- chuỗi -----------------------------------------------------------------------------------


def test_tax_codes_need_a_label() -> None:
    text = (
        "MST: 0300588569 · Hotline 0903123456 · Mã số doanh nghiệp 0101234567-001\n"
        "GPĐKKD: 0303217354 do sở KH & ĐT cấp · Giấy CNĐKDN số 0309532909\n"
        "Tax code: 0312345678 · Số giấy chứng nhận đăng ký doanh nghiệp: 0105555555."
    )
    assert cf.tax_codes_in(text) == [
        "0300588569",
        "0101234567-001",
        "0303217354",
        "0309532909",
        "0312345678",
        "0105555555",
    ]
    assert cf.tax_codes_in("Điện thoại: 0903123456, Fax 0283812596") == []
    assert cf.valid_tax_code(" 0300588569 ") == "0300588569"
    assert cf.valid_tax_code("0101234567 - 001") == "0101234567-001"
    assert cf.valid_tax_code("12345") is None


def test_names_and_domains() -> None:
    assert cf.names_match("Công ty Cổ phần Sữa Việt Nam", "CÔNG TY CP SỮA VIỆT NAM")
    assert cf.names_match("Vinamilk JSC", "Vinamilk")
    assert cf.names_match("Ngân hàng TMCP Ngoại thương Việt Nam (Vietcombank)", "Vietcombank")
    assert cf.names_match("Thế Giới Di Động", "The Gioi Di Dong")
    assert not cf.names_match("Vietcombank", "Vietinbank")
    assert not cf.names_match("FPT", "FPT Software")
    assert not cf.names_match("Nội thất Hòa Phát", "Hòa Phát")

    assert cf.domain_matches_name("vinamilk.com.vn", "Vinamilk")
    assert cf.domain_matches_name("www.thegioididong.com", "Công ty CP Thế Giới Di Động")
    assert cf.domain_matches_name("vietcombank.com.vn", "Ngân hàng TMCP Ngoại thương (Vietcombank)")
    assert cf.domain_matches_name("fpt-software.com", "FPT Software")
    assert cf.domain_matches_name("masangroup.com", "Masan")
    assert not cf.domain_matches_name("fpt-software.com", "FPT")
    assert not cf.domain_matches_name("vietnam.vn", "Công ty TNHH Việt Nam Xanh")

    assert cf.classify("masothue.com", "Vinamilk") == "registry"
    assert cf.classify("facebook.com", "Vinamilk") == "other"
    assert cf.classify("tiki.vn", "Tiki") == "website"  # sàn TMĐT nhưng chính là DN đầu vào
    assert cf.classify("tiki.vn", "Công ty ABC") == "other"
    assert cf.classify("vinamilk.com.vn", "Vinamilk") == "website"


def test_scrub_keeps_tax_code() -> None:
    text = (
        "MST: 0300588569. Người đại diện: NGUYỄN VĂN A, Điện thoại: 0903 123 456, "
        "email a.nguyen@gmail.com, tổng đài 028.7306.5555"
    )
    out = cf.scrub(text)
    assert "0300588569" in out
    for secret in ("NGUYỄN VĂN A", "0903 123 456", "a.nguyen@gmail.com", "028.7306.5555"):
        assert secret not in out
    assert cf.HIDDEN in out


# --- đọc website ------------------------------------------------------------------------------


def test_site_facts_footer_not_testimonials() -> None:
    """base.vn: trang chủ có nhiều tên công ty khách hàng; tên pháp lý lấy từ chân trang."""
    facts = cf.site_facts(html(CAREERS / "base_vn/home.html"), "https://base.vn/")
    assert "Công ty cổ phần Base Enterprise" in facts.names
    assert not any("BIDGROUP" in n for n in facts.names)
    assert facts.tax_codes == []
    assert "Tầng 16, Tòa HUD" in facts.footer


def test_site_facts_jsonld_and_labels() -> None:
    momo = cf.site_facts(html(CAREERS / "momo/home.html"), "https://www.momo.vn/")
    assert momo.org["name"] == "MoMo"
    assert momo.org["legalName"].startswith("Công ty Cổ phần Dịch vụ Di động Trực tuyến")
    assert "CÔNG TY CỔ PHẦN DỊCH VỤ DI ĐỘNG TRỰC TUYẾN" in momo.names

    vinamilk = cf.site_facts(html(KYC / "vinamilk_home.html"), "https://www.vinamilk.com.vn/")
    assert vinamilk.tax_codes == ["0300588569"]  # "Số giấy chứng nhận đăng ký doanh nghiệp:"
    assert "CÔNG TY CỔ PHẦN SỮA VIỆT NAM" in vinamilk.names

    tgdd = cf.site_facts(html(KYC / "tgdd_home.html"), "https://www.thegioididong.com/")
    assert tgdd.tax_codes == ["0303217354"]  # "GPDKKD: 0303217354"
    assert tgdd.org["legalName"] == "CÔNG TY CỔ PHẦN ĐẦU TƯ ĐIỆN MÁY XANH"
    assert "Công Ty Cổ Phần Đầu Tư Điện Máy Xanh" in tgdd.names  # tách câu "© 2018. Công Ty..."

    tiki = cf.site_facts(html(KYC / "tiki_home.html"), "https://tiki.vn/")
    assert tiki.tax_codes == ["0309532909"]
    # Chân trang dài (từ khoá sản phẩm): vẫn giữ dòng tên pháp lý.
    assert "Công ty TNHH TI KI" in tiki.names
    assert "Công ty TNHH TI KI" in tiki.footer
    assert len(tiki.footer) <= cf.FOOTER_CHARS


def test_footer_excerpt_keeps_identity_lines() -> None:
    lines = [f"Từ khoá sản phẩm {i}" for i in range(300)]
    lines.insert(10, "Công ty TNHH ABC Việt Nam")
    lines.insert(20, "Mã số thuế: 0101234567")
    out = cf.footer_excerpt(lines, limit=300)
    assert "Công ty TNHH ABC Việt Nam" in out
    assert "Mã số thuế: 0101234567" in out
    assert out.endswith("Từ khoá sản phẩm 299")
    assert len(out) <= 300
