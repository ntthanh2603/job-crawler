import { useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Globe, RefreshCw } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import {
  ConfirmButton,
  ErrorState,
  FilterSelect,
  InfoTip,
  PagedList,
  PageHeader,
  PageLoading,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  Tag,
  TextLink,
} from "@/components/common"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ago, cap, dt, num, sourceLabel } from "@/lib/format"
import {
  getGetBoardRunsQueryOptions,
  getGetBoardsQueryKey,
  getGetQueueQueryKey,
  useGetBoards,
  usePostBoardIndex,
} from "@/lib/gen/queries"
import type { BoardRun } from "@/lib/types"

const MODES: Record<BoardRun["mode"], { label: string; title: string; className: string }> = {
  full: {
    label: "Đủ",
    title: "Index đủ: đọc toàn bộ sitemap / danh sách (ban đêm)",
    className: "bg-neutral-tone/12 text-neutral-ink",
  },
  tail: {
    label: "Đuôi",
    title: "Index đuôi: chỉ đọc phần mới nhất của danh sách để bắt tin mới nhanh (ban ngày)",
    className: "bg-info/12 text-info-ink",
  },
}

function ModeTag({ mode }: { mode: BoardRun["mode"] }) {
  const m = MODES[mode] ?? MODES.full
  return (
    <Tag title={m.title} className={m.className}>
      {m.label}
    </Tag>
  )
}

