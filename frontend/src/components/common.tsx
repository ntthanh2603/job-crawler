// Thành phần dùng chung giữa các trang.
import { useQuery, type UseQueryOptions } from "@tanstack/react-query"
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  FileUp,
  Inbox,
  Info,
  RotateCw,
  Search,
  X,
} from "lucide-react"
import { useEffect, useState, type MouseEvent, type ReactNode } from "react"
import { Link, useNavigate, useSearchParams } from "react-router"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { TableHead } from "@/components/ui/table"
import { TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError, errorMessage } from "@/lib/api"
import { cap, label, num, reason, sourceProblem } from "@/lib/format"
import type { Job, ListParams, Page, SortOrder, SortState } from "@/lib/types"
import { cn } from "@/lib/utils"

// --- trạng thái -----------------------------------------------------------------------------------

export type Tone = "good" | "warn" | "bad" | "info" | "neutral" | "muted"

const TONE: Record<string, Tone> = {
  ok: "good",
  active: "good",
  verified: "good",
  complete: "good",
  correct: "good",
  partial: "warn",
  timeout: "warn",
  robots_disallowed: "warn",
  ambiguous: "neutral",
  running: "info",
  queued: "muted",
  deferred: "muted",
  not_found: "muted",
  none: "muted",
  expired: "muted",
  removed: "muted",
  failed: "bad",
  blocked: "bad",
  error: "bad",
  wrong: "bad",
  aborted: "bad",
}

/** Nền nhạt + chữ đậm cùng màu (đủ tương phản), chấm màu đậm. */
const BADGE_CLASS: Record<Tone, string> = {
  good: "bg-success/12 text-success-ink",
  warn: "bg-warning/16 text-warning-ink",
  bad: "bg-destructive/10 text-danger-ink",
  info: "bg-info/12 text-info-ink",
  neutral: "bg-neutral-tone/12 text-neutral-ink",
  muted: "bg-muted text-muted-foreground ring-1 ring-border/70 ring-inset",
}

export const DOT_CLASS: Record<Tone, string> = {
  good: "bg-success",
  warn: "bg-warning",
  bad: "bg-destructive",
  info: "bg-info",
  neutral: "bg-neutral-tone",
  muted: "bg-muted-foreground/50",
}

export function toneOf(value: string | null | undefined): Tone {
  return TONE[value ?? ""] ?? "muted"
}

/** Nhãn trạng thái dạng viên thuốc: chấm màu + chữ (viết hoa đầu). `value` là mã gốc. */
export function StatusBadge({
  value,
  text,
  tone,
  pulse,
  className,
}: {
  value: string | null | undefined
  text?: ReactNode
  tone?: Tone
  /** Chấm nhấp nháy (đang chạy). */
  pulse?: boolean
  className?: string
}) {
  const t = tone ?? toneOf(value)
  return (
    <span
      className={cn(
        "inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full px-2 text-meta font-medium whitespace-nowrap",
        BADGE_CLASS[t],
        className,
      )}
    >
      <span className="relative flex size-1.5 shrink-0" aria-hidden>
        {pulse && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", DOT_CLASS[t])} />}
        <span className={cn("relative size-1.5 rounded-full", DOT_CLASS[t])} />
      </span>
      {typeof text === "string" ? cap(text) : (text ?? label(value))}
    </span>
  )
}

/** Chấm màu + chữ, không nền: dùng trong chú giải, danh sách đếm dày. */
export function Dot({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <span className={cn("size-2 shrink-0 rounded-full", DOT_CLASS[tone])} aria-hidden />
      {children}
    </span>
  )
}

export function Tag({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 items-center rounded-md bg-secondary px-1.5 text-caption font-medium whitespace-nowrap text-secondary-foreground",
        className,
      )}
    >
      {typeof children === "string" ? cap(children) : children}
    </span>
  )
}

export function JobTags({ job }: { job: Pick<Job, "level" | "employment_type" | "work_mode"> }) {
  const values = [job.level, job.employment_type, job.work_mode].filter((v) => v && v !== "unknown")
  if (!values.length) return null
  return (
    <span className="inline-flex flex-wrap gap-1">
      {values.map((v) => (
        <Tag key={v}>{label(v)}</Tag>
      ))}
    </span>
  )
}

