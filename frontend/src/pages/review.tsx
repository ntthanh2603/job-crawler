import { useQuery } from "@tanstack/react-query"
import { ExternalLinkIcon, PartyPopper, SkipForward } from "lucide-react"
import { useEffect, useState } from "react"

import {
  Empty,
  ErrorState,
  JobTags,
  Kbd,
  Loading,
  PageHeader,
  ReasonTags,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  TextLink,
} from "@/components/common"
import { JobBody, ReviewBox } from "@/components/job"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, cap, date, displayName, num, pct, provinces, salary, sourceLabel } from "@/lib/format"
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
        description="Đánh dấu từng việc làm đúng hay sai để đo precision khi chưa có golden set. Việc làm được chọn ngẫu nhiên trong các tin đang mở chưa kiểm."
      />
      {query.isError && <ErrorState error={query.error} />}
      {!d && query.isLoading && <Loading rows={8} />}
      {d && (
        <div className="space-y-6">
          <StatGrid className="sm:grid-cols-3">
            <StatCard
              label="Precision"
              value={pct(d.precision)}
              hint={`Mục tiêu ≥ ${pct(d.target, 0)}`}
              status={
                d.precision === null
                  ? undefined
                  : d.precision >= d.target
                    ? { tone: "good", text: "Đạt" }
                    : { tone: "bad", text: "Chưa đạt" }
              }
            />
            <StatCard label="Đã kiểm" value={num(d.reviewed)} hint={`${num(d.correct)} đúng · ${num(d.reviewed - d.correct)} sai`} />
            <StatCard label="Chưa kiểm" value={num(d.unreviewed_active)} hint="Việc làm đang mở" />
          </StatGrid>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
            {next ? (
              <Section
                title={
                  <TextLink to={`/jobs/${next.job.id}`} className="text-base">
                    {cap(next.job.title)}
                  </TextLink>
                }
                actions={
                  <Button variant="outline" size="sm" onClick={() => setSkip(next.job.id)} disabled={query.isFetching}>
                    <SkipForward /> Bỏ qua
                    <Kbd>N</Kbd>
                  </Button>
                }
                bodyClassName="space-y-5"
              >
                <div className="space-y-2 text-sm">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <TextLink to={`/companies/${next.job.company_id}`} className="font-medium">
                      {displayName(next.job.company_name)}
                    </TextLink>
                    {provinces(next.job) !== "–" && <span className="text-muted-foreground">· {provinces(next.job)}</span>}
                    <span className="text-muted-foreground">
                      · {salary(next.job.salary) === "–" ? "Chưa rõ lương" : salary(next.job.salary)}
                    </span>
                    <JobTags job={next.job} />
                  </div>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-muted-foreground">
                    <span>Nguồn {sourceLabel(next.job.canonical_source)}</span>
                    <span>· Đăng {date(next.job.posted_at)}</span>
                    <span>· Hạn {date(next.job.expires_at)}</span>
                    <span>· Tin cậy {pct(next.job.confidence, 0)}</span>
                    <ReasonTags reasons={next.job.verification_reasons} />
                  </div>
                  <div className="flex flex-wrap gap-2 pt-1">
                    {next.postings.map((p) => (
                      <Button key={p.id} variant="outline" size="xs" asChild>
                        <a href={p.url} target="_blank" rel="noopener noreferrer">
                          Mở trên {sourceLabel(p.source)} <ExternalLinkIcon />
                        </a>
                      </Button>
                    ))}
                  </div>
                </div>
                <ReviewBox detail={next} shortcuts onDone={() => setSkip(null)} />
                <div className="rounded-lg border bg-muted/25 px-5 py-4">
                  <div className="mb-3 text-meta font-medium text-muted-foreground">Nội dung tin</div>
                  <JobBody detail={next} />
                </div>
              </Section>
            ) : (
              <Section>
                <Empty icon={<PartyPopper />}>Đã kiểm hết các việc làm đang mở.</Empty>
              </Section>
            )}

            <aside className="space-y-4">
              <Section title="Theo nguồn" bodyClassName="p-0">
                {d.by_source.length ? (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Nguồn</TableHead>
                        <TableHead className="text-right">Đã kiểm</TableHead>
                        <TableHead className="text-right">Precision</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {d.by_source.map((s) => (
                        <TableRow key={s.source}>
                          <TableCell>{sourceLabel(s.source)}</TableCell>
                          <TableCell className="text-right tabular-nums">{s.reviewed}</TableCell>
                          <TableCell className="text-right tabular-nums">{pct(s.precision)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <Empty>Chưa kiểm việc làm nào.</Empty>
                )}
              </Section>
              <Section
                title="Vừa kiểm"
                actions={
                  <TextLink to="/jobs?reviewed=wrong&status=all" className="text-meta text-brand">
                    Các việc bị đánh sai
                  </TextLink>
                }
              >
                {d.recent.length ? (
                  <ul className="space-y-3 text-sm">
                    {d.recent.map((r) => (
                      <li key={r.job_id} className="space-y-0.5">
                        <div className="flex items-start gap-2">
                          <StatusBadge value={r.correct ? "correct" : "wrong"} />
                          <TextLink to={`/jobs/${r.job_id}`} className="min-w-0 leading-5">
                            {cap(r.title)}
                          </TextLink>
                        </div>
                        <div className="text-meta text-muted-foreground">
                          {displayName(r.company_name)} · {ago(r.reviewed_at)}
                          {r.note && <> · {r.note}</>}
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">Chưa có đánh giá nào.</p>
                )}
              </Section>
            </aside>
          </div>
        </div>
      )}
    </>
  )
}
