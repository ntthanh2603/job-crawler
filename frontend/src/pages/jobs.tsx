import { useQuery } from "@tanstack/react-query"
import { Check, Download, X } from "lucide-react"
import { useEffect, useState } from "react"
import { useSearchParams } from "react-router"

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
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { cap, date, displayName, num, provinces, salary, sourceLabel } from "@/lib/format"
import type { Job, Meta, Page } from "@/lib/types"

const KEYS = [
  "q",
  "province",
  "level",
  "employment_type",
  "work_mode",
  "source",
  "status",
  "salary_from",
  "company_id",
  "reviewed",
  "sort",
] as const

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

export function JobsPage() {
  const [params, setParams] = useSearchParams()
  const filters: Record<string, string> = Object.fromEntries(KEYS.map((k) => [k, params.get(k) ?? ""]))
  filters.status ||= "active"
  filters.sort ||= "new"
  const page = Number(params.get("page") ?? 1)
  const [search, setSearch] = useState(filters.q)
  const [salaryFrom, setSalaryFrom] = useState(filters.salary_from)
  useEffect(() => setSearch(filters.q), [filters.q])
  useEffect(() => setSalaryFrom(filters.salary_from), [filters.salary_from])

  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api.get<Meta>("/meta"), staleTime: 300_000 })
  const query = useQuery({
    queryKey: ["jobs", filters, page],
    queryFn: () => api.get<Page<Job>>(`/jobs${qs({ ...filters, page })}`),
    placeholderData: (prev) => prev,
  })

  const update = (changes: Record<string, string | number>) => {
    const next: Record<string, string> = { ...filters, page: "1" }
    for (const [k, v] of Object.entries(changes)) next[k] = String(v)
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(next)) {
      if (!v || (k === "status" && v === "active") || (k === "sort" && v === "new") || (k === "page" && v === "1")) continue
      out[k] = v
    }
    setParams(out)
  }

  const exportUrl = (fmt: string) => `/api/jobs/export${qs({ ...filters, fmt })}`
  const items = query.data?.items ?? []

  return (
    <>
      <PageHeader
        title="Việc làm"
        description={query.data ? `${num(query.data.total)} việc làm khớp bộ lọc` : "Đang tải danh sách…"}
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <a href={exportUrl("csv")}>
                <Download /> Tải CSV
              </a>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a href={exportUrl("jsonl")}>
                <Download /> Tải JSONL
              </a>
            </Button>
          </>
        }
      />

      <Section bodyClassName="p-0">
        <FilterBar
          search={
            <SearchBox
              className="sm:w-72"
              value={search}
              onChange={setSearch}
              onSubmit={() => update({ q: search.trim(), salary_from: salaryFrom })}
              placeholder="Tìm vị trí hoặc doanh nghiệp"
            />
          }
        >
          <NumberChip
            prefix="Lương từ"
            suffix="triệu"
            value={salaryFrom}
            onChange={setSalaryFrom}
            onCommit={() => salaryFrom !== filters.salary_from && update({ salary_from: salaryFrom })}
            placeholder="–"
          />
          <FilterSelect
            label="Tỉnh/thành"
            value={filters.province}
            onChange={(v) => update({ province: v })}
            options={meta.data?.provinces ?? []}
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
          <FilterSelect label="Trạng thái" value={filters.status} onChange={(v) => update({ status: v })} options={STATUSES} all={null} />
          <FilterSelect label="Sắp xếp" value={filters.sort} onChange={(v) => update({ sort: v })} options={SORTS} all={null} />
          {filters.company_id && (
            <span className="inline-flex h-8 items-center gap-1 rounded-md border border-primary/40 bg-accent/40 pr-1 pl-2.5 text-sm">
              <TextLink to={`/companies/${filters.company_id}`}>Doanh nghiệp #{filters.company_id}</TextLink>
              <Button variant="ghost" size="icon-xs" aria-label="Bỏ lọc theo doanh nghiệp" onClick={() => update({ company_id: "" })}>
                <X />
              </Button>
            </span>
          )}
          {params.toString() && (
            <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => setParams({})}>
              Xoá bộ lọc
            </Button>
          )}
        </FilterBar>
        {query.isError && (
          <div className="p-5">
            <ErrorState error={query.error} />
          </div>
        )}
        {query.isLoading && (
          <div className="p-5">
            <Loading />
          </div>
        )}
        {query.data &&
          (items.length ? (
            <Table>
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
                  <TableRow key={j.id}>
                    <TableCell className="max-w-[30rem] min-w-64 whitespace-normal">
                      <div className="flex items-start gap-1.5">
                        <TextLink to={`/jobs/${j.id}`} className="font-medium">
                          {cap(j.title)}
                        </TextLink>
                        {j.reviewed === true && <Check className="mt-0.5 size-4 shrink-0 text-success" aria-label="Đã kiểm: đúng" />}
                        {j.reviewed === false && <X className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="Đã kiểm: sai" />}
                      </div>
                      <TextLink to={`/companies/${j.company_id}`} className="mt-0.5 block text-meta text-muted-foreground">
                        {displayName(j.company_name)}
                      </TextLink>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1">
                        {j.status !== "active" && <StatusBadge value={j.status} />}
                        <JobTags job={j} />
                      </div>
                    </TableCell>
                    <TableCell className="max-w-44 min-w-28 text-meta whitespace-normal">{provinces(j)}</TableCell>
                    <TableCell className="text-meta whitespace-nowrap">{salary(j.salary)}</TableCell>
                    <TableCell className="text-meta">
                      {sourceLabel(j.canonical_source)}
                      {j.source_urls.length > 1 && <span className="text-muted-foreground"> +{j.source_urls.length - 1}</span>}
                    </TableCell>
                    <TableCell className="text-meta whitespace-nowrap tabular-nums">
                      <div>{date(j.expires_at)}</div>
                      {j.posted_at && <div className="text-muted-foreground">Đăng {date(j.posted_at)}</div>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Không có việc làm nào khớp bộ lọc.</Empty>
          ))}
        {query.data && (
          <Pager {...query.data} onPage={(p) => update({ page: p })} />
        )}
      </Section>
    </>
  )
}
