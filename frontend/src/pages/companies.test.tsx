import { QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { createQueryClient } from "@/lib/query-client"

import { CompaniesPage } from "./companies"

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

const EMPTY_PAGE = { data: [], total: 0, page: 1, limit: 50, pageCount: 1, hasNextPage: false, exact: true }

/** Trang Doanh nghiệp (danh sách rỗng); `POST /api/companies` trả lần lượt từng phần tử của `created`. */
function renderPage(created: Response[]) {
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined)
  const posts: unknown[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>(async (input, init) => {
      const url = String(input)
      if (init?.method === "POST" && url === "/api/companies") {
        posts.push(JSON.parse(String(init.body)))
        return created.shift() ?? json({}, 500)
      }
      if (url.startsWith("/api/meta")) return json({ kyc_statuses: [], run_statuses: [] })
      return json(EMPTY_PAGE)
    }),
  )
  const router = createMemoryRouter([{ path: "/companies", element: <CompaniesPage /> }], {
    initialEntries: ["/companies"],
  })
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return posts
}

describe("thêm doanh nghiệp bằng tay", () => {
  it("lỗi của API hiện trong hộp thoại; sửa lại thì thêm được và đóng hộp thoại", async () => {
    const posts = renderPage([
      json({ detail: "Mã số thuế '12' không hợp lệ (10 chữ số; chi nhánh thêm -3 chữ số)" }, 400),
      json({ id: "7b0c6f0e-0000-4000-8000-000000000001", created: true, warnings: [], queued: true }),
    ])
    fireEvent.click((await screen.findAllByRole("button", { name: "Thêm doanh nghiệp" }))[0])
    const dialog = await screen.findByRole("dialog", { name: "Thêm doanh nghiệp" })
    const submit = within(dialog).getByRole("button", { name: "Thêm doanh nghiệp" })
    // Chưa có tên thì không gửi được.
    expect(submit.hasAttribute("disabled")).toBe(true)

    fireEvent.change(within(dialog).getByLabelText("Tên doanh nghiệp"), { target: { value: "  Công ty ABC " } })
    fireEvent.change(within(dialog).getByLabelText("Mã số thuế"), { target: { value: "12" } })
    fireEvent.click(submit)
    expect((await within(dialog).findByRole("alert")).textContent).toContain("Mã số thuế '12' không hợp lệ")

    fireEvent.change(within(dialog).getByLabelText("Mã số thuế"), { target: { value: "" } })
    fireEvent.change(within(dialog).getByLabelText("Website"), { target: { value: "abc.vn" } })
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Đưa vào hàng đợi ngay sau khi thêm" }))
    fireEvent.click(submit)
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull())
    expect(posts).toEqual([
      { name: "Công ty ABC", tax_code: "12", website: "", note: "", enqueue: false },
      { name: "Công ty ABC", tax_code: "", website: "abc.vn", note: "", enqueue: true },
    ])
  })
})

describe("nạp danh sách", () => {
  it("có liên kết tải CSV mẫu, không còn nút nạp doanh nghiệp mẫu", async () => {
    renderPage([])
    fireEvent.click((await screen.findAllByRole("button", { name: "Nạp danh sách" }))[0])
    const dialog = await screen.findByRole("dialog", { name: "Nạp danh sách doanh nghiệp" })
    const sample = within(dialog).getByRole("link", { name: "Tải CSV mẫu" })
    expect(sample.getAttribute("href")).toBe("/api/companies/import/sample.csv")
    expect(sample.hasAttribute("download")).toBe(true)
    expect(within(dialog).queryByText(/doanh nghiệp mẫu/)).toBeNull()
  })
})
