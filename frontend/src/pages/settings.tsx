import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  BadgeCheck,
  Briefcase,
  CalendarClock,
  Compass,
  Cpu,
  Globe,
  Network,
  RotateCw,
  Save,
  SearchX,
  Server,
  Sparkles,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useBlocker } from "react-router"
import { toast } from "sonner"

import { Empty, ErrorState, PageHeader, PageLoading, SearchBox } from "@/components/common"
import {
  currentRaw,
  fold,
  isCustom,
  isDirty,
  localError,
  sectionsOf,
  toPayload,
  type Draft,
  type Raw,
  type SectionView,
} from "@/components/settings/model"
import { SettingRow } from "@/components/settings/row"
import { ScheduleOverview } from "@/components/settings/schedule"
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
import { api, ApiError, errorMessage } from "@/lib/api"
import { num } from "@/lib/format"
import { settingsQuery } from "@/lib/queries"
import type { SettingGroup, SettingItem, SettingsSaved } from "@/lib/types"
import { cn } from "@/lib/utils"

const GROUP_ICON: Record<string, LucideIcon> = {
  schedule: CalendarClock,
  runner: Cpu,
  workers: Server,
  proxy: Network,
  network: Globe,
  sources: Compass,
  boards: Briefcase,
  ai: Sparkles,
  quality: BadgeCheck,
  maintenance: Wrench,
}

function GroupIcon({ group, className }: { group: string; className?: string }) {
  const Icon = GROUP_ICON[group] ?? Wrench
  return <Icon className={cn("size-4 shrink-0 stroke-[1.75]", className)} aria-hidden />
}

/**
 * Cảnh báo khi rời trang lúc còn thay đổi chưa lưu: chuyển trang trong ứng dụng (link, nút
 * quay lại / tiến tới) hỏi bằng hộp thoại; đóng tab hay tải lại dùng hộp thoại của trình duyệt.
 */
function useLeaveGuard(dirty: boolean) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && (currentLocation.pathname !== nextLocation.pathname || currentLocation.search !== nextLocation.search),
  )
  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ""
    }
    window.addEventListener("beforeunload", beforeUnload)
    return () => window.removeEventListener("beforeunload", beforeUnload)
  }, [dirty])
  return (
    <AlertDialog open={blocker.state === "blocked"} onOpenChange={(open) => !open && blocker.reset?.()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Rời trang khi chưa lưu?</AlertDialogTitle>
          <AlertDialogDescription>Các thay đổi chưa lưu trên trang Cài đặt sẽ mất.</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Ở lại</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              // Không để hộp thoại tự đóng (onOpenChange sẽ gọi reset() và huỷ lượt chuyển trang):
              // proceed() đưa blocker sang "proceeding", hộp thoại đóng theo.
              e.preventDefault()
              blocker.proceed?.()
            }}
          >
            Rời trang
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function matches(item: SettingItem, q: string) {
  return fold(`${item.label} ${item.key} ${item.help}`).includes(q)
}

type View = "all" | "custom"

// --- khối hiển thị ---------------------------------------------------------------------------------

/** Tiêu đề mục; mục chỉ có một cài đặt cùng tên thì bỏ tiêu đề để khỏi lặp. */
function sectionTitle(s: SectionView) {
  return s.items.length === 1 && fold(s.title) === fold(s.items[0].label) ? "" : s.title
}

function SectionCard({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <section className="@container min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-card">
      {title && (
        <div className="border-b bg-muted/40 px-4 py-2.5 sm:px-5 dark:bg-muted/20">
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          {description && <p className="text-meta text-pretty text-muted-foreground">{description}</p>}
        </div>
      )}
      <div className="divide-y">{children}</div>
    </section>
  )
}

