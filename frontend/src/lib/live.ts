// Cập nhật trực tiếp qua Server-Sent Events (`GET /api/events`, backend/jobcrawler/modules/live/live.py).
//
// Một EventSource mỗi tab mang mọi chủ đề mà các trang đang mở cần (trình duyệt chỉ mở 6 kết nối
// HTTP/1.1 tới một origin). Mỗi sự kiện là một `LiveOut`, ghi thẳng vào cache React Query đúng khoá
// của hook orval tương ứng (`useGetWorkers`, `useGetQueue`, `useGetRun`): trang vẫn đọc dữ liệu bằng
// hook đó, chỉ thôi poll khi kết nối đang mở. Mất mạng thì trình duyệt tự nối lại (kèm
// `Last-Event-ID`); kết nối hỏng hẳn (401, 5xx, máy chủ dừng) thì thử lại sau vài giây, trong lúc đó
// các trang poll như trước.
import type { QueryClient } from "@tanstack/react-query"

import {
  getGetQueueQueryKey,
  getGetRunFetchesQueryKey,
  getGetRunLogsQueryKey,
  getGetRunQueryKey,
  getGetWorkersQueryKey,
  type GetQueueParams,
  type LiveOut,
  type RunDetailOut,
} from "@/lib/gen/queries"

export const EVENTS_URL = "/api/events"

/** Một đăng ký: chủ đề kèm tham số (queue: trang của ba hàng đợi; run: id lượt chạy). */
export type LiveSub = { topic: "workers" } | { topic: "queue"; params?: GetQueueParams } | { topic: "run"; runId: string }

/** idle: không có gì để theo dõi; connecting: đang mở / tự nối lại; open: đang nhận; down: hỏng, chờ thử lại. */
export type LiveState = "idle" | "connecting" | "open" | "down"

/** Kết nối hỏng hẳn: thử lại sau chừng này ms (lần thứ 1, 2, …; hết danh sách thì giữ số cuối). */
export const RETRY_MS = [2_000, 4_000, 8_000, 15_000, 30_000]

/** Máy chủ gửi lại dữ liệu của mỗi chủ đề ít nhất mỗi 30 giây (`REFRESH_SECONDS`); im quá chừng này
 * thì coi kết nối đã chết dù trình duyệt chưa báo lỗi (proxy giữ kết nối nửa vời, máy ngủ dậy…).
 * Comment `: ping` của máy chủ không tới được JS nên không dùng để đo. */
export const STALE_MS = 45_000

/** `EventSource.CLOSED` (jsdom không có EventSource). */
const CLOSED = 2

const TOPICS = ["workers", "queue", "run"] as const

/** Tham số queue / run của các đăng ký: trang Hàng đợi (có tham số trang) thắng thanh bên. */
function targets(subs: Iterable<LiveSub>) {
  const topics = new Set<LiveSub["topic"]>()
  let queue: GetQueueParams | undefined
  let runId: string | undefined
  for (const s of subs) {
    topics.add(s.topic)
    if (s.topic === "queue" && s.params && !queue) queue = s.params
    if (s.topic === "run" && !runId) runId = s.runId
  }
  return { topics, queue, runId }
}

/** URL của luồng cho các đăng ký; null khi không có gì để theo dõi. */
export function eventsUrl(subs: Iterable<LiveSub>): string | null {
  const { topics, queue, runId } = targets(subs)
  if (!topics.size) return null
  const search = new URLSearchParams()
  for (const t of TOPICS) if (topics.has(t)) search.append("topic", t)
  for (const [key, value] of Object.entries(queue ?? {})) if (value !== undefined && value !== null) search.set(key, String(value))
  if (runId) search.set("run_id", runId)
  return `${EVENTS_URL}?${search}`
}

export type OpenEventSource = (url: string) => EventSource

/** Kết nối SSE của một tab: gộp đăng ký của các trang, ghi sự kiện vào cache React Query. */
export class LiveConnection {
  state: LiveState = "idle"
  private subs = new Map<number, LiveSub>()
  private nextId = 1
  private source: EventSource | null = null
  private url: string | null = null
  private target: ReturnType<typeof targets> = targets([])
  private failures = 0
  private hidden = false
  private retryTimer: ReturnType<typeof setTimeout> | undefined
  private syncTimer: ReturnType<typeof setTimeout> | undefined
  private staleTimer: ReturnType<typeof setTimeout> | undefined
  private listeners = new Set<() => void>()

  constructor(
    private client: QueryClient,
    private open: OpenEventSource = (url) => new EventSource(url),
  ) {}

