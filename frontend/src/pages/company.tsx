import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLinkIcon, Play } from "lucide-react"
import { useState } from "react"
import { Link, useParams } from "react-router"
import { toast } from "sonner"

import {
  ErrorState,
  ExternalLink,
  FilterSelect,
  JobTags,
  Loading,
  Meta,
  PagedList,
  PageHeader,
  Section,
  StatusBadge,
  Tag,
  TextLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
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
  num,
  pct,
  provinces,
  salary,
  sourceLabel,
} from "@/lib/format"
import type { CompanyDetail, EnqueueResult, Job, Posting, Run } from "@/lib/types"

const SIGNALS: Record<string, string> = {
  name: "tên",
  mst: "MST",
  website: "website",
  llm: "LLM",
  tax_code: "MST",
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

/** Việc làm của doanh nghiệp theo trang, chuyển giữa đang mở / đã đóng. */
function CompanyJobs({ id, active, inactive }: { id: string; active: number; inactive: number }) {
  const [status, setStatus] = useState<"active" | "inactive">("active")
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
            ["active", "Đang mở"],
            ["inactive", "Đã đóng"],
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
                <TableRow key={j.id}>
                  <TableCell className="max-w-96 min-w-56 whitespace-normal">
                    <TextLink to={`/jobs/${j.id}`} className="font-medium">
                      {cap(j.title)}
                    </TextLink>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
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

export function CompanyPage() {
  const { id } = useParams()
  const client = useQueryClient()
  const query = useQuery({
    queryKey: ["company", id],
    queryFn: () => api.get<CompanyDetail>(`/companies/${id}`),
  })
  const run = useMutation({
    mutationFn: () => api.post<EnqueueResult>("/enqueue", { company_ids: [id] }),
    onSuccess: (r) => {
      if (r.enqueued) toast.success("Đã đưa vào hàng đợi. Lượt chạy mới sẽ hiện sau ít phút.")
      else toast.warning("Doanh nghiệp đang có việc chờ hoặc đang chạy trong hàng đợi")
      client.invalidateQueries({ queryKey: ["company", id] })
    },
    onError: (e) => toast.error(cap(String(e))),
  })

  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading rows={10} />
  const d = query.data
  const c = d.company
  const ev = d.kyc_evidence ?? {}

  return (
    <>
      <PageHeader
        crumbs={[{ to: "/companies", label: "Doanh nghiệp" }]}
        title={companyName(c)}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {c.kyc_status ? <StatusBadge value={c.kyc_status} /> : <StatusBadge value="none" tone="muted" />}
            {c.kyc_confidence !== null && <span>Độ tin cậy {pct(c.kyc_confidence, 0)}</span>}
            {c.kyc_at && <span>· KYC {ago(c.kyc_at)}</span>}
            <span className="tabular-nums">· Mã #{c.id}</span>
          </span>
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <Link to={`/jobs?company_id=${c.id}&status=all`}>Mọi việc làm</Link>
            </Button>
            <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
              <Play /> Chạy lại
            </Button>
          </>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Tabs defaultValue="jobs" className="min-w-0 gap-4">
          <TabsList>
            {(
              [
                ["jobs", "Việc làm", d.active_jobs + d.inactive_jobs],
                ["runs", "Lượt chạy", d.runs_total],
                ["rejected", "Tin bị loại", d.rejected_total],
              ] as const
            ).map(([value, text, n]) => (
              <TabsTrigger key={value} value={value} className="px-3">
                {text}
                <span className="text-meta font-normal text-muted-foreground tabular-nums">{num(n)}</span>
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="jobs">
            <CompanyJobs id={c.id} active={d.active_jobs} inactive={d.inactive_jobs} />
          </TabsContent>

          <TabsContent value="runs">
            <Section bodyClassName="p-0" title="Các lượt chạy" description="Mới nhất trước">
              <PagedList<Run> queryKey={["company-runs", c.id]} url={`/companies/${c.id}/runs`} empty="Chưa chạy lần nào.">
                {(runs) => (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Bắt đầu</TableHead>
                        <TableHead>Kết quả</TableHead>
                        <TableHead className="text-right">Nhận / thấy</TableHead>
                        <TableHead>Nguồn chưa xong, lỗi</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {runs.map((r) => (
                        <TableRow key={r.run_id}>
                          <TableCell className="whitespace-nowrap tabular-nums">
                            <TextLink to={`/runs/${r.run_id}`} className="text-brand">
                              {dt(r.started_at)}
                            </TextLink>
                          </TableCell>
                          <TableCell>
                            <StatusBadge value={r.status} />
                            <div className="text-meta text-muted-foreground tabular-nums">{duration(r.duration_ms)}</div>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {r.jobs_accepted}/{r.jobs_found}
                          </TableCell>
                          <TableCell className="max-w-96 text-meta whitespace-normal [overflow-wrap:anywhere]">
                            {r.incomplete_sources.length > 0 && r.incomplete_sources.map(sourceLabel).join(", ")}
                            {r.error && <div className="mt-0.5 font-mono text-muted-foreground">{r.error.slice(0, 200)}</div>}
                            {!r.incomplete_sources.length && !r.error && <span className="text-muted-foreground">Không có</span>}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </PagedList>
            </Section>
          </TabsContent>

          <TabsContent value="rejected">
            <Section bodyClassName="p-0" title="Tin bị loại" description="Kèm lý do xác minh, mới nhất trước">
              <PagedList<Posting>
                queryKey={["company-rejected", c.id]}
                url={`/companies/${c.id}/rejected`}
                empty="Không có tin bị loại."
              >
                {(postings) => (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Tin</TableHead>
                        <TableHead>Nguồn</TableHead>
                        <TableHead>Lý do</TableHead>
                        <TableHead>Thời điểm</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {postings.map((p) => (
                        <TableRow key={p.id}>
                          <TableCell className="max-w-80 min-w-56 whitespace-normal">
                            <ExternalLink href={p.url}>{cap(p.title || p.url)}</ExternalLink>
                          </TableCell>
                          <TableCell className="text-meta">{sourceLabel(p.source)}</TableCell>
                          <TableCell className="max-w-96 text-meta whitespace-normal">
                            {p.verification_reasons
                              .filter((r) => !r.startsWith("SOURCE_"))
                              .map((r) => (
                                <div key={r}>
                                  <span className="font-mono text-caption text-muted-foreground">{r.split(":")[0]}</span>
                                  {r.includes(":") && <> · {cap(r.slice(r.indexOf(":") + 1).trim())}</>}
                                </div>
                              ))}
                          </TableCell>
                          <TableCell className="text-meta text-muted-foreground">{cap(ago(p.verified_at))}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </PagedList>
            </Section>
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
                ...(c.other_domains.length ? [["Domain khác", c.other_domains.join(", ")] as [string, string]] : []),
                ...(d.input_website ? [["Website nhập", d.input_website] as [string, string]] : []),
                ...(c.address ? [["Địa chỉ", cap(c.address)] as [string, string]] : []),
                ...(d.note ? [["Ghi chú", cap(d.note)] as [string, string]] : []),
                ["Thêm vào", dt(d.created_at)],
              ]}
            />
          </Section>

          <Section title="Bằng chứng KYC">
            {ev.reason && <p className="mb-3 text-sm">{cap(ev.reason.replace(/^rule:\s*/, ""))}</p>}
            {ev.signals && ev.signals.length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {ev.signals.map((s) => (
                  <Tag key={s}>{SIGNALS[s] ?? s.replaceAll("_", " ")}</Tag>
                ))}
              </div>
            )}
            {ev.evidence?.length ? (
              <ul className="space-y-3 text-meta">
                {ev.evidence.map((e, i) => (
                  <li key={i} className="space-y-0.5">
                    <ExternalLink href={e.url} className="break-all">
                      {e.url}
                    </ExternalLink>
                    <div className="text-muted-foreground">
                      {cap(SIGNALS[e.kind] ?? e.kind.replaceAll("_", " "))}: {e.snippet}
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

          <Section title="Tên gọi khác">
            <div className="flex flex-wrap gap-1">
              {d.aliases.map((a) => (
                <Tag key={a.alias} title={ALIAS_KINDS[a.kind ?? ""] ?? a.kind ?? ""}>
                  {a.alias}
                </Tag>
              ))}
            </div>
          </Section>

          {d.failed_pages.length > 0 && (
            <Section title="Trang parse lỗi">
              <ul className="space-y-2.5 text-meta">
                {d.failed_pages.map((f) => (
                  <li key={f.id}>
                    <TextLink to={`/failed-pages/${f.id}`} className="break-all text-brand">
                      {f.url}
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
