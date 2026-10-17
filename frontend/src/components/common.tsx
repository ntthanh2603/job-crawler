// Thành phần dùng chung giữa các trang.
import { useQuery } from "@tanstack/react-query"
import { AlertCircle, ChevronLeft, ChevronRight, FileUp, Inbox, Search } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"
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
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { cap, label, num } from "@/lib/format"
import type { Job, Page } from "@/lib/types"
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
  ambiguous: "neutral",
  running: "info",
  queued: "muted",
  failed: "bad",
  blocked: "bad",
  error: "bad",
  wrong: "bad",
  aborted: "bad",
}

// Chữ của nhãn: màu chữ thường, chỉ "lỗi" mới đổi màu; màu nằm ở chấm.
const TONE_CLASS: Record<Tone, string> = {
  good: "text-foreground/85",
  warn: "text-foreground/85",
  bad: "text-destructive",
  info: "text-foreground/85",
  neutral: "text-foreground/85",
  muted: "text-muted-foreground",
}

const DOT_CLASS: Record<Tone, string> = {
  good: "bg-success",
  warn: "bg-warning",
  bad: "bg-destructive",
  info: "bg-info",
  neutral: "bg-neutral-tone",
  muted: "bg-muted-foreground/60",
}

export function toneOf(value: string | null | undefined): Tone {
  return TONE[value ?? ""] ?? "muted"
}

/** Nhãn trạng thái: chấm màu + chữ (viết hoa đầu). `value` là mã gốc, hiện khi rê chuột. */
export function StatusBadge({
  value,
  text,
  tone,
  className,
}: {
  value: string | null | undefined
  text?: ReactNode
  tone?: Tone
  className?: string
}) {
  const t = tone ?? toneOf(value)
  return (
    <span
      title={value ?? undefined}
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1.5 text-meta font-medium whitespace-nowrap",
        TONE_CLASS[t],
        className,
      )}
    >
      <span className={cn("size-[7px] shrink-0 rounded-full", DOT_CLASS[t])} aria-hidden />
      {typeof text === "string" ? cap(text) : (text ?? label(value))}
    </span>
  )
}

