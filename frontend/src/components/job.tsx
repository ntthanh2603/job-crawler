import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Banknote, CalendarClock, Check, GraduationCap, MapPin, X } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"
import { toast } from "sonner"

import { ExternalLink, Kbd, ReasonTags, Section, StatusBadge } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { ago, cap, date, pct, salary, shortUrl, sourceLabel } from "@/lib/format"
import {
  getGetJobQueryKey,
  getGetJobsQueryKey,
  getGetReviewQueryKey,
  postReview,
  useDeleteReview,
} from "@/lib/gen/queries"
import type { Job, JobDetail } from "@/lib/types"

/** Nội dung tin: HTML do server sinh từ markdown (markdown-it, không cho HTML thô). */
export function JobBody({ detail }: { detail: JobDetail }) {
  const { html } = detail
  return (
    <div className="prose-job">
      <div dangerouslySetInnerHTML={{ __html: html.description }} />
      {html.requirements && (
        <>
          <h3>Yêu cầu</h3>
          <div dangerouslySetInnerHTML={{ __html: html.requirements }} />
        </>
      )}
      {html.benefits && (
        <>
          <h3>Quyền lợi</h3>
          <div dangerouslySetInnerHTML={{ __html: html.benefits }} />
        </>
      )}
    </div>
  )
}

function Fact({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 gap-3 bg-card px-4 py-3.5 sm:px-5">
      <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-lg bg-primary/8 text-brand dark:bg-primary/15 [&_svg]:size-4 [&_svg]:stroke-[1.75]">
        {icon}
      </span>
      <div className="min-w-0">
        <div className="text-meta text-muted-foreground">{label}</div>
        <div className="text-sm font-medium break-words">{children}</div>
      </div>
    </div>
  )
}

/** Dải thông tin chính của việc làm: nơi làm, lương, kinh nghiệm, hạn nộp. */
export function JobFacts({ job }: { job: Job }) {
  const exp = job.experience_years
  const places = [...new Set(job.locations.map((l) => l.province).filter(Boolean))]
  const address = job.locations.find((l) => l.address_raw && l.address_raw !== l.province)?.address_raw
  return (
    <div className="grid gap-px overflow-hidden rounded-xl border bg-border shadow-card sm:grid-cols-2 2xl:grid-cols-4">
      <Fact icon={<MapPin />} label="Nơi làm">
        {places.length ? places.join(", ") : <span className="text-muted-foreground">Chưa rõ</span>}
        {address && (
          <div className="line-clamp-2 text-meta font-normal text-muted-foreground" title={cap(address)}>
            {cap(address)}
          </div>
        )}
      </Fact>
      <Fact icon={<Banknote />} label="Lương">
        {salary(job.salary) === "–" ? <span className="text-muted-foreground">Không ghi</span> : salary(job.salary)}
      </Fact>
      <Fact icon={<GraduationCap />} label="Kinh nghiệm">
        {exp && (exp.min !== null || exp.max !== null) ? (
          exp.min !== null && exp.max !== null ? (
            exp.min === exp.max ? `${exp.min} năm` : `${exp.min}–${exp.max} năm`
          ) : exp.min !== null ? (
            `Từ ${exp.min} năm`
          ) : (
            `Tới ${exp.max} năm`
          )
        ) : (
          <span className="text-muted-foreground">Không ghi</span>
        )}
      </Fact>
      <Fact icon={<CalendarClock />} label="Hạn nộp">
        {job.expires_at ? date(job.expires_at) : <span className="text-muted-foreground">Không ghi</span>}
        {job.posted_at && <div className="text-meta font-normal text-muted-foreground">Đăng {date(job.posted_at)}</div>}
      </Fact>
    </div>
  )
}

