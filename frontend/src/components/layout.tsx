import {
  BookOpen,
  Briefcase,
  Building2,
  Cpu,
  Globe,
  History,
  Layers,
  LayoutDashboard,
  ListChecks,
  Menu,
  Monitor,
  Moon,
  SlidersHorizontal,
  Sun,
  Target,
  UserRound,
} from "lucide-react"
import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router"

import { useTheme, type Theme } from "@/components/theme"
import { Button } from "@/components/ui/button"
import { Dialog, SheetContent } from "@/components/ui/dialog"
import { num } from "@/lib/format"
import { useGetMe, useGetQueue } from "@/lib/gen/queries"
import { cn } from "@/lib/utils"

const GROUPS = [
  {
    title: "Vận hành",
    items: [
      { to: "/", label: "Tổng quan", icon: LayoutDashboard, end: true },
      { to: "/queue", label: "Hàng đợi", icon: Layers, queue: true },
      { to: "/boards", label: "Trang tuyển dụng", icon: Globe },
    ],
  },
  {
    title: "Dữ liệu",
    items: [
      { to: "/companies", label: "Doanh nghiệp", icon: Building2 },
      { to: "/jobs", label: "Việc làm", icon: Briefcase },
    ],
  },
  {
    title: "Chất lượng",
    items: [
      { to: "/review", label: "Kiểm tay", icon: ListChecks },
      { to: "/eval", label: "Golden set", icon: Target },
    ],
  },
  {
    title: "Hệ thống",
    items: [
      { to: "/workers", label: "Workers", icon: Cpu },
      { to: "/settings", label: "Cài đặt", icon: SlidersHorizontal },
      { to: "/audit", label: "Nhật ký thao tác", icon: History },
    ],
  },
]

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Sáng", icon: Sun },
  { value: "dark", label: "Tối", icon: Moon },
  { value: "system", label: "Theo hệ thống", icon: Monitor },
]

/** Chọn giao diện sáng / tối / theo hệ thống: ba nút liền nhau. */
function ThemeSwitch() {
  const { theme, setTheme } = useTheme()
  return (
    <div role="radiogroup" aria-label="Giao diện" className="flex rounded-lg bg-muted p-0.5">
      {THEMES.map((t) => (
        <button
          key={t.value}
          type="button"
          role="radio"
          aria-checked={theme === t.value}
          title={t.label}
          onClick={() => setTheme(t.value)}
          className={cn(
            "grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
            theme === t.value && "bg-card text-foreground shadow-card",
          )}
        >
          <t.icon className="size-3.5" />
          <span className="sr-only">{t.label}</span>
        </button>
      ))}
    </div>
  )
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <img src="/logo-mark.png" alt="" width={32} height={32} className="size-8 shrink-0" />
      <div className="leading-tight">
        <div className="text-[0.9375rem] font-semibold tracking-tight text-foreground">jobcrawler</div>
        <div className="text-caption text-muted-foreground">Thu thập tin tuyển dụng</div>
      </div>
    </div>
  )
}

/** Tên ngắn của hàng đợi (build, poll, trang tuyển dụng) cho dòng trạng thái. */
function queueShortName(name: string) {
  if (name.includes("poll")) return "poll"
  if (name.includes("board")) return "trang tuyển dụng"
  if (name.includes("build")) return "build"
  return name
}

/** Số việc trong ba hàng đợi (build, poll, trang tuyển dụng), dùng cho mục "Hàng đợi" và dòng
 * trạng thái runner. */
