import { useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  BadgeCheck,
  BellRing,
  Briefcase,
  CalendarClock,
  Compass,
  Cpu,
  Database,
  Globe,
  Loader2,
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

import { Empty, ErrorState, PageLoading, SearchBox } from "@/components/common"
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
import { SETTINGS_PARAM } from "@/components/settings-dialog"
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
import { DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { ApiError, errorMessage } from "@/lib/api"
import { num } from "@/lib/format"
import {
  getGetProxiesQueryKey,
  getGetSettingsQueryKey,
  useGetSettings,
  useImportEnvSettings,
  usePutSettings,
} from "@/lib/gen/queries"
import type { SettingGroup, SettingItem, SettingsPage as SettingsPageData } from "@/lib/types"
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
  alerts: BellRing,
  maintenance: Wrench,
}

function GroupIcon({ group, className }: { group: string; className?: string }) {
  const Icon = GROUP_ICON[group] ?? Wrench
  return <Icon className={cn("size-4 shrink-0 stroke-[1.75]", className)} aria-hidden />
}

/**
 * Cảnh báo khi đóng Cài đặt lúc còn thay đổi chưa lưu: đóng popup (Esc, nút đóng, bấm ra ngoài —
 * đều là bỏ `?settings` khỏi URL), chuyển trang, nút quay lại hỏi bằng hộp thoại; đóng tab hay
 * tải lại dùng hộp thoại của trình duyệt.
 */
function useLeaveGuard(dirty: boolean) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      (currentLocation.pathname !== nextLocation.pathname || !new URLSearchParams(nextLocation.search).has(SETTINGS_PARAM)),
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
          <AlertDialogTitle>Đóng Cài đặt khi chưa lưu?</AlertDialogTitle>
          <AlertDialogDescription>Các thay đổi chưa lưu sẽ mất.</AlertDialogDescription>
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
            Bỏ thay đổi
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function matches(item: SettingItem, q: string) {
  return fold(`${item.label} ${item.key} ${item.help}`).includes(q)
}

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

function GroupHeader({ group, dirty, autoRestart }: { group: SettingGroup; dirty: number; autoRestart: boolean }) {
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
              {restart === group.items.length ? "Mọi mục" : `${num(restart)} mục`}
              {autoRestart ? " áp dụng bằng cách khởi động lại backend" : " cần khởi động lại backend"}
            </span>
          )}
        </p>
      </div>
    </header>
  )
}

// --- popup -----------------------------------------------------------------------------------------

