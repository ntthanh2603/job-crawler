// Gọi API FastAPI cùng origin (dev: Vite chuyển /api sang cổng 8000).
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

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "" || value === false) continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ""
}

export const api = {
  get: <T>(path: string) => fetch(`/api${path}`, { credentials: "same-origin" }).then((r) => handle<T>(r)),
  post: <T>(path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => handle<T>(r)),
  put: <T>(path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => handle<T>(r)),
  form: <T>(path: string, form: FormData) =>
    fetch(`/api${path}`, { method: "POST", credentials: "same-origin", body: form }).then((r) => handle<T>(r)),
  delete: <T>(path: string) =>
    fetch(`/api${path}`, { method: "DELETE", credentials: "same-origin" }).then((r) => handle<T>(r)),
}
