// Đăng nhập một lần trước mọi test khi stack bật đăng nhập (UI_PASSWORD): gọi POST /api/auth/login
// bằng E2E_USER / E2E_PASSWORD, lưu cookie phiên vào AUTH_FILE để mọi test dùng chung.
import { request, type FullConfig } from "@playwright/test"

export const AUTH_FILE = "e2e/.auth/state.json"

export default async function globalSetup(config: FullConfig) {
  const username = process.env.E2E_USER
  const password = process.env.E2E_PASSWORD
  if (!username || !password) return
  const context = await request.newContext({ baseURL: config.projects[0]?.use.baseURL })
  const response = await context.post("/api/auth/login", { data: { username, password } })
  if (!response.ok()) throw new Error(`Đăng nhập E2E thất bại: HTTP ${response.status()}`)
  await context.storageState({ path: AUTH_FILE })
  await context.dispose()
}
