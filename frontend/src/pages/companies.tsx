import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Building2, Play, Upload, X } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import {
  Empty,
  ErrorState,
  FilePicker,
  FilterBar,
  FilterSelect,
  IncompleteSources,
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
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ago, cap, companyName, displayName, duration, num } from "@/lib/format"
import {
  getGetCompaniesQueryKey,
  getGetQueueQueryKey,
  postEnqueue,
  useGetCompanies,
  useGetMeta,
  usePostImport,
} from "@/lib/gen/queries"
import type { CompanyItem, EnqueueResult } from "@/lib/types"
import { useUrlFilters, type FilterChanges } from "@/lib/url-filters"
import { cn } from "@/lib/utils"

/** Bộ lọc trên URL (mặc định: không lọc). */
const FILTERS = { q: "", kyc: "", run: "", jobs: "" }

function enqueueMessage(r: EnqueueResult) {
  return `Đã đưa ${num(r.enqueued)}/${num(r.selected)} doanh nghiệp vào hàng đợi${
    r.already_queued ? ` (${num(r.already_queued)} đã có việc đang chờ hoặc đang chạy)` : ""
  }`
}

// Hai cách viết của cùng một tên (khác hoa/thường, khoảng trắng) thì không hiện lặp.
const normName = (s: string | null) => (s ?? "").toLocaleLowerCase("vi").replace(/\s+/g, " ").trim()
const sameName = (a: string | null, b: string | null) => normName(a) === normName(b)

type EnqueueBody = { company_ids?: string[]; stale_hours?: number; all?: boolean }

function useEnqueue(onDone?: () => void) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: EnqueueBody) => postEnqueue(body),
    onSuccess: (r) => {
      toast.success(enqueueMessage(r))
      client.invalidateQueries({ queryKey: getGetCompaniesQueryKey() })
      client.invalidateQueries({ queryKey: getGetQueueQueryKey() })
      onDone?.()
    },
  })
}