/** Ô có giải thích khi rê chuột (hoặc focus bằng bàn phím). */
export function Hint({ content, children, className }: { content: ReactNode; children: ReactNode; className?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className={cn("cursor-help outline-none focus-visible:ring-2 focus-visible:ring-ring/50", className)}>
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-80 text-meta">{content}</TooltipContent>
    </Tooltip>
  )
}

/** Biểu tượng (i) kèm giải thích. */
export function InfoTip({ children }: { children: ReactNode }) {
  return (
    <Hint content={children} className="inline-grid size-4 place-items-center rounded-full text-muted-foreground hover:text-foreground">
      <Info className="size-3.5" aria-label="Giải thích" />
    </Hint>
  )
}

const REASON_CLASS: Record<string, string> = {
  bad: "bg-destructive/10 text-danger-ink",
  warn: "bg-warning/16 text-warning-ink",
  good: "bg-success/12 text-success-ink",
  info: "bg-secondary text-secondary-foreground",
}

/** Lý do xác minh tin dạng nhãn tiếng Việt; rê chuột xem chi tiết và mã gốc. */
export function ReasonTags({ reasons, hideSource = false }: { reasons: string[]; hideSource?: boolean }) {
  const items = reasons.map(reason).filter((r) => !(hideSource && r.code.startsWith("SOURCE_")))
  if (!items.length) return null
  return (
    <span className="inline-flex flex-wrap gap-1">
      {items.map((r, i) => (
        <Hint
          key={`${r.code}-${i}`}
          content={
            <>
              {r.detail && <span className="block break-words">{r.detail}</span>}
              <span className="font-mono opacity-70">{r.code}</span>
            </>
          }
        >
          <span
            className={cn(
              "inline-flex h-5 items-center rounded-md px-1.5 text-caption font-medium whitespace-nowrap",
              REASON_CLASS[r.tone],
            )}
          >
            {r.label}
          </span>
        </Hint>
      ))}
    </span>
  )
}

