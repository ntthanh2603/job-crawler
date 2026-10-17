import { useQuery } from "@tanstack/react-query"
import { Check, Download, Search, X } from "lucide-react"
import { useEffect, useState, type FormEvent } from "react"
import { Link, useSearchParams } from "react-router"

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
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { date, num, pct, provinces, salary, sourceKind } from "@/lib/format"
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
  ["active", "đang mở"],
  ["expired", "hết hạn"],
  ["removed", "đã gỡ"],
  ["all", "mọi trạng thái"],
] as const

const SORTS = [
  ["new", "mới thấy"],
  ["posted", "ngày đăng"],
  ["salary", "lương cao"],
  ["expires", "sắp hết hạn"],
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

  const exportQuery = (fmt: string) => `/api/jobs/export${qs({ ...filters, fmt })}`
  const items = query.data?.items ?? []

  return (
    <>
      <PageHeader
        title="Job"
        description={query.data ? `${num(query.data.total)} job khớp bộ lọc` : "Đang tải…"}
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <a href={exportQuery("csv")}>
                <Download /> CSV
              </a>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a href={exportQuery("jsonl")}>
                <Download /> JSONL
              </a>
            </Button>
          </>
        }
      />

      <Section className="mb-4" bodyClassName="flex flex-wrap items-end gap-3">
        <form
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            update({ q: search.trim(), salary_from: salaryFrom })
          }}
          className="flex items-end gap-3"
        >
          <Field label="Tìm">
            <div className="relative">
              <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="vị trí hoặc doanh nghiệp"
                className="h-8 w-60 pl-8"
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
              className="h-8 w-24"
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
          <FilterSelect value={filters.source} onChange={(v) => update({ source: v })} options={meta.data?.sources ?? []} />
        </Field>
        <Field label="Kiểm tay">
          <FilterSelect value={filters.reviewed} onChange={(v) => update({ reviewed: v })} options={["no", "yes", "correct", "wrong"]} />
        </Field>
        <Field label="Trạng thái">
          <Select value={filters.status} onValueChange={(v) => update({ status: v })}>
            <SelectTrigger size="sm" className="w-36">
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
            <SelectTrigger size="sm" className="w-36">
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
            Xoá lọc
          </Button>
        )}
      </Section>

      {filters.company_id && (
        <p className="mb-3 text-xs text-muted-foreground">
          Đang lọc theo doanh nghiệp #{filters.company_id} ·{" "}
          <Link to={`/companies/${filters.company_id}`} className="text-primary hover:underline">
            xem doanh nghiệp
          </Link>
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
                  <TableHead>Doanh nghiệp</TableHead>
                  <TableHead>Nơi làm</TableHead>
                  <TableHead>Lương</TableHead>
                  <TableHead>Nguồn</TableHead>
                  <TableHead>Đăng</TableHead>
                  <TableHead>Hạn nộp</TableHead>
                  <TableHead className="pr-4 text-right">Tin cậy</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((j) => (
                  <TableRow key={j.id}>
                    <TableCell className="max-w-96 min-w-64 pl-4 whitespace-normal">
                      <div className="flex items-start gap-1.5">
                        <Link to={`/jobs/${j.id}`} className="font-medium hover:underline">
                          {j.title}
                        </Link>
                        {j.reviewed === true && <Check className="mt-0.5 size-4 shrink-0 text-success" aria-label="đã kiểm: đúng" />}
                        {j.reviewed === false && <X className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="đã kiểm: sai" />}
                        {j.status !== "active" && <StatusBadge value={j.status} />}
                      </div>
                      <div className="mt-1">
                        <JobTags job={j} />
                      </div>
                    </TableCell>
                    <TableCell className="max-w-56 text-xs whitespace-normal">
                      <Link to={`/companies/${j.company_id}`} className="hover:underline">
                        {j.company_name}
                      </Link>
                    </TableCell>
                    <TableCell className="max-w-40 text-xs whitespace-normal">{provinces(j)}</TableCell>
                    <TableCell className="text-xs whitespace-nowrap">{salary(j.salary)}</TableCell>
                    <TableCell className="text-xs">
                      {sourceKind(j.canonical_source)}
                      {j.source_urls.length > 1 && <span className="text-muted-foreground"> +{j.source_urls.length - 1}</span>}
                    </TableCell>
                    <TableCell className="text-xs whitespace-nowrap">{date(j.posted_at)}</TableCell>
                    <TableCell className="text-xs whitespace-nowrap">{date(j.expires_at)}</TableCell>
                    <TableCell className="pr-4 text-right text-xs tabular-nums">{pct(j.confidence, 0)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty>Không có job nào khớp bộ lọc.</Empty>
          ))}
        {query.data && (
          <Pager page={query.data.page} pages={query.data.pages} total={query.data.total} onPage={(p) => update({ page: p })} />
        )}
      </Section>
      <p className="mt-3 text-xs text-muted-foreground">
        Tìm theo tiêu đề và tên doanh nghiệp (mô tả lưu nén nên không tìm trong mô tả).
      </p>
    </>
  )
}
