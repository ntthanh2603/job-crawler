import { describe, expect, it } from "vitest"

import {
  actionLabel,
  actorLabel,
  auditHref,
  browserLabel,
  fieldLabel,
  formatAuditValue,
  hasDetails,
  isMasked,
  resourceHref,
  resourceName,
  resourceTypeLabel,
  summary,
} from "./audit"

describe("nhãn", () => {
  it("tên thao tác đã biết → tiếng Việt, tên lạ giữ nguyên", () => {
    expect(actionLabel("settings.update")).toBe("Lưu cài đặt")
    expect(actionLabel("company.poll")).toBe("Poll ngay")
    expect(actionLabel("worker.drain")).toBe("Cho worker dừng nhận việc")
    expect(actionLabel("worker.joined")).toBe("Worker tham gia bằng khoá")
    expect(actionLabel("api_key.create")).toBe("Tạo khoá API")
    expect(actionLabel("integration.companies")).toBe("Hệ thống ngoài đẩy doanh nghiệp")
    expect(actionLabel("something.new")).toBe("something.new")
  })

  it("loại đối tượng, người thực hiện, tên trường", () => {
    expect(resourceTypeLabel("company")).toBe("Doanh nghiệp")
    expect(resourceTypeLabel("widget")).toBe("Widget")
    expect(resourceTypeLabel(null)).toBe("–")
    expect(actorLabel("system")).toBe("Hệ thống")
    expect(actorLabel("anonymous")).toBe("Không đăng nhập")
    expect(actorLabel("worker")).toBe("Worker")
    expect(actorLabel("api:Backend đối tác")).toBe("Hệ thống ngoài Backend đối tác")
    expect(resourceTypeLabel("api_client")).toBe("Hệ thống ngoài")
    expect(actorLabel("an")).toBe("an")
    expect(fieldLabel("already_queued")).toBe("Đã có trong hàng đợi")
    expect(fieldLabel("join_id")).toBe("Khoá tham gia")
    expect(fieldLabel("crawl_timezone")).toBe("crawl_timezone")
  })
})

describe("đối tượng", () => {
  const uuid = "0192f3a1-7c2b-7d3e-9f00-123456789abc"

  it("link tới doanh nghiệp / việc làm, đối tượng khác không có link", () => {
    expect(resourceHref({ resource_type: "company", resource_id: uuid })).toBe(`/companies/${uuid}`)
    expect(resourceHref({ resource_type: "job", resource_id: uuid })).toBe(`/jobs/${uuid}`)
    expect(resourceHref({ resource_type: "board", resource_id: "topcv" })).toBeNull()
    expect(resourceHref({ resource_type: "company", resource_id: null })).toBeNull()
  })

  it("worker và khoá tham gia link tới nhật ký của chính nó", () => {
    expect(resourceHref({ resource_type: "worker", resource_id: "host-1:4242" })).toBe(
      "/audit?resource_type=worker&resource_id=host-1%3A4242",
    )
    expect(resourceHref({ resource_type: "join_key", resource_id: "a1b2c3d4e5f6" })).toBe(
      "/audit?resource_type=join_key&resource_id=a1b2c3d4e5f6",
    )
    expect(auditHref("join_key", "a b&c")).toBe("/audit?resource_type=join_key&resource_id=a+b%26c")
  })

  it("tên ngắn: UUID rút gọn, trang tuyển dụng theo thương hiệu", () => {
    expect(resourceName({ resource_type: "company", resource_id: uuid })).toBe("0192f3a1…")
    expect(resourceName({ resource_type: "board", resource_id: "topcv" })).toBe("TopCV")
    expect(resourceName({ resource_type: "worker", resource_id: "host-1:4242" })).toBe("host-1:4242")
  })
})

describe("giá trị", () => {
  it("bộ lọc xuất việc làm: nhãn và giá trị của ngành nghề, học vấn, nguồn", () => {
    expect(fieldLabel("category")).toBe("Ngành nghề")
    expect(fieldLabel("experience")).toBe("Kinh nghiệm")
    expect(formatAuditValue({ education: "bachelor" })).toBe("Học vấn: Đại học")
    expect(formatAuditValue("topcv", "source")).toBe("TopCV")
    expect(formatAuditValue("bachelor")).toBe("bachelor") // khoá khác: giữ nguyên
  })

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
    expect(hasDetails({ changes: null, metadata: null, request_id: null, ip: "10.0.0.1" })).toBe(true)
  })

  it("thời hạn, thời điểm, bộ lọc lồng trong object", () => {
    expect(formatAuditValue(900, "ttl_seconds")).toBe("15 phút")
    expect(formatAuditValue(7200, "ttl_seconds")).toBe("2 giờ")
    expect(formatAuditValue({ company_id: "0192f3a1-7c2b-7d3e-9f00-123456789abc", status: "all" })).toBe(
      "Doanh nghiệp: 0192f3a1…, Trạng thái: Mọi trạng thái",
    )
    expect(formatAuditValue("jsonl")).toBe("JSONL")
  })
})

