import { useMutation, useQuery } from "@tanstack/react-query"
import { Activity, Briefcase, Building2, Database, Layers, RefreshCw, Wrench } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router"
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
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
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, byCount, bytes, cap, companyName, dec, dt, duration, hour, label, num, pct, sourceLabel } from "@/lib/format"
import type { Overview } from "@/lib/types"

const WINDOWS = [
  ["1", "1 giờ qua"],
  ["6", "6 giờ qua"],
  ["24", "24 giờ qua"],
  ["168", "7 ngày qua"],
  ["720", "30 ngày qua"],
  ["0", "Toàn bộ"],
] as const

const chartConfig = {
  ok: { label: "Thành công", color: "var(--success)" },
  partial: { label: "Một phần", color: "var(--warning)" },
  ambiguous: { label: "Chưa rõ", color: "var(--neutral-tone)" },
  failed: { label: "Thất bại", color: "var(--destructive)" },
  running: { label: "Đang chạy", color: "var(--info)" },
} satisfies ChartConfig

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
            <Select value={hours} onValueChange={(v) => setParams({ hours: v })}>
              <SelectTrigger size="sm" className="w-36 bg-card" aria-label="Khoảng thời gian">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WINDOWS.map(([v, text]) => (
                  <SelectItem key={v} value={v}>
                    {text}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="flex h-8 items-center gap-2 rounded-md border bg-card px-2.5">
              <Switch id="auto" checked={auto} onCheckedChange={setAuto} />
              <Label htmlFor="auto" className="text-xs font-normal">
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

      {st && runs && data && (
        <div className="space-y-6">
          <StatGrid className="grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
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
              label="Lượt chạy"
              icon={<Activity />}
              value={num(runs.runs)}
              hint={`${num(runs.companies)} doanh nghiệp${runs.per_hour ? ` · ${num(runs.per_hour)} lượt/giờ` : ""}`}
            />
            <StatCard
              label="Việc làm được nhận"
              icon={<Briefcase />}
              value={num(runs.jobs_accepted)}
              hint={`Trên ${num(runs.jobs_found)} tin · ${num(runs.companies_with_jobs)} doanh nghiệp có việc`}
            />
            <StatCard
              label="Việc làm đang mở"
              icon={<Building2 />}
              value={num(st.jobs_by_status.active ?? 0)}
              hint={`${num(st.companies)} doanh nghiệp trong hệ thống`}
            />
            <StatCard
              label="Hàng đợi"
              icon={<Layers />}
              value={
                <span>
                  {num(st.queues.reduce((n, q) => n + q.queued, 0))}{" "}
                  <span className="text-sm font-normal text-muted-foreground">đang chờ</span>
                </span>
              }
              hint={
                <span className="flex flex-col gap-0.5">
                  {st.queues.map((q) => (
                    <span key={q.name}>
                      {q.error
                        ? `${cap(q.name)}: lỗi kết nối`
                        : `${q.name.includes("board") ? "Index" : "Doanh nghiệp"}: ${q.active} chạy · ${q.workers} worker`}
                    </span>
                  ))}
                </span>
              }
            />
          </StatGrid>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <Section title="Lượt chạy 24 giờ qua" description="Theo giờ bắt đầu, chia theo kết quả">
              <ChartContainer config={chartConfig} className="aspect-auto h-64 w-full">
                <BarChart data={data.chart.map((b) => ({ hour: hour(b.hour), ...b.counts }))} margin={{ left: 0, right: 4, top: 4 }}>
                  <CartesianGrid vertical={false} strokeDasharray="3 3" />
                  <XAxis dataKey="hour" tickLine={false} axisLine={false} interval={2} fontSize={11} tickMargin={8} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} fontSize={11} width={36} tickMargin={4} />
                  <ChartTooltip cursor={{ fillOpacity: 0.06 }} content={<ChartTooltipContent />} />
                  <ChartLegend content={<ChartLegendContent />} />
                  {Object.keys(chartConfig).map((key, i, all) => (
                    <Bar
                      key={key}
                      dataKey={key}
                      stackId="runs"
                      fill={`var(--color-${key})`}
                      radius={i === all.length - 1 ? [4, 4, 0, 0] : 0}
                      maxBarSize={28}
                    />
                  ))}
                </BarChart>
              </ChartContainer>
            </Section>

            <Section title="Kết quả lượt chạy">
              <dl className="grid grid-cols-[minmax(7.5rem,max-content)_1fr] gap-x-4 gap-y-3.5 text-sm">
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
                        <div key={e} className="text-xs">
                          ×{n} <code className="rounded bg-muted px-1 py-0.5">{e}</code>
                        </div>
                      ))
                    : "Không có"}
                </dd>
                <dt className="text-muted-foreground">KYC</dt>
                <dd className="flex flex-wrap gap-1">
                  {byCount(st.kyc_status).map(([k, v]) => (
                    <Tag key={k}>
                      {label(k)} · {num(v)}
                    </Tag>
                  ))}
                </dd>
                <dt className="text-muted-foreground">Việc làm theo nguồn</dt>
                <dd className="flex flex-wrap gap-1">
                  {st.jobs_by_source.slice(0, 10).map(([s, n]) => (
                    <Link key={s} to={`/jobs?source=${s}`} className="rounded-md transition-opacity hover:opacity-80">
                      <Tag>
                        {sourceLabel(s)} · {num(n)}
                      </Tag>
                    </Link>
                  ))}
                </dd>
              </dl>
            </Section>
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <Section title="Lượt chạy gần đây" bodyClassName="p-0">
              {data.recent_runs.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Doanh nghiệp</TableHead>
                      <TableHead>Kết quả</TableHead>
                      <TableHead className="text-right">Thời gian</TableHead>
                      <TableHead className="text-right">Việc làm</TableHead>
                      <TableHead className="pr-4">Bắt đầu</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.recent_runs.map((r) => (
                      <TableRow key={r.run_id}>
                        <TableCell className="max-w-64 truncate pl-4">
                          <TextLink to={`/companies/${r.company_id}`}>
                            {companyName({ brand_name: null, legal_name: null, input_name: r.company_name })}
                          </TextLink>
                        </TableCell>
                        <TableCell>
                          <Link to={`/runs/${r.run_id}`} className="transition-opacity hover:opacity-80">
                            <StatusBadge value={r.status} />
                          </Link>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{duration(r.duration_ms)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.jobs_accepted}/{r.jobs_found}
                        </TableCell>
                        <TableCell className="pr-4 text-muted-foreground">{cap(ago(r.started_at))}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có lượt chạy nào.</Empty>
              )}
            </Section>

            <Section title="Nguồn" description="Request trong các lượt chạy doanh nghiệp" bodyClassName="p-0">
              {st.sources.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Nguồn</TableHead>
                      <TableHead className="text-right">Request</TableHead>
                      <TableHead>Kết quả</TableHead>
                      <TableHead className="pr-4 text-right">Trung bình</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {st.sources.slice(0, 12).map((s) => (
                      <TableRow key={s.kind}>
                        <TableCell className="pl-4 font-medium">{sourceLabel(s.kind)}</TableCell>
                        <TableCell className="text-right tabular-nums">{num(s.fetches)}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {byCount(s.outcomes).map(([k, v]) => (
                              <StatusBadge key={k} value={k} text={`${label(k)} · ${num(v)}`} />
                            ))}
                          </div>
                        </TableCell>
                        <TableCell className="pr-4 text-right tabular-nums">{duration(s.avg_ms)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có request nào.</Empty>
              )}
            </Section>
          </div>

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
                      <TableHead className="pl-4">Dữ liệu</TableHead>
                      <TableHead className="text-right">Số giá trị</TableHead>
                      <TableHead className="text-right">Đang lưu</TableHead>
                      <TableHead className="text-right">Gốc (ước tính)</TableHead>
                      <TableHead className="text-right">Tỉ lệ nén</TableHead>
                      <TableHead className="pr-4">Mẫu theo từ điển</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {st.storage.map((p) => (
                      <TableRow key={p.kind}>
                        <TableCell className="pl-4">
                          {STORAGE_NAMES[p.kind] ?? cap(p.kind)}
                          <div className="font-mono text-xs text-muted-foreground">{p.table}</div>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{num(p.values)}</TableCell>
                        <TableCell className="text-right tabular-nums">{bytes(p.stored_bytes)}</TableCell>
                        <TableCell className="text-right tabular-nums">{bytes(p.original_bytes)}</TableCell>
                        <TableCell className="text-right font-medium tabular-nums">
                          {p.stored_bytes ? `x${dec(p.original_bytes / p.stored_bytes, 2)}` : "–"}
                        </TableCell>
                        <TableCell className="pr-4">
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
              <div className="mb-2 text-xs font-medium text-muted-foreground">Dung lượng bảng</div>
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