export function Tag({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 items-center rounded-[4px] border px-1.5 text-caption whitespace-nowrap text-muted-foreground",
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

export function ReasonTags({ reasons }: { reasons: string[] }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {reasons.map((r) => (
        <Tag key={r} title={r} className="font-mono tracking-tight">
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
  useEffect(() => {
    if (typeof title === "string") document.title = `${cap(title)} · jobcrawler`
  }, [title])
  return (
    <header className="mb-8 flex flex-wrap items-end justify-between gap-x-8 gap-y-4 border-b pb-6">
      <div className="min-w-0 space-y-2">
        {crumbs && (
          <nav aria-label="Đường dẫn" className="flex flex-wrap items-center gap-1 text-meta text-muted-foreground">
            {crumbs.map((c) => (
              <span key={c.to} className="inline-flex items-center gap-1">
                <Link to={c.to} className="transition-colors hover:text-foreground">
                  {cap(c.label)}
                </Link>
                <ChevronRight className="size-3.5 opacity-60" />
              </span>
            ))}
          </nav>
        )}
        <h1 className="text-page font-semibold text-foreground">
          {typeof title === "string" ? cap(title) : title}
        </h1>
        {description && (
          <div className="max-w-2xl text-sm text-muted-foreground">
            {typeof description === "string" ? cap(description) : description}
          </div>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

/** Dải số liệu: các ô chia bằng đường kẻ mảnh, số cột do `className`. */
export function StatGrid({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("grid overflow-hidden rounded-lg border bg-card", className)}>{children}</div>
  )
}

/** Một ô số liệu trong `StatGrid`. `status` (đạt / chưa đạt) hiện cạnh tiêu đề. */
export function StatCard({
  label: title,
  value,
  hint,
  status,
  icon,
  className,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  status?: { tone: Tone; text: string }
  icon?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("-mt-px -ml-px border-t border-l px-5 py-4", className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-meta text-muted-foreground [&_svg]:size-4 [&_svg]:stroke-[1.75]">
          {icon}
          {cap(title)}
        </span>
        {status && <StatusBadge value={null} tone={status.tone} text={status.text} />}
      </div>
      <div className="mt-2 text-stat font-semibold text-foreground tabular-nums">
        {value}
      </div>
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
      className={cn(
        "overflow-hidden rounded-lg border bg-card text-card-foreground",
        className,
      )}
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

// --- trạng thái tải / lỗi / rỗng --------------------------------------------------------------------

export function Loading({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Đang tải">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-10 w-full rounded-lg" style={{ opacity: 1 - i * 0.08 }} />
      ))}
    </div>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-lg border border-destructive/25 bg-destructive/8 p-4 text-sm text-destructive"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0" />
      <div>
        <div className="font-medium">Không tải được dữ liệu</div>
        <div className="opacity-90">{cap(message)}</div>
      </div>
    </div>
  )
}

export function Empty({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      <div className="grid size-10 place-items-center rounded-lg bg-muted text-muted-foreground [&_svg]:size-5 [&_svg]:stroke-[1.75]">
        {icon ?? <Inbox />}
      </div>
      <div className="max-w-sm text-sm text-muted-foreground">
        {typeof children === "string" ? cap(children) : children}
      </div>
    </div>
  )
}

// --- phân trang, chọn, xác nhận ---------------------------------------------------------------------

/**
 * Phân trang dưới bảng. Nhận thẳng một `Page` từ API (`page`, `pages`, `total`, `size`, `exact`);
 * `exact = false` nghĩa là server chỉ đếm tới `total` (10.000) — hiện "10.000+".
 */
export function Pager({
  page,
  pages,
  total,
  size = 50,
  exact = true,
  onPage,
  className,
}: {
  page: number
  pages: number
  total: number
  size?: number
  exact?: boolean
  onPage: (page: number) => void
  className?: string
}) {
  const first = total ? (page - 1) * size + 1 : 0
  const last = Math.min(total, page * size)
  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-meta text-muted-foreground",
        className,
      )}
    >
      <span className="tabular-nums">
        Hiển thị {num(first)}–{num(last)} / {num(total)}
        {!exact && "+"}
        {!exact && page >= pages && " · lọc hẹp hơn để xem tiếp"}
      </span>
      {pages > 1 && (
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
            <ChevronLeft /> Trước
          </Button>
          <span className="px-2 tabular-nums">
            Trang {page}/{pages}
          </span>
          <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
            Sau <ChevronRight />
          </Button>
        </div>
      )}
    </div>
  )
}

/**
 * Danh sách con lấy theo trang từ API (`url` trả `Page<T>`): tự lo trạng thái tải / lỗi / rỗng và
 * phân trang; `children` vẽ các dòng của trang hiện tại.
 */
export function PagedList<T>({
  queryKey,
  url,
  empty,
  children,
}: {
  queryKey: unknown[]
  url: string
  empty: ReactNode
  children: (items: T[]) => ReactNode
}) {
  const [page, setPage] = useState(1)
  const query = useQuery({
    queryKey: [...queryKey, page],
    queryFn: () => api.get<Page<T>>(`${url}${url.includes("?") ? "&" : "?"}page=${page}`),
    placeholderData: (prev) => prev,
  })
  if (query.isError)
    return (
      <div className="p-5">
        <ErrorState error={query.error} />
      </div>
    )
  if (!query.data)
    return (
      <div className="p-5">
        <Loading rows={5} />
      </div>
    )
  const d = query.data
  return (
    <div className={cn(query.isFetching && "opacity-70 transition-opacity")}>
      {d.items.length ? children(d.items) : <Empty>{empty}</Empty>}
      {d.pages > 1 && <Pager {...d} onPage={setPage} />}
    </div>
  )
}

const ALL = "__all__"

/**
 * Thanh lọc ở đầu bảng (trong cùng khung với bảng): ô tìm (`search`) và các bộ lọc. Trên điện
 * thoại ô tìm chiếm một dòng, các bộ lọc thành một hàng vuốt ngang.
 */
export function FilterBar({ search, children, className }: { search?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cn("space-y-2 border-b px-5 py-3 sm:flex sm:flex-wrap sm:items-center sm:gap-2 sm:space-y-0", className)}>
      {search}
      <div className="-mx-5 flex gap-2 overflow-x-auto px-5 [scrollbar-width:none] max-sm:[&>*]:shrink-0 sm:contents">
        {children}
      </div>
    </div>
  )
}

/** Ô tìm kiếm: Enter để tìm. */
export function SearchBox({
  value,
  onChange,
  onSubmit,
  placeholder,
  className,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
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
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        className="h-8 w-full bg-card pl-8 text-sm"
      />
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
        size="sm"
        className={cn("w-auto max-w-64 gap-1.5 bg-card text-sm", active && "border-primary/40 bg-accent/40", className)}
      >
        {name && <span className="shrink-0 text-muted-foreground">{name}</span>}
        <SelectValue placeholder={all ?? undefined} />
      </SelectTrigger>
      <SelectContent>
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
        "flex h-8 items-center gap-1.5 rounded-md border bg-card px-2.5 text-sm shadow-xs transition-[box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50",
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

export function Field({ label: text, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1.5 text-meta font-medium text-foreground/70", className)}>
      {cap(text)}
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

export function Meta({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(7rem,max-content)_1fr] gap-x-5 gap-y-3 text-sm">
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
        "flex cursor-pointer flex-col items-center gap-1.5 rounded-lg border border-dashed bg-muted/40 px-4 py-5 text-center transition-colors hover:bg-muted/60 focus-within:ring-2 focus-within:ring-ring/50",
        file && "border-solid border-brand/40 bg-brand/5",
        className,
      )}
    >
      <FileUp className="size-5 stroke-[1.75] text-muted-foreground" />
      <span className="max-w-full truncate text-sm font-medium">{file ? file.name : title}</span>
      <span className="text-meta text-muted-foreground">{file ? "Bấm để chọn file khác" : hint}</span>
      <input type="file" accept={accept} className="sr-only" onChange={(e) => onChange(e.target.files?.[0] ?? null)} />
    </label>
  )
}
