// Nhật ký thao tác: nhãn tiếng Việt cho tên thao tác / loại đối tượng / trường, và cách hiện giá trị.
import { cap, dec, dt, num, sourceLabel } from "@/lib/format"
import type { AuditEvent } from "@/lib/types"

const ACTIONS: Record<string, string> = {
  "auth.login": "Đăng nhập",
  "auth.login_failed": "Đăng nhập sai",
  "auth.logout": "Đăng xuất",
  "settings.update": "Lưu cài đặt",
  "settings.reset": "Đặt lại cài đặt về mặc định",
  "settings.import_env": "Nạp cài đặt từ biến môi trường (.env)",
  "proxies.check": "Kiểm tra proxy",
  "jobs.export": "Xuất dữ liệu việc làm",
  "company.create": "Thêm doanh nghiệp",
  "company.build": "Build lại bộ khung",
  "company.poll": "Poll ngay",
  "companies.enqueue": "Đưa doanh nghiệp vào hàng đợi",
  "companies.import": "Nạp danh sách doanh nghiệp",
  "job.review": "Kiểm tay việc làm",
  "job.unreview": "Xoá đánh giá kiểm tay",
  "board.index": "Index trang tuyển dụng",
  "maintenance.run": "Chạy bảo trì",
  "eval.run": "Chạy golden set",
  "worker.drain": "Cho worker dừng nhận việc",
  "worker.undrain": "Cho worker nhận việc lại",
  "worker.join_key": "Tạo khoá tham gia cho worker",
  "worker.join_key_revoke": "Huỷ khoá tham gia của worker",
  "worker.joined": "Worker tham gia bằng khoá",
}

/** Tên thao tác → nhãn tiếng Việt; tên lạ giữ nguyên ("foo.bar"). */
export function actionLabel(action: string): string {
  return ACTIONS[action] ?? action
}

/** Các tên thao tác đã biết (danh sách lọc khi chưa tải được danh sách từ máy chủ). */
export const KNOWN_ACTIONS = Object.keys(ACTIONS)

export const RESOURCE_TYPES: Record<string, string> = {
  company: "Doanh nghiệp",
  job: "Việc làm",
  board: "Trang tuyển dụng",
  worker: "Worker",
  join_key: "Khoá tham gia",
  settings: "Cài đặt",
  proxies: "Proxy",
  session: "Phiên đăng nhập",
}

export function resourceTypeLabel(type: string | null | undefined): string {
  if (!type) return "–"
  return RESOURCE_TYPES[type] ?? cap(type)
}

/** Trang Nhật ký lọc theo một đối tượng. Lọc theo khoá tham gia thì kèm dòng worker đã dùng khoá. */
export function auditHref(resourceType: string, resourceId: string): string {
  return `/audit?${new URLSearchParams({ resource_type: resourceType, resource_id: resourceId })}`
}

/** Đường dẫn trong ứng dụng tới đối tượng của dòng nhật ký (doanh nghiệp, việc làm), nếu có. Worker
 * và khoá tham gia không có trang riêng: link tới nhật ký của chính nó (khoá → worker đã dùng khoá). */
export function resourceHref(event: Pick<AuditEvent, "resource_type" | "resource_id">): string | null {
  if (!event.resource_id) return null
  if (event.resource_type === "company") return `/companies/${event.resource_id}`
  if (event.resource_type === "job") return `/jobs/${event.resource_id}`
  if (event.resource_type === "worker" || event.resource_type === "join_key") return auditHref(event.resource_type, event.resource_id)
  return null
}

/** Tên hiện của đối tượng: tên máy chủ tra sẵn (`resource_label`: tên doanh nghiệp, tiêu đề việc
 * làm), trang tuyển dụng theo thương hiệu, còn lại id (UUID rút gọn 8 ký tự). Phiên đăng nhập
 * không hiện id (mã JWT không có nghĩa với người đọc). */
export function resourceName(
  event: Pick<AuditEvent, "resource_type" | "resource_id"> & Partial<Pick<AuditEvent, "resource_label">>,
): string {
  const id = event.resource_id
  if (event.resource_label) return event.resource_label
  if (!id || event.resource_type === "session") return ""
  if (event.resource_type === "board") return sourceLabel(`board:${id}`)
  return shortId(id)
}