/** Hộp thoại chạy nhiều doanh nghiệp: lâu chưa chạy (theo số giờ) hoặc tất cả. */
function RunDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [mode, setMode] = useState<"stale" | "all">("stale")
  const [hours, setHours] = useState("24")
  const enqueue = useEnqueue(() => onOpenChange(false))
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Chạy doanh nghiệp</DialogTitle>
          <DialogDescription>
            Mỗi doanh nghiệp một việc trong hàng đợi; doanh nghiệp đang chờ hoặc đang chạy không bị nạp trùng.
          </DialogDescription>
        </DialogHeader>
        <RadioGroup value={mode} onValueChange={(v) => setMode(v === "all" ? "all" : "stale")} className="gap-2">
          <label
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border p-3.5 transition-colors hover:bg-muted/50",
              mode === "stale" && "border-primary/50 bg-primary/5",
            )}
          >
            <RadioGroupItem value="stale" className="mt-0.5" />
            <div className="min-w-0 flex-1 space-y-2">
              <div className="text-sm font-medium">Doanh nghiệp lâu chưa cập nhật</div>
              <NumberChip prefix="Chưa có lượt chạy xong trong" suffix="giờ" value={hours} onChange={setHours} min={1} />
            </div>
          </label>
          <label
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border p-3.5 transition-colors hover:bg-muted/50",
              mode === "all" && "border-primary/50 bg-primary/5",
            )}
          >
            <RadioGroupItem value="all" className="mt-0.5" />
            <div>
              <div className="text-sm font-medium">Tất cả doanh nghiệp</div>
              <div className="text-meta text-muted-foreground">Chạy lại toàn bộ danh sách, có thể mất nhiều giờ.</div>
            </div>
          </label>
        </RadioGroup>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Huỷ
          </Button>
          <Button
            disabled={enqueue.isPending}
            onClick={() => enqueue.mutate(mode === "all" ? { all: true } : { stale_hours: Number(hours) || 24 })}
          >
            <Play /> {enqueue.isPending ? "Đang đưa vào hàng đợi…" : "Đưa vào hàng đợi"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Hộp thoại nạp companies.csv; kết quả nạp hiện ngay trong hộp thoại. */
function ImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const client = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [enqueue, setEnqueue] = useState(false)
  const mutation = usePostImport({
    mutation: {
      onSuccess: (r) => {
        toast.success(`Đã nạp ${num(r.rows)} dòng: ${num(r.created)} mới, ${num(r.updated)} cập nhật`)
        client.invalidateQueries({ queryKey: getGetCompaniesQueryKey() })
      },
    },
  })
  const r = mutation.data

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v)
        if (!v) {
          setFile(null)
          mutation.reset()
        }
      }}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Nạp danh sách doanh nghiệp</DialogTitle>
          <DialogDescription>
            File CSV gồm cột <code className="font-mono">name</code> (bắt buộc), <code className="font-mono">tax_code</code>,{" "}
            <code className="font-mono">website</code>, <code className="font-mono">note</code>. Nạp lại cùng file không tạo
            trùng doanh nghiệp.
          </DialogDescription>
        </DialogHeader>
        <form
          id="import-form"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (file) mutation.mutate({ data: { file, enqueue } })
          }}
        >
          <FilePicker
            file={file}
            onChange={(f) => {
              setFile(f)
              mutation.reset()
            }}
            title="Chọn file companies.csv"
            hint="UTF-8, phân cách bằng dấu phẩy, chấm phẩy hoặc tab"
          />
          <div className="flex items-center gap-2.5">
            <Checkbox id="enqueue" checked={enqueue} onCheckedChange={(v) => setEnqueue(v === true)} />
            <Label htmlFor="enqueue" className="font-normal">
              Đưa vào hàng đợi ngay sau khi nạp
            </Label>
          </div>
        </form>
        {r && (
          <div className="space-y-3 rounded-lg border bg-muted/30 p-4 text-sm">
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
                <div key={k} className="rounded-md bg-card px-2 py-2 shadow-card">
                  <div className="text-base font-semibold tabular-nums">{num(v)}</div>
                  <div className="text-meta text-muted-foreground">{k}</div>
                </div>
              ))}
            </div>
            {r.enqueued !== null && (
              <p className="text-muted-foreground">
                Đã đưa {num(r.enqueued)} doanh nghiệp vào hàng đợi
                {r.already_queued ? ` (${num(r.already_queued)} đã có sẵn)` : ""}.
              </p>
            )}
            {(r.errors.length > 0 || r.warnings.length > 0) && (
              <ul className="max-h-40 space-y-1 overflow-auto text-meta">
                {r.errors.slice(0, 50).map((m) => (
                  <li key={m} className="text-danger-ink">
                    {cap(m)}
                  </li>
                ))}
                {r.warnings.slice(0, 50).map((m) => (
                  <li key={m} className="text-warning-ink">
                    {cap(m)}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {r ? "Đóng" : "Huỷ"}
          </Button>
          <Button type="submit" form="import-form" disabled={!file || mutation.isPending}>
            <Upload /> {mutation.isPending ? "Đang nạp…" : "Nạp danh sách"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Tên chính + tên pháp lý / tên nhập (khi khác) + domain · MST. */
function CompanyCell({ row }: { row: CompanyItem }) {
  const c = row.company
  const shown = companyName(c)
  const site = c.primary_domain || row.input_website
  return (
    <>
      <TextLink to={`/companies/${c.id}`} className="font-medium">
        {shown}
      </TextLink>
      {c.legal_name && !sameName(c.legal_name, shown) && (
        <div className="text-meta text-muted-foreground">{displayName(c.legal_name)}</div>
      )}
      {!sameName(c.input_name, shown) && !sameName(c.input_name, c.legal_name) && (
        <div className="text-meta text-muted-foreground">Tên nhập: {displayName(c.input_name)}</div>
      )}
      {(site || c.tax_code) && (
        <div className="mt-0.5 text-meta break-all text-muted-foreground tabular-nums">
          {[site, c.tax_code && `MST ${c.tax_code}`].filter(Boolean).join(" · ")}
        </div>
      )}
    </>
  )
}

function LastRun({ row }: { row: CompanyItem }) {
  if (!row.last_status) return <span className="text-meta text-muted-foreground">Chưa chạy</span>
  return (
    <div className="flex flex-col items-start gap-1">
      <StatusBadge value={row.last_status} />
      <span className="text-meta whitespace-nowrap text-muted-foreground tabular-nums">
        {cap(ago(row.last_started))} · {duration(row.last_duration_ms)}
      </span>
      <IncompleteSources sources={row.last_incomplete} />
    </div>
  )
}

function KycBadge({ status }: { status: string | null }) {
  return status ? <StatusBadge value={status} /> : <StatusBadge value="none" tone="muted" />
}

export function CompaniesPage() {
  const { filters, page, update: setFilters, reset, isFiltered } = useUrlFilters(FILTERS)
  const [search, setSearch] = useState(filters.q)
  useEffect(() => setSearch(filters.q), [filters.q])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [runOpen, setRunOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const rowLink = useRowLink()

  const meta = useGetMeta({ query: { staleTime: 300_000 } })
  const query = useGetCompanies({ ...filters, page }, { query: { placeholderData: (prev) => prev } })

  // Đổi bộ lọc thì bỏ chọn (các dòng đã chọn không còn trên trang); chuyển trang thì giữ.
  const update = (changes: FilterChanges<keyof typeof FILTERS>) => {
    setFilters(changes)
    if (!("page" in changes)) setSelected(new Set())
  }

  const enqueue = useEnqueue(() => setSelected(new Set()))

  const items = query.data?.items ?? []
  const allChecked = items.length > 0 && items.every((r) => selected.has(r.company.id))
  const someChecked = !allChecked && items.some((r) => selected.has(r.company.id))
  const toggleAll = () => setSelected(allChecked ? new Set() : new Set(items.map((r) => r.company.id)))
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const filtered = isFiltered()

  return (
    <>
      <PageHeader
        title="Doanh nghiệp"
        description={
          query.data
            ? `${num(query.data.total)} doanh nghiệp${filtered ? " khớp bộ lọc" : ""} · bấm một dòng để xem chi tiết`
            : "Đang tải danh sách…"
        }
        actions={
          <>
            <Button variant="outline" onClick={() => setImportOpen(true)}>
              <Upload /> Nạp danh sách
            </Button>
            <Button onClick={() => setRunOpen(true)}>
              <Play /> Chạy…
            </Button>
          </>
        }
      />
      <RunDialog open={runOpen} onOpenChange={setRunOpen} />
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} />

      <Section bodyClassName="p-0">
        <FilterBar
          search={
            <SearchBox
              value={search}
              onChange={setSearch}
              onSubmit={(v) => update({ q: (v ?? search).trim() })}
              placeholder="Tìm theo tên, MST, domain hoặc tên gọi khác"
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
              {/* Màn hình hẹp: mỗi doanh nghiệp một thẻ. */}
              <ul className="divide-y md:hidden">
                {items.map((row) => (
                  <li key={row.company.id} {...rowLink(`/companies/${row.company.id}`, "flex gap-3 px-4 py-3.5")}>
                    <Checkbox
                      className="mt-0.5"
                      checked={selected.has(row.company.id)}
                      onCheckedChange={() => toggle(row.company.id)}
                      aria-label={`Chọn ${companyName(row.company)}`}
                    />
                    <div className="min-w-0 flex-1 space-y-2">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 text-sm">
                          <CompanyCell row={row} />
                        </div>
                        <span className="shrink-0 text-right">
                          <span className="block text-base font-semibold text-brand tabular-nums">{num(row.active_jobs)}</span>
                          <span className="text-caption text-muted-foreground">việc mở</span>
                        </span>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                        <KycBadge status={row.company.kyc_status} />
                        {row.last_status ? (
                          <>
                            <StatusBadge value={row.last_status} />
                            <span className="text-meta text-muted-foreground">{cap(ago(row.last_started))}</span>
                            <IncompleteSources sources={row.last_incomplete} />
                          </>
                        ) : (
                          <span className="text-meta text-muted-foreground">Chưa chạy</span>
                        )}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>

              <Table className="max-md:hidden">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-10">
                      <Checkbox
                        checked={allChecked ? true : someChecked ? "indeterminate" : false}
                        onCheckedChange={toggleAll}
                        aria-label="Chọn cả trang"
                      />
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
                    return (
                      <TableRow
                        key={c.id}
                        data-state={selected.has(c.id) ? "selected" : undefined}
                        {...rowLink(`/companies/${c.id}`)}
                      >
                        <TableCell>
                          <Checkbox
                            checked={selected.has(c.id)}
                            onCheckedChange={() => toggle(c.id)}
                            aria-label={`Chọn ${companyName(c)}`}
                          />
                        </TableCell>
                        <TableCell className="max-w-[30rem] min-w-64 whitespace-normal">
                          <CompanyCell row={row} />
                        </TableCell>
                        <TableCell>
                          <KycBadge status={c.kyc_status} />
                        </TableCell>
                        <TableCell className="max-w-72 min-w-44 whitespace-normal">
                          <LastRun row={row} />
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {row.active_jobs ? (
                            <TextLink to={`/jobs?company_id=${c.id}`} className="text-base font-semibold text-brand">
                              {num(row.active_jobs)}
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
            </div>
          ) : (
            <Empty
              icon={<Building2 />}
              title={filtered ? "Không có doanh nghiệp nào khớp bộ lọc" : "Chưa có doanh nghiệp nào"}
              action={
                filtered ? (
                  <Button variant="outline" size="sm" onClick={reset}>
                    Xoá bộ lọc
                  </Button>
                ) : (
                  <Button size="sm" onClick={() => setImportOpen(true)}>
                    <Upload /> Nạp danh sách
                  </Button>
                )
              }
            />
          ))}
        {query.data && <Pager {...query.data} onPage={(p) => update({ page: p })} />}
      </Section>

      {/* Thanh thao tác khi đã chọn doanh nghiệp: luôn nằm ở đáy màn hình. */}
      {selected.size > 0 && (
        <div className="pointer-events-none fixed inset-x-0 bottom-4 z-40 flex justify-center px-4 lg:pl-64">
          <div className="pointer-events-auto flex items-center gap-2 rounded-xl border bg-popover py-2 pr-2 pl-4 text-sm shadow-xl">
            <span className="font-medium tabular-nums">Đã chọn {num(selected.size)}</span>
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
              Bỏ chọn
            </Button>
            <Button size="sm" disabled={enqueue.isPending} onClick={() => enqueue.mutate({ company_ids: [...selected] })}>
              <Play /> Chạy ngay
            </Button>
          </div>
        </div>
      )}
    </>
  )
}