describe("nguồn request", () => {
  it("User-Agent → trình duyệt + hệ điều hành; client khác theo tên", () => {
    const chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    expect(browserLabel(chrome)).toBe("Chrome 141 · Windows")
    expect(browserLabel(`${chrome} Edg/141.0.3537.71`)).toBe("Edge 141 · Windows")
    expect(browserLabel("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) Gecko/20100101 Firefox/133.0")).toBe("Firefox 133 · macOS")
    expect(
      browserLabel("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"),
    ).toBe("Safari 18 · iOS")
    expect(browserLabel("curl/8.9.1")).toBe("curl 8")
    expect(browserLabel("python-httpx/0.28.1")).toBe("python-httpx 0")
    expect(browserLabel(null)).toBe("")
  })
})

describe("tóm tắt", () => {
  it("con số chính của từng thao tác", () => {
    expect(summary({ action: "company.poll", metadata: { queued: true, trigger: "manual" }, changes: null })).toBe(
      "Đã vào hàng đợi",
    )
    expect(
      summary({ action: "companies.import", metadata: { filename: "a.csv", rows: 57, created: 3, updated: 0, errors: 2 }, changes: null }),
    ).toBe("a.csv · 57 dòng · 3 mới · 2 bị bỏ")
    expect(summary({ action: "auth.login_failed", metadata: { failures: 20, locked: true }, changes: null })).toBe(
      "Lần sai thứ 20 · Bị khoá đăng nhập 5 phút",
    )
    expect(
      summary({ action: "jobs.export", metadata: { fmt: "csv", filters: { company_id: "x", status: "all" } }, changes: null }),
    ).toBe("CSV · lọc theo doanh nghiệp, trạng thái")
    expect(summary({ action: "jobs.export", metadata: { fmt: "jsonl" }, changes: null })).toBe("JSONL · không lọc")
    expect(summary({ action: "proxies.check", metadata: { total: 5, ok: 4, failed: 1 }, changes: null })).toBe(
      "5 proxy · 4 sống · 1 lỗi",
    )
    expect(
      summary({ action: "settings.update", metadata: { reset: ["a"] }, changes: { a: { before: 1, after: 2 }, b: { before: 1, after: 3 } } }),
    ).toBe("2 cài đặt · 1 về mặc định")
    expect(summary({ action: "job.review", metadata: null, changes: { correct: { before: null, after: false }, note: { before: null, after: "sai tên" } } })).toBe(
      "Sai · “sai tên”",
    )
    expect(summary({ action: "worker.join_key", metadata: { ttl_seconds: 900 }, changes: null })).toBe("Hạn 15 phút")
    expect(
      summary({ action: "worker.joined", metadata: { join_id: "a1b2", hostname: "may-1", pool: "vn", version: "1.2" }, changes: null }),
    ).toBe("Máy may-1 · nhóm vn")
    expect(summary({ action: "worker.joined", metadata: { join_id: "a1b2", hostname: "may-1", pool: "default" }, changes: null })).toBe(
      "Máy may-1",
    )
    expect(summary({ action: "something.new", metadata: { rows: 2 }, changes: null })).toBe("Dòng hợp lệ: 2")
  })

  it("đối tượng hiện theo tên máy chủ tra sẵn, phiên đăng nhập không hiện id", () => {
    expect(resourceName({ resource_type: "company", resource_id: "0192f3a1-7c2b-7d3e-9f00-123456789abc", resource_label: "FPT" })).toBe("FPT")
    expect(resourceName({ resource_type: "session", resource_id: "jti-1", resource_label: null })).toBe("")
    expect(actionLabel("auth.login_failed")).toBe("Đăng nhập sai")
    expect(resourceTypeLabel("join_key")).toBe("Khoá tham gia")
  })
})