function useQueueSummary() {
  const query = useGetQueue(undefined, { query: { refetchInterval: 15_000, refetchOnWindowFocus: true } })
  const queues = query.data?.queues ?? []
  return {
    loaded: Boolean(query.data),
    active: queues.reduce((n, q) => n + q.active, 0),
    queued: queues.reduce((n, q) => n + q.queued, 0),
    workers: queues.reduce((n, q) => n + q.workers, 0),
    // Hàng đợi không có runner nào nhận việc (một process runner có thể chạy nhiều vai).
    idle: queues.filter((q) => !q.workers && !q.error).map((q) => queueShortName(q.name)),
    error: query.isError || queues.some((q) => q.error),
  }
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const me = useGetMe()
  const queue = useQueueSummary()
  const busy = queue.active + queue.queued
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-16 shrink-0 items-center px-5">
        <Brand />
      </div>
      <nav aria-label="Điều hướng chính" className="flex-1 space-y-6 overflow-y-auto px-3 pt-2 pb-4">
        {GROUPS.map((group) => (
          <div key={group.title}>
            <div className="mb-1.5 px-3 text-caption font-semibold tracking-wide text-muted-foreground/80 uppercase">
              {group.title}
            </div>
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "flex h-9 items-center gap-3 rounded-lg px-3 text-sm text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                      isActive &&
                        "bg-primary/8 font-medium text-brand hover:bg-primary/10 hover:text-brand dark:bg-primary/15 dark:hover:bg-primary/20",
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <item.icon className={cn("size-4 shrink-0 stroke-[1.75]", isActive ? "text-brand" : "text-muted-foreground")} />
                      <span className="flex-1 truncate">{item.label}</span>
                      {item.queue && busy > 0 && (
                        <span
                          title={`${num(queue.active)} đang chạy · ${num(queue.queued)} đang chờ`}
                          className="rounded-full bg-primary/12 px-1.5 text-caption font-semibold text-brand tabular-nums"
                        >
                          {num(busy)}
                        </span>
                      )}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
        <div className="border-t border-sidebar-border pt-4">
          <a
            href="/api/docs"
            target="_blank"
            rel="noopener"
            className="flex h-9 items-center gap-3 rounded-lg px-3 text-sm text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <BookOpen className="size-4 stroke-[1.75] text-muted-foreground" />
            Tài liệu API
          </a>
        </div>
      </nav>
      <div className="space-y-3 border-t border-sidebar-border px-4 py-3.5">
        {queue.loaded && (
          <div className="flex items-center gap-2 text-meta text-muted-foreground">
            <span className="relative flex size-2" aria-hidden>
              {queue.active > 0 && <span className="absolute inset-0 animate-ping rounded-full bg-success opacity-60" />}
              <span
                className={cn(
                  "relative size-2 rounded-full",
                  queue.error || queue.idle.length ? "bg-warning" : queue.active ? "bg-success" : "bg-muted-foreground/50",
                )}
              />
            </span>
            <span className="min-w-0 truncate">
              {queue.error
                ? "Không đọc được hàng đợi"
                : !queue.workers
                  ? "Không có runner nào chạy"
                  : queue.idle.length
                    ? `Hàng đợi ${queue.idle.join(", ")} không có runner`
                    : queue.active
                      ? `${num(queue.active)} việc đang chạy`
                      : "Runner đang rảnh"}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-2 text-meta text-sidebar-foreground">
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-muted text-muted-foreground">
              <UserRound className="size-3.5" />
            </span>
            <span className="truncate">{me.data?.user ? me.data.user : "Không đăng nhập"}</span>
          </span>
          <ThemeSwitch />
        </div>
      </div>
    </div>
  )
}

export function Layout() {
  const [open, setOpen] = useState(false)
  const location = useLocation()
  useEffect(() => {
    setOpen(false)
    window.scrollTo({ top: 0 })
  }, [location.pathname])

  return (
    <div className="min-h-svh lg:grid lg:grid-cols-[256px_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Bỏ qua tới nội dung
      </a>
      <aside className="sticky top-0 hidden h-svh border-r border-sidebar-border bg-sidebar lg:block">
        <Sidebar />
      </aside>

      <Dialog open={open} onOpenChange={setOpen}>
        <SheetContent title="Điều hướng" className="lg:hidden">
          <Sidebar onNavigate={() => setOpen(false)} />
        </SheetContent>
      </Dialog>

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-background/85 px-3 backdrop-blur-md lg:hidden">
          <Button variant="ghost" size="icon" onClick={() => setOpen(true)} aria-label="Mở menu" aria-expanded={open}>
            <Menu />
          </Button>
          <Brand />
        </header>
        <main id="main" className="mx-auto w-full max-w-[1440px] px-4 pt-6 pb-16 sm:px-6 lg:px-8 lg:pt-8 xl:px-10">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
