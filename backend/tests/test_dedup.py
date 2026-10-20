from __future__ import annotations

from datetime import timedelta

import pytest

from jobcrawler.common.schemas import ExtractionMethod, WorkMode
from jobcrawler.pipeline.dedup import (
    cluster_jobs,
    jd_minhash,
    jd_similarity,
    job_fingerprint,
    merge,
    rank_conflict,
    stored_fingerprint,
    title_similarity,
)

from .jobs_fixtures import NOW, board_job, normalized, raw_job

JD = raw_job().description or ""


def careers(**kw: object):
    return normalized(raw_job(**kw), board=False)


def board(**kw: object):
    values: dict[str, object] = {
        "source": "topcv",
        "external_id": "t1",
        "url": "https://www.topcv.vn/viec-lam/ke-toan-tong-hop/1.html",
        "title": "Kế Toán Tổng Hợp - Hà Nội (Tuyển Gấp)",
        "salary_raw": None,
    } | kw
    return normalized(raw_job(**values), board=True)


def test_jd_minhash_similarity() -> None:
    a = jd_minhash("Hạch toán chứng từ, lập báo cáo tài chính, báo cáo thuế định kỳ hằng tháng")
    b = jd_minhash("Hạch toán chứng từ, lập báo cáo tài chính, báo cáo thuế định kỳ hằng tháng.")
    c = jd_minhash("Lập trình backend Java Spring, thiết kế API, tối ưu cơ sở dữ liệu PostgreSQL")
    assert jd_similarity(a, b) == 1.0
    assert jd_similarity(a, c) < 0.2
    assert jd_similarity(jd_minhash(""), a) == 0.0


def test_same_job_on_board_and_careers_is_one_cluster_with_careers_canonical() -> None:
    site = careers(salary_raw=None)
    topcv = board(salary_raw="15 - 20 triệu", posted_at=NOW)
    [cluster] = cluster_jobs([topcv, site])
    assert cluster.canonical is site  # trang careers của doanh nghiệp thắng trang tuyển dụng
    assert cluster.source_urls == [site.url, topcv.url]
    job = merge(cluster)
    assert job.url == site.url
    assert job.salary is not None
    assert job.salary.min == 15e6
    assert job.posted_at == site.posted_at  # ngày đăng sớm nhất


def test_different_place_or_title_or_jd_are_not_merged() -> None:
    site = careers()
    danang = board(locations=["Đà Nẵng"])
    other_title = board(external_id="t2", title="Kế toán kho")
    other_jd = board(
        external_id="t3",
        description="<p>" + "Lập trình backend Java Spring, thiết kế API REST. " * 10 + "</p>",
    )
    clusters = cluster_jobs([site, danang, other_title, other_jd])
    assert len(clusters) == 4


def test_same_apply_url_merges_despite_different_jd() -> None:
    a = careers(apply_url="https://acme.vn/apply/42")
    b = board(
        apply_url="https://acme.vn/apply/42?utm_source=topcv",
        description="<p>"
        + "Mô tả ngắn gọn khác hẳn, đăng lại trên trang tuyển dụng. " * 6
        + "</p>",
    )
    assert len(cluster_jobs([a, b])) == 1


def test_unknown_location_does_not_block_merge() -> None:
    assert len(cluster_jobs([careers(), board(locations=[])])) == 1


def test_canonical_among_boards_prefers_fuller_jd_then_newest() -> None:
    short = board(external_id="a", source="vietnamworks", posted_at=NOW)
    longer = board(
        external_id="b",
        source="itviec",
        description=JD + "<p>Được đào tạo thêm.</p>",
        posted_at=NOW - timedelta(days=5),
    )
    [cluster] = cluster_jobs([short, longer])
    assert cluster.canonical is longer
    newer = board(external_id="c", source="careerviet", posted_at=NOW + timedelta(hours=1))
    [cluster] = cluster_jobs([short, newer])
    assert cluster.canonical is newer


def test_ats_beats_careers_page() -> None:
    page = careers(method=ExtractionMethod.HEURISTIC)
    ats = careers(source="greenhouse", external_id="acme:1", method=ExtractionMethod.ATS_API)
    [cluster] = cluster_jobs([page, ats])
    assert cluster.canonical is ats


def test_vietnamworks_jsonld_and_parser_copies_merge() -> None:
    a = normalized(board_job("vietnamworks", "job_2.html"))
    b = normalized(board_job("vietnamworks", "job_2_http.html"))
    other = normalized(board_job("vietnamworks", "job_1.html"))
    clusters = cluster_jobs([a, b, other])
    assert sorted(len(c.members) for c in clusters) == [1, 2]
    merged = merge(next(c for c in clusters if len(c.members) == 2))
    assert merged.work_mode is WorkMode.UNKNOWN
    assert merged.provinces == {"Hải Phòng"}


def test_job_without_location_does_not_chain_provinces() -> None:
    """Tin không rõ địa điểm khớp mọi nơi nhưng không được nối tin Hải Phòng với tin Thái Nguyên."""
    hp = careers(external_id="hp", url="https://careers.acme.vn/jobs/hp", locations=["Hải Phòng"])
    tn = careers(external_id="tn", url="https://careers.acme.vn/jobs/tn", locations=["Thái Nguyên"])
    anywhere = board(title="Kế toán tổng hợp", locations=[])
    clusters = cluster_jobs([hp, anywhere, tn])
    assert [[m.external_id for m in c.members] for c in clusters] == [["hp", "t1"], ["tn"]]