export function shortId(id: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) ? `${id.slice(0, 8)}…` : id
}

export function actorLabel(actor: string): string {
  if (actor === "system") return "Hệ thống"
  if (actor === "anonymous") return "Không đăng nhập"
  if (actor === "worker") return "Worker"
  return actor
}

const FIELDS: Record<string, string> = {
  // kiểm tay
  correct: "Kết quả kiểm",
  note: "Ghi chú",
  // thêm doanh nghiệp, đưa vào hàng đợi, import
  name: "Tên doanh nghiệp",
  queued: "Vào hàng đợi",
  trigger: "Cách kích hoạt",
  selected: "Được chọn",
  enqueued: "Đã đưa vào hàng đợi",
  already_queued: "Đã có trong hàng đợi",
  all: "Mọi doanh nghiệp",
  stale_hours: "Lâu chưa chạy (giờ)",
  company_ids: "Doanh nghiệp",
  filename: "File",
  rows: "Dòng hợp lệ",
  created: "Tạo mới",
  updated: "Cập nhật",
  unchanged: "Giữ nguyên",
  duplicates: "Trùng",
  errors: "Bị bỏ",
  enqueue: "Đưa vào hàng đợi sau khi nạp",
  // golden set
  golden: "Golden set",
  include_inactive: "Gồm việc đã đóng",
  companies: "Số doanh nghiệp",
  precision: "Precision",
  recall: "Recall",
  // worker, khoá tham gia
  hostname: "Máy",
  pool: "Nhóm",
  version: "Phiên bản",
  join_id: "Khoá tham gia",
  gone: "Worker đã rời",
  ttl_seconds: "Thời hạn",
  // cài đặt
  reset: "Về giá trị mặc định",
  // đăng nhập
  failures: "Số lần sai liên tiếp",
  locked: "Bị khoá đăng nhập",
  expires_at: "Phiên hết hạn lúc",
  // kiểm tra proxy
  total: "Số proxy kiểm",
  ok: "Sống",
  failed: "Lỗi",
  proxy_ids: "Proxy",
  // xuất dữ liệu (bộ lọc của trang Việc làm)
  fmt: "Định dạng",
  filters: "Bộ lọc",
  search: "Tìm kiếm",
  status: "Trạng thái",
  province: "Tỉnh/thành",
  level: "Cấp bậc",
  employment_type: "Loại hình",
  work_mode: "Hình thức làm việc",
  salary_from: "Lương từ (triệu)",
  company_id: "Doanh nghiệp",
  reviewed: "Kiểm tay",
}

/** Tên trường trong `changes` / `metadata` → nhãn tiếng Việt; tên lạ giữ nguyên. */
export function fieldLabel(key: string): string {
  return FIELDS[key] ?? key
}

const WORDS: Record<string, string> = {
  manual: "Thủ công",
  upload: "File tải lên",
  server: "File trên máy chủ",
  csv: "CSV",
  jsonl: "JSONL",
  // trạng thái việc làm (bộ lọc khi xuất dữ liệu)
  all: "Mọi trạng thái",
  active: "Đang mở",
  expired: "Hết hạn",
  removed: "Đã gỡ",
}

/** Giá trị đã che phía máy chủ ("***", "••••••••"). */
export function isMasked(value: unknown): boolean {
  return typeof value === "string" && /^(\*{3,}|•{3,})$/.test(value.trim())
}

/**
 * Một giá trị trước / sau hoặc trong metadata → chữ ngắn: rỗng "Trống", đã che "Ẩn", có / không,
 * số kiểu Việt Nam, danh sách nối bằng dấu phẩy, object thành "khoá: giá trị".
 */
