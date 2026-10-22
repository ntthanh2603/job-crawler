// Sơ đồ bộ khung: một ngày thu thập (build đêm / poll ngày) và đường một lượt poll đi theo bộ khung
// của từng nguồn — đọc danh sách / API → so mã tin với lượt trước → mở chi tiết tin mới → kết quả.
import { ArrowDown, ArrowRight, FileSearch, GitCompareArrows, ListChecks, Moon, Sun, Waypoints } from "lucide-react"
import { useState, type ReactNode } from "react"

import { DOT_CLASS, ExternalLink, type Tone } from "@/components/common"
import { Button } from "@/components/ui/button"
import { ago, num, shortUrl, sourceLabel } from "@/lib/format"
import type { CompanyCrawl, CrawlPlan, PollLimits } from "@/lib/types"
import { cn } from "@/lib/utils"

type Windows = CompanyCrawl["windows"]
type PlanApi = CrawlPlan["apis"][number]
type PlanPage = CrawlPlan["listings"][number]

// --- một ngày thu thập -----------------------------------------------------------------------------

const DAY = 24 * 60

/** Đoạn [bắt đầu, kết thúc) theo phút trong ngày của khung "HH:MM-HH:MM"; qua nửa đêm thì thành hai
 * đoạn, bắt đầu = kết thúc là cả ngày (như `Window` ở backend/modules/crawl/schedule_service.py). */
export function windowSegments(value: string): [number, number][] {
  const m = /^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/.exec(value.trim())
  if (!m) return []
  const start = Number(m[1]) * 60 + Number(m[2])
  const end = Number(m[3]) * 60 + Number(m[4])
  const length = (((end - start) % DAY) + DAY) % DAY || DAY
  const stop = start + length
  return stop <= DAY ? [[start, stop]] : [[start, DAY], [0, stop - DAY]]
}

/** Phút trong ngày của `at` theo múi giờ `timezone` (null nếu múi giờ không hợp lệ). */
export function minuteOfDay(at: string | Date, timezone: string): number | null {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: timezone,
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(at))
    const part = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0)
    return part("hour") * 60 + part("minute")
  } catch {
    return null
  }
}

function clockText(minute: number) {
  const h = Math.floor(minute / 60)
  return `${String(h).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`
}

function windowText(value: string) {
  return value.replace(/\s*-\s*/, "–")
}

const BANDS = {
  build: { className: "bg-neutral-tone/22 text-neutral-ink", icon: Moon, label: "Build đêm" },
  poll: { className: "bg-info/18 text-info-ink", icon: Sun, label: "Poll ngày" },
} as const

