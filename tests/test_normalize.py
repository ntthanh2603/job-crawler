from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime, timedelta

import pytest

from jobcrawler.fakes import FakeChatClient
from jobcrawler.llm import LLMClient
from jobcrawler.pipeline import normalize, provinces
from jobcrawler.pipeline.normalize import (
    CONTACT_MASK,
    classify_employment,
    classify_level,
    classify_work_mode,
    clean_text_field,
    mask_contacts,
    normalize_title,
    parse_date,
    parse_experience,
    parse_salary,
    split_sections,
    to_markdown,
)
from jobcrawler.schemas import EmploymentType, JobLevel, SalaryPeriod, WorkMode
from jobcrawler.sources.ats import greenhouse, lever

from .jobs_fixtures import NOW, board_job, board_jobs, load_json, normalized, raw_job

# --- tỉnh/thành -----------------------------------------------------------------------------------


def test_34_provinces_after_2025_merger() -> None:
    assert len(provinces.PROVINCES) == 34
    assert len(provinces.OLD_PROVINCES) == 29  # 63 tỉnh cũ - 34 = 29 tên bị gộp
    assert all(new in provinces.PROVINCES for new, _ in provinces.OLD_PROVINCES.values())


@pytest.mark.parametrize(
    "text", ["Hà Nội", "HN", "Ha Noi", "Hanoi", "TP. Hà Nội", "Thành phố Hà Nội", "ha noi"]
)
def test_hanoi_spellings_give_same_value(text: str) -> None:
    assert provinces.province_of(text) == "Hà Nội"


@pytest.mark.parametrize(
    ("address", "province"),
    [
        ("TP.HCM", "Hồ Chí Minh"),
        ("Vietnam, Ho_Chi_Minh_City", "Hồ Chí Minh"),
        ("Thủ Dầu Một, Bình Dương", "Hồ Chí Minh"),  # tỉnh cũ → tên mới
        ("Vũng Tàu", "Hồ Chí Minh"),
        ("241 Ngô Gia Tự, Bắc Giang, VN", "Bắc Ninh"),
        ("32 Hồng Quang, Hải Dương", "Hải Phòng"),
        ("Hội An, Quảng Nam", "Đà Nẵng"),
        ("Phường Vỹ Dạ, Thừa Thiên Huế", "Huế"),
        ("Đắk Nông", "Lâm Đồng"),
        ("Rạch Giá, Kiên Giang", "An Giang"),
        # Tên nhắc sau cùng thắng: "phường Vĩnh Phúc, Hà Nội" là Hà Nội.
        ("125 Nguyễn Văn Linh, phường Vĩnh Phúc, Hà Nội", "Hà Nội"),
        ("Remote, France", None),
        ("Toàn quốc", None),
    ],
)
def test_province_of(address: str, province: str | None) -> None:
    assert provinces.province_of(address) == province


def test_find_provinces_in_title() -> None:
    assert provinces.find_provinces("Nhân viên kinh doanh - HN/HCM/Bình Dương") == [
        "Hà Nội",
        "Hồ Chí Minh",
    ]


# --- lương ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10 - 15 triệu", (10e6, 15e6, "VND", SalaryPeriod.MONTH, False)),
        ("Tới 30 triệu", (None, 30e6, "VND", SalaryPeriod.MONTH, False)),
        ("Từ 1,000 USD", (1000, None, "USD", SalaryPeriod.MONTH, False)),
        ("$1.5k-2k", (1500, 2000, "USD", SalaryPeriod.MONTH, False)),
        ("40tr-60tr ₫/tháng", (40e6, 60e6, "VND", SalaryPeriod.MONTH, False)),
        ("Upto 3000$", (None, 3000, "USD", SalaryPeriod.MONTH, False)),
        ("12,500,000 VND", (12.5e6, 12.5e6, "VND", SalaryPeriod.MONTH, False)),
        ("15.000.000đ - 20.000.000đ", (15e6, 20e6, "VND", SalaryPeriod.MONTH, False)),
        ("1,5 - 2 triệu/tuần", (1.5e6, 2e6, "VND", SalaryPeriod.MONTH, False)),
        ("200 - 300 triệu/năm", (200e6, 300e6, "VND", SalaryPeriod.YEAR, False)),
        ("50.000đ/giờ", (50_000, 50_000, "VND", SalaryPeriod.HOUR, False)),
        ("$120k - $150k", (120_000, 150_000, "USD", SalaryPeriod.YEAR, False)),
        ("10 - 15", (10e6, 15e6, "VND", SalaryPeriod.MONTH, False)),
        ("18 – 25 triệu (thỏa thuận theo năng lực)", (18e6, 25e6, "VND", SalaryPeriod.MONTH, True)),
        ("Thỏa thuận", (None, None, None, None, True)),
        ("Thoả thuận", (None, None, None, None, True)),
        ("Cạnh tranh", (None, None, None, None, True)),
        ("Negotiable", (None, None, None, None, True)),
        ("Thương lượng", (None, None, None, None, True)),
    ],
)
def test_parse_salary_text(raw: str, expected: tuple[object, ...]) -> None:
    salary = parse_salary(raw)
    assert salary is not None
    assert (salary.min, salary.max, salary.currency, salary.period, salary.negotiable) == expected
    assert salary.raw == raw


