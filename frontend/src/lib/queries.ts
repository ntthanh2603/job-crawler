// Khoá + hàm tải của các query dùng ở nhiều file (đọc ở trang này, invalidate / ghi đè ở chỗ
// khác): khai báo một lần để hai nơi không lệch khoá. Query chỉ dùng trong một file giữ tại chỗ.
import { queryOptions } from "@tanstack/react-query"

import { api, qs } from "@/lib/api"
import type { CompanyCrawl, Job, JobDetail, Meta, Page, QueuePage, ReviewPage, Run, SettingsPage } from "@/lib/types"

/** Tiền tố khoá: `invalidateQueries({ queryKey: keys.jobs })` làm mới mọi trang danh sách việc làm. */
export const keys = {
  settings: ["settings"] as const,
  meta: ["meta"] as const,
  queueSummary: ["queue-summary"] as const,
  jobs: ["jobs"] as const,
  job: (id: string) => ["job", id] as const,
  review: ["review"] as const,
  companyRuns: (id: string) => ["company-runs", id] as const,
  companyCrawl: (id: string) => ["company-crawl", id] as const,
}

export const settingsQuery = () =>
  queryOptions({ queryKey: keys.settings, queryFn: () => api.get<SettingsPage>("/settings") })

/** Danh mục cho bộ lọc (tỉnh, cấp bậc, nguồn…): ít đổi. */
export const metaQuery = () =>
  queryOptions({ queryKey: keys.meta, queryFn: () => api.get<Meta>("/meta"), staleTime: 300_000 })

/** Ba hàng đợi (build, poll, trang tuyển dụng): số việc và runner, cho thanh bên. */
export const queueSummaryQuery = () =>
  queryOptions({ queryKey: keys.queueSummary, queryFn: () => api.get<QueuePage>("/queue") })

export const jobsQuery = (filters: Record<string, string>, page: number) =>
  queryOptions({
    queryKey: [...keys.jobs, filters, page] as const,
    queryFn: () => api.get<Page<Job>>(`/jobs${qs({ ...filters, page })}`),
  })

export const jobQuery = (id: string) =>
  queryOptions({ queryKey: keys.job(id), queryFn: () => api.get<JobDetail>(`/jobs/${id}`) })

export const reviewQuery = (skip: string | null) =>
  queryOptions({
    queryKey: [...keys.review, skip] as const,
    queryFn: () => api.get<ReviewPage>(`/review${qs({ skip })}`),
  })

/** Một trang lượt chạy của doanh nghiệp; cùng khoá với `PagedList` ở tab "Lượt chạy"
 * (`queryKey={keys.companyRuns(id)}` + số trang) nên trang 1 chỉ tải một lần. */
export const companyRunsQuery = (id: string, page = 1) =>
  queryOptions({
    queryKey: [...keys.companyRuns(id), page] as const,
    queryFn: () => api.get<Page<Run>>(`/companies/${id}/runs?page=${page}`),
  })

/** Bộ khung & lịch thu thập của doanh nghiệp (thẻ số liệu và tab "Bộ khung & lịch"). */
export const companyCrawlQuery = (id: string) =>
  queryOptions({
    queryKey: keys.companyCrawl(id),
    queryFn: () => api.get<CompanyCrawl>(`/companies/${id}/crawl`),
    refetchInterval: 15_000,
  })
