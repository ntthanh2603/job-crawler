import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, RefreshCw } from "lucide-react"
import { Link } from "react-router"
import { toast } from "sonner"

import { ConfirmButton, Empty, ErrorState, Loading, PageHeader, Section, StatusBadge } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { ago, dt, num } from "@/lib/format"
import type { Boards } from "@/lib/types"

export function BoardsPage() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ["boards"], queryFn: () => api.get<Boards>("/boards"), refetchInterval: 30_000 })
  const index = useMutation({
    mutationFn: (name: string) => api.post<{ queued: boolean; workers: number }>(`/boards/${name}/index`),
    onSuccess: (r, name) => {
      if (!r.queued) toast.warning(`${name} đang chờ/đang index`)
      else if (!r.workers) toast.warning(`Đã đưa ${name} vào queue nhưng chưa có worker index nào chạy`)
      else toast.success(`Đã đưa ${name} vào queue index`)
      client.invalidateQueries({ queryKey: ["boards"] })
    },
    onError: (e) => toast.error(String(e)),
  })

  const d = query.data
  return (
    <>
      <PageHeader
        title="Trang tuyển dụng"
        description={d ? `Index tăng dần vào job_postings (queue ${d.queue.name})` : "Đang tải…"}
      />
      {query.isError && <ErrorState error={query.error} />}
      {query.isLoading && <Loading rows={10} />}
      {d && (
        <div className="space-y-6">
          {d.queue.error ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>Không đọc được queue</AlertTitle>
              <AlertDescription>{d.queue.error}</AlertDescription>
            </Alert>
          ) : d.queue.workers ? (
            <Alert>
              <RefreshCw />
              <AlertTitle>
                {d.queue.workers} worker index đang chạy · {d.queue.queued} chờ · {d.queue.active} đang index
              </AlertTitle>
            </Alert>
          ) : (
            <Alert>
              <AlertTriangle />
              <AlertTitle>Chưa có worker index</AlertTitle>
              <AlertDescription>
                <p>
                  Bật service indexer (<code>INDEXER_REPLICAS=1</code> rồi <code>docker compose up -d</code>) để lượt index
                  chạy; nên có proxy trước khi index đầy đủ.
                </p>
              </AlertDescription>
            </Alert>
          )}

          <Section bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Trang</TableHead>
                  <TableHead className="text-right">Tin đang mở</TableHead>
                  <TableHead className="text-right">Đã index</TableHead>
                  <TableHead>Profile đã map</TableHead>
                  <TableHead>Lượt cuối</TableHead>
                  <TableHead className="pr-4" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {d.boards.map((b) => (
                  <TableRow key={b.source}>
                    <TableCell className="pl-4 font-medium">{b.source}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      <Link to={`/jobs?source=${b.source}`} className="text-primary hover:underline">
                        {num(b.active_postings)}
                      </Link>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{num(b.postings)}</TableCell>
                    <TableCell className="w-48">
                      <div className="text-xs tabular-nums">
                        {num(b.profiles_mapped)}/{num(b.profiles)}
                      </div>
                      {b.profiles > 0 && <Progress value={(b.profiles_mapped / b.profiles) * 100} className="mt-1 h-1.5" />}
                    </TableCell>
                    <TableCell className="text-xs whitespace-normal">
                      {b.last_status ? (
                        <div className="space-y-0.5">
                          <div className="flex items-center gap-1.5">
                            <StatusBadge value={b.last_status} />
                            <span className="text-muted-foreground">{ago(b.last_finished ?? b.last_started)}</span>
                          </div>
                          <div className="text-muted-foreground">
                            thấy {num(b.last_urls_seen)} · tải {num(b.last_fetched)} · mới {num(b.last_new)} · gỡ {num(b.last_removed)}
                            {!b.last_complete && " · danh sách chưa đủ"}
                          </div>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">chưa index</span>
                      )}
                    </TableCell>
                    <TableCell className="pr-4 text-right">
                      <ConfirmButton
                        title={`Index ${b.source} ngay?`}
                        description="Đưa trang này vào queue index (job dài, chạy ở worker --boards)."
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
                    <TableHead>Bắt đầu</TableHead>
                    <TableHead>Trạng thái</TableHead>
                    <TableHead className="text-right">Thấy</TableHead>
                    <TableHead className="text-right">Tải</TableHead>
                    <TableHead className="text-right">Mới</TableHead>
                    <TableHead className="text-right">Đổi</TableHead>
                    <TableHead className="text-right">Lỗi</TableHead>
                    <TableHead className="text-right">Gỡ</TableHead>
                    <TableHead className="text-right">Profile mới/map</TableHead>
                    <TableHead className="pr-4">Ghi chú</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.runs.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="pl-4">{r.source}</TableCell>
                      <TableCell className="text-xs whitespace-nowrap">{dt(r.started_at)}</TableCell>
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
                        {r.profiles_new}/{r.profiles_matched}
                      </TableCell>
                      <TableCell className="max-w-72 truncate pr-4 text-xs text-muted-foreground" title={r.error ?? ""}>
                        {!r.complete && "danh sách chưa đủ. "}
                        {r.error}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Empty>Chưa có lượt index.</Empty>
            )}
          </Section>
        </div>
      )}
    </>
  )
}