/** Thanh 24 giờ (giờ CRAWL_TIMEZONE): khung build đêm, khung poll ngày, bây giờ và lượt build gần nhất. */
export function CrawlDay({
  windows: w,
  interval,
  lastBuildAt,
}: {
  windows: Windows
  interval: string
  lastBuildAt?: string | null
}) {
  const now = minuteOfDay(new Date(), w.timezone)
  const built =
    lastBuildAt && Date.now() - new Date(lastBuildAt).getTime() < DAY * 60_000 ? minuteOfDay(lastBuildAt, w.timezone) : null
  const bands = [
    ...windowSegments(w.build).map((s) => ({ kind: "build" as const, s })),
    ...windowSegments(w.poll).map((s) => ({ kind: "poll" as const, s })),
  ]
  const left = (minute: number) => `${(minute / DAY) * 100}%`
  return (
    <figure className="space-y-2" aria-label="Một ngày thu thập">
      <div className="relative pt-5">
        {now !== null && (
          <span
            className="absolute top-0 -translate-x-1/2 text-caption font-medium whitespace-nowrap text-foreground tabular-nums"
            style={{ left: `clamp(2.25rem, ${left(now)}, calc(100% - 2.25rem))` }}
          >
            Bây giờ {clockText(now)}
          </span>
        )}
        <div className="relative h-9 overflow-hidden rounded-lg bg-muted ring-1 ring-border/70 ring-inset">
          {bands.map(({ kind, s: [start, end] }) => {
            const band = BANDS[kind]
            const Icon = band.icon
            return (
              <div
                key={`${kind}-${start}`}
                data-band={kind}
                className={cn("absolute inset-y-0 flex items-center justify-center gap-1 overflow-hidden px-1", band.className)}
                style={{ left: left(start), width: left(end - start) }}
                title={`${band.label} ${clockText(start)}–${clockText(end)}`}
              >
                {end - start >= 150 && (
                  <>
                    <Icon className="size-3.5 shrink-0" aria-hidden />
                    <span className="truncate text-caption font-medium">{band.label}</span>
                  </>
                )}
              </div>
            )
          })}
          {built !== null && (
            <span
              className="absolute bottom-0 size-2 -translate-x-1/2 translate-y-1/2 rotate-45 bg-neutral-ink"
              style={{ left: left(built) }}
              title={`Build gần nhất lúc ${clockText(built)}`}
            />
          )}
          {now !== null && <span className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-foreground" style={{ left: left(now) }} />}
        </div>
        <div className="relative mt-1 h-4 text-caption text-muted-foreground tabular-nums" aria-hidden>
          {[0, 6, 12, 18, 24].map((h) => (
            <span
              key={h}
              className={cn("absolute", h === 0 ? "" : h === 24 ? "-translate-x-full" : "-translate-x-1/2")}
              style={{ left: left(h * 60) }}
            >
              {h}h
            </span>
          ))}
        </div>
      </div>
      <figcaption className="flex flex-wrap gap-x-5 gap-y-1 text-meta text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-neutral-tone/40" aria-hidden />
          <span>
            <span className="font-medium text-foreground">Build đêm {windowText(w.build)}</span>: tìm nguồn, đọc đủ, ghi lại bộ khung
          </span>
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="size-2.5 rounded-sm bg-info/40" aria-hidden />
          <span>
            <span className="font-medium text-foreground">Poll ngày {windowText(w.poll)}</span>: mỗi {interval}, chỉ theo bộ khung
          </span>
        </span>
        {built !== null && (
          <span className="inline-flex items-center gap-1.5">
            <span className="size-2 rotate-45 bg-neutral-ink" aria-hidden />
            Build gần nhất {clockText(built)}
          </span>
        )}
        <span>Giờ {w.timezone}</span>
      </figcaption>
    </figure>
  )
}

// --- đường lượt poll -------------------------------------------------------------------------------

const RING: Record<Tone, string> = {
  good: "border-success/45 bg-success/8",
  warn: "border-warning/55 bg-warning/10",
  bad: "border-destructive/40 bg-destructive/6",
  info: "border-info/45 bg-info/8",
  neutral: "border-neutral-tone/40 bg-neutral-tone/8",
  muted: "border-border bg-muted/60",
}

type Outcome = { value: CrawlPlan["last_poll"]; tone: Tone; label: string; help: string }

/** Lượt poll gần nhất thuộc bộ khung hiện tại (lượt build ghi lại bộ khung sau lượt poll đó thì không). */
function polledSinceBuild(plan: CrawlPlan) {
  return plan.polled_at !== null && new Date(plan.polled_at).getTime() >= new Date(plan.built_at).getTime()
}

/** Bốn đường một lượt poll có thể đi (`modules/crawl/crawl_service.py: _poll`). */
function outcomes(plan: CrawlPlan, limits: PollLimits): Outcome[] {
  return [
    {
      value: "unchanged",
      tone: "good",
      label: "Không đổi",
      help: "Tập mã tin như lượt trước: chỉ cập nhật lịch, không ghi lượt chạy",
    },
    {
      value: "changed",
      tone: "info",
      label: "Có tin mới / tin gỡ",
      help: "Chỉ tin mới được xử lý, tin không còn thấy được đóng; ghi một lượt Poll",
    },
    {
      value: "retrying",
      tone: "warn",
      label: `Lỗi tạm ${num(Math.min(plan.failures, limits.max_failures))}/${num(limits.max_failures)}`,
      help: `Lỗi ${num(limits.max_failures)} lượt liên tiếp thì bộ khung hỏng và được build lại`,
    },
    {
      value: "broken",
      tone: "bad",
      label: "Hỏng → build lại",
      // Lượt build không đọc được nguồn cũng ghi bộ khung `broken` (chưa có lượt poll nào).
      help: polledSinceBuild(plan)
        ? "Trang chính 404/410, danh sách trống hoặc lỗi lặp lại: doanh nghiệp được build lại ngay"
        : "Lượt build không đọc được nguồn này: không poll, chờ lượt build sau",
    },
  ]
}

function Stage({ step, icon, title, children }: { step: number; icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <li className="min-w-0 flex-1 rounded-lg border bg-background/60 px-3 py-2.5">
      <div className="mb-1 flex items-center gap-1.5 text-meta font-medium text-muted-foreground [&_svg]:size-3.5 [&_svg]:shrink-0">
        <StepNo>{step}</StepNo>
        {icon}
        <span className="min-w-0">{title}</span>
      </div>
      {children}
    </li>
  )
}

function StepNo({ children }: { children: ReactNode }) {
  return (
    <span className="grid size-4.5 shrink-0 place-items-center rounded-full bg-secondary text-caption text-secondary-foreground tabular-nums">
      {children}
    </span>
  )
}

function Arrow() {
  return (
    <li aria-hidden className="grid shrink-0 place-items-center text-muted-foreground/70">
      <ArrowDown className="size-4 @lg:hidden" />
      <ArrowRight className="hidden size-4 @lg:block" />
    </li>
  )
}

function Big({ children }: { children: ReactNode }) {
  return <div className="text-sm font-semibold text-foreground tabular-nums">{children}</div>
}

function Sub({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <p className="mt-0.5 text-meta text-muted-foreground" title={title}>
      {children}
    </p>
  )
}

/** Ô vuông số trang: tô đậm phần lượt poll được đọc trong số trang cần để đọc trọn nguồn. */
export function PageBudget({ pages, needed }: { pages: number; needed: number }) {
  const total = Math.max(needed, pages, 1)
  const shown = Math.min(total, 20)
  const legend = [`${num(Math.min(pages, needed))} ô đậm: trang cần đọc, được đọc`]
  if (pages > needed) legend.push(`${num(pages - needed)} ô nhạt: phần dư nếu site thêm trang`)
  if (needed > pages) legend.push(`${num(needed - pages)} ô vàng: trang không được đọc`)
  return (
    <div
      className="mt-1.5 flex flex-wrap items-center gap-0.5"
      role="img"
      aria-label={`Được đọc ${num(pages)} trang, cần ${num(needed)} trang để đọc trọn`}
      title={legend.join(" · ")}
    >
      {Array.from({ length: shown }, (_, i) => (
        <span
          key={i}
          className={cn("h-2 w-2.5 rounded-[2px]", i < pages ? (i < needed ? "bg-info" : "bg-info/35") : "bg-warning/60")}
        />
      ))}
      {total > shown && <span className="text-caption text-muted-foreground">+{num(total - shown)}</span>}
    </div>
  )
}

function budgetText(plan: CrawlPlan, limits: PollLimits) {
  if (plan.pages <= 0) return `Tối đa ${num(limits.max_pages)} trang/lượt (giới hạn chung)`
  if (plan.pages_needed > plan.pages) return `Đọc ${num(plan.pages)}/${num(plan.pages_needed)} trang/lượt: chưa trọn, không đóng tin`
  return `Đọc trọn ${num(Math.max(plan.pages_needed, 1))} trang/lượt`
}

/** Đường một lượt poll đi theo bộ khung của nguồn (đọc → so → mở chi tiết), rồi bốn kết quả có thể
 * có, tô sáng kết quả của lượt poll gần nhất. Bố cục theo bề rộng khung chứa (container query). */
export function PlanFlow({ plan, limits }: { plan: CrawlPlan; limits: PollLimits }) {
  const calls = plan.apis.filter((a) => !a.embedded).length
  const reads = [calls ? `${num(calls)} API` : null, plan.listings_total ? `${num(plan.listings_total)} trang` : null]
  const list = plan.ats ? `API ${sourceLabel(plan.ats)}` : reads.filter(Boolean).join(" · ") || "Trang bắt đầu"
  const paths = outcomes(plan, limits)
  const current = paths.find((o) => o.value === plan.last_poll)
  return (
    <div className="@container space-y-1.5">
      <ol className="flex flex-col gap-1.5 @lg:flex-row @lg:items-stretch" aria-label="Đường một lượt poll">
        <Stage step={1} icon={<ListChecks />} title="Đọc danh sách">
          <Big>{list}</Big>
          <Sub>{plan.ats ? "Gọi API của ATS" : calls ? "Gọi API trước, rồi tới trang" : "HTTP trước, trống mới render"}</Sub>
          <Sub>{budgetText(plan, limits)}</Sub>
          {plan.pages > 0 && <PageBudget pages={plan.pages} needed={plan.pages_needed} />}
          {plan.llm_listing && <Sub>LLM đọc lại khi trang đổi</Sub>}
        </Stage>
        <Arrow />
        <Stage step={2} icon={<GitCompareArrows />} title="So mã tin">
          <Big>{num(plan.jobs)} mã tin</Big>
          <Sub>So với lượt đọc trọn trước; chỉ phần khác đi tiếp</Sub>
          {plan.indirect > 0 && (
            <Sub title="Tin chỉ tới được qua trang tin khác: lượt poll không thấy lại vẫn giữ, lượt build đêm mới đóng">
              Giữ {num(plan.indirect)} tin gián tiếp
            </Sub>
          )}
        </Stage>
        <Arrow />
        <Stage step={3} icon={<FileSearch />} title="Mở chi tiết">
          <Big>Chỉ tin mới</Big>
          <Sub>Tối đa {num(limits.max_detail_pages)} trang/lượt; tin đã biết dùng lại</Sub>
          {plan.ignored > 0 && <Sub>Bỏ qua {num(plan.ignored)} link không phải tin</Sub>}
        </Stage>
      </ol>
      <div className={cn("rounded-lg border px-3 py-2.5", current ? RING[current.tone] : "bg-background/60")}>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="flex items-center gap-1.5 text-meta font-medium text-muted-foreground [&_svg]:size-3.5">
            <StepNo>4</StepNo>
            <Waypoints />
            Kết quả lượt poll gần nhất
          </span>
          <ul className="flex flex-wrap gap-1" aria-label="Kết quả lượt poll">
            {paths.map((o) => {
              const active = o.value === plan.last_poll
              return (
                <li
                  key={o.value}
                  title={o.help}
                  aria-current={active ? "step" : undefined}
                  className={cn(
                    "inline-flex h-6 items-center gap-1.5 rounded-full px-2 text-meta whitespace-nowrap",
                    active ? "bg-card font-semibold text-foreground shadow-card ring-1 ring-border" : "text-muted-foreground/75",
                  )}
                >
                  <span className={cn("size-1.5 shrink-0 rounded-full", active ? DOT_CLASS[o.tone] : "bg-muted-foreground/30")} aria-hidden />
                  {o.label}
                </li>
              )
            })}
          </ul>
        </div>
        <p className="mt-1.5 text-meta text-muted-foreground">
          {plan.last_poll === "never" ? `Chưa poll kể từ lần build ${ago(plan.built_at)}.` : current?.help}
          {plan.last_poll !== "never" && polledSinceBuild(plan) && ` · ${ago(plan.polled_at)}`}
        </p>
      </div>
    </div>
  )
}

// --- thứ tự đọc của bộ khung -------------------------------------------------------------------------

/** Cách lượt poll gọi một API của bộ khung. */
export function apiText(a: PlanApi) {
  if (a.embedded) return "JSON nhúng trong trang danh sách, đọc cùng trang"
  if (!a.paged) return "Một request, không phân trang"
  const parts = [`${num(a.pages)} trang`]
  if (a.size) parts.push(`${num(a.size)} tin/trang`)
  if (a.total !== null) parts.push(`${num(a.total)} tin lúc build`)
  if (a.page_param) parts.push(`${a.by_offset ? "offset" : "trang"} theo “${a.page_param}”`)
  return parts.join(" · ")
}

/** Nhãn loại của một trang trong bộ khung. */
export function pageKind(p: PlanPage) {
  if (p.sitemap) return "Sitemap"
  return p.hub ? "Danh mục" : "Trang"
}

/** Vai trò của một trang trong bộ khung và cách lượt poll mở nó. */
export function pageText(p: PlanPage) {
  if (p.sitemap) return "Sitemap của site · lấy link cùng dạng URL với tin trên trang danh sách"
  const parts = [p.hub ? "Trang danh mục dẫn tới tin" : "Trang danh sách"]
  if (p.api_only) parts.push("API trả tin thì bỏ qua trang này")
  else if (p.rendered) parts.push("HTTP trước, HTML trống mới mở trình duyệt")
  return parts.join(" · ")
}

/** Nhãn loại của một dòng trong bộ khung: Trang / Danh mục / GET / POST / JSON. */
export function Kind({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 w-14 shrink-0 items-center justify-center rounded-md text-caption font-medium",
        className ?? "bg-secondary text-secondary-foreground",
      )}
    >
      {children}
    </span>
  )
}

