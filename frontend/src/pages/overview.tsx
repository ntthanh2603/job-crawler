import { useMutation, useQuery } from "@tanstack/react-query"
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
} from "@/components/common"
import { BarList, ShareBar, StackedColumns, type BarItem } from "@/components/charts"
import { JobDelta, RunTags } from "@/components/crawl"
import { DetectionSection } from "@/components/detection"
import { Button } from "@/components/ui/button"
import type { ChartConfig } from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { api, qs } from "@/lib/api"
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
import type { Overview, Pair } from "@/lib/types"

const WINDOWS = [
  ["1", "1 giờ qua"],
  ["6", "6 giờ qua"],
  ["24", "24 giờ qua"],
  ["168", "7 ngày qua"],
  ["720", "30 ngày qua"],
  ["0", "Toàn bộ"],
] as const

const TABS = ["runs", "jobs", "storage"] as const

// Thứ tự khoá = thứ tự xếp chồng từ dưới lên (đã kiểm định màu kề nhau, xem index.css --viz-*).
const runsConfig = {
  ok: { label: "Thành công", color: "var(--viz-ok)" },
  ambiguous: { label: "Chưa rõ", color: "var(--viz-ambiguous)" },
  partial: { label: "Một phần", color: "var(--viz-partial)" },
  running: { label: "Đang chạy", color: "var(--viz-running)" },
  failed: { label: "Thất bại", color: "var(--viz-failed)" },
} satisfies ChartConfig

const fetchConfig = {
  ok: { label: "Thành công", color: "var(--viz-ok)" },
  error: { label: "Lỗi / quá giờ", color: "var(--viz-ambiguous)" },
  blocked: { label: "Bị chặn", color: "var(--viz-failed)" },
} satisfies ChartConfig

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
  company_runs: "Lượt chạy",
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
          title="Kết quả lượt chạy"
          description="Lượt mới nhất của mỗi doanh nghiệp trong khoảng đã chọn"
          bodyClassName="space-y-5"
        >
          {latest.length ? <ShareBar items={latest} /> : <p className="text-sm text-muted-foreground">Chưa có lượt chạy nào.</p>}
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
                Lượt chạy dừng trước khi đọc hết một nguồn được ghi là "Một phần". Hệ thống tự xếp lượt chạy tiếp
                (sau khoảng một phút) để đọc nốt, có giới hạn số lần.
              </InfoTip>
            </span>
          }
          description="Số lượt chạy theo loại nguồn còn dở"
          bodyClassName="space-y-5"
        >
          {runs.incomplete_sources.length ? (
            <IncompleteList items={runs.incomplete_sources.map(([source, count]) => ({ source, count }))} />
          ) : (
            <p className="text-sm text-muted-foreground">Mọi lượt chạy đều đọc hết nguồn.</p>
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
        <Section title="Lượt chạy 24 giờ qua" description="Theo giờ bắt đầu, chia theo kết quả">
          <StackedColumns data={chartRows(data.chart)} config={runsConfig} x="hour" tick={hour} />
        </Section>
        <Section title="Request 24 giờ qua" description="Mọi request tới website doanh nghiệp, KYC và trang tuyển dụng">
          <StackedColumns data={data.fetch_chart} config={fetchConfig} x="hour" tick={hour} />
        </Section>
      </div>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Section title="Lượt chạy gần đây" description="Bấm một dòng để xem chi tiết lượt chạy" bodyClassName="p-0">
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
                        <StatusBadge value={r.status} />
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
            <Empty>Chưa có lượt chạy nào.</Empty>
          )}
        </Section>
        <Section title="Request theo nguồn" description="Trong các lượt chạy doanh nghiệp" bodyClassName="p-0">
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
  const hours = params.get("hours") ?? "24"
  // Khoảng đo độ trễ phát hiện (phút), lưu ở `?latency=` để giữ khi tải lại.
  const latency = LATENCY_RANGES.includes(Number(params.get("latency"))) ? Number(params.get("latency")) : 60
  const [tab, setTab] = useTabParam(TABS, "runs")
  const [storage, setStorage] = useState(false)
  const [auto, setAuto] = useState(false)

  const query = useQuery({
    queryKey: ["overview", hours, storage],
    queryFn: () => api.get<Overview>(`/overview${qs({ hours, storage })}`),
    refetchInterval: auto ? 30_000 : false,
    placeholderData: (prev) => prev,
  })
  const maintenance = useMutation({
    mutationFn: () => api.post<{ queued: boolean }>("/maintenance"),
    onSuccess: (r) =>
      r.queued ? toast.success("Đã đưa lượt bảo trì vào hàng đợi") : toast.warning("Đang có lượt bảo trì chờ hoặc đang chạy"),
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
  const queues = st?.queues ?? []
  const active = queues.reduce((n, q) => n + q.active, 0)
  const queued = queues.reduce((n, q) => n + q.queued, 0)
  const workers = queues.reduce((n, q) => n + q.workers, 0)

  return (
    <>
      <PageHeader
        title="Tổng quan"
        description={
          st
            ? `${runs?.since ? `Lượt chạy từ ${dt(runs.since)}` : "Toàn bộ lượt chạy"} · cập nhật lúc ${hour(st.generated_at)}`
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
              description="Đóng các lượt chạy bị treo, xoá dữ liệu quá hạn lưu, huấn luyện từ điển nén và nén lại dữ liệu. Runner hàng đợi build sẽ chạy việc này."
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
              label="Lượt chạy"
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
                { value: "runs", label: "Lượt chạy" },
                { value: "jobs", label: "Việc làm" },
                { value: "storage", label: "Lưu trữ" },
              ]}
            />
            <TabsContent value="runs">
              <RunsTab data={data} />
            </TabsContent>
            <TabsContent value="jobs">
              <JobsTab data={data} />
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
