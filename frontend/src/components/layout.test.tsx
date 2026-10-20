import { QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { createQueryClient } from "@/lib/query-client"
import type { MeOut } from "@/lib/gen/queries"

import { Layout } from "./layout"

function json(body: unknown) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } })
}

let router: ReturnType<typeof createMemoryRouter>

/** Khung ứng dụng với `GET /api/me` trả `me` (hàng đợi rỗng). */
function renderLayout(me: MeOut) {
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>(async (input) => json(String(input).startsWith("/api/me") ? me : { queues: [] })),
  )
  router = createMemoryRouter([{ element: <Layout />, children: [{ index: true, element: <p>trang</p> }] }])
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe("người dùng ở thanh bên", () => {
  it("SSO có logout_url: tên người dùng và liên kết Đăng xuất tới logout_url", async () => {
    renderLayout({ user: "an.nguyen@example.com", auth: "proxy", logout_url: "/oauth2/sign_out?rd=%2F" })
    expect(await screen.findByText("an.nguyen@example.com")).toBeTruthy()
    const link = screen.getByRole("link", { name: "Đăng xuất" })
    expect(link.getAttribute("href")).toBe("/oauth2/sign_out?rd=%2F")
  })

  it("đăng nhập bằng mật khẩu (JWT): nút Đăng xuất, không có liên kết SSO", async () => {
    renderLayout({ user: "admin", auth: "jwt", logout_url: null })
    expect(await screen.findByText("admin")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Đăng xuất" })).toBeTruthy()
    // Cài đặt nằm trong menu tài khoản (bấm avatar), không trong danh sách trang.
    expect(screen.getByRole("navigation", { name: "Điều hướng chính" }).textContent).not.toContain("Cài đặt")
    // Menu của Radix mở bằng bàn phím (Enter) như bằng chuột.
    fireEvent.keyDown(screen.getByRole("button", { name: "Tài khoản" }), { key: "Enter" })
    expect(await screen.findByText("Đăng nhập bằng mật khẩu")).toBeTruthy()
    fireEvent.click(screen.getByRole("menuitem", { name: "Cài đặt" }))
    expect(router.state.location.search).toBe("?settings=")
    expect(screen.queryByRole("link", { name: "Đăng xuất" })).toBeNull()
  })

  it("không có người dùng", async () => {
    renderLayout({ user: null, auth: "none", logout_url: null })
    expect(await screen.findByText("Không đăng nhập")).toBeTruthy()
    expect(screen.queryByRole("link", { name: "Đăng xuất" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Đăng xuất" })).toBeNull()
  })
})