/** Lý do loại tin: nhãn + chi tiết (chỉ các lý do loại / hoãn; bỏ lý do thông tin như nguồn). */
export function ReasonList({ reasons }: { reasons: string[] }) {
  const all = reasons.map(reason)
  const bad = all.filter((r) => r.tone === "bad" || r.tone === "warn")
  const items = bad.length ? bad : all.filter((r) => r.tone !== "info")
  if (!items.length) return <span className="text-muted-foreground">–</span>
  return (
    <ul className="space-y-1.5">
      {items.map((r, i) => (
        <li key={`${r.code}-${i}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span
            title={r.code}
            className={cn(
              "inline-flex h-5 shrink-0 items-center rounded-md px-1.5 text-caption font-medium whitespace-nowrap",
              REASON_CLASS[r.tone],
            )}
          >
            {r.label}
          </span>
          {r.detail && <span className="min-w-0 text-meta break-words text-muted-foreground [overflow-wrap:anywhere]">{r.detail}</span>}
        </li>
      ))}
    </ul>
  )
}

/** Nguồn chưa xong của một lượt chạy: nhãn gọn, rê chuột xem giải thích. */
export function IncompleteSources({ sources, className }: { sources: string[]; className?: string }) {
  if (!sources.length) return null
  return (
    <span className={cn("inline-flex flex-wrap gap-1", className)}>
      {sources.map((s) => {
        const p = sourceProblem(s)
        return (
          <Hint key={s} content={p.detail}>
            <span className="inline-flex h-5 max-w-56 items-center truncate rounded-md bg-warning/14 px-1.5 text-caption font-medium text-warning-ink">
              {p.label}
            </span>
          </Hint>
        )
      })}
    </span>
  )
}

/** Danh sách nguồn chưa xong kèm giải thích (trang lượt chạy, tổng quan). */
export function IncompleteList({ items }: { items: { source: string; count?: number }[] }) {
  return (
    <ul className="divide-y">
      {items.map(({ source, count }) => {
        const p = sourceProblem(source)
        return (
          <li key={source} className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
            <span className="mt-1.5 size-2 shrink-0 rounded-full bg-warning" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium break-words">{p.label}</div>
              <div className="text-meta text-muted-foreground">{p.detail}</div>
            </div>
            {count !== undefined && (
              <span className="shrink-0 text-sm font-semibold tabular-nums" title="Số lượt chạy">
                {num(count)}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

// --- bố cục trang ----------------------------------------------------------------------------------

export function PageHeader({
  title,
  description,
  actions,
  crumbs,
  children,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  crumbs?: { to: string; label: string }[]
  /** Dòng phụ dưới mô tả (nhãn trạng thái, link…). */
  children?: ReactNode
}) {
  useEffect(() => {
    if (typeof title === "string") document.title = `${cap(title)} · jobcrawler`
  }, [title])
  return (
    <header className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between lg:gap-8">
      <div className="min-w-0 space-y-1.5">
        {crumbs && (
          <nav aria-label="Đường dẫn" className="flex flex-wrap items-center gap-1 text-meta text-muted-foreground">
            {crumbs.map((c) => (
              <span key={c.to} className="inline-flex min-w-0 items-center gap-1">
                <Link to={c.to} className="max-w-64 truncate transition-colors hover:text-foreground">
                  {cap(c.label)}
                </Link>
                <ChevronRight className="size-3.5 shrink-0 opacity-60" />
              </span>
            ))}
          </nav>
        )}
        <h1 className="text-page font-semibold break-words text-foreground">
          {typeof title === "string" ? cap(title) : title}
        </h1>
        {description && (
          <div className="max-w-3xl text-sm text-muted-foreground">
            {typeof description === "string" ? cap(description) : description}
          </div>
        )}
        {children && <div className="pt-1">{children}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

/** Lưới các thẻ số liệu; số cột do `className`. */
export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid gap-3 sm:gap-4", className)}>{children}</div>
}

/** Một thẻ số liệu. `status` (đạt / chưa đạt…) hiện góc phải, không có thì hiện biểu tượng. */
export function StatCard({
  label: title,
  value,
  hint,
  status,
  icon,
  className,
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  status?: { tone: Tone; text: string; pulse?: boolean }
  icon?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("min-w-0 rounded-xl border bg-card p-4 shadow-card sm:px-5", className)}>
      <div className="flex min-h-6 flex-wrap items-start justify-between gap-x-2 gap-y-1">
        <span className="pt-0.5 text-meta font-medium text-muted-foreground">
          {typeof title === "string" ? cap(title) : title}
        </span>
        {status ? (
          <StatusBadge value={null} tone={status.tone} text={status.text} pulse={status.pulse} />
        ) : (
          icon && (
            <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-primary/8 text-brand dark:bg-primary/15 [&_svg]:size-4 [&_svg]:stroke-[1.75]">
              {icon}
            </span>
          )
        )}
      </div>
      <div className="mt-1.5 text-stat font-semibold text-foreground tabular-nums">{value}</div>
      {hint && (
        <div className="mt-1 text-meta text-muted-foreground">{typeof hint === "string" ? cap(hint) : hint}</div>
      )}
    </div>
  )
}

export function Section({
  id,
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  id?: string
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section
      id={id}
      className={cn("min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-card", className)}
    >
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b px-5 py-3.5">
          <div className="min-w-0">
            {title && (
              <h2 className="text-title font-semibold text-foreground">{typeof title === "string" ? cap(title) : title}</h2>
            )}
            {description && (
              <p className="text-meta text-muted-foreground">
                {typeof description === "string" ? cap(description) : description}
              </p>
            )}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cn("p-5", bodyClassName)}>{children}</div>
    </section>
  )
}

// --- tab theo URL --------------------------------------------------------------------------------

/** Tab hiện tại lưu ở `?tab=` (giữ khi tải lại trang, gửi link được). */
export function useTabParam<T extends string>(tabs: readonly T[], fallback: T): [T, (tab: string) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get("tab") as T | null
  const tab = raw && tabs.includes(raw) ? raw : fallback
  const set = (next: string) =>
    setParams(
      (prev) => {
        const out = new URLSearchParams(prev)
        if (next === fallback) out.delete("tab")
        else out.set("tab", next)
        return out
      },
      { replace: true },
    )
  return [tab, set]
}

/** Thanh tab gạch chân, kèm số đếm. Đặt trong `<Tabs>`. */
export function TabBar({ items, className }: { items: { value: string; label: string; count?: number }[]; className?: string }) {
  return (
    <TabsList
      className={cn(
        "h-auto w-full justify-start gap-1 overflow-x-auto rounded-none border-b bg-transparent p-0 [scrollbar-width:none]",
        className,
      )}
    >
      {items.map((t) => (
        <TabsTrigger
          key={t.value}
          value={t.value}
          className="relative h-10 flex-none rounded-none border-0 px-3 text-muted-foreground after:rounded-full after:bg-primary hover:text-foreground group-data-[orientation=horizontal]/tabs:after:inset-x-2 group-data-[orientation=horizontal]/tabs:after:bottom-0 group-data-[orientation=horizontal]/tabs:after:h-0.5 data-[state=active]:bg-transparent data-[state=active]:text-foreground data-[state=active]:shadow-none data-[state=active]:after:opacity-100 dark:data-[state=active]:border-0 dark:data-[state=active]:bg-transparent"
        >
          {t.label}
          {t.count !== undefined && (
            <span className="rounded-full bg-muted px-1.5 text-caption font-medium text-muted-foreground tabular-nums">
              {num(t.count)}
            </span>
          )}
        </TabsTrigger>
      ))}
    </TabsList>
  )
}

// --- dòng bảng bấm được ---------------------------------------------------------------------------

/**
 * Cả dòng bảng / thẻ là một link: bấm vào chỗ trống trong dòng thì mở `to` (Ctrl/⌘ mở tab mới).
 * Link, nút, ô chọn trong dòng vẫn giữ hành vi riêng; bôi đen chữ không mở trang.
 */
export function useRowLink() {
  const navigate = useNavigate()
  return (to: string, className?: string) => ({
    className: cn("cursor-pointer", className),
    onClick: (e: MouseEvent) => {
      if ((e.target as HTMLElement).closest("a,button,input,label,[role=checkbox],[role=combobox]")) return
      if (window.getSelection()?.toString()) return
      if (e.metaKey || e.ctrlKey) window.open(to, "_blank", "noopener")
      else navigate(to)
    },
  })
}

// --- trạng thái tải / lỗi / rỗng --------------------------------------------------------------------

export function Loading({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Đang tải">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-11 w-full rounded-lg" style={{ opacity: 1 - i * 0.08 }} />
      ))}
    </div>
  )
}

/** Khung trang lúc tải: tiêu đề + dải thẻ + một khung lớn. */
export function PageLoading() {
  return (
    <div aria-busy="true" aria-label="Đang tải" className="space-y-6">
      <div className="space-y-2.5">
        <Skeleton className="h-3.5 w-28" />
        <Skeleton className="h-8 w-72 max-w-full" />
        <Skeleton className="h-4 w-96 max-w-full" />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-28 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-96 rounded-xl" />
    </div>
  )
}

/**
 * Khung lỗi. Mặc định: "Không tải được dữ liệu" + thông báo lỗi + nút "Thử lại" (khi có `onRetry`);
 * `title`, `message`, `action` thay từng phần (trang lỗi của router).
 */
export function ErrorState({
  error,
  onRetry,
  title,
  message,
  action,
}: {
  error: unknown
  onRetry?: () => void
  title?: ReactNode
  message?: ReactNode
  action?: ReactNode
}) {
  const missing = error instanceof ApiError && error.status === 404
  return (
    <div
      role="alert"
      className="flex flex-wrap items-start gap-3 rounded-xl border border-destructive/25 bg-destructive/6 p-4 text-sm"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1">
        <div className="font-medium text-danger-ink">{title ?? (missing ? "Không tìm thấy" : "Không tải được dữ liệu")}</div>
        <div className="break-words text-muted-foreground">{message ?? errorMessage(error)}</div>
      </div>
      {action ??
        (onRetry && !missing && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw /> Thử lại
          </Button>
        ))}
    </div>
  )
}

export function Empty({
  children,
  title,
  icon,
  action,
  className,
}: {
  children?: ReactNode
  title?: string
  icon?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-14 text-center", className)}>
      <div className="grid size-11 place-items-center rounded-full bg-muted text-muted-foreground [&_svg]:size-5 [&_svg]:stroke-[1.75]">
        {icon ?? <Inbox />}
      </div>
      <div className="max-w-sm space-y-1">
        {title && <div className="text-sm font-medium text-foreground">{cap(title)}</div>}
        {children && (
          <div className="text-sm text-muted-foreground">{typeof children === "string" ? cap(children) : children}</div>
        )}
      </div>
      {action}
    </div>
  )
}

// --- phân trang, chọn, xác nhận ---------------------------------------------------------------------

/** Số dòng mỗi trang cho ô chọn dưới bảng (như data-table của open-asm). */
export const PAGE_SIZES = [10, 20, 50, 100] as const

/** Các nút số trang: trang đầu, trang cuối và ±2 quanh trang hiện tại; khoảng bỏ qua là "gap". */
export function pageWindow(page: number, pageCount: number): (number | "gap")[] {
  const out: (number | "gap")[] = []
  let prev = 0
  for (let p = 1; p <= pageCount; p++) {
    if (p !== 1 && p !== pageCount && Math.abs(p - page) > 2) continue
    if (prev && p - prev > 1) out.push("gap")
    out.push(p)
    prev = p
  }
  return out
}

/**
 * Phân trang dưới bảng (như data-table của open-asm): "1–50 trong 1.234", ô số dòng mỗi trang,
 * nút trước / sau và các nút số trang. Nhận thẳng một `Page` từ API (`page`, `pageCount`, `total`,
 * `limit`, `exact`); `exact = false` nghĩa là server chỉ đếm tới `total` (10.000) — hiện "10.000+".
 */
export function Pager({
  page,
  pageCount,
  total,
  limit,
  exact = true,
  onPage,
  onLimit,
  className,
}: {
  page: number
  pageCount: number
  total: number
  limit: number
  exact?: boolean
  onPage: (page: number) => void
  /** Có thì hiện ô chọn số dòng mỗi trang. */
  onLimit?: (limit: number) => void
  className?: string
}) {
  if (!total) return null
  const first = (page - 1) * limit + 1
  const last = Math.min(total, page * limit)
  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-meta text-muted-foreground",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="tabular-nums">
          <span className="font-medium text-foreground">
            {num(first)}–{num(last)}
          </span>{" "}
          trong {num(total)}
          {!exact && "+"}
          {!exact && page >= pageCount && " · lọc hẹp hơn để xem tiếp"}
        </span>
        {onLimit && (
          <span className="inline-flex items-center gap-2">
            Số dòng
            <Select value={String(limit)} onValueChange={(v) => onLimit(Number(v))}>
              <SelectTrigger size="sm" className="w-[4.75rem] bg-card tabular-nums" aria-label="Số dòng mỗi trang">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                {PAGE_SIZES.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </span>
        )}
      </div>
      {pageCount > 1 && (
        <nav aria-label="Phân trang" className="flex items-center gap-1">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Trang trước">
            <ChevronLeft /> <span className="max-sm:sr-only">Trước</span>
          </Button>
          <span className="px-1.5 tabular-nums sm:hidden">
            <span className="font-medium text-foreground">{page}</span> / {num(pageCount)}
          </span>
          {pageWindow(page, pageCount).map((p, i) =>
            p === "gap" ? (
              <span key={`gap-${i}`} className="px-1 max-sm:hidden" aria-hidden>
                …
              </span>
            ) : (
              <Button
                key={p}
                variant={p === page ? "default" : "ghost"}
                size="sm"
                className="min-w-8 px-2 tabular-nums max-sm:hidden"
                aria-label={`Trang ${p}`}
                aria-current={p === page ? "page" : undefined}
                onClick={() => onPage(p)}
              >
                {num(p)}
              </Button>
            ),
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={page >= pageCount}
            onClick={() => onPage(page + 1)}
            aria-label="Trang sau"
          >
            <span className="max-sm:sr-only">Sau</span> <ChevronRight />
          </Button>
        </nav>
      )}
    </div>
  )
}

/** Bấm tiêu đề cột: cột khác → tăng dần; cùng cột → đảo chiều (như data-table của open-asm). */
export function nextSort<C extends string>(sort: SortState<C>, column: C): SortState<C> {
  return { sortBy: column, sortOrder: sort.sortBy === column && sort.sortOrder === "ASC" ? "DESC" : "ASC" }
}

/** Tiêu đề cột sắp xếp được (`sortBy` = `column`): mũi tên chiều hiện tại, bấm để đổi. */
export function SortHead<C extends string>({
  column,
  sort,
  onSort,
  children,
  className,
}: {
  column: C
  sort: SortState<C>
  onSort: (column: C) => void
  children: ReactNode
  className?: string
}) {
  const active = sort.sortBy === column
  const Icon = !active ? ChevronsUpDown : sort.sortOrder === "ASC" ? ArrowUp : ArrowDown
  return (
    <TableHead
      className={className}
      aria-sort={active ? (sort.sortOrder === "ASC" ? "ascending" : "descending") : undefined}
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        className={cn(
          "-mx-1 inline-flex items-center gap-1 rounded px-1 transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
          active && "text-foreground",
        )}
      >
        {children}
        <Icon className={cn("size-3.5 shrink-0", !active && "opacity-40")} aria-hidden />
      </button>
    </TableHead>
  )
}

/** Ô "Sắp xếp" chọn sẵn cột + chiều (dùng cả khi bảng thành thẻ trên màn hình hẹp, không có tiêu
 * đề cột để bấm). `options`: [cột, chiều, nhãn]. */
export function SortSelect<C extends string>({
  sort,
  onChange,
  options,
}: {
  sort: SortState<C>
  onChange: (sort: SortState<C>) => void
  options: readonly (readonly [C, SortOrder, string])[]
}) {
  return (
    <FilterSelect
      label="Sắp xếp"
      value={`${sort.sortBy}:${sort.sortOrder}`}
      onChange={(v) => {
        const [sortBy, sortOrder] = v.split(":")
        onChange({ sortBy: sortBy as C, sortOrder: sortOrder === "ASC" ? "ASC" : "DESC" })
      }}
      options={options.map(([c, o, text]) => [`${c}:${o}`, text] as const)}
      all={null}
    />
  )
}

/**
 * Danh sách con lấy theo trang từ API (như `useServerDataTable` của open-asm, giữ trạng thái trong
 * component): `options(params)` trả query options do orval sinh
 * (`(p) => getGetRunLogsQueryOptions(runId, p)`) với `page`, `limit`, `sortBy`, `sortOrder`,
 * `search`; tự lo trạng thái tải / lỗi / rỗng, ô tìm (khi có `searchPlaceholder`) và phân trang.
 * `children(items, table)` vẽ các dòng; `table.sort` / `table.onSort` cho `SortHead`.
 */
export function PagedList<T, C extends string>({
  options,
  empty,
  defaultSort,
  defaultLimit = 20,
  searchPlaceholder,
  children,
}: {
  // Kiểu lỗi / khoá khác nhau theo endpoint.
  options: (params: ListParams<C>) => UseQueryOptions<Page<T>, any, Page<T>, any>
  empty: ReactNode
  defaultSort: SortState<C>
  defaultLimit?: number
  searchPlaceholder?: string
  children: (items: T[], table: { sort: SortState<C>; onSort: (column: C) => void }) => ReactNode
}) {
  const [page, setPage] = useState(1)
  const [limit, setLimit] = useState(defaultLimit)
  const [sort, setSort] = useState(defaultSort)
  const [search, setSearch] = useState("")
  const [draft, setDraft] = useState("")
  const query = useQuery({ ...options({ ...sort, page, limit, search }), placeholderData: (prev) => prev })
  const onSort = (column: C) => {
    setSort((s) => nextSort(s, column))
    setPage(1)
  }
  const bar = searchPlaceholder && (
    <FilterBar
      search={
        <SearchBox
          value={draft}
          onChange={setDraft}
          placeholder={searchPlaceholder}
          onSubmit={(v) => {
            setSearch((v ?? draft).trim())
            setPage(1)
          }}
        />
      }
    />
  )
  if (query.isError)
    return (
      <>
        {bar}
        <div className="p-5">
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        </div>
      </>
    )
  if (!query.data)
    return (
      <>
        {bar}
        <div className="p-5">
          <Loading rows={5} />
        </div>
      </>
    )
  const d = query.data
  return (
    <>
      {bar}
      <div className={cn("transition-opacity", query.isFetching && query.isPlaceholderData && "opacity-60")}>
        {d.data.length ? children(d.data, { sort, onSort }) : <Empty>{search ? "Không có dòng nào khớp." : empty}</Empty>}
        <Pager
          {...d}
          onPage={setPage}
          onLimit={(n) => {
            setLimit(n)
            setPage(1)
          }}
        />
      </div>
    </>
  )
}

const ALL = "__all__"

/**
 * Thanh lọc ở đầu bảng (trong cùng khung với bảng): ô tìm (`search`), các bộ lọc, và phần bên
 * phải (`end`, vd. sắp xếp). Trên điện thoại ô tìm chiếm một dòng, các bộ lọc thành hàng vuốt ngang.
 */
export function FilterBar({
  search,
  children,
  end,
  className,
}: {
  search?: ReactNode
  children?: ReactNode
  end?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("space-y-2.5 border-b bg-muted/30 px-4 py-3 sm:px-5", className)}>
      {(search || end) && (
        <div className="flex flex-wrap items-center gap-2">
          {search && <div className="min-w-0 flex-1 basis-60">{search}</div>}
          {end && <div className="flex shrink-0 flex-wrap items-center gap-2">{end}</div>}
        </div>
      )}
      {children && (
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 [scrollbar-width:none] max-sm:[&>*]:shrink-0 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0">
          {children}
        </div>
      )}
    </div>
  )
}

/** Ô tìm kiếm: Enter để tìm; nút xoá khi có chữ. */
export function SearchBox({
  value,
  onChange,
  onSubmit,
  placeholder,
  className,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: (value?: string) => void
  placeholder: string
  className?: string
}) {
  return (
    <form
      role="search"
      className={cn("relative", className)}
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
    >
      <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        className="h-9 w-full bg-card pr-9 pl-9 text-sm"
      />
      {value && (
        <button
          type="button"
          aria-label="Xoá ô tìm"
          className="absolute top-1/2 right-1.5 grid size-6 -translate-y-1/2 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={() => {
            onChange("")
            onSubmit("")
          }}
        >
          <X className="size-3.5" />
        </button>
      )}
    </form>
  )
}

/**
 * Ô chọn bộ lọc, tên bộ lọc nằm trong ô ("Cấp bậc  Tất cả"). `options` là mã (hiện bằng `labels`,
 * mặc định nhãn tiếng Việt) hoặc cặp [mã, chữ]; `all = null` khi không có lựa chọn "tất cả".
 */
export function FilterSelect({
  value,
  onChange,
  options,
  label: name,
  labels,
  all = "Tất cả",
  className,
}: {
  value: string
  onChange: (value: string) => void
  options: readonly (string | readonly [string, string])[]
  label?: string
  labels?: (value: string) => string
  all?: string | null
  className?: string
}) {
  const text = labels ?? label
  const items = options.map((o) => (typeof o === "string" ? ([o, text(o)] as const) : o))
  const active = Boolean(value) && all !== null
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectTrigger
        className={cn(
          "h-9 w-auto max-w-72 gap-1.5 bg-card text-sm",
          active && "border-primary/50 bg-primary/6 text-foreground dark:bg-primary/12",
          className,
        )}
      >
        {name && <span className="shrink-0 text-muted-foreground">{name}</span>}
        <SelectValue placeholder={all ?? undefined} />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="max-h-80">
        {all !== null && <SelectItem value={ALL}>{all}</SelectItem>}
        {items.map(([v, t]) => (
          <SelectItem key={v} value={v}>
            {t}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** Ô số nhỏ kèm chữ trước/sau ("Lương từ [15] triệu", "Chưa chạy trong [24] giờ"). */
export function NumberChip({
  prefix,
  suffix,
  value,
  onChange,
  onCommit,
  min = 0,
  placeholder,
  className,
}: {
  prefix: string
  suffix?: string
  value: string
  onChange: (value: string) => void
  onCommit?: () => void
  min?: number
  placeholder?: string
  className?: string
}) {
  return (
    <label
      className={cn(
        "flex h-9 items-center gap-1.5 rounded-md border border-input bg-card px-3 text-sm shadow-xs transition-[box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50",
        value && "border-primary/50 bg-primary/6 dark:bg-primary/12",
        className,
      )}
    >
      <span className="text-muted-foreground">{prefix}</span>
      <input
        type="number"
        min={min}
        inputMode="numeric"
        value={value}
        placeholder={placeholder}
        aria-label={`${prefix} ${suffix ?? ""}`.trim()}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => onCommit?.()}
        onKeyDown={(e) => e.key === "Enter" && onCommit?.()}
        className="w-10 [appearance:textfield] bg-transparent text-center font-medium tabular-nums outline-none placeholder:font-normal placeholder:text-muted-foreground/70 [&::-webkit-inner-spin-button]:appearance-none"
      />
      {suffix && <span className="text-muted-foreground">{suffix}</span>}
    </label>
  )
}

export function ConfirmButton({
  title,
  description,
  confirm = "Đồng ý",
  onConfirm,
  children,
  variant = "outline",
  size = "sm",
  disabled,
}: {
  title: string
  description?: ReactNode
  confirm?: string
  onConfirm: () => void
  children: ReactNode
  variant?: "outline" | "default" | "destructive" | "secondary" | "ghost"
  size?: "sm" | "default" | "xs"
  disabled?: boolean
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant={variant} size={size} disabled={disabled}>
          {children}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{cap(title)}</AlertDialogTitle>
          {description && <AlertDialogDescription>{description}</AlertDialogDescription>}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Huỷ</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>{confirm}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Danh sách nhãn – giá trị. Giá trị chuỗi được viết hoa đầu, nên URL / mã truyền dạng phần tử. */
export function Meta({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(6.5rem,max-content)_1fr] gap-x-5 gap-y-3 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted-foreground">{cap(k)}</dt>
          <dd className="min-w-0 break-words">{typeof v === "string" ? cap(v) : v}</dd>
        </div>
      ))}
    </dl>
  )
}

/** Phím tắt bàn phím (nằm trong nút hoặc chữ). */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-grid h-5 min-w-5 place-items-center rounded border border-current/30 px-1 font-mono text-caption leading-none opacity-75",
        className,
      )}
    >
      {children}
    </kbd>
  )
}

export function ExternalLink({
  href,
  children,
  className,
  title,
}: {
  href: string
  children: ReactNode
  className?: string
  title?: string
}) {
  return (
    <a
      href={href}
      title={title}
      target="_blank"
      rel="noopener noreferrer"
      className={cn("text-brand underline-offset-2 transition-colors hover:underline", className)}
    >
      {children}
    </a>
  )
}

/** Link nội bộ màu chữ thường, gạch chân khi rê chuột. */
export function TextLink({ to, children, className }: { to: string; children: ReactNode; className?: string }) {
  return (
    <Link to={to} className={cn("underline-offset-2 transition-colors hover:text-brand hover:underline", className)}>
      {children}
    </Link>
  )
}

/** Ô chọn file (thay nút mặc định tiếng Anh của trình duyệt). */
export function FilePicker({
  file,
  onChange,
  title,
  hint,
  accept = ".csv,text/csv",
  className,
}: {
  file: File | null
  onChange: (file: File | null) => void
  title: string
  hint?: string
  accept?: string
  className?: string
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer flex-col items-center gap-1.5 rounded-xl border border-dashed border-input bg-muted/40 px-4 py-6 text-center transition-colors hover:border-primary/50 hover:bg-primary/4 focus-within:ring-[3px] focus-within:ring-ring/50",
        file && "border-solid border-primary/50 bg-primary/6",
        className,
      )}
    >
      <span className="grid size-9 place-items-center rounded-full bg-card text-brand shadow-card">
        <FileUp className="size-4.5 stroke-[1.75]" />
      </span>
      <span className="max-w-full truncate text-sm font-medium">{file ? file.name : title}</span>
      <span className="text-meta text-muted-foreground">{file ? "Bấm để chọn file khác" : hint}</span>
      <input type="file" accept={accept} className="sr-only" onChange={(e) => onChange(e.target.files?.[0] ?? null)} />
    </label>
  )
}
