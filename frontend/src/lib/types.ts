// Kiểu dữ liệu khớp API FastAPI (src/jobcrawler/web/api.py).

/** Một trang danh sách. `exact = false`: có hơn `total` dòng (server đếm tối đa 10.000). */
export type Page<T> = { items: T[]; total: number; page: number; pages: number; size: number; exact: boolean }

export type Location = { province: string | null; address_raw: string | null }

export type Salary = {
  min: number | null
  max: number | null
  currency: string | null
  period: "month" | "year" | "hour" | null
  negotiable: boolean
  raw: string | null
}

export type Job = {
  id: string
  reviewed: boolean | null
  title: string
  title_normalized: string
  company_id: string
  company_name: string
  level: string
  employment_type: string
  work_mode: string
  locations: Location[]
  salary: Salary | null
  experience_years: { min: number | null; max: number | null } | null
  description_md: string
  requirements_md: string | null
  benefits_md: string | null
  skills: string[]
  posted_at: string | null
  expires_at: string | null
  apply_url: string | null
  source_urls: string[]
  canonical_source: string
  status: string
  confidence: number
  verification_reasons: string[]
  first_seen_at: string
  last_seen_at: string
}

export type Posting = {
  id: string
  source: string
  url: string
  title: string | null
  status: string
  accepted: boolean | null
  confidence: number | null
  verification_reasons: string[]
  last_seen_at: string
  verified_at: string | null
}

export type Review = {
  correct: boolean
  note: string | null
  reviewer: string | null
  reviewed_at: string
}

export type JobDetail = {
  job: Job
  html: { description: string; requirements: string; benefits: string }
  postings: Posting[]
  review: Review | null
}

export type Company = {
  id: string
  input_name: string
  legal_name: string | null
  brand_name: string | null
  tax_code: string | null
  primary_domain: string | null
  other_domains: string[]
  address: string | null
  kyc_status: string | null
  kyc_confidence: number | null
  kyc_evidence: unknown
  kyc_at: string | null
}

export type CompanyItem = {
  company: Company
  input_website: string | null
  last_status: string | null
  last_started: string | null
  last_duration_ms: number | null
  last_incomplete: string[]
  active_jobs: number
}

export type Run = {
  run_id: string
  company_id: string
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  status: string
  jobs_found: number
  jobs_accepted: number
  incomplete_sources: string[]
  error: string | null
}

export type FailedPageItem = { id: string; url: string; source: string; reason: string; created_at: string }

export type CompanyDetail = {
  company: Company
  input_website: string | null
  note: string | null
  created_at: string
  kyc_evidence: KycEvidence | null
  aliases: { alias: string; kind: string | null }[]
  profiles: {
    source: string
    url: string
    name_on_source: string
    website: string | null
    tax_code: string | null
    match_confidence: number | null
    match_signal: string | null
    indexed_at: string | null
  }[]
  // Số đếm cho các tab; danh sách lấy theo trang ở /companies/{id}/jobs|runs|rejected.
  active_jobs: number
  inactive_jobs: number
  runs_total: number
  rejected_total: number
  failed_pages: FailedPageItem[]
}

export type KycEvidence = {
  reason?: string
  signals?: string[]
  evidence?: { url: string; kind: string; snippet: string }[]
}

export type RunDetail = {
  run: Run
  company_name: string | null
  by_source: { source: string; total: number; outcomes: Record<string, number> }[]
  fetches_total: number
  logs_total: number
  failed_pages: FailedPageItem[]
}

export type Fetch = {
  source: string
  url: string
  http_status: number | null
  outcome: string
  duration_ms: number | null
  items: number | null
  error: string | null
}

export type LogLine = { ts: string; level: string; event: string; context: Record<string, unknown> }

export type FailedPage = FailedPageItem & { http_status: number | null; run_id: string | null; html: string }

export type QueueStats = {
  name: string
  queued: number
  active: number
  scheduled: number
  workers: number
  complete: number
  failed: number
  retried: number
  aborted: number
  error: string | null
}

export type RunStats = {
  since: string | null
  runs: number
  companies: number
  by_status: Record<string, number>
  latest_by_status: Record<string, number>
  failed_rate: number | null
  p50_seconds: number | null
  p95_seconds: number | null
  max_seconds: number | null
  over_timeout: number
  jobs_found: number
  jobs_accepted: number
  companies_with_jobs: number
  per_hour: number | null
  incomplete_sources: [string, number][]
  errors: [string, number][]
}