def test_same_source_postings_need_same_known_place() -> None:
    """Cùng trang careers: "[Hải Dương]" và "[Hải Phòng]" là hai nơi tuyển (cùng Hải Phòng sau sáp
    nhập); tin không ghi nơi làm cũng là tin riêng."""
    hd = careers(
        external_id="hd",
        url="https://careers.acme.vn/jobs/hd",
        title="[Hải Dương] Chuyên Viên Kinh Doanh Phần Mềm",
        locations=[],
    )
    hp = careers(
        external_id="hp",
        url="https://careers.acme.vn/jobs/hp",
        title="[Hải Phòng] Chuyên Viên Kinh Doanh Phần Mềm",
        locations=[],
    )
    plain = careers(
        external_id="x",
        url="https://careers.acme.vn/jobs/x",
        title="Chuyên Viên Kinh Doanh Phần Mềm",
        locations=[],
    )
    assert hd.provinces == hp.provinces == {"Hải Phòng"}
    assert (hd.places, hp.places) == ({"Hải Dương"}, {"Hải Phòng"})
    assert hd.title_normalized == hp.title_normalized == plain.title_normalized
    assert len(cluster_jobs([hd, plain, hp])) == 3


def test_old_province_in_title_beats_new_province_in_address() -> None:
    lam_dong = careers(
        external_id="ld",
        url="https://careers.acme.vn/jobs/ld",
        title="Lâm Đồng| Chuyên Viên Kinh Doanh Phần Mềm",
        locations=["Số 187 Phù Đổng Thiên Vương, phường Lâm Viên - Đà Lạt, tỉnh Lâm Đồng."],
    )
    binh_thuan = careers(
        external_id="bt",
        url="https://careers.acme.vn/jobs/bt",
        title="Bình Thuận| Chuyên Viên Kinh Doanh Phần Mềm",
        locations=["J46 Tôn Đức Thắng, phường Hàm Thắng, tỉnh Lâm Đồng."],
    )
    assert binh_thuan.places == {"Bình Thuận"}
    assert len(cluster_jobs([lam_dong, binh_thuan])) == 2


def test_reposted_job_without_place_is_merged() -> None:
    """Doanh nghiệp đăng lại cùng tin với mã mới (MISA /tuyen-dung/5883 và /5920), không ghi nơi
    làm: một job."""
    first = careers(external_id="5883", url="https://careers.acme.vn/jobs/5883", locations=[])
    again = careers(external_id="5920", url="https://careers.acme.vn/jobs/5920", locations=[])
    assert not first.places
    assert not again.places
    assert len(cluster_jobs([first, again])) == 1
    assert job_fingerprint(first) == job_fingerprint(again)


def test_same_title_without_place_but_different_jd_stays_separate() -> None:
    a = careers(external_id="a", url="https://careers.acme.vn/jobs/a", locations=[])
    b = careers(
        external_id="b",
        url="https://careers.acme.vn/jobs/b",
        locations=[],
        description="<p>Phụ trách kế toán kho, kiểm kê hàng tồn và đối chiếu công nợ nhà cung cấp "
        "hằng tuần; lập báo cáo nhập xuất tồn cho chi nhánh miền Trung.</p>" * 2,
    )
    assert len(cluster_jobs([a, b])) == 2
    assert job_fingerprint(a) != job_fingerprint(b)


def test_fingerprint_distinguishes_places_and_matches_stored_row() -> None:
    hd = careers(external_id="hd", title="[Hải Dương] Kế toán tổng hợp", locations=[])
    hp = careers(external_id="hp", title="[Hải Phòng] Kế toán tổng hợp", locations=[])
    assert job_fingerprint(hd) != job_fingerprint(hp)
    # Cách tính từ một dòng `jobs` (điền lại cho dữ liệu cũ) khớp với lúc lưu.
    job = careers()
    locations = [loc.model_dump(mode="json") for loc in job.locations]
    assert stored_fingerprint(
        job.title, locations, job.description_md, job.requirements_md
    ) == job_fingerprint(job)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Java Developer", "Senior Java Developer"),
        ("Kế toán", "Kế toán trưởng"),
        ("Kỹ sư phần mềm", "Thực tập sinh Kỹ sư phần mềm"),
        ("Chuyên viên tuyển dụng", "Trợ lý Chuyên viên tuyển dụng"),
        ("Business Analyst I", "Business Analyst II"),
        ("Sales Manager", "Sales"),
    ],
)
def test_titles_differing_by_rank_are_different_jobs(a: str, b: str) -> None:
    assert rank_conflict(a, b)
    assert title_similarity(a, b) == 0.0


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Nhân viên kinh doanh", "Kinh doanh"),
        ("Kế Toán Tổng Hợp - Hà Nội (Tuyển Gấp)", "Kế toán tổng hợp"),
        ("Senior Java Developer", "Java Developer (Senior)"),
    ],
)
def test_titles_without_rank_difference_still_match(a: str, b: str) -> None:
    assert not rank_conflict(a, b)
    # Như khi gộp: so tiêu đề đã chuẩn hoá (bỏ dấu, bỏ nhiễu như "(Tuyển gấp)", tên tỉnh).
    title_a = careers(title=a).title_normalized
    title_b = careers(title=b).title_normalized
    assert title_similarity(title_a, title_b) >= 90


def test_junior_and_senior_with_template_jd_stay_separate() -> None:
    junior = careers(external_id="j", title="Java Developer", url="https://careers.acme.vn/jobs/j")
    senior = careers(
        external_id="s", title="Senior Java Developer", url="https://careers.acme.vn/jobs/s"
    )
    assert len(cluster_jobs([junior, senior])) == 2
