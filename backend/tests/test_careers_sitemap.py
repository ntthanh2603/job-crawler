"""Site careers kiểu tuyendung.frt.vn (sources/careers.py): thẻ tin là link `/<slug>-<mã>` ở gốc
site, trang danh mục dẫn tới tin, danh sách đủ chỉ nạp bằng XHR nhưng sitemap có mọi tin, mô tả
công việc nằm trong form "Ứng tuyển"."""

from __future__ import annotations

from typing import Any

from jobcrawler.common.schemas import ExtractionMethod, RawJob
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.sources.base import KnownJobs, SourceSession
from jobcrawler.sources.careers import CareersSource
from jobcrawler.sources.plan import PlanSpec, SitemapSpec

SITE = "https://tuyendung.frt.vn"
HUB = f"{SITE}/tuyen-dung/fptshop"
HOME_JOBS = {
    "ho-chi-minh-duoc-si-ban-thuoc-11510": "[Hồ Chí Minh] Dược Sĩ Bán Thuốc",
    "quan-12-ho-chi-minh-duoc-si-ban-thuoc-11517": "[Quận 12 - Hồ Chí Minh] Dược Sĩ Bán Thuốc",
    "quan-ly-du-an-drone-nong-nghiep-16271": "Quản lý Dự Án Drone Nông Nghiệp",
}
HUB_JOBS = {
    "tp-nha-trang-nhan-vien-ban-hang-da-nhiem-17316": "[TP. Nha Trang] Nhân viên Bán hàng",
    "chau-doc-tu-van-ban-hang-da-nhiem-15719": "[Châu Đốc] Tư Vấn Bán Hàng Đa Nhiệm",
    "garmin-121-le-loi-tu-van-ban-hang-dong-ho-garmin-17405": "[Garmin] Tư Vấn Bán Hàng",
}
# Chỉ có trong sitemap (trang danh sách nạp thêm bằng XHR khi cuộn).
SITEMAP_JOBS = {
    "vinh-long-duoc-si-ban-thuoc-14888": "Dược Sĩ Bán Thuốc",
    "dong-thap-duoc-si-ban-thuoc-14685": "Dược Sĩ Bán Thuốc",
}


def card(slug: str, title: str) -> str:
    return (
        f"<div class='list-item'><a href='/{slug}' title=\"{title}\"></a>"
        f"<h5>{title}</h5><span>Hồ Chí Minh</span></div>"
    )


MENU = (
    "<div class='menu'><a href='/'>TRANG CHỦ</a><a href='/gioi-thieu'>GIỚI THIỆU</a>"
    "<a href='/tuyen-dung/fptshop'>FPT Shop</a><a href='/tin-tuc'>TIN TỨC</a></div>"
    "<form action='/Search'><input name='q'><button>Tìm</button></form>"
)


def home() -> str:
    cards = "".join(card(s, t) for s, t in HOME_JOBS.items())
    return f"<html><body>{MENU}<div class='list'>{cards}</div></body></html>"


def hub() -> str:
    cards = "".join(card(s, t) for s, t in HUB_JOBS.items())
    return f"<html><body>{MENU}<h2>FPT Shop</h2>{cards}</body></html>"


