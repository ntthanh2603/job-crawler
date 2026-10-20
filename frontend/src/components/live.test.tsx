import { QueryClientProvider } from "@tanstack/react-query"
import { act, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { RunDetailOut, WorkersPageOut } from "@/lib/gen/queries"
import { createQueryClient } from "@/lib/query-client"
import { FakeEventSource } from "@/test/fake-event-source"

import { LiveProvider, LiveStatus, RUN_LINGER_MS, useLiveRun, useLiveWorkers } from "./live"

const workers = (alive: number) =>
  ({ workers: [], summary: { alive }, now_ms: 1, auth_enabled: true }) as unknown as WorkersPageOut
const run = (status: string, logs: number) =>
  ({ run: { run_id: "r-1", status }, logs_total: logs, fetches_total: 0 }) as unknown as RunDetailOut

let fetchMock: ReturnType<typeof vi.fn<typeof globalThis.fetch>>

/** Máy chủ trả `body(url)` cho mọi GET. */
function serve(body: (url: string) => unknown) {
  fetchMock = vi.fn<typeof globalThis.fetch>(
    async (input) => new Response(JSON.stringify(body(String(input))), { headers: { "Content-Type": "application/json" } }),
  )
  vi.stubGlobal("fetch", fetchMock)
}

function renderLive(ui: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <LiveProvider>{ui}</LiveProvider>
    </QueryClientProvider>,
  )
}

/** Cho timer giả chạy tới `ms` và để React cập nhật. */
const tick = (ms = 0) => act(() => vi.advanceTimersByTimeAsync(ms))

/** Vài nhịp ngắn liền nhau: fetch xong → dữ liệu vào cache → React render → effect đăng ký → mở
 * kết nối (mỗi bước chờ một timer). */
async function settle() {
  for (let i = 0; i < 5; i++) await tick(10)
}

/** Máy chủ gửi một sự kiện; React Query báo thay đổi ở nhịp sau. */
async function push(source: FakeEventSource, msg: object) {
  await act(async () => source.emit({ workers: null, queue: null, run: null, ...msg }))
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.reset()
  vi.stubGlobal("EventSource", FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function WorkersProbe() {
  const { query, state } = useLiveWorkers()
  return (
    <>
      <LiveStatus state={state} pollSeconds={5} />
      <p>sống: {query.data?.summary.alive ?? "–"}</p>
    </>
  )
}

describe("useLiveWorkers", () => {
  it("SSE mở: dữ liệu tới từ sự kiện, thôi poll; SSE hỏng: poll 5 giây như trước", async () => {
    serve(() => workers(1))
    renderLive(<WorkersProbe />)
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(1) // lần tải đầu vẫn qua API JSON
    expect(screen.getByText("sống: 1")).toBeTruthy()
    expect(FakeEventSource.last.url).toBe("/api/events?topic=workers")
    expect(screen.getByText("Đang kết nối…")).toBeTruthy()

    await act(async () => FakeEventSource.last.accept())
    expect(screen.getByText("Cập nhật trực tiếp")).toBeTruthy()
    await push(FakeEventSource.last, { topic: "workers", workers: workers(7) })
    expect(screen.getByText("sống: 7")).toBeTruthy()
    await tick(20_000)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Lỗi HTTP (vd. backend đang khởi động lại): quay về poll tới khi nối lại được.
    await act(async () => FakeEventSource.last.fail())
    expect(screen.getByText("Tự làm mới mỗi 5 giây")).toBeTruthy()
    await tick(5_000)
    await settle()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(screen.getByText("sống: 1")).toBeTruthy()
  })

  it("không có EventSource (trình duyệt cũ): chỉ poll", async () => {
    vi.stubGlobal("EventSource", undefined)
    serve(() => workers(2))
    renderLive(<WorkersProbe />)
    await settle()
    expect(screen.getByText("Tự làm mới mỗi 5 giây")).toBeTruthy()
    await tick(5_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

function RunProbe() {
  const { query, state } = useLiveRun("r-1")
  return (
    <p>
      {query.data?.run.status ?? "–"} · {query.data?.logs_total ?? "–"} · {state}
    </p>
  )
}

describe("useLiveRun", () => {
  it("theo dõi lượt đang chạy, thêm một lúc sau khi xong rồi thôi", async () => {
    serve(() => run("running", 1))
    renderLive(<RunProbe />)
    await settle()
    expect(FakeEventSource.last.url).toBe("/api/events?topic=run&run_id=r-1")
    const source = FakeEventSource.last
    await act(async () => source.accept())
    expect(screen.getByText("running · 1 · open")).toBeTruthy()
    await push(source, { topic: "run", run: run("success", 4) })
    expect(screen.getByText("success · 4 · idle")).toBeTruthy()
    // Log ghi muộn của lượt vừa xong vẫn tới trong RUN_LINGER_MS.
    await tick(RUN_LINGER_MS - 500)
    expect(source.closed).toBe(false)
    await push(source, { topic: "run", run: run("success", 6) })
    expect(screen.getByText("success · 6 · idle")).toBeTruthy()
    await tick(500)
    await settle()
    expect(source.closed).toBe(true)
    expect(FakeEventSource.open).toEqual([])
  })

  it("lượt đã xong: không mở luồng", async () => {
    serve(() => run("failed", 2))
    renderLive(<RunProbe />)
    await settle()
    await tick(RUN_LINGER_MS)
    expect(screen.getByText("failed · 2 · idle")).toBeTruthy()
    expect(FakeEventSource.instances).toEqual([])
  })
})
