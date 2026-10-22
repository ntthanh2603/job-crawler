import { describe, expect, it } from "vitest"

import {
  cleanPool,
  cleanSlots,
  gatewayTarget,
  isLoopback,
  parseSlots,
  workerCommandLines,
  workerRunCommand,
} from "@/lib/worker-command"

const base = {
  image: "ghcr.io/ntthanh2603/jobcrawler-worker:main",
  joinKey: "jcj_abc-DEF_123",
  pool: "residential",
  name: "jobcrawler-worker-a1b2c3",
}

describe("workerRunCommand", () => {
  it("pulls the image then runs it with the join key and gateway config", () => {
    const cmd = workerRunCommand({ ...base, gatewayUrl: "https://gw.example.com" })
    expect(cmd.split("\n")).toEqual([
      "docker pull ghcr.io/ntthanh2603/jobcrawler-worker:main",
      "docker run -d --name jobcrawler-worker-a1b2c3 --restart unless-stopped --init --stop-timeout 40 --shm-size 1g " +
        "-e GATEWAY_ADDR=gw.example.com:443 -e GATEWAY_TLS=true -e WORKER_POOL=residential " +
        "-e WORKER_JOIN_KEY=jcj_abc-DEF_123 ghcr.io/ntthanh2603/jobcrawler-worker:main",
    ])
  })

  it("splits each command into flag groups that join back into the copied text", () => {
    const spec = { ...base, gatewayUrl: "https://gw.example.com" }
    const lines = workerCommandLines(spec)
    expect(lines[1]).toContain("--stop-timeout 40")
    expect(lines[1]).toContain("-e WORKER_JOIN_KEY=jcj_abc-DEF_123")
    expect(lines.map((groups) => groups.join(" ")).join("\n")).toBe(workerRunCommand(spec))
  })

  it("reaches a gateway on the Docker host through host.docker.internal", () => {
    for (const url of ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]) {
      const cmd = workerRunCommand({ ...base, gatewayUrl: url })
      expect(cmd).toContain("--add-host host.docker.internal:host-gateway")
      expect(cmd).toContain("-e GATEWAY_ADDR=host.docker.internal:8000 -e GATEWAY_TLS=false")
    }
  })

  it("falls back to the default pool and strips unsafe characters", () => {
    expect(workerRunCommand({ ...base, gatewayUrl: "http://10.0.0.5:8000", pool: " ;$" })).toContain(
      "-e WORKER_POOL=default",
    )
    expect(cleanPool("hn pool;rm -rf")).toBe("hnpoolrm-rf")
  })

  it("sets the worker's max concurrent jobs only when given (empty = worker sizes itself)", () => {
    const url = "http://10.0.0.5:8000"
    expect(workerRunCommand({ ...base, gatewayUrl: url })).not.toContain("_SLOTS")
    const cmd = workerRunCommand({ ...base, gatewayUrl: url, httpSlots: 32, renderSlots: 0 })
    expect(cmd).toContain("-e WORKER_POOL=residential -e WORKER_HTTP_SLOTS=32 -e WORKER_RENDER_SLOTS=0 -e WORKER_JOIN_KEY=")
    expect(cleanSlots("3a2;9")).toBe("329")
    expect(parseSlots("", 1)).toBeNull()
    expect(parseSlots("0", 1)).toBeNull() // HTTP cần ít nhất 1
    expect(parseSlots("0", 0)).toBe(0) // render 0 = không mở trình duyệt
  })

  it("passes the browser fingerprint only for known curl_cffi targets", () => {
    const url = "http://10.0.0.5:8000"
    expect(workerRunCommand({ ...base, gatewayUrl: url, impersonate: "safari" })).toContain("-e WORKER_IMPERSONATE=safari ")
    expect(workerRunCommand({ ...base, gatewayUrl: url, impersonate: "x; rm" })).not.toContain("WORKER_IMPERSONATE")
  })
})

describe("gatewayTarget", () => {
  it("takes host, default port and TLS from the URL", () => {
    expect(gatewayTarget("http://Gw.Example.com")).toEqual({ host: "gw.example.com", port: "80", tls: false })
    expect(gatewayTarget("https://[2001:db8::1]:8443")).toEqual({ host: "[2001:db8::1]", port: "8443", tls: true })
    expect(isLoopback("http://127.0.0.2:8000")).toBe(true)
    expect(isLoopback("http://192.168.1.10:8000")).toBe(false)
  })
})
