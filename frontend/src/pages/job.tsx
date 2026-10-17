import { useQuery } from "@tanstack/react-query"
import { ExternalLinkIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import { ErrorState, JobTags, Loading, Meta, PageHeader, ReasonTags, Section, StatusBadge, Tag } from "@/components/common"
import { JobBody, PostingsTable, ReviewSection } from "@/components/job"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { date, dt, pct, salary } from "@/lib/format"
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
        crumbs={[{ to: "/jobs", label: "Job" }]}
        title={j.title}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <Link to={`/companies/${j.company_id}`} className="text-primary hover:underline">
              {j.company_name}
            </Link>
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

      <div className="grid gap-6 xl:grid-cols-[1fr_360px]">
        <div className="min-w-0 space-y-6">
          <Section title="Mô tả công việc">
            <JobBody detail={d} />
          </Section>
          <Section title="Tin theo nguồn" description={`${d.postings.length} tin gộp thành job này`} bodyClassName="p-0">
            <PostingsTable detail={d} />
          </Section>
        </div>
        <aside className="space-y-4">
          <div className="xl:sticky xl:top-6 space-y-4">
            <ReviewSection detail={d} />
            <Section title="Thông tin">
              <Meta
                items={[
                  [
                    "Nơi làm",
                    j.locations.length ? (
                      <div className="space-y-0.5">
                        {j.locations.map((l, i) => (
                          <div key={i}>
                            {l.province}
                            {l.address_raw && l.address_raw !== l.province && (
                              <span className="text-xs text-muted-foreground"> {l.address_raw}</span>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      "–"
                    ),
                  ],
                  [
                    "Lương",
                    <>
                      {salary(j.salary)}
                      {j.salary?.raw && <div className="text-xs text-muted-foreground">{j.salary.raw}</div>}
                    </>,
                  ],
                  ["Kinh nghiệm", exp ? `${exp.min ?? "?"}–${exp.max ?? "?"} năm` : "–"],
                  [
                    "Kỹ năng",
                    j.skills.length ? (
                      <div className="flex flex-wrap gap-1">
                        {j.skills.map((s) => (
                          <Tag key={s}>{s}</Tag>
                        ))}
                      </div>
                    ) : (
                      "–"
                    ),
                  ],
                  ["Đăng", date(j.posted_at)],
                  ["Hạn nộp", date(j.expires_at)],
                  ["Nguồn chuẩn", <span className="break-all">{j.canonical_source}</span>],
                  [
                    "Độ tin cậy",
                    <div className="space-y-1">
                      {pct(j.confidence, 0)}
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
