import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Layers } from "lucide-react"
import { useState } from "react"

import {
  Empty,
  ErrorState,
  InfoTip,
  PageHeader,
  PageLoading,
  Pager,
  Section,
  StatusBadge,
  Tag,
  TextLink,
  type Tone,
} from "@/components/common"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, cap, hour, num, sourceLabel } from "@/lib/format"
import type { QueueJob, QueuePage as QueueData, QueueStats } from "@/lib/types"
import { cn } from "@/lib/utils"

const FUNCTIONS: Record<string, string> = {
  process_company: "Xử lý doanh nghiệp",
  index_board: "Index trang tuyển dụng",
  maintenance_cron: "Bảo trì",
  index_boards_cron: "Lên lịch index",
  overview_cron: "Tính số liệu tổng quan",
}

function jobLabel(j: QueueJob) {
  if (j.function === "index_board" && j.label) return sourceLabel(j.label)
  if (j.label) return cap(j.label)
  return FUNCTIONS[j.function] ?? j.key
}

function queueTitle(name: string) {
  return name.includes("board") ? "Index trang tuyển dụng" : "Doanh nghiệp"
}

/** Trạng thái một việc: đang chạy, hẹn giờ (lượt chạy tiếp, việc định kỳ) hoặc đang chờ. */
function jobState(j: QueueJob, now: number): { tone: Tone; text: string; pulse?: boolean } {
  if (j.status === "active") return { tone: "info", text: "Đang chạy", pulse: true }
  if (j.scheduled && j.scheduled * 1000 > now) return { tone: "muted", text: `Hẹn ${hour(new Date(j.scheduled * 1000).toISOString())}` }
  if (j.status === "queued" || j.status === "new" || j.status === "deferred") return { tone: "muted", text: "Đang chờ" }
  if (j.status === "failed" || j.status === "aborted") return { tone: "bad", text: j.status === "failed" ? "Lỗi" : "Đã huỷ" }
  return { tone: "muted", text: cap(j.status) }
}

function Counts({ q }: { q: QueueStats }) {
  const cells: { label: string; value: string; hint?: string; tone?: "warn" | "bad" }[] = [
    { label: "Đang chạy", value: num(q.active) },
    { label: "Đang chờ", value: num(q.queued) },
    { label: "Hẹn giờ", value: num(q.scheduled), hint: "Việc định kỳ và lượt chạy tiếp" },
    { label: "Worker", value: num(q.workers), tone: q.workers ? undefined : "warn" },
    { label: "Đã xong", value: num(q.complete), hint: "Từ khi worker khởi động" },
    {
      label: "Lỗi / chạy lại / huỷ",
      value: `${num(q.failed)} / ${num(q.retried)} / ${num(q.aborted)}`,
      tone: q.failed ? "bad" : undefined,
    },
  ]
  return (
    <div className="grid grid-cols-2 gap-px border-b bg-border sm:grid-cols-3 xl:grid-cols-6">
      {cells.map((c) => (
        <div key={c.label} className="bg-card px-5 py-3">
          <div className="flex items-center gap-1 text-meta text-muted-foreground">
            {c.label}
            {c.hint && <InfoTip>{c.hint}</InfoTip>}
          </div>
          <div
            className={cn(
              "text-xl font-semibold tabular-nums",
              c.tone === "warn" && "text-warning-ink",
              c.tone === "bad" && "text-danger-ink",
            )}
          >
            {c.value}
          </div>
        </div>
      ))}
    </div>
  )
}

