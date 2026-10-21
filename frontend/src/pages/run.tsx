import { AlertTriangle, Building2, CircleAlert } from "lucide-react"
import { Link, useParams } from "react-router"

import {
  Dot,
  Empty,
  ErrorState,
  ExternalLink,
  IncompleteList,
  PagedList,
  PageHeader,
  PageLoading,
  Section,
  SortHead,
  StatCard,
  StatGrid,
  StatusBadge,
  TabBar,
  TextLink,
  toneOf,
  useTabParam,
  type Tone,
} from "@/components/common"
import { RunTags } from "@/components/crawl"
import { LiveStatus, useLiveRun } from "@/components/live"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import {
  getGetRunFetchesQueryOptions,
  getGetRunLogsQueryOptions,
  type GetRunFetchesSortBy,
  type GetRunLogsSortBy,
} from "@/lib/gen/queries"
import { ago, byCount, cap, companyName, dt, duration, failReason, hour, label, num, shortUrl, sourceLabel } from "@/lib/format"
import type { RunDetail, SortState } from "@/lib/types"
import { cn } from "@/lib/utils"

const LEVELS: Record<string, [Tone, string]> = {
  debug: ["muted", "Gỡ lỗi"],
  info: ["info", "Thông tin"],
  warning: ["warn", "Cảnh báo"],
  error: ["bad", "Lỗi"],
  critical: ["bad", "Nghiêm trọng"],
}

const TABS = ["fetches", "logs"] as const

// Request theo thứ tự gửi, log theo thời gian (cũ trước), như lượt chạy diễn ra.
const FETCH_SORT: SortState<GetRunFetchesSortBy> = { sortBy: "created_at", sortOrder: "ASC" }
const LOG_SORT: SortState<GetRunLogsSortBy> = { sortBy: "ts", sortOrder: "ASC" }

function value(v: unknown): string {
  const text = typeof v === "object" ? JSON.stringify(v) : String(v)
  return text.length > 160 ? `${text.slice(0, 160)}…` : text
}

