// Trang tổng quan: độ trễ phát hiện tin mới (cửa sổ phát hiện của các lượt poll, độ tươi, khung giờ).
import { Clock3, Moon, Sun, Timer } from "lucide-react"
import type { ReactNode } from "react"
import { Link } from "react-router"

import { SCHEDULE_ORDER, SCHEDULE_STATES, secondsText } from "@/components/crawl"
import { Dot, ErrorState, InfoTip, Loading, Section, StatusBadge, type Tone } from "@/components/common"
import { dec, hour, num, pct } from "@/lib/format"
import { useGetCrawlStatus } from "@/lib/gen/queries"
import type { CrawlStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

const RANGES = [
  [15, "15 phút"],
  [60, "1 giờ"],
  [360, "6 giờ"],
] as const

// Ngưỡng (giây) của histogram cửa sổ phát hiện (modules/crawl/sla.py WINDOW_EDGES); ô cuối là > 1 giờ.
const EDGES = [30, 60, 90, 120, 135, 150, 165, 180, 210, 240, 300, 600, 1800, 3600]
const TICKS = ["30s", "1p", "1p30", "2p", "2p15", "2p30", "2p45", "3p", "3p30", "4p", "5p", "10p", "30p", "1h", ">1h"]
// Nhãn trục luôn hiện; các nhãn khác chỉ hiện khi khung đủ rộng (còn lại xem khi rê / chạm).
const KEY_TICKS = new Set([1, 3, 7, 10, 13])

function rangeText(minutes: number) {
  return minutes < 60 ? `${minutes} phút qua` : `${minutes / 60} giờ qua`
}

/** Phân vị lấy từ histogram là ngưỡng trên của ô: "≤ 2,5 phút"; ô cuối "> 1 giờ". */
function bound(value: number | null, max: number) {
  if (value === null) return "–"
  if (value > max) return `> ${secondsText(max)}`
  return `≤ ${secondsText(value)}`
}

/** Giờ theo múi của lịch thu thập: "00:00", "00:00 ngày mai", "00:00 ngày 21/10". */
function clock(iso: string, timezone: string) {
  try {
    const time = new Intl.DateTimeFormat("vi-VN", { timeZone: timezone, hour: "2-digit", minute: "2-digit" })
    const day = new Intl.DateTimeFormat("vi-VN", { timeZone: timezone, day: "2-digit", month: "2-digit" })
    const at = new Date(iso)
    const today = day.format(new Date())
    const tomorrow = day.format(new Date(Date.now() + 86_400_000))
    const d = day.format(at)
    return `${time.format(at)}${d === today ? "" : d === tomorrow ? " ngày mai" : ` ngày ${d}`}`
  } catch {
    return hour(iso)
  }
}

const windowText = (w: string) => w.replace(/\s*-\s*/, "–")

function Metric({
  label,
  value,
  sub,
  tone,
  hint,
  className,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: Tone
  hint?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("min-w-0 rounded-lg bg-muted/50 px-4 py-3", className)}>
      <div className="flex items-center gap-1 text-meta text-muted-foreground">
        {label}
        {hint && <InfoTip>{hint}</InfoTip>}
      </div>
      <div
        className={cn(
          "mt-0.5 text-lg font-semibold tabular-nums",
          tone === "good" && "text-success-ink",
          tone === "bad" && "text-danger-ink",
          tone === "warn" && "text-warning-ink",
        )}
      >
        {value}
      </div>
      {sub && <div className="text-meta text-muted-foreground">{sub}</div>}
    </div>
  )
}

function binRange(i: number) {
  if (i >= EDGES.length) return `Trên ${secondsText(EDGES[EDGES.length - 1])}`
  const from = i ? secondsText(EDGES[i - 1]) : "0"
  return `${from} – ${secondsText(EDGES[i])}`
}

