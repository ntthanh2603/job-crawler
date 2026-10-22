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
import { RunTags } from "@/components/crawl"
import { LiveStatus, useLiveQueue } from "@/components/live"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ago, cap, hour, num, sourceLabel } from "@/lib/format"
import type { QueueJob, QueueStats } from "@/lib/types"
import { cn } from "@/lib/utils"

const FUNCTIONS: Record<string, string> = {
  build_company: "Build bộ khung",
  poll_company: "Poll tin mới",
  process_company: "Xử lý doanh nghiệp",
  index_board: "Index đủ trang tuyển dụng",
  tail_board: "Index đuôi trang tuyển dụng",
  index_boards_cron: "Lên lịch index đủ",
  tail_boards_cron: "Lên lịch index đuôi",
  maintenance_cron: "Bảo trì",
  overview_cron: "Tính số liệu tổng quan",
}

const BOARD_FUNCTIONS = new Set(["index_board", "tail_board"])

/** Ba hàng đợi theo thứ tự API: build, poll, trang tuyển dụng; mỗi hàng đợi một tham số trang. */
const QUEUES = [
  {
    title: "Build bộ khung",
    description: "Lượt build (đủ) của từng doanh nghiệp: ban đêm, khi bộ khung hỏng, khi thêm mới hoặc bấm Build",
    param: "page",
  },
  {
    title: "Poll tin mới",
    description: "Lượt nhẹ theo bộ khung, ban ngày theo chu kỳ; hàng đợi riêng để không phải chờ lượt build",
    param: "poll_page",
  },
  {
    title: "Trang tuyển dụng",
    description: "Index đủ (đêm) và index đuôi (ngày) các trang tuyển dụng",
    param: "board_page",
  },
] as const

type PageParam = (typeof QUEUES)[number]["param"]

function queueMeta(name: string, index: number) {
  if (name.includes("poll")) return QUEUES[1]
  if (name.includes("board")) return QUEUES[2]
  if (name.includes("build")) return QUEUES[0]
  return QUEUES[Math.min(index, 2)]
}

