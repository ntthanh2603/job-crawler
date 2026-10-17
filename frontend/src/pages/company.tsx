import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLinkIcon, Play } from "lucide-react"
import { Link, useParams } from "react-router"
import { toast } from "sonner"

import {
  Empty,
  ErrorState,
  ExternalLink,
  JobTags,
  Loading,
  Meta,
  PageHeader,
  Section,
  StatusBadge,
  Tag,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import { ago, companyName, date, dt, duration, pct, provinces, salary, sourceKind } from "@/lib/format"
import type { CompanyDetail, EnqueueResult } from "@/lib/types"

export function CompanyPage() {
  const { id } = useParams()
  const client = useQueryClient()
  const query = useQuery({
    queryKey: ["company", id],
    queryFn: () => api.get<CompanyDetail>(`/companies/${id}`),
  })
  const run = useMutation({
    mutationFn: () => api.post<EnqueueResult>("/enqueue", { company_ids: [Number(id)] }),
    onSuccess: (r) => {
      if (r.enqueued) toast.success("Đã đưa vào queue — lượt chạy mới sẽ hiện sau ít phút")
      else toast.warning("Doanh nghiệp đang có job chờ/chạy trong queue")
      client.invalidateQueries({ queryKey: ["company", id] })
    },
    onError: (e) => toast.error(String(e)),
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
          <span className="flex flex-wrap items-center gap-2">
            {c.kyc_status ? <StatusBadge value={c.kyc_status} /> : <Tag>chưa KYC</Tag>}
            {c.kyc_confidence !== null && <span>độ tin cậy {pct(c.kyc_confidence, 0)}</span>}
            {c.kyc_at && <span>· KYC {ago(c.kyc_at)}</span>}
            <span>· #{c.id}</span>
          </span>
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <Link to={`/jobs?company_id=${c.id}&status=all`}>Mọi job</Link>
            </Button>
            <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
              <Play /> Chạy lại
            </Button>
          </>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[1fr_360px]">
        <Tabs defaultValue="jobs" className="min-w-0">
          <TabsList>
            <TabsTrigger value="jobs">Job đang mở ({d.jobs.length})</TabsTrigger>
            <TabsTrigger value="runs">Lượt chạy ({d.runs.length})</TabsTrigger>
            <TabsTrigger value="rejected">Tin bị loại ({d.rejected.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="jobs">
            <Section
              bodyClassName="p-0"
              description={d.inactive_jobs ? `${d.inactive_jobs} job đã hết hạn / đã gỡ không hiển thị` : undefined}
            >
              {d.jobs.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Vị trí</TableHead>
                      <TableHead>Nơi làm</TableHead>
                      <TableHead>Lương</TableHead>
                      <TableHead>Nguồn</TableHead>
                      <TableHead className="pr-4">Hạn nộp</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.jobs.map((j) => (
                      <TableRow key={j.id}>
                        <TableCell className="max-w-96 min-w-64 pl-4 whitespace-normal">
                          <Link to={`/jobs/${j.id}`} className="font-medium hover:underline">
                            {j.title}
                          </Link>
                          <div className="mt-1">
                            <JobTags job={j} />
                          </div>
                        </TableCell>
                        <TableCell className="text-xs">{provinces(j)}</TableCell>
                        <TableCell className="text-xs whitespace-nowrap">{salary(j.salary)}</TableCell>
                        <TableCell className="text-xs">
                          {sourceKind(j.canonical_source)}
                          {j.source_urls.length > 1 && <span className="text-muted-foreground"> +{j.source_urls.length - 1}</span>}
                        </TableCell>
                        <TableCell className="pr-4 text-xs whitespace-nowrap">{date(j.expires_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa có job đang mở.</Empty>
              )}
            </Section>
          </TabsContent>

          <TabsContent value="runs">
            <Section bodyClassName="p-0">
              {d.runs.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Bắt đầu</TableHead>
                      <TableHead>Trạng thái</TableHead>
                      <TableHead className="text-right">Thời gian</TableHead>
                      <TableHead className="text-right">Nhận/thấy</TableHead>
                      <TableHead className="pr-4">Nguồn chưa xong / lỗi</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.runs.map((r) => (
                      <TableRow key={r.run_id}>
                        <TableCell className="pl-4 whitespace-nowrap">
                          <Link to={`/runs/${r.run_id}`} className="text-primary hover:underline">
                            {dt(r.started_at)}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={r.status} />
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{duration(r.duration_ms)}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.jobs_accepted}/{r.jobs_found}
                        </TableCell>
                        <TableCell className="max-w-96 pr-4 text-xs whitespace-normal">
                          {r.incomplete_sources.join(", ")}
                          {r.error && <div className="mt-0.5 font-mono text-muted-foreground">{r.error.slice(0, 200)}</div>}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Chưa chạy lần nào.</Empty>
              )}
            </Section>
          </TabsContent>

          <TabsContent value="rejected">
            <Section bodyClassName="p-0">
              {d.rejected.length ? (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="pl-4">Tin</TableHead>
                      <TableHead>Nguồn</TableHead>
                      <TableHead>Lý do</TableHead>
                      <TableHead className="pr-4">Lúc</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.rejected.map((p) => (
                      <TableRow key={p.id}>
                        <TableCell className="max-w-80 pl-4 whitespace-normal">
                          <ExternalLink href={p.url}>{p.title || p.url}</ExternalLink>
                        </TableCell>
                        <TableCell className="text-xs">{sourceKind(p.source)}</TableCell>
                        <TableCell className="max-w-96 text-xs whitespace-normal">
                          {p.verification_reasons
                            .filter((r) => !r.startsWith("SOURCE_"))
                            .map((r) => (
                              <div key={r}>{r}</div>
                            ))}
                        </TableCell>
                        <TableCell className="pr-4 text-xs text-muted-foreground">{ago(p.verified_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Không có tin bị loại.</Empty>
              )}
            </Section>
          </TabsContent>
        </Tabs>

        <aside className="space-y-4">
          <Section title="Thông tin">
            <Meta
              items={[
                ["Tên nhập", c.input_name],
                ...(c.legal_name ? [["Tên pháp lý", c.legal_name] as [string, string]] : []),
                ...(c.brand_name ? [["Thương hiệu", c.brand_name] as [string, string]] : []),
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
                ...(c.address ? [["Địa chỉ", c.address] as [string, string]] : []),
                ...(d.note ? [["Ghi chú", d.note] as [string, string]] : []),
                ["Thêm vào", dt(d.created_at)],
              ]}
            />
          </Section>

          <Section title="Bằng chứng KYC">
            {ev.reason && <p className="mb-2 text-sm">{ev.reason}</p>}
            {ev.signals && ev.signals.length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {ev.signals.map((s) => (
                  <Tag key={s}>{s}</Tag>
                ))}
              </div>
            )}
            {ev.evidence?.length ? (
              <ul className="space-y-2 text-xs">
                {ev.evidence.map((e, i) => (
                  <li key={i}>
                    <ExternalLink href={e.url} className="break-all">
                      {e.url}
                    </ExternalLink>{" "}
                    <span className="text-muted-foreground">({e.kind})</span>
                    <div className="text-muted-foreground">{e.snippet}</div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Chưa có.</p>
            )}
          </Section>

          <Section title="Profile trên trang tuyển dụng">
            {d.profiles.length ? (
              <ul className="space-y-3 text-sm">
                {d.profiles.map((p) => (
                  <li key={p.url}>
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium">{p.source}</span>
                      <ExternalLink href={p.url} className="inline-flex items-center gap-1 truncate">
                        {p.name_on_source} <ExternalLinkIcon className="size-3" />
                      </ExternalLink>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      khớp {p.match_confidence === null ? "–" : pct(p.match_confidence, 0)}
                      {p.match_signal && ` theo ${p.match_signal}`} · index {ago(p.indexed_at)}
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Chưa map profile nào.</p>
            )}
          </Section>

          <Section title="Tên gọi khác">
            <div className="flex flex-wrap gap-1">
              {d.aliases.map((a) => (
                <Tag key={a.alias} title={a.kind ?? ""}>
                  {a.alias}
                </Tag>
              ))}
            </div>
          </Section>

          {d.failed_pages.length > 0 && (
            <Section title="Trang parse lỗi">
              <ul className="space-y-2 text-xs">
                {d.failed_pages.map((f) => (
                  <li key={f.id}>
                    <Link to={`/failed-pages/${f.id}`} className="break-all text-primary hover:underline">
                      {f.url}
                    </Link>
                    <div className="text-muted-foreground">
                      {f.reason} · {ago(f.created_at)}
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
