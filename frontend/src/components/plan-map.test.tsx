import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { CrawlPlan } from "@/lib/types"

import { pageGroupKey, pageLabel, PlanMap, planTree } from "./plan-map"

const SITE = "https://about.acme.vn"

function page(url: string, values: Partial<CrawlPlan["listings"][number]> = {}) {
  return { url, hub: false, rendered: false, api_only: false, ...values }
}

function api(url: string, listing: string | null, values: Partial<CrawlPlan["apis"][number]> = {}) {
  return {
    url,
    method: "GET",
    embedded: false,
    paged: false,
    page_param: "",
    by_offset: false,
    size: 0,
    total: null,
    pages: 1,
    listing,
    ...values,
  }
}

function plan(values: Partial<CrawlPlan> = {}): CrawlPlan {
  return {
    id: "01a152c1-0000-7000-8000-000000000002",
    source: "careers:about.acme.vn",
    kind: "careers",
    url: `${SITE}/tuyen-dung`,
    status: "active",
    jobs: 60,
    built_at: "2026-10-19T18:00:00Z",
    polled_at: "2026-10-20T03:00:00Z",
    changed_at: null,
    failures: 0,
    last_error: null,
    last_poll: "unchanged",
    readable: true,
    ats: null,
    listings: [],
    listings_total: 0,
    apis: [],
    apis_total: 0,
    llm_listing: false,
    ignored: 0,
    indirect: 0,
    pages: 0,
    pages_needed: 0,
    ...values,
  }
}

describe("gom trang", () => {
  it("bỏ số trang ở tham số và đường dẫn, giữ tham số khác", () => {
    expect(pageGroupKey(`${SITE}/jobs?page=2`)).toBe(`${SITE}/jobs`)
    expect(pageGroupKey(`${SITE}/cong-viec/page/3/`)).toBe(`${SITE}/cong-viec`)
    expect(pageGroupKey(`${SITE}/jobs?dept=it&p=4`)).toBe(`${SITE}/jobs?dept=it`)
    expect(pageGroupKey(`${SITE}/`)).toBe(`${SITE}/`)
    expect(pageLabel(`${SITE}/jobs?page=2`)).toBe("Trang 2")
    expect(pageLabel(`${SITE}/jobs/page/6/`)).toBe("Trang 6")
    expect(pageLabel(`${SITE}/en/jobs`)).toBe("about.acme.vn/en/jobs")
  })

  it("trang sau vào nhóm trang đầu, API vào trang đã gọi nó, API lạc đứng riêng", () => {
    const tree = planTree(
      plan({
        listings: [
          page(`${SITE}/cong-viec`),
          page(`${SITE}/cong-viec/page/2/`),
          page(`${SITE}/ha-noi`, { hub: true }),
          page(`${SITE}/cong-viec?page=3`),
        ],
        apis: [api(`${SITE}/api/jobs`, `${SITE}/cong-viec/page/2/`), api(`${SITE}/api/meta`, null)],
      }),
    )
    expect(tree.groups.map((g) => [g.page.url, g.more.map((p) => p.url), g.apis.map((a) => a.url)])).toEqual([
      [`${SITE}/cong-viec`, [`${SITE}/cong-viec/page/2/`, `${SITE}/cong-viec?page=3`], [`${SITE}/api/jobs`]],
      [`${SITE}/ha-noi`, [], []],
    ])
    expect(tree.loose.map((a) => a.url)).toEqual([`${SITE}/api/meta`])
  })
})

describe("bản đồ", () => {
  it("vẽ trang, trang sau, API con và mở tab khi bấm ô kết quả", () => {
    const onShow = vi.fn()
    const list = `${SITE}/viec-lam`
    render(
      <PlanMap
        plan={plan({
          listings: [page(list, { rendered: true, api_only: true }), page(`${list}?page=2`)],
          listings_total: 2,
          apis: [api(`${SITE}/api/search?page=1`, list, { paged: true, page_param: "page", size: 6, total: 22, pages: 4 })],
          apis_total: 1,
          ignored: 8,
          indirect: 3,
        })}
        names={79}
        onShow={onShow}
      />,
    )
    const map = screen.getByLabelText("Bản đồ bộ khung")
    expect(within(map).getByText("Trang careers · about.acme.vn · 2 trang · 1 API")).toBeTruthy()
    expect(within(map).getByText("Trang 2")).toBeTruthy()
    expect(within(map).getByText(/4 trang · 6 tin\/trang · 22 tin lúc build/)).toBeTruthy()
    expect(within(map).getByText("Thay cho trang")).toBeTruthy()

    fireEvent.click(within(map).getByRole("button", { name: /60 mã tin\s*· 3 gián tiếp/ }))
    fireEvent.click(within(map).getByRole("button", { name: /8 link bỏ qua/ }))
    fireEvent.click(within(map).getByRole("button", { name: /79 mã → tên/ }))
    expect(onShow.mock.calls).toEqual([["jobs"], ["ignored"], ["names"]])
  })

  it("bảng tin ATS: một nhánh gọi API của ATS", () => {
    render(<PlanMap plan={plan({ source: "workday:acme/External", kind: "workday", ats: "workday", pages_needed: 3 })} names={0} />)
    expect(screen.getByText(/Gọi API bảng tin của Workday · cần 3 trang/)).toBeTruthy()
  })
})
