// Lịch thu thập: nhãn loại / lý do lượt chạy, trạng thái lịch, bộ khung của doanh nghiệp (sơ đồ ở
// plan-flow.tsx).
import { useQueryClient } from "@tanstack/react-query"
import { Hammer, Map as MapIcon, Radar, Waypoints } from "lucide-react"
import type { ReactNode } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  ExternalLink,
  Loading,
  Section,
  StatusBadge,
  type Tone,
  RunStatusBadge,
} from "@/components/common"
import { CrawlDay, PlanFlow, PlanReads } from "@/components/plan-flow"
import { Button } from "@/components/ui/button"
import { ago, cap, dec, duration, dt, num, shortUrl, sourceLabel } from "@/lib/format"
import {
  getGetCompanyCrawlQueryKey,
  getGetQueueQueryKey,
  useGetCompanyCrawl,
  usePostCompanyBuild,
  usePostCompanyPoll,
} from "@/lib/gen/queries"
import type { CrawlPlan, CrawlSchedule, PollLimits, RunKind, RunTrigger, ScheduleState } from "@/lib/types"
import { cn } from "@/lib/utils"

// --- nhãn ------------------------------------------------------------------------------------------

export const RUN_KINDS: Record<RunKind, { label: string; title: string; className: string }> = {
  build: {
    label: "Build",
    title: "Lượt đủ: tìm nguồn, đọc hết danh sách và chi tiết, ghi lại bộ khung",
    className: "bg-neutral-tone/12 text-neutral-ink",
  },
  poll: {
    label: "Poll",
    title: "Lượt nhẹ: chỉ đọc các trang danh sách / API trong bộ khung, tải chi tiết tin mới",
    className: "bg-info/12 text-info-ink",
  },
}

/** "Lượt build" / "Lượt poll" theo loại lượt; không rõ loại (lượt đã quá hạn lưu) thì "Lượt build / poll". */
export function runName(kind: RunKind | null | undefined): string {
  return kind === "poll" ? "Lượt poll" : kind === "build" ? "Lượt build" : "Lượt build / poll"
}

export const TRIGGERS: Record<RunTrigger, { label: string; title: string }> = {
  nightly: { label: "Đêm", title: "Build lại theo lịch ban đêm" },
  schedule: { label: "Chu kỳ", title: "Poll theo chu kỳ ban ngày" },
  error: { label: "Bộ khung hỏng", title: "Poll gặp lỗi hoặc cấu trúc trang đổi nên build lại bộ khung" },
  board: { label: "Trang tuyển dụng", title: "Trang tuyển dụng vừa có tin mới của doanh nghiệp" },
  new: { label: "Mới", title: "Doanh nghiệp chưa có bộ khung" },
  manual: { label: "Thủ công", title: "Người dùng bấm Build / Poll ngay" },
  continuation: { label: "Build tiếp", title: "Lượt build trước chưa xong hết, build tiếp phần còn lại" },
}

export const SCHEDULE_STATES: Record<ScheduleState, { label: string; tone: Tone; help: string }> = {
  ready: { label: "Đang poll", tone: "good", help: "Có bộ khung, được poll theo chu kỳ trong khung giờ poll" },
  new: { label: "Chưa có bộ khung", tone: "muted", help: "Chưa build lần nào; bộ khung được tạo ở lượt build đầu tiên" },
  no_sources: {
    label: "Không có nguồn để poll",
    tone: "neutral",
    help: "Đã build nhưng không có trang careers / ATS nào để poll (tin chỉ đến từ trang tuyển dụng)",
  },
  broken: { label: "Bộ khung hỏng", tone: "bad", help: "Poll gặp lỗi, đã xếp build lại bộ khung" },
  paused: { label: "Tạm dừng", tone: "muted", help: "Đã tắt thu thập doanh nghiệp này" },
}

export const SCHEDULE_ORDER: ScheduleState[] = ["ready", "new", "no_sources", "broken", "paused"]

export const PLAN_STATUS: Record<CrawlPlan["status"], { label: string; tone: Tone }> = {
  active: { label: "Đang dùng", tone: "good" },
  broken: { label: "Hỏng, chờ build lại", tone: "bad" },
  retired: { label: "Không còn dùng", tone: "muted" },
}

