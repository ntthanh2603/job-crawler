// Tổng quan nhóm "Lịch thu thập": độ trễ phát hiện tin mới ước tính và dải 24 giờ cho khung build /
// khung poll, tính theo giá trị đang sửa (chưa lưu cũng thấy ngay).
import { Clock3, Moon, Sun } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"

import { dec, num } from "@/lib/format"
import type { SettingItem } from "@/lib/types"
import { cn } from "@/lib/utils"

import { currentRaw, nowIn, parseWindow, toPayload, windowLength, windowSegments, type Draft } from "./model"

const TARGET = 180

function minutesText(seconds: number) {
  return dec(seconds / 60, seconds % 60 ? 1 : 0)
}

function useNow(timeZone: string) {
  const [now, setNow] = useState(() => nowIn(timeZone))
  useEffect(() => {
    setNow(nowIn(timeZone))
    const timer = window.setInterval(() => setNow(nowIn(timeZone)), 30_000)
    return () => window.clearInterval(timer)
  }, [timeZone])
  return now
}

function Track({
  icon,
  label,
  value,
  barClass,
  iconClass,
  nowMinute,
}: {
  icon: ReactNode
  label: string
  value: string
  barClass: string
  iconClass: string
  nowMinute: number | null
}) {
  const w = parseWindow(value)
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] items-center gap-x-4 gap-y-1.5 sm:grid-cols-[11rem_minmax(0,1fr)]">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn("grid size-6 shrink-0 place-items-center rounded-md bg-muted [&_svg]:size-3.5", iconClass)}>
          {icon}
        </span>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-meta font-medium text-foreground">{label}</div>
          <div className="truncate text-caption text-muted-foreground tabular-nums">
            {w ? (w.allDay ? "Cả ngày" : `${w.start} → ${w.end} · ${windowLength(value)}`) : "Khung giờ chưa hợp lệ"}
          </div>
        </div>
      </div>
      <div className="relative h-2.5 rounded-full bg-muted" aria-hidden>
        {windowSegments(value).map(([a, b]) => (
          <span
            key={`${a}-${b}`}
            className={cn("absolute inset-y-0 rounded-full", barClass)}
            style={{ left: `${(a / 1440) * 100}%`, width: `${((b - a) / 1440) * 100}%` }}
          />
        ))}
        {nowMinute !== null && (
          <span className="absolute -inset-y-1 w-0.5 -translate-x-1/2 rounded-full bg-foreground/70" style={{ left: `${(nowMinute / 1440) * 100}%` }} />
        )}
      </div>
    </div>
  )
}

/** Thẻ đầu nhóm Lịch thu thập. */
export function ScheduleOverview({ items, draft }: { items: Map<string, SettingItem>; draft: Draft }) {
  const value = (key: string) => {
    const item = items.get(key)
    return item ? toPayload(item, currentRaw(item, draft)) : null
  }
  const interval = Number(value("poll_interval_seconds"))
  const deadline = Number(value("poll_deadline_seconds"))
  const build = String(value("build_window") ?? "")
  const poll = String(value("poll_window") ?? "")
  const tz = String(value("crawl_timezone") ?? "")
  const now = useNow(tz)
  const ok = Number.isFinite(interval) && Number.isFinite(deadline) && interval > 0
  const high = interval + deadline
  const tone = !ok ? "muted" : interval >= TARGET ? "bad" : high > TARGET ? "warn" : "good"
  const nowMinute = now ? Number(now.slice(0, 2)) * 60 + Number(now.slice(3)) : null

  return (
    <section className="@container overflow-hidden rounded-xl border bg-card shadow-card">
      <div className="grid @3xl:grid-cols-[18rem_minmax(0,1fr)]">
        <div className="space-y-2 border-b p-4 sm:p-5 @3xl:border-r @3xl:border-b-0">
          <div className="flex items-center gap-2 text-meta font-medium text-muted-foreground">
            <Clock3 className="size-4 stroke-[1.75] text-brand" />
            Độ trễ phát hiện tin mới
          </div>
          <div className="text-2xl font-semibold tracking-tight text-foreground tabular-nums">
            {ok ? `≈ ${minutesText(interval)}–${minutesText(high)} phút` : "–"}
          </div>
          <p className="text-meta text-muted-foreground">
            {ok
              ? `Chu kỳ poll ${num(interval)} giây + một lượt poll (tối đa ${num(deadline)} giây).`
              : "Nhập chu kỳ poll và deadline của lượt poll để ước tính."}
          </p>
          {ok && (
            <p
              className={cn(
                "flex items-start gap-1.5 text-meta font-medium",
                tone === "good" && "text-success-ink",
                tone === "warn" && "text-info-ink",
                tone === "bad" && "text-danger-ink",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "mt-1.5 size-2 shrink-0 rounded-full",
                  tone === "good" && "bg-success",
                  tone === "warn" && "bg-info",
                  tone === "bad" && "bg-destructive",
                )}
              />
              {tone === "good"
                ? "Trong mục tiêu 3 phút"
                : tone === "warn"
                  ? `Cần lượt poll ≤ ${num(TARGET - interval)} giây để đạt 3 phút`
                  : "Chu kỳ poll vượt mục tiêu 3 phút"}
            </p>
          )}
        </div>

        <div className="space-y-3 p-4 sm:p-5">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <div className="text-meta font-medium text-muted-foreground">Một ngày thu thập</div>
            <div className="text-caption text-muted-foreground tabular-nums">
              Múi giờ {tz || "–"}
              {now && <> · bây giờ {now}</>}
            </div>
          </div>
          <div className="space-y-3">
            <Track
              icon={<Moon />}
              label="Build bộ khung"
              value={build}
              barClass="bg-chart-3"
              iconClass="text-chart-3"
              nowMinute={nowMinute}
            />
            <Track
              icon={<Sun />}
              label="Poll tin mới"
              value={poll}
              barClass="bg-warning"
              iconClass="text-warning-ink"
              nowMinute={nowMinute}
            />
          </div>
          <div className="grid grid-cols-[minmax(0,1fr)] gap-x-4 sm:grid-cols-[11rem_minmax(0,1fr)]">
            <span className="hidden sm:block" />
            <div className="flex justify-between text-caption text-muted-foreground tabular-nums" aria-hidden>
              {["0h", "6h", "12h", "18h", "24h"].map((t) => (
                <span key={t}>{t}</span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
