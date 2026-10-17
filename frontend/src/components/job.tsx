import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Check, X } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { ExternalLink, Kbd, ReasonTags, Section, StatusBadge } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { ago, cap, pct, sourceLabel } from "@/lib/format"
import type { JobDetail } from "@/lib/types"

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

export function PostingsTable({ detail }: { detail: JobDetail }) {
  // Ba cột (nguồn + đường dẫn, trạng thái + lần thấy cuối, xác minh) để vừa khung ~550px.
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
            <TableCell className="max-w-64">
              <div className="text-sm font-medium">{sourceLabel(p.source)}</div>
              <ExternalLink href={p.url} className="block truncate text-meta" title={p.url}>
                {p.url.replace(/^https?:\/\/(www\.)?/, "")}
              </ExternalLink>
            </TableCell>
            <TableCell className="text-meta">
              <StatusBadge value={p.status} />
              <div className="mt-1 text-muted-foreground">Thấy {ago(p.last_seen_at)}</div>
            </TableCell>
            <TableCell className="text-meta whitespace-normal">
              <div className="flex flex-wrap items-center gap-1">
                {p.accepted === true && <StatusBadge value="ok" text="Nhận" />}
                {p.accepted === false && <StatusBadge value="failed" text="Loại" />}
                {p.confidence !== null && <span className="text-muted-foreground tabular-nums">{pct(p.confidence, 0)}</span>}
                <ReasonTags reasons={p.verification_reasons} />
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
    mutationFn: (correct: boolean) => api.post(`/jobs/${job.id}/review`, { correct, note }),
    onSuccess: (_, correct) => {
      toast.success(`Đã ghi việc #${job.id}: ${correct ? "Đúng" : "Sai"}`)
      client.invalidateQueries({ queryKey: ["job", String(job.id)] })
      client.invalidateQueries({ queryKey: ["review"] })
      client.invalidateQueries({ queryKey: ["jobs"] })
      onDone?.()
    },
    onError: (e) => toast.error(cap(String(e))),
  })
  const remove = useMutation({
    mutationFn: () => api.delete(`/jobs/${job.id}/review`),
    onSuccess: () => {
      toast.success("Đã xoá đánh giá")
      client.invalidateQueries({ queryKey: ["job", String(job.id)] })
      client.invalidateQueries({ queryKey: ["review"] })
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
      <div className="flex flex-wrap gap-2">
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
        {review && !onDone && (
          <Button variant="ghost" size="sm" className="self-center" onClick={() => remove.mutate()}>
            Xoá đánh giá
          </Button>
        )}
      </div>
    </div>
  )
}

export function ReviewSection({ detail }: { detail: JobDetail }) {
  return (
    <Section title="Kiểm tay">
      <ReviewBox detail={detail} />
    </Section>
  )
}
