import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Play, PlayCircle, Search, Upload } from "lucide-react"
import { useEffect, useState, type FormEvent } from "react"
import { Link, useSearchParams } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  Field,
  FilterSelect,
  Loading,
  PageHeader,
  Pager,
  Section,
  StatusBadge,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, companyName, duration, num } from "@/lib/format"
import type { CompanyItem, EnqueueResult, ImportOutcome, Meta, Page } from "@/lib/types"

function enqueueMessage(r: EnqueueResult) {
  return `Đã đưa ${r.enqueued}/${r.selected} doanh nghiệp vào queue${
    r.already_queued ? ` (${r.already_queued} đã có job chờ/chạy)` : ""
  }`
}

function ImportCard() {
  const client = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [enqueue, setEnqueue] = useState(false)
  const mutation = useMutation({
    mutationFn: () => {
      const form = new FormData()
      form.append("file", file!)
      form.append("enqueue", String(enqueue))
      return api.form<ImportOutcome>("/companies/import", form)
    },
    onSuccess: () => {
      toast.success("Đã nạp danh sách")
      client.invalidateQueries({ queryKey: ["companies"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  })
  const r = mutation.data

  return (
    <Section title="Nạp danh sách (CSV)" description="Cột name (bắt buộc), tax_code, website, note. Chạy lại không tạo trùng.">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (file) mutation.mutate()
        }}
      >
        <Input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <div className="flex items-center gap-2">
          <Checkbox id="enqueue" checked={enqueue} onCheckedChange={(v) => setEnqueue(v === true)} />
          <Label htmlFor="enqueue" className="text-sm font-normal">
            Đưa luôn vào queue sau khi nạp
          </Label>
        </div>
        <Button type="submit" size="sm" disabled={!file || mutation.isPending}>
          <Upload /> {mutation.isPending ? "Đang nạp…" : "Nạp"}
        </Button>
      </form>
      {r && (
        <div className="mt-4 space-y-3 border-t pt-4 text-sm">
          <div className="grid grid-cols-3 gap-2 text-center">
            {[
              ["Hợp lệ", r.rows],
              ["Tạo mới", r.created],
              ["Cập nhật", r.updated],
              ["Giữ nguyên", r.unchanged],
              ["Trùng", r.duplicates],
              ["Bỏ", r.errors.length],
            ].map(([k, v]) => (
              <div key={k} className="rounded-md bg-muted px-2 py-1.5">
                <div className="text-lg font-semibold tabular-nums">{num(v as number)}</div>
                <div className="text-xs text-muted-foreground">{k}</div>
              </div>
            ))}
          </div>
          {r.enqueued !== null && (
            <p className="text-muted-foreground">
              Vào queue: {r.enqueued} ({r.already_queued} đã có)
            </p>
          )}
          {(r.errors.length > 0 || r.warnings.length > 0) && (
            <ul className="max-h-48 space-y-1 overflow-auto text-xs">
              {r.errors.slice(0, 50).map((m) => (
                <li key={m} className="text-destructive">
                  {m}
                </li>
              ))}
              {r.warnings.slice(0, 50).map((m) => (
                <li key={m} className="text-warning">
                  {m}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Section>
  )
}

export function CompaniesPage() {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const filters = {
    q: params.get("q") ?? "",
    kyc: params.get("kyc") ?? "",
    run: params.get("run") ?? "",
    jobs: params.get("jobs") ?? "",
    page: Number(params.get("page") ?? 1),
  }
  const [search, setSearch] = useState(filters.q)
  useEffect(() => setSearch(filters.q), [filters.q])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [staleHours, setStaleHours] = useState("24")

  const meta = useQuery({ queryKey: ["meta"], queryFn: () => api.get<Meta>("/meta"), staleTime: 300_000 })
  const query = useQuery({
    queryKey: ["companies", filters],
    queryFn: () => api.get<Page<CompanyItem>>(`/companies${qs(filters)}`),
    placeholderData: (prev) => prev,
  })

  const update = (changes: Record<string, string | number>) => {
    const next = { ...filters, page: 1, ...changes }
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(next)) if (v !== "" && !(k === "page" && v === 1)) out[k] = String(v)
    setParams(out)
    setSelected(new Set())
  }

  const enqueue = useMutation({
    mutationFn: (body: { company_ids?: number[]; stale_hours?: number; all?: boolean }) =>
      api.post<EnqueueResult>("/enqueue", body),
    onSuccess: (r) => {
      toast.success(enqueueMessage(r))
      setSelected(new Set())
      client.invalidateQueries({ queryKey: ["companies"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  })

  const items = query.data?.items ?? []
  const allChecked = items.length > 0 && items.every((r) => selected.has(r.company.id))
  const toggleAll = () =>
    setSelected(allChecked ? new Set() : new Set(items.map((r) => r.company.id)))
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <>
      <PageHeader
        title="Doanh nghiệp"
        description={query.data ? `${num(query.data.total)} doanh nghiệp khớp bộ lọc` : "Đang tải…"}
        actions={
          <>
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              chưa chạy trong
              <Input
                type="number"
                min={1}
                value={staleHours}
                onChange={(e) => setStaleHours(e.target.value)}
                className="h-8 w-16"
              />
              giờ
            </div>
            <ConfirmButton
              title="Chạy doanh nghiệp cũ?"
              description={`Đưa các doanh nghiệp chưa có lượt chạy xong trong ${staleHours} giờ qua vào queue.`}
              onConfirm={() => enqueue.mutate({ stale_hours: Number(staleHours) || 24 })}
            >
              <PlayCircle /> Chạy DN cũ
            </ConfirmButton>
            <ConfirmButton
              title="Chạy TẤT CẢ doanh nghiệp?"
              description="Mỗi doanh nghiệp một job trong queue; job đang chờ/chạy sẽ không bị nạp trùng."
              onConfirm={() => enqueue.mutate({ all: true })}
            >
              <Play /> Chạy tất cả
            </ConfirmButton>
          </>
        }
      />

      <div className="grid gap-6 xl:grid-cols-[1fr_340px]">
        <div className="min-w-0 space-y-4">
          <Section bodyClassName="flex flex-wrap items-end gap-3">
            <form
              className="flex items-end gap-2"
              onSubmit={(e: FormEvent) => {
                e.preventDefault()
                update({ q: search.trim() })
              }}
            >
              <Field label="Tìm">
                <div className="relative">
                  <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="tên, MST, domain, alias"
                    className="h-8 w-64 pl-8"
                  />
                </div>
              </Field>
            </form>
            <Field label="KYC">
              <FilterSelect
                value={filters.kyc}
                onChange={(v) => update({ kyc: v })}
                options={[...(meta.data?.kyc_statuses ?? []), "none"]}
              />
            </Field>
            <Field label="Lượt mới nhất">
              <FilterSelect
                value={filters.run}
                onChange={(v) => update({ run: v })}
                options={[...(meta.data?.run_statuses ?? []), "never"]}
              />
            </Field>
            <Field label="Job">
              <FilterSelect value={filters.jobs} onChange={(v) => update({ jobs: v })} options={["with", "without"]} />
            </Field>
            {(filters.q || filters.kyc || filters.run || filters.jobs) && (
              <Button variant="ghost" size="sm" onClick={() => setParams({})}>
                Xoá lọc
              </Button>
            )}
          </Section>

          <Section
            bodyClassName="p-0"
            title={selected.size ? `Đã chọn ${selected.size}` : "Danh sách"}
            actions={
              <Button
                size="sm"
                disabled={!selected.size || enqueue.isPending}
                onClick={() => enqueue.mutate({ company_ids: [...selected] })}
              >
                <Play /> Chạy các DN đã chọn
              </Button>
            }
          >
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
                      <TableHead className="w-10 pl-4">
                        <Checkbox checked={allChecked} onCheckedChange={toggleAll} aria-label="Chọn tất cả" />
                      </TableHead>
                      <TableHead>Doanh nghiệp</TableHead>
                      <TableHead>KYC</TableHead>
                      <TableHead>Domain / MST</TableHead>
                      <TableHead>Lượt mới nhất</TableHead>
                      <TableHead className="text-right">Thời gian</TableHead>
                      <TableHead className="pr-4 text-right">Job mở</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((row) => {
                      const c = row.company
                      const shown = companyName(c)
                      return (
                        <TableRow key={c.id} data-state={selected.has(c.id) ? "selected" : undefined}>
                          <TableCell className="pl-4">
                            <Checkbox
                              checked={selected.has(c.id)}
                              onCheckedChange={() => toggle(c.id)}
                              aria-label={`Chọn ${shown}`}
                            />
                          </TableCell>
                          <TableCell className="max-w-80 min-w-56 whitespace-normal">
                            <Link to={`/companies/${c.id}`} className="font-medium hover:underline">
                              {shown}
                            </Link>
                            {c.legal_name && c.legal_name !== shown && (
                              <div className="text-xs text-muted-foreground">{c.legal_name}</div>
                            )}
                            {c.input_name !== shown && c.input_name !== c.legal_name && (
                              <div className="text-xs text-muted-foreground">nhập: {c.input_name}</div>
                            )}
                          </TableCell>
                          <TableCell>{c.kyc_status ? <StatusBadge value={c.kyc_status} /> : <span className="text-muted-foreground">–</span>}</TableCell>
                          <TableCell className="text-xs">
                            <div>{c.primary_domain || row.input_website || "–"}</div>
                            {c.tax_code && <div className="text-muted-foreground">{c.tax_code}</div>}
                          </TableCell>
                          <TableCell className="whitespace-normal">
                            {row.last_status ? (
                              <div className="flex flex-col gap-0.5">
                                <span className="flex items-center gap-1.5">
                                  <StatusBadge value={row.last_status} />
                                  <span className="text-xs text-muted-foreground">{ago(row.last_started)}</span>
                                </span>
                                {row.last_incomplete.length > 0 && (
                                  <span className="text-xs text-muted-foreground">
                                    chưa xong: {row.last_incomplete.join(", ")}
                                  </span>
                                )}
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">chưa chạy</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{duration(row.last_duration_ms)}</TableCell>
                          <TableCell className="pr-4 text-right tabular-nums">
                            {row.active_jobs ? (
                              <Link to={`/jobs?company_id=${c.id}`} className="text-primary hover:underline">
                                {row.active_jobs}
                              </Link>
                            ) : (
                              0
                            )}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              ) : (
                <Empty>Không có doanh nghiệp nào. Nạp companies.csv ở khung bên.</Empty>
              ))}
            {query.data && (
              <Pager
                page={query.data.page}
                pages={query.data.pages}
                total={query.data.total}
                onPage={(p) => update({ page: p })}
              />
            )}
          </Section>
        </div>
        <aside className="space-y-4">
          <ImportCard />
        </aside>
      </div>
    </>
  )
}