function Pill({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-flex h-5 shrink-0 items-center rounded-md px-1.5 text-caption font-medium whitespace-nowrap", className)}
    >
      {children}
    </span>
  )
}

/** Nhãn loại lượt chạy (Build / Poll) và lý do (Đêm, Chu kỳ...). */
export function RunTags({ kind, trigger, className }: { kind?: string | null; trigger?: string | null; className?: string }) {
  const k = kind ? RUN_KINDS[kind as RunKind] : undefined
  const t = trigger ? TRIGGERS[trigger as RunTrigger] : undefined
  if (!k && !t && !kind && !trigger) return null
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1", className)}>
      {kind && (
        <Pill title={k?.title} className={k?.className ?? "bg-secondary text-secondary-foreground"}>
          {k?.label ?? cap(kind)}
        </Pill>
      )}
      {trigger && (
        <Pill title={t?.title} className="bg-secondary text-secondary-foreground">
          {t?.label ?? cap(trigger)}
        </Pill>
      )}
    </span>
  )
}

/** "+3 mới · 1 đóng" của một lượt chạy. */
export function JobDelta({ added, closed, className }: { added: number; closed: number; className?: string }) {
  return (
    <span className={cn("text-meta whitespace-nowrap tabular-nums", className)}>
      <span className={added ? "font-medium text-success-ink" : "text-muted-foreground"}>+{num(added)} mới</span>
      <span className="text-muted-foreground"> · </span>
      <span className={closed ? "font-medium text-foreground" : "text-muted-foreground"}>{num(closed)} đóng</span>
    </span>
  )
}

export function ScheduleBadge({ state }: { state: string }) {
  const s = SCHEDULE_STATES[state as ScheduleState]
  return <StatusBadge value={state} tone={s?.tone ?? "muted"} text={s?.label ?? cap(state)} />
}

// --- thời gian -------------------------------------------------------------------------------------

/** Số giây → "45 giây", "2,5 phút", "1,5 giờ". */
export function secondsText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–"
  if (value < 120) return `${num(value)} giây`
  if (value < 3600) return `${dec(value / 60, value % 60 ? 1 : 0)} phút`
  const hours = value / 3600
  return `${dec(hours, Number.isInteger(hours) ? 0 : 1)} giờ`
}

/** Thời điểm tương đối: quá khứ "3 phút trước", tương lai "sau 2 phút". */
export function relative(value: string | null | undefined): string {
  if (!value) return "–"
  const diff = (new Date(value).getTime() - Date.now()) / 1000
  if (diff <= 5) return cap(ago(value))
  if (diff < 60) return `Sau ${Math.round(diff)} giây`
  if (diff < 3600) return `Sau ${Math.round(diff / 60)} phút`
  if (diff < 86_400) {
    const hours = Math.round(diff / 360) / 10
    return `Sau ${dec(hours, Number.isInteger(hours) ? 0 : 1)} giờ`
  }
  return dt(value)
}

/** Lịch sắp tới: "Sau 2 phút"; đã tới giờ: "Đến hạn"; trễ hơn 1 phút: "Quá hạn 3 phút". */
export function due(value: string | null | undefined): string {
  if (!value) return "–"
  const late = (Date.now() - new Date(value).getTime()) / 1000
  if (late <= 0) return relative(value)
  if (late < 60) return "Đến hạn"
  return `Quá hạn ${secondsText(late)}`
}

// --- bộ khung doanh nghiệp ---------------------------------------------------------------------------

/** Bộ khung & lịch thu thập của doanh nghiệp (thẻ số liệu và tab "Bộ khung & lịch"), làm mới 15 giây. */
export function useCompanyCrawl(id: string) {
  return useGetCompanyCrawl(id, { query: { refetchInterval: 15_000 } })
}

/** Poll ngay / build lại bộ khung một doanh nghiệp. */
export function useCrawlActions(id: string) {
  const client = useQueryClient()
  const refresh = () => {
    client.invalidateQueries({ queryKey: getGetCompanyCrawlQueryKey(id) })
    client.invalidateQueries({ queryKey: getGetQueueQueryKey() })
  }
  const poll = usePostCompanyPoll({
    mutation: {
      onSuccess: (r) => {
        if (r.queued) toast.success("Đã đưa lượt poll vào hàng đợi")
        else toast.warning("Lượt poll của doanh nghiệp này đã có trong hàng đợi")
        refresh()
      },
    },
  })
  const build = usePostCompanyBuild({
    mutation: {
      onSuccess: (r) => {
        if (r.queued) toast.success("Đã đưa lượt build lại bộ khung vào hàng đợi. Lượt chạy mới sẽ hiện sau ít phút.")
        else toast.warning("Lượt build của doanh nghiệp này đã có trong hàng đợi")
        refresh()
      },
    },
  })
  return { poll, build }
}

