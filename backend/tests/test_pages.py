from __future__ import annotations

from pathlib import Path

from jobcrawler.common.pages import (
    extract_links,
    html_to_markdown,
    looks_like_spa_shell,
    main_markdown,
    page_title,
    parse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "careers"


def test_extract_links_resolves_and_skips_non_http() -> None:
    html = """
    <html><head><base href="https://a.vn/vi/"></head><body>
      <a href="tuyen-dung">Tuyển dụng</a>
      <a href="/job/1#top"> Kỹ sư   phần mềm </a>
      <a href="mailto:hr@a.vn">Email</a><a href="#x">Lên đầu</a>
      <a href="javascript:void(0)">JS</a>
      <a href="https://b.vn/" title="Đối tác"></a>
    </body></html>"""
    links = extract_links(parse(html), "https://a.vn/")
    assert [(link.url, link.text) for link in links] == [
        ("https://a.vn/vi/tuyen-dung", "Tuyển dụng"),
        ("https://a.vn/job/1", "Kỹ sư phần mềm"),
        ("https://b.vn/", "Đối tác"),
    ]


def test_main_markdown_drops_navigation_and_scripts() -> None:
    html = """<html><body>
      <header><a href="/">Logo</a></header><nav>Trang chủ | Tuyển dụng</nav>
      <main><h1>Nhân viên kinh doanh</h1>
        <h2>Mô tả công việc</h2><ul><li>Tìm khách hàng</li><li>Chăm sóc&nbsp;khách</li></ul>
        <script>var x = 1;</script>
        <p>Liên hệ <a href="https://a.vn/apply">ứng tuyển</a></p>
      </main>
      <footer>© 2026</footer></body></html>"""
    md = main_markdown(html)
    assert md.startswith("# Nhân viên kinh doanh")
    assert "## Mô tả công việc" in md
    assert "- Tìm khách hàng" in md
    assert "Chăm sóc khách" in md
    assert "[ứng tuyển](https://a.vn/apply)" in md
    for noise in ("Logo", "Trang chủ", "var x", "© 2026"):
        assert noise not in md


def test_main_markdown_keeps_forms_that_wrap_the_content() -> None:
    """tuyendung.frt.vn bọc cả mô tả công việc trong form "Ứng tuyển"; ASP.NET WebForms bọc cả
    trang trong `<form runat=server>`. Form tìm kiếm / đăng nhập (ít chữ) vẫn là nhiễu."""
    duties = "".join(
        f"<p>• Nhiệm vụ số {i}: tư vấn, bán hàng, chăm sóc khách hàng.</p>" for i in range(12)
    )
    html = f"""<html><body>
      <form action="/Search"><input name="q"><select><option>Hà Nội</option></select>
        <button>Tìm việc</button></form>
      <form action="/Applicant/Apply" method="post"><input type="hidden" name="token" value="x">
        <h3>Tư vấn bán hàng</h3><h4>CHI TIẾT CÔNG VIỆC</h4>{duties}
        <button type="submit">Ứng tuyển</button></form>
      <form action="/login"><label>Email</label><input name="email"></form>
    </body></html>"""
    md = main_markdown(html)
    assert "### Tư vấn bán hàng" in md
    assert "Nhiệm vụ số 11" in md
    for noise in ("Tìm việc", "Hà Nội", "Ứng tuyển", "Email"):
        assert noise not in md


def test_html_to_markdown_collapses_blank_lines() -> None:
    assert html_to_markdown("<p>a</p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_page_title_prefers_h1() -> None:
    tree = parse('<title>Site | X</title><meta property="og:title" content="OG"><h1>Tin</h1>')
    assert page_title(tree) == "Tin"
    assert page_title(parse('<title>Site</title><meta property="og:title" content="OG">')) == "OG"


def test_spa_shell_detection() -> None:
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert looks_like_spa_shell(shell)
    rendered = (FIXTURES / "momo" / "careers_jobs_rendered.html").read_text(encoding="utf-8")
    assert not looks_like_spa_shell(rendered)
    base = (FIXTURES / "base_vn" / "alljobs.html").read_text(encoding="utf-8")
    assert not looks_like_spa_shell(base)
