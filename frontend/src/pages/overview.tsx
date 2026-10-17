import { useMutation, useQuery } from "@tanstack/react-query"
import { Activity, Briefcase, Building2, Clock, Gauge, Layers, RefreshCw, ShieldAlert, Wrench } from "lucide-react"
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
  StatusBadge,
  Tag,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, bytes, dt, duration, hour, label, num, pct } from "@/lib/format"
import type { Overview } from "@/lib/types"

const WINDOWS = [
  ["1", "1 giờ"],
  ["6", "6 giờ"],
  ["24", "24 giờ"],
  ["168", "7 ngày"],
  ["720", "30 ngày"],
  ["0", "Tất cả"],
] as const

const chartConfig = {
  ok: { label: "ok", color: "var(--chart-1)" },
  partial: { label: "partial", color: "var(--chart-2)" },
  ambiguous: { label: "ambiguous", color: "var(--chart-3)" },
  failed: { label: "failed", color: "var(--chart-4)" },
  running: { label: "đang chạy", color: "var(--chart-5)" },
} satisfies ChartConfig

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
      r.queued
        ? toast.success("Đã đưa lượt bảo trì vào queue")
        : toast.warning("Đang có lượt bảo trì chờ/chạy"),
    onError: (e) => toast.error(String(e)),
  })

  const data = query.data
  const st = data?.stats
  const runs = st?.runs

  return (
    <>
      <PageHeader
        title="Tổng quan"
        description={
          st ? (
            <>
              {runs?.since ? `Lượt chạy từ ${dt(runs.since)}` : "Toàn bộ lượt chạy"} · cập nhật {hour(st.generated_at)}
            </>
          ) : (
            "Đang tải…"
          )
        }
        actions={
          <>
            <Select value={hours} onValueChange={(v) => setParams({ hours: v })}>
              <SelectTrigger size="sm" className="w-32">
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
            <div className="flex items-center gap-2 rounded-md border px-2.5 py-1.5">
              <Switch id="auto" checked={auto} onCheckedChange={setAuto} />
              <Label htmlFor="auto" className="text-xs">
                Tự tải lại 30 s
              </Label>
            </div>
            <Button variant="outline" size="sm" onClick={() => query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={query.isFetching ? "animate-spin" : ""} />
            </Button>
            <ConfirmButton
              title="Chạy bảo trì ngay?"
              description="Đóng các lượt chạy treo, xoá dữ liệu cũ quá hạn lưu, huấn luyện từ điển nén và nén lại dữ liệu. Worker doanh nghiệp sẽ chạy job này."
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
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            {st.targets.map((t) => (
              <StatCard
                key={t.name}
                label={t.name}
                icon={t.name.includes("tỉ lệ") ? <ShieldAlert /> : <Gauge />}
                tone={t.ok === null ? undefined : t.ok ? "good" : "bad"}
                value={t.value === null ? "–" : t.name.includes("tỉ lệ") ? pct(t.value) : `${Math.round(t.value)} s`}
                hint={`mục tiêu ${t.target} · ${t.ok === null ? "chưa có dữ liệu" : t.ok ? "đạt" : "chưa đạt"}`}
              />
            ))}
            <StatCard
              label="Lượt chạy"
              icon={<Activity />}
              value={num(runs.runs)}
              hint={`${num(runs.companies)} doanh nghiệp${runs.per_hour ? ` · ${num(runs.per_hour)}/giờ` : ""}`}
            />
            <StatCard
              label="Job nhận / tìm thấy"
              icon={<Briefcase />}
              value={num(runs.jobs_accepted)}
              hint={`/ ${num(runs.jobs_found)} tin · ${runs.companies_with_jobs} DN có job`}
            />
            {st.queues.map((q) => (
              <StatCard
                key={q.name}
                label={`Queue ${q.name}`}
                icon={<Layers />}
                tone={q.error ? "bad" : undefined}
                value={
                  <>
                    {num(q.queued)} <span className="text-sm font-normal text-muted-foreground">chờ</span>
                  </>
                }
                hint={q.error ? `lỗi: ${q.error}` : `${q.active} đang chạy · ${q.workers} worker`}
              />
            ))}
            <StatCard
              label="Job đang mở"
              icon={<Building2 />}
              value={num(st.jobs_by_status.active ?? 0)}
              hint={`${num(st.companies)} doanh nghiệp trong DB`}
            />
          </div>

          <div className="grid gap-6 xl:grid-cols-[3fr_2fr]">
            <Section title="Lượt chạy 24 giờ qua" description="Theo giờ bắt đầu, xếp chồng theo trạng thái">
              <ChartContainer config={chartConfig} className="aspect-auto h-64 w-full">
                <BarChart data={data.chart.map((b) => ({ hour: hour(b.hour), ...b.counts }))} margin={{ left: -16 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="hour" tickLine={false} axisLine={false} interval={2} fontSize={11} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} fontSize={11} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <ChartLegend content={<ChartLegendContent />} />
                  {Object.keys(chartConfig).map((key, i, all) => (
                    <Bar
                      key={key}
                      dataKey={key}
                      stackId="runs"
                      fill={`var(--color-${key})`}
                      radius={i === all.length - 1 ? [3, 3, 0, 0] : 0}
                    />
                  ))}
                </BarChart>
              </ChartContainer>
            </Section>

            <Section title="Lượt chạy">
              <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-3 text-sm">
                <dt className="text-muted-foreground">Theo trạng thái</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {Object.entries(runs.by_status).map(([k, v]) => (
                    <StatusBadge key={k} value={k} text={`${label(k)} ${num(v)}`} />
                  ))}
                </dd>
                <dt className="text-muted-foreground">Mới nhất mỗi DN</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {Object.entries(runs.latest_by_status).map(([k, v]) => (
                    <StatusBadge key={k} value={k} text={`${label(k)} ${num(v)}`} />
                  ))}
                </dd>
                <dt className="text-muted-foreground">Thời gian</dt>
                <dd className="tabular-nums">
                  p50 {duration(runs.p50_seconds === null ? null : runs.p50_seconds * 1000)} · p95{" "}
                  {duration(runs.p95_seconds === null ? null : runs.p95_seconds * 1000)} · max{" "}
                  {duration(runs.max_seconds === null ? null : runs.max_seconds * 1000)} · quá timeout {runs.over_timeout}
                </dd>
                <dt className="text-muted-foreground">Nguồn chưa xong</dt>
                <dd className="flex flex-wrap gap-1">
                  {runs.incomplete_sources.length ? runs.incomplete_sources.map(([s, n]) => <Tag key={s}>{s} {n}</Tag>) : "–"}
                </dd>
                <dt className="text-muted-foreground">Lỗi hay gặp</dt>
                <dd className="space-y-1">
                  {runs.errors.length
                    ? runs.errors.slice(0, 4).map(([e, n]) => (
                        <div key={e} className="text-xs">
                          ×{n} <code className="rounded bg-muted px-1">{e}</code>
                        </div>
                      ))
                    : "–"}
                </dd>
                <dt className="text-muted-foreground">KYC</dt>
                <dd className="flex flex-wrap gap-1">
                  {Object.entries(st.kyc_status).map(([k, v]) => (
                    <Tag key={k}>
                      {label(k)} {num(v)}
                    </Tag>
                  ))}
                </dd>
                <dt className="text-muted-foreground">Job theo nguồn</dt>
                <dd className="flex flex-wrap gap-1">
                  {st.jobs_by_source.slice(0, 10).map(([s, n]) => (
                    <Link key={s} to={`/jobs?source=${s}`}>
                      <Tag className="hover:bg-accent">
                        {s} {num(n)}
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
                      <TableHead>Trạng thái</TableHead>
                      <TableHead className="text-right">Thời gian</TableHead>
                      <TableHead className="text-right">Job</TableHead>
                      <TableHead className="pr-4">Lúc</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.recent_runs.map((r) => (
                      <TableRow key={r.run_id}>
                        <TableCell className="max-w-56 truncate pl-4">
                          <Link to={`/companies/${r.company_id}`} className="hover:underline">
                            {r.company_name}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Link to={`/runs/${r.run_id}`}>
                            <StatusBadge value={r.status} />
                          </Link>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{duration(r.duration_ms)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.jobs_accepted}/{r.jobs_found}
                        </TableCell>
                        <TableCell className="pr-4 text-muted-foreground">{ago(r.started_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có lượt chạy.</Empty>
              )}
            </Section>

            <Section title="Nguồn" description="Request trong các lượt chạy doanh nghiệp" bodyClassName="p-0">
              {st.sources.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Loại</TableHead>
                      <TableHead className="text-right">Request</TableHead>
                      <TableHead>Kết quả</TableHead>
                      <TableHead className="pr-4 text-right">TB</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {st.sources.slice(0, 12).map((s) => (
                      <TableRow key={s.kind}>
                        <TableCell className="pl-4 font-medium">{s.kind}</TableCell>
                        <TableCell className="text-right tabular-nums">{num(s.fetches)}</TableCell>
                        <TableCell>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(s.outcomes).map(([k, v]) => (
                              <StatusBadge key={k} value={k} text={`${k} ${num(v)}`} />
                            ))}
                          </div>
                        </TableCell>
                        <TableCell className="pr-4 text-right tabular-nums">{duration(s.avg_ms)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có request.</Empty>
              )}
            </Section>
          </div>

          <Section
            title="Dữ liệu và nén"
            description="Cột lớn lưu nén zstd (kèm từ điển huấn luyện trên dữ liệu)"
            actions={
              !storage && (
                <Button variant="outline" size="sm" onClick={() => setStorage(true)}>
                  <Clock /> Đo tỉ lệ nén
                </Button>
              )
            }
            bodyClassName="space-y-4"
          >
            {st.storage.length > 0 && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Cột</TableHead>
                    <TableHead className="text-right">Giá trị</TableHead>
                    <TableHead className="text-right">Lưu</TableHead>
                    <TableHead className="text-right">Gốc (ước tính)</TableHead>
                    <TableHead className="text-right">Tỉ lệ</TableHead>
                    <TableHead>Theo từ điển (mẫu)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {st.storage.map((p) => (
                    <TableRow key={p.kind}>
                      <TableCell>
                        <code className="text-xs">{p.table}</code> · {p.kind}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{num(p.values)}</TableCell>
                      <TableCell className="text-right tabular-nums">{bytes(p.stored_bytes)}</TableCell>
                      <TableCell className="text-right tabular-nums">{bytes(p.original_bytes)}</TableCell>
                      <TableCell className="text-right font-medium tabular-nums">
                        {p.stored_bytes ? `x${(p.original_bytes / p.stored_bytes).toFixed(2)}` : "–"}
                      </TableCell>
                      <TableCell className="flex flex-wrap gap-1">
                        {Object.entries(p.by_dict ?? {}).map(([k, n]) => (
                          <Tag key={k}>
                            {k === "0" ? "không từ điển" : k}: {n}
                          </Tag>
                        ))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <div className="flex flex-wrap gap-1.5">
              {st.tables.map(([name, size]) => (
                <Tag key={name}>
                  {name} {bytes(size)}
                </Tag>
              ))}
            </div>
          </Section>
        </div>
      )}
    </>
  )
}