/** Histogram cửa sổ phát hiện, vạch mục tiêu 3 phút. */
function WindowHistogram({ counts, target }: { counts: number[]; target: number }) {
  const total = counts.reduce((n, c) => n + c, 0)
  if (!total)
    return (
      <p className="flex h-36 items-center justify-center rounded-lg bg-muted/40 px-4 text-center text-meta text-muted-foreground">
        Chưa có lượt poll đủ nào để đo cửa sổ phát hiện (cần hai lượt poll thành công liên tiếp).
      </p>
    )
  const max = Math.max(...counts, 1)
  const targetBin = EDGES.findIndex((e) => e >= target)
  const marker = ((targetBin + 1) / counts.length) * 100
  return (
    <figure className="@container space-y-2">
      <div className="relative">
        <div className="flex h-40 items-end gap-0.5 border-b border-border pt-5" role="img" aria-label="Phân bố cửa sổ phát hiện">
          {counts.map((c, i) => {
            const over = i > targetBin
            return (
              <div
                key={i}
                className="group flex h-full min-w-0 flex-1 flex-col items-center justify-end"
                title={`${binRange(i)}: ${num(c)} lượt (${pct(c / total, 0)})`}
              >
                <div
                  className="w-full max-w-6 rounded-t-[4px] transition-opacity group-hover:opacity-80"
                  style={{
                    height: c ? `max(2px, ${(c / max) * 100}%)` : 0,
                    background: over ? "var(--viz-failed)" : "var(--viz-1)",
                  }}
                />
              </div>
            )
          })}
        </div>
        {/* Vạch mục tiêu. */}
        <div className="pointer-events-none absolute inset-y-0 border-l border-dashed border-foreground/55" style={{ left: `${marker}%` }}>
          <span className="absolute -top-0.5 left-1 text-caption font-medium whitespace-nowrap text-foreground">
            Mục tiêu {secondsText(target)}
          </span>
        </div>
      </div>
      <div className="flex gap-0.5">
        {/* Nhãn là ngưỡng trên của ô: đặt ở mép phải ô (trùng vạch mục tiêu); ô cuối ở giữa. */}
        {TICKS.slice(0, counts.length).map((t, i) => (
          <span key={t} className="relative h-4 min-w-0 flex-1">
            <span
              className={cn(
                "absolute top-0 text-caption whitespace-nowrap text-muted-foreground tabular-nums",
                i < counts.length - 1 ? "right-0 translate-x-1/2" : "left-1/2 -translate-x-1/2",
                !KEY_TICKS.has(i) && "@max-xl:invisible",
              )}
            >
              {t}
            </span>
          </span>
        ))}
      </div>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-meta text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: "var(--viz-1)" }} aria-hidden /> Trong mục tiêu
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm" style={{ background: "var(--viz-failed)" }} aria-hidden /> Vượt mục tiêu
        </span>
        <span>p = phút · h = giờ</span>
      </figcaption>
    </figure>
  )
}

function RangeSwitch({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div role="radiogroup" aria-label="Khoảng thời gian" className="flex rounded-lg bg-muted p-0.5">
      {RANGES.map(([v, text]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className={cn(
            "h-8 rounded-md px-3 text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
            value === v && "bg-card font-medium text-foreground shadow-card",
          )}
        >
          {text}
        </button>
      ))}
    </div>
  )
}

function WindowLine({ w }: { w: CrawlStatus["windows"] }) {
  const Icon = w.in_poll ? Sun : w.in_build ? Moon : Clock3
  const text = w.in_poll
    ? `Đang trong khung poll ${windowText(w.poll)} · build đêm bắt đầu lúc ${clock(w.next_build_start, w.timezone)}`
    : w.in_build
      ? `Đang trong khung build đêm ${windowText(w.build)} · poll bắt đầu lúc ${clock(w.next_poll_start, w.timezone)}`
      : `Ngoài khung build và poll · poll bắt đầu lúc ${clock(w.next_poll_start, w.timezone)}`
  return (
    <div className="flex items-start gap-2 text-sm">
      <Icon className="mt-0.5 size-4 shrink-0 stroke-[1.75] text-brand" />
      <span>
        {text}
        <span className="text-muted-foreground"> · giờ {w.timezone}</span>
      </span>
    </div>
  )
}

