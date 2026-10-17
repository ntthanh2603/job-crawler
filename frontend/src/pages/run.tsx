import { useQuery } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { Link, useParams } from "react-router"

import { Empty, ErrorState, ExternalLink, Loading, PageHeader, Section, StatusBadge } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { dt, duration, hour } from "@/lib/format"
import type { RunDetail } from "@/lib/types"

const LEVEL_TONE: Record<string, string> = { error: "failed", critical: "failed", warning: "partial" }

export function RunPage() {
  const { id } = useParams()
  const query = useQuery({ queryKey: ["run", id], queryFn: () => api.get<RunDetail>(`/runs/${id}`) })

  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading rows={10} />
  const d = query.data
  const r = d.run

  return (
    <>
      <PageHeader
        crumbs={[
          { to: "/companies", label: "Doanh nghiệp" },
          { to: `/companies/${r.company_id}`, label: d.company_name ?? `#${r.company_id}` },
        ]}
        title={`Lượt chạy ${dt(r.started_at)}`}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge value={r.status} /> {duration(r.duration_ms)} · nhận {r.jobs_accepted}/{r.jobs_found} tin ·{" "}
            <code className="text-xs">{r.run_id}</code>
          </span>
        }
      />

      {(r.incomplete_sources.length > 0 || r.error) && (
        <Alert className="mb-6">
          <AlertTriangle />
          <AlertTitle>
            {r.incomplete_sources.length ? `Nguồn chưa xong: ${r.incomplete_sources.join(", ")}` : "Lỗi"}
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
                    <TableCell className="max-w-72 truncate pl-4">{s.source}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.total}</TableCell>
                    <TableCell className="pr-4">
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(s.outcomes).map(([k, v]) => (
                          <StatusBadge key={k} value={k} text={`${k} ${v}`} />
                        ))}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Không có request.</Empty>
          )}
          {d.failed_pages.length > 0 && (
            <div className="border-t p-4 text-xs">
              <div className="mb-1 font-medium">Trang parse lỗi</div>
              {d.failed_pages.map((f) => (
                <div key={f.id}>
                  <Link to={`/failed-pages/${f.id}`} className="text-primary hover:underline">
                    {f.url}
                  </Link>{" "}
                  <span className="text-muted-foreground">{f.reason}</span>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title="Log" description={`${d.logs.length} dòng`} bodyClassName="p-0">
          {d.logs.length ? (
            <ScrollArea className="h-96">
              <div className="divide-y">
                {d.logs.map((l, i) => (
                  <div key={i} className="flex gap-3 px-4 py-2 text-xs">
                    <span className="shrink-0 text-muted-foreground tabular-nums">{hour(l.ts)}</span>
                    <span className="shrink-0">
                      <StatusBadge value={LEVEL_TONE[l.level] ?? "muted"} text={l.event} />
                    </span>
                    <code className="min-w-0 break-all text-muted-foreground">{JSON.stringify(l.context).slice(0, 400)}</code>
                  </div>
                ))}
              </div>
            </ScrollArea>
          ) : (
            <Empty>Không có log (LOG_DB_LEVEL?).</Empty>
          )}
        </Section>
      </div>

      <Section title="Request" description={`${d.fetches.length} request`} className="mt-6" bodyClassName="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-4">Nguồn</TableHead>
              <TableHead>URL</TableHead>
              <TableHead className="text-right">HTTP</TableHead>
              <TableHead>Kết quả</TableHead>
              <TableHead className="text-right">Thời gian</TableHead>
              <TableHead className="text-right">Tin</TableHead>
              <TableHead className="pr-4">Lỗi</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {d.fetches.map((f, i) => (
              <TableRow key={i}>
                <TableCell className="pl-4 text-xs">{f.source}</TableCell>
                <TableCell className="max-w-96 truncate text-xs">
                  <ExternalLink href={f.url}>{f.url}</ExternalLink>
                </TableCell>
                <TableCell className="text-right tabular-nums">{f.http_status ?? "–"}</TableCell>
                <TableCell>
                  <StatusBadge value={f.outcome} text={f.outcome} />
                </TableCell>
                <TableCell className="text-right tabular-nums">{duration(f.duration_ms)}</TableCell>
                <TableCell className="text-right tabular-nums">{f.items ?? ""}</TableCell>
                <TableCell className="max-w-64 truncate pr-4 text-xs text-muted-foreground">{f.error}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Section>
    </>
  )
}
