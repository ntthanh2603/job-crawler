// Gọi API FastAPI cùng origin (dev: Vite chuyển /api sang cổng 8000).

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (typeof body?.detail === "string") message = body.detail
      else if (Array.isArray(body?.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ")
    } catch {
      // body không phải JSON
    }
    throw new ApiError(res.status, message)
  }
  return (await res.json()) as T
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
  form: <T>(path: string, form: FormData) =>
    fetch(`/api${path}`, { method: "POST", credentials: "same-origin", body: form }).then((r) => handle<T>(r)),
  delete: <T>(path: string) =>
    fetch(`/api${path}`, { method: "DELETE", credentials: "same-origin" }).then((r) => handle<T>(r)),
}