function NoPolls({ data, minutes }: { data: CrawlStatus; minutes: number }) {
  const w = data.windows
  let message: ReactNode
  if (!w.in_poll)
    message = `Đang ngoài khung poll nên không có lượt poll nào trong ${rangeText(minutes)}. Poll bắt đầu lại lúc ${clock(w.next_poll_start, w.timezone)}.`
  else if (!data.freshness.ready)
    message =
      "Chưa có doanh nghiệp nào có bộ khung để poll. Bộ khung của mỗi doanh nghiệp được tạo ở lượt build đầu tiên (trong khung build đêm hoặc ngay khi thêm doanh nghiệp)."
  else
    message = (
      <>
        Chưa có lượt poll nào xong trong {rangeText(minutes)} dù đang trong khung poll. Xem tình trạng các runner ở trang{" "}
        <Link to="/queue" className="text-brand underline-offset-2 hover:underline">
          Hàng đợi
        </Link>
        .
      </>
    )
  return (
    <div className="flex items-start gap-3 rounded-lg border border-dashed bg-muted/30 p-4">
      <Timer className="mt-0.5 size-5 shrink-0 stroke-[1.75] text-muted-foreground" />
      <div className="space-y-0.5">
        <div className="text-sm font-medium">Chưa có số đo độ trễ</div>
        <p className="text-meta text-muted-foreground">{message}</p>
      </div>
    </div>
  )
}

