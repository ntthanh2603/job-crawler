import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle, Clock3, Moon, RotateCcw, RotateCw, Save, SearchX, Sun, X } from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Empty, ErrorState, PageHeader, PageLoading, SearchBox } from "@/components/common"
import { SettingField } from "@/components/settings/fields"
import {
  currentRaw,
  describeWindow,
  fallbackValue,
  fold,
  formatValue,
  isDirty,
  localError,
  parseWindow,
  toPayload,
  type Draft,
  type Raw,
} from "@/components/settings/model"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { cap, dec, num } from "@/lib/format"
import type { SettingGroup, SettingItem, SettingsPage as SettingsData, SettingsSaved, SettingSource } from "@/lib/types"
import { cn } from "@/lib/utils"

const SOURCE: Record<SettingSource, { text: string; title: string; className: string }> = {
  default: {
    text: "Mặc định",
    title: "Đang dùng giá trị mặc định",
    className: "bg-muted text-muted-foreground ring-1 ring-border/70 ring-inset",
  },
  env: {
    text: "Biến môi trường",
    title: "Giá trị lấy từ cấu hình triển khai (biến môi trường / file .env)",
    className: "bg-info/12 text-info-ink",
  },
  ui: { text: "Giao diện", title: "Giá trị đặt ở trang này, đè lên cấu hình triển khai", className: "bg-primary/10 text-brand" },
}

function Pill({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-flex h-5 shrink-0 items-center gap-1 rounded-md px-1.5 text-caption font-medium whitespace-nowrap", className)}
    >
      {children}
    </span>
  )
}

/** Cảnh báo khi rời trang (đóng tab, tải lại, bấm link trong ứng dụng) lúc còn thay đổi chưa lưu. */
function useLeaveGuard(dirty: boolean) {
  const navigate = useNavigate()
  const [pending, setPending] = useState<string | null>(null)
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ""
    }
    // Router hiện dùng BrowserRouter (không có useBlocker): chặn click vào link nội bộ ở pha capture,
    // trước khi react-router xử lý.
    const click = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      const a = (e.target as HTMLElement | null)?.closest?.("a[href]") as HTMLAnchorElement | null
      if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return
      const url = new URL(a.href, window.location.href)
      if (url.origin !== window.location.origin || url.pathname.startsWith("/api/")) return
      if (url.pathname === window.location.pathname && url.search === window.location.search) return
      e.preventDefault()
      e.stopPropagation()
      setPending(url.pathname + url.search + url.hash)
    }
    window.addEventListener("beforeunload", beforeUnload)
    document.addEventListener("click", click, true)
    return () => {
      window.removeEventListener("beforeunload", beforeUnload)
      document.removeEventListener("click", click, true)
    }
  }, [dirty])
  const dialog = (
    <AlertDialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Rời trang khi chưa lưu?</AlertDialogTitle>
          <AlertDialogDescription>Các thay đổi chưa lưu trên trang Cài đặt sẽ mất.</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Ở lại</AlertDialogCancel>
          <AlertDialogAction
            onClick={() => {
              const to = pending
              setPending(null)
              if (to) setTimeout(() => navigate(to), 0)
            }}
          >
            Rời trang
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
  return dialog
}

// --- tóm tắt lịch ------------------------------------------------------------------------------------

function minutesText(seconds: number) {
  return dec(seconds / 60, seconds % 60 ? 1 : 0)
}

