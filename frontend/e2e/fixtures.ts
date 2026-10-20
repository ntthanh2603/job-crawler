// `test` dùng chung cho E2E: mỗi test tự kiểm tra trang không có lỗi console / lỗi JS chưa bắt
// (kể cả request API hỏng: trình duyệt ghi "Failed to load resource" vào console).
import { expect, test as base, type Page } from "@playwright/test"

export const test = base.extend<{ consoleErrors: string[] }>({
  consoleErrors: [
    async ({ page }, use) => {
      const errors: string[] = []
      page.on("console", (msg) => {
        if (msg.type() === "error") errors.push(`${msg.text()} (${msg.location().url})`)
      })
      page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`))
      await use(errors)
      expect(errors, "lỗi console trong lúc chạy test").toEqual([])
    },
    { auto: true },
  ],
})

export { expect }

/** Trang đã tải xong dữ liệu: hết khung "Đang tải" và không có khối lỗi (ErrorState, role=alert). */
export async function expectLoaded(page: Page) {
  await expect(page.getByLabel("Đang tải", { exact: true })).toHaveCount(0, { timeout: 15_000 })
  await expect(page.getByRole("alert")).toHaveCount(0)
}

/** Tiêu đề trang (h1 của PageHeader). */
export function pageHeading(page: Page, name: string) {
  return page.getByRole("heading", { level: 1, name, exact: true })
}