function jobLabel(j: QueueJob) {
  if (BOARD_FUNCTIONS.has(j.function) && j.label) return sourceLabel(j.label)
  if (j.label) return cap(j.label)
  return FUNCTIONS[j.function] ?? j.key
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
    {
      label: "Đang chạy / tối đa",
      value: q.capacity ? `${num(q.active)} / ${num(q.capacity)}` : num(q.active),
      hint: "Tối đa = tổng số job chạy cùng lúc của các runner (theo worker đang kết nối, không quá cài đặt)",
      tone: q.capacity && q.active >= q.capacity && q.queued > 0 ? "warn" : undefined,
    },
    { label: "Đang chờ", value: num(q.queued) },
    { label: "Hẹn giờ", value: num(q.scheduled), hint: "Việc định kỳ và lượt build tiếp" },
    { label: "Runner", value: num(q.workers), hint: "Số runner đang nhận việc của hàng đợi này", tone: q.workers ? undefined : "warn" },
    { label: "Đã xong", value: num(q.complete), hint: "Từ khi runner khởi động" },
    {
      label: "Lỗi / chạy lại / huỷ",
      value: `${num(q.failed)} / ${num(q.retried)} / ${num(q.aborted)}`,
      tone: q.failed ? "bad" : undefined,
    },
  ]
  return (
    <div className="grid grid-cols-2 gap-px border-b bg-border sm:grid-cols-3 xl:grid-cols-6">
      {cells.map((c) => (
        <div key={c.label} className="bg-card px-4 py-3 sm:px-5">
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

/** Tên việc (link tới doanh nghiệp nếu có), nhãn lý do xếp và lượt chạy tiếp. */
function JobName({ j }: { j: QueueJob }) {
  const trigger = j.continuation > 0 && j.trigger === "continuation" ? null : j.trigger
  return (
    <div className="min-w-0 space-y-0.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {j.company_id ? (
          <TextLink to={`/companies/${j.company_id}`} className="font-medium break-words">
            {jobLabel(j)}
          </TextLink>
        ) : (
          <span className="font-medium break-words">{jobLabel(j)}</span>
        )}
        <RunTags trigger={trigger} />
        {j.continuation > 0 && (
          <Tag className="bg-warning/14 text-warning-ink" title="Lượt build trước chạy một phần; lượt này đọc nốt các nguồn còn dở">
            Build tiếp lần {j.continuation}
          </Tag>
        )}
      </div>
      {j.label && <div className="text-meta text-muted-foreground">{FUNCTIONS[j.function] ?? j.function}</div>}
    </div>
  )
}

function started(j: QueueJob) {
  return j.started ? cap(ago(new Date(j.started).toISOString())) : "Chưa chạy"
}

function JobList({ jobs, now }: { jobs: QueueJob[]; now: number }) {
  return (
    <>
      {/* Màn hình hẹp: mỗi việc một thẻ. */}
      <ul className="divide-y md:hidden">
        {jobs.map((j) => {
          const state = jobState(j, now)
          return (
            <li key={j.key} className="space-y-2 px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <JobName j={j} />
                <StatusBadge value={j.status} tone={state.tone} text={state.text} pulse={state.pulse} />
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-meta text-muted-foreground">
                <span>{started(j)}</span>
                <span className="tabular-nums">
                  Lần chạy {j.attempts ?? 0}/{j.retries ?? 1}
                </span>
              </div>
              {j.error && <div className="text-meta break-words text-danger-ink [overflow-wrap:anywhere]">{cap(j.error)}</div>}
            </li>
          )
        })}
      </ul>

      <Table className="max-md:hidden">
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
          {jobs.map((j) => {
            const state = jobState(j, now)
            return (
              <TableRow key={j.key}>
                <TableCell className="max-w-96 min-w-56 whitespace-normal">
                  <JobName j={j} />
                </TableCell>
                <TableCell>
                  <StatusBadge value={j.status} tone={state.tone} text={state.text} pulse={state.pulse} />
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {j.attempts ?? 0}/{j.retries ?? 1}
                </TableCell>
                <TableCell className="text-meta text-muted-foreground">{started(j)}</TableCell>
                <TableCell className="max-w-64 truncate text-meta text-muted-foreground" title={j.error ?? ""}>
                  {j.error ? cap(j.error) : "–"}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </>
  )
}

export function QueuePage() {
  // Mỗi hàng đợi một trang riêng (API: `page` build, `poll_page` poll, `board_page` trang tuyển dụng).
  const [pages, setPages] = useState<Record<PageParam, number>>({ page: 1, poll_page: 1, board_page: 1 })
  const { query, state } = useLiveQueue(pages, 5_000)
  const now = query.dataUpdatedAt || Date.now()

  return (
    <>
      <PageHeader
        title="Hàng đợi"
        description="Việc đang chạy, đang chờ và hẹn giờ của từng hàng đợi."
        actions={<LiveStatus state={state} pollSeconds={5} updatedAt={query.dataUpdatedAt} />}
      />
      {query.isError && !query.data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      <div className="space-y-6">
        {query.data?.queues.map((q, i) => {
          const meta = queueMeta(q.name, i)
          return (
            <Section
              key={q.name}
              title={
                <span className="inline-flex flex-wrap items-center gap-2">
                  {meta.title}
                  {q.active > 0 && <StatusBadge value={null} tone="info" text={`${num(q.active)} đang chạy`} pulse />}
                </span>
              }
              description={meta.description}
              bodyClassName="p-0"
            >
              {q.error && (
                <div className="flex items-start gap-2 border-b bg-destructive/5 px-5 py-3 text-sm text-danger-ink">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" /> Không đọc được hàng đợi: {q.error}
                </div>
              )}
              {!q.error && !q.workers && (
                <div className="flex items-start gap-2 border-b bg-warning/8 px-5 py-3 text-sm text-warning-ink">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" /> Không có runner nào nhận việc của hàng đợi này.
                </div>
              )}
              <Counts q={q} />
              {q.jobs.length ? (
                <>
                  <JobList jobs={q.jobs} now={now} />
                  {q.pages > 1 && (
                    <Pager
                      page={q.page}
                      pageCount={q.pages}
                      total={q.total}
                      limit={25}
                      onPage={(p) => setPages((cur) => ({ ...cur, [meta.param]: p }))}
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
