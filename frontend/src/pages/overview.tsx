import { Activity, Briefcase, Building2, Database, Layers, RefreshCw, Wrench } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Dot,
  Empty,
  ErrorState,
  FilterSelect,
  IncompleteList,
  IncompleteSources,
  InfoTip,
  PageLoading,
  PageHeader,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  TabBar,
  Tag,
  TextLink,
  toneOf,
  useRowLink,
  useTabParam,
  RunStatusBadge,
} from "@/components/common"
import { BarList, ShareBar, StackedColumns, type BarItem } from "@/components/charts"
import { JobDelta, RunTags } from "@/components/crawl"
import { DetectionSection } from "@/components/detection"
import { useLiveQueue } from "@/components/live"
import { Button } from "@/components/ui/button"
import type { ChartConfig } from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { useGetOverview, usePostMaintenance } from "@/lib/gen/queries"
import {
  ago,
  byCount,
  bytes,
  cap,
  companyName,
  dec,
  displayName,
  dt,
  duration,
  hour,
  label,
  num,
  pct,
  sourceLabel,
} from "@/lib/format"
import type { LlmStats, Overview, Pair } from "@/lib/types"

const WINDOWS = [
  ["1", "1 giờ qua"],
  ["6", "6 giờ qua"],
  ["24", "24 giờ qua"],
  ["168", "7 ngày qua"],
  ["720", "30 ngày qua"],
  ["0", "Toàn bộ"],
] as const

const TABS = ["runs", "jobs", "llm", "storage"] as const

// Thứ tự khoá = thứ tự xếp chồng từ dưới lên (đã kiểm định màu kề nhau, xem index.css --viz-*).
const runsConfig = {
  ok: { label: "Thành công", color: "var(--viz-ok)" },
  ambiguous: { label: "Xong · KYC chưa rõ", color: "var(--viz-ambiguous)" },
  partial: { label: "Một phần", color: "var(--viz-partial)" },
  running: { label: "Đang chạy", color: "var(--viz-running)" },
  failed: { label: "Thất bại", color: "var(--viz-failed)" },
} satisfies ChartConfig

const fetchConfig = {
  ok: { label: "Thành công", color: "var(--viz-ok)" },
  error: { label: "Lỗi / quá giờ", color: "var(--viz-ambiguous)" },
  blocked: { label: "Bị chặn", color: "var(--viz-failed)" },
} satisfies ChartConfig

const llmConfig = {
  ok: { label: "Thành công", color: "var(--viz-ok)" },
  cached: { label: "Dùng lại từ cache", color: "var(--viz-1)" },
  failed: { label: "Lỗi", color: "var(--viz-failed)" },
} satisfies ChartConfig

const tokensConfig = {
  prompt_tokens: { label: "Token vào", color: "var(--viz-1)" },
  completion_tokens: { label: "Token ra", color: "var(--viz-2)" },
} satisfies ChartConfig

// Tên prompt (`backend/jobcrawler/prompts/*.py`) → bước dùng LLM.
const PROMPT_NAMES: Record<string, string> = {
  careers_pick: "Chọn trang careers",
  job_listing: "Bóc danh sách tin",
  job_detail: "Bóc chi tiết tin",
  job_verify: "Chấm tin còn nghi ngờ",
  job_fields: "Phân loại cấp bậc / loại hình",
  kyc_pick: "KYC: chọn website / pháp nhân",
  profile_match: "Khớp profile nhà tuyển dụng",
  healthcheck: "Kiểm tra kết nối",
}

// Loại lỗi (`llm_metrics.error_kind`): phần trước dấu ":" của `LLMResult.error`.
const LLM_ERRORS: Record<string, string> = {
  timeout: "Quá giờ",
  rate_limited: "Chờ lượt quá lâu",
  parse_error: "JSON sai schema",
  empty_response: "Phản hồi rỗng",
  connection: "Không kết nối được",
  error: "Phản hồi sai dạng",
  http_429: "Quá tải (429)",
}

