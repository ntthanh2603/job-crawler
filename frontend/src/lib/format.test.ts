import { describe, expect, it, vi } from "vitest"

import {
  ago,
  byCount,
  bytes,
  cap,
  companyName,
  date,
  dec,
  displayName,
  dt,
  education,
  duration,
  failReason,
  hour,
  label,
  num,
  pct,
  provinces,
  reason,
  salary,
  shortUrl,
  sourceKind,
  sourceLabel,
  sourceProblem,
} from "./format"
import type { Salary } from "./types"

const vnd = (over: Partial<Salary>): Salary => ({
  min: null,
  max: null,
  currency: "VND",
  period: "month",
  negotiable: false,
  raw: null,
  ...over,
})

describe("số", () => {
  it("định dạng kiểu Việt Nam", () => {
    expect(num(1234567)).toBe("1.234.567")
    expect(num(2.6)).toBe("3")
    expect(num(null)).toBe("–")
    expect(dec(2.94)).toBe("2,9")
    expect(dec(1234.5, 2)).toBe("1.234,50")
    expect(pct(0.123)).toBe("12,3%")
    expect(pct(1, 0)).toBe("100%")
    expect(pct(undefined)).toBe("–")
  })

  it("thời lượng", () => {
    expect(duration(450)).toBe("450 ms")
    expect(duration(1500)).toBe("2 s")
    expect(duration(90_000)).toBe("90 s")
    expect(duration(150_000)).toBe("2,5 phút")
    expect(duration(null)).toBe("–")
  })

  it("dung lượng", () => {
    expect(bytes(512)).toBe("512 B")
    expect(bytes(2048)).toBe("2,0 KB")
    expect(bytes(5 * 1024 * 1024)).toBe("5,0 MB")
    expect(bytes(3 * 1024 ** 3)).toBe("3,0 GB")
    expect(bytes(undefined)).toBe("–")
  })
})

describe("ngày giờ (giờ Việt Nam)", () => {
  it("dt / date / hour đổi sang UTC+7", () => {
    expect(dt("2026-10-16T07:29:00Z")).toBe("16/10/2026 14:29")
    // 20:00 UTC là 03:00 sáng hôm sau ở Việt Nam.
    expect(date("2026-10-16T20:00:00Z")).toBe("17/10/2026")
    expect(hour("2026-10-16T20:05:00Z")).toBe("03:05")
    expect(dt(null)).toBe("–")
    expect(date(undefined)).toBe("–")
  })

  it("ago: giây, phút, giờ, ngày, quá 30 ngày thì ghi ngày", () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-10-19T12:00:00Z"))
    expect(ago("2026-10-19T11:59:30Z")).toBe("30 giây trước")
    expect(ago("2026-10-19T11:55:00Z")).toBe("5 phút trước")
    expect(ago("2026-10-19T09:00:00Z")).toBe("3 giờ trước")
    expect(ago("2026-10-17T12:00:00Z")).toBe("2 ngày trước")
    expect(ago("2026-09-01T12:00:00Z")).toBe("01/09/2026")
    // Thời điểm trong tương lai (lệch đồng hồ): ghi ngày giờ.
    expect(ago("2026-10-19T13:00:00Z")).toBe("19/10/2026 20:00")
    expect(ago(null)).toBe("–")
  })
})

describe("lương", () => {
  it("khoảng lương VND theo triệu", () => {
    expect(salary(vnd({ min: 15_000_000, max: 25_000_000 }))).toBe("15 tr – 25 tr/tháng")
    expect(salary(vnd({ min: 12_500_000 }))).toBe("Từ 12,5 tr/tháng")
    expect(salary(vnd({ max: 30_000_000, period: "year" }))).toBe("Tới 30 tr/năm")
    expect(salary(vnd({ min: 20_000_000, max: 20_000_000 }))).toBe("20 tr/tháng")
  })

  it("ngoại tệ ghi đơn vị", () => {
    expect(salary(vnd({ min: 1000, max: 2000, currency: "USD" }))).toBe("1.000 – 2.000 USD/tháng")
  })

  it("không có số: thoả thuận, chuỗi gốc, hoặc trống", () => {
    expect(salary(vnd({ negotiable: true }))).toBe("Thoả thuận")
    expect(salary(vnd({ raw: "Cạnh tranh" }))).toBe("Cạnh tranh")
    expect(salary(vnd({ raw: "0 VND/MONTH" }))).toBe("–")
    expect(salary(null)).toBe("–")
  })
})

