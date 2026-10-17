import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { ConfirmButton, Empty, ErrorState, Loading, PageHeader, Section, StatusBadge, TextLink } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ago, cap, dt, num, sourceLabel } from "@/lib/format"
import type { Boards } from "@/lib/types"

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
          ) : d.queue.workers ? (
            <Alert>
              <RefreshCw />
              <AlertTitle>
                {d.queue.workers} worker index đang chạy · {d.queue.queued} trang đang chờ · {d.queue.active} trang đang index
              </AlertTitle>
            </Alert>
          ) : (
            <Alert className="border-warning/30 bg-warning/8">
              <AlertTriangle className="text-warning" />
              <AlertTitle>Chưa có worker index nào chạy</AlertTitle>
              <AlertDescription>
                <p>
                  Bật service indexer (đặt <code>INDEXER_REPLICAS=1</code> rồi chạy <code>docker compose up -d</code>) để các
                  lượt index được thực hiện. Nên có proxy trước khi index đầy đủ.
                </p>
              </AlertDescription>
            </Alert>
          )}

          <Section title="Các trang" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Trang</TableHead>
                  <TableHead className="text-right">Tin đang mở</TableHead>
                  <TableHead className="text-right">Đã index</TableHead>
                  <TableHead>Profile đã khớp</TableHead>
                  <TableHead>Lượt index cuối</TableHead>
                  <TableHead className="pr-4 text-right">Thao tác</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.boards.map((b) => (
                  <TableRow key={b.source}>
                    <TableCell className="pl-4 font-medium">{sourceLabel(b.source)}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      <TextLink to={`/jobs?source=${b.source}`} className="text-brand">
                        {num(b.active_postings)}
                      </TextLink>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{num(b.postings)}</TableCell>
                    <TableCell className="w-44 min-w-36">
                      <div className="flex items-center justify-between text-xs tabular-nums">
                        <span>
                          {num(b.profiles_mapped)}/{num(b.profiles)}
                        </span>
                        {b.profiles > 0 && (
                          <span className="text-muted-foreground">{Math.round((b.profiles_mapped / b.profiles) * 100)}%</span>
                        )}
                      </div>
                      {b.profiles > 0 && <Progress value={(b.profiles_mapped / b.profiles) * 100} className="mt-1.5 h-1.5" />}
                    </TableCell>
                    <TableCell className="min-w-60 text-xs whitespace-normal">
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
                    <TableCell className="pr-4 text-right">
                      <ConfirmButton
                        title={`Index ${sourceLabel(b.source)} ngay?`}
                        description="Đưa trang này vào hàng đợi index. Đây là việc dài, chạy ở worker index (worker --boards)."
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
            {d.runs.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-4">Trang</TableHead>
                    <TableHead>Kết quả</TableHead>
                    <TableHead className="text-right">Thấy</TableHead>
                    <TableHead className="text-right">Tải</TableHead>
                    <TableHead className="text-right">Mới</TableHead>
                    <TableHead className="text-right">Đổi</TableHead>
                    <TableHead className="text-right">Lỗi</TableHead>
                    <TableHead className="text-right">Gỡ</TableHead>
                    <TableHead className="text-right" title="Profile nhà tuyển dụng mới / khớp được doanh nghiệp">
                      Profile mới / khớp
                    </TableHead>
                    <TableHead className="pr-4">Ghi chú</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.runs.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="pl-4">
                        <div className="font-medium">{sourceLabel(r.source)}</div>
                        <div className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">{dt(r.started_at)}</div>
                      </TableCell>
                      <TableCell>
                        <StatusBadge value={r.status} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.urls_seen)}</TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.fetched)}</TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.new)}</TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.updated)}</TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.failed)}</TableCell>
                      <TableCell className="text-right tabular-nums">{num(r.removed)}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {r.profiles_new} / {r.profiles_matched}
                      </TableCell>
                      <TableCell className="max-w-56 truncate pr-4 text-xs text-muted-foreground" title={r.error ?? ""}>
                        {[!r.complete && "Danh sách chưa đủ", r.error && cap(r.error)].filter(Boolean).join(". ") || "–"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Empty>Chưa có lượt index nào.</Empty>
            )}
          </Section>
        </div>
      )}
    </>
  )
}