/** Thẻ tóm tắt: độ trễ phát hiện tin mới và hai khung giờ, tính theo giá trị đang sửa. */
function ScheduleSummary({ items, draft }: { items: Map<string, SettingItem>; draft: Draft }) {
  const value = (key: string) => {
    const item = items.get(key)
    return item ? toPayload(item, currentRaw(item, draft)) : null
  }
  const interval = Number(value("poll_interval_seconds"))
  const deadline = Number(value("poll_deadline_seconds"))
  const build = String(value("build_window") ?? "")
  const poll = String(value("poll_window") ?? "")
  const tz = String(value("crawl_timezone") ?? "")
  const ok = Number.isFinite(interval) && Number.isFinite(deadline) && interval > 0
  const low = interval
  const high = interval + deadline
  const tone = !ok ? "muted" : low >= 180 ? "bad" : high > 180 ? "warn" : "good"
  const buildWindow = parseWindow(build)
  return (
    <section className="@container overflow-hidden rounded-xl border bg-border shadow-card">
      <div className="grid gap-px @xl:grid-cols-2 @4xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1fr)] [&>div]:bg-card">
      <div className="space-y-1.5 p-4 sm:px-5 @xl:col-span-2 @4xl:col-span-1">
        <div className="flex items-center gap-2 text-meta font-medium text-muted-foreground">
          <Clock3 className="size-4 stroke-[1.75] text-brand" />
          Độ trễ phát hiện tin mới
        </div>
        <div className="text-2xl font-semibold tracking-tight text-foreground tabular-nums">
          {ok ? `≈ ${minutesText(low)}–${minutesText(high)} phút` : "–"}
        </div>
        <p className="text-meta text-muted-foreground">
          {ok
            ? `Chu kỳ poll ${num(interval)} giây + thời gian một lượt poll (tối đa ${num(deadline)} giây). Mục tiêu: tin mới được phát hiện trong 3 phút.`
            : "Nhập chu kỳ poll và deadline của lượt poll để ước tính."}
        </p>
        {ok && (
          <Pill
            className={cn(
              "h-auto min-h-5 py-0.5 whitespace-normal",
              tone === "good" && "bg-success/12 text-success-ink",
              tone === "warn" && "bg-info/12 text-info-ink",
              tone === "bad" && "bg-destructive/10 text-danger-ink",
            )}
          >
            {tone === "good"
              ? "Trong mục tiêu 3 phút"
              : tone === "warn"
                ? `Đạt mục tiêu 3 phút khi lượt poll xong trong ${num(180 - low)} giây`
                : "Chu kỳ poll đã vượt mục tiêu 3 phút"}
          </Pill>
        )}
      </div>
      <div className="space-y-1.5 p-4 sm:px-5">
        <div className="flex items-center gap-2 text-meta font-medium text-muted-foreground">
          <Moon className="size-4 stroke-[1.75] text-brand" />
          Build bộ khung
        </div>
        <div className="text-title font-semibold text-foreground tabular-nums">{describeWindow(build)}</div>
        <p className="text-meta text-muted-foreground">
          {buildWindow ? "Mỗi doanh nghiệp được build lại một lần, rải đều trong khung." : "Khung giờ chưa hợp lệ."}
        </p>
      </div>
      <div className="space-y-1.5 p-4 sm:px-5">
        <div className="flex items-center gap-2 text-meta font-medium text-muted-foreground">
          <Sun className="size-4 stroke-[1.75] text-brand" />
          Poll tin mới
        </div>
        <div className="text-title font-semibold text-foreground tabular-nums">{describeWindow(poll)}</div>
        <p className="text-meta text-muted-foreground">Giờ theo múi {tz || "–"}.</p>
      </div>
      </div>
    </section>
  )
}

// --- một cài đặt -----------------------------------------------------------------------------------

