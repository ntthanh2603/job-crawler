import { useMutation, useQuery } from "@tanstack/react-query"
import { Activity, Briefcase, Building2, Database, Layers, RefreshCw, Wrench } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  FilterSelect,
  Loading,
  PageHeader,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  Tag,
  TextLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { BarList, ShareBar, StackedColumns, type BarItem } from "@/components/charts"
import type { ChartConfig } from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
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

function seconds(value: number | null) {
  return duration(value === null ? null : value * 1000)
}

export function OverviewPage() {
  const [params, setParams] = useSearchParams()
  const hours = params.get("hours") ?? "24"
  const [storage, setStorage] = useState(false)
  const [auto, setAuto] = useState(false)

  const query = useQuery({
    queryKey: ["overview", hours, storage],
    queryFn: () => api.get<Overview>(`/overview${qs({ hours, storage })}`),
    refetchInterval: auto ? 30_000 : false,
  })
  const maintenance = useMutation({
    mutationFn: () => api.post<{ queued: boolean }>("/maintenance"),
    onSuccess: (r) =>
      r.queued ? toast.success("Đã đưa lượt bảo trì vào hàng đợi") : toast.warning("Đang có lượt bảo trì chờ hoặc đang chạy"),
    onError: (e) => toast.error(cap(String(e))),
  })

  const data = query.data
  const st = data?.stats
  const runs = st?.runs
  const [p95, failed] = st?.targets ?? []
  const charts = data?.charts

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
            <FilterSelect label="Khoảng" value={hours} onChange={(v) => setParams({ hours: v })} options={WINDOWS} all={null} />
            <div className="flex h-8 items-center gap-2 rounded-md border bg-card px-2.5 shadow-xs">
              <Switch id="auto" checked={auto} onCheckedChange={setAuto} />
              <Label htmlFor="auto" className="text-sm font-normal">
                Tự làm mới
              </Label>
            </div>
            <Button
              variant="outline"
              size="icon-sm"
              onClick={() => query.refetch()}
              disabled={query.isFetching}
              aria-label="Làm mới"
            >
              <RefreshCw className={query.isFetching ? "animate-spin" : ""} />
            </Button>
            <ConfirmButton
              title="Chạy bảo trì ngay?"
              description="Đóng các lượt chạy bị treo, xoá dữ liệu quá hạn lưu, huấn luyện từ điển nén và nén lại dữ liệu. Worker doanh nghiệp sẽ chạy việc này."
              onConfirm={() => maintenance.mutate()}
            >
              <Wrench /> Bảo trì
            </ConfirmButton>
          </>
        }
      />

      {query.isError && <ErrorState error={query.error} />}
      {query.isLoading && <Loading rows={8} />}

      {st && runs && data && charts && (
        <div className="space-y-6">
          <StatGrid className="grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
            <StatCard
              label="Việc đang mở"
              icon={<Briefcase />}
              value={num(charts.totals.active_jobs)}
              hint={`+${num(charts.totals.new_jobs_7d)} việc mới trong 7 ngày`}
            />
            <StatCard
              label="Doanh nghiệp"
              icon={<Building2 />}
              value={num(charts.totals.companies)}
              hint={`${num(charts.totals.companies_with_jobs)} doanh nghiệp có việc đang mở`}
            />
            <StatCard
              label="Lượt chạy"
              icon={<Activity />}
              value={num(runs.runs)}
              hint={`${num(runs.companies)} doanh nghiệp${runs.per_hour ? ` · ${num(runs.per_hour)} lượt/giờ` : ""}`}
            />
            {p95 && (
              <StatCard
                label="P95 mỗi doanh nghiệp"
                value={p95.value === null ? "–" : `${Math.round(p95.value)} s`}
                hint={`Mục tiêu ${p95.target}`}
                status={p95.ok === null ? undefined : { tone: p95.ok ? "good" : "bad", text: p95.ok ? "Đạt" : "Chưa đạt" }}
              />
            )}
            {failed && (
              <StatCard
                label="Tỉ lệ thất bại"
                value={pct(failed.value)}
                hint={`Theo lượt mới nhất · mục tiêu ${failed.target}`}
                status={
                  failed.ok === null ? undefined : { tone: failed.ok ? "good" : "bad", text: failed.ok ? "Đạt" : "Chưa đạt" }
                }
              />
            )}
            <StatCard
              label="Hàng đợi"
              icon={<Layers />}
              value={num(st.queues.reduce((n, q) => n + q.queued, 0))}
              hint={st.queues
                .map((q) => `${q.name.includes("board") ? "Index" : "Doanh nghiệp"}: ${q.active} chạy · ${q.workers} worker`)
                .join(" · ")}
            />
          </StatGrid>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <Section title="Việc làm mới 30 ngày" description="Theo ngày được thấy lần đầu, chia theo nơi đăng">
              <StackedColumns
                data={charts.jobs_by_day}
                config={daysConfig}
                x="day"
                tick={(v) => dayFmt.format(new Date(v))}
                tooltipLabel={(v) => cap(dayLong.format(new Date(v)))}
              />
            </Section>
            <Section title="Việc đang mở theo nguồn" description="Nguồn của bản tin chuẩn sau khi gộp trùng">
              <BarList
                items={bars(charts.jobs_by_source, sourceLabel, (k) => `/jobs?source=${encodeURIComponent(k)}`)}
                total={charts.totals.active_jobs}
              />
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

          <div className="grid gap-6 lg:grid-cols-2 2xl:grid-cols-3">
            <Section title="Theo tỉnh/thành" description="Việc đang mở, 12 nơi nhiều nhất">
              <BarList
                items={bars(charts.jobs_by_province, (k) => k, (k) => `/jobs?province=${encodeURIComponent(k)}`)}
                total={charts.totals.active_jobs}
              />
            </Section>
            <Section title="Khoảng lương" description="Mức cao nhất ghi trong tin, VND/tháng">
              <BarList items={bars(charts.salary_bands, (k) => k)} total={charts.totals.active_jobs} />
            </Section>
            <Section title="Cấp bậc">
              <BarList
                items={bars(charts.jobs_by_level, label, (k) => `/jobs?level=${k}`)}
                total={charts.totals.active_jobs}
              />
            </Section>
            <Section title="Loại hình">
              <BarList
                items={bars(charts.jobs_by_employment, label, (k) => `/jobs?employment_type=${k}`)}
                total={charts.totals.active_jobs}
              />
            </Section>
            <Section title="Hình thức làm việc">
              <BarList
                items={bars(charts.jobs_by_work_mode, label, (k) => `/jobs?work_mode=${k}`)}
                total={charts.totals.active_jobs}
              />
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

          <div className="grid gap-6 xl:grid-cols-2">
            <Section title="Doanh nghiệp nhiều việc nhất" description="Số việc đang mở">
              <BarList
                items={charts.top_companies.map((c) => ({
                  key: c.id,
                  label: displayName(c.name),
                  value: c.active_jobs,
                  href: `/companies/${c.id}`,
                }))}
                total={charts.totals.active_jobs}
              />
            </Section>
            <Section title="Lượt chạy gần đây" bodyClassName="p-0">
              {data.recent_runs.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Doanh nghiệp</TableHead>
                      <TableHead>Kết quả</TableHead>
                      <TableHead className="text-right">Việc làm</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.recent_runs.map((r) => (
                      <TableRow key={r.run_id}>
                        <TableCell className="max-w-72 min-w-0">
                          <TextLink to={`/companies/${r.company_id}`} className="block truncate">
                            {companyName({ brand_name: null, legal_name: null, input_name: r.company_name })}
                          </TextLink>
                          <div className="text-meta text-muted-foreground">{cap(ago(r.started_at))}</div>
                        </TableCell>
                        <TableCell>
                          <Link to={`/runs/${r.run_id}`} className="transition-opacity hover:opacity-80">
                            <StatusBadge value={r.status} />
                          </Link>
                          <div className="text-meta text-muted-foreground tabular-nums">{duration(r.duration_ms)}</div>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.jobs_accepted}/{r.jobs_found}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có lượt chạy nào.</Empty>
              )}
            </Section>

          </div>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <Section title="Kết quả lượt chạy">
              <dl className="grid grid-cols-[minmax(8rem,max-content)_1fr] gap-x-5 gap-y-4 text-sm">
                <dt className="text-muted-foreground">Theo kết quả</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {byCount(runs.by_status).map(([k, v]) => (
                    <StatusBadge key={k} value={k} text={`${label(k)} · ${num(v)}`} />
                  ))}
                </dd>
                <dt className="text-muted-foreground">Mới nhất mỗi DN</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {byCount(runs.latest_by_status).map(([k, v]) => (
                    <StatusBadge key={k} value={k} text={`${label(k)} · ${num(v)}`} />
                  ))}
                </dd>
                <dt className="text-muted-foreground">Thời gian</dt>
                <dd className="tabular-nums">
                  P50 {seconds(runs.p50_seconds)} · P95 {seconds(runs.p95_seconds)} · tối đa {seconds(runs.max_seconds)}
                  {runs.over_timeout > 0 && <span className="text-destructive"> · {runs.over_timeout} quá giờ</span>}
                </dd>
                <dt className="text-muted-foreground">Nguồn chưa xong</dt>
                <dd className="flex flex-wrap gap-1">
                  {runs.incomplete_sources.length
                    ? runs.incomplete_sources.map(([s, n]) => (
                        <Tag key={s}>
                          {sourceLabel(s)} · {n}
                        </Tag>
                      ))
                    : "Không có"}
                </dd>
                <dt className="text-muted-foreground">Lỗi hay gặp</dt>
                <dd className="space-y-1">
                  {runs.errors.length
                    ? runs.errors.slice(0, 4).map(([e, n]) => (
                        <div key={e} className="text-meta">
                          ×{n} <code className="rounded bg-muted px-1 py-0.5">{e}</code>
                        </div>
                      ))
                    : "Không có"}
                </dd>
              </dl>
            </Section>
            <Section title="Nguồn" description="Request trong các lượt chạy doanh nghiệp" bodyClassName="p-0">
              {st.sources.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Nguồn</TableHead>
                      <TableHead className="text-right">Request</TableHead>
                      <TableHead>Kết quả</TableHead>
                      <TableHead className="text-right">Trung bình</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {st.sources.slice(0, 12).map((s) => (
                      <TableRow key={s.kind}>
                        <TableCell className="font-medium">{sourceLabel(s.kind)}</TableCell>
                        <TableCell className="text-right tabular-nums">{num(s.fetches)}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {byCount(s.outcomes).map(([k, v]) => (
                              <StatusBadge key={k} value={k} text={`${label(k)} · ${num(v)}`} />
                            ))}
                          </div>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{duration(s.avg_ms)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có request nào.</Empty>
              )}
            </Section>
          </div>

          <p className="text-meta text-muted-foreground">
            Số liệu phân bố tính lúc {hour(charts.generated_at)}, làm mới mỗi 5 phút.
          </p>

          <Section
            title="Dữ liệu và nén"
            description="Các cột lớn lưu nén zstd, kèm từ điển huấn luyện trên chính dữ liệu"
            actions={
              !storage && (
                <Button variant="outline" size="sm" onClick={() => setStorage(true)}>
                  <Database /> Đo tỉ lệ nén
                </Button>
              )
            }
            bodyClassName="space-y-4"
          >
            {st.storage.length > 0 && (
              <div className="overflow-hidden rounded-lg border">
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
                          {STORAGE_NAMES[p.kind] ?? cap(p.kind)}
                          <div className="font-mono text-meta text-muted-foreground">{p.table}</div>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{num(p.values)}</TableCell>
                        <TableCell className="text-right tabular-nums">{bytes(p.stored_bytes)}</TableCell>
                        <TableCell className="text-right tabular-nums">{bytes(p.original_bytes)}</TableCell>
                        <TableCell className="text-right font-medium tabular-nums">
                          {p.stored_bytes ? `x${dec(p.original_bytes / p.stored_bytes, 2)}` : "–"}
                        </TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(p.by_dict ?? {}).map(([k, n]) => (
                              <Tag key={k}>
                                {k === "0" ? "Không từ điển" : `Từ điển ${k}`} · {n}
                              </Tag>
                            ))}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            <div>
              <div className="mb-2 text-meta font-medium text-foreground/70">Dung lượng bảng</div>
              <div className="flex flex-wrap gap-1.5">
                {st.tables.map(([name, size]) => (
                  <Tag key={name} title={name}>
                    {TABLE_NAMES[name] ?? cap(name)} · {bytes(size)}
                  </Tag>
                ))}
              </div>
            </div>
          </Section>
        </div>
      )}
    </>
  )
}