def test_parse_salary_prefers_structured_fields() -> None:
    salary = parse_salary(
        "8000000 - 12000000 VND/MONTH", low=8e6, high=12e6, currency="VND", period="MONTH"
    )
    assert salary is not None
    assert (salary.min, salary.max, salary.currency, salary.period) == (
        8e6,
        12e6,
        "VND",
        SalaryPeriod.MONTH,
    )
    yearly = parse_salary("$257K - $335K", low=257000, high=335000, currency="USD", period="1 YEAR")
    assert yearly is not None
    assert yearly.period is SalaryPeriod.YEAR


def test_parse_salary_without_amount_drops_default_currency() -> None:
    # JSON-LD của trang tuyển dụng ghi currency mặc định kể cả khi lương là "Cạnh tranh".
    salary = parse_salary("Cạnh tranh", currency="VND", period="MONTH")
    assert salary is not None
    assert (salary.currency, salary.period, salary.negotiable) == (None, None, True)
    assert parse_salary(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # KiotViet: nhãn là tiêu đề mục, giá trị ở dòng sau.
        ("##### Mức thu nhập\n\n15 - 30 Triệu/Tháng", (15e6, 30e6, False)),
        ("- **Mức lương:** Từ 9.000.000 VND đến 20.000.000 VND", (9e6, 20e6, False)),
        ("Thu nhập: Up to 1.000 USD...", (None, 1000, False)),
        ("Salary range: $1,500 - $2,500", (1500, 2500, False)),
        ("**Mức lương:** Thỏa thuận", (None, None, True)),
        # Không phải mức lương.
        ("- Lương tháng 13, thưởng lễ Tết", None),
        ("- Lương thưởng hấp dẫn", None),
        ("Thu nhập không giới hạn theo năng lực", None),
        ("Doanh thu năm 2023 đạt 1.000 tỷ", None),
    ],
)
def test_salary_in_text(text: str, expected: tuple[object, ...] | None) -> None:
    salary = normalize.salary_in_text(text)
    if expected is None:
        assert salary is None
    else:
        assert salary is not None
        assert (salary.min, salary.max, salary.negotiable) == expected


def test_normalize_job_takes_salary_from_body_only_when_missing() -> None:
    body = "<h5>Mức thu nhập</h5><p>15 - 30 Triệu</p>"
    from_body = normalized(raw_job(description=body, salary_raw=None))
    assert from_body.salary is not None
    assert (from_body.salary.min, from_body.salary.max) == (15e6, 30e6)
    structured = normalized(raw_job(description=body, salary_raw="20 - 25 triệu"))
    assert structured.salary is not None
    assert structured.salary.min == 20e6


# --- kinh nghiệm, ngày ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2 - 3 năm", (2, 3)),
        ("Trên 3 năm", (3, None)),
        ("12 months", (1, None)),
        ("3+ years", (3, None)),
        ("Dưới 1 năm", (0, 1)),
        ("Không yêu cầu kinh nghiệm", (0, None)),
        ("no requirements", (0, None)),
    ],
)
def test_parse_experience(raw: str, expected: tuple[float | None, float | None]) -> None:
    exp = parse_experience(raw)
    assert exp is not None
    assert (exp.min, exp.max) == expected


def test_parse_experience_from_jd_needs_context() -> None:
    jd = "- Ít nhất 2 năm kinh nghiệm kế toán\n- Làm việc 5 ngày/tuần"
    exp = parse_experience(jd, require_context=True)
    assert exp is not None
    assert exp.min == 2
    assert parse_experience("Công ty thành lập 10 năm", require_context=True) is None


@pytest.mark.parametrize(
    ("raw", "end_of_day", "expected"),
    [
        ("Hạn nộp: 30/11/2025", True, datetime(2025, 11, 30, 16, 59, 59, tzinfo=UTC)),
        ("30-11-2025", False, datetime(2025, 11, 29, 17, 0, tzinfo=UTC)),
        ("Ngày đăng 05.10.2026", False, datetime(2026, 10, 4, 17, 0, tzinfo=UTC)),
        ("30 tháng 11 năm 2025", True, datetime(2025, 11, 30, 16, 59, 59, tzinfo=UTC)),
        ("Nov 30, 2025", True, datetime(2025, 11, 30, 16, 59, 59, tzinfo=UTC)),
        ("2026-11-30T23:59:59+07:00", True, datetime(2026, 11, 30, 16, 59, 59, tzinfo=UTC)),
        ("2 ngày trước", False, NOW - timedelta(days=2)),
        ("Posted 30+ Days Ago", False, NOW - timedelta(days=30)),
        ("5 giờ trước", False, NOW - timedelta(hours=5)),
        ("Hôm qua", False, NOW - timedelta(days=1)),
        ("Hôm nay", False, NOW),
        ("Còn 15 ngày", True, NOW + timedelta(days=15)),
        ("3 weeks left", True, NOW + timedelta(weeks=3)),
    ],
)
def test_parse_date(raw: str, end_of_day: bool, expected: datetime) -> None:
    assert parse_date(raw, now=NOW, end_of_day=end_of_day) == expected