export function formatAuditValue(value: unknown, key?: string): string {
  if (value === null || value === undefined || value === "") return "Trống"
  if (isMasked(value)) return "Ẩn"
  if (typeof value === "boolean") {
    if (key === "correct") return value ? "Đúng" : "Sai"
    return value ? "Có" : "Không"
  }
  if (typeof value === "number") {
    if ((key === "precision" || key === "recall") && value >= 0 && value <= 1) return `${dec(value * 100)}%`
    if (key === "ttl_seconds") return durationText(value)
    if (key === "stale_hours") return `${num(value)} giờ`
    return Number.isInteger(value) ? num(value) : dec(value, Math.min(3, String(value).split(".")[1]?.length ?? 1))
  }
  if (typeof value === "string") {
    if (key === "expires_at" && !Number.isNaN(Date.parse(value))) return dt(value)
    if (key === "company_id") return shortId(value)
    return WORDS[value] ?? value
  }
  if (Array.isArray(value)) return value.length ? value.map((v) => formatAuditValue(v)).join(", ") : "Trống"
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([k, v]) => `${fieldLabel(k)}: ${formatAuditValue(v, k)}`).join(", ") : "Trống"
  }
  return String(value)
}

/** Dòng có gì để mở xem chi tiết không. */
export function hasDetails(
  event: Pick<AuditEvent, "changes" | "metadata" | "request_id"> & Partial<Pick<AuditEvent, "ip" | "user_agent">>,
): boolean {
  return Boolean(
    (event.changes && Object.keys(event.changes).length) ||
      (event.metadata && Object.keys(event.metadata).length) ||
      event.request_id ||
      event.ip ||
      event.user_agent,
  )
}

/** Số giây → "15 phút", "2 giờ", "45 giây". */
function durationText(seconds: number): string {
  if (seconds % 3600 === 0 && seconds >= 3600) return `${num(seconds / 3600)} giờ`
  if (seconds % 60 === 0 && seconds >= 60) return `${num(seconds / 60)} phút`
  return `${num(seconds)} giây`
}

/** User-Agent → "Chrome 141 · Windows" (trình duyệt + bản chính + hệ điều hành); client không phải
 * trình duyệt (curl, httpx, python…) hiện tên của nó; không nhận ra thì trả nguyên văn đã rút gọn. */
export function browserLabel(ua: string | null | undefined): string {
  if (!ua) return ""
  const tools = /^(curl|Wget|python-requests|python-httpx|httpx|PostmanRuntime|okhttp|Go-http-client)\/?([\d.]*)/i.exec(ua)
  if (tools) return tools[2] ? `${tools[1]} ${tools[2].split(".")[0]}` : tools[1]
  const browsers: [RegExp, string][] = [
    [/Edg(?:e|A|iOS)?\/(\d+)/, "Edge"],
    [/OPR\/(\d+)/, "Opera"],
    [/Firefox\/(\d+)/, "Firefox"],
    [/(?:Chrome|CriOS)\/(\d+)/, "Chrome"],
    [/Version\/(\d+)[\d.]* (?:Mobile\/\S+ )?Safari/, "Safari"],
  ]
  let browser = ""
  for (const [re, name] of browsers) {
    const m = re.exec(ua)
    if (m) {
      browser = `${name} ${m[1]}`
      break
    }
  }
  const os = /Windows/.test(ua)
    ? "Windows"
    : /Android/.test(ua)
      ? "Android"
      : /iPhone|iPad|iPod/.test(ua)
        ? "iOS"
        : /Mac OS X|Macintosh/.test(ua)
          ? "macOS"
          : /Linux/.test(ua)
            ? "Linux"
            : ""
  const text = [browser, os].filter(Boolean).join(" · ")
  return text || (ua.length > 60 ? `${ua.slice(0, 60)}…` : ua)
}

function count(meta: Record<string, unknown>, key: string): number | null {
  const v = meta[key]
  return typeof v === "number" ? v : null
}

/** Một dòng tóm tắt của thao tác (hiện ngay dưới tên thao tác, không cần mở chi tiết): kết quả và
 * con số chính lấy từ `metadata` / `changes`. Thao tác lạ: vài mục đầu của metadata. */