function GroupHeader({ group, dirty }: { group: SettingGroup; dirty: number }) {
  const custom = group.items.filter(isCustom).length
  const restart = group.items.filter((i) => !i.live).length
  return (
    <header className="flex items-start gap-3">
      <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary/10 text-brand dark:bg-primary/15">
        <GroupIcon group={group.group} className="size-5" />
      </span>
      <div className="min-w-0 space-y-1">
        <h2 className="text-lg font-semibold tracking-tight text-foreground">{group.title}</h2>
        <p className="max-w-3xl text-meta text-pretty text-muted-foreground">{group.description}</p>
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5 text-caption text-muted-foreground tabular-nums">
          <span>{num(group.items.length)} cài đặt</span>
          {custom > 0 && <span className="text-info-ink">{num(custom)} khác mặc định</span>}
          {dirty > 0 && <span className="font-medium text-brand">{num(dirty)} chưa lưu</span>}
          {restart > 0 && (
            <span className="inline-flex items-center gap-1">
              <RotateCw className="size-3 text-warning-ink" aria-hidden />
              {restart === group.items.length ? "Mọi mục cần khởi động lại" : `${num(restart)} mục cần khởi động lại`}
            </span>
          )}
        </p>
      </div>
    </header>
  )
}

// --- trang -----------------------------------------------------------------------------------------

