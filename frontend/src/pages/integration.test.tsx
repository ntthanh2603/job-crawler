import { QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, within } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { createQueryClient } from "@/lib/query-client"

import { IntegrationPage } from "./integration"

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

const ID = "0192f3a1-7c2b-7d3e-9f00-123456789abc"
const WEBHOOK = {
  enabled: false,
  url: null,
  secret_set: false,
  lag: 0,
  last_ok_at: null,
  failing_since: null,
  last_error: null,
}
const CLIENT = {
  id: ID,
  name: "Backend đối tác",
  scopes: ["companies:write", "jobs:read"],
  all_companies: false,
  include_description: true,
  companies: 1200,
  active_keys: 1,
  last_used_at: null,
  created_by: "admin",
  created_at: "2026-10-22T08:00:00Z",
  webhook: WEBHOOK,
}
const KEY = {
  id: "0192f3a1-7c2b-7d3e-9f00-000000000001",
  prefix: "AbCdEf12",
  created_by: "admin",
  created_at: "2026-10-22T08:00:00Z",
  last_used_at: null,
  revoked_at: null,
}

function renderPage() {
  const calls: { method: string; url: string }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? "GET"
      calls.push({ method, url })
      if (method === "POST" && url === `/api/api-clients/${ID}/keys`)
        return json({ id: KEY.id, prefix: "Zz9", key: "jca_Zz9-mot-lan-thoi", created_at: KEY.created_at })
      if (url.startsWith(`/api/api-clients/${ID}`)) return json({ ...CLIENT, keys: [KEY] })
      if (url.startsWith("/api/api-clients"))
        return json({ data: [CLIENT], total: 1, page: 1, limit: 100, pageCount: 1, hasNextPage: false, exact: true })
      return json({}, 404)
    }),
  )
  const router = createMemoryRouter([{ path: "/integration", element: <IntegrationPage /> }], {
    initialEntries: ["/integration"],
  })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return calls
}

describe("trang Tích hợp", () => {
  it("liệt kê hệ thống, khoá; khoá mới chỉ hiện một lần; không có lệnh vận hành", async () => {
    const calls = renderPage()
    expect(await screen.findByText("Backend đối tác")).toBeTruthy()
    expect(await screen.findByText("jca_AbCdEf12…")).toBeTruthy()
    expect(screen.getByText(/1\.200 doanh nghiệp đã đẩy/)).toBeTruthy()
    expect(screen.getByText(/\/api\/integration\/v1$/)).toBeTruthy()
    // Chưa có URL / secret webhook thì không bật được.
    expect(screen.getByRole("switch", { name: "Bật" }).hasAttribute("disabled")).toBe(true)
    expect(document.body.textContent).not.toMatch(/curl|docker run/)

    fireEvent.click(screen.getByRole("button", { name: "Tạo khoá" }))
    const dialog = await screen.findByRole("dialog", { name: "Khoá API mới" })
    expect(within(dialog).getByLabelText("Khoá API mới").textContent).toBe("jca_Zz9-mot-lan-thoi")
    expect(calls.some((c) => c.method === "POST" && c.url === `/api/api-clients/${ID}/keys`)).toBe(true)
    fireEvent.click(within(dialog).getByRole("button", { name: "Đã lưu, đóng" }))
    expect(screen.queryByText("jca_Zz9-mot-lan-thoi")).toBeNull()
  })
})
