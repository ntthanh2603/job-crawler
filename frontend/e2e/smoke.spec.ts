// Smoke test trên stack đang chạy: khung ứng dụng, các trang chính, Cài đặt, trang không tồn tại,
// màn hình điện thoại. Chỉ đọc: không bấm nút ghi (lưu, chạy, xoá...), không gửi form.
import { expectLoaded, pageHeading, expect, test } from "./fixtures"

/** Mục trên thanh bên → đường dẫn và tiêu đề trang. */
const PAGES = [
  { link: "Tổng quan", path: "/", heading: "Tổng quan" },
  { link: "Doanh nghiệp", path: "/companies", heading: "Doanh nghiệp" },
  { link: "Việc làm", path: "/jobs", heading: "Việc làm" },
  { link: "Hàng đợi", path: "/queue", heading: "Hàng đợi" },
  { link: "Workers", path: "/workers", heading: "Workers lấy trang" },
  { link: "Nhật ký thao tác", path: "/audit", heading: "Nhật ký thao tác" },
]

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")

test("khung ứng dụng tải được: thanh bên, người dùng, tiêu đề", async ({ page }) => {
  await page.goto("/")
  const nav = page.getByRole("navigation", { name: "Điều hướng chính" })
  await expect(nav).toBeVisible()
  for (const p of PAGES) await expect(nav.getByRole("link", { name: new RegExp(`^${escape(p.link)}`) })).toBeVisible()
  await expect(page.getByRole("radiogroup", { name: "Giao diện" })).toBeVisible()

  // Người dùng ở góc thanh bên theo GET /api/me; "Đăng xuất" chỉ khi API trả logout_url (SSO).
  const me = (await (await page.request.get("/api/me")).json()) as {
    user: string | null
    auth: string
    logout_url?: string | null
  }
  const sidebar = page.getByRole("complementary")
  await expect(sidebar.getByText(me.user || "Không đăng nhập", { exact: true })).toBeVisible()
  // Cài đặt nằm trong menu tài khoản (bấm avatar), không trong danh sách trang.
  await expect(sidebar.getByRole("button", { name: "Tài khoản" })).toBeVisible()
  await expect(nav.getByRole("link", { name: "Cài đặt" })).toHaveCount(0)
  const logout = sidebar.getByRole("link", { name: "Đăng xuất" })
  if (me.logout_url) await expect(logout).toHaveAttribute("href", me.logout_url)
  else await expect(logout).toHaveCount(0)
  // Đăng nhập bằng mật khẩu: nút Đăng xuất to dưới avatar.
  await expect(sidebar.getByRole("button", { name: "Đăng xuất" })).toHaveCount(me.auth === "jwt" ? 1 : 0)

  await expect(pageHeading(page, "Tổng quan")).toBeVisible()
  // Tiêu đề tab theo trang (PageHeader).
  await expect(page).toHaveTitle("Tổng quan · jobcrawler")
  await expectLoaded(page)
})

test.describe("trang chính", () => {
  for (const p of PAGES) {
    test(`${p.link}: mở từ thanh bên, có tiêu đề, không lỗi`, async ({ page }) => {
      // Bắt đầu ở trang khác để lần bấm là điều hướng phía trình duyệt thật.
      await page.goto(p.path === "/" ? "/audit" : "/")
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible()

      const link = page
        .getByRole("navigation", { name: "Điều hướng chính" })
        .getByRole("link", { name: new RegExp(`^${escape(p.link)}`) })
      await link.click()
      await expect(page).toHaveURL((url) => url.pathname === p.path)
      await expect(link).toHaveAttribute("aria-current", "page")
      await expect(pageHeading(page, p.heading)).toBeVisible()
      await expect(page).toHaveTitle(`${p.heading} · jobcrawler`)
      await expectLoaded(page)
    })

    test(`${p.link}: mở thẳng bằng đường dẫn ${p.path}`, async ({ page }) => {
      await page.goto(p.path)
      await expect(pageHeading(page, p.heading)).toBeVisible()
      await expectLoaded(page)
    })
  }
})

test("Cài đặt: popup trên trang đang xem, mở nhóm Proxy, tìm, đóng", async ({ page }) => {
  await page.goto("/jobs")
  await expect(pageHeading(page, "Việc làm")).toBeVisible()
  await page.getByRole("complementary").getByRole("button", { name: "Tài khoản" }).click()
  await page.getByRole("menuitem", { name: "Cài đặt" }).click()
  const dialog = page.getByRole("dialog", { name: "Cài đặt" })
  await expect(dialog).toBeVisible()
  // Trang bên dưới giữ nguyên, URL thêm ?settings.
  await expect(page).toHaveURL((url) => url.pathname === "/jobs" && url.searchParams.has("settings"))
  await expectLoaded(page)

  const groups = dialog.getByRole("navigation", { name: "Nhóm cài đặt" })
  const proxy = groups.getByRole("button", { name: /^Proxy/ })
  await proxy.click()
  await expect(proxy).toHaveAttribute("aria-current", "true")
  await expect(dialog.getByRole("heading", { level: 2, name: "Proxy", exact: true })).toBeVisible()
  await expect(dialog.getByText("Chế độ proxy", { exact: true })).toBeVisible()

  // Tìm: chỉ lọc trên giao diện, không lưu gì.
  const search = dialog.getByRole("search").getByLabel("Tìm cài đặt")
  await search.fill("timeout")
  await expect(dialog.getByText("Timeout mỗi request", { exact: true })).toBeVisible()
  await expect(dialog.getByText("Chế độ proxy", { exact: true })).toHaveCount(0)

  await search.fill("khong co cai dat nao nhu the nay")
  await expect(dialog.getByText("Không có cài đặt nào khớp")).toBeVisible()
  await dialog.getByRole("button", { name: "Xem tất cả" }).click()
  await expect(search).toHaveValue("")
  await expect(dialog.getByText("Chế độ proxy", { exact: true })).toBeVisible()

  // Esc đóng popup, về đúng trang cũ.
  await page.keyboard.press("Escape")
  await expect(dialog).toHaveCount(0)
  await expect(page).toHaveURL((url) => url.pathname === "/jobs" && !url.searchParams.has("settings"))
})

