import { useQuery } from "@tanstack/react-query"
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
  StatusBadge,
  TextLink,
  useRowLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { qs } from "@/lib/api"
import { cap, date, displayName, num, provinces, salary, sourceLabel } from "@/lib/format"
import { jobsQuery, metaQuery } from "@/lib/queries"
import { useUrlFilters } from "@/lib/url-filters"
import { cn } from "@/lib/utils"

/** Bộ lọc trên URL và giá trị mặc định (không ghi lên URL). */
const FILTERS = {
  q: "",
  province: "",
  level: "",
  employment_type: "",
  work_mode: "",
  source: "",
  status: "active",
  salary_from: "",
  company_id: "",
  reviewed: "",
  sort: "new",
}

const STATUSES = [
  ["active", "Đang mở"],
  ["expired", "Hết hạn"],
  ["removed", "Đã gỡ"],
  ["all", "Mọi trạng thái"],
] as const

const SORTS = [
  ["new", "Mới thấy nhất"],
  ["posted", "Ngày đăng"],
  ["salary", "Lương cao nhất"],
  ["expires", "Sắp hết hạn"],
] as const

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
  const [search, setSearch] = useState(filters.q)
  const [salaryFrom, setSalaryFrom] = useState(filters.salary_from)
  useEffect(() => setSearch(filters.q), [filters.q])
  useEffect(() => setSalaryFrom(filters.salary_from), [filters.salary_from])
  const rowLink = useRowLink()

  const meta = useQuery(metaQuery())
  const query = useQuery({ ...jobsQuery(filters, page), placeholderData: (prev) => prev })

  const exportUrl = (fmt: string) => `/api/jobs/export${qs({ ...filters, fmt })}`
  const items = query.data?.items ?? []
  const filtered = isFiltered("sort")

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
              onSubmit={(v) => update({ q: (v ?? search).trim(), salary_from: salaryFrom })}
              placeholder="Tìm vị trí hoặc doanh nghiệp"
            />
          }
          end={
            <FilterSelect label="Sắp xếp" value={filters.sort} onChange={(v) => update({ sort: v })} options={SORTS} all={null} />
          }
        >
          <FilterSelect label="Trạng thái" value={filters.status} onChange={(v) => update({ status: v })} options={STATUSES} all={null} />
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
          <FilterSelect label="Cấp bậc" value={filters.level} onChange={(v) => update({ level: v })} options={meta.data?.levels ?? []} />
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
                    <TableHead>Vị trí</TableHead>
                    <TableHead>Nơi làm</TableHead>
                    <TableHead>Lương</TableHead>
                    <TableHead>Nguồn</TableHead>
                    <TableHead>Hạn nộp</TableHead>
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
        {query.data && <Pager {...query.data} onPage={(p) => update({ page: p })} />}
      </Section>
    </>
  )
}
