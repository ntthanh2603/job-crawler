import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Play, PlayCircle, Search, Upload } from "lucide-react"
import { useEffect, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  Field,
  FilePicker,
  FilterSelect,
  Loading,
  PageHeader,
  Pager,
  Section,
  StatusBadge,
  TextLink,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, cap, companyName, duration, num, sourceLabel } from "@/lib/format"
import type { CompanyItem, EnqueueResult, ImportOutcome, Meta, Page } from "@/lib/types"

function enqueueMessage(r: EnqueueResult) {
  return `Đã đưa ${r.enqueued}/${r.selected} doanh nghiệp vào hàng đợi${
    r.already_queued ? ` (${r.already_queued} đã có việc đang chờ hoặc đang chạy)` : ""
  }`
}

const IMPORT_ID = "nap-danh-sach"

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
    onSuccess: (r) => {
      toast.success(`Đã nạp ${num(r.rows)} dòng: ${num(r.created)} mới, ${num(r.updated)} cập nhật`)
      client.invalidateQueries({ queryKey: ["companies"] })
    },
    onError: (e) => toast.error(cap(e instanceof Error ? e.message : String(e))),
  })
  const r = mutation.data

  return (
    <Section
      title="Nạp danh sách"
      description="File CSV: cột name (bắt buộc), tax_code, website, note"
      className="scroll-mt-6"
      id={IMPORT_ID}
    >
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (file) mutation.mutate()
        }}
      >
        <FilePicker
          file={file}
          onChange={setFile}
          title="Chọn file companies.csv"
          hint="UTF-8, phân cách bằng dấu phẩy, chấm phẩy hoặc tab"
        />
        <div className="flex items-center gap-2">
          <Checkbox id="enqueue" checked={enqueue} onCheckedChange={(v) => setEnqueue(v === true)} />
          <Label htmlFor="enqueue" className="text-sm font-normal">
            Đưa vào hàng đợi ngay sau khi nạp
          </Label>
        </div>
        <Button type="submit" size="sm" className="w-full" disabled={!file || mutation.isPending}>
          <Upload /> {mutation.isPending ? "Đang nạp…" : "Nạp danh sách"}
        </Button>
        <p className="text-xs text-muted-foreground">Nạp lại cùng file không tạo trùng doanh nghiệp.</p>
      </form>
      {r && (
        <div className="mt-4 space-y-3 border-t pt-4 text-sm">
          <div className="grid grid-cols-3 gap-2 text-center">
            {(
              [
                ["Hợp lệ", r.rows],
                ["Tạo mới", r.created],
                ["Cập nhật", r.updated],
                ["Giữ nguyên", r.unchanged],
                ["Trùng", r.duplicates],
                ["Bị bỏ", r.errors.length],
              ] as [string, number][]
            ).map(([k, v]) => (
              <div key={k} className="rounded-lg bg-muted/60 px-2 py-2">
                <div className="text-base font-semibold tabular-nums">{num(v)}</div>
                <div className="text-xs text-muted-foreground">{k}</div>
              </div>
            ))}
          </div>
          {r.enqueued !== null && (
            <p className="text-muted-foreground">
              Đã đưa {r.enqueued} doanh nghiệp vào hàng đợi
              {r.already_queued ? ` (${r.already_queued} đã có sẵn)` : ""}.
            </p>
          )}
          {(r.errors.length > 0 || r.warnings.length > 0) && (
            <ul className="max-h-48 space-y-1 overflow-auto rounded-lg bg-muted/40 p-2 text-xs">
              {r.errors.slice(0, 50).map((m) => (
                <li key={m} className="text-destructive">
                  {cap(m)}
                </li>
              ))}
              {r.warnings.slice(0, 50).map((m) => (
                <li key={m} className="text-warning">
                  {cap(m)}
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
    onError: (e) => toast.error(cap(e instanceof Error ? e.message : String(e))),
  })

  const items = query.data?.items ?? []
  const allChecked = items.length > 0 && items.every((r) => selected.has(r.company.id))
  const toggleAll = () => setSelected(allChecked ? new Set() : new Set(items.map((r) => r.company.id)))
  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const filtered = Boolean(filters.q || filters.kyc || filters.run || filters.jobs)

  return (
    <>
      <PageHeader
        title="Doanh nghiệp"
        description={query.data ? `${num(query.data.total)} doanh nghiệp khớp bộ lọc` : "Đang tải danh sách…"}
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              className="2xl:hidden"
              onClick={() => document.getElementById(IMPORT_ID)?.scrollIntoView({ behavior: "smooth" })}
            >
              <Upload /> Nạp CSV
            </Button>
            <div className="flex h-8 items-center gap-1.5 rounded-md border bg-card pr-1 pl-2.5 text-xs text-muted-foreground">
              Chưa chạy trong
              <Input
                type="number"
                min={1}
                value={staleHours}
                onChange={(e) => setStaleHours(e.target.value)}
                className="h-6 w-14 px-1.5 text-center"
                aria-label="Số giờ"
              />
              giờ
            </div>
            <ConfirmButton
              title="Chạy các doanh nghiệp lâu chưa cập nhật?"
              description={`Đưa các doanh nghiệp chưa có lượt chạy xong trong ${staleHours} giờ qua vào hàng đợi.`}
              onConfirm={() => enqueue.mutate({ stale_hours: Number(staleHours) || 24 })}
            >
              <PlayCircle /> Chạy DN cũ
            </ConfirmButton>
            <ConfirmButton
              title="Chạy tất cả doanh nghiệp?"
              description="Mỗi doanh nghiệp một việc trong hàng đợi; doanh nghiệp đang chờ hoặc đang chạy sẽ không bị nạp trùng."
              onConfirm={() => enqueue.mutate({ all: true })}
            >
              <Play /> Chạy tất cả
            </ConfirmButton>
          </>
        }
      />

      {/* Khung nạp CSV nằm bên phải khi đủ rộng (≥ 1536px), hẹp hơn thì xuống dưới để bảng không tràn. */}
      <div className="grid gap-6 2xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <form
              className="flex items-end"
              onSubmit={(e: FormEvent) => {
                e.preventDefault()
                update({ q: search.trim() })
              }}
            >
              <Field label="Tìm kiếm">
                <div className="relative">
                  <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Tên, MST, domain, tên gọi khác"
                    className="h-8 w-72 bg-card pl-8"
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
            <Field label="Lượt chạy mới nhất">
              <FilterSelect
                value={filters.run}
                onChange={(v) => update({ run: v })}
                options={[...(meta.data?.run_statuses ?? []), "never"]}
              />
            </Field>
            <Field label="Việc làm">
              <FilterSelect value={filters.jobs} onChange={(v) => update({ jobs: v })} options={["with", "without"]} />
            </Field>
            {filtered && (
              <Button variant="ghost" size="sm" onClick={() => setParams({})}>
                Xoá bộ lọc
              </Button>
            )}
          </div>

          <Section
            bodyClassName="p-0"
            title={selected.size ? `Đã chọn ${selected.size} doanh nghiệp` : "Danh sách doanh nghiệp"}
            actions={
              <Button
                size="sm"
                disabled={!selected.size || enqueue.isPending}
                onClick={() => enqueue.mutate({ company_ids: [...selected] })}
              >
                <Play /> Chạy mục đã chọn
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
                      <TableHead>Lượt chạy mới nhất</TableHead>
                      <TableHead className="pr-4 text-right">Việc đang mở</TableHead>
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
                          <TableCell className="max-w-[26rem] min-w-60 whitespace-normal">
                            <TextLink to={`/companies/${c.id}`} className="font-medium">
                              {shown}
                            </TextLink>
                            {c.legal_name && cap(c.legal_name) !== shown && (
                              <div className="text-xs text-muted-foreground">{cap(c.legal_name)}</div>
                            )}
                            {cap(c.input_name) !== shown && c.input_name !== c.legal_name && (
                              <div className="text-xs text-muted-foreground">Tên nhập: {c.input_name}</div>
                            )}
                            {(c.primary_domain || row.input_website || c.tax_code) && (
                              <div className="mt-0.5 text-xs break-all text-muted-foreground tabular-nums">
                                {[c.primary_domain || row.input_website, c.tax_code && `MST ${c.tax_code}`]
                                  .filter(Boolean)
                                  .join(" · ")}
                              </div>
                            )}
                          </TableCell>
                          <TableCell>
                            {c.kyc_status ? (
                              <StatusBadge value={c.kyc_status} />
                            ) : (
                              <StatusBadge value="none" tone="muted" />
                            )}
                          </TableCell>
                          <TableCell className="min-w-44 whitespace-normal">
                            {row.last_status ? (
                              <div className="flex flex-col items-start gap-1">
                                <StatusBadge value={row.last_status} />
                                <span className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">
                                  {cap(ago(row.last_started))} · {duration(row.last_duration_ms)}
                                </span>
                                {row.last_incomplete.length > 0 && (
                                  <span className="text-xs text-muted-foreground">
                                    Chưa xong: {row.last_incomplete.map(sourceLabel).join(", ")}
                                  </span>
                                )}
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">Chưa chạy</span>
                            )}
                          </TableCell>
                          <TableCell className="pr-4 text-right tabular-nums">
                            {row.active_jobs ? (
                              <TextLink to={`/jobs?company_id=${c.id}`} className="font-medium text-brand">
                                {row.active_jobs}
                              </TextLink>
                            ) : (
                              <span className="text-muted-foreground">0</span>
                            )}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              ) : (
                <Empty>
                  {filtered ? "Không có doanh nghiệp nào khớp bộ lọc." : "Chưa có doanh nghiệp nào. Nạp companies.csv ở khung Nạp danh sách."}
                </Empty>
              ))}
            {query.data && (
              <Pager page={query.data.page} pages={query.data.pages} total={query.data.total} onPage={(p) => update({ page: p })} />
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