def test_parse_date_rejects_garbage() -> None:
    assert parse_date("Đang cập nhật", now=NOW) is None
    assert parse_date("", now=NOW) is None


# --- cấp bậc, loại hình, hình thức ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_level", "title", "level"),
    [
        (None, "Senior Backend Engineer", JobLevel.SENIOR),
        (None, "Java Backend - Middle+ - Upto 3000$", JobLevel.MIDDLE),
        (None, "Junior QA", JobLevel.JUNIOR),
        (None, "Fresher Data Analyst", JobLevel.FRESHER),
        (None, "Thực tập sinh Marketing", JobLevel.INTERN),
        (None, "Trưởng nhóm Kinh doanh", JobLevel.LEAD),
        (None, "Giám sát Kinh doanh Bán lẻ", JobLevel.LEAD),
        (None, "Trưởng phòng kế toán - Đồng Nai", JobLevel.MANAGER),
        (None, "Quản lý cửa hàng", JobLevel.MANAGER),
        (None, "Senior Manager, Finance", JobLevel.MANAGER),
        (None, "Giám đốc chi nhánh", JobLevel.DIRECTOR),
        (None, "CVC/CVCC Điều Phối Kinh Doanh", JobLevel.SENIOR),
        # Không phải cấp bậc: lĩnh vực, trợ lý, chức danh chuyên viên.
        (None, "Chuyên viên quản lý rủi ro", JobLevel.UNKNOWN),
        (None, "Trợ lý Giám đốc", JobLevel.UNKNOWN),
        (None, "Personal Relationship Manager", JobLevel.UNKNOWN),
        (None, "Lead Generation Specialist", JobLevel.UNKNOWN),
        (None, "Nhân viên kinh doanh", JobLevel.UNKNOWN),
        (None, "Chuyên viên/ Nhân viên Quản lý đối tác (Partnership Manager)", JobLevel.UNKNOWN),
        (None, "Nhân viên giám sát chất lượng", JobLevel.UNKNOWN),
        (None, "Chuyên viên cao cấp Phân tích dữ liệu", JobLevel.SENIOR),
        # Trường cấp bậc của nguồn đi trước tiêu đề.
        ("Trưởng nhóm / Giám sát", "Kỹ sư phần mềm", JobLevel.LEAD),
        ("Manager/Supervisor", "IT", JobLevel.MANAGER),
        ("Mới tốt nghiệp", "Kỹ sư", JobLevel.FRESHER),
        ("Nhân viên", "Kỹ sư", JobLevel.UNKNOWN),
    ],
)
def test_classify_level(raw_level: str | None, title: str, level: JobLevel) -> None:
    assert classify_level(raw_level, title) is level


@pytest.mark.parametrize(
    ("raw", "title", "kind"),
    [
        ("FULL_TIME", "Kế toán", EmploymentType.FULL_TIME),
        ("Full-time", "Kế toán", EmploymentType.FULL_TIME),
        ("FullTime", "Kế toán", EmploymentType.FULL_TIME),
        ("Toàn thời gian", "Kế toán", EmploymentType.FULL_TIME),
        ("PART_TIME, FULL_TIME", "Kế toán", EmploymentType.PART_TIME),
        ("Contract", "Kế toán", EmploymentType.CONTRACT),
        ("CONTRACTOR", "Kế toán", EmploymentType.CONTRACT),
        ("INTERN", "Kế toán", EmploymentType.INTERNSHIP),
        # Tiêu đề ghi rõ thì thắng giá trị mặc định FULL_TIME của trang.
        ("FULL_TIME", "Thực tập sinh Kế toán", EmploymentType.INTERNSHIP),
        ("FULL_TIME", "Nhân viên bán hàng Part-time", EmploymentType.PART_TIME),
        (None, "Cộng tác viên viết bài", EmploymentType.FREELANCE),
        (None, "Kế toán", EmploymentType.UNKNOWN),
    ],
)
def test_classify_employment(raw: str | None, title: str, kind: EmploymentType) -> None:
    assert classify_employment(raw, title) is kind


