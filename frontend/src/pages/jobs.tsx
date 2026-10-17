import { useQuery } from "@tanstack/react-query"
import { Check, Download, Search, X } from "lucide-react"
import { useEffect, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router"

import {
  Empty,
  ErrorState,
  Field,
  FilterSelect,
  JobTags,
  Loading,
  PageHeader,
  Pager,
  Section,
  StatusBadge,
  TextLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { cap, date, num, provinces, salary, sourceLabel } from "@/lib/format"
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

      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-xl border bg-card/60 p-3">
        <form
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            update({ q: search.trim(), salary_from: salaryFrom })
          }}
          className="flex flex-wrap items-end gap-3"
        >
          <Field label="Tìm kiếm">
            <div className="relative">
              <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Vị trí hoặc doanh nghiệp"
                className="h-8 w-64 bg-card pl-8"
              />
            </div>
          </Field>
          <Field label="Lương từ (triệu)">
            <Input
              type="number"
              min={0}
              value={salaryFrom}
              onChange={(e) => setSalaryFrom(e.target.value)}
              onBlur={() => salaryFrom !== filters.salary_from && update({ salary_from: salaryFrom })}
              className="h-8 w-28 bg-card"
              placeholder="Ví dụ 15"
            />
          </Field>
          <button type="submit" hidden />
        </form>
        <Field label="Tỉnh/thành">
          <FilterSelect value={filters.province} onChange={(v) => update({ province: v })} options={meta.data?.provinces ?? []} />
        </Field>
        <Field label="Cấp bậc">
          <FilterSelect value={filters.level} onChange={(v) => update({ level: v })} options={meta.data?.levels ?? []} />
        </Field>
        <Field label="Loại hình">
          <FilterSelect
            value={filters.employment_type}
            onChange={(v) => update({ employment_type: v })}
            options={meta.data?.employment_types ?? []}
          />
        </Field>
        <Field label="Hình thức">
          <FilterSelect value={filters.work_mode} onChange={(v) => update({ work_mode: v })} options={meta.data?.work_modes ?? []} />
        </Field>
        <Field label="Nguồn">
          <FilterSelect
            value={filters.source}
            onChange={(v) => update({ source: v })}
            options={meta.data?.sources ?? []}
            labels={sourceLabel}
          />
        </Field>
        <Field label="Kiểm tay">
          <FilterSelect value={filters.reviewed} onChange={(v) => update({ reviewed: v })} options={["no", "yes", "correct", "wrong"]} />
        </Field>
        <Field label="Trạng thái">
          <Select value={filters.status} onValueChange={(v) => update({ status: v })}>
            <SelectTrigger size="sm" className="w-36 bg-card">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUSES.map(([v, text]) => (
                <SelectItem key={v} value={v}>
                  {text}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Sắp xếp">
          <Select value={filters.sort} onValueChange={(v) => update({ sort: v })}>
            <SelectTrigger size="sm" className="w-40 bg-card">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SORTS.map(([v, text]) => (
                <SelectItem key={v} value={v}>
                  {text}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        {params.toString() && (
          <Button variant="ghost" size="sm" onClick={() => setParams({})}>
            Xoá bộ lọc
          </Button>
        )}
      </div>

      {filters.company_id && (
        <p className="mb-3 text-xs text-muted-foreground">
          Đang lọc theo doanh nghiệp #{filters.company_id} ·{" "}
          <TextLink to={`/companies/${filters.company_id}`} className="text-brand">
            Xem doanh nghiệp
          </TextLink>
        </p>
      )}

      <Section bodyClassName="p-0">
        {query.isError && (
          <div className="p-4">
            <ErrorState error={query.error} />
          </div>
        )}
        {query.isLoading && (
          <div className="p-4">
            <Loading />
          </div>
        )}
        {query.data &&
          (items.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Vị trí</TableHead>
                  <TableHead>Nơi làm</TableHead>
                  <TableHead>Lương</TableHead>
                  <TableHead>Nguồn</TableHead>
                  <TableHead className="pr-4">Hạn nộp</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((j) => (
                  <TableRow key={j.id}>
                    <TableCell className="max-w-[28rem] min-w-56 pl-4 whitespace-normal">
                      <div className="flex items-start gap-1.5">
                        <TextLink to={`/jobs/${j.id}`} className="font-medium">
                          {cap(j.title)}
                        </TextLink>
                        {j.reviewed === true && <Check className="mt-0.5 size-4 shrink-0 text-success" aria-label="Đã kiểm: đúng" />}
                        {j.reviewed === false && <X className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="Đã kiểm: sai" />}
                      </div>
                      <TextLink to={`/companies/${j.company_id}`} className="mt-0.5 block text-xs text-muted-foreground">
                        {cap(j.company_name)}
                      </TextLink>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1">
                        {j.status !== "active" && <StatusBadge value={j.status} />}
                        <JobTags job={j} />
                      </div>
                    </TableCell>
                    <TableCell className="max-w-44 min-w-28 text-xs whitespace-normal">{provinces(j)}</TableCell>
                    <TableCell className="text-xs whitespace-nowrap">{salary(j.salary)}</TableCell>
                    <TableCell className="text-xs">
                      {sourceLabel(j.canonical_source)}
                      {j.source_urls.length > 1 && <span className="text-muted-foreground"> +{j.source_urls.length - 1}</span>}
                    </TableCell>
                    <TableCell className="pr-4 text-xs whitespace-nowrap tabular-nums">
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
          <Pager page={query.data.page} pages={query.data.pages} total={query.data.total} onPage={(p) => update({ page: p })} />
        )}
      </Section>
      <p className="mt-3 text-xs text-muted-foreground">
        Tìm theo tiêu đề và tên doanh nghiệp. Mô tả được lưu nén nên không tìm trong mô tả.
      </p>
    </>
  )
}
