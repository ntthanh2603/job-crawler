// Kiểm thử E2E (Playwright, Chromium) trên một stack đang chạy: `npm run e2e` (`task e2e`).
// Chỉ đọc, không đổi dữ liệu. Không tự khởi động stack: E2E_BASE_URL trỏ tới giao diện đang chạy
// (mặc định http://127.0.0.1:8000, cổng của `frontend` trong compose; Vite dev server: :5173).
// Stack bật đăng nhập (UI_PASSWORD) thì truyền E2E_USER / E2E_PASSWORD: e2e/global-setup.ts đăng nhập một
// lần, các test dùng chung cookie phiên.
import { defineConfig, devices } from "@playwright/test"

import { AUTH_FILE } from "./e2e/global-setup"

const CI = Boolean(process.env.CI)
const loggedIn = Boolean(process.env.E2E_USER && process.env.E2E_PASSWORD)

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  reporter: CI ? [["list"], ["html", { open: "never" }]] : "list",
  // Rộng hơn mặc định (5 s): Vite dev server dịch chunk của trang lúc mở lần đầu.
  expect: { timeout: 10_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8000",
    storageState: loggedIn ? AUTH_FILE : undefined,
    locale: "vi-VN",
    timezoneId: "Asia/Ho_Chi_Minh",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
})
