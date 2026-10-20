import { QueryClient } from "@tanstack/react-query"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { FakeEventSource } from "@/test/fake-event-source"

import {
  getGetQueueQueryKey,
  getGetRunLogsQueryKey,
  getGetRunQueryKey,
  getGetWorkersQueryKey,
  type LiveOut,
  type QueuePageOut,
  type RunDetailOut,
  type WorkersPageOut,
} from "./gen/queries"
import { eventsUrl, LiveConnection, RETRY_MS, STALE_MS } from "./live"

const workers = { workers: [], summary: { alive: 2 }, now_ms: 1, auth_enabled: true } as unknown as WorkersPageOut
const queue = { queues: [{ name: "build", queued: 3, jobs: [] }] } as unknown as QueuePageOut
const run = (logs: number, fetches = 0) => ({ run: { status: "running" }, logs_total: logs, fetches_total: fetches }) as unknown as RunDetailOut

function event(msg: Partial<LiveOut> & Pick<LiveOut, "topic">): LiveOut {
  return { workers: null, queue: null, run: null, ...msg }
}

let client: QueryClient
let live: LiveConnection

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.reset()
  client = new QueryClient()
  live = new LiveConnection(client, (url) => new FakeEventSource(url) as unknown as EventSource)
})

afterEach(() => {
  live.stop()
})

/** Các đăng ký gộp lại sau một nhịp (cùng lượt render). */
const flush = () => vi.advanceTimersByTimeAsync(0)

describe("eventsUrl", () => {
  it("gộp chủ đề, tham số trang Hàng đợi thắng thanh bên, kèm id lượt chạy", () => {
    expect(eventsUrl([])).toBeNull()
    expect(eventsUrl([{ topic: "queue" }, { topic: "workers" }, { topic: "queue" }])).toBe("/api/events?topic=workers&topic=queue")
    expect(
      eventsUrl([
        { topic: "queue" },
        { topic: "queue", params: { page: 2, poll_page: 1, board_page: 1 } },
        { topic: "run", runId: "r-1" },
      ]),
    ).toBe("/api/events?topic=queue&topic=run&page=2&poll_page=1&board_page=1&run_id=r-1")
  })
})

describe("LiveConnection", () => {
  it("một EventSource cho mọi đăng ký; trạng thái theo kết nối", async () => {
    const states: string[] = []
    live.onState(() => states.push(live.state))
    const offWorkers = live.subscribe({ topic: "workers" })
    const offQueue = live.subscribe({ topic: "queue" })
    await flush()
    expect(FakeEventSource.instances.map((s) => s.url)).toEqual(["/api/events?topic=workers&topic=queue"])
    FakeEventSource.last.accept()
    expect(live.state).toBe("open")

    // Bớt một chủ đề: mở lại với URL mới, đóng kết nối cũ.
    offQueue()
    await flush()
    expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/events?topic=workers"])
    // Đăng ký trùng URL: không mở lại.
    const again = live.subscribe({ topic: "workers" })
    await flush()
    expect(FakeEventSource.instances).toHaveLength(2)
    again()
    offWorkers()
    await flush()
    expect(FakeEventSource.open).toEqual([])
    expect(live.state).toBe("idle")
    expect(states).toEqual(["connecting", "open", "connecting", "idle"])
  })

  it("ghi sự kiện vào cache đúng khoá của hook orval", async () => {
    const params = { page: 2, poll_page: 1, board_page: 1 }
    live.subscribe({ topic: "workers" })
    live.subscribe({ topic: "queue", params })
    await flush()
    const source = FakeEventSource.last
    source.accept()
    source.emit(event({ topic: "workers", workers }))
    source.emit(event({ topic: "queue", queue }))
    source.onmessage?.(new MessageEvent("message", { data: "không phải JSON" }))
    expect(client.getQueryData(getGetWorkersQueryKey())).toEqual(workers)
    expect(client.getQueryData(getGetQueueQueryKey(params))).toEqual(queue)
    // Thanh bên / Tổng quan đọc số đếm ở khoá không tham số.
    expect(client.getQueryData(getGetQueueQueryKey())).toEqual(queue)
  })

  it("lượt chạy: log mới thì tải lại danh sách log", async () => {
    const invalidate = vi.spyOn(client, "invalidateQueries")
    live.subscribe({ topic: "run", runId: "r-1" })
    await flush()
    const source = FakeEventSource.last
    source.accept()
    source.emit(event({ topic: "run", run: run(3) }))
    expect(client.getQueryData(getGetRunQueryKey("r-1"))).toEqual(run(3))
    expect(invalidate).not.toHaveBeenCalled() // chưa có bản trước để so
    source.emit(event({ topic: "run", run: run(3) }))
    expect(invalidate).not.toHaveBeenCalled()
    source.emit(event({ topic: "run", run: run(5) }))
    expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: getGetRunLogsQueryKey("r-1") })
  })

  it("trình duyệt tự nối lại: chỉ đổi trạng thái; hỏng hẳn: thử lại sau, giãn dần", async () => {
    live.subscribe({ topic: "workers" })
    await flush()
    const first = FakeEventSource.last
    first.accept()
    first.fail({ retry: true })
    expect(live.state).toBe("connecting")
    expect(FakeEventSource.instances).toHaveLength(1)
    first.accept()
    expect(live.state).toBe("open")

    first.fail() // vd. 401 khi token hết hạn: EventSource đóng hẳn
    expect(live.state).toBe("down")
    expect(first.closed).toBe(true)
    await vi.advanceTimersByTimeAsync(RETRY_MS[0] - 1)
    expect(FakeEventSource.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(live.state).toBe("connecting")
    FakeEventSource.last.fail()
    await vi.advanceTimersByTimeAsync(RETRY_MS[1])
    expect(FakeEventSource.instances).toHaveLength(3)
    expect(FakeEventSource.last.url).toBe("/api/events?topic=workers")
    FakeEventSource.last.accept()
    expect(live.state).toBe("open")
  })

  it("im quá STALE_MS (kết nối chết mà trình duyệt không báo): đóng, poll, nối lại", async () => {
    live.subscribe({ topic: "workers" })
    await flush()
    const first = FakeEventSource.last
    first.accept()
    // Có sự kiện (máy chủ gửi lại ít nhất mỗi 30 giây): kết nối còn sống.
    await vi.advanceTimersByTimeAsync(STALE_MS - 1)
    first.emit(event({ topic: "workers", workers }))
    await vi.advanceTimersByTimeAsync(STALE_MS - 1)
    expect(live.state).toBe("open")
    await vi.advanceTimersByTimeAsync(1)
    expect(first.closed).toBe(true)
    expect(live.state).toBe("down")
    await vi.advanceTimersByTimeAsync(RETRY_MS[0])
    expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/events?topic=workers"])
    expect(FakeEventSource.instances).toHaveLength(2)
  })

  it("tab ẩn: đóng kết nối; hiện lại: mở ngay", async () => {
    live.subscribe({ topic: "workers" })
    await flush()
    FakeEventSource.last.accept()
    live.setHidden(true)
    expect(FakeEventSource.open).toEqual([])
    expect(live.state).toBe("idle")
    live.setHidden(false)
    expect(FakeEventSource.open.map((s) => s.url)).toEqual(["/api/events?topic=workers"])
    expect(live.state).toBe("connecting")
  })
})
