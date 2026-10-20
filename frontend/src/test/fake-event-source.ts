// EventSource giả cho test (jsdom không có): ghi lại mọi kết nối, test tự mở / gửi sự kiện / báo lỗi.
export class FakeEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static instances: FakeEventSource[] = []

  readyState = FakeEventSource.CONNECTING
  closed = false
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent<string>) => void) | null = null
  onerror: ((e: Event) => void) | null = null

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  static get last(): FakeEventSource {
    const last = FakeEventSource.instances.at(-1)
    if (!last) throw new Error("chưa mở EventSource nào")
    return last
  }

  /** Các kết nối chưa đóng. */
  static get open(): FakeEventSource[] {
    return FakeEventSource.instances.filter((s) => !s.closed)
  }

  static reset() {
    FakeEventSource.instances = []
  }

  close() {
    this.closed = true
    this.readyState = FakeEventSource.CLOSED
  }

  /** Máy chủ trả 200 text/event-stream. */
  accept() {
    this.readyState = FakeEventSource.OPEN
    this.onopen?.(new Event("open"))
  }

  /** Một sự kiện `data` (JSON). */
  emit(data: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }))
  }

  /** Mất kết nối: `retry` = trình duyệt tự nối lại; không thì hỏng hẳn (lỗi HTTP). */
  fail({ retry = false } = {}) {
    this.readyState = retry ? FakeEventSource.CONNECTING : FakeEventSource.CLOSED
    this.onerror?.(new Event("error"))
  }
}
