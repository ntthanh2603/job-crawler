import { useQuery } from "@tanstack/react-query"

import { Empty, ErrorState, Loading, PageHeader, Section, StatCard, StatusBadge, TextLink } from "@/components/common"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ago, cap, num, sourceLabel } from "@/lib/format"
import type { QueueJob, QueuePage as QueueData } from "@/lib/types"

const FUNCTIONS: Record<string, string> = {
  process_company: "Xử lý doanh nghiệp",
  index_board: "Index trang tuyển dụng",
  maintenance_cron: "Bảo trì định kỳ",
  index_boards_cron: "Lên lịch index",
}

function jobLabel(j: QueueJob) {
  if (j.function === "index_board" && j.label) return sourceLabel(j.label)
  if (j.label) return cap(j.label)
  return FUNCTIONS[j.function] ?? j.key
}

// Ô số liệu nằm trong khung: nền nhạt, không đổ bóng.
const INNER = "bg-muted/30 shadow-none"

function queueTitle(name: string) {
  return name.includes("board") ? "Hàng đợi index trang tuyển dụng" : "Hàng đợi doanh nghiệp"
}

export function QueuePage() {
  const query = useQuery({ queryKey: ["queue"], queryFn: () => api.get<QueueData>("/queue"), refetchInterval: 5_000 })

  return (
    <>
      <PageHeader title="Hàng đợi" description="Hai hàng đợi SAQ trên Redis, tự cập nhật mỗi 5 giây." />
      {query.isError && <ErrorState error={query.error} />}
      {query.isLoading && <Loading rows={8} />}
      <div className="space-y-6">
        {query.data?.queues.map((q) => (
          <Section
            key={q.name}
            title={queueTitle(q.name)}
            description={q.error ? `Lỗi kết nối: ${q.error}` : `Tên hàng đợi: ${q.name}`}
            bodyClassName="space-y-4"
          >
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
              <StatCard className={INNER} label="Đang chờ" value={num(q.queued)} />
              <StatCard className={INNER} label="Đang chạy" value={num(q.active)} />
              <StatCard className={INNER} label="Hẹn giờ" value={num(q.scheduled)} hint="Cron và việc chờ chạy lại" />
              <StatCard
                className={INNER}
                label="Worker"
                value={num(q.workers)}
                status={q.workers ? undefined : { tone: "warn", text: "Không có" }}
              />
              <StatCard className={INNER} label="Đã xong" value={num(q.complete)} hint="Từ khi worker khởi động" />
              <StatCard
                className={INNER}
                label="Lỗi / chạy lại / huỷ"
                value={`${num(q.failed)} / ${num(q.retried)} / ${num(q.aborted)}`}
                status={q.failed ? { tone: "bad", text: "Có lỗi" } : undefined}
              />
            </div>
            {q.jobs.length ? (
              <div className="overflow-hidden rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Việc</TableHead>
                      <TableHead>Loại</TableHead>
                      <TableHead>Trạng thái</TableHead>
                      <TableHead className="text-right">Lần chạy</TableHead>
                      <TableHead>Bắt đầu</TableHead>
                      <TableHead className="pr-4">Lỗi</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {q.jobs.map((j) => (
                      <TableRow key={j.key}>
                        <TableCell className="max-w-80 truncate pl-4">
                          {j.company_id ? (
                            <TextLink to={`/companies/${j.company_id}`}>{jobLabel(j)}</TextLink>
                          ) : (
                            jobLabel(j)
                          )}
                        </TableCell>
                        <TableCell className="text-xs">{FUNCTIONS[j.function] ?? j.function}</TableCell>
                        <TableCell>
                          <StatusBadge value={j.status} />
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {j.attempts ?? 0}/{j.retries ?? 1}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {j.started ? cap(ago(new Date(j.started).toISOString())) : "Chưa chạy"}
                        </TableCell>
                        <TableCell className="max-w-64 truncate pr-4 text-xs text-muted-foreground" title={j.error ?? ""}>
                          {j.error ? cap(j.error) : "–"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <Empty>Không có việc nào đang chờ hoặc đang chạy.</Empty>
            )}
          </Section>
        ))}
      </div>
    </>
  )
}