@pytest.mark.parametrize(
    ("raw", "title", "locations", "mode"),
    [
        ("TELECOMMUTE", "Dev", [], WorkMode.REMOTE),
        ("hybrid", "Dev", [], WorkMode.HYBRID),
        ("At office", "Dev", [], WorkMode.ONSITE),
        (None, "Backend Developer (Remote)", [], WorkMode.REMOTE),
        (None, "Account Executive", ["Remote, France"], WorkMode.REMOTE),
        (None, "Office Manager", ["Hà Nội"], WorkMode.UNKNOWN),
        (None, "Kế toán", ["Hà Nội"], WorkMode.UNKNOWN),
    ],
)
def test_classify_work_mode(
    raw: str | None, title: str, locations: list[str], mode: WorkMode
) -> None:
    assert classify_work_mode(raw, title, locations) is mode


# --- văn bản --------------------------------------------------------------------------------------


def test_to_markdown_cleans_html() -> None:
    html = (
        "<h2>Mô tả công việc</h2>"
        "<p>✅ Làm việc với khách hàng 🔥🔥</p>"
        "<p>• Lập báo cáo   tuần</p>"
        "<p>➤ Hỗ trợ   phòng ban</p>"
        "<p>KhÃ´ng thá»ƒ thiáº¿u</p>"  # mojibake (UTF-8 đọc nhầm cp1252) → ftfy sửa
        "<p>{'@type': 'EducationalOccupationalCredential', 'credentialCategory': 'bachelor'}</p>"
        "<p><a href='https://www.facebook.com/sharer/sharer.php?u=x'>Chia sẻ Facebook</a></p>"
        "<p>Ứng tuyển ngay</p>"
        "<p><b>Lưu tin</b></p>"
        "<h3>Việc làm liên quan</h3><p>Kế toán trưởng - Công ty khác</p>"
    )
    md = to_markdown(html)
    assert md == (
        "## Mô tả công việc\n\n- Làm việc với khách hàng\n\n- Lập báo cáo tuần\n\n"
        "- Hỗ trợ phòng ban\n\nKhông thể thiếu"
    )


def test_to_markdown_keeps_bold_and_tables() -> None:
    md = to_markdown(
        "<p><strong>Quyền lợi</strong></p><table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>"
    )
    assert md.startswith("**Quyền lợi**")
    assert "| --- | --- |" in md


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Hai thẻ in đậm liền nhau.
        ("- **Tuổi****:** Từ 18 tuổi trở lên", "- **Tuổi:** Từ 18 tuổi trở lên"),
        (
            "1. **Lấy** **khách** **hàng** **trọn****g** **tâm**",
            "1. **Lấy** **khách** **hàng** **trọng** **tâm**",
        ),
        # VNPay: "*" trang trí nằm trong thẻ in đậm.
        ("*** Số lượng cần tuyển: 06**", "- Số lượng cần tuyển: 06"),
        ("***** **Thông tin liên hệ: P. Nhân sự**", "- Thông tin liên hệ: P. Nhân sự"),
        ("*****Nice to Have**", "- Nice to Have"),
        ("*Hãy kể* ***bạn đã làm gì****.", "Hãy kể bạn đã làm gì."),
        ("***Bosch (BGSV)******is a subsidiary***", "***Bosch (BGSV)is a subsidiary***"),
        ("- *******Hồ sơ gửi đến email:****** abc", "- Hồ sơ gửi đến email: abc"),
        (
            '***Lưu ý: Ứng viên "Từ chối" sẽ không được xét*',
            '- Lưu ý: Ứng viên "Từ chối" sẽ không được xét',
        ),
        ("### **** Mục tiêu\nNội dung", "### Mục tiêu\nNội dung"),
        # Giữ nguyên: nhấn mạnh cân, chú thích (*), một dấu sao.
        ("***Đậm nghiêng***", "***Đậm nghiêng***"),
        ("**Lương:** thoả thuận (*)", "**Lương:** thoả thuận (*)"),
        ("*** Lương (**) thoả thuận**", "- Lương (**) thoả thuận"),
        ("Khách sạn 5* ở Đà Nẵng", "Khách sạn 5* ở Đà Nẵng"),
        ("**Ưu tiên:** khách sạn 5* (**)", "**Ưu tiên:** khách sạn 5* (**)"),
        (
            "với***Chính sách bảo vệ dữ liệu***của VNPAY",
            "với***Chính sách bảo vệ dữ liệu***của VNPAY",
        ),
        ("**Đậm đầu** và *nghiêng cuối*", "**Đậm đầu** và *nghiêng cuối*"),
        ("at Sun* Inc.", "at Sun* Inc."),
        # In đậm viết dính: thêm cách / bỏ cách thừa để CommonMark hiểu.
        (
            "- **Tư vấn khách hàng:**Trực tiếp hướng dẫn",
            "- **Tư vấn khách hàng:** Trực tiếp hướng dẫn",
        ),
        ("*Sales B2B:*Có hiểu biết", "*Sales B2B:* Có hiểu biết"),
        ("** Thu nhập và đãi ngộ:**", "**Thu nhập và đãi ngộ:**"),
        ("** Join Katalon as our Director", "Join Katalon as our Director"),
        ("## ** MÔ TẢ CÔNG VIỆC**\nA", "## **MÔ TẢ CÔNG VIỆC**\nA"),
        ("## **📌 MÔ TẢ CÔNG VIỆC**\nA", "## **MÔ TẢ CÔNG VIỆC**\nA"),
        (
            "*Tử tế thu hút tử tế.*[*Luật*](https://a.vn/x)",
            "*Tử tế thu hút tử tế.*[*Luật*](https://a.vn/x)",
        ),
        (
            "Online at:* [*https://a.vn/*](https://a.vn/)*,*",
            "Online at: [https://a.vn/](https://a.vn/),",
        ),
    ],
)
def test_fix_broken_emphasis(line: str, expected: str) -> None:
    assert to_markdown(line, "markdown") == expected


