import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CheckCircle2, Cpu, Globe2, MonitorPlay, Pause, Play, Plus } from "lucide-react"
import { useState, type ReactNode } from "react"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  Hint,
  PageHeader,
  PageLoading,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  Tag,
} from "@/components/common"
import { JoinKeyDialog } from "@/components/join-key-dialog"
import { useLiveWorkers } from "@/components/live"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { drainWorker, getGetWorkersQueryKey, undrainWorker } from "@/lib/gen/queries"
import { ago, bytes, cap, dec, dt, num, pct } from "@/lib/format"
import type { WorkerStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

/** "Lần cuối báo" theo giờ máy chủ (không lệch nếu đồng hồ máy người xem sai). */
function seenAgo(worker: WorkerStatus, nowMs: number): string {
  if (!worker.last_seen_ms) return "–"
  const at = new Date(Date.now() - Math.max(0, nowMs - worker.last_seen_ms))
  return cap(ago(at.toISOString()))
}

function WorkerBadge({ worker }: { worker: WorkerStatus }) {
  if (!worker.alive) return <StatusBadge value={null} tone="muted" text="Đã rời" />
  if (worker.draining) return <StatusBadge value={null} tone="warn" text="Đang dừng nhận việc" />
  const busy = worker.stats.http_inflight + worker.stats.render_inflight > 0
  return <StatusBadge value={null} tone="good" text={busy ? "Đang làm việc" : "Sẵn sàng"} pulse={busy} />
}

/** Slot đang dùng / tổng kèm thanh. */
function Slots({ used, total, className }: { used: number; total: number; className?: string }) {
  if (!total) return <span className="text-muted-foreground">–</span>
  return (
    <div className={cn("min-w-24", className)}>
      <div className="text-meta tabular-nums">
        <span className="font-medium text-foreground">{num(used)}</span>
        <span className="text-muted-foreground"> / {num(total)}</span>
      </div>
      <Progress value={Math.min(100, (used / total) * 100)} className="mt-1 h-1.5" />
    </div>
  )
}

/** Slot render kèm trạng thái: tạm tắt sau lỗi Chromium (thử lại lúc …) hoặc tắt hẳn. */
function RenderSlots({ worker, nowMs }: { worker: WorkerStatus; nowMs: number }) {
  const { render_disabled_reason: reason, render_retry_at_ms: retryAt } = worker.stats
  return (
    <div className="space-y-0.5">
      <Slots used={worker.stats.render_inflight} total={worker.render_slots} />
      {reason && worker.alive && (
        <div className="text-meta whitespace-nowrap text-warning-ink" title={reason}>
          {retryAt > 0
            ? `Tạm tắt · thử lại ${retryAt > nowMs ? `sau ${num(Math.ceil((retryAt - nowMs) / 1000))} giây` : "ngay"}`
            : "Tắt (không mở được Chromium)"}
        </div>
      )}
    </div>
  )
}

/** Việc gateway trả lại backend (worker rời / quá hạn báo sống) và kết quả trả muộn bị bỏ. */
function HandBack({ worker }: { worker: WorkerStatus }) {
  if (!worker.lost && !worker.dropped) return null
  return (
    <div
      className="text-caption whitespace-nowrap text-muted-foreground"
      title="Trả lại: việc worker đang giữ được giao lại ngay khi worker rời hoặc quá hạn báo sống. Bỏ: kết quả worker gửi muộn cho việc đã giao lại."
    >
      {[worker.lost ? `trả lại ${num(worker.lost)}` : "", worker.dropped ? `bỏ ${num(worker.dropped)}` : ""]
        .filter(Boolean)
        .join(" · ")}
    </div>
  )
}

function Proxies({ worker }: { worker: WorkerStatus }) {
  if (!worker.local_proxies) return <span className="text-muted-foreground">Không có</span>
  return (
    <span className="tabular-nums">
      {num(worker.local_proxies)}
      {worker.alive && (
        <span className={cn("text-muted-foreground", worker.stats.proxies_alive < worker.local_proxies && "text-warning-ink")}>
          {" "}
          · {num(worker.stats.proxies_alive)} sống
        </span>
      )}
    </span>
  )
}

function done(w: WorkerStatus) {
  return w.stats.http_done + w.stats.render_done
}

function failed(w: WorkerStatus) {
  return w.stats.http_failed + w.stats.render_failed
}

function Resources({ worker }: { worker: WorkerStatus }) {
  const { cpu_percent: cpu, rss_bytes: rss } = worker.stats
  if (!worker.alive || (!cpu && !rss)) return <span className="text-muted-foreground">–</span>
  return (
    <span className="text-meta whitespace-nowrap tabular-nums">
      {cpu > 0 && <>CPU {dec(cpu)}%</>}
      {cpu > 0 && rss > 0 && " · "}
      {rss > 0 && <>RAM {bytes(rss)}</>}
    </span>
  )
}

function WorkerName({ worker }: { worker: WorkerStatus }) {
  const labels = Object.entries(worker.labels)
  return (
    <div className="min-w-0 space-y-1">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="truncate font-mono text-meta font-medium text-foreground" title={worker.worker_id}>
          {worker.worker_id}
        </span>
        {worker.version && <Tag title="Phiên bản worker">v{worker.version.replace(/^v/, "")}</Tag>}
      </div>
      <div className="flex flex-wrap items-center gap-1 text-meta text-muted-foreground">
        <span className="truncate">{worker.hostname || "Không rõ máy"}</span>
        {worker.started_at_ms > 0 && (
          <Hint content={`Khởi động ${dt(new Date(worker.started_at_ms).toISOString())}`}>
            <span>· chạy từ {ago(new Date(worker.started_at_ms).toISOString())}</span>
          </Hint>
        )}
      </div>
      {labels.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {labels.map(([k, v]) => (
            <span key={k} className="inline-flex h-5 items-center rounded-md bg-secondary px-1.5 font-mono text-caption text-secondary-foreground">
              {k}={v}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function DrainAction({
  worker,
  onToggle,
  busy,
  compact,
}: {
  worker: WorkerStatus
  onToggle: (w: WorkerStatus) => void
  busy: boolean
  /** Trong bảng: màn hình chưa đủ rộng thì chỉ hiện biểu tượng. */
  compact?: boolean
}) {
  const text = (label: string) => <span className={compact ? "max-2xl:sr-only" : undefined}>{label}</span>
  if (!worker.alive && !worker.draining) return null
  if (worker.draining)
    return (
      <ConfirmButton
        title={`Cho ${worker.worker_id} nhận việc lại?`}
        description="Worker tiếp tục nhận việc lấy trang mới như bình thường."
        confirm="Nhận việc lại"
        onConfirm={() => onToggle(worker)}
        size="xs"
        disabled={busy}
      >
        <Play /> {text("Nhận việc lại")}
      </ConfirmButton>
    )
  return (
    <ConfirmButton
      title={`Dừng nhận việc ở ${worker.worker_id}?`}
      description="Worker làm nốt các việc đang chạy rồi thôi nhận việc mới; các worker khác nhận phần việc còn lại. Có thể cho nhận việc lại bất cứ lúc nào."
      confirm="Dừng nhận việc"
      onConfirm={() => onToggle(worker)}
      size="xs"
      disabled={busy}
    >
      <Pause /> {text("Dừng nhận việc")}
    </ConfirmButton>
  )
}

function WorkerList({
  workers,
  nowMs,
  onToggle,
  busy,
}: {
  workers: WorkerStatus[]
  nowMs: number
  onToggle: (w: WorkerStatus) => void
  busy: boolean
}) {
  const showResources = workers.some((w) => w.alive && (w.stats.cpu_percent > 0 || w.stats.rss_bytes > 0))
  return (
    <>
      {/* Màn hình hẹp: mỗi worker một thẻ. */}
      <ul className="divide-y xl:hidden">
        {workers.map((w) => (
          <li key={w.worker_id} className={cn("space-y-3 px-4 py-3.5", !w.alive && "opacity-70")}>
            <div className="flex items-start justify-between gap-3">
              <WorkerName worker={w} />
              <WorkerBadge worker={w} />
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-2.5 text-meta">
              <div>
                <div className="mb-0.5 text-caption text-muted-foreground">Slot HTTP</div>
                <Slots used={w.stats.http_inflight} total={w.http_slots} />
              </div>
              <div>
                <div className="mb-0.5 text-caption text-muted-foreground">Slot render</div>
                <RenderSlots worker={w} nowMs={nowMs} />
              </div>
              <div>
                <div className="text-caption text-muted-foreground">Xong / lỗi</div>
                <span className="tabular-nums">
                  {num(done(w))} / <span className={failed(w) ? "text-danger-ink" : ""}>{num(failed(w))}</span>
                </span>
                <HandBack worker={w} />
              </div>
              <div>
                <div className="text-caption text-muted-foreground">Proxy riêng</div>
                <Proxies worker={w} />
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-meta text-muted-foreground">
              <span>
                Báo lần cuối {seenAgo(w, nowMs).toLocaleLowerCase("vi")}
                {showResources && w.alive && (
                  <>
                    {" · "}
                    <Resources worker={w} />
                  </>
                )}
              </span>
              <DrainAction worker={w} onToggle={onToggle} busy={busy} />
            </div>
          </li>
        ))}
      </ul>

      <Table className="max-xl:hidden">
        <TableHeader>
          <TableRow>
            <TableHead>Worker</TableHead>
            <TableHead>Trạng thái</TableHead>
            <TableHead>Slot HTTP</TableHead>
            <TableHead>Slot render</TableHead>
            <TableHead className="text-right">Xong / lỗi</TableHead>
            <TableHead>{showResources ? "Proxy · tài nguyên" : "Proxy riêng"}</TableHead>
            <TableHead className="text-right">
              <span className="sr-only">Thao tác</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {workers.map((w) => (
            <TableRow key={w.worker_id} className={cn(!w.alive && "text-muted-foreground")}>
              <TableCell className="max-w-64 min-w-44 whitespace-normal">
                <WorkerName worker={w} />
              </TableCell>
              <TableCell>
                <div className="space-y-1">
                  <WorkerBadge worker={w} />
                  <div className="text-meta whitespace-nowrap text-muted-foreground">Báo {seenAgo(w, nowMs).toLocaleLowerCase("vi")}</div>
                </div>
              </TableCell>
              <TableCell>
                <Slots used={w.stats.http_inflight} total={w.http_slots} />
              </TableCell>
              <TableCell>
                <RenderSlots worker={w} nowMs={nowMs} />
              </TableCell>
              <TableCell className="text-right text-meta tabular-nums">
                {num(done(w))} / <span className={failed(w) ? "text-danger-ink" : ""}>{num(failed(w))}</span>
                <HandBack worker={w} />
              </TableCell>
              <TableCell className="text-meta">
                <div className="space-y-0.5">
                  <Proxies worker={w} />
                  {showResources && (
                    <div className="text-muted-foreground">
                      <Resources worker={w} />
                    </div>
                  )}
                </div>
              </TableCell>
              <TableCell className="text-right">
                <DrainAction worker={w} onToggle={onToggle} busy={busy} compact />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  )
}

function SlotHint({ used, total }: { used: number; total: number }): ReactNode {
  if (!total) return "Chưa có slot nào"
  return (
    <span className="block space-y-1.5">
      <Progress value={Math.min(100, (used / total) * 100)} className="h-1.5" />
      <span className="block">{pct(used / total, 0)} đang dùng</span>
    </span>
  )
}

export function WorkersPage() {
  const client = useQueryClient()
  const { query } = useLiveWorkers()
  const drain = useMutation({
    mutationFn: (w: WorkerStatus) =>
      // orval không mã hoá tham số path; id worker do worker tự đặt nên mã hoá ở đây.
      w.draining ? undrainWorker(encodeURIComponent(w.worker_id)) : drainWorker(encodeURIComponent(w.worker_id)),
    onSuccess: (r, w) => {
      toast.success(r.draining ? `${w.worker_id} sẽ dừng nhận việc sau khi làm nốt việc đang chạy` : `${w.worker_id} nhận việc lại`)
      client.invalidateQueries({ queryKey: getGetWorkersQueryKey() })
    },
  })

  const [joining, setJoining] = useState(false)
  const d = query.data
  const s = d?.summary
  const pools = d ? [...new Set(d.workers.map((w) => w.pool))] : []
  return (
    <>
      <PageHeader
        title="Workers lấy trang"
        description="Các máy lấy trang (HTTP và trình duyệt) đang kết nối với backend."
        actions={
          d?.auth_enabled && (
            <Button onClick={() => setJoining(true)}>
              <Plus /> Thêm worker
            </Button>
          )
        }
      >
        {d && (
          <StatusBadge
            value={null}
            tone={d.auth_enabled ? "good" : "warn"}
            text={d.auth_enabled ? "Worker cần khoá để nối vào" : "Gateway không hỏi khoá: worker nào cũng nối vào được"}
          />
        )}
      </PageHeader>
      <JoinKeyDialog open={joining} onOpenChange={setJoining} pools={pools} />
      {query.isError && !d && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      {d && s && (
        <div className="space-y-6">
          <StatGrid className="grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Worker đang sống"
              value={num(s.alive)}
              hint={[
                s.gone ? `${num(s.gone)} vừa rời` : null,
                s.draining ? `${num(s.draining)} đang dừng nhận việc` : null,
                Object.keys(s.pools).length > 1 ? `${num(Object.keys(s.pools).length)} pool` : null,
              ]
                .filter(Boolean)
                .join(" · ") || (s.alive ? "Tất cả đang nhận việc" : "Chưa có worker nào")}
              status={s.alive ? { tone: "good", text: "Đang chạy" } : { tone: "warn", text: "Không có" }}
            />
            <StatCard
              label="Slot HTTP đang dùng"
              icon={<Globe2 />}
              value={`${num(s.http_inflight)} / ${num(s.http_slots)}`}
              hint={<SlotHint used={s.http_inflight} total={s.http_slots} />}
            />
            <StatCard
              label="Slot render đang dùng"
              icon={<MonitorPlay />}
              value={`${num(s.render_inflight)} / ${num(s.render_slots)}`}
              hint={<SlotHint used={s.render_inflight} total={s.render_slots} />}
            />
            <StatCard
              label="Việc xong / lỗi"
              icon={<CheckCircle2 />}
              value={
                <>
                  {num(s.done)} <span className="text-muted-foreground">/</span>{" "}
                  <span className={s.failed ? "text-danger-ink" : ""}>{num(s.failed)}</span>
                </>
              }
              hint={s.done + s.failed ? `Tỉ lệ lỗi ${pct(s.failed / (s.done + s.failed))} · từ khi worker khởi động` : "Chưa có việc nào"}
            />
          </StatGrid>

          {d.workers.length === 0 ? (
            <Section>
              <Empty icon={<Cpu />} title="Chưa có worker nào kết nối">
                Worker là máy nhận việc lấy trang (gửi request, mở trang bằng trình duyệt) từ backend, có thể chạy ở máy chủ khác
                và dùng proxy riêng. Khi có worker kết nối, chúng sẽ hiện ở đây.
              </Empty>
            </Section>
          ) : pools.length > 1 ? (
            pools.map((pool) => {
              const list = d.workers.filter((w) => w.pool === pool)
              const p = s.pools[pool]
              return (
                <Section
                  key={pool}
                  title={
                    <span className="inline-flex items-center gap-2">
                      Pool <span className="font-mono">{pool}</span>
                    </span>
                  }
                  description={
                    p
                      ? `${num(p.workers)} worker đang sống · HTTP ${num(p.http_inflight)}/${num(p.http_slots)} · render ${num(p.render_inflight)}/${num(p.render_slots)}`
                      : "Không còn worker nào sống"
                  }
                  bodyClassName="p-0"
                >
                  <WorkerList workers={list} nowMs={d.now_ms} onToggle={(w) => drain.mutate(w)} busy={drain.isPending} />
                </Section>
              )
            })
          ) : (
            <Section
              title="Danh sách worker"
              description={pools[0] && pools[0] !== "default" ? `Pool ${pools[0]}` : "Đang sống trước, rồi worker vừa rời"}
              bodyClassName="p-0"
            >
              <WorkerList workers={d.workers} nowMs={d.now_ms} onToggle={(w) => drain.mutate(w)} busy={drain.isPending} />
            </Section>
          )}
        </div>
      )}
    </>
  )
}