function SettingRow({
  item,
  draft,
  error,
  onChange,
}: {
  item: SettingItem
  draft: Draft
  error: string | undefined
  onChange: (key: string, raw: Raw | undefined) => void
}) {
  const id = `setting-${item.key}`
  const raw = currentRaw(item, draft)
  const dirty = isDirty(item, draft)
  const resetting = draft[item.key] === null && item.source === "ui"
  const source = SOURCE[item.source]
  const fallback = fallbackValue(item)
  const secretState = item.secret
    ? resetting
      ? item.env_is_set
      : item.is_set
    : null
  return (
    <div
      id={`row-${item.key}`}
      className={cn(
        "grid scroll-mt-24 gap-3 px-4 py-4 sm:px-5 @2xl:grid-cols-[minmax(0,1fr)_minmax(0,20rem)] @2xl:gap-8 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]",
        dirty && "bg-primary/[0.03] dark:bg-primary/[0.06]",
      )}
    >
      <div className="min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <label htmlFor={id} className="mr-1 text-sm font-medium text-foreground">
            {item.label}
          </label>
          <Pill title={source.title} className={source.className}>
            {source.text}
          </Pill>
          {!item.live && (
            <Pill title="Thay đổi chỉ có hiệu lực sau khi khởi động lại các service" className="bg-warning/16 text-warning-ink">
              <RotateCw className="size-3" /> Cần khởi động lại
            </Pill>
          )}
          {dirty && <Pill className="bg-primary text-primary-foreground">{resetting ? "Sẽ khôi phục" : "Đã sửa"}</Pill>}
        </div>
        <p className="text-meta text-muted-foreground">{item.help}</p>
        <p className="text-caption text-muted-foreground/80">
          {item.secret ? (
            <>Mặc định: {item.key === "llm_api_key" ? "không cần khoá" : "chưa đặt"}</>
          ) : (
            <>Mặc định: {formatValue(item, item.default)}</>
          )}
          {item.source !== "default" && item.env_value !== null && !item.secret && (
            <> · Biến môi trường: {formatValue(item, item.env_value)}</>
          )}
          {item.secret && item.env_is_set && <> · Biến môi trường: đã đặt</>}
          <span className="font-mono"> · {item.key}</span>
        </p>
      </div>
      <div className="min-w-0 space-y-1.5">
        {item.secret && (
          <div className="flex flex-wrap items-center gap-1.5 text-meta">
            {secretState ? (
              <Pill className="bg-success/12 text-success-ink">Đã đặt</Pill>
            ) : (
              <Pill className="bg-muted text-muted-foreground ring-1 ring-border/70 ring-inset">Chưa đặt</Pill>
            )}
          </div>
        )}
        <SettingField item={item} value={raw} onChange={(v) => onChange(item.key, v)} id={id} invalid={Boolean(error)} />
        {error && (
          <p role="alert" className="text-meta font-medium text-danger-ink">
            {cap(error)}
          </p>
        )}
        {resetting ? (
          <p className="flex flex-wrap items-center gap-x-2 text-meta text-muted-foreground">
            Khi lưu sẽ về {item.env_value !== null || item.env_is_set ? "giá trị biến môi trường" : "giá trị mặc định"}
            {!item.secret && <>: {formatValue(item, fallback)}</>}
            <button type="button" className="text-brand hover:underline" onClick={() => onChange(item.key, undefined)}>
              Giữ giá trị đang đặt
            </button>
          </p>
        ) : (
          item.source === "ui" && (
            <button
              type="button"
              onClick={() => onChange(item.key, null)}
              className="inline-flex items-center gap-1 text-meta text-muted-foreground transition-colors hover:text-foreground"
            >
              <RotateCcw className="size-3.5" />
              {item.secret ? "Xoá khoá đặt ở giao diện" : "Khôi phục"}{" "}
              <span className="text-muted-foreground/80">
                ({item.env_value !== null || item.env_is_set ? "biến môi trường" : "mặc định"}
                {!item.secret && `: ${formatValue(item, fallback)}`})
              </span>
            </button>
          )
        )}
      </div>
    </div>
  )
}

// --- trang -----------------------------------------------------------------------------------------

function matches(item: SettingItem, q: string) {
  return fold(`${item.label} ${item.key} ${item.help}`).includes(q)
}

