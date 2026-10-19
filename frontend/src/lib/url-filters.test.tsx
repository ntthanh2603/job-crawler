import { act, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { MemoryRouter, useLocation } from "react-router"
import { describe, expect, it } from "vitest"

import { filtersToParams, parseFilters, parsePage, useUrlFilters } from "./url-filters"

const DEFAULTS = { q: "", status: "active", sort: "new" }

describe("parsePage", () => {
  it("số nguyên ≥ 1, giá trị lạ về 1", () => {
    expect(parsePage("3")).toBe(3)
    expect(parsePage("2.7")).toBe(2)
    for (const raw of ["x", "", "0", "-2", "NaN", "Infinity", null, undefined]) expect(parsePage(raw)).toBe(1)
  })
})

describe("parseFilters / filtersToParams", () => {
  it("thiếu hoặc rỗng thì lấy mặc định; khoá lạ bỏ qua", () => {
    const params = new URLSearchParams("q=dev&status=&foo=bar")
    expect(parseFilters(params, DEFAULTS)).toEqual({ q: "dev", status: "active", sort: "new" })
  })

  it("không ghi giá trị mặc định, rỗng, trang 1", () => {
    expect(filtersToParams({ q: "", status: "active", sort: "salary" }, DEFAULTS, 1)).toEqual({ sort: "salary" })
    expect(filtersToParams({ q: "dev", status: "expired", sort: "new" }, DEFAULTS, 3)).toEqual({
      q: "dev",
      status: "expired",
      page: "3",
    })
    expect(filtersToParams({}, DEFAULTS, Number.NaN)).toEqual({})
  })
})

function setup(url: string) {
  const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
  return renderHook(() => ({ f: useUrlFilters(DEFAULTS), location: useLocation() }), { wrapper })
}

describe("useUrlFilters", () => {
  it("đọc bộ lọc và trang từ URL; trang không hợp lệ thành 1", () => {
    const { result } = setup("/jobs?q=dev&page=x")
    expect(result.current.f.filters).toEqual({ q: "dev", status: "active", sort: "new" })
    expect(result.current.f.page).toBe(1)
  })

  it("đổi bộ lọc thì về trang 1 và không ghi mặc định lên URL", () => {
    const { result } = setup("/jobs?status=expired&page=4")
    expect(result.current.f.page).toBe(4)
    act(() => result.current.f.update({ q: "tester" }))
    expect(result.current.location.search).toBe("?q=tester&status=expired")
    expect(result.current.f.page).toBe(1)
    act(() => result.current.f.update({ status: "active" }))
    expect(result.current.location.search).toBe("?q=tester")
  })

  it("chuyển trang giữ bộ lọc", () => {
    const { result } = setup("/jobs?q=dev")
    act(() => result.current.f.update({ page: 2 }))
    expect(result.current.location.search).toBe("?q=dev&page=2")
    expect(result.current.f.page).toBe(2)
    act(() => result.current.f.update({ page: 1 }))
    expect(result.current.location.search).toBe("?q=dev")
  })

  it("isFiltered bỏ qua khoá được chỉ định; reset xoá hết", () => {
    const { result } = setup("/jobs?sort=salary")
    expect(result.current.f.isFiltered()).toBe(true)
    expect(result.current.f.isFiltered("sort")).toBe(false)
    act(() => result.current.f.update({ q: "x" }))
    expect(result.current.f.isFiltered("sort")).toBe(true)
    act(() => result.current.f.reset())
    expect(result.current.location.search).toBe("")
    expect(result.current.f.filters).toEqual(DEFAULTS)
  })

  it("giữ tham chiếu bộ lọc khi URL không đổi", () => {
    const { result, rerender } = setup("/jobs?q=dev")
    const first = result.current.f.filters
    rerender()
    expect(result.current.f.filters).toBe(first)
  })
})
