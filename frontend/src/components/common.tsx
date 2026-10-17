// Thành phần dùng chung giữa các trang.
import { AlertCircle, ChevronLeft, ChevronRight, Inbox } from "lucide-react"
import type { ReactNode } from "react"
import { Link } from "react-router"

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
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { label, num } from "@/lib/format"
import type { Job } from "@/lib/types"
import { cn } from "@/lib/utils"

// --- trạng thái -----------------------------------------------------------------------------------

const TONE: Record<string, string> = {
  ok: "good",
  active: "good",
  verified: "good",
  partial: "warn",
  ambiguous: "warn",
  timeout: "warn",
  running: "info",
  failed: "bad",
  blocked: "bad",
  error: "bad",
}

const TONE_CLASS: Record<string, string> = {
  good: "bg-success/15 text-success",
  warn: "bg-warning/15 text-warning",
  bad: "bg-destructive/15 text-destructive",
  info: "bg-info/15 text-info",
  muted: "bg-muted text-muted-foreground",
}

export function StatusBadge({ value, text, className }: { value: string | null | undefined; text?: ReactNode; className?: string }) {
  const tone = TONE[value ?? ""] ?? "muted"
  return (
    <Badge variant="secondary" className={cn("font-medium", TONE_CLASS[tone], className)}>
      {text ?? label(value)}
    </Badge>
  )
}

export function Tag({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <Badge variant="outline" title={title} className={cn("font-normal text-muted-foreground", className)}>
      {children}
    </Badge>
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

export function ReasonTags({ reasons }: { reasons: string[] }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {reasons.map((r) => (
        <Tag key={r} title={r}>
          {r.split(":")[0]}
        </Tag>
      ))}
    </span>
  )
}

// --- bố cục trang ----------------------------------------------------------------------------------

export function PageHeader({
  title,
  description,
  actions,
  crumbs,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  crumbs?: { to: string; label: string }[]
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        {crumbs && (
          <nav className="mb-1 flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
            {crumbs.map((c) => (
              <span key={c.to} className="inline-flex items-center gap-1">
                <Link to={c.to} className="hover:text-foreground">
                  {c.label}
                </Link>
                <ChevronRight className="size-3" />
              </span>
            ))}
          </nav>
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-balance">{title}</h1>
        {description && <div className="mt-1 text-sm text-muted-foreground">{description}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function StatCard({
  label: title,
  value,
  hint,
  tone,
  icon,
}: {
  label: ReactNode
  value: ReactNode
  hint?: ReactNode
  tone?: "good" | "bad" | "warn"
  icon?: ReactNode
}) {
  return (
    <Card className="gap-0 py-0">
      <CardContent className="relative overflow-hidden p-4">
        {tone && (
          <span
            className={cn(
              "absolute inset-y-0 left-0 w-1",
              tone === "good" && "bg-success",
              tone === "bad" && "bg-destructive",
              tone === "warn" && "bg-warning",
            )}
          />
        )}
        <div className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground">
          <span>{title}</span>
          {icon && <span className="text-muted-foreground/70 [&_svg]:size-4">{icon}</span>}
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">{value}</div>
        {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
      </CardContent>
    </Card>
  )
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <Card className={cn("gap-0 py-0", className)}>
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold">{title}</h2>}
            {description && <p className="text-xs text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cn("p-4", bodyClassName)}>{children}</div>
    </Card>
  )
}

// --- trạng thái tải / lỗi / rỗng --------------------------------------------------------------------

export function Loading({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
      <AlertCircle className="mt-0.5 size-4 shrink-0" />
      <div>
        <div className="font-medium">Không tải được dữ liệu</div>
        <div className="opacity-90">{message}</div>
      </div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center text-sm text-muted-foreground">
      <Inbox className="size-6 opacity-60" />
      {children}
    </div>
  )
}

// --- phân trang, chọn, xác nhận ---------------------------------------------------------------------

export function Pager({
  page,
  pages,
  total,
  size = 50,
  onPage,
}: {
  page: number
  pages: number
  total: number
  size?: number
  onPage: (page: number) => void
}) {
  const first = total ? (page - 1) * size + 1 : 0
  const last = Math.min(total, page * size)
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3 text-sm text-muted-foreground">
      <span className="tabular-nums">
        {num(first)}–{num(last)} / {num(total)}
      </span>
      {pages > 1 && (
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
            <ChevronLeft /> Trước
          </Button>
          <span className="px-2 tabular-nums">
            {page} / {pages}
          </span>
          <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
            Sau <ChevronRight />
          </Button>
        </div>
      )}
    </div>
  )
}

const ALL = "__all__"

export function FilterSelect({
  value,
  onChange,
  options,
  placeholder = "Tất cả",
  className,
}: {
  value: string
  onChange: (value: string) => void
  options: string[]
  placeholder?: string
  className?: string
}) {
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectTrigger size="sm" className={cn("min-w-36", className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{placeholder}</SelectItem>
        {options.map((o) => (
          <SelectItem key={o} value={o}>
            {label(o)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export function Field({ label: text, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1.5 text-xs font-medium text-muted-foreground", className)}>
      {text}
      {children}
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
          <AlertDialogTitle>{title}</AlertDialogTitle>
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

export function Meta({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

export function ExternalLink({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={cn("text-primary hover:underline", className)}>
      {children}
    </a>
  )
}
