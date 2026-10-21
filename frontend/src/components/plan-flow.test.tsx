import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { CrawlPlan } from "@/lib/types"

import { minuteOfDay, PlanFlow, PlanReads, windowSegments } from "./plan-flow"

const LIMITS = { max_failures: 3, max_detail_pages: 10, max_pages: 10 }
const LIST = "https://tuyendung.acme.vn/viec-lam"

function plan(values: Partial<CrawlPlan> = {}): CrawlPlan {
  return {
    id: "01a152c1-0000-7000-8000-000000000001",
    source: "careers:tuyendung.acme.vn",
    kind: "careers",
    url: LIST,
    status: "active",
    jobs: 12,
    built_at: "2026-10-19T18:00:00Z",
    polled_at: "2026-10-20T03:00:00Z",
    changed_at: null,
    failures: 0,
    last_error: null,
    last_poll: "unchanged",
    readable: true,
    ats: null,
    listings: [{ url: LIST, hub: false, rendered: false, api_only: false }],
    listings_total: 1,
    apis: [],
    apis_total: 0,
    llm_listing: false,
    ignored: 0,
    indirect: 0,
    pages: 3,
    pages_needed: 1,
    ...values,
  }
}

describe("khung giờ", () => {
  it("tách khung qua nửa đêm, 24:00 và cả ngày như backend", () => {
    expect(windowSegments("00:00-06:00")).toEqual([[0, 360]])
    expect(windowSegments("06:00-24:00")).toEqual([[360, 1440]])
    expect(windowSegments("22:30 - 06:00")).toEqual([
      [1350, 1440],
      [0, 360],
    ])
    expect(windowSegments("08:00-08:00")).toEqual([
      [480, 1440],
      [0, 480],
    ])
    expect(windowSegments("cả ngày")).toEqual([])
  })

  it("đổi mốc giờ sang phút trong ngày theo múi giờ thu thập", () => {
    expect(minuteOfDay("2026-10-20T10:15:00Z", "Asia/Ho_Chi_Minh")).toBe(17 * 60 + 15)
    expect(minuteOfDay("2026-10-20T17:30:00Z", "Asia/Ho_Chi_Minh")).toBe(30)
    expect(minuteOfDay("2026-10-20T10:15:00Z", "Không/Có")).toBeNull()
  })
})

describe("đường lượt poll", () => {
  function current() {
    return within(screen.getByRole("list", { name: "Kết quả lượt poll" }))
      .getAllByRole("listitem")
      .filter((li) => li.getAttribute("aria-current") === "step")
      .map((li) => li.textContent)
  }

  it("tô sáng đúng một kết quả: lỗi tạm kèm số lượt còn lại trước khi build lại", () => {
    render(<PlanFlow plan={plan({ last_poll: "retrying", failures: 2 })} limits={LIMITS} />)
    expect(current()).toEqual(["Lỗi tạm 2/3"])
    expect(screen.getByText(/Lỗi 3 lượt liên tiếp thì bộ khung hỏng/)).toBeTruthy()
  })

  it("bộ khung hỏng ngay ở lượt build: không có thời điểm poll, nói rõ chờ lượt build sau", () => {
    render(<PlanFlow plan={plan({ status: "broken", last_poll: "broken", polled_at: null })} limits={LIMITS} />)
    expect(current()).toEqual(["Hỏng → build lại"])
    const help = screen.getByText(/Lượt build không đọc được nguồn này/)
    expect(help.textContent).not.toContain("–")
  })

  it("chưa poll: không kết quả nào được tô", () => {
    render(<PlanFlow plan={plan({ last_poll: "never", polled_at: null })} limits={LIMITS} />)
    expect(current()).toEqual([])
    expect(screen.getByText(/Chưa poll kể từ lần build/)).toBeTruthy()
  })

  it("tóm tắt phần đọc: số API gọi riêng (bỏ JSON nhúng), số trang và phần trang của nguồn", () => {
    const api = {
      url: "https://api.acme.vn/jobs?page=1",
      method: "GET",
      embedded: false,
      paged: true,
      page_param: "page",
      by_offset: false,
      size: 20,
      total: 90,
      pages: 5,
      listing: LIST,
    }
    render(
      <PlanFlow
        plan={plan({ apis: [api, { ...api, embedded: true }], apis_total: 2, pages: 4, pages_needed: 6 })}
        limits={LIMITS}
      />,
    )
    expect(screen.getByText("1 API · 1 trang")).toBeTruthy()
    expect(screen.getByText(/Đọc 4\/6 trang\/lượt/)).toBeTruthy()
    expect(screen.getByRole("img", { name: "Được đọc 4 trang, cần 6 trang để đọc trọn" })).toBeTruthy()
  })
})

describe("thứ tự đọc", () => {
  it("API trước rồi tới trang, đánh số; dài thì thu gọn và mở được", () => {
    const pages = Array.from({ length: 7 }, (_, i) => ({
      url: `${LIST}?page=${i + 1}`,
      hub: i === 6,
      rendered: i === 0,
      api_only: false,
    }))
    const api = {
      url: "https://api.acme.vn/meta",
      method: "POST",
      embedded: false,
      paged: false,
      page_param: "",
      by_offset: false,
      size: 0,
      total: null,
      pages: 1,
      listing: null,
    }
    render(<PlanReads plan={plan({ apis: [api], apis_total: 1, listings: pages, listings_total: 9 })} />)
    const rows = () => within(screen.getByRole("list")).getAllByRole("listitem")
    expect(rows()).toHaveLength(6)
    expect(rows()[0].textContent).toContain("POST")
    expect(rows()[0].textContent).toContain("Một request, không phân trang")
    expect(rows()[1].textContent).toContain("HTML trống mới mở trình duyệt")
    expect(screen.getByText("+2 trang / API nữa trong bộ khung")).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "Xem cả 8 mục" }))
    expect(rows()).toHaveLength(8)
    expect(rows()[7].textContent).toContain("Danh mục")
  })
})
