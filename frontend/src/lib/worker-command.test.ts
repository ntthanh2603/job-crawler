import { describe, expect, it } from "vitest"

import { cleanPool, gatewayTarget, isLoopback, workerRunCommand } from "@/lib/worker-command"

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
})

describe("gatewayTarget", () => {
  it("takes host, default port and TLS from the URL", () => {
    expect(gatewayTarget("http://Gw.Example.com")).toEqual({ host: "gw.example.com", port: "80", tls: false })
    expect(gatewayTarget("https://[2001:db8::1]:8443")).toEqual({ host: "[2001:db8::1]", port: "8443", tls: true })
    expect(isLoopback("http://127.0.0.2:8000")).toBe(true)
    expect(isLoopback("http://192.168.1.10:8000")).toBe(false)
  })
})