export function PostingsTable({ detail }: { detail: JobDetail }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Nguồn</TableHead>
          <TableHead>Trạng thái</TableHead>
          <TableHead>Xác minh</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {detail.postings.map((p) => (
          <TableRow key={p.id}>
            <TableCell className="max-w-72">
              <div className="text-sm font-medium">{sourceLabel(p.source)}</div>
              <ExternalLink href={p.url} className="block truncate text-meta" title={p.url}>
                {shortUrl(p.url)}
              </ExternalLink>
            </TableCell>
            <TableCell className="text-meta">
              <StatusBadge value={p.status} />
              <div className="mt-1 text-muted-foreground">Thấy {ago(p.last_seen_at)}</div>
            </TableCell>
            <TableCell className="text-meta whitespace-normal">
              <div className="flex flex-wrap items-center gap-1.5">
                {p.accepted === true && <StatusBadge value="ok" text="Nhận" />}
                {p.accepted === false && <StatusBadge value="failed" text="Loại" />}
                {p.accepted === null && <StatusBadge value="queued" text="Chưa chấm" />}
                {p.confidence !== null && <span className="text-muted-foreground tabular-nums">{pct(p.confidence, 0)}</span>}
                <ReasonTags reasons={p.verification_reasons} hideSource />
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Đánh dấu đúng/sai. `onDone` (trang Kiểm tay) chuyển sang việc tiếp theo. */
export function ReviewBox({
  detail,
  onDone,
  shortcuts = false,
}: {
  detail: JobDetail
  onDone?: () => void
  shortcuts?: boolean
}) {
  const client = useQueryClient()
  const { job, review } = detail
  const [note, setNote] = useState(review?.note ?? "")
  useEffect(() => setNote(review?.note ?? ""), [job.id, review?.note])

  const save = useMutation({
    mutationFn: (correct: boolean) => postReview(job.id, { correct, note }),
    onSuccess: (_, correct) => {
      toast.success(`Đã ghi “${job.title}”: ${correct ? "Đúng" : "Sai"}`)
      client.invalidateQueries({ queryKey: getGetJobQueryKey(job.id) })
      client.invalidateQueries({ queryKey: getGetReviewQueryKey() })
      client.invalidateQueries({ queryKey: getGetJobsQueryKey() })
      onDone?.()
    },
  })
  const remove = useDeleteReview({
    mutation: {
      onSuccess: () => {
        toast.success("Đã xoá đánh giá")
        client.invalidateQueries({ queryKey: getGetJobQueryKey(job.id) })
        client.invalidateQueries({ queryKey: getGetReviewQueryKey() })
      },
    },
  })

  useEffect(() => {
    if (!shortcuts) return
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase()
      if (["input", "textarea", "select"].includes(tag) || e.ctrlKey || e.metaKey || save.isPending) return
      if (e.key === "d" || e.key === "D") save.mutate(true)
      if (e.key === "s" || e.key === "S") save.mutate(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [shortcuts, save])

  return (
    <div className="space-y-3">
      {review ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          Đã đánh dấu
          <StatusBadge value={review.correct ? "correct" : "wrong"} />
          <span className="text-meta text-muted-foreground">
            {review.reviewer ? `${review.reviewer} · ` : ""}
            {ago(review.reviewed_at)}
          </span>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Tin thật, còn mở, đúng doanh nghiệp và khớp trang gốc?</p>
      )}
      <Textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Ghi chú khi sai: tin cũ, nhầm doanh nghiệp, tiêu đề sai…"
        className="min-h-16 bg-background"
      />
      <div className="grid grid-cols-2 gap-2">
        <Button
          className="bg-success text-white hover:bg-success/90 dark:text-background"
          onClick={() => save.mutate(true)}
          disabled={save.isPending}
        >
          <Check /> Đúng
          {shortcuts && <Kbd>D</Kbd>}
        </Button>
        <Button variant="destructive" onClick={() => save.mutate(false)} disabled={save.isPending}>
          <X /> Sai
          {shortcuts && <Kbd>S</Kbd>}
        </Button>
      </div>
      {review && !onDone && (
        <Button variant="ghost" size="sm" className="w-full text-muted-foreground" onClick={() => remove.mutate({ jobId: job.id })}>
          Xoá đánh giá
        </Button>
      )}
    </div>
  )
}

export function ReviewSection({ detail }: { detail: JobDetail }) {
  return (
    <Section title="Kiểm tay" description="Dùng để đo precision khi chưa có golden set">
      <ReviewBox detail={detail} />
    </Section>
  )
}