def test_relative_links_resolved_against_job_url() -> None:
    md = to_markdown(
        "[Tìm hiểu thêm](/careers/why-bosch/) · [Mục](#apply) · [Web](https://acme.vn/a)",
        "markdown",
        "https://www.bosch.com.vn/careers/job/123",
    )
    assert md == (
        "[Tìm hiểu thêm](https://www.bosch.com.vn/careers/why-bosch/) · Mục · [Web](https://acme.vn/a)"
    )
    assert to_markdown("[Tất cả việc làm](/jobs)", "markdown") == "Tất cả việc làm"
    assert to_markdown("[[HN] Data](/tuyen-dung/hn-data/)", "markdown", "https://ghtk.vn/a/") == (
        "[[HN] Data](https://ghtk.vn/tuyen-dung/hn-data/)"
    )
    assert to_markdown(
        '[Kế toán](/viec-lam/ke-toan-a1.html "Kế toán")', "markdown", "https://sapo.vn/x"
    ) == ("[Kế toán](https://sapo.vn/viec-lam/ke-toan-a1.html)")


def test_combining_mark_entities_are_composed() -> None:
    # Dấu viết bằng entity chỉ được giải mã sau ftfy: kết quả vẫn phải là NFC.
    md = to_markdown("<p>Đo&#769;ng BHXH theo luật định</p>")
    assert md == "Đóng BHXH theo luật định"
    assert unicodedata.is_normalized("NFC", md)


@pytest.mark.parametrize(
    "text",
    ["*+ Kỹ năng giao tiếp**:* lắng nghe tốt", "*- Đóng BHXH đầy đủ.*", "***Lưu ý:** gửi CV sớm*"],
)
def test_broken_emphasis_cleanup_is_stable(text: str) -> None:
    once = to_markdown(text, "markdown")
    assert to_markdown(once, "markdown") == once


def test_cleaning_twice_changes_nothing() -> None:
    """`jobcrawler db reclean` chạy lại bộ làm sạch trên nội dung đã lưu: phải không đổi gì."""
    html = (
        "<p>Chương trình &ldquo;Đối tác&rdquo; cho Bachelor&lsquo;s</p>"
        "<h3>Chúng tôi tìm ai?</h3><h3>Yêu cầu</h3><p><b>Tuổi</b><b>:</b> 18</p>"
        "<p>Tên / Chức danh: Vũ Tuấn - HRBP</p><p><b>*** Số lượng: 02</b></p>"
        "<h3>Quyền lợi</h3><p><a href='/ve-chung-toi'>Văn hoá</a></p>"
    )
    url = "https://acme.vn/jobs/1"
    desc, req, ben = split_sections(clean_text_field(html, "html", url))
    assert "“Đối tác”" in desc
    for part in (desc, req, ben):
        assert part
        assert clean_text_field(part, "markdown", url) == part


def test_masking_inside_emphasis_leaves_no_stray_stars() -> None:
    out = clean_text_field(
        "- ***Mọi thông tin xin liên hệ :*0222 390 6766*– P. Nhân sự***", "markdown"
    )
    assert out == f"- Mọi thông tin xin liên hệ :{CONTACT_MASK}– P. Nhân sự"


def test_masked_contact_links_collapse() -> None:
    text = (
        "Gửi CV: [an.nguyen@gmail.com](mailto:an.nguyen@gmail.com), "
        "gọi [0912 345 678](tel:0912345678)"
    )
    assert mask_contacts(text) == f"Gửi CV: {CONTACT_MASK}, gọi {CONTACT_MASK}"
    titled = '[Hotline: 0912 345 678](tel:0912345678 "Hotline")'
    assert mask_contacts(titled) == CONTACT_MASK
    kept = "[tuyendung@acme.vn](mailto:tuyendung@acme.vn)"
    assert mask_contacts(kept) == kept