def detail(slug: str, title: str) -> str:
    """Trang tin như tuyendung.frt.vn: không có `main` / `article`; menu, cột giới thiệu công ty,
    khối "Tin Liên Quan" (có cả chính tin này kèm nơi làm), chân trang, khung chat nằm cạnh mô
    tả; mô tả nằm trong form "Ứng tuyển", bảng liên hệ có tên người."""
    duties = "".join(
        f"<p>• Nhiệm vụ {i}: tư vấn sản phẩm, bán hàng, chăm sóc khách hàng tại cửa hàng.</p>"
        for i in range(8)
    )
    return f"""<html><head><meta property="og:title" content="{title}"></head><body>{MENU}
    <form action="/Applicant/Apply" method="post"><input type="hidden" name="t" value="x">
      <div class="main--content--head"><div class="vacancies">Vị Trí Tuyển Dụng</div>
        <h3>{title.upper()}</h3></div>
      <div class="main--content--subhead"><ul><li>Số lượng cần tuyển: <strong>2</strong></li>
        <li>Loại hình công việc: <strong>Toàn thời gian</strong></li>
        <li>Mức lương: <strong><strong>lương thỏa thuận</strong></strong></li>
        <li><p style="display : inline">Hạn nộp hồ sơ:</p><strong> 02-11-2026</strong></li>
      </ul></div>
      <div class="main--content--body"><h4>CHI TIẾT CÔNG VIỆC</h4>{duties}
        <h4>YÊU CẦU CÔNG VIỆC</h4><p>• Tốt nghiệp trung cấp trở lên, giao tiếp tốt.</p>
        <h4>QUYỀN LỢI</h4><ul><li>Thưởng theo doanh số, BHXH đầy đủ.</li></ul>
        <h4>THÔNG TIN THAM KHẢO</h4><ul><li>Tìm hiểu về FPT Retail <a href="/gioi-thieu">tại
          đây</a></li></ul></div>
      <div class="main--content--foot"><h3>THÔNG TIN LIÊN HỆ</h3><table><tr>
        <td>Thông tin liên hệ:</td><td>Nguyễn Thị Hoa</td></tr></table>
        <button type="submit">Ứng tuyển</button></div>
    </form>
    <div class="main--right"><h3>Công Ty Cổ Phần Bán Lẻ Kỹ Thuật Số FPT</h3>
      <p>FPT Retail được thành lập từ năm 2012, sở hữu chuỗi FPT Shop và FPT Long Châu.</p>
      <h3>Tin Liên Quan</h3><ul><li><a href="/{slug}"><b>{title}</b><br>
        <span>Hồ Chí Minh</span></a></li></ul></div>
    <div class="nav-footer">VỀ FPT RETAIL <a href="/gioi-thieu">Giới thiệu chung</a></div>
    <div class="chat">Chat với FPT Shop · Tuyển dụng FPT Retail đang trả lời...</div>
    </body></html>"""


