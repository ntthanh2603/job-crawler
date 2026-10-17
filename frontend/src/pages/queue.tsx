import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { Empty, ErrorState, Loading, PageHeader, Section, StatCard, StatusBadge } from "@/components/common"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ago, num } from "@/lib/format"
import type { QueuePage as QueueData } from "@/lib/types"

const STATUS_TONE: Record<string, string> = { active: "running", queued: "muted", failed: "failed", complete: "ok" }

function fromMillis(value: number | null | undefined) {
  return value ? new Date(value).toISOString() : null
}

export function QueuePage() {
  const query = useQuery({ queryKey: ["queue"], queryFn: () => api.get<QueueData>("/queue"), refetchInterval: 5_000 })

  return (
    <>
      <PageHeader title="Queue" description="Hai queue SAQ trên Redis, tự cập nhật mỗi 5 giây." />
      {query.isError && <ErrorState error={query.error} />}
      {query.isLoading && <Loading rows={8} />}
      <div className="space-y-6">
        {query.data?.queues.map((q) => (
          <Section key={q.name} title={q.name} description={q.error ? `lỗi: ${q.error}` : undefined} bodyClassName="space-y-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
              <StatCard label="Chờ" value={num(q.queued)} />
              <StatCard label="Đang chạy" value={num(q.active)} />
              <StatCard label="Hẹn giờ" value={num(q.scheduled)} />
              <StatCard label="Worker" value={num(q.workers)} tone={q.workers ? undefined : "warn"} />
              <StatCard label="Xong" value={num(q.complete)} hint="từ khi worker chạy" />
              <StatCard label="Lỗi" value={num(q.failed)} tone={q.failed ? "bad" : undefined} />
              <StatCard label="Chạy lại / huỷ" value={`${num(q.retried)} / ${num(q.aborted)}`} />
            </div>
            {q.jobs.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job</TableHead>
                    <TableHead>Hàm</TableHead>
                    <TableHead>Trạng thái</TableHead>
                    <TableHead className="text-right">Lần chạy</TableHead>
                    <TableHead>Bắt đầu</TableHead>
                    <TableHead>Lỗi</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {q.jobs.map((j) => (
                    <TableRow key={j.key}>
                      <TableCell className="max-w-80 truncate">
                        {j.company_id ? (
                          <Link to={`/companies/${j.company_id}`} className="hover:underline">
                            {j.label || j.key}
                          </Link>
                        ) : (
                          j.label || j.key
                        )}
                      </TableCell>
                      <TableCell className="text-xs">{j.function}</TableCell>
                      <TableCell>
                        <StatusBadge value={STATUS_TONE[j.status] ?? j.status} text={j.status} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {j.attempts ?? 0}/{j.retries ?? 1}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{j.started ? ago(fromMillis(j.started)) : "–"}</TableCell>
                      <TableCell className="max-w-64 truncate text-xs text-muted-foreground">{j.error}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Empty>Không có job đang chờ/chạy.</Empty>
            )}
          </Section>
        ))}
      </div>
    </>
  )
}
