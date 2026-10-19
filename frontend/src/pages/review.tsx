import { useQuery } from "@tanstack/react-query"
import { ExternalLinkIcon, PartyPopper, SkipForward } from "lucide-react"
import { useEffect, useState } from "react"

import {
  Empty,
  ErrorState,
  JobTags,
  Kbd,
  PageHeader,
  PageLoading,
  ReasonTags,
  Section,
  StatCard,
  StatGrid,
  StatusBadge,
  TextLink,
} from "@/components/common"
import { JobBody, JobFacts, ReviewBox } from "@/components/job"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ago, cap, displayName, num, pct, sourceLabel } from "@/lib/format"
import { reviewQuery } from "@/lib/queries"
import { cn } from "@/lib/utils"

export function ReviewPage() {
  const [skip, setSkip] = useState<string | null>(null)
  const query = useQuery({ ...reviewQuery(skip), placeholderData: (prev) => prev })
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
      {query.isError && !d && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {!d && query.isLoading && <PageLoading />}
      {d && (
        <div className="space-y-6">
          <StatGrid className="sm:grid-cols-3">
            <StatCard
              label="Precision"
              value={pct(d.precision)}
              hint={`Mục tiêu ≥ ${pct(d.target, 0)}${d.reviewed ? "" : " · cần kiểm vài chục việc để có số"}`}
              status={
                d.precision === null
                  ? undefined
                  : d.precision >= d.target
                    ? { tone: "good", text: "Đạt" }
                    : { tone: "bad", text: "Chưa đạt" }
              }
            />
            <StatCard
              label="Đã kiểm"
              value={num(d.reviewed)}
              hint={`${num(d.correct)} đúng · ${num(d.reviewed - d.correct)} sai`}
            />
            <StatCard label="Chưa kiểm" value={num(d.unreviewed_active)} hint="Việc làm đang mở" />
          </StatGrid>

          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
            {next ? (
              <div className={cn("min-w-0 space-y-4 transition-opacity", query.isFetching && "opacity-60")}>
                <Section bodyClassName="space-y-4">
                  <div className="space-y-2">
                    <TextLink to={`/jobs/${next.job.id}`} className="text-lg leading-snug font-semibold">
                      {cap(next.job.title)}
                    </TextLink>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                      <TextLink to={`/companies/${next.job.company_id}`} className="font-medium">
                        {displayName(next.job.company_name)}
                      </TextLink>
                      <JobTags job={next.job} />
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-muted-foreground">
                      <span>Nguồn {sourceLabel(next.job.canonical_source)}</span>
                      <span>· Tin cậy {pct(next.job.confidence, 0)}</span>
                      <ReasonTags reasons={next.job.verification_reasons} hideSource />
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {next.postings.map((p) => (
                      <Button key={p.id} variant="outline" size="sm" asChild>
                        <a href={p.url} target="_blank" rel="noopener noreferrer">
                          Mở trên {sourceLabel(p.source)} <ExternalLinkIcon />
                        </a>
                      </Button>
                    ))}
                  </div>
                </Section>
                <JobFacts job={next.job} />
                <Section title="Nội dung tin" bodyClassName="px-5 py-5 sm:px-6">
                  <JobBody detail={next} />
                </Section>
              </div>
            ) : (
              <Section>
                <Empty icon={<PartyPopper />} title="Đã kiểm hết">
                  Không còn việc làm đang mở nào chưa kiểm.
                </Empty>
              </Section>
            )}

            <aside className="space-y-4">
              {next && (
                <div className="xl:sticky xl:top-6">
                  <Section
                    title="Đánh giá"
                    actions={
                      <Button variant="ghost" size="sm" onClick={() => setSkip(next.job.id)} disabled={query.isFetching}>
                        <SkipForward /> Bỏ qua <Kbd>N</Kbd>
                      </Button>
                    }
                  >
                    <ReviewBox detail={next} shortcuts onDone={() => setSkip(null)} />
                    <p className="mt-3 text-meta text-muted-foreground">
                      Phím tắt: <Kbd>D</Kbd> đúng · <Kbd>S</Kbd> sai · <Kbd>N</Kbd> bỏ qua.
                    </p>
                  </Section>
                </div>
              )}
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
                          <TableCell className="text-right tabular-nums">{num(s.reviewed)}</TableCell>
                          <TableCell className="text-right font-medium tabular-nums">{pct(s.precision)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <Empty className="py-10">Chưa kiểm việc làm nào.</Empty>
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
                      <li key={r.job_id} className="space-y-1">
                        <div className="flex items-start gap-2">
                          <StatusBadge value={r.correct ? "correct" : "wrong"} />
                          <TextLink to={`/jobs/${r.job_id}`} className="min-w-0 pt-0.5 leading-5">
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
