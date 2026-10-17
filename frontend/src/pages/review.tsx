import { useQuery } from "@tanstack/react-query"
import { ExternalLinkIcon, SkipForward } from "lucide-react"
import { useEffect, useState } from "react"
import { Link } from "react-router"

import {
  Empty,
  ErrorState,
  JobTags,
  Loading,
  PageHeader,
  ReasonTags,
  Section,
  StatCard,
  StatusBadge,
} from "@/components/common"
import { JobBody, ReviewBox } from "@/components/job"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, date, num, pct, provinces, salary, sourceKind } from "@/lib/format"
import type { ReviewPage as ReviewData } from "@/lib/types"

export function ReviewPage() {
  const [skip, setSkip] = useState<number | null>(null)
  const query = useQuery({
    queryKey: ["review", skip],
    queryFn: () => api.get<ReviewData>(`/review${qs({ skip })}`),
    placeholderData: (prev) => prev,
  })
  const d = query.data
  const next = d?.next

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase()
      if (["input", "textarea", "select"].includes(tag) || e.ctrlKey || e.metaKey) return
      if ((e.key === "n" || e.key === "N") && next) setSkip(next.job.id)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [next])

  return (
    <>
      <PageHeader
        title="Kiểm tay"
        description="Đánh dấu từng job đúng/sai để đo precision khi chưa có golden set. Job được chọn ngẫu nhiên trong các job đang mở chưa kiểm."
      />
      {query.isError && <ErrorState error={query.error} />}
      {!d && query.isLoading && <Loading rows={8} />}
      {d && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatCard
              label="Precision"
              value={pct(d.precision)}
              hint={`mục tiêu ≥ ${pct(d.target, 0)}`}
              tone={d.precision === null ? undefined : d.precision >= d.target ? "good" : "bad"}
            />
            <StatCard label="Đã kiểm" value={num(d.reviewed)} hint={`${num(d.correct)} đúng · ${num(d.reviewed - d.correct)} sai`} />
            <StatCard label="Chưa kiểm" value={num(d.unreviewed_active)} hint="job đang mở" />
          </div>

          <div className="grid gap-6 xl:grid-cols-[1fr_360px]">
            {next ? (
              <Section
                title={
                  <Link to={`/jobs/${next.job.id}`} className="text-base hover:underline">
                    {next.job.title}
                  </Link>
                }
                actions={
                  <Button variant="outline" size="sm" onClick={() => setSkip(next.job.id)} disabled={query.isFetching}>
                    <SkipForward /> Bỏ qua <kbd className="rounded border px-1 text-[10px]">N</kbd>
                  </Button>
                }
                bodyClassName="space-y-4"
              >
                <div className="space-y-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link to={`/companies/${next.job.company_id}`} className="font-medium text-primary hover:underline">
                      {next.job.company_name}
                    </Link>
                    <span>· {provinces(next.job)}</span>
                    <span>· {salary(next.job.salary)}</span>
                    <JobTags job={next.job} />
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    Nguồn {next.job.canonical_source} · đăng {date(next.job.posted_at)} · hạn {date(next.job.expires_at)} · tin cậy{" "}
                    {pct(next.job.confidence, 0)} <ReasonTags reasons={next.job.verification_reasons} />
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {next.postings.map((p) => (
                      <Button key={p.id} variant="outline" size="xs" asChild>
                        <a href={p.url} target="_blank" rel="noopener noreferrer">
                          {sourceKind(p.source)} <ExternalLinkIcon />
                        </a>
                      </Button>
                    ))}
                  </div>
                </div>
                <ReviewBox detail={next} shortcuts onDone={() => setSkip(null)} />
                <div className="rounded-lg border bg-muted/30 p-4">
                  <JobBody detail={next} />
                </div>
              </Section>
            ) : (
              <Section>
                <Empty>Không còn job đang mở nào chưa kiểm.</Empty>
              </Section>
            )}

            <aside className="space-y-4">
              <Section title="Theo nguồn" bodyClassName="p-0">
                {d.by_source.length ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="pl-4">Nguồn</TableHead>
                        <TableHead className="text-right">Đã kiểm</TableHead>
                        <TableHead className="pr-4 text-right">Precision</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {d.by_source.map((s) => (
                        <TableRow key={s.source}>
                          <TableCell className="pl-4">{s.source}</TableCell>
                          <TableCell className="text-right tabular-nums">{s.reviewed}</TableCell>
                          <TableCell className="pr-4 text-right tabular-nums">{pct(s.precision)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <Empty>Chưa kiểm job nào.</Empty>
                )}
              </Section>
              <Section
                title="Vừa kiểm"
                actions={
                  <Link to="/jobs?reviewed=wrong&status=all" className="text-xs text-primary hover:underline">
                    Job đánh dấu sai →
                  </Link>
                }
              >
                {d.recent.length ? (
                  <ul className="space-y-2.5 text-sm">
                    {d.recent.map((r) => (
                      <li key={r.job_id}>
                        <div className="flex items-start gap-2">
                          <StatusBadge value={r.correct ? "ok" : "failed"} text={r.correct ? "Đúng" : "Sai"} />
                          <Link to={`/jobs/${r.job_id}`} className="min-w-0 hover:underline">
                            {r.title}
                          </Link>
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {r.company_name} · {ago(r.reviewed_at)}
                          {r.note && <> · {r.note}</>}
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">Chưa có.</p>
                )}
              </Section>
            </aside>
          </div>
        </div>
      )}
    </>
  )
}