describe("chữ", () => {
  it("cap viết hoa chữ đầu, giữ phần còn lại", () => {
    expect(cap("đã lưu")).toBe("Đã lưu")
    expect(cap("iPhone")).toBe("IPhone")
    expect(cap(null)).toBe("")
  })

  it("displayName: tên dài viết hoa → hoa đầu mỗi từ, giữ viết tắt", () => {
    expect(displayName("CÔNG TY CỔ PHẦN ĐẦU TƯ ĐIỆN MÁY XANH")).toBe("Công Ty Cổ Phần Đầu Tư Điện Máy Xanh")
    expect(displayName("CÔNG TY TNHH FPT")).toBe("Công Ty TNHH FPT")
    expect(displayName("MISA")).toBe("MISA")
    expect(displayName("FPT Software")).toBe("FPT Software")
    expect(displayName("công ty abc")).toBe("Công ty abc")
    expect(displayName(null)).toBe("")
  })

  it("companyName ưu tiên tên thương hiệu, rồi tên pháp lý, rồi tên nhập", () => {
    expect(companyName({ brand_name: "VNG", legal_name: "CÔNG TY CỔ PHẦN VNG", input_name: "vng" })).toBe("VNG")
    expect(companyName({ brand_name: null, legal_name: "CÔNG TY CỔ PHẦN VNG", input_name: "vng" })).toBe("Công Ty Cổ Phần VNG")
    expect(companyName({ brand_name: null, legal_name: null, input_name: "vng" })).toBe("Vng")
  })

  it("label: nhãn đã biết, mã lạ thì viết hoa đầu", () => {
    expect(label("partial")).toBe("Một phần")
    expect(label("full_time")).toBe("Toàn thời gian")
    expect(label("something")).toBe("Something")
    expect(label(null)).toBe("–")
  })

  it("education: học vấn riêng, không lẫn nhãn KYC của none", () => {
    expect(education("bachelor")).toBe("Đại học")
    expect(education("none")).toBe("Không yêu cầu")
    expect(education("unknown")).toBe("–")
    expect(education(null)).toBe("–")
  })

  it("failReason bỏ mã trước dấu hai chấm", () => {
    expect(failReason("no_job: không bóc được tin")).toBe("Không bóc được tin")
    expect(failReason("no_job")).toBe("No job")
  })

  it("shortUrl bỏ giao thức, www và / cuối", () => {
    expect(shortUrl("https://www.example.vn/careers/")).toBe("example.vn/careers")
    expect(shortUrl(null)).toBe("")
  })

  it("provinces bỏ trùng và rỗng", () => {
    const locations = [
      { province: "Hà Nội", address_raw: null },
      { province: null, address_raw: "x" },
      { province: "Hà Nội", address_raw: null },
      { province: "Đà Nẵng", address_raw: null },
    ]
    expect(provinces({ locations })).toBe("Hà Nội, Đà Nẵng")
    expect(provinces({ locations: [] })).toBe("–")
  })

  it("byCount xếp giảm dần", () => {
    expect(byCount({ a: 1, b: 3, c: 2 })).toEqual([
      ["b", 3],
      ["c", 2],
      ["a", 1],
    ])
  })
})

describe("nguồn", () => {
  it("sourceKind / sourceLabel", () => {
    expect(sourceKind("careers:abc.vn")).toBe("careers")
    expect(sourceKind("board:topcv:123")).toBe("topcv")
    expect(sourceLabel("board:topcv:123")).toBe("TopCV")
    expect(sourceLabel("vietnamworks")).toBe("VietnamWorks")
    expect(sourceLabel("lạ")).toBe("Lạ")
  })

  it("sourceProblem mô tả nguồn chưa xong", () => {
    expect(sourceProblem("careers:abc.vn").label).toBe("Careers abc.vn")
    expect(sourceProblem("discovery:abc.vn").label).toBe("Tìm nguồn trên abc.vn")
    expect(sourceProblem("board:topcv:123").label).toBe("Profile TopCV")
    expect(sourceProblem("verify").label).toBe("Tin chờ LLM chấm")
    expect(sourceProblem("greenhouse:acme").label).toBe("Greenhouse acme")
  })

  it("reason: mã đã biết, nguồn, mã lạ", () => {
    expect(reason("SPAM_REPOST: đăng lại của https://x.vn/1")).toEqual({
      code: "SPAM_REPOST",
      label: "Đăng lại",
      detail: "Đăng lại của https://x.vn/1",
      tone: "bad",
    })
    expect(reason("SOURCE_TOPCV")).toMatchObject({ label: "Nguồn TopCV", tone: "info" })
    expect(reason("WEIRD_CODE")).toMatchObject({ label: "Weird code", detail: "", tone: "info" })
  })
})