/** Nội dung popup Cài đặt (`components/settings-dialog.tsx` mở / đóng theo `?settings`). */
export function SettingsPanel({
  initialGroup,
  initialKey = null,
}: {
  initialGroup: string | null
  initialKey?: string | null
}) {
  const client = useQueryClient()
  // Đang áp dụng cài đặt cần khởi động lại: hỏi lại mỗi 2 giây tới khi backend báo xong (lúc
  // backend khởi động lại API trả lỗi vài giây — giữ dữ liệu cũ, hỏi tiếp).
  const query = useGetSettings({
    query: { refetchInterval: (q) => (q.state.data?.restart_pending ? 2000 : false) },
  })
  const [draft, setDraft] = useState<Draft>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [group, setGroup] = useState<string>(initialGroup || "schedule")
  const [search, setSearch] = useState("")
  const [restart, setRestart] = useState<string[]>([])
  const contentRef = useRef<HTMLDivElement>(null)

  const groups: SettingGroup[] = useMemo(() => query.data?.groups ?? [], [query.data])
  const items = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, i] as const))), [groups])
  const groupOf = useMemo(() => new Map(groups.flatMap((g) => g.items.map((i) => [i.key, g.group] as const))), [groups])
  const dirtyKeys = useMemo(() => [...items.values()].filter((i) => isDirty(i, draft)).map((i) => i.key), [items, draft])
  const dirty = dirtyKeys.length > 0
  const leaveDialog = useLeaveGuard(dirty)
  const autoRestart = query.data?.auto_restart ?? false
  const pending = query.data?.restart_pending ?? false
  const envKeys = query.data?.env_keys ?? []
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
    setGroup(groupOf.get(first)!)
    setTimeout(() => document.getElementById(`row-${first}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 50)
  }

  // Mở thẳng tới một ô (vd. từ hộp thoại "Thêm worker"): cuộn tới và đặt con trỏ vào, một lần.
  const focused = useRef(false)
  useEffect(() => {
    if (focused.current || !initialKey || !groupOf.has(initialKey)) return
    focused.current = true
    showFirst([initialKey])
    setTimeout(() => document.getElementById(`setting-${initialKey}`)?.focus({ preventScroll: true }), 100)
    // Chỉ chạy khi cài đặt vừa tải xong.
  }, [initialKey, groupOf])

  // Hết "đang áp dụng" (backend đã khởi động lại xong): báo một lần.
  const wasPending = useRef(false)
  useEffect(() => {
    if (wasPending.current && !pending && query.data) {
      toast.success("Đã áp dụng cài đặt")
      setRestart([])
      void client.invalidateQueries({ queryKey: getGetProxiesQueryKey() })
    }
    wasPending.current = pending
  }, [pending, query.data, client])

  const setPage = (data: SettingsPageData) => {
    client.setQueryData(getGetSettingsQueryKey(), {
      version: data.version,
      groups: data.groups,
      env_keys: data.env_keys,
      auto_restart: data.auto_restart,
      restart_pending: data.restart_pending,
    })
    // Danh sách proxy vừa lưu: trạng thái xoay vòng theo danh sách mới.
    void client.invalidateQueries({ queryKey: getGetProxiesQueryKey() })
  }

  const importEnv = useImportEnvSettings({
    mutation: {
      onSuccess: (data) => {
        setPage(data)
        const skipped = Object.keys(data.skipped)
        if (data.imported.length) toast.success(`Đã lưu ${num(data.imported.length)} cài đặt vào cơ sở dữ liệu`)
        if (skipped.length)
          toast.warning(
            `${num(skipped.length)} giá trị không hợp lệ, vẫn lấy từ biến môi trường: ${skipped.map((k) => labelOf(k)).join(", ")}`,
          )
      },
    },
  })

  const save = usePutSettings({
    mutation: {
      onSuccess: (data, { data: { values } }) => {
        const { restart_required } = data
        setPage(data)
        setDraft({})
        setErrors({})
        toast.success(`Đã lưu ${num(Object.keys(values).length)} thay đổi`)
        setRestart(restart_required)
        // Phải tự khởi động lại backend: đưa thông báo ở đầu vào tầm nhìn.
        if (restart_required.length && !data.auto_restart) contentRef.current?.scrollTo({ top: 0, behavior: "smooth" })
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
    save.mutate({ data: { values } })
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
    setGroup(key)
    contentRef.current?.scrollTo({ top: 0 })
  }

  const q = fold(search.trim())
  const filtering = Boolean(q)
  const keep = (i: SettingItem) => matches(i, q)
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
      autoRestart={autoRestart}
      onChange={change}
    />
  )

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b px-4 py-4 pr-14 sm:px-6">
        <DialogTitle className="text-lg font-semibold tracking-tight">Cài đặt</DialogTitle>
        <DialogDescription className="mt-1 max-w-4xl text-meta text-pretty text-muted-foreground">
          Cài đặt lưu trong cơ sở dữ liệu; lưu xong backend nạp lại trong vài giây. Mục có biểu tượng{" "}
          <RotateCw className="inline size-3.5 align-[-2px] text-warning-ink" aria-label="khởi động lại" />{" "}
          {autoRestart
            ? "được áp dụng bằng cách tự khởi động lại các process của backend (vài giây)."
            : "chỉ có hiệu lực sau khi backend khởi động lại."}
        </DialogDescription>
      </header>
      {query.isError && !query.data && (
        <div className="p-4 sm:p-6">
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        </div>
      )}
      {query.isLoading && (
        <div className="p-4 sm:p-6">
          <PageLoading />
        </div>
      )}
      {query.data && current && (
        <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[15rem_minmax(0,1fr)] md:grid-rows-1">
          {/* Điều hướng: cột dọc bên trái trên màn hình rộng, hàng vuốt ngang trên điện thoại. */}
          <nav
            aria-label="Nhóm cài đặt"
            className="min-w-0 space-y-3 border-b px-4 py-3 md:overflow-y-auto md:border-r md:border-b-0 md:bg-muted/20 md:p-4"
          >
            <SearchBox
              value={search}
              onChange={setSearch}
              onSubmit={(v) => v !== undefined && setSearch(v)}
              placeholder="Tìm cài đặt"
            />
            <div className="-mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1 [scrollbar-width:none] md:mx-0 md:flex-col md:gap-0.5 md:overflow-visible md:px-0 md:pb-0">
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
                      "group/nav flex h-9 shrink-0 items-center gap-2.5 rounded-lg border px-3 text-sm whitespace-nowrap text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none disabled:opacity-40 md:border-transparent",
                      "max-md:bg-card",
                      active &&
                        "border-primary/40 bg-primary/8 font-medium text-brand hover:bg-primary/10 hover:text-brand max-md:bg-primary/8 dark:bg-primary/15",
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

          <div ref={contentRef} className="min-h-0 min-w-0 space-y-5 overflow-y-auto px-4 py-5 sm:px-6">
            {pending ? (
              <div
                role="status"
                className="flex items-start gap-3 rounded-xl border border-info/40 bg-info/8 p-4 text-sm dark:bg-info/10"
              >
                <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-info-ink" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-info-ink">Đang áp dụng — backend đang khởi động lại các process</div>
                  <div className="text-muted-foreground">
                    {restart.length ? `${restart.map(labelOf).join(", ")}. ` : ""}Thường mất vài giây; việc đang chạy được
                    xếp lại.
                  </div>
                </div>
              </div>
            ) : (
              restart.length > 0 &&
              !autoRestart && (
                <div
                  role="status"
                  className="flex items-start gap-3 rounded-xl border border-warning/40 bg-warning/8 p-4 text-sm dark:bg-warning/10"
                >
                  <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning-ink" />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-warning-ink">Đã lưu — các mục sau có hiệu lực khi backend khởi động lại</div>
                    <div className="text-muted-foreground">{restart.map(labelOf).join(", ")}.</div>
                  </div>
                  <Button variant="ghost" size="icon-sm" aria-label="Đóng thông báo" onClick={() => setRestart([])}>
                    <X />
                  </Button>
                </div>
              )
            )}

            {envKeys.length > 0 && (
              <div className="flex flex-col gap-3 rounded-xl border bg-card p-4 text-sm shadow-card sm:flex-row sm:items-center">
                <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-info/12 text-info-ink">
                  <Database className="size-4.5" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-foreground">
                    {num(envKeys.length)} cài đặt đang lấy từ biến môi trường của backend
                  </div>
                  <div className="text-meta text-pretty text-muted-foreground">
                    {envKeys.map(labelOf).join(", ")}. Lưu vào cơ sở dữ liệu để sửa và giữ tại trang này — giá trị đang dùng
                    không đổi.
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="shrink-0 self-start sm:self-center"
                  disabled={importEnv.isPending}
                  onClick={() => importEnv.mutate()}
                >
                  <Database /> {importEnv.isPending ? "Đang lưu…" : "Lưu vào cơ sở dữ liệu"}
                </Button>
              </div>
            )}

            {filtering ? (
              results.length === 0 ? (
                <div className="rounded-xl border bg-card shadow-card">
                  <Empty
                    icon={<SearchX />}
                    title="Không có cài đặt nào khớp"
                    action={
                      <Button variant="outline" size="sm" onClick={() => setSearch("")}>
                        Xem tất cả
                      </Button>
                    }
                  >
                    Thử từ khác, ví dụ “proxy”, “poll”, “LLM”.
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
                <GroupHeader
                  group={current}
                  dirty={current.items.filter((i) => dirtyKeys.includes(i.key)).length}
                  autoRestart={autoRestart}
                />
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
      )}

      {/* Thanh lưu ở đáy popup khi có thay đổi chưa lưu. */}
      {dirty && (
        <footer className="flex shrink-0 items-center gap-2 border-t bg-popover px-4 py-2.5 text-sm sm:px-6">
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
        </footer>
      )}
      {leaveDialog}
    </div>
  )
}
