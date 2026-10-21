from __future__ import annotations

import pytest

from jobcrawler.pipeline.categories import (
    CATEGORY_NAMES,
    MAX_CATEGORIES,
    classify_categories,
    label_categories,
    merge_categories,
    path_categories,
    title_category,
)

IT = "Công nghệ thông tin"
SALES = "Kinh doanh / Bán hàng"
FINANCE = "Tài chính / Ngân hàng / Bảo hiểm"
CONSTRUCTION = "Xây dựng"
HR = "Nhân sự / Hành chính"
MANUFACTURING = "Sản xuất / Cơ khí"
ELECTRICAL = "Điện / Điện tử / Viễn thông"
TRANSLATION = "Biên phiên dịch"
LOGISTICS = "Logistics / Thu mua / Kho vận"


def test_names_are_unique() -> None:
    assert len(set(CATEGORY_NAMES)) == len(CATEGORY_NAMES) > 20


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # JSON-LD `industry` của các trang tuyển dụng (mẫu trong tests/fixtures/boards/).
        ("IT phần mềm, Công nghệ thông tin", [IT]),
        ("IT phần cứng/mạng", [IT]),
        ("Information Technology", [IT]),
        ("Computer & Network Security", [IT]),
        ("Xây dựng, Bất động sản, An toàn lao động", [CONSTRUCTION, "Bất động sản"]),
        ("Bảo hiểm,Ngân hàng,Bán hàng - Kinh doanh,Bán hàng - Kinh doanh", [FINANCE, SALES]),
        ("Quản lý chất lượng (QA / QC), Điện / Điện tử", [MANUFACTURING, ELECTRICAL]),
        ("Cơ khí - Ô tô - Tự động hóa", [MANUFACTURING]),
        ("Giáo dục - Đào tạo,Biên phiên dịch", ["Giáo dục / Đào tạo", TRANSLATION]),
        ("Kỹ thuật xây dựng/Cơ sở hạ tầng", [CONSTRUCTION]),
        ("Human Resources", [HR]),
        ("Dược phẩm", ["Y tế / Dược / Sức khoẻ"]),
        ("Điện", [ELECTRICAL]),
        # "điện" trong "điện thoại", "tư vấn chuyên môn" không phải "vận chuyển".
        ("Điện thoại di động", []),
        ("Tư vấn chuyên môn", ["Tư vấn"]),
        ("Quan hệ đối ngoại", []),
        # "thuê" bỏ dấu cũng là "thue": cho thuê không phải thuế.
        ("Cho thuê văn phòng", ["Bất động sản"]),
        ("Thuế", ["Kế toán / Kiểm toán / Thuế"]),
        # Nguồn để đoạn mô tả nhóm vào ô phòng ban (Shopee): không phải nhãn ngành.
        ("<p>The team ensures efficient operation of the network, design and data</p>", []),
        ("The mission of the team is to ensure sustainable operation of the network", []),
    ],
)
def test_label_categories(label: str, expected: list[str]) -> None:
    assert label_categories([label]) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (["Tài chính/Ngân hàng/Bảo hiểm", "Tài chính", "Huy động vốn"], [FINANCE]),
        (
            ["Xây dựng", "Quản lý dự án xây dựng", "Giám sát dự án/công trình/thi công"],
            [CONSTRUCTION],
        ),
        # Gốc khớp hai nhóm, cấp dưới chọn một.
        (["Nhân sự/Hành chính/Pháp chế", "Nhân sự", "Tuyển dụng"], [HR]),
        (["Nhân sự/Hành chính/Pháp chế", "Pháp chế"], ["Luật / Pháp chế"]),
        # Cấp dưới khớp thêm nhóm khác không mở rộng nhóm của gốc.
        (["Kinh doanh/Bán hàng", "Sales IT Phần mềm"], [SALES]),
        (["Nhóm nghề khác", "Chuyên môn khác"], []),
    ],
)
def test_path_categories(path: list[str], expected: list[str]) -> None:
    assert path_categories(path) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chuyên Viên QS Văn Phòng ( 2 Năm Kinh Nghiệm )", CONSTRUCTION),
        ("Nhân viên kinh doanh phần mềm", SALES),  # cụm sớm nhất
        ("Kỹ sư thiết kế cơ khí", MANUFACTURING),  # cùng chỗ: cụm dài hơn
        ("Solution Architect", IT),
        ("Kiến trúc sư", CONSTRUCTION),
        ("Kế toán công trình", "Kế toán / Kiểm toán / Thuế"),
        ("Trợ lý kinh doanh", SALES),  # "trợ lý" là cụm yếu
        ("Trợ lý giám đốc", HR),
        ("Data Entry", HR),
        ("Data Analyst", IT),
        ("Phiên dịch tiếng Hàn", TRANSLATION),
        ("Kỹ sư bảo vệ thực vật", "Năng lượng / Môi trường / Nông nghiệp"),
        ("Nhân viên bảo vệ", "Lao động phổ thông"),
        ("Sản xuất video TikTok", "Báo chí / Truyền hình / Xuất bản"),
        ("Trưởng nhóm Quan hệ khách hàng (Vũng Tàu)", FINANCE),
        ("Chuyên viên bậc cao phụ trách nguồn vốn Senior Treasury Associate", FINANCE),
        ("Nhân viên vận hành máy", MANUFACTURING),
        ("Central Operation Manager", "Chăm sóc khách hàng / Vận hành"),
        ("Analista de Operação Logistica - HUB Vila Guilherme", "Logistics / Thu mua / Kho vận"),
        ("Engineer/Senior Site Reliability Engineer - Engineering Infrastructure", IT),
        ("Data Analytics Intern - Brand Analytics", IT),
        ("Kỹ thuật viên", None),
        ("", None),
        # "tư vấn tài…/chuyển…/tại…" bỏ dấu chứa "vận tải", "vận chuyển".
        ("Chuyên viên tư vấn tài chính", FINANCE),
        ("Chuyên viên tư vấn tại Hà Nội", "Tư vấn"),
        ("Nhân viên tư vấn tài khoản", "Tư vấn"),
        ("Tư vấn chuyển đổi số", "Tư vấn"),
        ("Nhân viên tư vấn chuyên nghiệp", "Tư vấn"),
        ("Tư vấn vận tải", LOGISTICS),
        # Bỏ dấu trùng từ viết tắt: "sắp", "ủi", "ít", "ai" không phải SAP, UI, IT, AI.
        ("Nhân viên sắp xếp kho", LOGISTICS),
        ("Thợ ủi", None),
        ("Nhân viên ủi đồ", None),
        ("Nhân viên văn phòng (ít nhất 1 năm kinh nghiệm)", HR),
        ("Ai cũng có thể làm - CTV bán hàng", SALES),
        ("AI Engineer", IT),
        ("SAP Consultant", IT),
        ("Nhân Viên It Helpdesk", IT),
        ("UI Designer", "Thiết kế"),
    ],
)
def test_title_category(title: str, expected: str | None) -> None:
    assert title_category(title) == expected