export type BoardStats = {
  source: string
  active_postings: number
  postings: number
  profiles: number
  profiles_mapped: number
  last_status: string | null
  last_started: string | null
  last_finished: string | null
  last_complete: boolean | null
  last_urls_seen: number | null
  last_fetched: number | null
  last_new: number | null
  last_removed: number | null
}

export type StorageStats = {
  kind: string
  table: string
  values: number
  stored_bytes: number
  original_bytes: number
  by_dict: Record<string, number> | null
}

export type Stats = {
  generated_at: string
  queues: QueueStats[]
  companies: number
  kyc_status: Record<string, number>
  jobs_by_status: Record<string, number>
  jobs_by_source: [string, number][]
  runs: RunStats
  sources: { kind: string; fetches: number; outcomes: Record<string, number>; avg_ms: number | null }[]
  boards: BoardStats[]
  storage: StorageStats[]
  tables: [string, number][]
  targets: { name: string; value: number | null; target: string; ok: boolean | null; kind: "seconds" | "rate" }[]
}

export type Pair = [string, number]

/** Phân bố trên job đang mở / doanh nghiệp — tính sẵn bằng cron (services/overview.py). */
export type Charts = {
  generated_at: string
  totals: {
    active_jobs: number
    companies: number
    companies_with_jobs: number
    new_jobs_7d: number
    with_salary: number
  }
  jobs_by_day: { day: string; careers: number; boards: number }[]
  jobs_by_source: Pair[]
  jobs_by_province: Pair[]
  jobs_by_level: Pair[]
  jobs_by_employment: Pair[]
  jobs_by_work_mode: Pair[]
  salary_bands: Pair[]
  kyc: Pair[]
  top_companies: { id: string; name: string; active_jobs: number }[]
}

export type Overview = {
  stats: Stats
  chart: { hour: string; counts: Record<string, number>; total: number }[]
  fetch_chart: { hour: string; ok: number; blocked: number; error: number }[]
  charts: Charts
  recent_runs: (Run & { company_name: string })[]
}

export type Meta = {
  provinces: string[]
  levels: string[]
  employment_types: string[]
  work_modes: string[]
  sources: string[]
  kyc_statuses: string[]
  run_statuses: string[]
  boards: string[]
}

export type ImportOutcome = {
  rows: number
  created: number
  updated: number
  unchanged: number
  duplicates: number
  errors: string[]
  warnings: string[]
  enqueued: number | null
  already_queued: number
}

export type EnqueueResult = { selected: number; enqueued: number; already_queued: number }

export type ReviewPage = {
  reviewed: number
  correct: number
  precision: number | null
  target: number
  unreviewed_active: number
  by_source: { source: string; reviewed: number; correct: number; precision: number | null }[]
  next: JobDetail | null
  recent: {
    job_id: string
    title: string
    company_name: string
    correct: boolean
    note: string | null
    reviewer: string | null
    reviewed_at: string
  }[]
}

export type EvalReport = {
  precision: number | null
  recall: number | null
  golden: number
  predicted: number
  matched: number
  target_precision: number
  target_recall: number
  perf: Record<string, string>
  companies: {
    name: string
    company_id: string | null
    golden: number
    predicted: number
    matched: number
    run_status: string | null
    missing: string[]
    extra: string[]
    precision: number | null
    recall: number | null
  }[]
  sources: { source: string; predicted: number; matched: number; golden: number; precision: number | null; recall: number | null }[]
  warnings: string[]
  markdown: string
}

export type BoardRun = {
  id: string
  source: string
  started_at: string
  finished_at: string | null
  status: string
  complete: boolean
  urls_seen: number
  fetched: number
  new: number
  updated: number
  unchanged: number
  failed: number
  removed: number
  profiles_new: number
  profiles_matched: number
  error: string | null
}

export type Boards = { boards: BoardStats[]; queue: QueueStats }

export type QueueJob = {
  key: string
  function: string
  status: string
  attempts: number
  retries: number
  timeout: number
  queued: number
  started: number
  /** Hẹn giờ chạy, giây epoch (0 = chạy ngay). */
  scheduled: number
  error: string | null
  label: string
  company_id: string | null
  /** Lượt tự chạy tiếp thứ mấy sau lượt `partial` (0 = lượt thường). */
  continuation: number
}

export type QueuePage = {
  queues: (QueueStats & { jobs: QueueJob[]; page: number; pages: number; total: number })[]
}
