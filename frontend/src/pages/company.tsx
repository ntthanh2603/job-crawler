import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Briefcase, ExternalLinkIcon, Play } from "lucide-react"
import { useState, type ReactNode } from "react"
import { Link, useParams } from "react-router"
import { toast } from "sonner"

import {
  ErrorState,
  ExternalLink,
  FilterSelect,
  IncompleteSources,
  JobTags,
  Meta,
  PagedList,
  PageHeader,
  PageLoading,
  ReasonList,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  TabBar,
  Tag,
  TextLink,
  toneOf,
  useRowLink,
  useTabParam,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import {
  ago,
  cap,
  companyName,
  date,
  displayName,
  dt,
  duration,
  failReason,
  label,
  num,
  pct,
  provinces,
  salary,
  shortUrl,
  sourceLabel,
} from "@/lib/format"
import type { CompanyDetail, EnqueueResult, Job, Page, Posting, Run } from "@/lib/types"

const SIGNALS: Record<string, string> = {
  name: "tên",
  mst: "MST",
  website: "website",
  llm: "LLM",
  tax_code: "MST",
  input: "website nhập",
  input_domain: "website nhập",
  site_name: "tên trên website",
  domain_name: "tên domain",
}

const ALIAS_KINDS: Record<string, string> = {
  input: "Tên nhập",
  legal: "Tên pháp lý",
  short: "Tên ngắn",
  brand: "Thương hiệu",
  english: "Tên tiếng Anh",
}

const TABS = ["jobs", "runs", "rejected"] as const

/** Việc làm của doanh nghiệp theo trang, chuyển giữa đang mở / đã đóng. */
function CompanyJobs({ id, active, inactive }: { id: string; active: number; inactive: number }) {
  const [status, setStatus] = useState<"active" | "inactive">("active")
  const rowLink = useRowLink()
  return (
    <Section
      bodyClassName="p-0"
      title={status === "active" ? "Việc làm đang mở" : "Việc làm đã đóng"}
      description={status === "active" ? `${num(active)} việc` : `${num(inactive)} việc đã hết hạn hoặc đã gỡ`}
      actions={
        <FilterSelect
          label="Trạng thái"
          value={status}
          onChange={(v) => setStatus(v === "inactive" ? "inactive" : "active")}
          options={[
            ["active", `Đang mở (${num(active)})`],
            ["inactive", `Đã đóng (${num(inactive)})`],
          ]}
          all={null}
        />
      }
    >
      <PagedList<Job>
        key={status}
        queryKey={["company-jobs", id, status]}
        url={`/companies/${id}/jobs?status=${status}`}
        empty={status === "active" ? "Chưa có việc làm đang mở." : "Không có việc làm đã đóng."}
      >
        {(jobs) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Vị trí</TableHead>
                <TableHead>Nơi làm</TableHead>
                <TableHead>Lương</TableHead>
                <TableHead>Hạn nộp</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {jobs.map((j) => (
                <TableRow key={j.id} {...rowLink(`/jobs/${j.id}`)}>
                  <TableCell className="max-w-96 min-w-56 whitespace-normal">
                    <TextLink to={`/jobs/${j.id}`} className="font-medium">
                      {cap(j.title)}
                    </TextLink>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      {j.status !== "active" && <StatusBadge value={j.status} />}
                      <JobTags job={j} />
                      <span className="text-meta text-muted-foreground">
                        {sourceLabel(j.canonical_source)}
                        {j.source_urls.length > 1 && ` +${j.source_urls.length - 1}`}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="max-w-48 text-meta whitespace-normal">{provinces(j)}</TableCell>
                  <TableCell className="text-meta whitespace-nowrap">{salary(j.salary)}</TableCell>
                  <TableCell className="text-meta whitespace-nowrap tabular-nums">{date(j.expires_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </PagedList>
    </Section>
  )
}

function CompanyRuns({ id }: { id: string }) {
  const rowLink = useRowLink()
  return (
    <Section bodyClassName="p-0" title="Các lượt chạy" description="Mới nhất trước · bấm một dòng để xem request và log">
      <PagedList<Run> queryKey={["company-runs", id]} url={`/companies/${id}/runs`} empty="Chưa chạy lần nào.">
        {(runs) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Bắt đầu</TableHead>
                <TableHead>Kết quả</TableHead>
                <TableHead className="text-right">Tin nhận / thấy</TableHead>
                <TableHead>Nguồn chưa xong, lỗi</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((r) => (
                <TableRow key={r.run_id} {...rowLink(`/runs/${r.run_id}`)}>
                  <TableCell className="whitespace-nowrap tabular-nums">
                    <TextLink to={`/runs/${r.run_id}`} className="font-medium">
                      {dt(r.started_at)}
                    </TextLink>
                    <div className="text-meta text-muted-foreground">{cap(ago(r.started_at))}</div>
                  </TableCell>
                  <TableCell>
                    <StatusBadge value={r.status} />
                    <div className="mt-0.5 text-meta text-muted-foreground tabular-nums">{duration(r.duration_ms)}</div>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    <span className="font-medium">{num(r.jobs_accepted)}</span>
                    <span className="text-muted-foreground"> / {num(r.jobs_found)}</span>
                  </TableCell>
                  <TableCell className="max-w-96 min-w-48 text-meta whitespace-normal [overflow-wrap:anywhere]">
                    <IncompleteSources sources={r.incomplete_sources} />
                    {r.error && <div className="mt-1 font-mono text-danger-ink">{r.error.slice(0, 200)}</div>}
                    {!r.incomplete_sources.length && !r.error && <span className="text-muted-foreground">–</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </PagedList>
    </Section>
  )
}

function CompanyRejected({ id }: { id: string }) {
  return (
    <Section bodyClassName="p-0" title="Tin bị loại" description="Tin không qua bước xác minh, kèm lý do · mới nhất trước">
      <PagedList<Posting> queryKey={["company-rejected", id]} url={`/companies/${id}/rejected`} empty="Không có tin bị loại.">
        {(postings) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Tin</TableHead>
                <TableHead>Lý do loại</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {postings.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="w-[40%] max-w-96 min-w-56 align-top whitespace-normal">
                    <ExternalLink href={p.url} className="font-medium text-foreground hover:text-brand">
                      {cap(p.title || shortUrl(p.url))}
                    </ExternalLink>
                    <div className="mt-0.5 text-meta text-muted-foreground">
                      {sourceLabel(p.source)} · {ago(p.verified_at ?? p.last_seen_at)}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-[36rem] min-w-64 align-top whitespace-normal">
                    <ReasonList reasons={p.verification_reasons} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </PagedList>
    </Section>
  )
}

export function CompanyPage() {
  const { id } = useParams()
  const client = useQueryClient()
  const [tab, setTab] = useTabParam(TABS, "jobs")
  const query = useQuery({
    queryKey: ["company", id],
    queryFn: () => api.get<CompanyDetail>(`/companies/${id}`),
  })
  // Trang 1 các lượt chạy (cùng khoá với tab "Lượt chạy" nên chỉ tải một lần).
  const runs = useQuery({
    queryKey: ["company-runs", id, 1],
    queryFn: () => api.get<Page<Run>>(`/companies/${id}/runs?page=1`),
  })
  const run = useMutation({
    mutationFn: () => api.post<EnqueueResult>("/enqueue", { company_ids: [id] }),
    onSuccess: (r) => {
      if (r.enqueued) toast.success("Đã đưa vào hàng đợi. Lượt chạy mới sẽ hiện sau ít phút.")
      else toast.warning("Doanh nghiệp đang có việc chờ hoặc đang chạy trong hàng đợi")
      client.invalidateQueries({ queryKey: ["company", id] })
      client.invalidateQueries({ queryKey: ["queue-summary"] })
    },
    onError: (e) => toast.error(cap(e instanceof Error ? e.message : String(e))),
  })

  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data) return <PageLoading />
  const d = query.data
  const c = d.company
  const ev = d.kyc_evidence ?? {}
  const shown = companyName(c)
  const last = runs.data?.items[0]

  return (
    <>
      <PageHeader
        crumbs={[{ to: "/companies", label: "Doanh nghiệp" }]}
        title={shown}
        description={c.legal_name && displayName(c.legal_name) !== shown ? displayName(c.legal_name) : undefined}
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to={`/jobs?company_id=${c.id}&status=all`}>
                <Briefcase /> Mọi việc làm
              </Link>
            </Button>
            <Button onClick={() => run.mutate()} disabled={run.isPending}>
              <Play /> {run.isPending ? "Đang xếp hàng…" : "Chạy lại"}
            </Button>
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm text-muted-foreground">
          {c.kyc_status ? <StatusBadge value={c.kyc_status} /> : <StatusBadge value="none" tone="muted" />}
          {c.kyc_confidence !== null && <span>Độ tin cậy {pct(c.kyc_confidence, 0)}</span>}
          {c.primary_domain && (
            <ExternalLink href={`https://${c.primary_domain}`} className="inline-flex items-center gap-1">
              {c.primary_domain} <ExternalLinkIcon className="size-3" />
            </ExternalLink>
          )}
          {c.tax_code && <span className="tabular-nums">MST {c.tax_code}</span>}
        </div>
      </PageHeader>

      <StatGrid className="mb-6 grid-cols-2 xl:grid-cols-4">
        <StatCard label="Việc đang mở" value={num(d.active_jobs)} hint={`${num(d.inactive_jobs)} việc đã đóng`} />
        <StatCard
          label="Lượt chạy mới nhất"
          value={last ? cap(ago(last.started_at)) : runs.isLoading ? "…" : "Chưa chạy"}
          status={last ? { tone: toneOf(last.status), text: label(last.status) } : undefined}
          hint={
            last ? (
              <span className="flex flex-wrap items-center gap-1.5">
                {duration(last.duration_ms)} · nhận {num(last.jobs_accepted)}/{num(last.jobs_found)} tin
                <IncompleteSources sources={last.incomplete_sources} />
              </span>
            ) : undefined
          }
        />
        <StatCard label="Tin bị loại" value={num(d.rejected_total)} hint="Không qua bước xác minh" />
        <StatCard
          label="Profile trang tuyển dụng"
          value={num(d.profiles.length)}
          hint={d.profiles.length ? [...new Set(d.profiles.map((p) => sourceLabel(p.source)))].join(", ") : "Chưa khớp profile nào"}
        />
      </StatGrid>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Tabs value={tab} onValueChange={setTab} className="min-w-0 gap-4">
          <TabBar
            items={[
              { value: "jobs", label: "Việc làm", count: d.active_jobs + d.inactive_jobs },
              { value: "runs", label: "Lượt chạy", count: d.runs_total },
              { value: "rejected", label: "Tin bị loại", count: d.rejected_total },
            ]}
          />
          <TabsContent value="jobs">
            <CompanyJobs id={c.id} active={d.active_jobs} inactive={d.inactive_jobs} />
          </TabsContent>
          <TabsContent value="runs">
            <CompanyRuns id={c.id} />
          </TabsContent>
          <TabsContent value="rejected">
            <CompanyRejected id={c.id} />
          </TabsContent>
        </Tabs>

        <aside className="space-y-4">
          <Section title="Thông tin">
            <Meta
              items={[
                ["Tên nhập", displayName(c.input_name)],
                ...(c.legal_name ? [["Tên pháp lý", displayName(c.legal_name)] as [string, string]] : []),
                ...(c.brand_name ? [["Thương hiệu", cap(c.brand_name)] as [string, string]] : []),
                ["MST", c.tax_code || "–"],
                [
                  "Website",
                  c.primary_domain ? (
                    <ExternalLink href={`https://${c.primary_domain}`}>{c.primary_domain}</ExternalLink>
                  ) : (
                    "–"
                  ),
                ],
                ...(c.other_domains.length
                  ? [["Domain khác", <span className="break-all">{c.other_domains.join(", ")}</span>] as [string, ReactNode]]
                  : []),
                ...(d.input_website
                  ? [
                      [
                        "Website nhập",
                        <ExternalLink
                          href={/^https?:/.test(d.input_website) ? d.input_website : `https://${d.input_website}`}
                          className="break-all"
                        >
                          {shortUrl(d.input_website)}
                        </ExternalLink>,
                      ] as [string, ReactNode],
                    ]
                  : []),
                ...(c.address ? [["Địa chỉ", cap(c.address)] as [string, string]] : []),
                ...(d.note ? [["Ghi chú", cap(d.note)] as [string, string]] : []),
                ["Thêm vào", dt(d.created_at)],
              ]}
            />
          </Section>

          <Section title="Bằng chứng KYC" description={c.kyc_at ? `Xác minh ${ago(c.kyc_at)}` : undefined}>
            {ev.reason && <p className="mb-3 text-sm">{cap(ev.reason.replace(/^rule:\s*/, ""))}</p>}
            {ev.signals && ev.signals.length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {ev.signals.map((s) => (
                  <Tag key={s}>{SIGNALS[s] ?? s.replaceAll("_", " ")}</Tag>
                ))}
              </div>
            )}
            {ev.evidence?.length ? (
              <ul className="space-y-3 border-t pt-3 text-meta">
                {ev.evidence.map((e, i) => (
                  <li key={i} className="space-y-0.5">
                    <ExternalLink href={e.url} className="break-all">
                      {shortUrl(e.url)}
                    </ExternalLink>
                    <div className="text-muted-foreground">
                      <span className="font-medium text-foreground/80">{cap(SIGNALS[e.kind] ?? e.kind.replaceAll("_", " "))}:</span>{" "}
                      {e.snippet}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Chưa có bằng chứng.</p>
            )}
          </Section>

          <Section title="Profile trên trang tuyển dụng">
            {d.profiles.length ? (
              <ul className="space-y-3 text-sm">
                {d.profiles.map((p) => (
                  <li key={p.url} className="space-y-0.5">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="shrink-0 font-medium">{sourceLabel(p.source)}</span>
                      <ExternalLink href={p.url} className="inline-flex min-w-0 items-center gap-1">
                        <span className="truncate">{cap(p.name_on_source)}</span>
                        <ExternalLinkIcon className="size-3 shrink-0" />
                      </ExternalLink>
                    </div>
                    <div className="text-meta text-muted-foreground">
                      Khớp {p.match_confidence === null ? "–" : pct(p.match_confidence, 0)}
                      {p.match_signal && ` theo ${SIGNALS[p.match_signal] ?? p.match_signal}`} · index {ago(p.indexed_at)}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Chưa khớp profile nào.</p>
            )}
          </Section>

          {d.aliases.length > 0 && (
            <Section title="Tên gọi khác" description="Dùng để khớp tin và profile với doanh nghiệp">
              <div className="flex flex-wrap gap-1">
                {d.aliases.map((a, i) => (
                  <Tag key={`${a.alias}-${a.kind}-${i}`} title={ALIAS_KINDS[a.kind ?? ""] ?? a.kind ?? ""} className="h-6 text-meta">
                    {a.alias}
                  </Tag>
                ))}
              </div>
            </Section>
          )}

          {d.failed_pages.length > 0 && (
            <Section title="Trang parse lỗi" description="HTML lưu 7 ngày để xem lại">
              <ul className="space-y-2.5 text-meta">
                {d.failed_pages.map((f) => (
                  <li key={f.id}>
                    <TextLink to={`/failed-pages/${f.id}`} className="break-all text-brand">
                      {shortUrl(f.url)}
                    </TextLink>
                    <div className="text-muted-foreground">
                      {failReason(f.reason)} · {ago(f.created_at)}
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          )}
        </aside>
      </div>
    </>
  )
}