def urlset(*entries: tuple[str, str | None]) -> str:
    rows = "".join(
        f"<url><loc>{SITE}/{path}</loc>"
        + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
        + "</url>"
        for path, lastmod in entries
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset>{rows}</urlset>'


def sitemap(jobs: dict[str, str]) -> str:
    return urlset(
        ("", None),
        ("gioi-thieu", None),
        # Tin tức: khuôn URL khác (/tin-tuc/...), không phải tin.
        ("tin-tuc/fpt-retail-duoc-vinh-danh-34", None),
        ("tin-tuc/nha-thuoc-long-chau-33", None),
        ("tin-tuc/fpt-shop-ban-hang-online-32", None),
        # Tin cũ sitemap còn giữ (lastmod quá SITEMAP_MAX_AGE_DAYS).
        ("ha-noi-thu-ngan-10021", "2020-01-15"),
        *((slug, "2026-10-01") for slug in jobs),
    )


def all_jobs() -> dict[str, str]:
    return {**HOME_JOBS, **HUB_JOBS, **SITEMAP_JOBS}


def frt(jobs: dict[str, str] | None = None) -> FakeFetcher:
    jobs = all_jobs() if jobs is None else jobs
    fetcher = FakeFetcher()
    fetcher.add(f"{SITE}/", home())
    fetcher.add(HUB, hub())
    fetcher.add(f"{SITE}/sitemap.xml", sitemap(jobs), content_type="text/xml; charset=utf-8")
    for slug, title in {**jobs, "ha-noi-thu-ngan-10021": "Thu ngân"}.items():
        fetcher.add(f"{SITE}/{slug}", detail(slug, title))
    return fetcher


def session(fetcher: FakeFetcher, **kw: Any) -> SourceSession:
    return SourceSession(
        "careers:tuyendung.frt.vn", fetcher, LLMClient(FakeChatClient(["{}"]), model=None), **kw
    )


async def build(fetcher: FakeFetcher) -> tuple[SourceSession, list[RawJob]]:
    s = session(fetcher)
    return s, [job async for job in CareersSource(f"{SITE}/").jobs(s)]


async def poll(fetcher: FakeFetcher, spec: PlanSpec, jobs: list[RawJob]) -> SourceSession:
    known = KnownJobs.of_refs((j.source, j.external_id, j.url) for j in jobs)
    s = session(fetcher, mode="poll", plan=spec, known=known)
    s.polled = [job async for job in CareersSource(f"{SITE}/").jobs(s)]  # pyright: ignore[reportAttributeAccessIssue]
    return s


async def test_build_reads_cards_hubs_and_sitemap() -> None:
    s, jobs = await build(frt())
    assert sorted(j.url for j in jobs) == sorted(f"{SITE}/{slug}" for slug in all_jobs())
    # Mô tả trong form "Ứng tuyển": đủ dấu hiệu, không cần LLM; trường có nhãn bóc được.
    job = next(j for j in jobs if j.url.endswith("-11517"))
    assert job.method is ExtractionMethod.HEURISTIC
    assert job.title == "[Quận 12 - Hồ Chí Minh] Dược Sĩ Bán Thuốc"
    description = job.description or ""
    assert description.startswith("#### CHI TIẾT CÔNG VIỆC")
    assert "Nhiệm vụ 7" in description
    assert "#### QUYỀN LỢI" in description
    # Không lấy menu, phần đầu tin, bảng liên hệ, cột bên, tin liên quan, chân trang, chat.
    for noise in ("TRANG CHỦ", "Vị Trí Tuyển Dụng", "Nguyễn", "2012", "Tin Liên Quan", "VỀ FPT"):
        assert noise not in description
    assert "Chat với" not in description
    # Trang không ghi địa điểm: lấy từ thẻ "Tin Liên Quan" của chính tin.
    assert job.locations == ["Hồ Chí Minh"]
    assert job.extra["openings"] == "2"
    assert job.salary_raw == "lương thỏa thuận"
    assert job.expires_raw == "02-11-2026"
    assert job.employment_type == "Toàn thời gian"
    assert s.plan.listings == [f"{SITE}/"]
    assert s.plan.hubs == [HUB]
    assert s.plan.sitemaps == [SitemapSpec(url=f"{SITE}/sitemap.xml", shapes=["tuyendung.frt.vn"])]
    assert not s.plan.indirect
    opened = [u for kind, u, _ in s.fetcher.calls if kind == "fetch"]  # pyright: ignore[reportAttributeAccessIssue]
    assert f"{SITE}/ha-noi-thu-ngan-10021" not in opened  # lastmod quá cũ
    assert not any("/tin-tuc/" in u for u in opened)
    assert f"{SITE}/sitemap_index.xml" not in opened  # sitemap.xml đọc được
    assert s.plan.demand() == 3  # trang chủ + trang danh mục + sitemap


async def test_poll_opens_only_new_sitemap_jobs() -> None:
    s, jobs = await build(frt())
    fetcher = frt({**all_jobs(), "can-tho-duoc-si-ban-thuoc-15001": "Dược Sĩ Bán Thuốc"})
    polled = await poll(fetcher, s.plan, jobs)
    assert polled.broken is None
    assert not polled.truncated
    opened = [u for kind, u, _ in fetcher.calls if kind == "fetch"]
    # Sitemap, trang chủ, trang danh mục, rồi chỉ trang của tin mới (trang danh mục không bị mở
    # lại như trang chi tiết).
    assert sorted(opened) == sorted(
        [f"{SITE}/sitemap.xml", f"{SITE}/", HUB, f"{SITE}/can-tho-duoc-si-ban-thuoc-15001"]
    )
    new = [j for j in polled.polled if j.url.endswith("-15001")]  # pyright: ignore[reportAttributeAccessIssue]
    assert len(new) == 1
    assert len(polled.polled) == len(all_jobs()) + 1  # pyright: ignore[reportAttributeAccessIssue]


async def test_poll_marks_plan_broken_when_sitemap_is_gone() -> None:
    s, jobs = await build(frt())
    fetcher = frt()
    fetcher.add(f"{SITE}/sitemap.xml", b"", status=404)
    polled = await poll(fetcher, s.plan, jobs)
    assert polled.broken == f"sitemap_http_404: {SITE}/sitemap.xml"
    # Sitemap không còn URL cùng khuôn (site đổi đường dẫn tin): build lại.
    fetcher = frt()
    fetcher.add(f"{SITE}/sitemap.xml", urlset(("gioi-thieu", None)), content_type="text/xml")
    polled = await poll(fetcher, s.plan, jobs)
    assert polled.broken == f"sitemap_no_jobs: {SITE}/sitemap.xml"


async def test_no_sitemap_falls_back_to_index_then_gives_up() -> None:
    fetcher = frt()
    del fetcher.results[f"{SITE}/sitemap.xml"]
    s, jobs = await build(fetcher)
    assert sorted(j.url for j in jobs) == sorted(
        f"{SITE}/{slug}" for slug in {**HOME_JOBS, **HUB_JOBS}
    )
    assert s.plan.sitemaps == []
    urls = [u for _, u, _ in fetcher.calls]
    assert f"{SITE}/sitemap.xml" in urls
    assert f"{SITE}/sitemap_index.xml" in urls
