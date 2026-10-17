import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Check, X } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { ExternalLink, ReasonTags, Section, StatusBadge } from "@/components/common"
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
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="pl-4">Nguồn</TableHead>
          <TableHead>Đường dẫn</TableHead>
          <TableHead>Trạng thái</TableHead>
          <TableHead>Xác minh</TableHead>
          <TableHead className="pr-4">Thấy lần cuối</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {detail.postings.map((p) => (
          <TableRow key={p.id}>
            <TableCell className="pl-4 text-xs font-medium">{sourceLabel(p.source)}</TableCell>
            <TableCell className="max-w-80 truncate text-xs">
              <ExternalLink href={p.url}>{p.url}</ExternalLink>
            </TableCell>
            <TableCell>
              <StatusBadge value={p.status} />
            </TableCell>
            <TableCell className="text-xs whitespace-normal">
              <div className="flex flex-wrap items-center gap-1">
                {p.accepted === true && <StatusBadge value="ok" text="Nhận" />}
                {p.accepted === false && <StatusBadge value="failed" text="Loại" />}
                {p.confidence !== null && <span className="text-muted-foreground tabular-nums">{pct(p.confidence, 0)}</span>}
                <ReasonTags reasons={p.verification_reasons} />
              </div>
            </TableCell>
            <TableCell className="pr-4 text-xs text-muted-foreground">{cap(ago(p.last_seen_at))}</TableCell>
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
          <span className="text-xs text-muted-foreground">
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
          {shortcuts && <kbd className="rounded border border-current/40 px-1 font-mono text-[10px] opacity-80">D</kbd>}
        </Button>
        <Button variant="destructive" onClick={() => save.mutate(false)} disabled={save.isPending}>
          <X /> Sai
          {shortcuts && <kbd className="rounded border border-current/40 px-1 font-mono text-[10px] opacity-80">S</kbd>}
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
