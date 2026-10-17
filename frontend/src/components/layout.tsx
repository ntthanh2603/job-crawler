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
  Sun,
  Target,
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

const NAV = [
  { to: "/", label: "Tổng quan", icon: LayoutDashboard, end: true },
  { to: "/companies", label: "Doanh nghiệp", icon: Building2 },
  { to: "/jobs", label: "Job", icon: Briefcase },
  { to: "/review", label: "Kiểm tay", icon: ListChecks },
  { to: "/eval", label: "Golden set", icon: Target },
  { to: "/boards", label: "Trang tuyển dụng", icon: Globe },
  { to: "/queue", label: "Queue", icon: Layers },
]

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const icons: Record<Theme, typeof Sun> = { light: Sun, dark: Moon, system: Monitor }
  const Icon = icons[theme]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Đổi giao diện sáng/tối">
          <Icon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={() => setTheme("light")}>
          <Sun /> Sáng
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setTheme("dark")}>
          <Moon /> Tối
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => setTheme("system")}>
          <Monitor /> Theo hệ thống
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<{ user: string | null; auth: boolean }>("/me") })
  return (
    <div className="flex h-full flex-col">
      <div className="flex h-14 items-center gap-2 px-4">
        <div className="grid size-7 place-items-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          j
        </div>
        <span className="font-semibold tracking-tight">jobcrawler</span>
      </div>
      <nav className="flex-1 space-y-0.5 px-2 py-2">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground hover:bg-sidebar-accent",
              )
            }
          >
            <item.icon className="size-4" />
            {item.label}
          </NavLink>
        ))}
        <a
          href="/api/docs"
          target="_blank"
          rel="noopener"
          className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
        >
          <BookOpen className="size-4" />
          API
        </a>
      </nav>
      <div className="flex items-center justify-between gap-2 border-t border-sidebar-border px-4 py-3 text-xs text-muted-foreground">
        <span className="truncate">{me.data?.user ? `Đăng nhập: ${me.data.user}` : ""}</span>
        <ThemeToggle />
      </div>
    </div>
  )
}

export function Layout() {
  const [open, setOpen] = useState(false)
  const location = useLocation()
  useEffect(() => setOpen(false), [location.pathname])

  return (
    <div className="min-h-svh lg:grid lg:grid-cols-[232px_1fr]">
      <aside className="sticky top-0 hidden h-svh border-r border-sidebar-border bg-sidebar lg:block">
        <Sidebar />
      </aside>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} />
          <aside className="absolute inset-y-0 left-0 w-64 border-r border-sidebar-border bg-sidebar shadow-xl">
            <Button variant="ghost" size="icon-sm" className="absolute top-3 right-3" onClick={() => setOpen(false)} aria-label="Đóng menu">
              <X />
            </Button>
            <Sidebar onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b bg-background/80 px-4 backdrop-blur lg:hidden">
          <Button variant="ghost" size="icon-sm" onClick={() => setOpen(true)} aria-label="Mở menu">
            <Menu />
          </Button>
          <span className="font-semibold">jobcrawler</span>
        </header>
        <main className="mx-auto w-full max-w-[1400px] px-4 py-6 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