function Fact({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "warn" | "bad" }) {
  return (
    <div className="min-w-0">
      <dt className="text-meta text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "text-sm font-medium tabular-nums",
          tone === "warn" && "text-warning-ink",
          tone === "bad" && "text-danger-ink",
        )}
      >
        {value}
      </dd>
      {sub && <dd className="text-meta text-muted-foreground">{sub}</dd>}
    </div>
  )
}

function ScheduleFacts({ schedule: s }: { schedule: CrawlSchedule }) {
  const overdue = s.state === "ready" && s.next_poll_at && new Date(s.next_poll_at).getTime() < Date.now() - 60_000
  return (
    <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-4">
      <Fact
        label="Poll gần nhất"
        value={relative(s.last_poll_at)}
        sub={s.last_poll_ms !== null ? `Mất ${duration(s.last_poll_ms)}` : undefined}
      />
      <Fact
        label="Poll đủ gần nhất"
        value={relative(s.last_poll_ok_at)}
        sub="Mọi nguồn trong bộ khung đều đọc được"
      />
      <Fact
        label="Poll tiếp theo"
        value={s.state === "ready" ? due(s.next_poll_at) : "–"}
        sub={s.state === "ready" ? undefined : SCHEDULE_STATES[s.state]?.label}
        tone={overdue ? "warn" : undefined}
      />
      <Fact label="Tin thay đổi gần nhất" value={relative(s.last_change_at)} sub="Lần poll / build thấy tin mới hoặc tin đóng" />
      <Fact
        label="Build gần nhất"
        value={relative(s.last_build_at)}
        sub={s.last_build_status ? <RunStatusBadge value={s.last_build_status} className="mt-1" /> : undefined}
      />
      <Fact label="Build tiếp theo" value={due(s.next_build_at)} />
      <Fact label="Số lượt poll" value={num(s.polls)} />
      <Fact
        label="Lỗi poll liên tiếp"
        value={num(s.poll_failures)}
        tone={s.poll_failures ? "warn" : undefined}
        sub={s.poll_failures ? "Lặp lại nhiều lần thì bộ khung được build lại" : undefined}
      />
    </dl>
  )
}