export function QueuePage() {
  // Mỗi hàng đợi một trang riêng (API: `page` cho doanh nghiệp, `board_page` cho index).
  const [pages, setPages] = useState({ page: 1, board_page: 1 })
  const query = useQuery({
    queryKey: ["queue", pages],
    queryFn: () => api.get<QueueData>(`/queue${qs(pages)}`),
    refetchInterval: 5_000,
    placeholderData: (prev) => prev,
  })
  const now = query.dataUpdatedAt || Date.now()

  return (
    <>
      <PageHeader
        title="Hàng đợi"
        description="Việc đang chạy, đang chờ và hẹn giờ của từng hàng đợi."
        actions={
          <span className="inline-flex items-center gap-2 text-meta text-muted-foreground">
            <span className="relative flex size-2">
              <span className="absolute inset-0 animate-ping rounded-full bg-success opacity-50" />
              <span className="relative size-2 rounded-full bg-success" />
            </span>
            Tự cập nhật mỗi 5 giây{query.dataUpdatedAt ? ` · ${hour(new Date(query.dataUpdatedAt).toISOString())}` : ""}
          </span>
        }
      />
      {query.isError && !query.data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      <div className="space-y-6">
        {query.data?.queues.map((q) => {
          const board = q.name.includes("board")
          return (
            <Section
              key={q.name}
              title={
                <span className="inline-flex items-center gap-2">
                  {queueTitle(q.name)}
                  {q.active > 0 && <StatusBadge value={null} tone="info" text={`${num(q.active)} đang chạy`} pulse />}
                </span>
              }
              description={`Hàng đợi ${q.name}`}
              bodyClassName="p-0"
            >
              {q.error && (
                <div className="flex items-start gap-2 border-b bg-destructive/5 px-5 py-3 text-sm text-danger-ink">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" /> Không đọc được hàng đợi: {q.error}
                </div>
              )}
              {!q.error && !q.workers && (
                <div className="flex items-start gap-2 border-b bg-warning/8 px-5 py-3 text-sm text-warning-ink">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" /> Không có worker nào nhận việc của hàng đợi này.
                </div>
              )}
              <Counts q={q} />
              {q.jobs.length ? (
                <>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Việc</TableHead>
                        <TableHead>Trạng thái</TableHead>
                        <TableHead className="text-right">Lần chạy</TableHead>
                        <TableHead>Bắt đầu</TableHead>
                        <TableHead>Lỗi</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {q.jobs.map((j) => {
                        const state = jobState(j, now)
                        return (
                          <TableRow key={j.key}>
                            <TableCell className="max-w-96 min-w-56 whitespace-normal">
                              <div className="flex flex-wrap items-center gap-1.5">
                                {j.company_id ? (
                                  <TextLink to={`/companies/${j.company_id}`} className="font-medium">
                                    {jobLabel(j)}
                                  </TextLink>
                                ) : (
                                  <span className="font-medium">{jobLabel(j)}</span>
                                )}
                                {j.continuation > 0 && (
                                  <Tag
                                    className="bg-warning/14 text-warning-ink"
                                    title="Lượt trước chạy một phần; lượt này đọc nốt các nguồn còn dở"
                                  >
                                    Chạy tiếp lần {j.continuation}
                                  </Tag>
                                )}
                              </div>
                              <div className="text-meta text-muted-foreground">{FUNCTIONS[j.function] ?? j.function}</div>
                            </TableCell>
                            <TableCell>
                              <StatusBadge value={j.status} tone={state.tone} text={state.text} pulse={state.pulse} />
                            </TableCell>
                            <TableCell className="text-right tabular-nums">
                              {j.attempts ?? 0}/{j.retries ?? 1}
                            </TableCell>
                            <TableCell className="text-meta text-muted-foreground">
                              {j.started ? cap(ago(new Date(j.started).toISOString())) : "Chưa chạy"}
                            </TableCell>
                            <TableCell className="max-w-64 truncate text-meta text-muted-foreground" title={j.error ?? ""}>
                              {j.error ? cap(j.error) : "–"}
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                  {q.pages > 1 && (
                    <Pager
                      page={q.page}
                      pages={q.pages}
                      total={q.total}
                      size={25}
                      onPage={(p) => setPages((cur) => ({ ...cur, [board ? "board_page" : "page"]: p }))}
                    />
                  )}
                </>
              ) : (
                <Empty icon={<Layers />} title="Hàng đợi trống">
                  Không có việc nào đang chờ, đang chạy hay hẹn giờ.
                </Empty>
              )}
            </Section>
          )
        })}
      </div>
    </>
  )
}
