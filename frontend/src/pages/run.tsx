import { useQuery } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { useParams } from "react-router"

import {
  Empty,
  ErrorState,
  ExternalLink,
  Loading,
  PagedList,
  PageHeader,
  Section,
  StatusBadge,
  TextLink,
  type Tone,
} from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { byCount, cap, companyName, dt, duration, failReason, hour, label, num, sourceLabel } from "@/lib/format"
import type { Fetch, LogLine, RunDetail } from "@/lib/types"

const LEVELS: Record<string, [Tone, string]> = {
  debug: ["muted", "Gỡ lỗi"],
  info: ["info", "Thông tin"],
  warning: ["warn", "Cảnh báo"],
  error: ["bad", "Lỗi"],
  critical: ["bad", "Nghiêm trọng"],
}

function value(v: unknown): string {
  const text = typeof v === "object" ? JSON.stringify(v) : String(v)
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

/** Ngữ cảnh của một dòng log: các cặp khoá–giá trị (bỏ giá trị rỗng) thay cho JSON thô. */
function LogContext({ context }: { context: Record<string, unknown> }) {
  const entries = Object.entries(context).filter(
    ([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0),
  )
  if (!entries.length) return null
  return (
    <dl className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-meta">
      {entries.slice(0, 14).map(([k, v]) => (
        <div key={k} className="flex min-w-0 gap-1">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-all text-foreground/80">{value(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

export function RunPage() {
  const { id } = useParams()
  const query = useQuery({ queryKey: ["run", id], queryFn: () => api.get<RunDetail>(`/runs/${id}`) })

  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading rows={10} />
  const d = query.data
  const r = d.run
  const name = d.company_name ? companyName({ brand_name: null, legal_name: null, input_name: d.company_name }) : "Doanh nghiệp"

  return (
    <>
      <PageHeader
        crumbs={[
          { to: "/companies", label: "Doanh nghiệp" },
          { to: `/companies/${r.company_id}`, label: name },
        ]}
        title={`Lượt chạy lúc ${dt(r.started_at)}`}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <StatusBadge value={r.status} />
            <span>Thời gian {duration(r.duration_ms)}</span>
            <span>· Nhận {r.jobs_accepted}/{r.jobs_found} tin</span>
            <code className="text-meta text-muted-foreground/80">{r.run_id}</code>
          </span>
        }
      />

      {(r.incomplete_sources.length > 0 || r.error) && (
        <Alert className="mb-6 border-warning/30 bg-warning/8">
          <AlertTriangle className="text-warning" />
          <AlertTitle>
            {r.incomplete_sources.length
              ? `Nguồn chưa xong: ${r.incomplete_sources.map(sourceLabel).join(", ")}`
              : "Lượt chạy gặp lỗi"}
          </AlertTitle>
          {r.error && <AlertDescription className="font-mono text-meta">{r.error}</AlertDescription>}
        </Alert>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Theo nguồn" bodyClassName="p-0">
          {d.by_source.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Nguồn</TableHead>
                  <TableHead className="text-right">Request</TableHead>
                  <TableHead>Kết quả</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.by_source.map((s) => (
                  <TableRow key={s.source}>
                    <TableCell className="max-w-72">
                      <div className="font-medium">{sourceLabel(s.source)}</div>
                      <div className="truncate font-mono text-meta text-muted-foreground">{s.source}</div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{num(s.total)}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {byCount(s.outcomes).map(([k, v]) => (
                          <StatusBadge key={k} value={k} text={`${label(k)} · ${v}`} />
                        ))}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Không có request nào.</Empty>
          )}
          {d.failed_pages.length > 0 && (
            <div className="space-y-1.5 border-t px-5 py-4 text-meta">
              <div className="font-medium">Trang parse lỗi</div>
              {d.failed_pages.map((f) => (
                <div key={f.id}>
                  <TextLink to={`/failed-pages/${f.id}`} className="break-all text-brand">
                    {f.url}
                  </TextLink>
                  <span className="text-muted-foreground"> · {failReason(f.reason)}</span>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title="Log" description={`${num(d.logs_total)} dòng`} bodyClassName="p-0">
          <PagedList<LogLine> queryKey={["run-logs", r.run_id]} url={`/runs/${r.run_id}/logs`} empty="Không có log cho lượt chạy này.">
            {(logs) => (
              <div className="divide-y">
                {logs.map((l, i) => {
                  const [tone, text] = LEVELS[l.level] ?? ["muted", cap(l.level)]
                  return (
                    <div key={i} className="grid grid-cols-[auto_6.5rem_1fr] items-start gap-3 px-5 py-2.5 text-meta">
                      <span className="text-muted-foreground tabular-nums">{hour(l.ts)}</span>
                      <StatusBadge value={l.level} tone={tone} text={text} />
                      <div className="min-w-0">
                        <code className="font-mono font-medium text-foreground">{l.event}</code>
                        <LogContext context={l.context} />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </PagedList>
        </Section>
      </div>

      <Section
        title="Request"
        description={`${num(d.fetches_total)} request trong lượt chạy`}
        className="mt-6"
        bodyClassName="p-0"
      >
        <PagedList<Fetch> queryKey={["run-fetches", r.run_id]} url={`/runs/${r.run_id}/fetches`} empty="Không có request nào.">
          {(fetches) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Nguồn</TableHead>
                  <TableHead>URL</TableHead>
                  <TableHead className="text-right">HTTP</TableHead>
                  <TableHead>Kết quả</TableHead>
                  <TableHead className="text-right">Thời gian</TableHead>
                  <TableHead className="text-right">Số tin</TableHead>
                  <TableHead>Lỗi</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fetches.map((f, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-meta">{sourceLabel(f.source)}</TableCell>
                    <TableCell className="max-w-72 truncate text-meta">
                      <ExternalLink href={f.url}>{f.url}</ExternalLink>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{f.http_status ?? "–"}</TableCell>
                    <TableCell>
                      <StatusBadge value={f.outcome} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{duration(f.duration_ms)}</TableCell>
                    <TableCell className="text-right tabular-nums">{f.items ?? "–"}</TableCell>
                    <TableCell className="max-w-44 truncate text-meta text-muted-foreground" title={f.error ?? ""}>
                      {f.error ? cap(f.error) : ""}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </PagedList>
      </Section>
    </>
  )
}