function PlanItem({ plan: p, limits }: { plan: CrawlPlan; limits: PollLimits }) {
  const st = PLAN_STATUS[p.status] ?? { label: cap(p.status), tone: "muted" as Tone }
  const target = p.source.includes(":") ? p.source.split(":").slice(1).join(":") : ""
  return (
    <li className={cn("space-y-4 px-4 py-4 sm:px-5", p.status === "retired" && "opacity-70")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{sourceLabel(p.kind || p.source)}</span>
            {target && <span className="truncate text-meta text-muted-foreground">{target}</span>}
            <StatusBadge value={p.status} tone={st.tone} text={st.label} />
          </div>
          <ExternalLink href={p.url} className="block text-meta break-all">
            {shortUrl(p.url)}
          </ExternalLink>
          <div className="flex flex-wrap gap-x-4 gap-y-1 pt-0.5 text-meta text-muted-foreground">
            <span>Build {ago(p.built_at)}</span>
            <span>{p.polled_at ? `Poll ${ago(p.polled_at)}` : "Chưa poll"}</span>
            {p.changed_at && <span>Thay đổi {ago(p.changed_at)}</span>}
            {p.failures > 0 && <span className="font-medium text-warning-ink">{num(p.failures)} lỗi liên tiếp</span>}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <span className="text-right">
            <span className="block text-base font-semibold text-brand tabular-nums">{num(p.jobs)}</span>
            <span className="text-caption text-muted-foreground">tin trong bộ khung</span>
          </span>
          <Button variant="outline" size="sm" asChild>
            <Link to={`/plans/${p.id}`}>
              <MapIcon /> Bản đồ bộ khung
            </Link>
          </Button>
        </div>
      </div>
      {p.status === "retired" ? (
        <p className="text-meta text-muted-foreground">Lượt build gần nhất không còn thấy nguồn này nên lượt poll không đọc nữa.</p>
      ) : p.readable ? (
        <PlanFlow plan={p} limits={limits} />
      ) : (
        <p className="text-meta text-warning-ink">
          Bộ khung ghi theo phiên bản cũ nên không đọc lại được: lượt poll tới đánh hỏng và doanh nghiệp được build lại.
        </p>
      )}
      <PlanReads plan={p} />
      {p.last_error && (
        <pre className="overflow-x-auto rounded-lg bg-destructive/6 p-2.5 font-mono text-meta whitespace-pre-wrap text-danger-ink [overflow-wrap:anywhere]">
          {p.last_error}
        </pre>
      )}
    </li>
  )
}

/** Tab "Bộ khung & lịch" của trang doanh nghiệp. */
export function CompanyCrawlTab({ id }: { id: string }) {
  const query = useCompanyCrawl(id)
  const { poll, build } = useCrawlActions(id)
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data)
    return (
      <Section>
        <Loading rows={4} />
      </Section>
    )
  const { schedule, plans, poll_interval_seconds: interval, windows, limits } = query.data
  const active = plans.filter((p) => p.status === "active").length
  return (
    <div className="space-y-6">
      <Section
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            Lịch thu thập {schedule && <ScheduleBadge state={schedule.state} />}
          </span>
        }
        description={`Ban ngày poll mỗi ${secondsText(interval)} theo bộ khung; bộ khung được build lại mỗi đêm hoặc ngay khi poll thấy hỏng.`}
        actions={
          <>
            <Button size="sm" onClick={() => poll.mutate({ companyId: id })} disabled={poll.isPending}>
              <Radar /> {poll.isPending ? "Đang xếp lượt poll…" : "Poll ngay"}
            </Button>
            <ConfirmButton
              title="Build lại bộ khung?"
              description="Tìm lại nguồn trên website, đọc đủ danh sách và chi tiết tin rồi ghi bộ khung mới. Lượt build mất vài phút; trong lúc đó vẫn poll theo bộ khung hiện tại."
              confirm="Build lại"
              onConfirm={() => build.mutate({ companyId: id })}
              disabled={build.isPending}
            >
              <Hammer /> Build lại bộ khung
            </ConfirmButton>
          </>
        }
      >
        <CrawlDay windows={windows} interval={secondsText(interval)} lastBuildAt={schedule?.last_build_at} />
        {schedule ? (
          <div className="mt-5 space-y-4 border-t pt-5">
            <ScheduleFacts schedule={schedule} />
            {schedule.last_error && (
              <div className="rounded-lg border border-destructive/25 bg-destructive/5 p-3">
                <div className="text-meta font-medium text-danger-ink">Lỗi gần nhất</div>
                <pre className="mt-1 font-mono text-meta whitespace-pre-wrap text-danger-ink [overflow-wrap:anywhere]">
                  {schedule.last_error}
                </pre>
              </div>
            )}
          </div>
        ) : (
          <p className="mt-5 border-t pt-5 text-sm text-muted-foreground">
            Doanh nghiệp chưa có lịch thu thập. Lịch và bộ khung được tạo ở lượt build đầu tiên.
          </p>
        )}
      </Section>

      <Section
        title="Bộ khung theo nguồn"
        description={
          plans.length
            ? `${num(active)} nguồn đang dùng${plans.length > active ? ` · ${num(plans.length - active)} hỏng hoặc không còn dùng` : ""} · mỗi lượt poll chỉ đọc các trang này`
            : undefined
        }
        bodyClassName="p-0"
      >
        {plans.length ? (
          <ul className="divide-y">
            {plans.map((p) => (
              <PlanItem key={p.id} plan={p} limits={limits} />
            ))}
          </ul>
        ) : (
          <Empty icon={<Waypoints />} title="Chưa có bộ khung">
            {schedule?.state === "no_sources"
              ? "Lượt build gần nhất không tìm thấy trang careers hay ATS nào để poll; tin của doanh nghiệp chỉ đến từ trang tuyển dụng."
              : "Bộ khung (trang danh sách, API của từng nguồn) được ghi ở lượt build. Bấm “Build lại bộ khung” để tạo ngay."}
          </Empty>
        )}
      </Section>
    </div>
  )
}
