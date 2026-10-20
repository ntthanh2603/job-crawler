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
}

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

export function workerRunCommand(c: WorkerCommand): string {
  const { host, port, tls } = gatewayTarget(c.gatewayUrl)
  const local = isLoopback(c.gatewayUrl)
  const run = [
    "docker run -d",
    `--name ${c.name}`,
    "--restart unless-stopped",
    "--init --stop-timeout 40 --shm-size 1g",
    ...(local ? [`--add-host ${DOCKER_HOST_ALIAS}:host-gateway`] : []),
    `-e GATEWAY_ADDR=${local ? DOCKER_HOST_ALIAS : host}:${port}`,
    `-e GATEWAY_TLS=${tls}`,
    `-e WORKER_POOL=${cleanPool(c.pool) || "default"}`,
    `-e WORKER_JOIN_KEY=${c.joinKey}`,
    c.image,
  ]
  return `docker pull ${c.image}\n${run.join(" ")}`
}
