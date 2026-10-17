import { useQuery } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { useParams } from "react-router"

import { Empty, ErrorState, ExternalLink, Loading, PageHeader, Section, StatusBadge, TextLink, type Tone } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { byCount, cap, companyName, dt, duration, failReason, hour, label, num, sourceLabel } from "@/lib/format"
import type { RunDetail } from "@/lib/types"

const LEVELS: Record<string, [Tone, string]> = {
  debug: ["muted", "Gỡ lỗi"],
  info: ["info", "Thông tin"],
  warning: ["warn", "Cảnh báo"],
  error: ["bad", "Lỗi"],
  critical: ["bad", "Nghiêm trọng"],
}

export function RunPage() {
  const { id } = useParams()
  const query = useQuery({ queryKey: ["run", id], queryFn: () => api.get<RunDetail>(`/runs/${id}`) })

  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading rows={10} />
  const d = query.data
  const r = d.run
  const name = d.company_name ? companyName({ brand_name: null, legal_name: null, input_name: d.company_name }) : `#${r.company_id}`

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
            <code className="text-xs text-muted-foreground/80">{r.run_id}</code>
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
          {r.error && <AlertDescription className="font-mono text-xs">{r.error}</AlertDescription>}
        </Alert>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        <Section title="Theo nguồn" bodyClassName="p-0">
          {d.by_source.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Nguồn</TableHead>
                  <TableHead className="text-right">Request</TableHead>
                  <TableHead className="pr-4">Kết quả</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.by_source.map((s) => (
                  <TableRow key={s.source}>
                    <TableCell className="max-w-72 pl-4">
                      <div className="font-medium">{sourceLabel(s.source)}</div>
                      <div className="truncate font-mono text-xs text-muted-foreground">{s.source}</div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{num(s.total)}</TableCell>
                    <TableCell className="pr-4">
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
            <div className="space-y-1.5 border-t p-4 text-xs">
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

        <Section title="Log" description={`${num(d.logs.length)} dòng`} bodyClassName="p-0">
          {d.logs.length ? (
            <ScrollArea className="h-96">
              <div className="divide-y">
                {d.logs.map((l, i) => {
                  const [tone, text] = LEVELS[l.level] ?? ["muted", cap(l.level)]
                  return (
                    <div key={i} className="grid grid-cols-[auto_auto_1fr] items-start gap-3 px-4 py-2 text-xs">
                      <span className="pt-0.5 text-muted-foreground tabular-nums">{hour(l.ts)}</span>
                      <StatusBadge value={l.level} tone={tone} text={text} />
                      <div className="min-w-0">
                        <code className="font-medium">{l.event}</code>
                        <div className="break-all text-muted-foreground">{JSON.stringify(l.context).slice(0, 400)}</div>
                      </div>
                    </div>
                  )
                })}
              </div>
            </ScrollArea>
          ) : (
            <Empty>Không có log (kiểm tra LOG_DB_LEVEL).</Empty>
          )}
        </Section>
      </div>

      <Section title="Request" description={`${num(d.fetches.length)} request trong lượt chạy`} className="mt-6" bodyClassName="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-4">Nguồn</TableHead>
              <TableHead>URL</TableHead>
              <TableHead className="text-right">HTTP</TableHead>
              <TableHead>Kết quả</TableHead>
              <TableHead className="text-right">Thời gian</TableHead>
              <TableHead className="text-right">Số tin</TableHead>
              <TableHead className="pr-4">Lỗi</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {d.fetches.map((f, i) => (
              <TableRow key={i}>
                <TableCell className="pl-4 text-xs">{sourceLabel(f.source)}</TableCell>
                <TableCell className="max-w-96 truncate text-xs">
                  <ExternalLink href={f.url}>{f.url}</ExternalLink>
                </TableCell>
                <TableCell className="text-right tabular-nums">{f.http_status ?? "–"}</TableCell>
                <TableCell>
                  <StatusBadge value={f.outcome} />
                </TableCell>
                <TableCell className="text-right tabular-nums">{duration(f.duration_ms)}</TableCell>
                <TableCell className="text-right tabular-nums">{f.items ?? "–"}</TableCell>
                <TableCell className="max-w-64 truncate pr-4 text-xs text-muted-foreground" title={f.error ?? ""}>
                  {f.error ? cap(f.error) : ""}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
    </>
  )
}
