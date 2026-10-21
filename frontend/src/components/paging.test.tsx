import { QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/components/ui/table"
import { createQueryClient } from "@/lib/query-client"
import type { ListParams, Page, SortState } from "@/lib/types"

import { nextSort, PagedList, pageLimit, Pager, pageWindow, SortHead } from "./common"

type Col = "started_at" | "duration_ms"

// Kiểu cột khai tường minh (như các trang): suy từ giá trị thì chỉ được đúng một cột.
const SORT: SortState<Col> = { sortBy: "started_at", sortOrder: "DESC" }

function page<T>(data: T[], values: Partial<Page<T>> = {}): Page<T> {
  return { data, total: data.length, page: 1, limit: 20, pageCount: 1, hasNextPage: false, exact: true, ...values }
}

describe("phân trang", () => {
  it("số dòng mỗi trang từ URL: giá trị ngoài danh sách về 50", () => {
    expect(pageLimit("20")).toBe(20)
    expect(pageLimit("100")).toBe(100)
    expect(pageLimit("500")).toBe(50)
    expect(pageLimit("abc")).toBe(50)
    expect(pageLimit("")).toBe(50)
  })

  it("nút số trang: trang đầu, trang cuối, ±2 quanh trang hiện tại, khoảng bỏ qua thành gap", () => {
    expect(pageWindow(5, 20)).toEqual([1, "gap", 3, 4, 5, 6, 7, "gap", 20])
    expect(pageWindow(1, 3)).toEqual([1, 2, 3])
    expect(pageWindow(2, 4)).toEqual([1, 2, 3, 4])
    expect(pageWindow(1, 1)).toEqual([1])
  })

  it("bấm cột như data-table của open-asm: cột khác → tăng dần, cùng cột → đảo chiều", () => {
    const start = { sortBy: "started_at", sortOrder: "DESC" } as const
    expect(nextSort(start, "duration_ms")).toEqual({ sortBy: "duration_ms", sortOrder: "ASC" })
    expect(nextSort({ sortBy: "duration_ms", sortOrder: "ASC" }, "duration_ms")).toEqual({
      sortBy: "duration_ms",
      sortOrder: "DESC",
    })
    expect(nextSort(start, "started_at")).toEqual({ sortBy: "started_at", sortOrder: "ASC" })
  })

  it("Pager: khoảng dòng, ô số dòng, nút số trang", () => {
    const onPage = vi.fn()
    render(<Pager page={2} pageCount={3} total={120} limit={50} onPage={onPage} onLimit={() => undefined} />)
    expect(screen.getByText(/51–100/).parentElement?.textContent).toContain("51–100 trong 120")
    expect(screen.getByRole("combobox", { name: "Số dòng mỗi trang" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "Trang 2" }).getAttribute("aria-current")).toBe("page")
    fireEvent.click(screen.getByRole("button", { name: "Trang 3" }))
    fireEvent.click(screen.getByRole("button", { name: "Trang trước" }))
    expect(onPage.mock.calls).toEqual([[3], [1]])
  })
})

describe("PagedList", () => {
  it("gửi page / limit / sortBy / sortOrder / search; đổi sắp xếp hay tìm thì về trang 1", async () => {
    const calls: ListParams<Col>[] = []
    const rows = [{ id: "r1" }, { id: "r2" }]
    render(
      <QueryClientProvider client={createQueryClient()}>
        <PagedList
          options={(p: ListParams<Col>) => ({
            queryKey: ["runs", p],
            queryFn: async () => {
              calls.push(p)
              return page(rows, { total: 45, pageCount: 3, hasNextPage: true, page: p.page })
            },
          })}
          defaultSort={SORT}
          searchPlaceholder="Tìm lỗi…"
          empty="Không có."
        >
          {(items, { sort, onSort }) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortHead column="duration_ms" sort={sort} onSort={onSort}>
                    Thời gian
                  </SortHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell>{r.id}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </PagedList>
      </QueryClientProvider>,
    )
    expect(await screen.findByText("r1")).toBeTruthy()
    expect(calls.at(-1)).toEqual({ sortBy: "started_at", sortOrder: "DESC", page: 1, limit: 20, search: "" })

    fireEvent.click(screen.getByRole("button", { name: "Trang 2" }))
    await waitFor(() => expect(calls.at(-1)?.page).toBe(2))

    fireEvent.click(screen.getByRole("button", { name: /Thời gian/ }))
    await waitFor(() => expect(calls.at(-1)).toMatchObject({ sortBy: "duration_ms", sortOrder: "ASC", page: 1 }))
    expect(screen.getByRole("columnheader", { name: /Thời gian/ }).getAttribute("aria-sort")).toBe("ascending")

    fireEvent.change(screen.getByRole("textbox", { name: "Tìm lỗi…" }), { target: { value: " timeout " } })
    fireEvent.submit(screen.getByRole("search"))
    await waitFor(() => expect(calls.at(-1)).toMatchObject({ search: "timeout", page: 1 }))
  })
})
