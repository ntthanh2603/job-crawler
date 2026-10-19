import { MutationObserver } from "@tanstack/react-query"
import { toast } from "sonner"
import { describe, expect, it, vi } from "vitest"

import { ApiError } from "./api"
import { createQueryClient } from "./query-client"

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }))

async function runFailing(options: { onError?: (e: Error) => void } = {}) {
  const client = createQueryClient()
  const observer = new MutationObserver(client, {
    mutationFn: () => Promise.reject(new ApiError(409, "lượt poll đã có trong hàng đợi")),
    ...options,
  })
  await observer.mutate().catch(() => undefined)
}

describe("createQueryClient", () => {
  it("mutation lỗi báo toast bằng thông báo của API", async () => {
    await runFailing()
    expect(toast.error).toHaveBeenCalledWith("Lượt poll đã có trong hàng đợi")
  })

  it("mutation tự khai báo onError thì thay cho toast mặc định", async () => {
    const onError = vi.fn()
    await runFailing({ onError })
    expect(onError).toHaveBeenCalledOnce()
    expect(toast.error).not.toHaveBeenCalled()
  })

  it("query không thử lại lỗi 4xx, thử lại một lần lỗi mạng / 5xx", () => {
    const retry = createQueryClient().getDefaultOptions().queries?.retry as (count: number, e: Error) => boolean
    expect(retry(0, new ApiError(404, "x"))).toBe(false)
    expect(retry(0, new ApiError(503, "x"))).toBe(true)
    expect(retry(1, new ApiError(503, "x"))).toBe(false)
    expect(retry(0, new TypeError("Failed to fetch"))).toBe(true)
  })
})
