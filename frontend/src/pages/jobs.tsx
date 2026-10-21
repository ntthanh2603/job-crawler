import { Briefcase, Check, ChevronDown, Download, X } from "lucide-react"
import { useEffect, useState } from "react"

import {
  Empty,
  ErrorState,
  FilterBar,
  FilterSelect,
  JobTags,
  Loading,
  NumberChip,
  PageHeader,
  Pager,
  SearchBox,
  Section,
  SortHead,
  SortSelect,
  StatusBadge,
  TextLink,
  nextSort,
  pageLimit,
  useRowLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { qs } from "@/lib/api"
import { cap, date, displayName, education, num, provinces, salary, sourceLabel } from "@/lib/format"
import { useGetJobs, useGetMeta, type GetJobsSortBy } from "@/lib/gen/queries"
import type { SortOrder, SortState } from "@/lib/types"
import { useUrlFilters } from "@/lib/url-filters"
import { cn } from "@/lib/utils"

/** Bộ lọc trên URL và giá trị mặc định (không ghi lên URL). */
const FILTERS = {
  search: "",
  category: "",
  province: "",
  experience: "",
  level: "",
  education: "",
  employment_type: "",
  work_mode: "",
  source: "",
  status: "active",
  salary_from: "",
  company_id: "",
  reviewed: "",
  pageSize: "50",
  sortBy: "first_seen_at",
  sortOrder: "DESC",
}

const STATUSES = [
  ["active", "Đang mở"],
  ["expired", "Hết hạn"],
  ["removed", "Đã gỡ"],
  ["all", "Mọi trạng thái"],
] as const

/** Kinh nghiệm tối thiểu tin yêu cầu (`experience` của API: số năm, 3 = 3–4 năm). */
const EXPERIENCES = [
  ["0", "Dưới 1 năm"],
  ["1", "1 năm"],
  ["2", "2 năm"],
  ["3", "3–4 năm"],
  ["5", "Từ 5 năm"],
] as const

const SORTS: readonly (readonly [GetJobsSortBy, SortOrder, string])[] = [
  ["first_seen_at", "DESC", "Mới thấy nhất"],
  ["first_seen_at", "ASC", "Thấy lâu nhất"],
  ["posted_at", "DESC", "Mới đăng nhất"],
  ["posted_at", "ASC", "Đăng lâu nhất"],
  ["salary", "DESC", "Lương cao nhất"],
  ["salary", "ASC", "Lương thấp nhất"],
  ["expires_at", "ASC", "Sắp hết hạn"],
  ["expires_at", "DESC", "Hạn xa nhất"],
]

/** Ngành chính, các ngành khác gom thành "+N" (rê chuột xem đủ). */
function Categories({ values }: { values: string[] }) {
  if (!values.length) return <Muted value="–" />
  return (
    <span title={values.length > 1 ? values.join(" · ") : undefined}>
      {values[0]}
      {values.length > 1 && <span className="text-muted-foreground"> +{values.length - 1}</span>}
    </span>
  )
}

/** Giá trị trống: gạch mờ thay vì "–" đậm lặp lại cả cột. */
function Muted({ value }: { value: string }) {
  return value === "–" ? <span className="text-muted-foreground/60">–</span> : <>{value}</>
}

function Reviewed({ value }: { value: boolean | null }) {
  if (value === true)
    return (
      <span title="Đã kiểm tay: đúng" className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-success/15 text-success-ink">
        <Check className="size-3" aria-label="Đã kiểm: đúng" />
      </span>
    )
  if (value === false)
    return (
      <span title="Đã kiểm tay: sai" className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-destructive/12 text-danger-ink">
        <X className="size-3" aria-label="Đã kiểm: sai" />
      </span>
    )
  return null
}

export function JobsPage() {
  const { filters, page, update, reset, isFiltered } = useUrlFilters(FILTERS)
  const [search, setSearch] = useState(filters.search)
  const [salaryFrom, setSalaryFrom] = useState(filters.salary_from)
  useEffect(() => setSearch(filters.search), [filters.search])
  useEffect(() => setSalaryFrom(filters.salary_from), [filters.salary_from])
  const rowLink = useRowLink()

  const meta = useGetMeta({ query: { staleTime: 300_000 } })
  const sort: SortState<GetJobsSortBy> = {
    sortBy: filters.sortBy as GetJobsSortBy,
    sortOrder: filters.sortOrder === "ASC" ? "ASC" : "DESC",
  }
  const { pageSize, sortBy: _sortBy, sortOrder: _sortOrder, ...rest } = filters
  // Bộ lọc trên URL là chuỗi; `salary_from` trống hoặc không phải số không âm thì không gửi.
  const minSalary = Number(filters.salary_from)
  const jobFilters = {
    ...rest,
    salary_from: filters.salary_from && Number.isFinite(minSalary) && minSalary >= 0 ? minSalary : undefined,
  }
  const query = useGetJobs(
    { ...jobFilters, ...sort, page, limit: pageLimit(pageSize) },
    { query: { placeholderData: (prev) => prev } },
  )
  const onSort = (column: GetJobsSortBy) => update(nextSort(sort, column))

  // File tải về (CSV / JSONL), không phải JSON: dùng liên kết thường. Xuất mọi dòng theo bộ lọc.
  const exportUrl = (fmt: string) => `/api/jobs/export${qs({ ...jobFilters, fmt })}`
  const items = query.data?.data ?? []
  const filtered = isFiltered("pageSize", "sortBy", "sortOrder")

  return (
    <>
      <PageHeader
        title="Việc làm"
        description={
          query.data
            ? `${num(query.data.total)}${query.data.exact ? "" : "+"} việc làm${filtered ? " khớp bộ lọc" : " đang mở"}`
            : "Đang tải danh sách…"
        }
        actions={
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">
                <Download /> Xuất dữ liệu <ChevronDown className="opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-60">
              <DropdownMenuItem asChild>
                <a href={exportUrl("csv")}>
                  <Download /> CSV <span className="ml-auto text-meta text-muted-foreground">mở bằng Excel</span>
                </a>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <a href={exportUrl("jsonl")}>
                  <Download /> JSONL <span className="ml-auto text-meta text-muted-foreground">đủ mọi trường</span>
                </a>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        }
      />

      <Section bodyClassName="p-0">
        <FilterBar
          search={
            <SearchBox
              value={search}
              onChange={setSearch}
              onSubmit={(v) => update({ search: (v ?? search).trim(), salary_from: salaryFrom })}
              placeholder="Tìm vị trí hoặc doanh nghiệp"
            />
          }
          end={
            <SortSelect sort={sort} onChange={update} options={SORTS} />
          }
        >
          <FilterSelect label="Trạng thái" value={filters.status} onChange={(v) => update({ status: v })} options={STATUSES} all={null} />
          <FilterSelect
            label="Ngành nghề"
            value={filters.category}
            onChange={(v) => update({ category: v })}
            options={meta.data?.categories ?? []}
          />
          <FilterSelect
            label="Tỉnh/thành"
            value={filters.province}
            onChange={(v) => update({ province: v })}
            options={meta.data?.provinces ?? []}
          />
          <NumberChip
            prefix="Lương từ"
            suffix="triệu"
            value={salaryFrom}
            onChange={setSalaryFrom}
            onCommit={() => salaryFrom !== filters.salary_from && update({ salary_from: salaryFrom })}
            placeholder="–"
          />
          <FilterSelect label="Kinh nghiệm" value={filters.experience} onChange={(v) => update({ experience: v })} options={EXPERIENCES} />
          <FilterSelect label="Cấp bậc" value={filters.level} onChange={(v) => update({ level: v })} options={meta.data?.levels ?? []} />
          <FilterSelect
            label="Học vấn"
            value={filters.education}
            onChange={(v) => update({ education: v })}
            options={meta.data?.educations ?? []}
            labels={education}
          />
          <FilterSelect
            label="Loại hình"
            value={filters.employment_type}
            onChange={(v) => update({ employment_type: v })}
            options={meta.data?.employment_types ?? []}
          />
          <FilterSelect
            label="Hình thức"
            value={filters.work_mode}
            onChange={(v) => update({ work_mode: v })}
            options={meta.data?.work_modes ?? []}
          />
          <FilterSelect
            label="Nguồn"
            value={filters.source}
            onChange={(v) => update({ source: v })}
            options={meta.data?.sources ?? []}
            labels={sourceLabel}
          />
          <FilterSelect
            label="Kiểm tay"
            value={filters.reviewed}
            onChange={(v) => update({ reviewed: v })}
            options={["no", "yes", "correct", "wrong"]}
          />
          {filters.company_id && (
            <span className="inline-flex h-9 items-center gap-1 rounded-md border border-primary/50 bg-primary/6 pr-1 pl-3 text-sm dark:bg-primary/12">
              <span className="text-muted-foreground">Doanh nghiệp</span>
              <TextLink to={`/companies/${filters.company_id}`} className="max-w-48 truncate font-medium">
                {items[0]?.company_id === filters.company_id ? displayName(items[0].company_name) : "đã chọn"}
              </TextLink>
              <Button variant="ghost" size="icon-xs" aria-label="Bỏ lọc theo doanh nghiệp" onClick={() => update({ company_id: "" })}>
                <X />
              </Button>
            </span>
          )}
          {filtered && (
            <Button variant="ghost" className="text-muted-foreground" onClick={reset}>
              <X /> Xoá bộ lọc
            </Button>
          )}
        </FilterBar>

        {query.isError && (
          <div className="p-5">
            <ErrorState error={query.error} onRetry={() => query.refetch()} />
          </div>
        )}
        {query.isLoading && (
          <div className="p-5">
            <Loading />
          </div>
        )}
        {query.data &&
          (items.length ? (
            <div className={cn("transition-opacity", query.isPlaceholderData && "opacity-60")}>
              {/* Màn hình hẹp: mỗi việc một thẻ. */}
              <ul className="divide-y md:hidden">
                {items.map((j) => (
                  <li key={j.id} {...rowLink(`/jobs/${j.id}`, "space-y-1.5 px-4 py-3.5")}>
                    <div className="flex items-start gap-1.5">
                      <TextLink to={`/jobs/${j.id}`} className="font-medium">
                        {cap(j.title)}
                      </TextLink>
                      <Reviewed value={j.reviewed} />
                    </div>
                    <div className="text-meta text-muted-foreground">{displayName(j.company_name)}</div>
                    <div className="flex flex-wrap gap-x-3 gap-y-1 text-meta">
                      {j.categories.length > 0 && <Categories values={j.categories} />}
                      {provinces(j) !== "–" && <span>{provinces(j)}</span>}
                      {salary(j.salary) !== "–" && <span className="font-medium">{salary(j.salary)}</span>}
                      <span className="text-muted-foreground">{sourceLabel(j.canonical_source)}</span>
                      {j.expires_at && <span className="text-muted-foreground">Hạn {date(j.expires_at)}</span>}
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      {j.status !== "active" && <StatusBadge value={j.status} />}
                      <JobTags job={j} />
                    </div>
                  </li>
                ))}
              </ul>

              <Table className="max-md:hidden">
                <TableHeader>
                  <TableRow>
                    <SortHead column="first_seen_at" sort={sort} onSort={onSort}>
                      Vị trí
                    </SortHead>
                    <TableHead>Ngành nghề</TableHead>
                    <TableHead>Nơi làm</TableHead>
                    <SortHead column="salary" sort={sort} onSort={onSort}>
                      Lương
                    </SortHead>
                    <TableHead>Nguồn</TableHead>
                    <SortHead column="expires_at" sort={sort} onSort={onSort}>
                      Hạn nộp
                    </SortHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((j) => (
                    <TableRow key={j.id} {...rowLink(`/jobs/${j.id}`)}>
                      <TableCell className="max-w-[32rem] min-w-72 whitespace-normal">
                        <div className="flex items-start gap-1.5">
                          <TextLink to={`/jobs/${j.id}`} className="font-medium">
                            {cap(j.title)}
                          </TextLink>
                          <Reviewed value={j.reviewed} />
                        </div>
                        <TextLink to={`/companies/${j.company_id}`} className="mt-0.5 block text-meta text-muted-foreground">
                          {displayName(j.company_name)}
                        </TextLink>
                        {(j.status !== "active" || j.level !== "unknown" || j.employment_type !== "unknown" || j.work_mode !== "unknown") && (
                          <div className="mt-1.5 flex flex-wrap items-center gap-1">
                            {j.status !== "active" && <StatusBadge value={j.status} />}
                            <JobTags job={j} />
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="max-w-48 min-w-32 text-meta whitespace-normal">
                        <Categories values={j.categories} />
                      </TableCell>
                      <TableCell className="max-w-44 min-w-28 text-meta whitespace-normal">
                        <Muted value={provinces(j)} />
                      </TableCell>
                      <TableCell className="text-meta font-medium whitespace-nowrap">
                        <Muted value={salary(j.salary)} />
                      </TableCell>
                      <TableCell className="text-meta">
                        {sourceLabel(j.canonical_source)}
                        {j.source_urls.length > 1 && (
                          <span className="text-muted-foreground" title={`${j.source_urls.length} nguồn cùng đăng`}>
                            {" "}
                            +{j.source_urls.length - 1}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-meta whitespace-nowrap tabular-nums">
                        <Muted value={date(j.expires_at)} />
                        {j.posted_at && <div className="text-muted-foreground">Đăng {date(j.posted_at)}</div>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <Empty
              icon={<Briefcase />}
              title="Không có việc làm nào khớp bộ lọc"
              action={
                filtered && (
                  <Button variant="outline" size="sm" onClick={reset}>
                    Xoá bộ lọc
                  </Button>
                )
              }
            />
          ))}
        {query.data && (
          <Pager {...query.data} onPage={(p) => update({ page: p })} onLimit={(n) => update({ pageSize: String(n) })} />
        )}
      </Section>
    </>
  )
}