@pytest.mark.parametrize(
    "text",
    [
        "Gửi CV về email: **[[email protected]](/cdn-cgi/l/email-protection#f4979c9d)**",
        "Email: [email protected]",
        "Email: [Gửi thư](https://acme.vn/cdn-cgi/l/email-protection#ab12)",
    ],
)
def test_cloudflare_email_masked(text: str) -> None:
    out = clean_text_field(text, "markdown", "https://acme.vn/jobs/1")
    assert CONTACT_MASK in out
    assert "protected" not in out
    assert "cdn-cgi" not in out


def test_split_sections() -> None:
    md = "## Mô tả công việc\n- Việc A\n\n## Yêu cầu ứng viên\n- Kỹ năng B\n\n**Quyền lợi:**\n- C"
    desc, req, ben = split_sections(md)
    assert desc == "## Mô tả công việc\n- Việc A"
    assert req == "- Kỹ năng B"
    assert ben == "- C"
    assert split_sections("Chỉ có một đoạn") == ("Chỉ có một đoạn", None, None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gọi 0912 345 678 hoặc 0912.345.678", f"Gọi {CONTACT_MASK} hoặc {CONTACT_MASK}"),
        ("Hotline: +84 912 345 678", f"Hotline: {CONTACT_MASK}"),
        ("Tel (028) 3822 1234", f"Tel {CONTACT_MASK}"),
        ("Tổng đài 1900 1234", f"Tổng đài {CONTACT_MASK}"),
        ("Gửi CV về nguyenvana@gmail.com", f"Gửi CV về {CONTACT_MASK}"),
        ("Email: thanh.nt@acme.vn", f"Email: {CONTACT_MASK}"),
        # Email ứng tuyển chung của công ty được giữ.
        ("Gửi CV về tuyendung@acme.vn hoặc hr.hcm@acme.vn", None),
        ("Liên hệ: Ms. Hương - 0912345678", f"Liên hệ: {CONTACT_MASK}"),
        ("Người liên hệ: Nguyễn Văn An", f"Người liên hệ: {CONTACT_MASK}"),
        ("LH chị Lan để biết thêm", f"LH {CONTACT_MASK} để biết thêm"),
        ("Gặp Mr John Smith tại văn phòng", f"Gặp {CONTACT_MASK} tại văn phòng"),
        # KiotViet: "Thông tin liên hệ" có nhãn tên/chức danh.
        ("- Tên / Chức danh: Vũ Tuấn - HRBP", f"- Tên / Chức danh: {CONTACT_MASK} - HRBP"),
        (
            "##### Thông tin liên hệ\n\n- Tên / Chức danh: Vũ Tuấn - HRBP\n\n- Mail: x",
            f"##### Thông tin liên hệ\n\n- Tên / Chức danh: {CONTACT_MASK} - HRBP\n\n- Mail: x",
        ),
        ("Liên hệ:\nNguyễn Văn An", f"Liên hệ:\n{CONTACT_MASK}"),
        ("Liên hệ: Tên / Chức danh: Vũ Tuấn", f"Liên hệ: Tên / Chức danh: {CONTACT_MASK}"),
        ("Họ tên: Trần Thị Mai", f"Họ tên: {CONTACT_MASK}"),
        ("Họ và tên: Lê Minh", f"Họ và tên: {CONTACT_MASK}"),
        ("Người phụ trách tuyển dụng: Ms Thu", f"Người phụ trách tuyển dụng: {CONTACT_MASK}"),
        ("Tên người liên hệ: Phạm Quang Huy", f"Tên người liên hệ: {CONTACT_MASK}"),
        # Không phải thông tin cá nhân.
        ("Tên / Chức danh: Trưởng phòng kinh doanh", None),
        ("Họ tên: ghi rõ trong CV", None),
        ("Người phụ trách: Phòng Nhân sự", None),
        ("Liên hệ với khách hàng để chốt đơn", None),
        ("Liên hệ: Phòng Nhân sự", None),
        ("Thông tin liên hệ: Số điện thoại và địa chỉ email", None),
        ("MST: 0101248141", None),
        ("Lương 8000000 - 12000000 VND, làm 08:00 - 17:00, hạn 05/11/2025", None),
        ("Ứng tuyển tại https://careers.acme.vn/jobs/123", None),
    ],
)
def test_mask_contacts(text: str, expected: str | None) -> None:
    assert mask_contacts(text) == (expected if expected is not None else text)


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("Chuyên Viên Thiết Kế Hạ Tầng (Lương 40-60 Triệu VND)", "chuyen vien thiet ke ha tang"),
        (
            "HCM -  Nhân Viên Thu Phí Tại Địa Bàn - Thu nhập từ 15tr - Không yêu cầu kinh nghiệm",
            "nhan vien thu phi tai dia ban",
        ),
        ("Kỹ Sư Hiện Trường (20-30Tr)", "ky su hien truong"),
        ("Nhân Viên Vận Hành Máy (Tuyển Gấp)", "nhan vien van hanh may"),
        (
            "CVC/CVCC Điều Phối Kinh Doanh Và Quản Trị Chất Lượng - HN/HCM - ID5592",
            "cvc cvcc dieu phoi kinh doanh va quan tri chat luong",
        ),
        (
            "Trưởng phòng kế toán - Tiếng Anh hoặc Nhật - Đồng Nai",
            "truong phong ke toan tieng anh hoac nhat",
        ),
        ("Kế toán tổng hợp tại Hà Nội", "ke toan tong hop"),
        ("Java Backend - Middle+ - Upto 3000$", "java backend middle"),
        # Không phải địa điểm: giữ nguyên.
        (
            "Giáo Viên Chủ Nhiệm Lớp - Ocean Edu Bắc Giang 2",
            "giao vien chu nhiem lop ocean edu bac giang 2",
        ),
        ("Data Engineer (Big Data / Lakehouse)", "data engineer big data lakehouse"),
    ],
)
def test_normalize_title(title: str, key: str) -> None:
    assert normalize_title(title) == key


