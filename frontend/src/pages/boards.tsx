import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { toast } from "sonner"

import {
  ConfirmButton,
  ErrorState,
  Loading,
  PagedList,
  PageHeader,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  TextLink,
} from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ago, cap, dt, num, sourceLabel } from "@/lib/format"
import type { BoardRun, Boards } from "@/lib/types"

export function BoardsPage() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ["boards"], queryFn: () => api.get<Boards>("/boards"), refetchInterval: 30_000 })
  const index = useMutation({
    mutationFn: (name: string) => api.post<{ queued: boolean; workers: number }>(`/boards/${name}/index`),
    onSuccess: (r, name) => {
      const board = sourceLabel(name)
      if (!r.queued) toast.warning(`${board} đang chờ hoặc đang được index`)
      else if (!r.workers) toast.warning(`Đã đưa ${board} vào hàng đợi nhưng chưa có worker index nào chạy`)
      else toast.success(`Đã đưa ${board} vào hàng đợi index`)
      client.invalidateQueries({ queryKey: ["boards"] })
    },
    onError: (e) => toast.error(cap(String(e))),
  })

  const d = query.data
  return (
    <>
      <PageHeader
        title="Trang tuyển dụng"
        description="Index tăng dần tin đang mở trên các trang tuyển dụng, khớp profile nhà tuyển dụng với doanh nghiệp."
      />
      {query.isError && <ErrorState error={query.error} />}
      {query.isLoading && <Loading rows={10} />}
      {d && (
        <div className="space-y-6">
          {d.queue.error ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Không đọc được hàng đợi index</AlertTitle>
              <AlertDescription>
                <p>{cap(d.queue.error)}</p>
              </AlertDescription>
            </Alert>
          ) : (
            <StatGrid className="grid-cols-2 lg:grid-cols-4">
              <StatCard
                label="Worker index"
                value={num(d.queue.workers)}
                status={d.queue.workers ? { tone: "good", text: "Đang chạy" } : { tone: "warn", text: "Đang dừng" }}
              />
              <StatCard label="Trang đang index" value={num(d.queue.active)} />
              <StatCard label="Trang đang chờ" value={num(d.queue.queued)} />
              <StatCard
                label="Tin đang mở trên các trang"
                value={num(d.boards.reduce((n, b) => n + b.active_postings, 0))}
                hint={`${num(d.boards.reduce((n, b) => n + b.profiles_mapped, 0))} profile đã khớp doanh nghiệp`}
              />
            </StatGrid>
          )}

          <Section title="Các trang" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Trang</TableHead>
                  <TableHead className="text-right">Đang mở</TableHead>
                  <TableHead className="text-right">Đã index</TableHead>
                  <TableHead>Profile khớp</TableHead>
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
                      <TextLink to={`/jobs?source=${b.source}`} className="text-brand">
                        {num(b.active_postings)}
                      </TextLink>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{num(b.postings)}</TableCell>
                    <TableCell className="w-36 min-w-32">
                      <div className="flex items-center justify-between text-meta tabular-nums">
                        <span>
                          {num(b.profiles_mapped)}/{num(b.profiles)}
                        </span>
                        {b.profiles > 0 && (
                          <span className="text-muted-foreground">{Math.round((b.profiles_mapped / b.profiles) * 100)}%</span>
                        )}
                      </div>
                      {b.profiles > 0 && <Progress value={(b.profiles_mapped / b.profiles) * 100} className="mt-1.5 h-1.5" />}
                    </TableCell>
                    <TableCell className="min-w-52 text-meta whitespace-normal">
                      {b.last_status ? (
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <StatusBadge value={b.last_status} />
                            <span className="text-muted-foreground">{cap(ago(b.last_finished ?? b.last_started))}</span>
                          </div>
                          <div className="text-muted-foreground">
                            Thấy {num(b.last_urls_seen)} · tải {num(b.last_fetched)} · mới {num(b.last_new)} · gỡ {num(b.last_removed)}
                            {!b.last_complete && " · danh sách chưa đủ"}
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
                        onConfirm={() => index.mutate(b.source)}
                        size="xs"
                      >
                        Index ngay
                      </ConfirmButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          <Section title="Các lượt index gần đây" bodyClassName="p-0">
            <PagedList<BoardRun> queryKey={["board-runs"]} url="/boards/runs" empty="Chưa có lượt index nào.">
              {(runs) => (
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
                      <TableHead className="text-right" title="Profile nhà tuyển dụng mới / khớp được doanh nghiệp">
                        Profile
                      </TableHead>
                      <TableHead>Ghi chú</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {runs.map((r) => (
                      <TableRow key={r.id}>
                        <TableCell>
                          <div className="font-medium">{sourceLabel(r.source)}</div>
                          <div className="text-meta whitespace-nowrap text-muted-foreground tabular-nums">{dt(r.started_at)}</div>
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={r.status} />
                        </TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.urls_seen)}</TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.fetched)}</TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.new)}</TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.updated)}</TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.failed)}</TableCell>
                        <TableCell className="px-2 text-right tabular-nums">{num(r.removed)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.profiles_new} / {r.profiles_matched}
                        </TableCell>
                        <TableCell className="max-w-40 truncate text-meta text-muted-foreground" title={r.error ?? ""}>
                          {[!r.complete && "Danh sách chưa đủ", r.error && cap(r.error)].filter(Boolean).join(". ") || "–"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </PagedList>
          </Section>
        </div>
      )}
    </>
  )
}
