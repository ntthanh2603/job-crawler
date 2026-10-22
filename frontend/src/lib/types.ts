// Kiểu dữ liệu của API: alias tới kiểu orval sinh từ spec của backend (src/lib/gen/queries.ts ←
// backend/openapi.json ← mô hình Pydantic trong backend/jobcrawler/modules/*/*_schema.py;
// `npm run gen:api` sinh lại). Chỉ viết tay phần spec không tả được (JSON tự do lưu trong DB).
import type * as S from "@/lib/gen/queries"

/** Một trang danh sách (như `GetManyBaseResponseDto` của open-asm). `exact = false`: có hơn
 * `total` dòng (server đếm tối đa 10.000). */
export type Page<T> = Omit<S.PageOutJobItem, "data"> & { data: T[] }

export type SortOrder = S.SortOrder

/** Cột + chiều sắp xếp của một danh sách (`sortBy` / `sortOrder` của API). */
export type SortState<C extends string = string> = { sortBy: C; sortOrder: SortOrder }

/** Tham số chung của các endpoint danh sách (`GetManyQuery` ở backend/common/pagination.py). */
export type ListParams<C extends string = string> = SortState<C> & { page: number; limit: number; search: string }

export type Location = S.Location

export type Salary = S.Salary

export type Job = S.JobItem

export type Posting = S.PostingOut

export type Review = S.ReviewOut

export type JobDetail = S.JobDetailOut

export type Company = S.CompanyEntity

export type CompanyItem = S.CompanyItem

/** Loại lượt chạy: build = lượt đủ (ghi bộ khung), poll = chỉ các trang trong bộ khung. */
export type RunKind = S.RunKind

/** Vì sao lượt chạy được xếp. */
export type RunTrigger = S.RunTrigger

export type Run = S.RunOut

export type FailedPageItem = S.FailedPageItem

/** `companies.kyc_evidence` là JSON tự do (spec: `unknown`): các trường giao diện đọc. */
export type KycEvidence = {
  reason?: string
  signals?: string[]
  evidence?: { url: string; kind: string; snippet: string }[]
}

export type CompanyDetail = Omit<S.CompanyDetailOut, "kyc_evidence"> & { kyc_evidence: KycEvidence | null }

export type RunDetail = S.RunDetailOut

export type Fetch = S.FetchOut

export type LogLine = S.LogOut

export type FailedPage = S.FailedPageOut

export type QueueStats = S.QueueStatsOut

export type RunStats = S.RunStatsOut

export type BoardStats = S.BoardStatsOut

export type StorageStats = S.StorageStatsOut

export type Stats = S.StatsOut

export type Pair = [string, number]

/** Phân bố trên job đang mở / doanh nghiệp — tính sẵn bằng cron (modules/overview/overview_service.py). */
export type Charts = S.ChartsOut

export type Overview = S.OverviewOut

/** Lời gọi LLM trên trang tổng quan (backend/jobcrawler/services/llm_metrics.py). */
export type LlmStats = S.LLMStatsOut

export type Meta = S.MetaOut

export type ImportOutcome = S.ImportOutcomeOut

export type EnqueueResult = S.EnqueueResultOut

export type ReviewPage = S.ReviewPageOut

export type EvalReport = S.EvalReportOut

/** `mode`: full = index đủ (đêm), tail = index đuôi (ngày, chỉ phần mới nhất của danh sách). */
export type BoardRun = S.BoardRunOut

export type Boards = S.BoardsOut

/** `scheduled`: hẹn giờ chạy, giây epoch (0 = chạy ngay); `trigger`: null với việc định kỳ, index
 * trang tuyển dụng; `continuation`: lượt tự chạy tiếp thứ mấy sau lượt `partial` (0 = lượt thường). */
export type QueueJob = S.QueueJobOut

export type QueuePage = S.QueuePageOut

// --- trang Cài đặt ---------------------------------------------------------------------------------

export type SettingItem = S.SettingItemOut

export type SettingKind = SettingItem["kind"]

/** Giá trị đang dùng lấy từ đâu: mặc định của code, biến môi trường / .env, hay đặt ở giao diện. */
export type SettingSource = SettingItem["source"]

/** Bí mật luôn là null (xem `is_set`); proxy đã ẩn mật khẩu. */
export type SettingValue = S.SettingValue

export type SettingOption = S.SettingOptionOut

/** Mục con trong nhóm: tiêu đề, giải thích ngắn, các cài đặt theo thứ tự hiển thị. */
export type SettingSection = S.SettingSectionOut

export type SettingGroup = S.SettingGroupOut

export type SettingsPage = S.SettingsPageOut

export type SettingsSaved = S.SettingsSavedOut

/** Proxy chung kèm trạng thái xoay vòng (`line` = dòng đã che mật khẩu của cài đặt `proxies`). */
export type ProxyStatus = S.ProxyStatusOut

export type ProxyCheck = S.ProxyCheckOut

// --- trang Workers -----------------------------------------------------------------------------------

/** `render_disabled_reason`: render tạm tắt vì lỗi Chromium (rỗng = render được), thử lại lúc
 * `render_retry_at_ms` (0 = tắt hẳn, máy không có Chromium). */
export type WorkerStats = S.WorkerStats

/** `dropped`: kết quả gateway bỏ vì worker không còn giữ việc; `lost`: việc gateway trả lại backend
 * để giao lại vì worker rời / quá hạn báo sống. */
export type WorkerStatus = S.WorkerStatus

export type WorkerPoolSummary = S.WorkerPoolOut

/** `now_ms`: giờ máy chủ (ms epoch) lúc trả lời, để tính "lần cuối báo" không lệch đồng hồ. */
export type WorkersPage = S.WorkersPageOut

// --- lịch thu thập: bộ khung, poll, độ trễ phát hiện --------------------------------------------------

/** Trạng thái lịch của một doanh nghiệp (`company_schedules.state`). */
export type ScheduleState = S.ScheduleState

export type CrawlSchedule = S.ScheduleOut

/** Bộ khung của một nguồn: trang danh sách / API đọc ở mỗi lượt poll. */
export type CrawlPlan = S.PlanOut

export type CompanyCrawl = S.CompanyCrawlOut

/** Giới hạn của lượt poll (cài đặt) mà sơ đồ bộ khung nhắc tới. */
export type PollLimits = S.PollLimitsOut

/** Toàn bộ một bộ khung (`GET /api/crawl/plans/{id}`): `spec` là JSON gốc (`crawl_plans.spec`). */
export type PlanDetail = S.PlanDetailOut

/** `polls.window_histogram`: số lượt theo ngưỡng cửa sổ (giây) 30, 60, 90, 120, 135, 150, 165, 180,
 * 210, 240, 300, 600, 1800, 3600, +∞. */
export type CrawlStatus = S.CrawlStatusOut

// --- nhật ký thao tác (`GET /api/audit`) --------------------------------------------------------

/** Giá trị trước / sau của một trường (bí mật đã che phía máy chủ). */
export type AuditChange = { before: unknown; after: unknown }

/** `actor`: tên đăng nhập, `system` (CLI, cron) hoặc `anonymous`; `action`: tên có chấm, ổn định
 * (`settings.update`, `company.build`...). `changes` là JSON tự do: dạng ghi ở modules/audit/audit_service.py. */
export type AuditEvent = Omit<S.AuditEventOut, "changes"> & { changes: Record<string, AuditChange> | null }

/** Một trang nhật ký; `next` = id truyền vào `before` để lấy trang sau, `null` khi đã hết. */
export type AuditPage = Omit<S.AuditPageOut, "items"> & { items: AuditEvent[] }