# --- tin thật -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "raw"), board_jobs(), ids=[n for n, _ in board_jobs()])
def test_board_fixture_jobs_normalize(name: str, raw: object) -> None:
    from jobcrawler.schemas import RawJob

    assert isinstance(raw, RawJob)
    job = normalized(raw)
    assert job.board
    assert job.title
    assert job.title_normalized
    assert job.title_normalized == job.title_normalized.lower()
    assert len(job.jd_text) >= 200, name
    assert "<" not in job.description_md.replace("<br", "")  # đã chuyển HTML
    assert "{'@type'" not in job.jd_text
    # Mọi tin thật đều ở Việt Nam: có tỉnh/thành chuẩn.
    assert job.locations
    assert all(loc.province in provinces.PROVINCES for loc in job.locations)
    assert job.posted_at is not None
    assert job.posted_at.tzinfo is UTC
    if job.salary is not None and job.salary.min is not None:
        assert job.salary.currency in ("VND", "USD")
        assert job.salary.period is SalaryPeriod.MONTH


def test_board_fixture_details() -> None:
    vnw = normalized(board_job("vietnamworks", "job_1.html"))
    assert vnw.salary is not None
    assert (vnw.salary.min, vnw.salary.max, vnw.salary.currency) == (40e6, 60e6, "VND")
    assert vnw.title_normalized == "chuyen vien thiet ke ha tang"

    kiot = normalized(board_job("jobsgo", "job_1.html"))
    assert [loc.province for loc in kiot.locations] == [
        "Bắc Ninh",
        "Hải Phòng",
        "Đà Nẵng",
        "Phú Thọ",
        "Thái Nguyên",
        "Thanh Hóa",
        "Phú Thọ",  # phường Vĩnh Phúc, Vĩnh Phúc (cũ)
        "Nghệ An",
        "Quảng Ninh",
        "Hải Phòng",  # Hải Dương (cũ)
        "Hà Nội",
    ]
    assert kiot.experience_years is not None
    assert kiot.experience_years.min == 0

    ocean = normalized(board_job("vieclam24h", "job_3.html"))
    assert ocean.provinces == {"Bắc Ninh"}  # Bắc Giang cũ

    topcv = normalized(board_job("topcv", "job_1.html"))
    # Mô tả của TopCV gồm cả mục yêu cầu/quyền lợi: tách ra, không lặp.
    assert topcv.requirements_md
    assert "Tốt nghiệp Đại học" in topcv.requirements_md
    assert "Yêu cầu ứng viên" not in topcv.description_md
    assert "Quyền lợi" not in topcv.description_md

    supervisor = normalized(board_job("careerviet", "job_2.html"))
    assert supervisor.level is JobLevel.LEAD
    assert supervisor.salary is not None
    assert supervisor.salary.negotiable
    assert supervisor.experience_years is not None
    assert supervisor.experience_years.min == 5


def test_ats_fixture_jobs_normalize() -> None:
    gitlab = greenhouse.parse_jobs(load_json("greenhouse/gitlab_jobs.json"), "gitlab")
    job = normalized(gitlab[0])
    assert not job.board
    assert job.work_mode is WorkMode.REMOTE
    assert job.locations[0].province is None  # Remote, France
    assert job.locations[0].address_raw == "Remote, France"
    assert len(job.description_md) > 200

    palantir = lever.parse_postings(load_json("lever/palantir_postings.json"), "palantir")
    lv = normalized(palantir[0])
    assert (lv.work_mode, lv.employment_type) == (WorkMode.HYBRID, EmploymentType.FULL_TIME)


