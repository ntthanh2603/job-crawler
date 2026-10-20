// Dòng proxy của cài đặt `proxies` ↔ các ô của bảng proxy (loại, host, cổng, tài khoản, mật khẩu).
// Máy chủ trả dòng đã che mật khẩu (`http://user:•••@host:port`, `host:port:user:•••`); gửi lại đúng
// dòng đó thì máy chủ giữ mật khẩu đã lưu (backend `_unmask_proxies`).

export const PROXY_MASK = "•••"

export const PROXY_SCHEMES = ["http", "https", "socks5", "socks5h", "socks4", "socks4a"] as const

export type ProxyRow = {
  scheme: string
  host: string
  port: string
  username: string
  /** null = giữ mật khẩu đã lưu (chỉ khi `masked`). */
  password: string | null
  /** Dòng máy chủ trả khi mật khẩu đang ẩn; gửi lại nguyên dòng này để giữ mật khẩu. */
  masked: string | null
}

function decode(text: string): string {
  try {
    return decodeURIComponent(text)
  } catch {
    return text
  }
}

function split(user: string, password: string | null, rest: Omit<ProxyRow, "username" | "password" | "masked">, line: string): ProxyRow {
  const hidden = password !== null && password.includes(PROXY_MASK)
  return {
    ...rest,
    username: user,
    password: hidden ? null : (password ?? ""),
    masked: hidden ? line : null,
  }
}

/** Một dòng proxy → các ô; dòng trống / chú thích / không hiểu → null. Nhận `scheme://[user[:pass]@]host:port`,
 * `host:port`, `host:port:user:pass`, `user:pass@host:port` (mặc định http). */
export function parseProxyLine(raw: string): ProxyRow | null {
  const line = raw.split("#", 1)[0].trim()
  if (!line) return null
  const url = /^([a-z][a-z0-9+.-]*):\/\/(?:([^@/]*)@)?(\[[^\]]+\]|[^:/@[\]]+):(\d+)\/?$/i.exec(line)
  if (url) {
    const [, scheme, credentials, host, port] = url
    const [user, ...pass] = (credentials ?? "").split(":")
    return split(decode(user ?? ""), pass.length ? decode(pass.join(":")) : null, { scheme: scheme.toLowerCase(), host: host.replace(/^\[|\]$/g, ""), port }, line)
  }
  const at = /^([^@:]+):([^@]*)@([^:@]+):(\d+)$/.exec(line)
  if (at) return split(at[1], at[2], { scheme: "http", host: at[3], port: at[4] }, line)
  const parts = line.split(":")
  if (parts.length === 2 && /^\d+$/.test(parts[1])) return split("", null, { scheme: "http", host: parts[0], port: parts[1] }, line)
  if (parts.length === 4 && /^\d+$/.test(parts[1])) return split(parts[2], parts[3], { scheme: "http", host: parts[0], port: parts[1] }, line)
  return null
}

/** Proxy nào (không tính mật khẩu): để nhận ra trùng và biết dòng đã đổi chỗ khác mật khẩu chưa. */
export function proxyIdentity(row: Pick<ProxyRow, "scheme" | "host" | "port" | "username">): string {
  return `${row.scheme}://${row.username ? `${row.username}@` : ""}${row.host.toLowerCase()}:${Number(row.port)}`
}

/** Các ô → dòng lưu trong cài đặt. Giữ mật khẩu đã lưu thì gửi lại dòng đã che của máy chủ. */
export function toProxyLine(row: ProxyRow): string {
  if (row.password === null && row.masked) return row.masked
  const host = row.host.includes(":") ? `[${row.host}]` : row.host
  const user = row.username.trim()
  const credentials = user
    ? `${encodeURIComponent(user)}${row.password ? `:${encodeURIComponent(row.password)}` : ""}@`
    : ""
  return `${row.scheme}://${credentials}${host}:${Number(row.port)}`
}

/** Lỗi của các ô (null = hợp lệ). */
export function proxyRowError(row: ProxyRow): string | null {
  if (!(PROXY_SCHEMES as readonly string[]).includes(row.scheme)) return "Loại proxy không hỗ trợ"
  const host = row.host.trim()
  if (!host) return "Thiếu host"
  if (/[\s/@]/.test(host)) return "Host không hợp lệ"
  const port = Number(row.port)
  if (!row.port.trim() || !Number.isInteger(port) || port < 1 || port > 65_535) return "Cổng phải là số từ 1 đến 65535"
  if (!row.username.trim() && row.password) return "Có mật khẩu thì cần tài khoản"
  if (row.password === null) {
    const saved = row.masked ? parseProxyLine(row.masked) : null
    if (!saved || proxyIdentity(saved) !== proxyIdentity(row)) return "Đã đổi proxy: nhập lại mật khẩu"
  }
  return null
}

export type PastedProxies = { rows: ProxyRow[]; invalid: { line: number; text: string }[] }

/** Nội dung dán (mỗi dòng một proxy) → các proxy hợp lệ và các dòng không hiểu. */
export function parsePastedProxies(text: string): PastedProxies {
  const rows: ProxyRow[] = []
  const invalid: PastedProxies["invalid"] = []
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.split("#", 1)[0].trim()
    if (!line) return
    const row = parseProxyLine(line)
    if (!row || row.password === null || proxyRowError(row)) invalid.push({ line: i + 1, text: line })
    else rows.push(row)
  })
  return { rows, invalid }
}

const ERROR_TEXT: Record<string, string> = {
  timeout: "Quá thời gian chờ",
  proxy: "Proxy từ chối hoặc không kết nối được",
  connect: "Không kết nối được",
}

/** Lỗi kiểm tra (`<kind>: <chi tiết>` hoặc `http_<mã>`) → câu ngắn; chi tiết xem khi rê chuột. */
export function checkErrorText(error: string | null): string {
  if (!error) return "Lỗi"
  if (error.includes("no_worker")) return "Không có worker nào đang chạy để kiểm tra"
  const http = /^http_(\d+)/.exec(error)
  if (http) return `Trang kiểm tra trả HTTP ${http[1]}`
  return ERROR_TEXT[error.split(":", 1)[0]] ?? error.slice(0, 80)
}
