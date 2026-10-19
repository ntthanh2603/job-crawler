import { render, screen } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { NotFound, PageError } from "./route-error"

function renderRoute(path: string, Page: () => never) {
  const router = createMemoryRouter(
    [
      {
        errorElement: <PageError />,
        children: [
          { path: "/broken", element: <Page /> },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
    { initialEntries: [path] },
  )
  return render(<RouterProvider router={router} />)
}

describe("trang lỗi của router", () => {
  it("chunk cũ sau khi triển khai: báo có phiên bản mới, nút tải lại", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined)
    renderRoute("/broken", () => {
      throw new TypeError("Failed to fetch dynamically imported module: http://localhost/assets/jobs-abc.js")
    })
    expect(screen.getByText("Phiên bản mới đã được triển khai — tải lại trang để tiếp tục")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Tải lại trang" })).toBeTruthy()
  })

  it("lỗi khác khi hiển thị: thông báo lỗi và nút tải lại", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined)
    renderRoute("/broken", () => {
      throw new Error("cannot read properties of undefined")
    })
    expect(screen.getByText("Không hiển thị được trang này")).toBeTruthy()
    expect(screen.getByText("Cannot read properties of undefined")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Tải lại trang" })).toBeTruthy()
  })

  it("đường dẫn không có: trang không tìm thấy", () => {
    renderRoute("/khong-co", () => {
      throw new Error("không tới đây")
    })
    expect(screen.getByRole("heading", { name: "Không tìm thấy trang" })).toBeTruthy()
    expect(screen.getByRole("link", { name: /Về trang Tổng quan/ }).getAttribute("href")).toBe("/")
  })
})