const clockFmt = new Intl.DateTimeFormat("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
const clock = (iso: string) => clockFmt.format(new Date(iso))

function llmError(kind: string) {
  const code = /^http_(\d+)$/.exec(kind)?.[1]
  return LLM_ERRORS[kind] ?? (code ? `Lỗi HTTP ${code}` : cap(kind))
}

const daysConfig = {
  careers: { label: "Trang careers của doanh nghiệp", color: "var(--viz-1)" },
  boards: { label: "Trang tuyển dụng", color: "var(--viz-2)" },
} satisfies ChartConfig

const UNKNOWN = new Set(["unknown", "Không rõ nơi làm", "Thoả thuận", "Không ghi", "Tiền tệ / kỳ khác"])

function bars(pairs: Pair[], toLabel: (key: string) => string, href?: (key: string) => string): BarItem[] {
  return pairs.map(([key, value]) => ({
    key,
    label: key === "unknown" ? "Chưa rõ" : toLabel(key),
    value,
    muted: UNKNOWN.has(key),
    href: href && !UNKNOWN.has(key) ? href(key) : undefined,
  }))
}

const dayFmt = new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit", timeZone: "UTC" })
const dayLong = new Intl.DateTimeFormat("vi-VN", { weekday: "long", day: "2-digit", month: "2-digit", timeZone: "UTC" })

const KYC_COLORS: Record<string, string> = {
  verified: "var(--viz-ok)",
  ambiguous: "var(--viz-ambiguous)",
  not_found: "var(--viz-muted)",
  none: "var(--viz-muted-2)",
}

const TABLE_NAMES: Record<string, string> = {
  job_postings: "Tin theo nguồn",
  jobs: "Việc làm",
  source_fetches: "Lượt request",
  logs: "Log",
  source_profiles: "Profile",
  companies: "Doanh nghiệp",
  company_aliases: "Tên gọi khác",
  company_runs: "Lượt build / poll",
  failed_pages: "Trang parse lỗi",
  board_index_runs: "Lượt index",
  compression_dicts: "Từ điển nén",
  job_reviews: "Kiểm tay",
  alembic_version: "Phiên bản schema",
}

const STORAGE_NAMES: Record<string, string> = {
  posting: "Tin theo nguồn (raw)",
  job_text: "Mô tả việc làm",
  html: "HTML trang lỗi",
}

/** Dòng biểu đồ lượt chạy: { hour, ok, partial, … } từ { hour, counts }. */
function chartRows(rows: Overview["chart"]) {
  return rows.map((b) => ({ hour: b.hour, ...b.counts }))
}

/** Bỏ các ngày đầu không có việc mới (giữ ít nhất 7 ngày) để cột không bị dồn về một góc. */
function trimDays(rows: Overview["charts"]["jobs_by_day"]) {
  const first = rows.findIndex((r) => r.careers + r.boards > 0)
  if (first < 0) return rows.slice(-7)
  return rows.slice(Math.max(0, Math.min(first, rows.length - 7)))
}

function seconds(value: number | null) {
  return duration(value === null ? null : value * 1000)
}

/** Số liệu nhỏ trong khung: nhãn, giá trị, dòng phụ và nhãn đạt / chưa đạt. */
function MiniStat({
  label: title,
  value,
  sub,
  ok,
}: {
  label: string
  value: string
  sub?: string
  ok?: boolean | null
}) {
  return (
    <div className="min-w-0 rounded-lg bg-muted/50 px-4 py-3">
      <div className="text-meta text-muted-foreground">{title}</div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-lg font-semibold tabular-nums">{value}</span>
        {ok !== undefined && ok !== null && (
          <StatusBadge value={null} tone={ok ? "good" : "bad"} text={ok ? "Đạt" : "Chưa đạt"} />
        )}
      </div>
      {sub && <div className="text-meta text-muted-foreground">{sub}</div>}
    </div>
  )
}

function RunsTab({ data }: { data: Overview }) {
  const st = data.stats
  const runs = st.runs
  const rowLink = useRowLink()
  const latest = Object.entries(runsConfig)
    .map(([k, c]) => ({ key: k, label: c.label, value: runs.latest_by_status[k] ?? 0, color: c.color }))
    .filter((i) => i.value)

  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Section
          title="Kết quả lượt build / poll"
          description="Lượt mới nhất của mỗi doanh nghiệp trong khoảng đã chọn"
          bodyClassName="space-y-5"
        >
          {latest.length ? <ShareBar items={latest} /> : <p className="text-sm text-muted-foreground">Chưa có lượt build / poll nào.</p>}
          <div className="grid gap-3 sm:grid-cols-3">
            {st.targets.map((t) => (
              <MiniStat
                key={t.name}
                label={t.kind === "rate" ? "Tỉ lệ thất bại" : "P95 mỗi doanh nghiệp"}
                value={t.value === null ? "–" : t.kind === "rate" ? pct(t.value) : `${num(t.value)} s`}
                sub={`Mục tiêu ${t.target}`}
                ok={t.ok}
              />
            ))}
            <MiniStat
              label="Tin nhận / thấy"
              value={`${num(runs.jobs_accepted)} / ${num(runs.jobs_found)}`}
              sub={runs.jobs_found ? `Nhận ${pct(runs.jobs_accepted / runs.jobs_found, 0)} số tin thấy` : undefined}
            />
          </div>
          <dl className="grid gap-x-6 gap-y-3 border-t pt-4 text-sm md:grid-cols-2">
            <div>
              <dt className="text-meta text-muted-foreground">Thời gian mỗi doanh nghiệp</dt>
              <dd className="tabular-nums">
                P50 {seconds(runs.p50_seconds)} · P95 {seconds(runs.p95_seconds)} · tối đa {seconds(runs.max_seconds)}
                {runs.over_timeout > 0 && <span className="text-danger-ink"> · {num(runs.over_timeout)} quá giờ</span>}
              </dd>
            </div>
            <div>
              <dt className="text-meta text-muted-foreground">Mọi lượt trong khoảng</dt>
              <dd className="flex flex-wrap gap-x-3 gap-y-1">
                {byCount(runs.by_status).map(([k, v]) => (
                  <Dot key={k} tone={toneOf(k)}>
                    {label(k)} <span className="font-medium tabular-nums">{num(v)}</span>
                  </Dot>
                ))}
                {!Object.keys(runs.by_status).length && "Chưa có"}
              </dd>
            </div>
          </dl>
        </Section>

        <Section
          title={
            <span className="inline-flex items-center gap-1.5">
              Nguồn chưa xong
              <InfoTip>
                Lượt build / poll dừng trước khi đọc hết một nguồn được ghi là "Một phần". Lượt build còn dở thì hệ
                thống tự xếp lượt build tiếp (sau khoảng một phút) để đọc nốt, có giới hạn số lần.
              </InfoTip>
            </span>
          }
          description="Số lượt build / poll theo loại nguồn còn dở"
          bodyClassName="space-y-5"
        >
          {runs.incomplete_sources.length ? (
            <IncompleteList items={runs.incomplete_sources.map(([source, count]) => ({ source, count }))} />
          ) : (
            <p className="text-sm text-muted-foreground">Mọi lượt build / poll đều đọc hết nguồn.</p>
          )}
          {runs.errors.length > 0 && (
            <div className="border-t pt-4">
              <div className="mb-2 text-meta font-medium text-muted-foreground">Lỗi hay gặp</div>
              <ul className="space-y-1.5">
                {runs.errors.slice(0, 5).map(([e, n]) => (
                  <li key={e} className="flex items-baseline gap-2 text-meta">
                    <span className="shrink-0 font-semibold tabular-nums">×{num(n)}</span>
                    <code className="min-w-0 rounded bg-muted px-1.5 py-0.5 break-all">{e}</code>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Lượt build / poll 24 giờ qua" description="Theo giờ bắt đầu, chia theo kết quả">
          <StackedColumns data={chartRows(data.chart)} config={runsConfig} x="hour" tick={hour} />
        </Section>
        <Section title="Request 24 giờ qua" description="Mọi request tới website doanh nghiệp, KYC và trang tuyển dụng">
          <StackedColumns data={data.fetch_chart} config={fetchConfig} x="hour" tick={hour} />
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Section title="Lượt build / poll gần đây" description="Bấm một dòng để xem chi tiết lượt" bodyClassName="p-0">
          {data.recent_runs.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Doanh nghiệp</TableHead>
                  <TableHead>Kết quả</TableHead>
                  <TableHead className="text-right">Tin nhận / thấy</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.recent_runs.map((r) => (
                  <TableRow key={r.run_id} {...rowLink(`/runs/${r.run_id}`)}>
                    <TableCell className="max-w-72 min-w-48">
                      <TextLink to={`/companies/${r.company_id}`} className="block truncate font-medium">
                        {companyName({ brand_name: null, legal_name: null, input_name: r.company_name })}
                      </TextLink>
                      <div className="text-meta text-muted-foreground">
                        {cap(ago(r.started_at))} · {duration(r.duration_ms)}
                      </div>
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <RunStatusBadge value={r.status} />
                        <RunTags kind={r.kind} trigger={r.trigger} />
                        <IncompleteSources sources={r.incomplete_sources} />
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      <span className="font-medium">{num(r.jobs_accepted)}</span>
                      <span className="text-muted-foreground"> / {num(r.jobs_found)}</span>
                      {r.jobs_new !== undefined && (
                        <div>
                          <JobDelta added={r.jobs_new} closed={r.jobs_closed ?? 0} />
                        </div>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Chưa có lượt build / poll nào.</Empty>
          )}
        </Section>
        <Section title="Request theo nguồn" description="Trong các lượt build / poll doanh nghiệp" bodyClassName="p-0">
          {st.sources.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Nguồn</TableHead>
                  <TableHead className="text-right">Request</TableHead>
                  <TableHead>Kết quả</TableHead>
                  <TableHead className="text-right">TB</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {st.sources.slice(0, 12).map((s) => (
                  <TableRow key={s.kind}>
                    <TableCell className="font-medium">{sourceLabel(s.kind)}</TableCell>
                    <TableCell className="text-right tabular-nums">{num(s.fetches)}</TableCell>
                    <TableCell className="whitespace-normal">
                      <div className="flex flex-wrap gap-x-2.5 gap-y-1 text-meta">
                        {byCount(s.outcomes).map(([k, v]) => (
                          <Dot key={k} tone={toneOf(k)}>
                            {label(k)} <span className="font-medium tabular-nums">{num(v)}</span>
                          </Dot>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-right text-meta tabular-nums">{duration(s.avg_ms)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Chưa có request nào.</Empty>
          )}
        </Section>
      </div>
    </div>
  )
}

function LlmTab({ llm }: { llm: LlmStats }) {
  const called = llm.calls - llm.cached
  const tokens = llm.prompt_tokens + llm.completion_tokens
  const status = !llm.enabled ? (
    <StatusBadge value={null} tone="warn" text="Chưa cấu hình" />
  ) : llm.paused_until && new Date(llm.paused_until).getTime() > Date.now() ? (
    <StatusBadge value={null} tone="bad" text={`Tạm dừng tới ${clock(llm.paused_until)}`} />
  ) : (
    <StatusBadge value={null} tone="good" text="Hoạt động" />
  )
  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Section
          title="Lời gọi LLM"
          description={`Từ ${dt(llm.since)} · tìm nguồn, bóc tin, chấm tin, phân loại trường, KYC`}
          actions={status}
          bodyClassName="space-y-5"
        >
          <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-4">
            <MiniStat
              label="Lời gọi"
              value={num(llm.calls)}
              sub={llm.per_hour ? `${num(llm.per_hour)} lời gọi/giờ` : undefined}
            />
            <MiniStat
              label="Thành công"
              value={called ? pct((llm.ok - llm.cached) / called) : "–"}
              sub={called ? `${num(llm.failed)} lỗi / ${num(called)} lần gọi` : undefined}
            />
            <MiniStat
              label="Thời gian mỗi lời gọi"
              value={llm.p95_seconds === null ? "–" : `P95 ≤ ${num(llm.p95_seconds)} s`}
              sub={
                llm.p50_seconds === null ? undefined : `P50 ≤ ${num(llm.p50_seconds)} s · TB ${duration(llm.avg_ms)}`
              }
            />
            <MiniStat
              label="Token"
              value={num(tokens)}
              sub={tokens ? `${num(llm.prompt_tokens)} vào · ${num(llm.completion_tokens)} ra` : undefined}
            />
          </div>
          <dl className="grid gap-x-6 gap-y-3 border-t pt-4 text-sm md:grid-cols-2">
            <div>
              <dt className="text-meta text-muted-foreground">Model</dt>
              <dd className="font-mono text-meta break-all">{llm.model ?? "–"}</dd>
            </div>
            <div>
              <dt className="text-meta text-muted-foreground">Dùng lại từ cache</dt>
              <dd className="tabular-nums">
                {num(llm.cached)} câu trả lời{llm.calls ? ` (${pct(llm.cached / llm.calls, 0)} số lời gọi)` : ""}
              </dd>
            </div>
            <div>
              <dt className="inline-flex items-center gap-1 text-meta text-muted-foreground">
                Giới hạn
                <InfoTip>
                  Mọi tiến trình dùng chung giới hạn request/phút. Khi endpoint báo vượt giới hạn, cả hệ thống tạm dừng gọi
                  LLM trong thời gian được yêu cầu; khi báo quá nhiều lời gọi cùng lúc, tiến trình đó tự bớt số lời gọi
                  đồng thời rồi tăng lại dần.
                </InfoTip>
              </dt>
              <dd className="tabular-nums">
                {llm.requests_per_minute ? `${num(llm.requests_per_minute)} request/phút` : "Không giới hạn request/phút"} ·{" "}
                {num(llm.max_concurrency)} lời gọi cùng lúc · chờ tối đa {num(llm.timeout_seconds)} s
              </dd>
            </div>
            <div>
              <dt className="text-meta text-muted-foreground">Bị từ chối vì quá tải (429)</dt>
              <dd className="tabular-nums">
                <span className={llm.throttled ? "text-warning-ink" : undefined}>{num(llm.throttled)} lần</span>
                {called > 0 && ` · ${dec(llm.attempts / called, 2)} lần gửi mỗi lời gọi`}
              </dd>
            </div>
          </dl>
        </Section>

        <Section
          title={
            <span className="inline-flex items-center gap-1.5">
              Lỗi hay gặp
              <InfoTip>
                Lời gọi lỗi (sau khi đã thử lại) được coi là "không chắc": bước đó tự quyết định bằng rule, không dừng lượt
                build / poll.
              </InfoTip>
            </span>
          }
          description="Số lời gọi lỗi theo nguyên nhân"
        >
          <BarList
            items={llm.errors.map(([k, v]) => ({ key: k, label: llmError(k), value: v, hint: k }))}
            total={called}
            empty={llm.calls ? "Không có lời gọi lỗi." : "Chưa có lời gọi nào."}
          />
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Lời gọi LLM 24 giờ qua" description="Theo giờ, chia theo kết quả">
          <StackedColumns data={llm.chart} config={llmConfig} x="hour" tick={hour} />
        </Section>
        <Section title="Token 24 giờ qua" description="Theo giờ, không tính câu trả lời dùng lại từ cache">
          <StackedColumns data={llm.chart} config={tokensConfig} x="hour" tick={hour} />
        </Section>
      </div>

      <Section title="Theo bước" description="Lời gọi trong khoảng đã chọn, theo prompt" bodyClassName="p-0">
        {llm.prompts.length ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Bước</TableHead>
                <TableHead className="text-right">Lời gọi</TableHead>
                <TableHead className="text-right">Dùng lại từ cache</TableHead>
                <TableHead className="text-right">Thành công</TableHead>
                <TableHead className="text-right">Thời gian TB</TableHead>
                <TableHead className="text-right">Token vào / ra</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {llm.prompts.map((p) => {
                const real = p.calls - p.cached
                return (
                  <TableRow key={p.prompt}>
                    <TableCell>
                      <div className="font-medium">{PROMPT_NAMES[p.prompt] ?? p.prompt}</div>
                      <div className="font-mono text-meta text-muted-foreground">{p.prompt}</div>
                    </TableCell>
                    <TableCell className="text-right font-medium tabular-nums">{num(p.calls)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {num(p.cached)}
                      <span className="ml-1.5 text-meta text-muted-foreground">{pct(p.calls ? p.cached / p.calls : null, 0)}</span>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{real ? pct((p.ok - p.cached) / real, 0) : "–"}</TableCell>
                    <TableCell className="text-right tabular-nums">{duration(p.avg_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {num(p.prompt_tokens)}
                      <span className="text-muted-foreground"> / {num(p.completion_tokens)}</span>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        ) : (
          <Empty>Chưa có lời gọi nào.</Empty>
        )}
      </Section>
    </div>
  )
}

function JobsTab({ data }: { data: Overview }) {
  const charts = data.charts
  const total = charts.totals.active_jobs
  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Section title="Việc làm mới theo ngày" description="Theo ngày được thấy lần đầu (tối đa 30 ngày), chia theo nơi đăng">
          <StackedColumns
            data={trimDays(charts.jobs_by_day)}
            config={daysConfig}
            x="day"
            tick={(v) => dayFmt.format(new Date(v))}
            tooltipLabel={(v) => cap(dayLong.format(new Date(v)))}
            height="h-72"
          />
        </Section>
        <Section title="Việc đang mở theo nguồn" description="Nguồn của bản tin chuẩn sau khi gộp trùng">
          <BarList items={bars(charts.jobs_by_source, sourceLabel, (k) => `/jobs?source=${encodeURIComponent(k)}`)} total={total} />
        </Section>
      </div>

      <div className="grid gap-6 lg:grid-cols-2 2xl:grid-cols-3">
        <Section title="Theo tỉnh/thành" description="12 nơi nhiều việc nhất">
          <BarList items={bars(charts.jobs_by_province, (k) => k, (k) => `/jobs?province=${encodeURIComponent(k)}`)} total={total} />
        </Section>
        <Section title="Khoảng lương" description="Mức cao nhất ghi trong tin, VND/tháng">
          <BarList items={bars(charts.salary_bands, (k) => k)} total={total} />
        </Section>
        <Section title="Cấp bậc">
          <BarList items={bars(charts.jobs_by_level, label, (k) => `/jobs?level=${k}`)} total={total} />
        </Section>
        <Section title="Loại hình">
          <BarList items={bars(charts.jobs_by_employment, label, (k) => `/jobs?employment_type=${k}`)} total={total} />
        </Section>
        <Section title="Hình thức làm việc">
          <BarList items={bars(charts.jobs_by_work_mode, label, (k) => `/jobs?work_mode=${k}`)} total={total} />
        </Section>
        <Section title="KYC doanh nghiệp" description="Đối chiếu pháp nhân theo MST / website">
          <ShareBar
            items={charts.kyc.map(([k, v]) => ({
              key: k,
              label: label(k),
              value: v,
              color: KYC_COLORS[k] ?? "var(--viz-muted)",
            }))}
          />
        </Section>
      </div>

      <Section title="Doanh nghiệp nhiều việc nhất" description="Số việc đang mở">
        <BarList
          className="grid gap-x-10 gap-y-2.5 space-y-0 md:grid-cols-2"
          items={charts.top_companies.map((c) => ({
            key: c.id,
            label: displayName(c.name),
            value: c.active_jobs,
            href: `/companies/${c.id}`,
          }))}
          total={total}
        />
      </Section>

      <p className="text-meta text-muted-foreground">
        Số liệu phân bố tính lúc {hour(charts.generated_at)}, làm mới mỗi 5 phút.
      </p>
    </div>
  )
}

function StorageTab({ data, measured, onMeasure }: { data: Overview; measured: boolean; onMeasure: () => void }) {
  const st = data.stats
  return (
    <div className="space-y-6">
      <Section
        title="Dữ liệu và nén"
        description="Các cột lớn lưu nén zstd, kèm từ điển huấn luyện trên chính dữ liệu"
        actions={
          !measured && (
            <Button variant="outline" size="sm" onClick={onMeasure}>
              <Database /> Đo tỉ lệ nén
            </Button>
          )
        }
        bodyClassName="p-0"
      >
        {st.storage.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dữ liệu</TableHead>
                <TableHead className="text-right">Số giá trị</TableHead>
                <TableHead className="text-right">Đang lưu</TableHead>
                <TableHead className="text-right">Gốc (ước tính)</TableHead>
                <TableHead className="text-right">Tỉ lệ nén</TableHead>
                <TableHead>Mẫu theo từ điển</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {st.storage.map((p) => (
                <TableRow key={p.kind}>
                  <TableCell>
                    <div className="font-medium">{STORAGE_NAMES[p.kind] ?? cap(p.kind)}</div>
                    <div className="font-mono text-meta text-muted-foreground">{p.table}</div>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{num(p.values)}</TableCell>
                  <TableCell className="text-right tabular-nums">{bytes(p.stored_bytes)}</TableCell>
                  <TableCell className="text-right tabular-nums">{bytes(p.original_bytes)}</TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {p.stored_bytes ? `×${dec(p.original_bytes / p.stored_bytes, 2)}` : "–"}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(p.by_dict ?? {}).map(([k, n]) => (
                        <Tag key={k}>
                          {k === "0" ? "Không từ điển" : `Từ điển ${k}`} · {num(n)}
                        </Tag>
                      ))}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <Empty>{measured ? "Đang đo tỉ lệ nén…" : "Bấm “Đo tỉ lệ nén” để xem dung lượng các cột nén (mất vài giây)."}</Empty>
        )}
      </Section>

      <Section title="Dung lượng bảng" description="Kể cả chỉ mục, sắp theo dung lượng">
        <BarList
          className="grid gap-x-10 gap-y-2.5 space-y-0 md:grid-cols-2"
          items={st.tables.map(([name, size]) => ({
            key: name,
            label: TABLE_NAMES[name] ?? cap(name),
            value: size,
            hint: name,
          }))}
          format={bytes}
        />
      </Section>
    </div>
  )
}

const LATENCY_RANGES = [15, 60, 360]

export function OverviewPage() {
  const [params, setParams] = useSearchParams()
  // Chỉ nhận các khoảng có trong ô chọn: giá trị lạ trên URL về 24 giờ (không thành "Toàn bộ").
  const hours = WINDOWS.find(([v]) => v === params.get("hours"))?.[0] ?? "24"
  // Khoảng đo độ trễ phát hiện (phút), lưu ở `?latency=` để giữ khi tải lại.
  const latency = LATENCY_RANGES.includes(Number(params.get("latency"))) ? Number(params.get("latency")) : 60
  const [tab, setTab] = useTabParam(TABS, "runs")
  const [storage, setStorage] = useState(false)
  const [auto, setAuto] = useState(false)

  const query = useGetOverview(
    { hours: Number(hours) || 0, storage },
    { query: { refetchInterval: auto ? 30_000 : false, placeholderData: (prev) => prev } },
  )
  // Thẻ "Hàng đợi": số đếm trực tiếp (chung với thanh bên); chưa có thì lấy từ số liệu tổng quan.
  const live = useLiveQueue().query.data
  const maintenance = usePostMaintenance({
    mutation: {
      onSuccess: (r) =>
        r.queued ? toast.success("Đã đưa lượt bảo trì vào hàng đợi") : toast.warning("Đang có lượt bảo trì chờ hoặc đang chạy"),
    },
  })

  const setLatency = (v: number) =>
    setParams(
      (prev) => {
        const out = new URLSearchParams(prev)
        if (v === 60) out.delete("latency")
        else out.set("latency", String(v))
        return out
      },
      { replace: true },
    )

  const setHours = (v: string) =>
    setParams((prev) => {
      const out = new URLSearchParams(prev)
      if (v === "24") out.delete("hours")
      else out.set("hours", v)
      return out
    })

  const data = query.data
  const st = data?.stats
  const runs = st?.runs
  const totals = data?.charts.totals
  const queues = live?.queues ?? st?.queues ?? []
  const active = queues.reduce((n, q) => n + q.active, 0)
  const queued = queues.reduce((n, q) => n + q.queued, 0)
  const workers = queues.reduce((n, q) => n + q.workers, 0)

  return (
    <>
      <PageHeader
        title="Tổng quan"
        description={
          st
            ? `${runs?.since ? `Lượt build / poll từ ${dt(runs.since)}` : "Toàn bộ lượt build / poll"} · cập nhật lúc ${hour(st.generated_at)}`
            : "Đang tải số liệu…"
        }
        actions={
          <>
            <FilterSelect label="Khoảng" value={hours} onChange={setHours} options={WINDOWS} all={null} />
            <label className="flex h-9 cursor-pointer items-center gap-2 rounded-md border border-input bg-card px-3 text-sm shadow-xs">
              <Switch id="auto" checked={auto} onCheckedChange={setAuto} />
              <Label htmlFor="auto" className="cursor-pointer font-normal">
                Tự làm mới
              </Label>
            </label>
            <Button
              variant="outline"
              size="icon"
              onClick={() => query.refetch()}
              disabled={query.isFetching}
              aria-label="Làm mới"
              title="Làm mới"
            >
              <RefreshCw className={query.isFetching ? "animate-spin" : ""} />
            </Button>
            <ConfirmButton
              size="default"
              title="Chạy bảo trì ngay?"
              description="Đóng các lượt build / poll bị treo, xoá dữ liệu quá hạn lưu, huấn luyện từ điển nén và nén lại dữ liệu. Runner hàng đợi build sẽ chạy việc này."
              onConfirm={() => maintenance.mutate()}
            >
              <Wrench /> Bảo trì
            </ConfirmButton>
          </>
        }
      />

      {query.isError && !data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}

      {data && st && runs && totals && (
        <div className="space-y-6">
          <StatGrid className="grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Việc đang mở"
              icon={<Briefcase />}
              value={num(totals.active_jobs)}
              hint={
                <>
                  <span className="font-medium text-success-ink">+{num(totals.new_jobs_7d)}</span> việc mới trong 7 ngày
                </>
              }
            />
            <StatCard
              label="Doanh nghiệp"
              icon={<Building2 />}
              value={num(totals.companies)}
              hint={`${num(totals.companies_with_jobs)} có việc đang mở (${pct(
                totals.companies ? totals.companies_with_jobs / totals.companies : null,
                0,
              )})`}
            />
            <StatCard
              label="Lượt build / poll"
              icon={<Activity />}
              value={num(runs.runs)}
              hint={`${num(runs.companies)} doanh nghiệp${runs.per_hour ? ` · ${num(runs.per_hour)} lượt/giờ` : ""}`}
            />
            <Link
              to="/queue"
              className="rounded-xl transition-shadow hover:[&>div]:border-primary/40 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <StatCard
                className="h-full"
                label="Hàng đợi"
                icon={<Layers />}
                value={num(active + queued)}
                status={
                  !workers
                    ? { tone: "warn", text: "Không có runner" }
                    : active
                      ? { tone: "info", text: "Đang chạy", pulse: true }
                      : undefined
                }
                hint={`${num(active)} đang chạy · ${num(queued)} đang chờ · ${num(workers)} runner`}
              />
            </Link>
          </StatGrid>

          <DetectionSection minutes={latency} onMinutes={setLatency} />

          <Tabs value={tab} onValueChange={setTab} className="gap-6">
            <TabBar
              items={[
                { value: "runs", label: "Lượt build / poll" },
                { value: "jobs", label: "Việc làm" },
                { value: "llm", label: "LLM" },
                { value: "storage", label: "Lưu trữ" },
              ]}
            />
            <TabsContent value="runs">
              <RunsTab data={data} />
            </TabsContent>
            <TabsContent value="jobs">
              <JobsTab data={data} />
            </TabsContent>
            <TabsContent value="llm">
              <LlmTab llm={data.llm} />
            </TabsContent>
            <TabsContent value="storage">
              <StorageTab data={data} measured={storage} onMeasure={() => setStorage(true)} />
            </TabsContent>
          </Tabs>
        </div>
      )}
    </>
  )
}