def test_classify_prefers_path_then_title_then_labels() -> None:
    # Có đường danh mục: bỏ nhãn ngành phẳng (TopCV: nhãn cũ do nhà tuyển dụng tự chọn).
    assert classify_categories(
        "Chuyên Viên QS", path=["Xây dựng", "Quản lý dự án xây dựng"], labels=["Bất động sản"]
    ) == [CONSTRUCTION]
    # Nhãn có thể là ngành của doanh nghiệp: tiêu đề đứng trước.
    assert classify_categories("Phiên dịch tiếng Hàn", labels=["Ô tô"]) == [
        TRANSLATION,
        MANUFACTURING,
    ]
    # Tiêu đề chỉ khớp cụm yếu ("tư vấn"): nguồn đã ghi ngành thì theo nguồn.
    assert classify_categories("Chuyên viên Tư vấn Khách hàng Cá nhân", labels=["Ngân hàng"]) == [
        FINANCE
    ]
    assert classify_categories("Chuyên viên tư vấn") == ["Tư vấn"]
    # Đường danh mục không khớp nhóm nào ("Nhóm nghề khác"): dùng nhãn phẳng.
    assert classify_categories("Nhân viên", path=["Nhóm nghề khác"], labels=["Ngân hàng"]) == [
        FINANCE
    ]
    # Trang tuyển dụng chuyên IT (TopDev, ITviec) ghi `industry` IT cho mọi tin.
    assert classify_categories(
        "Chuyên viên quan hệ khách hàng cá nhân (biết tiếng Hoa)",
        labels=["Information Technology"],
    ) == [FINANCE, IT]
    many = classify_categories("Kế toán", labels=["Ngân hàng, Bất động sản, Xây dựng, Luật"])
    assert len(many) == MAX_CATEGORIES
    assert many[0] == "Kế toán / Kiểm toán / Thuế"


def test_merge_categories_keeps_order_and_known_names() -> None:
    assert merge_categories([[IT], [SALES, IT], ["Không có trong danh mục"], [FINANCE, HR]]) == [
        IT,
        SALES,
        FINANCE,
    ]
