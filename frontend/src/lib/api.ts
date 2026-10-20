// Gọi API FastAPI cùng origin (/api): khi dev Vite chuyển sang cổng 8000, trong image nginx chuyển tới backend.
import { cap } from "@/lib/format"

export class ApiError extends Error {
  status: number
  /** `detail` gốc của FastAPI khi là object (vd. `{ errors: { key: "thông báo" } }` của trang Cài đặt). */
  detail: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }

  /** Lỗi theo từng trường (422 của `PUT /api/settings`): tên cài đặt → thông báo. */
  get fieldErrors(): Record<string, string> | null {
    const errors = (this.detail as { errors?: unknown } | null | undefined)?.errors
    return errors && typeof errors === "object" ? (errors as Record<string, string>) : null
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    let detail: unknown
    try {
      const body = await res.json()
      detail = body?.detail
      if (typeof body?.detail === "string") message = body.detail
      else if (Array.isArray(body?.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ")
      else if (body?.detail?.errors && typeof body.detail.errors === "object")
        message = Object.values(body.detail.errors as Record<string, string>).join("; ")
    } catch {
      // body không phải JSON
    }
    throw new ApiError(res.status, message, detail)
  }
  return (await res.json()) as T
}

/**
 * Thông báo lỗi để hiện cho người dùng (toast, khung lỗi): lỗi API dùng `detail` của FastAPI (đã
 * gộp vào `message`), lỗi mạng thành câu tiếng Việt; viết hoa chữ đầu.
 */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return cap(e.message) || `Lỗi ${e.status}`
  // fetch() bị từ chối (mất mạng, máy chủ tắt): TypeError "Failed to fetch" / "NetworkError…".
  if (e instanceof TypeError && /fetch|network/i.test(e.message)) return "Không kết nối được máy chủ"
  if (e instanceof Error) return cap(e.message) || "Đã có lỗi xảy ra"
  return cap(String(e ?? "")) || "Đã có lỗi xảy ra"
}

type QueryValue = string | number | boolean | null | undefined

export function qs(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "" || value === false) continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ""
}


/** Lời gọi do orval sinh ra (src/lib/gen/queries.ts): dạng cấu hình kiểu axios. */
export type RequestConfig = {
  url: string
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
  params?: Record<string, QueryValue>
  data?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
}

/**
 * Client của mọi hàm / hook sinh từ spec (`mutator` trong orval.config.ts, như `orvalClient` của
 * open-asm): fetch cùng origin kèm cookie / đăng nhập Basic của trình duyệt; query bỏ giá trị rỗng
 * như `qs`; body JSON, hoặc FormData để trình duyệt tự đặt `Content-Type` (boundary). Lỗi HTTP ném
 * `ApiError` (đọc `detail` của FastAPI).
 */
export async function apiClient<T>({ url, method, params, data, signal }: RequestConfig): Promise<T> {
  const form = data instanceof FormData
  const res = await fetch(`${url}${qs(params ?? {})}`, {
    method,
    credentials: "same-origin",
    signal,
    headers: data === undefined || form ? undefined : { "Content-Type": "application/json" },
    body: data === undefined ? undefined : form ? data : JSON.stringify(data),
  })
  return handle<T>(res)
}