test("Cài đặt: đường dẫn cũ /settings mở popup", async ({ page }) => {
  await page.goto("/settings")
  await expect(page.getByRole("dialog", { name: "Cài đặt" })).toBeVisible()
  await expect(page).toHaveURL((url) => url.pathname === "/" && url.searchParams.has("settings"))
})

test("Doanh nghiệp: hộp thoại thêm tay, tải CSV mẫu", async ({ page }) => {
  await page.goto("/companies")
  await expect(pageHeading(page, "Doanh nghiệp")).toBeVisible()

  // Thêm tay: chỉ mở rồi đóng, không gửi.
  await page.getByRole("button", { name: "Thêm doanh nghiệp" }).first().click()
  const create = page.getByRole("dialog", { name: "Thêm doanh nghiệp" })
  await expect(create.getByLabel("Tên doanh nghiệp")).toBeFocused()
  for (const label of ["Mã số thuế", "Website", "Ghi chú"]) await expect(create.getByLabel(label)).toBeVisible()
  await expect(create.getByRole("button", { name: "Thêm doanh nghiệp" })).toBeDisabled()
  await page.keyboard.press("Escape")
  await expect(create).toHaveCount(0)

  await page.getByRole("button", { name: "Nạp danh sách" }).first().click()
  const importer = page.getByRole("dialog", { name: "Nạp danh sách doanh nghiệp" })
  const sample = importer.getByRole("link", { name: "Tải CSV mẫu" })
  await expect(sample).toHaveAttribute("href", "/api/companies/import/sample.csv")
  const csv = await page.request.get("/api/companies/import/sample.csv")
  expect(csv.headers()["content-type"]).toContain("text/csv")
  const body = await csv.body()
  expect([...body.subarray(0, 3)], "BOM cho Excel").toEqual([0xef, 0xbb, 0xbf])
  const lines = body.subarray(3).toString("utf8").trim().split("\n")
  expect(lines[0]).toBe("name,website,note")
  expect(lines).toHaveLength(51)
})

test("đường dẫn không tồn tại hiện trang không tìm thấy", async ({ page }) => {
  await page.goto("/trang-cu-da-bi-xoa/123")
  await expect(pageHeading(page, "Không tìm thấy trang")).toBeVisible()
  // Khung ứng dụng vẫn còn: quay về được.
  await expect(page.getByRole("navigation", { name: "Điều hướng chính" })).toBeVisible()
  await page.getByRole("link", { name: "Về trang Tổng quan" }).click()
  await expect(page).toHaveURL((url) => url.pathname === "/")
  await expect(pageHeading(page, "Tổng quan")).toBeVisible()
})

test.describe("điện thoại (390px)", () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test("Tổng quan không cuộn ngang", async ({ page }) => {
    await page.goto("/")
    await expect(pageHeading(page, "Tổng quan")).toBeVisible()
    await expectLoaded(page)
    // Biểu đồ (recharts) đo kích thước sau khi gắn vào trang: chờ khung hình kế tiếp.
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    const { scrollWidth, clientWidth } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(scrollWidth, "chiều rộng nội dung so với màn hình").toBeLessThanOrEqual(clientWidth)
  })

  test("menu mở được và điều hướng", async ({ page }) => {
    await page.goto("/")
    await expect(pageHeading(page, "Tổng quan")).toBeVisible()
    await page.getByRole("button", { name: "Mở menu" }).click()
    const menu = page.getByRole("dialog", { name: "Điều hướng" })
    await expect(menu).toBeVisible()
    await menu.getByRole("link", { name: /^Việc làm/ }).click()
    await expect(menu).toBeHidden()
    await expect(pageHeading(page, "Việc làm")).toBeVisible()
    await expectLoaded(page)

    // Cài đặt trong menu tài khoản ở cuối menu: mở popup, menu trượt đóng lại.
    await page.getByRole("button", { name: "Mở menu" }).click()
    await expect(menu).toBeVisible()
    await menu.getByRole("button", { name: "Tài khoản" }).click()
    await page.getByRole("menuitem", { name: "Cài đặt" }).click()
    await expect(menu).toBeHidden()
    await expect(page.getByRole("dialog", { name: "Cài đặt" })).toBeVisible()
  })
})