export function summary(event: Pick<AuditEvent, "action" | "metadata" | "changes">): string {
  const meta = (event.metadata ?? {}) as Record<string, unknown>
  const parts: string[] = []
  const add = (text: string | null | false | undefined) => {
    if (text) parts.push(text)
  }
  switch (event.action) {
    case "settings.update":
    case "settings.reset":
    case "settings.import_env": {
      const n = Object.keys(event.changes ?? {}).length
      add(n ? `${num(n)} cài đặt` : null)
      const reset = Array.isArray(meta.reset) ? meta.reset.length : 0
      add(reset && event.action !== "settings.reset" ? `${num(reset)} về mặc định` : null)
      break
    }
    case "company.build":
    case "company.poll":
    case "board.index":
    case "maintenance.run":
      if (typeof meta.queued === "boolean") add(meta.queued ? "Đã vào hàng đợi" : "Đã có trong hàng đợi, không xếp thêm")
      if (meta.trigger && meta.trigger !== "manual") add(formatAuditValue(meta.trigger))
      break
    case "companies.enqueue":
      add(meta.all === true ? "Mọi doanh nghiệp" : count(meta, "selected") !== null ? `Chọn ${num(count(meta, "selected"))}` : null)
      add(count(meta, "enqueued") !== null ? `${num(count(meta, "enqueued"))} vào hàng đợi` : null)
      add(count(meta, "already_queued") ? `${num(count(meta, "already_queued"))} đã có sẵn` : null)
      break
    case "companies.import":
      add(typeof meta.filename === "string" ? meta.filename : null)
      add(count(meta, "rows") !== null ? `${num(count(meta, "rows"))} dòng` : null)
      add(count(meta, "created") ? `${num(count(meta, "created"))} mới` : null)
      add(count(meta, "updated") ? `${num(count(meta, "updated"))} cập nhật` : null)
      add(count(meta, "errors") ? `${num(count(meta, "errors"))} bị bỏ` : null)
      break
    case "company.create":
      add(typeof meta.name === "string" ? meta.name : null)
      add(meta.created === false ? "Đã có, chỉ bổ sung" : null)
      break
    case "job.review": {
      const correct = (event.changes?.correct as { after?: unknown } | undefined)?.after
      add(typeof correct === "boolean" ? (correct ? "Đúng" : "Sai") : null)
      const note = (event.changes?.note as { after?: unknown } | undefined)?.after
      add(typeof note === "string" && note ? `“${note}”` : null)
      break
    }
    case "auth.login_failed":
      add(count(meta, "failures") !== null ? `Lần sai thứ ${num(count(meta, "failures"))}` : null)
      add(meta.locked === true ? "Bị khoá đăng nhập 5 phút" : null)
      break
    case "auth.login":
      add(typeof meta.expires_at === "string" ? `Phiên đến ${formatAuditValue(meta.expires_at, "expires_at")}` : null)
      break
    case "jobs.export": {
      add(typeof meta.fmt === "string" ? formatAuditValue(meta.fmt) : null)
      const filters = meta.filters && typeof meta.filters === "object" ? Object.keys(meta.filters as object) : []
      add(filters.length ? `lọc theo ${filters.map(fieldLabel).join(", ").toLowerCase()}` : "không lọc")
      break
    }
    case "proxies.check":
      add(count(meta, "total") !== null ? `${num(count(meta, "total"))} proxy` : null)
      add(count(meta, "ok") !== null ? `${num(count(meta, "ok"))} sống` : null)
      add(count(meta, "failed") ? `${num(count(meta, "failed"))} lỗi` : null)
      break
    case "worker.join_key":
      add(count(meta, "ttl_seconds") !== null ? `Hạn ${formatAuditValue(meta.ttl_seconds, "ttl_seconds")}` : null)
      break
    case "worker.joined":
    case "worker.drain":
    case "worker.undrain":
      add(typeof meta.hostname === "string" ? `Máy ${meta.hostname}` : null)
      add(typeof meta.pool === "string" && meta.pool !== "default" ? `nhóm ${meta.pool}` : null)
      add(meta.gone === true ? "worker đã rời" : null)
      break
    case "eval.run":
      add(count(meta, "precision") !== null ? `Precision ${formatAuditValue(meta.precision, "precision")}` : null)
      add(count(meta, "recall") !== null ? `Recall ${formatAuditValue(meta.recall, "recall")}` : null)
      break
    default:
      for (const [k, v] of Object.entries(meta).slice(0, 3)) add(`${fieldLabel(k)}: ${formatAuditValue(v, k)}`)
  }
  return parts.join(" · ")
}
