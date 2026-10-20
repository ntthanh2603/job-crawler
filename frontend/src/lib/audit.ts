// Nhật ký thao tác: nhãn tiếng Việt cho tên thao tác / loại đối tượng / trường, và cách hiện giá trị.
import { cap, dec, num, sourceLabel } from "@/lib/format"
import type { AuditEvent } from "@/lib/types"

const ACTIONS: Record<string, string> = {
  "settings.update": "Lưu cài đặt",
  "settings.reset": "Đặt lại cài đặt về mặc định",
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
  settings: "Cài đặt",
}

export function resourceTypeLabel(type: string | null | undefined): string {
  if (!type) return "–"
  return RESOURCE_TYPES[type] ?? cap(type)
}

/** Đường dẫn trong ứng dụng tới đối tượng của dòng nhật ký (doanh nghiệp, việc làm), nếu có. */
export function resourceHref(event: Pick<AuditEvent, "resource_type" | "resource_id">): string | null {
  if (!event.resource_id) return null
  if (event.resource_type === "company") return `/companies/${event.resource_id}`
  if (event.resource_type === "job") return `/jobs/${event.resource_id}`
  return null
}

/** Tên ngắn của đối tượng: trang tuyển dụng theo thương hiệu, id dài (UUID) rút gọn 8 ký tự. */
export function resourceName(event: Pick<AuditEvent, "resource_type" | "resource_id">): string {
  const id = event.resource_id
  if (!id) return ""
  if (event.resource_type === "board") return sourceLabel(`board:${id}`)
  return shortId(id)
}

export function shortId(id: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) ? `${id.slice(0, 8)}…` : id
}

export function actorLabel(actor: string): string {
  if (actor === "system") return "Hệ thống"
  if (actor === "anonymous") return "Không đăng nhập"
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
  // worker
  hostname: "Máy",
  pool: "Nhóm",
  // cài đặt
  reset: "Về giá trị mặc định",
}

/** Tên trường trong `changes` / `metadata` → nhãn tiếng Việt; tên lạ giữ nguyên. */
export function fieldLabel(key: string): string {
  return FIELDS[key] ?? key
}

const WORDS: Record<string, string> = {
  manual: "Thủ công",
  upload: "File tải lên",
  server: "File trên máy chủ",
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
    return Number.isInteger(value) ? num(value) : dec(value, Math.min(3, String(value).split(".")[1]?.length ?? 1))
  }
  if (typeof value === "string") return WORDS[value] ?? value
  if (Array.isArray(value)) return value.length ? value.map((v) => formatAuditValue(v)).join(", ") : "Trống"
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length ? entries.map(([k, v]) => `${k}: ${formatAuditValue(v, k)}`).join(", ") : "Trống"
  }
  return String(value)
}

/** Dòng có gì để mở xem chi tiết không. */
export function hasDetails(event: Pick<AuditEvent, "changes" | "metadata" | "request_id">): boolean {
  return Boolean(
    (event.changes && Object.keys(event.changes).length) ||
      (event.metadata && Object.keys(event.metadata).length) ||
      event.request_id,
  )
}