const PREVIEW = 6

/** Các API và trang trong bộ khung, theo thứ tự lượt poll đọc (API trước, rồi trang danh sách / danh mục). */
export function PlanReads({ plan }: { plan: CrawlPlan }) {
  const [all, setAll] = useState(false)
  const rows: { key: string; kind: ReactNode; url: string; text: string; dim?: boolean }[] = [
    ...plan.apis.map((a, i) => ({
      key: `api-${i}`,
      kind: <Kind className="bg-info/12 text-info-ink">{a.embedded ? "JSON" : a.method}</Kind>,
      url: a.url,
      text: apiText(a),
      dim: a.embedded,
    })),
    ...plan.listings.map((p) => ({
      key: `page-${p.url}`,
      kind: <Kind>{pageKind(p)}</Kind>,
      url: p.url,
      text: pageText(p),
      dim: p.api_only,
    })),
  ]
  if (!rows.length) return null
  const more = plan.listings_total - plan.listings.length + (plan.apis_total - plan.apis.length)
  const shown = all ? rows : rows.slice(0, PREVIEW)
  return (
    <div className="space-y-1.5">
      <div className="text-meta font-medium text-muted-foreground">Thứ tự đọc của lượt poll</div>
      <ol className="divide-y rounded-lg border">
        {shown.map((r, i) => (
          <li key={r.key} className={cn("flex min-w-0 items-start gap-2.5 px-3 py-2", r.dim && "opacity-75")}>
            <span className="w-5 shrink-0 pt-0.5 text-right text-caption text-muted-foreground tabular-nums">{i + 1}</span>
            {r.kind}
            <div className="min-w-0 flex-1">
              <ExternalLink href={r.url} className="block truncate text-meta" title={r.url}>
                {shortUrl(r.url)}
              </ExternalLink>
              <div className="text-caption text-muted-foreground">{r.text}</div>
            </div>
          </li>
        ))}
      </ol>
      {(rows.length > PREVIEW || more > 0) && (
        <div className="flex flex-wrap items-center gap-2 text-meta text-muted-foreground">
          {rows.length > PREVIEW && (
            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => setAll((v) => !v)}>
              {all ? "Thu gọn" : `Xem cả ${num(rows.length)} mục`}
            </Button>
          )}
          {more > 0 && <span>+{num(more)} trang / API nữa trong bộ khung</span>}
        </div>
      )}
    </div>
  )
}
