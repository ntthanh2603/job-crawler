import { useQuery } from "@tanstack/react-query"
import {
  BookOpen,
  Briefcase,
  Building2,
  Globe,
  Layers,
  LayoutDashboard,
  ListChecks,
  Menu,
  Monitor,
  Moon,
  ScanSearch,
  Sun,
  Target,
  UserRound,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router"

import { useTheme, type Theme } from "@/components/theme"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"

const GROUPS = [
  {
    title: "Vận hành",
    items: [
      { to: "/", label: "Tổng quan", icon: LayoutDashboard, end: true },
      { to: "/queue", label: "Hàng đợi", icon: Layers },
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
]

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Sáng", icon: Sun },
  { value: "dark", label: "Tối", icon: Moon },
  { value: "system", label: "Theo hệ thống", icon: Monitor },
]

function ThemeMenu() {
  const { theme, setTheme } = useTheme()
  const current = THEMES.find((t) => t.value === theme) ?? THEMES[2]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs text-muted-foreground">
          <current.icon className="size-3.5" /> {current.label}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {THEMES.map((t) => (
          <DropdownMenuItem key={t.value} onClick={() => setTheme(t.value)}>
            <t.icon /> {t.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
        <ScanSearch className="size-4 stroke-[2]" aria-hidden />
      </div>
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">jobcrawler</div>
        <div className="text-[0.7rem] text-muted-foreground">Thu thập tin tuyển dụng</div>
      </div>
    </div>
  )
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<{ user: string | null; auth: boolean }>("/me") })
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-16 items-center px-4">
        <Brand />
      </div>
      <nav aria-label="Điều hướng chính" className="flex-1 space-y-5 overflow-y-auto px-3 py-3">
        {GROUPS.map((group) => (
          <div key={group.title}>
            <div className="mb-1.5 px-2.5 text-[0.7rem] font-medium text-muted-foreground/80">{group.title}</div>
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    cn(
                      "group relative flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm text-sidebar-foreground transition-colors hover:bg-sidebar-accent/70 hover:text-sidebar-accent-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                      isActive &&
                        "bg-sidebar-accent font-medium text-sidebar-accent-foreground shadow-[0_1px_2px_0_var(--shadow-color)] ring-1 ring-sidebar-border",
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <item.icon className={cn("size-4 stroke-[1.75]", isActive ? "text-brand" : "text-muted-foreground")} />
                      {item.label}
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
        <div className="border-t border-sidebar-border pt-3">
          <a
            href="/api/docs"
            target="_blank"
            rel="noopener"
            className="flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-sm text-sidebar-foreground transition-colors hover:bg-sidebar-accent/70 hover:text-sidebar-accent-foreground"
          >
            <BookOpen className="size-4 stroke-[1.75] text-muted-foreground" />
            Tài liệu API
          </a>
        </div>
      </nav>
      <div className="flex items-center justify-between gap-2 border-t border-sidebar-border px-3 py-2.5">
        <span className="flex min-w-0 items-center gap-2 px-1 text-xs text-muted-foreground">
          <UserRound className="size-3.5 shrink-0" />
          <span className="truncate">{me.data?.user ? me.data.user : "Không đăng nhập"}</span>
        </span>
        <ThemeMenu />
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
    <div className="min-h-svh lg:grid lg:grid-cols-[248px_1fr]">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Bỏ qua tới nội dung
      </a>
      <aside className="sticky top-0 hidden h-svh border-r border-sidebar-border bg-sidebar lg:block">
        <Sidebar />
      </aside>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-[2px]" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-72 border-r border-sidebar-border bg-sidebar shadow-xl">
            <Button
              variant="ghost"
              size="icon-sm"
              className="absolute top-4 right-3"
              onClick={() => setOpen(false)}
              aria-label="Đóng menu"
            >
              <X />
            </Button>
            <Sidebar onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-background/85 px-4 backdrop-blur lg:hidden">
          <Button variant="ghost" size="icon-sm" onClick={() => setOpen(true)} aria-label="Mở menu" aria-expanded={open}>
            <Menu />
          </Button>
          <Brand />
        </header>
        <main id="main" className="mx-auto w-full max-w-[1440px] px-4 pt-7 pb-14 sm:px-6 lg:px-10">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