/** Ngữ cảnh của một dòng log: các cặp khoá–giá trị (bỏ giá trị rỗng) thay cho JSON thô. */
function LogContext({ context }: { context: Record<string, unknown> }) {
  const entries = Object.entries(context).filter(
    ([, v]) => v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0),
  )
  if (!entries.length) return null
  return (
    <dl className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-meta">
      {entries.slice(0, 14).map(([k, v]) => (
        <div key={k} className="flex min-w-0 gap-1">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-all text-foreground/80">{value(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Giải thích lượt chạy chưa trọn vẹn: nguồn còn dở và lỗi. */
function RunProblems({ run }: { run: RunDetail["run"] }) {
  if (!run.incomplete_sources.length && !run.error) return null
  const failed = run.status === "failed"
  return (
    <div
      className={cn(
        "mb-6 rounded-xl border p-4 sm:p-5",
        failed ? "border-destructive/30 bg-destructive/5" : "border-warning/35 bg-warning/8",
      )}
    >
      <div className="flex items-start gap-3">
        {failed ? (
          <CircleAlert className="mt-0.5 size-5 shrink-0 text-destructive" />
        ) : (
          <AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning-ink" />
        )}
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <div className="font-medium">
              {run.incomplete_sources.length
                ? `Lượt chạy chưa đọc hết ${num(run.incomplete_sources.length)} nguồn`
                : "Lượt chạy gặp lỗi"}
            </div>
            {run.incomplete_sources.length > 0 && (
              <p className="text-meta text-muted-foreground">
                {run.kind === "poll"
                  ? "Tin đã thu được vẫn được lưu; việc làm của nguồn lỗi không bị coi là đã gỡ. Lỗi tạm lặp lại nhiều lượt, hoặc bộ khung hỏng (trang chính lỗi, cấu trúc đổi), thì bộ khung được build lại."
                  : "Tin đã thu được vẫn được lưu. Việc làm cũ của nguồn chưa xong không bị coi là đã gỡ; hệ thống tự xếp lượt chạy tiếp (sau khoảng một phút, có giới hạn số lần) để đọc nốt phần còn lại."}
              </p>
            )}
          </div>
          {run.incomplete_sources.length > 0 && (
            <div className="rounded-lg bg-card/70 p-3.5">
              <IncompleteList items={run.incomplete_sources.map((source) => ({ source }))} />
            </div>
          )}
          {run.error && (
            <pre className="overflow-x-auto rounded-lg bg-card/70 p-3 font-mono text-meta whitespace-pre-wrap text-danger-ink">
              {run.error}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

export function RunPage() {
  const { id = "" } = useParams()
  const [tab, setTab] = useTabParam(TABS, "fetches")
  // Lượt đang chạy: tiến độ, số request / log và danh sách log cập nhật trực tiếp.
  const { query, state } = useLiveRun(id)

  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data) return <PageLoading />
  const d = query.data
  const r = d.run
  const name = d.company_name ? companyName({ brand_name: null, legal_name: null, input_name: d.company_name }) : "Doanh nghiệp"

  return (
    <>
      <PageHeader
        crumbs={[
          { to: "/companies", label: "Doanh nghiệp" },
          { to: `/companies/${r.company_id}?tab=runs`, label: name },
        ]}
        title={`${r.kind === "poll" ? "Lượt poll" : r.kind === "build" ? "Lượt build" : "Lượt chạy"} ${dt(r.started_at)}`}
        actions={
          <Button variant="outline" asChild>
            <Link to={`/companies/${r.company_id}`}>
              <Building2 /> Xem doanh nghiệp
            </Link>
          </Button>
        }
      >
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm text-muted-foreground">
          <StatusBadge value={r.status} />
          <RunTags kind={r.kind} trigger={r.trigger} />
          <span>{cap(ago(r.started_at))}</span>
          <code className="truncate font-mono text-meta text-muted-foreground/80" title="Mã lượt chạy">
            {r.run_id}
          </code>
          {r.status === "running" && <LiveStatus state={state} pollSeconds={5} />}
        </div>
      </PageHeader>

      <StatGrid className="mb-6 grid-cols-2 xl:grid-cols-4">
        <StatCard label="Thời gian" value={duration(r.duration_ms)} hint={r.finished_at ? `Xong lúc ${hour(r.finished_at)}` : "Chưa xong"} />
        <StatCard
          label="Việc làm mới / đóng"
          value={
            <>
              <span className={r.jobs_new ? "text-success-ink" : undefined}>+{num(r.jobs_new)}</span>
              <span className="text-muted-foreground"> / {num(r.jobs_closed)}</span>
            </>
          }
          hint="Việc làm thấy lần đầu / đã đóng (hết hạn, gỡ)"
        />
        <StatCard
          label="Tin nhận / thấy"
          value={
            <>
              {num(r.jobs_accepted)}
              <span className="text-muted-foreground"> / {num(r.jobs_found)}</span>
            </>
          }
          hint="Sau xác minh và gộp trùng"
        />
        <StatCard
          label="Request"
          value={num(d.fetches_total)}
          hint={`${num(d.by_source.length)} nguồn${d.failed_pages.length ? ` · ${num(d.failed_pages.length)} trang parse lỗi` : ""}`}
        />
      </StatGrid>

      <RunProblems run={r} />

      <div className="space-y-6">
        <div className={cn("grid gap-6", d.failed_pages.length > 0 && "xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]")}>
          <Section title="Theo nguồn" description="Số request và kết quả" bodyClassName="p-0">
            {d.by_source.length ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Nguồn</TableHead>
                    <TableHead className="text-right">Request</TableHead>
                    <TableHead>Kết quả</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.by_source.map((s) => (
                    <TableRow key={s.source}>
                      <TableCell className="max-w-80">
                        <div className="font-medium">{sourceLabel(s.source)}</div>
                        <div className="truncate font-mono text-meta text-muted-foreground" title={s.source}>
                          {s.source}
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{num(s.total)}</TableCell>
                      <TableCell className="whitespace-normal">
                        <div className="flex flex-wrap gap-x-2.5 gap-y-1 text-meta">
                          {byCount(s.outcomes).map(([k, v]) => (
                            <Dot key={k} tone={toneOf(k)}>
                              {label(k)} <span className="font-medium tabular-nums">{num(v)}</span>
                            </Dot>
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
          </Section>
          {d.failed_pages.length > 0 && (
            <Section title="Trang parse lỗi" description="Bấm để xem HTML đã lưu">
              <ul className="space-y-2.5 text-meta">
                {d.failed_pages.map((f) => (
                  <li key={f.id}>
                    <TextLink to={`/failed-pages/${f.id}`} className="break-all text-brand">
                      {shortUrl(f.url)}
                    </TextLink>
                    <div className="text-muted-foreground">{failReason(f.reason)}</div>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </div>

        <Tabs value={tab} onValueChange={setTab} className="min-w-0 gap-4">
          <TabBar
            items={[
              { value: "fetches", label: "Request", count: d.fetches_total },
              { value: "logs", label: "Log", count: d.logs_total },
            ]}
          />
          <TabsContent value="fetches">
            <Section bodyClassName="p-0">
              <PagedList
                options={(p) => getGetRunFetchesQueryOptions(r.run_id, p)}
                defaultSort={FETCH_SORT}
                defaultLimit={50}
                searchPlaceholder="Tìm theo URL hoặc nguồn…"
                empty="Không có request nào.">
                {(fetches, { sort, onSort }) => (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <SortHead column="created_at" sort={sort} onSort={onSort}>
                          URL
                        </SortHead>
                        <SortHead column="http_status" sort={sort} onSort={onSort}>
                          Kết quả
                        </SortHead>
                        <SortHead column="items" sort={sort} onSort={onSort} className="text-right">
                          Số tin
                        </SortHead>
                        <SortHead column="duration_ms" sort={sort} onSort={onSort} className="text-right">
                          Thời gian
                        </SortHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {fetches.map((f, i) => (
                        <TableRow key={i}>
                          <TableCell className="max-w-80 min-w-48">
                            <ExternalLink href={f.url} className="block truncate text-meta" title={f.url}>
                              {shortUrl(f.url)}
                            </ExternalLink>
                            <div className="text-meta text-muted-foreground">
                              {sourceLabel(f.source)}
                              {f.http_status !== null && ` · HTTP ${f.http_status}`}
                            </div>
                          </TableCell>
                          <TableCell className="max-w-56 whitespace-normal">
                            <StatusBadge value={f.outcome} />
                            {f.error && (
                              <div className="mt-1 line-clamp-2 text-meta break-all text-muted-foreground" title={f.error}>
                                {cap(f.error)}
                              </div>
                            )}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {f.items ?? <span className="text-muted-foreground">–</span>}
                          </TableCell>
                          <TableCell className="text-right text-meta text-muted-foreground tabular-nums">
                            {duration(f.duration_ms)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </PagedList>
            </Section>
          </TabsContent>
          <TabsContent value="logs">
            <Section bodyClassName="p-0">
              <PagedList
                options={(p) => getGetRunLogsQueryOptions(r.run_id, p)}
                defaultSort={LOG_SORT}
                defaultLimit={100}
                searchPlaceholder="Tìm theo tên sự kiện…"
                empty="Không có log cho lượt chạy này.">
                {(logs) => (
                  <div className="divide-y">
                    {logs.map((l, i) => {
                      const [tone, text] = LEVELS[l.level] ?? ["muted", cap(l.level)]
                      return (
                        <div key={i} className="grid grid-cols-[3rem_minmax(0,1fr)] items-start gap-3 px-5 py-2.5 text-meta sm:grid-cols-[3rem_6.5rem_minmax(0,1fr)]">
                          <span className="pt-0.5 text-muted-foreground tabular-nums">{hour(l.ts)}</span>
                          <span className="max-sm:hidden">
                            <StatusBadge value={l.level} tone={tone} text={text} />
                          </span>
                          <div className="min-w-0">
                            <code className="font-mono font-medium text-foreground">{l.event}</code>
                            <LogContext context={l.context} />
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </PagedList>
            </Section>
          </TabsContent>
        </Tabs>
      </div>
    </>
  )
}