def test_normalize_job_parses_raw_dates_and_masks_contacts() -> None:
    raw = raw_job(
        posted_at=None,
        posted_raw="3 ngày trước",
        expires_at=None,
        expires_raw="Hạn nộp: 30/11/2026",
        description="<p>Mô tả.</p><p>Liên hệ: Ms. Hương 0912 345 678</p>",
    )
    job = normalized(raw)
    assert job.posted_at == NOW - timedelta(days=3)
    assert job.expires_at == datetime(2026, 11, 30, 16, 59, 59, tzinfo=UTC)
    assert "0912" not in job.description_md
    assert CONTACT_MASK in job.description_md


async def test_classify_unclear_asks_llm_only_for_unknown_fields() -> None:
    import time

    description = (raw_job().description or "") + "<p>Làm việc hybrid 3 ngày ở văn phòng.</p>"
    job = normalized(
        raw_job(title="Chuyên viên phân tích", employment_type="Full-time", description=description)
    )
    assert job.unclear_fields == ["level", "work_mode"]
    reply = {
        "level": "senior",
        "level_evidence": "trưởng nhóm 10 người",  # không có trong tin: bỏ
        "employment_type": "part_time",
        "employment_type_evidence": "Full-time",
        "work_mode": "hybrid",
        "work_mode_evidence": "Làm việc hybrid 3 ngày",
    }
    chat = FakeChatClient([json.dumps(reply, ensure_ascii=False)])
    llm = LLMClient(chat, model="qwen")

    filled = await normalize.classify_unclear([job], llm, until=time.monotonic() + 10, max_jobs=5)
    assert filled == 1
    # Trường rule đã quyết (full_time) không bị LLM ghi đè; cấp bậc không có bằng chứng thật.
    assert (job.level, job.employment_type, job.work_mode) == (
        JobLevel.UNKNOWN,
        EmploymentType.FULL_TIME,
        WorkMode.HYBRID,
    )
    assert len(chat.calls) == 1
    job.level = JobLevel.SENIOR
    # Không còn trường mơ hồ / không có LLM: không gọi.
    assert (
        await normalize.classify_unclear([job], llm, until=time.monotonic() + 10, max_jobs=5) == 0
    )
    disabled = LLMClient(FakeChatClient(["{}"]), model=None)
    other = normalized(raw_job(title="Chuyên viên"))
    assert (
        await normalize.classify_unclear([other], disabled, until=time.monotonic() + 10, max_jobs=5)
        == 0
    )


def test_units_keep_old_province_names() -> None:
    assert provinces.unit_of("32 Hồng Quang, Hải Dương") == "Hải Dương"
    assert provinces.unit_of("TP.HCM") == "Hồ Chí Minh"
    assert provinces.find_units("[Bình Thuận] Kinh doanh - Lâm Đồng") == ["Bình Thuận", "Lâm Đồng"]
    assert provinces.most_specific({"Bình Thuận", "Lâm Đồng"}) == {"Bình Thuận"}
    assert provinces.most_specific({"Đà Nẵng", "Hà Nội"}) == {"Đà Nẵng", "Hà Nội"}


async def test_classify_unclear_caches_answers_including_unknown() -> None:
    import time

    from jobcrawler.net.kv import MemoryKV

    kv = MemoryKV()
    reply = {"level": "unknown", "employment_type": "unknown", "work_mode": "unknown"}
    chat = FakeChatClient([json.dumps(reply)])
    llm = LLMClient(chat, model="qwen")
    for _ in range(2):
        job = normalized(raw_job(title="Chuyên viên"))
        filled = await normalize.classify_unclear(
            [job], llm, until=time.monotonic() + 10, max_jobs=5, kv=kv
        )
        assert filled == 0
        assert job.unclear_fields == ["level", "employment_type", "work_mode"]
    assert len(chat.calls) == 1  # lượt hai: tin không đổi, LLM đã trả unknown → không hỏi lại


def test_zero_salary_means_not_disclosed() -> None:
    assert parse_salary(None, low=0, high=0, currency="VND") is None
    s = parse_salary(None, low=0, high=1600, currency="USD")
    assert s is not None
    assert (s.min, s.max, s.currency) == (None, 1600, "USD")


def test_tight_bullets_become_list_items() -> None:
    md = to_markdown(
        "CHÍNH SÁCH:\n-Khi trở thành nhân viên\n*Tham gia BHXH\n*Ghi chú*\n-5% phí\n**Đậm**", "text"
    )
    lines = md.splitlines()
    assert "- Khi trở thành nhân viên" in lines
    assert "- Tham gia BHXH" in lines
    assert "*Ghi chú*" in lines  # in nghiêng cả dòng: giữ nguyên
    assert "-5% phí" in lines
    assert "**Đậm**" in lines
