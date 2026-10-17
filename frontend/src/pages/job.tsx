import { useQuery } from "@tanstack/react-query"
import { ExternalLinkIcon } from "lucide-react"
import { useParams } from "react-router"

import { ErrorState, JobTags, Loading, Meta, PageHeader, ReasonTags, Section, StatusBadge, Tag, TextLink } from "@/components/common"
import { JobBody, PostingsTable, ReviewSection } from "@/components/job"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { cap, date, displayName, dt, pct, salary, sourceLabel } from "@/lib/format"
import type { JobDetail } from "@/lib/types"

export function JobPage() {
  const { id } = useParams()
  const query = useQuery({ queryKey: ["job", id], queryFn: () => api.get<JobDetail>(`/jobs/${id}`) })
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading rows={10} />
  const d = query.data
  const j = d.job
  const exp = j.experience_years

  return (
    <>
      <PageHeader
        crumbs={[{ to: "/jobs", label: "Việc làm" }]}
        title={cap(j.title)}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <TextLink to={`/companies/${j.company_id}`} className="font-medium text-foreground">
              {displayName(j.company_name)}
            </TextLink>
            <StatusBadge value={j.status} />
            <JobTags job={j} />
          </span>
        }
        actions={
          j.apply_url && (
            <Button size="sm" asChild>
              <a href={j.apply_url} target="_blank" rel="noopener noreferrer">
                Trang ứng tuyển <ExternalLinkIcon />
              </a>
            </Button>
          )
        }
      />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0 space-y-6">
          <Section title="Mô tả công việc" bodyClassName="px-5 py-5">
            <JobBody detail={d} />
          </Section>
          <Section title="Tin theo nguồn" description={`${d.postings.length} tin được gộp thành việc làm này`} bodyClassName="p-0">
            <PostingsTable detail={d} />
          </Section>
        </div>
        <aside>
          <div className="space-y-4 xl:sticky xl:top-6">
            <ReviewSection detail={d} />
            <Section title="Thông tin">
              <Meta
                items={[
                  [
                    "Nơi làm",
                    j.locations.length ? (
                      <div className="space-y-1">
                        {j.locations.map((l, i) => (
                          <div key={i}>
                            {l.province || "–"}
                            {l.address_raw && l.address_raw !== l.province && (
                              <div className="text-meta text-muted-foreground">{cap(l.address_raw)}</div>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      "Chưa rõ"
                    ),
                  ],
                  [
                    "Lương",
                    <>
                      {salary(j.salary)}
                      {j.salary?.raw && <div className="text-meta text-muted-foreground">{cap(j.salary.raw)}</div>}
                    </>,
                  ],
                  ["Kinh nghiệm", exp ? `${exp.min ?? "?"}–${exp.max ?? "?"} năm` : "Không ghi"],
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
                  ["Hạn nộp", date(j.expires_at)],
                  ["Nguồn chính", sourceLabel(j.canonical_source)],
                  [
                    "Độ tin cậy",
                    <div className="space-y-1.5">
                      <span className="tabular-nums">{pct(j.confidence, 0)}</span>
                      <ReasonTags reasons={j.verification_reasons} />
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
