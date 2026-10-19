import { lazy, Suspense, type ComponentType } from "react"
import { createBrowserRouter, type RouteObject } from "react-router"

import { PageLoading } from "@/components/common"
import { Layout } from "@/components/layout"
import { NotFound, PageError, RootError } from "@/components/route-error"

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
  audit: lazyPage(() => import("@/pages/audit"), "AuditPage"),
  settings: lazyPage(() => import("@/pages/settings"), "SettingsPage"),
}

function page(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoading />}>
      <Component />
    </Suspense>
  )
}

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    // Khung ứng dụng hỏng: trang lỗi riêng giữa màn hình.
    errorElement: <RootError />,
    children: [
      {
        // Một trang hỏng (kể cả không tải được chunk): lỗi hiện trong khung, thanh bên vẫn dùng được.
        errorElement: <PageError />,
        children: [
          { index: true, element: page(pages.overview) },
          { path: "companies", element: page(pages.companies) },
          { path: "companies/:id", element: page(pages.company) },
          { path: "runs/:id", element: page(pages.run) },
          { path: "failed-pages/:id", element: page(pages.failedPage) },
          { path: "jobs", element: page(pages.jobs) },
          { path: "jobs/:id", element: page(pages.job) },
          { path: "review", element: page(pages.review) },
          { path: "eval", element: page(pages.eval) },
          { path: "boards", element: page(pages.boards) },
          { path: "queue", element: page(pages.queue) },
          { path: "workers", element: page(pages.workers) },
          { path: "audit", element: page(pages.audit) },
          { path: "settings", element: page(pages.settings) },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
]

export const router = createBrowserRouter(routes)