function Freshness({ data }: { data: CrawlStatus }) {
  const f = data.freshness
  const total = SCHEDULE_ORDER.reduce((n, s) => n + (f.by_state[s] ?? 0), 0)
  const staleTone: Tone | undefined =
    data.windows.in_poll && f.age_p95 !== null && f.age_p95 > data.target_seconds ? "warn" : undefined
  return (
    <div className="grid gap-5 border-t pt-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
      <div className="min-w-0">
        <div className="mb-2 flex items-center gap-1 text-meta font-medium text-muted-foreground">
          Doanh nghiệp theo trạng thái lịch
          <span className="font-normal tabular-nums">· {num(total)}</span>
        </div>
        <ul className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-1">
          {SCHEDULE_ORDER.map((s) => (
            <li key={s} className="flex items-center justify-between gap-3 text-sm" title={SCHEDULE_STATES[s].help}>
              <Dot tone={SCHEDULE_STATES[s].tone}>{SCHEDULE_STATES[s].label}</Dot>
              <span className={cn("font-medium tabular-nums", !f.by_state[s] && "text-muted-foreground")}>
                {num(f.by_state[s] ?? 0)}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div className="grid min-w-0 gap-3 sm:grid-cols-3">
        <Metric
          label="Độ tươi P95"
          hint="Tuổi của lượt poll đủ gần nhất (mọi nguồn đọc được), tính trên các doanh nghiệp đang poll. Ngoài khung poll con số này tăng dần là bình thường."
          value={secondsText(f.age_p95)}
          sub={`P50 ${secondsText(f.age_p50)} · cũ nhất ${secondsText(f.age_max)}`}
          tone={staleTone}
        />
        <Metric
          label="Quá hạn poll"
          hint="Doanh nghiệp đang poll mà lịch poll đã trễ hơn 1 phút."
          value={num(f.overdue)}
          sub={f.ready ? `Trên ${num(f.ready)} doanh nghiệp đang poll` : "Chưa có doanh nghiệp nào đang poll"}
          tone={f.overdue ? "warn" : undefined}
        />
        <Metric
          label="Chưa poll đủ lần nào"
          value={num(f.never_polled)}
          sub="Có bộ khung nhưng chưa có lượt poll đọc được mọi nguồn"
        />
      </div>
    </div>
  )
}

/** Khung "Độ trễ phát hiện tin mới" của trang tổng quan. */
export function DetectionSection({ minutes, onMinutes }: { minutes: number; onMinutes: (v: number) => void }) {
  const query = useGetCrawlStatus({ minutes }, { query: { refetchInterval: 30_000, placeholderData: (prev) => prev } })
  const d = query.data
  const p = d?.polls
  const target = d?.target_seconds ?? 180
  const met = p?.window_p95 === null || p?.window_p95 === undefined ? null : p.window_p95 <= target
  const withinTone: Tone | undefined =
    p?.within_target === null || p?.within_target === undefined ? undefined : p.within_target >= 0.95 ? "good" : "bad"
  return (
    <Section
      title="Độ trễ phát hiện tin mới"
      description="Cửa sổ phát hiện: thời gian xấu nhất từ lúc một tin được đăng tới lúc hệ thống thấy nó (từ lúc bắt đầu lượt poll trước tới lúc lượt poll sau xong)."
      actions={<RangeSwitch value={minutes} onChange={onMinutes} />}
    >
      {query.isError && !d && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {!d && !query.isError && <Loading rows={3} />}
      {d && p && (
        <div className={cn("space-y-5 transition-opacity", query.isPlaceholderData && "opacity-60")}>
          <WindowLine w={d.windows} />
          {p.polls === 0 ? (
            <NoPolls data={d} minutes={minutes} />
          ) : (
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div className="min-w-0 space-y-3">
                <div
                  className={cn(
                    "rounded-xl border p-4 sm:px-5",
                    met === true && "border-success/30 bg-success/6",
                    met === false && "border-destructive/30 bg-destructive/5",
                  )}
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <span className="text-meta font-medium text-muted-foreground">P95 cửa sổ phát hiện</span>
                    {met !== null && (
                      <StatusBadge
                        value={null}
                        tone={met ? "good" : "bad"}
                        text={met ? `Đạt mục tiêu ${secondsText(target)}` : `Vượt mục tiêu ${secondsText(target)}`}
                      />
                    )}
                  </div>
                  <div
                    className={cn(
                      "mt-1 text-stat font-semibold tabular-nums",
                      met === true && "text-success-ink",
                      met === false && "text-danger-ink",
                    )}
                  >
                    {bound(p.window_p95, EDGES[EDGES.length - 1])}
                  </div>
                  <div className="mt-0.5 text-meta text-muted-foreground">
                    P50 {bound(p.window_p50, EDGES[EDGES.length - 1])} · chu kỳ poll {secondsText(d.poll_interval_seconds)} ·{" "}
                    {num(p.polls)} lượt poll trong {rangeText(minutes)}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Metric
                    label="Lượt poll trong mục tiêu"
                    value={p.within_target === null ? "–" : pct(p.within_target)}
                    sub={`Cửa sổ ≤ ${secondsText(target)}`}
                    tone={withinTone}
                  />
                  <Metric
                    label="Lượt poll mỗi giây"
                    value={dec(p.per_second, 2)}
                    sub={
                      <span className="flex flex-wrap gap-x-2">
                        <span>{num(p.ok)} đủ</span>
                        {p.failed > 0 && <span className="text-danger-ink">{num(p.failed)} lỗi</span>}
                        {p.broken > 0 && <span className="text-danger-ink">{num(p.broken)} bộ khung hỏng</span>}
                        {p.skipped > 0 && <span>{num(p.skipped)} bỏ qua</span>}
                      </span>
                    }
                  />
                  <Metric
                    label="Thời gian một lượt poll"
                    value={`P95 ${bound(p.duration_p95, 90)}`}
                    sub={`P50 ${bound(p.duration_p50, 90)}`}
                  />
                  <Metric
                    label="Tin mới tìm được"
                    value={num(p.new_jobs)}
                    sub={`${num(p.changed)} lượt poll thấy thay đổi`}
                    tone={p.new_jobs ? "good" : undefined}
                  />
                </div>
              </div>
              <div className="min-w-0 rounded-xl border p-4 sm:px-5">
                <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-sm font-medium">Phân bố cửa sổ phát hiện</span>
                  <span className="text-meta text-muted-foreground">Số lượt poll theo khoảng thời gian</span>
                </div>
                <WindowHistogram counts={p.window_histogram} target={target} />
              </div>
            </div>
          )}
          <Freshness data={d} />
        </div>
      )}
    </Section>
  )
}
