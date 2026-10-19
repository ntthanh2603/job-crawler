import { describe, expect, it } from "vitest"

import {
  actionLabel,
  actorLabel,
  fieldLabel,
  formatAuditValue,
  hasDetails,
  isMasked,
  resourceHref,
  resourceName,
  resourceTypeLabel,
} from "./audit"

describe("nhãn", () => {
  it("tên thao tác đã biết → tiếng Việt, tên lạ giữ nguyên", () => {
    expect(actionLabel("settings.update")).toBe("Lưu cài đặt")
    expect(actionLabel("company.poll")).toBe("Poll ngay")
    expect(actionLabel("worker.drain")).toBe("Cho worker dừng nhận việc")
    expect(actionLabel("something.new")).toBe("something.new")
  })

  it("loại đối tượng, người thực hiện, tên trường", () => {
    expect(resourceTypeLabel("company")).toBe("Doanh nghiệp")
    expect(resourceTypeLabel("widget")).toBe("Widget")
    expect(resourceTypeLabel(null)).toBe("–")
    expect(actorLabel("system")).toBe("Hệ thống")
    expect(actorLabel("anonymous")).toBe("Không đăng nhập")
    expect(actorLabel("an")).toBe("an")
    expect(fieldLabel("already_queued")).toBe("Đã có trong hàng đợi")
    expect(fieldLabel("crawl_timezone")).toBe("crawl_timezone")
  })
})

describe("đối tượng", () => {
  const uuid = "0192f3a1-7c2b-7d3e-9f00-123456789abc"

  it("link tới doanh nghiệp / việc làm, đối tượng khác không có link", () => {
    expect(resourceHref({ resource_type: "company", resource_id: uuid })).toBe(`/companies/${uuid}`)
    expect(resourceHref({ resource_type: "job", resource_id: uuid })).toBe(`/jobs/${uuid}`)
    expect(resourceHref({ resource_type: "worker", resource_id: "w-1" })).toBeNull()
    expect(resourceHref({ resource_type: "company", resource_id: null })).toBeNull()
  })

  it("tên ngắn: UUID rút gọn, trang tuyển dụng theo thương hiệu", () => {
    expect(resourceName({ resource_type: "company", resource_id: uuid })).toBe("0192f3a1…")
    expect(resourceName({ resource_type: "board", resource_id: "topcv" })).toBe("TopCV")
    expect(resourceName({ resource_type: "worker", resource_id: "host-1:4242" })).toBe("host-1:4242")
  })
})

describe("giá trị", () => {
  it("giá trị đã che hiện là Ẩn", () => {
    expect(isMasked("***")).toBe(true)
    expect(isMasked("••••••••")).toBe(true)
    expect(isMasked("a***")).toBe(false)
    expect(formatAuditValue("***")).toBe("Ẩn")
    expect(formatAuditValue("••••••••")).toBe("Ẩn")
  })

  it("rỗng, có / không, đúng / sai khi kiểm tay", () => {
    expect(formatAuditValue(null)).toBe("Trống")
    expect(formatAuditValue("")).toBe("Trống")
    expect(formatAuditValue(true)).toBe("Có")
    expect(formatAuditValue(false, "correct")).toBe("Sai")
  })

  it("số, tỉ lệ, danh sách, object, từ đã biết", () => {
    expect(formatAuditValue(12345)).toBe("12.345")
    expect(formatAuditValue(2.25)).toBe("2,25")
    expect(formatAuditValue(0.875, "precision")).toBe("87,5%")
    expect(formatAuditValue(["a", 2])).toBe("a, 2")
    expect(formatAuditValue([])).toBe("Trống")
    expect(formatAuditValue({ topcv: 1.5, glints: null })).toBe("topcv: 1,5, glints: Trống")
    expect(formatAuditValue("manual")).toBe("Thủ công")
  })

  it("hasDetails", () => {
    expect(hasDetails({ changes: null, metadata: null, request_id: null })).toBe(false)
    expect(hasDetails({ changes: {}, metadata: {}, request_id: null })).toBe(false)
    expect(hasDetails({ changes: { a: { before: 1, after: 2 } }, metadata: null, request_id: null })).toBe(true)
    expect(hasDetails({ changes: null, metadata: null, request_id: "req-1" })).toBe(true)
  })
})
