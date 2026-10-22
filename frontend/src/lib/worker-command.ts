// Lệnh Docker của hộp thoại "Thêm worker": mỗi dòng một lệnh, dán được vào mọi shell.

// Máy chạy Docker, nhìn từ trong container.
export const DOCKER_HOST_ALIAS = "host.docker.internal"

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"])

export type WorkerCommand = {
  image: string
  gatewayUrl: string // địa chỉ giao diện: worker nối gRPC qua cùng cổng
  joinKey: string
  pool: string
  name: string
  // Số việc cùng lúc tối đa của worker (như WORKER_MAX_CONCURRENCY của open-asm): null = worker tự
  // tính theo CPU / RAM của máy nó. Render 0 = không mở Chromium.
  httpSlots?: number | null
  renderSlots?: number | null
  impersonate?: string // dấu vân tay TLS / HTTP worker giả lập (curl_cffi); rỗng = mặc định của worker
}

// Cấu hình của worker nằm trong lệnh khởi tạo (không đẩy từ trang Cài đặt): giá trị điền sẵn, hợp
// với máy 4 CPU / 8 GB (bằng số worker tự tính trên máy đó).
export const DEFAULT_HTTP_SLOTS = 32
export const DEFAULT_RENDER_SLOTS = 4
export const IMPERSONATE_OPTIONS = ["chrome", "edge", "safari", "firefox"] as const

export function gatewayTarget(url: string): { host: string; port: string; tls: boolean } {
  const u = new URL(url)
  const tls = u.protocol === "https:"
  return { host: u.hostname.toLowerCase(), port: u.port || (tls ? "443" : "80"), tls }
}

export function isLoopback(url: string): boolean {
  const { host } = gatewayTarget(url)
  return LOOPBACK.has(host) || host.startsWith("127.")
}

// Chỉ giữ ký tự không cần trích dẫn trong shell.
export function cleanPool(value: string): string {
  return value.replace(/[^A-Za-z0-9_.-]/g, "")
}

// Ô nhập số slot: chỉ chữ số, tối đa 3 chữ số.
export function cleanSlots(value: string): string {
  return value.replace(/\D/g, "").slice(0, 3)
}

// Chuỗi trong ô → số slot; trống (hoặc dưới `min`) = để worker tự tính.
export function parseSlots(value: string, min: number): number | null {
  if (value === "") return null
  const n = Number(value)
  return Number.isInteger(n) && n >= min ? n : null
}

// Từng dòng lệnh, mỗi dòng chia thành nhóm (cờ + giá trị) để hiển thị chỉ xuống dòng giữa các nhóm.
export function workerCommandLines(c: WorkerCommand): string[][] {
  const { host, port, tls } = gatewayTarget(c.gatewayUrl)
  const local = isLoopback(c.gatewayUrl)
  const run = [
    "docker run -d",
    `--name ${c.name}`,
    "--restart unless-stopped",
    "--init",
    "--stop-timeout 40",
    "--shm-size 1g",
    ...(local ? [`--add-host ${DOCKER_HOST_ALIAS}:host-gateway`] : []),
    `-e GATEWAY_ADDR=${local ? DOCKER_HOST_ALIAS : host}:${port}`,
    `-e GATEWAY_TLS=${tls}`,
    `-e WORKER_POOL=${cleanPool(c.pool) || "default"}`,
    ...(c.httpSlots != null && c.httpSlots >= 1 ? [`-e WORKER_HTTP_SLOTS=${c.httpSlots}`] : []),
    ...(c.renderSlots != null && c.renderSlots >= 0 ? [`-e WORKER_RENDER_SLOTS=${c.renderSlots}`] : []),
    ...(c.impersonate && (IMPERSONATE_OPTIONS as readonly string[]).includes(c.impersonate)
      ? [`-e WORKER_IMPERSONATE=${c.impersonate}`]
      : []),
    `-e WORKER_JOIN_KEY=${c.joinKey}`,
    c.image,
  ]
  return [[`docker pull ${c.image}`], run]
}

export function workerRunCommand(c: WorkerCommand): string {
  return workerCommandLines(c)
    .map((groups) => groups.join(" "))
    .join("\n")
}
