import { lazy, Suspense, type ComponentType } from "react"
import { Link, Route, Routes } from "react-router"

import { Empty, PageHeader, PageLoading } from "@/components/common"
import { Layout } from "@/components/layout"
import { Button } from "@/components/ui/button"

// Mỗi trang một chunk (trang tổng quan kéo theo recharts, không cần tải ở trang khác).
function lazyPage<K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K): ComponentType {
  return lazy(() => load().then((m) => ({ default: m[name] as ComponentType })))
}

const pages = {
  overview: lazyPage(() => import("@/pages/overview"), "OverviewPage"),
  companies: lazyPage(() => import("@/pages/companies"), "CompaniesPage"),
  company: lazyPage(() => import("@/pages/company"), "CompanyPage"),
  run: lazyPage(() => import("@/pages/run"), "RunPage"),
  failedPage: lazyPage(() => import("@/pages/failed-page"), "FailedPagePage"),
  jobs: lazyPage(() => import("@/pages/jobs"), "JobsPage"),
  job: lazyPage(() => import("@/pages/job"), "JobPage"),
  review: lazyPage(() => import("@/pages/review"), "ReviewPage"),
  eval: lazyPage(() => import("@/pages/eval"), "EvalPage"),
  boards: lazyPage(() => import("@/pages/boards"), "BoardsPage"),
  queue: lazyPage(() => import("@/pages/queue"), "QueuePage"),
  workers: lazyPage(() => import("@/pages/workers"), "WorkersPage"),
  settings: lazyPage(() => import("@/pages/settings"), "SettingsPage"),
}

function page(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoading />}>
      <Component />
    </Suspense>
  )
}

function NotFound() {
  return (
    <>
      <PageHeader title="Không tìm thấy trang" />
      <Empty
        title="Đường dẫn này không tồn tại hoặc đã bị đổi"
        action={
          <Button asChild size="sm">
            <Link to="/">Về trang Tổng quan</Link>
          </Button>
        }
      />
    </>
  )
}

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={page(pages.overview)} />
        <Route path="companies" element={page(pages.companies)} />
        <Route path="companies/:id" element={page(pages.company)} />
        <Route path="runs/:id" element={page(pages.run)} />
        <Route path="failed-pages/:id" element={page(pages.failedPage)} />
        <Route path="jobs" element={page(pages.jobs)} />
        <Route path="jobs/:id" element={page(pages.job)} />
        <Route path="review" element={page(pages.review)} />
        <Route path="eval" element={page(pages.eval)} />
        <Route path="boards" element={page(pages.boards)} />
        <Route path="queue" element={page(pages.queue)} />
        <Route path="workers" element={page(pages.workers)} />
        <Route path="settings" element={page(pages.settings)} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}