export function BoardsPage() {
  // Lọc lượt index theo kiểu (đủ / đuôi) trên trang đang xem.
  const [mode, setMode] = useState("")
  const client = useQueryClient()
  const query = useGetBoards({ query: { refetchInterval: 30_000 } })
  const index = usePostBoardIndex({
    mutation: {
      onSuccess: (r, { name }) => {
        const board = sourceLabel(name)
        if (!r.queued) toast.warning(`${board} đang chờ hoặc đang được index`)
        else if (!r.workers) toast.warning(`Đã đưa ${board} vào hàng đợi nhưng chưa có worker index nào chạy`)
        else toast.success(`Đã đưa ${board} vào hàng đợi index`)
        client.invalidateQueries({ queryKey: getGetBoardsQueryKey() })
        client.invalidateQueries({ queryKey: getGetQueueQueryKey() })
      },
    },
  })

  const d = query.data
  const mapped = d?.boards.reduce((n, b) => n + b.profiles_mapped, 0) ?? 0
  const profiles = d?.boards.reduce((n, b) => n + b.profiles, 0) ?? 0
  return (
    <>
      <PageHeader
        title="Trang tuyển dụng"
        description="Index tăng dần tin đang mở trên các trang tuyển dụng, rồi khớp profile nhà tuyển dụng với doanh nghiệp trong danh sách."
      />
      {query.isError && !d && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      {d && (
        <div className="space-y-6">
          {d.queue.error && (
            <div role="alert" className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
              <div>
                <div className="font-medium text-danger-ink">Không đọc được hàng đợi index</div>
                <div className="text-muted-foreground">{cap(d.queue.error)}</div>
              </div>
            </div>
          )}
          <StatGrid className="grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Tin đang mở trên các trang"
              icon={<Globe />}
              value={num(d.boards.reduce((n, b) => n + b.active_postings, 0))}
              hint={`${num(d.boards.length)} trang tuyển dụng`}
            />
            <StatCard
              label="Profile khớp doanh nghiệp"
              value={num(mapped)}
              hint={`Trên ${num(profiles)} profile nhà tuyển dụng`}
            />
            <StatCard
              label="Runner index"
              value={num(d.queue.workers)}
              status={d.queue.workers ? { tone: "good", text: "Đang chạy" } : { tone: "warn", text: "Đang dừng" }}
            />
            <StatCard
              label="Trang đang index / chờ"
              icon={<RefreshCw />}
              value={`${num(d.queue.active)} / ${num(d.queue.queued)}`}
              status={d.queue.active ? { tone: "info", text: "Đang index", pulse: true } : undefined}
            />
          </StatGrid>

          <Section title="Các trang" description="Bấm số tin đang mở để xem việc làm của trang đó" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Trang</TableHead>
                  <TableHead className="text-right">Đang mở</TableHead>
                  <TableHead className="text-right">Đã index</TableHead>
                  <TableHead>
                    <span className="inline-flex items-center gap-1">
                      Profile khớp
                      <InfoTip>Số profile nhà tuyển dụng trên trang đã khớp được với một doanh nghiệp trong danh sách.</InfoTip>
                    </span>
                  </TableHead>
                  <TableHead>Lượt index cuối</TableHead>
                  <TableHead className="text-right">
                    <span className="sr-only">Thao tác</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.boards.map((b) => (
                  <TableRow key={b.source}>
                    <TableCell className="font-medium">{sourceLabel(b.source)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      <TextLink to={`/jobs?source=${b.source}`} className="font-semibold text-brand">
                        {num(b.active_postings)}
                      </TextLink>
                    </TableCell>
                    <TableCell className="text-right text-muted-foreground tabular-nums">{num(b.postings)}</TableCell>
                    <TableCell className="w-40 min-w-36">
                      <div className="flex items-center justify-between text-meta tabular-nums">
                        <span>
                          {num(b.profiles_mapped)} <span className="text-muted-foreground">/ {num(b.profiles)}</span>
                        </span>
                        {b.profiles > 0 && (
                          <span className="text-muted-foreground">{Math.round((b.profiles_mapped / b.profiles) * 100)}%</span>
                        )}
                      </div>
                      {b.profiles > 0 && <Progress value={(b.profiles_mapped / b.profiles) * 100} className="mt-1.5 h-1.5" />}
                    </TableCell>
                    <TableCell className="min-w-56 text-meta whitespace-normal">
                      {b.last_status ? (
                        <div className="space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <StatusBadge value={b.last_status} pulse={b.last_status === "running"} />
                            <span className="text-muted-foreground">{cap(ago(b.last_finished ?? b.last_started))}</span>
                          </div>
                          <div className="text-muted-foreground tabular-nums">
                            Thấy {num(b.last_urls_seen)} · tải {num(b.last_fetched)} · mới {num(b.last_new)} · gỡ{" "}
                            {num(b.last_removed)}
                            {b.last_complete === false && <span className="text-warning-ink"> · danh sách chưa đủ</span>}
                          </div>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">Chưa index</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <ConfirmButton
                        title={`Index ${sourceLabel(b.source)} ngay?`}
                        description="Đưa trang này vào hàng đợi index ngay, không chờ lịch định kỳ. Lượt index có thể kéo dài vài chục phút."
                        onConfirm={() => index.mutate({ name: b.source })}
                        size="xs"
                      >
                        <RefreshCw /> Index ngay
                      </ConfirmButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          <Section
            title="Các lượt index gần đây"
            description="Mới nhất trước · index đủ chạy ban đêm, index đuôi chạy vài phút một lần ban ngày"
            actions={
              <FilterSelect
                label="Kiểu"
                value={mode}
                onChange={setMode}
                options={[
                  ["full", "Index đủ"],
                  ["tail", "Index đuôi"],
                ]}
              />
            }
            bodyClassName="p-0"
          >
            <PagedList options={(page) => getGetBoardRunsQueryOptions({ page })} empty="Chưa có lượt index nào.">
              {(all) => {
                const runs = mode ? all.filter((r) => (r.mode ?? "full") === mode) : all
                if (!runs.length)
                  return (
                    <p className="px-5 py-10 text-center text-sm text-muted-foreground">
                      Trang này không có lượt index {mode === "tail" ? "đuôi" : "đủ"} nào. Chuyển trang hoặc bỏ lọc để xem
                      thêm.
                    </p>
                  )
                return (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Trang</TableHead>
                        <TableHead>Kết quả</TableHead>
                        <TableHead className="px-2 text-right">Thấy</TableHead>
                        <TableHead className="px-2 text-right">Tải</TableHead>
                        <TableHead className="px-2 text-right">Mới</TableHead>
                        <TableHead className="px-2 text-right">Đổi</TableHead>
                        <TableHead className="px-2 text-right">Lỗi</TableHead>
                        <TableHead className="px-2 text-right">Gỡ</TableHead>
                        <TableHead className="text-right">
                          <span className="inline-flex items-center gap-1">
                            Profile
                            <InfoTip>Profile nhà tuyển dụng mới / khớp được doanh nghiệp trong lượt này.</InfoTip>
                          </span>
                        </TableHead>
                        <TableHead>Ghi chú</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {runs.map((r) => (
                        <TableRow key={r.id}>
                          <TableCell>
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium">{sourceLabel(r.source)}</span>
                              <ModeTag mode={r.mode ?? "full"} />
                            </div>
                            <div className="text-meta whitespace-nowrap text-muted-foreground tabular-nums">{dt(r.started_at)}</div>
                          </TableCell>
                          <TableCell>
                            <StatusBadge value={r.status} pulse={r.status === "running"} />
                          </TableCell>
                          <TableCell className="px-2 text-right tabular-nums">{num(r.urls_seen)}</TableCell>
                          <TableCell className="px-2 text-right tabular-nums">{num(r.fetched)}</TableCell>
                          <TableCell className="px-2 text-right font-medium tabular-nums">{num(r.new)}</TableCell>
                          <TableCell className="px-2 text-right tabular-nums">{num(r.updated)}</TableCell>
                          <TableCell className={`px-2 text-right tabular-nums ${r.failed ? "text-danger-ink" : ""}`}>{num(r.failed)}</TableCell>
                          <TableCell className="px-2 text-right tabular-nums">{num(r.removed)}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {num(r.profiles_new)} / {num(r.profiles_matched)}
                          </TableCell>
                          <TableCell className="max-w-56 truncate text-meta text-muted-foreground" title={r.error ?? ""}>
                            {[!r.complete && "Danh sách chưa đủ", r.error && cap(r.error)].filter(Boolean).join(". ") || "–"}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )
              }}
            </PagedList>
          </Section>
        </div>
      )}
    </>
  )
}
