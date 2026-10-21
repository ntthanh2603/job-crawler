import { ExternalLinkIcon } from "lucide-react"
import { useParams } from "react-router"

import {
  ErrorState,
  JobTags,
  Meta,
  PageHeader,
  PageLoading,
  ReasonTags,
  Section,
  StatusBadge,
  Tag,
  TextLink,
} from "@/components/common"
import { JobBody, JobFacts, PostingsTable, ReviewSection } from "@/components/job"
import { Button } from "@/components/ui/button"
import { cap, date, displayName, dt, education, num, pct, sourceLabel } from "@/lib/format"
import { useGetJob } from "@/lib/gen/queries"

export function JobPage() {
  const { id = "" } = useParams()
  const query = useGetJob(id)
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data) return <PageLoading />
  const d = query.data
  const j = d.job

  return (
    <>
      <PageHeader
        crumbs={[
          { to: "/jobs", label: "Việc làm" },
          { to: `/companies/${j.company_id}`, label: displayName(j.company_name) },
        ]}
        title={cap(j.title)}
        actions={
          j.apply_url && (
            <Button asChild>
              <a href={j.apply_url} target="_blank" rel="noopener noreferrer">
                Trang ứng tuyển <ExternalLinkIcon />
              </a>
            </Button>
          )
        }
      >
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm">
          <TextLink to={`/companies/${j.company_id}`} className="font-medium text-foreground">
            {displayName(j.company_name)}
          </TextLink>
          <StatusBadge value={j.status} />
          {d.review && (
            <StatusBadge value={d.review.correct ? "correct" : "wrong"} text={d.review.correct ? "Kiểm tay: đúng" : "Kiểm tay: sai"} />
          )}
          <JobTags job={j} />
        </div>
      </PageHeader>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0 space-y-6">
          <JobFacts job={j} />
          <Section title="Mô tả công việc" bodyClassName="px-5 py-5 sm:px-6">
            <JobBody detail={d} />
          </Section>
          <Section
            title="Tin theo nguồn"
            description={`${num(d.postings.length)} tin được gộp thành việc làm này`}
            bodyClassName="p-0"
          >
            <PostingsTable detail={d} />
          </Section>
        </div>
        <aside>
          <div className="space-y-4 xl:sticky xl:top-6">
            <ReviewSection detail={d} />
            <Section title="Chi tiết">
              <Meta
                items={[
                  [
                    "Ngành nghề",
                    j.categories.length ? (
                      <div className="flex flex-wrap gap-1">
                        {j.categories.map((c) => (
                          <TextLink key={c} to={`/jobs?category=${encodeURIComponent(c)}`} className="hover:no-underline">
                            <Tag className="hover:bg-secondary/70">{c}</Tag>
                          </TextLink>
                        ))}
                      </div>
                    ) : (
                      "Chưa rõ"
                    ),
                  ],
                  ["Học vấn", j.education === "unknown" ? "Không ghi" : education(j.education)],
                  ["Số lượng tuyển", j.openings ? `${num(j.openings)} người` : "Không ghi"],
                  [
                    "Kỹ năng",
                    j.skills.length ? (
                      <div className="flex flex-wrap gap-1">
                        {j.skills.map((s) => (
                          <Tag key={s}>{s}</Tag>
                        ))}
                      </div>
                    ) : (
                      "Không ghi"
                    ),
                  ],
                  ["Ngày đăng", date(j.posted_at)],
                  ["Nguồn chính", sourceLabel(j.canonical_source)],
                  [
                    "Độ tin cậy",
                    <div className="space-y-1.5">
                      <span className="font-medium tabular-nums">{pct(j.confidence, 0)}</span>
                      <div>
                        <ReasonTags reasons={j.verification_reasons} />
                      </div>
                    </div>,
                  ],
                  ["Thấy lần đầu", dt(j.first_seen_at)],
                  ["Thấy lần cuối", dt(j.last_seen_at)],
                ]}
              />
            </Section>
          </div>
        </aside>
      </div>
    </>
  )
}
