// Cập nhật trực tiếp (SSE) cho các trang: provider giữ một kết nối mỗi tab (src/lib/live.ts), hook
// đăng ký chủ đề và tự quay về poll khi kết nối không mở.
import { useQueryClient } from "@tanstack/react-query"
import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react"

import { hour } from "@/lib/format"
import { useGetQueue, useGetRun, useGetWorkers, type GetQueueParams } from "@/lib/gen/queries"
import { LiveConnection, type LiveState, type LiveSub } from "@/lib/live"
import { cn } from "@/lib/utils"

/** Tab bị ẩn quá chừng này thì đóng kết nối SSE (giải phóng kết nối cho tab khác). */
export const HIDDEN_CLOSE_MS = 60_000
/** Lượt chạy vừa xong: theo dõi thêm chừng này để nhận nốt log ghi muộn (log vào DB mỗi ~1 giây). */
export const RUN_LINGER_MS = 10_000

const LiveContext = createContext<LiveConnection | null>(null)

/** Kết nối SSE của khung ứng dụng (trang đăng nhập không có). Không có EventSource thì các trang poll. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const [connection] = useState(() => (typeof EventSource === "undefined" ? null : new LiveConnection(client)))
  useEffect(() => {
    if (!connection) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const onVisibility = () => {
      clearTimeout(timer)
      if (document.visibilityState === "hidden") timer = setTimeout(() => connection.setHidden(true), HIDDEN_CLOSE_MS)
      else connection.setHidden(false)
    }
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      clearTimeout(timer)
      document.removeEventListener("visibilitychange", onVisibility)
      connection.stop()
    }
  }, [connection])
  return <LiveContext.Provider value={connection}>{children}</LiveContext.Provider>
}

const idle = () => () => undefined

/** Theo dõi `sub` (null: không theo dõi); trả trạng thái kết nối. `open` = dữ liệu tự tới, khỏi poll. */
export function useLive(sub: LiveSub | null): LiveState {
  const connection = useContext(LiveContext)
  const key = sub ? JSON.stringify(sub) : null
  useEffect(() => {
    if (!connection || !key) return
    return connection.subscribe(JSON.parse(key) as LiveSub)
  }, [connection, key])
  const state = useSyncExternalStore(connection ? connection.onState : idle, () => connection?.state ?? "idle")
  return key ? state : "idle"
}

/** Trang Workers: qua SSE; mất SSE thì poll 5 giây như trước. */
export function useLiveWorkers() {
  const state = useLive({ topic: "workers" })
  const live = state === "open"
  const query = useGetWorkers({ query: { refetchInterval: live ? false : 5_000, refetchOnWindowFocus: !live } })
  return { query, state }
}

/** Ba hàng đợi (trang Hàng đợi với `params`, thanh bên / Tổng quan không tham số): qua SSE; mất SSE
 * thì poll mỗi `pollMs`. */
export function useLiveQueue(params?: GetQueueParams, pollMs = 15_000) {
  const state = useLive(params ? { topic: "queue", params } : { topic: "queue" })
  const live = state === "open"
  const query = useGetQueue(params, {
    query: { refetchInterval: live ? false : pollMs, refetchOnWindowFocus: !live, placeholderData: (prev) => prev },
  })
  return { query, state }
}

/** Lượt chạy: theo dõi qua SSE khi đang chạy (và RUN_LINGER_MS sau khi xong); mất SSE thì lượt
 * đang chạy poll 5 giây. */
export function useLiveRun(id: string) {
  const [watch, setWatch] = useState(false)
  const state = useLive(watch ? { topic: "run", runId: id } : null)
  const live = state === "open"
  const query = useGetRun(id, {
    query: { refetchInterval: (q) => (q.state.data?.run.status === "running" && !live ? 5_000 : false) },
  })
  const running = query.data?.run.status === "running"
  useEffect(() => {
    if (running) {
      setWatch(true)
      return
    }
    const timer = setTimeout(() => setWatch(false), RUN_LINGER_MS)
    return () => clearTimeout(timer)
  }, [running])
  return { query, state: running ? state : "idle" }
}

/** Dòng trạng thái cập nhật: trực tiếp, đang nối lại, hoặc đang poll mỗi `pollSeconds` giây. */
export function LiveStatus({ state, pollSeconds, updatedAt }: { state: LiveState; pollSeconds: number; updatedAt?: number }) {
  const [text, title, dot] =
    state === "open"
      ? ["Cập nhật trực tiếp", "Máy chủ gửi dữ liệu mới ngay khi có thay đổi", "bg-success"]
      : state === "connecting"
        ? ["Đang kết nối…", "Đang mở kết nối cập nhật trực tiếp", "bg-muted-foreground/50"]
        : [`Tự làm mới mỗi ${pollSeconds} giây`, "Không có kết nối cập nhật trực tiếp: hỏi lại máy chủ định kỳ", "bg-warning"]
  return (
    <span className="inline-flex items-center gap-2 text-meta text-muted-foreground" title={title}>
      <span className="relative flex size-2" aria-hidden>
        {state === "open" && <span className="absolute inset-0 animate-ping rounded-full bg-success opacity-50" />}
        <span className={cn("relative size-2 rounded-full", dot)} />
      </span>
      {text}
      {updatedAt ? ` · ${hour(new Date(updatedAt).toISOString())}` : ""}
    </span>
  )
}