export function SettingsPage() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsData>("/settings") })
  const [draft, setDraft] = useState<Draft>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [group, setGroup] = useState<string>("schedule")
  const [search, setSearch] = useState("")
  const [restart, setRestart] = useState<string[]>([])
  const topRef = useRef<HTMLDivElement>(null)

  const groups: SettingGroup[] = useMemo(() => query.data?.groups ?? [], [query.data])
  const items = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, i] as const))), [groups])
  const groupOf = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, g.group] as const))), [groups])
  const dirtyKeys = useMemo(() => [...items.values()].filter((i) => isDirty(i, draft)).map((i) => i.key), [items, draft])
  const dirty = dirtyKeys.length > 0
  const leaveDialog = useLeaveGuard(dirty)

  const change = (key: string, raw: Raw | undefined) => {
    setDraft((d) => {
      const next = { ...d }
      if (raw === undefined) delete next[key]
      else next[key] = raw
      return next
    })
    setErrors((e) => {
      if (!(key in e)) return e
      const next = { ...e }
      delete next[key]
      return next
    })
  }

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.put<SettingsSaved>("/settings", { values }),
    onSuccess: (data, values) => {
      const { restart_required, ...page } = data
      client.setQueryData(["settings"], page)
      setDraft({})
      setErrors({})
      const count = Object.keys(values).length
      toast.success(`Đã lưu ${num(count)} thay đổi`)
      setRestart(restart_required)
      if (restart_required.length) topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
    },
    onError: (e) => {
      const fields = e instanceof ApiError ? e.fieldErrors : null
      if (fields && Object.keys(fields).length) {
        setErrors(fields)
        const first = Object.keys(fields).find((k) => groupOf.has(k))
        if (first && !search) setGroup(groupOf.get(first)!)
        toast.error(`Chưa lưu: ${num(Object.keys(fields).length)} giá trị chưa hợp lệ`)
      } else {
        toast.error(cap(e instanceof Error ? e.message : String(e)))
      }
    },
  })

  const submit = () => {
    const local: Record<string, string> = {}
    const values: Record<string, unknown> = {}
    for (const key of dirtyKeys) {
      const item = items.get(key)!
      const raw = draft[key]
      const problem = localError(item, raw)
      if (problem) local[key] = problem
      else values[key] = toPayload(item, raw)
    }
    if (Object.keys(local).length) {
      setErrors(local)
      const first = Object.keys(local)[0]
      if (!search) setGroup(groupOf.get(first) ?? group)
      toast.error(`Chưa lưu: ${num(Object.keys(local).length)} giá trị chưa hợp lệ`)
      return
    }
    save.mutate(values)
  }

  const cancel = () => {
    setDraft({})
    setErrors({})
  }

  const q = fold(search.trim())
  const visible = q
    ? groups.map((g) => ({ ...g, items: g.items.filter((i) => matches(i, q)) })).filter((g) => g.items.length)
    : groups.filter((g) => g.group === group)
  const matchCount = (g: SettingGroup) => (q ? g.items.filter((i) => matches(i, q)).length : g.items.length)
  const labelOf = (key: string) => items.get(key)?.label ?? key

  return (
    <>
      <div ref={topRef} className="scroll-mt-20" />
      <PageHeader
        title="Cài đặt"
        description="Giá trị đặt ở đây đè lên cấu hình triển khai và được các service nạp lại; mục có nhãn “Cần khởi động lại” chỉ có hiệu lực khi service khởi động lại."
      />
      {query.isError && !query.data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      {query.data && (
        <div className={cn("space-y-6", dirty && "pb-20")}>
          {restart.length > 0 && (
            <div
              role="status"
              className="flex items-start gap-3 rounded-xl border border-warning/40 bg-warning/8 p-4 text-sm dark:bg-warning/10"
            >
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-ink" />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-warning-ink">Đã lưu — một số thay đổi cần khởi động lại service mới có hiệu lực</div>
                <div className="text-muted-foreground">{restart.map(labelOf).join(", ")}.</div>
              </div>
              <Button variant="ghost" size="icon-sm" aria-label="Đóng thông báo" onClick={() => setRestart([])}>
                <X />
              </Button>
            </div>
          )}

          <ScheduleSummary items={items} draft={draft} />

          <div className="grid gap-6 xl:grid-cols-[15rem_minmax(0,1fr)]">
            {/* Điều hướng nhóm: cột dọc trên màn hình rộng, hàng vuốt ngang trên điện thoại. */}
            <nav aria-label="Nhóm cài đặt" className="min-w-0 xl:sticky xl:top-6 xl:self-start">
              <SearchBox
                value={search}
                onChange={setSearch}
                onSubmit={(v) => v !== undefined && setSearch(v)}
                placeholder="Tìm cài đặt"
                className="mb-3 max-w-md xl:max-w-none"
              />
              <div className="-mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1 [scrollbar-width:none] sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8 xl:mx-0 xl:flex-col xl:gap-0.5 xl:overflow-visible xl:px-0 xl:pb-0">
                {groups.map((g) => {
                  const active = !q && g.group === group
                  const changed = g.items.filter((i) => dirtyKeys.includes(i.key)).length
                  const bad = g.items.some((i) => errors[i.key])
                  const count = matchCount(g)
                  return (
                    <button
                      key={g.group}
                      type="button"
                      aria-current={active ? "true" : undefined}
                      disabled={Boolean(q) && !count}
                      onClick={() => {
                        setSearch("")
                        setGroup(g.group)
                      }}
                      className={cn(
                        "flex h-9 shrink-0 items-center gap-2 rounded-lg border px-3 text-sm whitespace-nowrap text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none disabled:opacity-40 xl:border-transparent",
                        "max-xl:bg-card",
                        active &&
                          "border-primary/40 bg-primary/8 font-medium text-brand hover:bg-primary/10 hover:text-brand max-xl:bg-primary/8 dark:bg-primary/15",
                      )}
                    >
                      <span className="flex-1 truncate text-left">{g.title}</span>
                      {bad && <span className="size-2 rounded-full bg-destructive" aria-label="Có lỗi" />}
                      {changed > 0 ? (
                        <span className="rounded-full bg-primary px-1.5 text-caption font-semibold text-primary-foreground tabular-nums">
                          {num(changed)}
                        </span>
                      ) : (
                        q && <span className="text-caption text-muted-foreground tabular-nums">{num(count)}</span>
                      )}
                    </button>
                  )
                })}
              </div>
            </nav>

            <div className="min-w-0 space-y-6">
              {visible.length === 0 && (
                <div className="rounded-xl border bg-card shadow-card">
                  <Empty icon={<SearchX />} title="Không có cài đặt nào khớp" action={
                    <Button variant="outline" size="sm" onClick={() => setSearch("")}>
                      Xoá ô tìm
                    </Button>
                  }>
                    Thử tìm bằng từ khác, ví dụ “proxy”, “poll”, “LLM”.
                  </Empty>
                </div>
              )}
              {visible.map((g) => (
                <section
                  key={g.group}
                  className="@container min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-card"
                >
                  <div className="border-b px-4 py-3.5 sm:px-5">
                    <h2 className="text-title font-semibold text-foreground">{g.title}</h2>
                    <p className="text-meta text-muted-foreground">{g.description}</p>
                  </div>
                  <div className="divide-y">
                    {g.items.map((item) => (
                      <SettingRow key={item.key} item={item} draft={draft} error={errors[item.key]} onChange={change} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Thanh lưu: luôn ở đáy màn hình khi có thay đổi chưa lưu. */}
      {dirty && (
        <div className="pointer-events-none fixed inset-x-0 bottom-4 z-40 flex justify-center px-4 lg:pl-64">
          <div className="pointer-events-auto flex w-full max-w-xl items-center gap-2 rounded-xl border bg-popover py-2 pr-2 pl-4 text-sm shadow-xl">
            <span className="min-w-0 flex-1 truncate font-medium tabular-nums">{num(dirtyKeys.length)} thay đổi chưa lưu</span>
            <Button variant="ghost" size="sm" onClick={cancel} disabled={save.isPending}>
              Huỷ
            </Button>
            <Button size="sm" onClick={submit} disabled={save.isPending}>
              <Save /> {save.isPending ? "Đang lưu…" : "Lưu"}
            </Button>
          </div>
        </div>
      )}
      {leaveDialog}
    </>
  )
}
