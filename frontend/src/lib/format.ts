// Định dạng hiển thị: giờ Việt Nam, số, thời lượng, lương...
import type { Job, Salary } from "./types"

const TZ = "Asia/Ho_Chi_Minh"
const dateOnly = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "numeric" })
const hourOnly = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, hour: "2-digit", minute: "2-digit" })
const numberFmt = new Intl.NumberFormat("vi-VN")
const decimalFmt = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1 })

/** Số thập phân kiểu Việt Nam: 2,9 · 1.234,5. */
export function dec(value: number, digits = 1): string {
  return new Intl.NumberFormat("vi-VN", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)
}

/** "16/10/2026 14:29" (giờ Việt Nam). */
export function dt(value: string | null | undefined): string {
  if (!value) return "–"
  const d = new Date(value)
  return `${dateOnly.format(d)} ${hourOnly.format(d)}`
}

export function date(value: string | null | undefined): string {
  return value ? dateOnly.format(new Date(value)) : "–"
}

export function hour(value: string | null | undefined): string {
  return value ? hourOnly.format(new Date(value)) : "–"
}

export function ago(value: string | null | undefined): string {
  if (!value) return "–"
  const seconds = (Date.now() - new Date(value).getTime()) / 1000
  if (seconds < 0) return dt(value)
  if (seconds < 60) return `${Math.floor(seconds)} giây trước`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} giờ trước`
  const days = Math.floor(seconds / 86400)
  return days < 30 ? `${days} ngày trước` : date(value)
}

export function num(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : numberFmt.format(Math.round(value))
}

export function pct(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "–" : `${dec(value * 100, digits)}%`
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–"
  const seconds = ms / 1000
  if (seconds < 1) return `${Math.round(ms)} ms`
  if (seconds < 120) return `${Math.round(seconds)} s`
  return `${dec(seconds / 60)} phút`
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–"
  let amount = value
  for (const unit of ["B", "KB", "MB"]) {
    if (amount < 1024) return unit === "B" ? `${amount} B` : `${dec(amount)} ${unit}`
    amount /= 1024
  }
  return `${dec(amount)} GB`
}

export function salary(s: Salary | null): string {
  if (!s) return "–"
  if (s.min === null && s.max === null) return s.negotiable ? "Thoả thuận" : s.raw || "–"
  const amount = (v: number | null) => {
    if (v === null) return "?"
    if (s.currency === "VND" && v >= 1_000_000) return `${decimalFmt.format(v / 1_000_000)} tr`
    return numberFmt.format(v)
  }
  let span: string
  if (s.min !== null && s.max !== null && s.min !== s.max) span = `${amount(s.min)} – ${amount(s.max)}`
  else if (s.min !== null && s.max === null) span = `Từ ${amount(s.min)}`
  else if (s.min === null) span = `Tới ${amount(s.max)}`
  else span = amount(s.min)
  const unit = s.currency && s.currency !== "VND" ? ` ${s.currency}` : ""
  const period = { month: "/tháng", year: "/năm", hour: "/giờ" }[s.period ?? "month"] ?? ""
  return `${span}${unit}${period}`
}

export function provinces(job: Pick<Job, "locations">): string {
  const names = [...new Set(job.locations.map((l) => l.province).filter(Boolean))]
  return names.join(", ") || "–"
}

/** Viết hoa chữ cái đầu (giữ nguyên phần còn lại). */
export function cap(text: string | null | undefined): string {
  if (!text) return ""
  return text.charAt(0).toLocaleUpperCase("vi") + text.slice(1)
}

/** Loại nguồn: "careers:abc.vn" → "careers", "board:topcv:123" → "topcv". */
export function sourceKind(value: string | null | undefined): string {
  const parts = (value ?? "").split(":")
  return parts[0] === "board" && parts[1] ? parts[1] : parts[0]
}

const SOURCE_NAMES: Record<string, string> = {
  careers: "Careers",
  discovery: "Tìm nguồn",
  kyc: "KYC",
  robots: "robots.txt",
  topcv: "TopCV",
  careerlink: "CareerLink",
  jobsgo: "JobsGO",
  careerviet: "CareerViet",
  vietnamworks: "VietnamWorks",
  glints: "Glints",
  topdev: "TopDev",
  vieclam24h: "Vieclam24h",
  itviec: "ITviec",
  "123job": "123job",
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  workday: "Workday",
  teamtailor: "Teamtailor",
  workable: "Workable",
  recruitee: "Recruitee",
  bamboohr: "BambooHR",
}

/** Tên hiển thị của nguồn (theo thương hiệu); nhận cả "careers:abc.vn", "board:topcv:x". */
export function sourceLabel(value: string | null | undefined): string {
  const kind = sourceKind(value)
  return SOURCE_NAMES[kind] ?? cap(kind)
}

// Nguyên âm tiếng Việt (kể cả có dấu): từ không có nguyên âm là viết tắt ("TNHH", "FPT", "MTV").
const VOWEL = /[aeiouyàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹ]/i

/**
 * Tên dài viết HOA toàn bộ ("CÔNG TY CỔ PHẦN ĐẦU TƯ ĐIỆN MÁY XANH") → viết hoa đầu mỗi từ để dễ
 * đọc; giữ nguyên tên ngắn (thương hiệu viết tắt), từ không có nguyên âm ("TNHH") và tên đã viết
 * thường/hoa lẫn.
 */
export function displayName(text: string | null | undefined): string {
  if (!text) return ""
  const letters = text.replace(/[^\p{L}]/gu, "")
  const words = text.trim().split(/\s+/).length
  // Thương hiệu viết tắt ("MISA", "VNPAY", "FPT Software") giữ nguyên; chỉ đổi tên dài viết hoa.
  if (words < 3 || letters !== letters.toLocaleUpperCase("vi")) return cap(text)
  return text.replace(/\p{L}[\p{L}\p{M}'’]*/gu, (word) =>
    VOWEL.test(word) ? word.charAt(0) + word.slice(1).toLocaleLowerCase("vi") : word,
  )
}

export function companyName(c: { brand_name: string | null; legal_name: string | null; input_name: string }): string {
  return displayName(c.brand_name || c.legal_name || c.input_name)
}

export const LABELS: Record<string, string> = {
  // lượt chạy
  ok: "Thành công",
  partial: "Một phần",
  failed: "Thất bại",
  ambiguous: "Chưa rõ",
  running: "Đang chạy",
  // KYC
  verified: "Đã xác minh",
  not_found: "Không tìm thấy",
  none: "Chưa KYC",
  // job
  active: "Đang mở",
  expired: "Hết hạn",
  removed: "Đã gỡ",
  // kết quả request
  blocked: "Bị chặn",
  timeout: "Hết giờ",
  error: "Lỗi",
  robots_disallowed: "Robots chặn",
  // job trong queue
  queued: "Đang chờ",
  complete: "Xong",
  aborted: "Đã huỷ",
  new: "Mới",
  // bộ lọc
  never: "Chưa chạy",
  with: "Có job đang mở",
  without: "Không có job",
  no: "Chưa kiểm",
  yes: "Đã kiểm",
  correct: "Đúng",
  wrong: "Sai",
  // cấp bậc
  intern: "Thực tập sinh",
  fresher: "Fresher",
  junior: "Junior",
  middle: "Middle",
  senior: "Senior",
  lead: "Trưởng nhóm",
  manager: "Quản lý",
  director: "Giám đốc",
  // loại hình
  full_time: "Toàn thời gian",
  part_time: "Bán thời gian",
  contract: "Hợp đồng",
  internship: "Thực tập",
  freelance: "Tự do",
  temporary: "Thời vụ",
  // hình thức
  onsite: "Tại văn phòng",
  hybrid: "Hybrid",
  remote: "Từ xa",
}

/** Nhãn tiếng Việt (viết hoa đầu) cho giá trị mã hoá; giá trị lạ thì chỉ viết hoa chữ đầu. */
export function label(value: string | null | undefined): string {
  if (!value) return "–"
  return LABELS[value] ?? cap(value)
}

/** "no_job: không bóc được tin" → "Không bóc được tin". */
export function failReason(reason: string): string {
  const i = reason.indexOf(":")
  return cap(i >= 0 ? reason.slice(i + 1).trim() : reason.replaceAll("_", " "))
}

/** Các cặp (khoá, số lượng) xếp theo số lượng giảm dần. */
export function byCount(counts: Record<string, number>): [string, number][] {
  return Object.entries(counts).sort((a, b) => b[1] - a[1])
}
