import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Play, PlayCircle, Upload } from "lucide-react"
import { useEffect, useState } from "react"
import { useSearchParams } from "react-router"
import { toast } from "sonner"

import {
  ConfirmButton,
  Empty,
  ErrorState,
  FilePicker,
  FilterBar,
  FilterSelect,
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
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import { ago, cap, companyName, displayName, duration, num, sourceLabel } from "@/lib/format"
import type { CompanyItem, EnqueueResult, ImportOutcome, Meta, Page } from "@/lib/types"

function enqueueMessage(r: EnqueueResult) {
  return `Đã đưa ${r.enqueued}/${r.selected} doanh nghiệp vào hàng đợi${
    r.already_queued ? ` (${r.already_queued} đã có việc đang chờ hoặc đang chạy)` : ""
  }`
}

// Hai cách viết của cùng một tên (khác hoa/thường, khoảng trắng) thì không hiện lặp.
const normName = (s: string | null) => (s ?? "").toLocaleLowerCase("vi").replace(/\s+/g, " ").trim()
const sameName = (a: string | null, b: string | null) => normName(a) === normName(b)

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
        <p className="text-meta text-muted-foreground">Nạp lại cùng file không tạo trùng doanh nghiệp.</p>
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
                <div className="text-meta text-muted-foreground">{k}</div>
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
            <ul className="max-h-48 space-y-1 overflow-auto rounded-lg bg-muted/40 p-2 text-meta">
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
            <NumberChip prefix="Chưa chạy trong" suffix="giờ" value={staleHours} onChange={setStaleHours} min={1} />
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
            <FilterBar
              search={
                <SearchBox
                  className="sm:w-80"
                  value={search}
                  onChange={setSearch}
                  onSubmit={() => update({ q: search.trim() })}
                  placeholder="Tên, MST, domain, tên gọi khác"
                />
              }
            >
              <FilterSelect
                label="KYC"
                value={filters.kyc}
                onChange={(v) => update({ kyc: v })}
                options={[...(meta.data?.kyc_statuses ?? []), "none"]}
              />
              <FilterSelect
                label="Lượt chạy mới nhất"
                value={filters.run}
                onChange={(v) => update({ run: v })}
                options={[...(meta.data?.run_statuses ?? []), "never"]}
              />
              <FilterSelect label="Việc làm" value={filters.jobs} onChange={(v) => update({ jobs: v })} options={["with", "without"]} />
              {filtered && (
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
                      <TableHead className="w-10">
                        <Checkbox checked={allChecked} onCheckedChange={toggleAll} aria-label="Chọn tất cả" />
                      </TableHead>
                      <TableHead>Doanh nghiệp</TableHead>
                      <TableHead>KYC</TableHead>
                      <TableHead>Lượt chạy mới nhất</TableHead>
                      <TableHead className="text-right">Việc đang mở</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((row) => {
                      const c = row.company
                      const shown = companyName(c)
                      return (
                        <TableRow key={c.id} data-state={selected.has(c.id) ? "selected" : undefined}>
                          <TableCell>
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
                            {c.legal_name && !sameName(c.legal_name, shown) && (
                              <div className="text-meta text-muted-foreground">{displayName(c.legal_name)}</div>
                            )}
                            {!sameName(c.input_name, shown) && !sameName(c.input_name, c.legal_name) && (
                              <div className="text-meta text-muted-foreground">Tên nhập: {displayName(c.input_name)}</div>
                            )}
                            {(c.primary_domain || row.input_website || c.tax_code) && (
                              <div className="mt-0.5 text-meta break-all text-muted-foreground tabular-nums">
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
                                <span className="text-meta whitespace-nowrap text-muted-foreground tabular-nums">
                                  {cap(ago(row.last_started))} · {duration(row.last_duration_ms)}
                                </span>
                                {row.last_incomplete.length > 0 && (
                                  <span className="text-meta text-muted-foreground">
                                    Chưa xong: {row.last_incomplete.map(sourceLabel).join(", ")}
                                  </span>
                                )}
                              </div>
                            ) : (
                              <span className="text-meta text-muted-foreground">Chưa chạy</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
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
              <Pager {...query.data} onPage={(p) => update({ page: p })} />
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
