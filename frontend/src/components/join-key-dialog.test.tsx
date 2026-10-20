import { QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { createMemoryRouter, RouterProvider } from "react-router"
import { describe, expect, it, vi } from "vitest"

import { createQueryClient } from "@/lib/query-client"

import { JoinKeyDialog } from "./join-key-dialog"

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
}

const KEY = {
  id: "a1b2c3d4e5f6",
  key: "jcj_secret-KEY_1",
  expires_at_ms: Date.now() + 900_000,
  ttl_seconds: 900,
  gateway_url: "http://localhost:8000",
  worker_image: "ghcr.io/ntthanh2603/jobcrawler-worker:main",
}

describe("hộp thoại Thêm worker", () => {
  it("ghép lệnh docker pull + run kèm khoá, pool; chép lệnh; đóng thì huỷ khoá", async () => {
    const calls: string[] = []
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof globalThis.fetch>(async (input, init) => {
        const url = String(input)
        calls.push(`${init?.method ?? "GET"} ${url}`)
        if (init?.method === "POST" && url === "/api/workers/join-keys") return json(KEY)
        return json({ id: KEY.id, status: "pending", worker_id: null })
      }),
    )
    const writeText = vi.fn(async (_text: string) => undefined)
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })

    const onOpenChange = vi.fn()
    const router = createMemoryRouter(
      [{ path: "/workers", element: <JoinKeyDialog open onOpenChange={onOpenChange} pools={["residential"]} /> }],
      { initialEntries: ["/workers"] },
    )
    const view = render(
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    const dialog = await screen.findByRole("dialog", { name: "Thêm worker" })
    const command = await within(dialog).findByLabelText("Lệnh chạy worker")
    expect(command.textContent?.split("\n")).toEqual([
      "docker pull ghcr.io/ntthanh2603/jobcrawler-worker:main",
      "docker run -d --name jobcrawler-worker-a1b2c3 --restart unless-stopped --init --stop-timeout 40 --shm-size 1g " +
        "--add-host host.docker.internal:host-gateway -e GATEWAY_ADDR=host.docker.internal:8000 -e GATEWAY_TLS=false " +
        "-e WORKER_POOL=default -e WORKER_JOIN_KEY=jcj_secret-KEY_1 ghcr.io/ntthanh2603/jobcrawler-worker:main",
    ])
    expect(within(dialog).getByText(/worker ở máy khác không nối vào được/)).toBeTruthy()

    fireEvent.change(within(dialog).getByLabelText("Pool"), { target: { value: "residential; rm" } })
    expect(command.textContent).toContain("-e WORKER_POOL=residentialrm ")

    fireEvent.click(within(dialog).getByRole("button", { name: "Chép lệnh" }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(command.textContent))

    view.unmount()
    await waitFor(() => expect(calls).toContain(`DELETE /api/workers/join-keys/${KEY.id}`))
  })
})