export function SettingsPage() {
  const client = useQueryClient()
  const query = useQuery(settingsQuery())
  const [draft, setDraft] = useState<Draft>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [group, setGroup] = useState<string>("schedule")
  const [search, setSearch] = useState("")
  const [view, setView] = useState<View>("all")
  const [restart, setRestart] = useState<string[]>([])
  const topRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)

  const groups: SettingGroup[] = useMemo(() => query.data?.groups ?? [], [query.data])
  const items = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, i] as const))), [groups])
  const groupOf = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, g.group] as const))), [groups])
  const dirtyKeys = useMemo(() => [...items.values()].filter((i) => isDirty(i, draft)).map((i) => i.key), [items, draft])
  const dirty = dirtyKeys.length > 0
  const leaveDialog = useLeaveGuard(dirty)
  const customCount = useMemo(() => [...items.values()].filter(isCustom).length, [items])
  const tzItem = items.get("crawl_timezone")
  const timezone = tzItem ? String(toPayload(tzItem, currentRaw(tzItem, draft)) ?? "") : ""

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

  /** Mở nhóm chứa lỗi đầu tiên rồi cuộn tới ô đó. */
  const showFirst = (keys: string[]) => {
    const first = keys.find((k) => groupOf.has(k))
    if (!first) return
    setSearch("")
    setView("all")
    setGroup(groupOf.get(first)!)
    setTimeout(() => document.getElementById(`row-${first}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50)
  }

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.put<SettingsSaved>("/settings", { values }),
    onSuccess: (data, values) => {
      const { restart_required, ...page } = data
      client.setQueryData(settingsQuery().queryKey, page)
      setDraft({})
      setErrors({})
      toast.success(`Đã lưu ${num(Object.keys(values).length)} thay đổi`)
      setRestart(restart_required)
      if (restart_required.length) topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
    },
    onError: (e) => {
      const fields = e instanceof ApiError ? e.fieldErrors : null
      if (fields && Object.keys(fields).length) {
        setErrors(fields)
        showFirst(Object.keys(fields))
        toast.error(`Chưa lưu: ${num(Object.keys(fields).length)} giá trị chưa hợp lệ`)
      } else {
        toast.error(errorMessage(e))
      }
    },
  })

  const submit = () => {
    if (!dirtyKeys.length || save.isPending) return
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
      showFirst(Object.keys(local))
      toast.error(`Chưa lưu: ${num(Object.keys(local).length)} giá trị chưa hợp lệ`)
      return
    }
    save.mutate(values)
  }
  const submitRef = useRef(submit)
  submitRef.current = submit

  // Ctrl/⌘ + S: lưu.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") {
        e.preventDefault()
        submitRef.current()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const cancel = () => {
    setDraft({})
    setErrors({})
  }

  const openGroup = (key: string) => {
    setSearch("")
    setView("all")
    setGroup(key)
    // Đang cuộn xuống dưới: đưa đầu nhóm mới vào tầm nhìn.
    const top = contentRef.current?.getBoundingClientRect().top
    if (top !== undefined && top < 0) contentRef.current?.scrollIntoView({ block: "start" })
  }

  const q = fold(search.trim())
  const filtering = Boolean(q) || view === "custom"
  const keep = (i: SettingItem) =>
    (!q || matches(i, q)) && (view !== "custom" || isCustom(i) || dirtyKeys.includes(i.key))
  const results = filtering
    ? groups.map((g) => ({ group: g, sections: sectionsOf(g, keep) })).filter((r) => r.sections.length)
    : []
  const current = groups.find((g) => g.group === group) ?? groups[0]
  const labelOf = (key: string) => items.get(key)?.label ?? key
  const row = (item: SettingItem) => (
    <SettingRow
      key={item.key}
      item={item}
      draft={draft}
      error={errors[item.key]}
      timezone={timezone}
      onChange={change}
    />
  )

  return (
    <>
      <div ref={topRef} className="scroll-mt-20" />
      <PageHeader
        title="Cài đặt"
        description={
          <>
            Thay đổi được mọi service nạp lại trong vài giây. Mục có biểu tượng{" "}
            <RotateCw className="inline size-3.5 align-[-2px] text-warning-ink" aria-label="khởi động lại" /> chỉ có hiệu
            lực sau khi khởi động lại service.
          </>
        }
      />
      {query.isError && !query.data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      {query.data && current && (
        <div className={cn("space-y-6", dirty && "pb-20")}>
          {restart.length > 0 && (
            <div
              role="status"
              className="flex items-start gap-3 rounded-xl border border-warning/40 bg-warning/8 p-4 text-sm dark:bg-warning/10"
            >
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-ink" />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-warning-ink">Đã lưu — cần khởi động lại service để các mục sau có hiệu lực</div>
                <div className="text-muted-foreground">{restart.map(labelOf).join(", ")}.</div>
              </div>
              <Button variant="ghost" size="icon-sm" aria-label="Đóng thông báo" onClick={() => setRestart([])}>
                <X />
              </Button>
            </div>
          )}

          <div className="grid gap-6 xl:grid-cols-[15.5rem_minmax(0,1fr)] xl:gap-8">
            {/* Điều hướng: cột dọc dính trên màn hình rộng, hàng vuốt ngang trên điện thoại. */}
            <nav aria-label="Nhóm cài đặt" className="min-w-0 space-y-3 xl:sticky xl:top-6 xl:self-start">
              <div className="flex flex-col gap-2 sm:flex-row xl:flex-col">
                <SearchBox
                  value={search}
                  onChange={setSearch}
                  onSubmit={(v) => v !== undefined && setSearch(v)}
                  placeholder="Tìm cài đặt"
                  className="sm:max-w-sm sm:flex-1 xl:max-w-none"
                />
                <div role="group" aria-label="Lọc cài đặt" className="grid h-9 shrink-0 grid-cols-2 rounded-lg bg-muted p-0.5 text-meta sm:w-72 xl:w-auto">
                  {(
                    [
                      ["all", "Tất cả", null],
                      ["custom", "Tuỳ chỉnh", customCount],
                    ] as const
                  ).map(([value, text, count]) => (
                    <button
                      key={value}
                      type="button"
                      aria-pressed={view === value}
                      title={value === "custom" ? "Cài đặt đặt bằng biến môi trường hoặc ở giao diện" : undefined}
                      onClick={() => setView(value)}
                      className={cn(
                        "inline-flex items-center justify-center gap-1.5 rounded-md px-2 font-medium whitespace-nowrap text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                        view === value && "bg-card text-foreground shadow-xs",
                      )}
                    >
                      {text}
                      {count !== null && <span className="text-caption text-muted-foreground tabular-nums">{num(count)}</span>}
                    </button>
                  ))}
                </div>
              </div>

              <div className="-mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1 [scrollbar-width:none] sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8 xl:mx-0 xl:flex-col xl:gap-0.5 xl:overflow-visible xl:px-0 xl:pb-0">
                {groups.map((g) => {
                  const active = !filtering && g.group === current.group
                  const changed = g.items.filter((i) => dirtyKeys.includes(i.key)).length
                  const bad = g.items.some((i) => errors[i.key])
                  const hits = filtering ? g.items.filter(keep).length : null
                  return (
                    <button
                      key={g.group}
                      type="button"
                      aria-current={active ? "true" : undefined}
                      disabled={hits === 0}
                      onClick={() => openGroup(g.group)}
                      className={cn(
                        "group/nav flex h-9 shrink-0 items-center gap-2.5 rounded-lg border px-3 text-sm whitespace-nowrap text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none disabled:opacity-40 xl:border-transparent",
                        "max-xl:bg-card",
                        active &&
                          "border-primary/40 bg-primary/8 font-medium text-brand hover:bg-primary/10 hover:text-brand max-xl:bg-primary/8 dark:bg-primary/15",
                      )}
                    >
                      <GroupIcon
                        group={g.group}
                        className={cn("text-muted-foreground group-hover/nav:text-foreground", active && "text-brand group-hover/nav:text-brand")}
                      />
                      <span className="flex-1 truncate text-left">{g.title}</span>
                      {bad && <span className="size-2 rounded-full bg-destructive" aria-label="Có lỗi" />}
                      {changed > 0 ? (
                        <span
                          className="rounded-full bg-primary px-1.5 text-caption font-semibold text-primary-foreground tabular-nums"
                          aria-label={`${num(changed)} thay đổi chưa lưu`}
                        >
                          {num(changed)}
                        </span>
                      ) : (
                        hits !== null && <span className="text-caption text-muted-foreground tabular-nums">{num(hits)}</span>
                      )}
                    </button>
                  )
                })}
              </div>
            </nav>

            <div ref={contentRef} className="min-w-0 scroll-mt-6 space-y-5">
              {filtering ? (
                results.length === 0 ? (
                  <div className="rounded-xl border bg-card shadow-card">
                    <Empty
                      icon={<SearchX />}
                      title={q ? "Không có cài đặt nào khớp" : "Mọi cài đặt đang dùng giá trị mặc định"}
                      action={
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setSearch("")
                            setView("all")
                          }}
                        >
                          Xem tất cả
                        </Button>
                      }
                    >
                      {q ? "Thử từ khác, ví dụ “proxy”, “poll”, “LLM”." : "Chưa có mục nào đặt bằng biến môi trường hay ở giao diện."}
                    </Empty>
                  </div>
                ) : (
                  results.map(({ group: g, sections }) => (
                    <div key={g.group} className="space-y-3">
                      <button
                        type="button"
                        onClick={() => openGroup(g.group)}
                        className="inline-flex items-center gap-2 rounded-md text-sm font-semibold text-foreground hover:text-brand focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
                      >
                        <GroupIcon group={g.group} className="text-brand" />
                        {g.title}
                      </button>
                      {sections.map((s) => (
                        <SectionCard key={s.title || g.group} title={sectionTitle(s)} description={s.description}>
                          {s.items.map(row)}
                        </SectionCard>
                      ))}
                    </div>
                  ))
                )
              ) : (
                <>
                  <GroupHeader group={current} dirty={current.items.filter((i) => dirtyKeys.includes(i.key)).length} />
                  {current.group === "schedule" && <ScheduleOverview items={items} draft={draft} />}
                  {sectionsOf(current).map((s) => (
                    <SectionCard key={s.title || current.group} title={sectionTitle(s)} description={s.description}>
                      {s.items.map(row)}
                    </SectionCard>
                  ))}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Thanh lưu: luôn ở đáy màn hình khi có thay đổi chưa lưu. */}
      {dirty && (
        <div className="pointer-events-none fixed inset-x-0 bottom-4 z-40 flex justify-center px-4 lg:pl-64">
          <div className="pointer-events-auto flex w-full max-w-xl items-center gap-2 rounded-xl border bg-popover py-2 pr-2 pl-4 text-sm shadow-xl">
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium tabular-nums">{num(dirtyKeys.length)} thay đổi chưa lưu</span>
              <span className="ml-2 hidden text-caption text-muted-foreground sm:inline">Ctrl + S để lưu</span>
            </span>
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