  /** Theo dõi một chủ đề; trả hàm huỷ. Các đăng ký đổi trong cùng một lượt render mở lại kết nối một lần. */
  subscribe(sub: LiveSub): () => void {
    const id = this.nextId++
    this.subs.set(id, sub)
    this.schedule()
    return () => {
      this.subs.delete(id)
      this.schedule()
    }
  }

  /** Cho `useSyncExternalStore`. */
  onState = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** Tab bị ẩn lâu: đóng kết nối; hiện lại: mở ngay (máy chủ gửi bản mới nhất khi mở). */
  setHidden(hidden: boolean) {
    this.hidden = hidden
    this.sync()
  }

  /** Đóng kết nối (khung ứng dụng rời đi); đăng ký mới sau đó mở lại. */
  stop() {
    clearTimeout(this.syncTimer)
    this.syncTimer = undefined
    this.disconnect()
    this.url = null
    this.setState("idle")
  }

  private schedule() {
    if (this.syncTimer !== undefined) return
    this.syncTimer = setTimeout(() => {
      this.syncTimer = undefined
      this.sync()
    }, 0)
  }

  private sync() {
    const url = this.hidden ? null : eventsUrl(this.subs.values())
    if (url === this.url && (this.source || this.retryTimer !== undefined)) return
    this.disconnect()
    this.url = url
    this.failures = 0
    if (url) this.connect(url)
    else this.setState("idle")
  }

  private connect(url: string) {
    this.target = targets(this.subs.values())
    const source = this.open(url)
    this.source = source
    this.setState("connecting")
    this.watch(source)
    source.onopen = () => {
      this.failures = 0
      this.watch(source)
      this.setState("open")
    }
    source.onmessage = (e: MessageEvent<string>) => {
      this.watch(source)
      this.receive(e.data)
    }
    source.onerror = () => {
      if (this.source !== source) return
      if (source.readyState !== CLOSED) {
        this.setState("connecting") // trình duyệt tự nối lại sau `retry` của máy chủ
        return
      }
      // Lỗi HTTP (401, 5xx…) hoặc không phải text/event-stream: trình duyệt không tự nối lại.
      this.fail()
    }
  }

  /** Hẹn giờ coi kết nối đã chết nếu không có sự kiện nào trong STALE_MS. */
  private watch(source: EventSource) {
    clearTimeout(this.staleTimer)
    this.staleTimer = setTimeout(() => {
      if (this.source === source) this.fail()
    }, STALE_MS)
  }

  /** Đóng kết nối hỏng, các trang poll trong lúc chờ thử lại (giãn dần). */
  private fail() {
    this.disconnect()
    this.setState("down")
    const delay = RETRY_MS[Math.min(this.failures++, RETRY_MS.length - 1)]
    this.retryTimer = setTimeout(() => {
      this.retryTimer = undefined
      if (this.url) this.connect(this.url)
    }, delay)
  }

  private disconnect() {
    clearTimeout(this.retryTimer)
    clearTimeout(this.staleTimer)
    this.retryTimer = this.staleTimer = undefined
    if (this.source) {
      this.source.onopen = this.source.onmessage = this.source.onerror = null
      this.source.close()
      this.source = null
    }
  }

  private setState(state: LiveState) {
    if (state === this.state) return
    this.state = state
    for (const listener of this.listeners) listener()
  }

  private receive(data: string) {
    let msg: LiveOut
    try {
      msg = JSON.parse(data) as LiveOut
    } catch {
      return
    }
    const client = this.client
    if (msg.topic === "workers" && msg.workers) {
      client.setQueryData(getGetWorkersQueryKey(), msg.workers)
    } else if (msg.topic === "queue" && msg.queue) {
      // Số đếm của ba hàng đợi không phụ thuộc trang: thanh bên và Tổng quan (khoá không tham số)
      // dùng chung bản của trang Hàng đợi.
      client.setQueryData(getGetQueueQueryKey(), msg.queue)
      if (this.target.queue) client.setQueryData(getGetQueueQueryKey(this.target.queue), msg.queue)
    } else if (msg.topic === "run" && msg.run && this.target.runId) {
      const id = this.target.runId
      const key = getGetRunQueryKey(id)
      const prev = client.getQueryData<RunDetailOut>(key)
      client.setQueryData(key, msg.run)
      // Log / request được ghi thêm: tải lại các trang danh sách đang mở của lượt chạy.
      if (prev && prev.logs_total !== msg.run.logs_total) void client.invalidateQueries({ queryKey: getGetRunLogsQueryKey(id) })
      if (prev && prev.fetches_total !== msg.run.fetches_total)
        void client.invalidateQueries({ queryKey: getGetRunFetchesQueryKey(id) })
    }
  }
}
