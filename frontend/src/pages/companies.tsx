import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Building2, Download, Play, Plus, Upload, X } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
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
  SortHead,
  SortSelect,
  StatusBadge,
  TextLink,
  nextSort,
  pageLimit,
  useRowLink,
  RunStatusBadge,
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { errorMessage } from "@/lib/api"
import { ago, cap, companyName, displayName, duration, num, runStatusLabel } from "@/lib/format"
import {
  getGetCompaniesQueryKey,
  getGetQueueQueryKey,
  postEnqueue,
  useGetCompanies,
  useGetEnqueuePreview,
  useGetApiClients,
  useGetMeta,
  usePostCompany,
  usePostImport,
  type GetCompaniesSortBy,
} from "@/lib/gen/queries"
import type { CompanyItem, EnqueueResult, SortOrder, SortState } from "@/lib/types"
import { useUrlFilters, type FilterChanges } from "@/lib/url-filters"
import { cn } from "@/lib/utils"

/** Bộ lọc, số dòng và sắp xếp trên URL (như open-asm: `search`, `pageSize`, `sortBy`, `sortOrder`);
 * giá trị mặc định không ghi lên URL. */
const FILTERS = { search: "", kyc: "", run: "", jobs: "", pageSize: "50", sortBy: "created_at", sortOrder: "DESC" }

const SORTS: readonly (readonly [GetCompaniesSortBy, SortOrder, string])[] = [
  ["created_at", "DESC", "Mới thêm trước"],
  ["created_at", "ASC", "Thêm sớm nhất trước"],
  ["name", "ASC", "Tên A → Z"],
  ["name", "DESC", "Tên Z → A"],
  ["active_jobs", "DESC", "Nhiều việc mở nhất"],
  ["active_jobs", "ASC", "Ít việc mở nhất"],
  ["last_run_at", "DESC", "Build gần đây nhất"],
  ["last_run_at", "ASC", "Lâu chưa build nhất"],
]

/** File CSV mẫu (50 doanh nghiệp lớn của Việt Nam), đúng định dạng nạp danh sách. */
const SAMPLE_CSV_URL = "/api/companies/import/sample.csv"

const EMPTY_COMPANY = { name: "", tax_code: "", website: "", careers_url: "", note: "" }

/** Giá trị "không gắn hệ thống ngoài" của ô chọn trong hộp thoại nạp danh sách. */
const NO_CLIENT = "none"

function enqueueMessage(r: EnqueueResult) {
  return `Đã đưa ${num(r.enqueued)}/${num(r.selected)} doanh nghiệp vào hàng đợi build${
    r.already_queued ? ` (${num(r.already_queued)} đã có lượt build đang chờ hoặc đang chạy)` : ""
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
      if (r.enqueued) toast.success(enqueueMessage(r))
      else if (r.selected) toast.warning("Các doanh nghiệp đã chọn đều đang có lượt build chờ hoặc đang chạy")
      else toast.warning("Không có doanh nghiệp nào khớp để build")
      client.invalidateQueries({ queryKey: getGetCompaniesQueryKey() })
      client.invalidateQueries({ queryKey: getGetQueueQueryKey() })
      onDone?.()
    },
  })
}

/** Hộp thoại build nhiều doanh nghiệp: lâu chưa build (theo số giờ) hoặc tất cả; đếm trước số doanh nghiệp khớp
 * để không bấm mà không có gì được xếp (vd. ngay sau build đêm, mọi doanh nghiệp đều vừa build). */
function BuildDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [mode, setMode] = useState<"stale" | "all">("stale")
  const [hours, setHours] = useState("24")
  const staleHours = Number(hours) || 24
  const enqueue = useEnqueue(() => onOpenChange(false))
  const preview = useGetEnqueuePreview(mode === "all" ? undefined : { stale_hours: staleHours }, {
    query: { enabled: open },
  })
  const matched = preview.data?.selected
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Build doanh nghiệp</DialogTitle>
          <DialogDescription>
            Mỗi doanh nghiệp một lượt build (tìm nguồn, đọc đủ site, ghi lại bộ khung) trong hàng đợi build; doanh nghiệp
            đang chờ hoặc đang build không bị nạp trùng. Poll không cần bấm: tự chạy theo chu kỳ trong khung giờ poll.
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
              <div className="text-sm font-medium">Doanh nghiệp lâu chưa build</div>
              <NumberChip prefix="Chưa có lượt build xong trong" suffix="giờ" value={hours} onChange={setHours} min={1} />
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
              <div className="text-meta text-muted-foreground">Build lại toàn bộ danh sách, có thể mất nhiều giờ.</div>
            </div>
          </label>
        </RadioGroup>
        <p className={cn("text-sm", matched === 0 ? "text-warning-ink" : "text-muted-foreground")}>
          {matched === undefined
            ? "Đang đếm doanh nghiệp…"
            : matched > 0
              ? `Khớp ${num(matched)} doanh nghiệp.`
              : mode === "all"
                ? "Chưa có doanh nghiệp nào."
                : `Mọi doanh nghiệp đã có lượt build xong trong ${num(staleHours)} giờ qua (build đêm). Giảm số giờ hoặc chọn Tất cả doanh nghiệp.`}
        </p>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Huỷ
          </Button>
          <Button
            disabled={enqueue.isPending || !matched}
            onClick={() => enqueue.mutate(mode === "all" ? { all: true } : { stale_hours: staleHours })}
          >
            <Play />{" "}
            {enqueue.isPending
              ? "Đang đưa vào hàng đợi build…"
              : matched
                ? `Build ${num(matched)} doanh nghiệp`
                : "Đưa vào hàng đợi build"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Hộp thoại thêm một doanh nghiệp bằng tay (như một dòng của companies.csv); lỗi hiện trong hộp thoại. */
function CreateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [form, setForm] = useState(EMPTY_COMPANY)
  const [enqueue, setEnqueue] = useState(false)
  const set = (key: keyof typeof EMPTY_COMPANY) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [key]: e.target.value }))
  const mutation = usePostCompany({
    mutation: {
      onError: () => {}, // hiện dưới các ô thay cho toast chung
      onSuccess: (r, { data }) => {
        const name = data.name.trim()
        const view = { label: "Xem", onClick: () => navigate(`/companies/${r.id}`) }
        if (r.created) toast.success(`Đã thêm ${name}${r.queued ? " và đưa vào hàng đợi" : ""}`, { action: view })
        else toast.info(`${name} đã có trong danh sách: đã bổ sung thông tin còn trống`, { action: view })
        for (const w of r.warnings) toast.warning(cap(w))
        client.invalidateQueries({ queryKey: getGetCompaniesQueryKey() })
        if (r.queued) client.invalidateQueries({ queryKey: getGetQueueQueryKey() })
        close()
      },
    },
  })
  const close = () => {
    onOpenChange(false)
    setForm(EMPTY_COMPANY)
    setEnqueue(false)
    mutation.reset()
  }
  const name = form.name.trim()

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? onOpenChange(true) : close())}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Thêm doanh nghiệp</DialogTitle>
          <DialogDescription>
            Đã có doanh nghiệp cùng mã số thuế, hoặc cùng tên mà không khác mã số thuế / website, thì không tạo trùng mà chỉ
            bổ sung thông tin còn trống.
          </DialogDescription>
        </DialogHeader>
        <form
          id="create-form"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (name && !mutation.isPending) mutation.mutate({ data: { ...form, name, enqueue } })
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="company-name">Tên doanh nghiệp</Label>
            <Input
              id="company-name"
              value={form.name}
              onChange={set("name")}
              autoFocus
              required
              autoComplete="off"
              placeholder="Công ty Cổ phần ABC"
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="company-tax">Mã số thuế</Label>
              <Input
                id="company-tax"
                value={form.tax_code}
                onChange={set("tax_code")}
                autoComplete="off"
                spellCheck={false}
                placeholder="0101234567"
                className="tabular-nums"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="company-website">Website</Label>
              <Input
                id="company-website"
                value={form.website}
                onChange={set("website")}
                autoComplete="off"
                spellCheck={false}
                placeholder="abc.vn"
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="company-careers">Trang tuyển dụng</Label>
            <Input
              id="company-careers"
              value={form.careers_url}
              onChange={set("careers_url")}
              autoComplete="off"
              spellCheck={false}
              placeholder="abc.vn/tuyen-dung (nếu biết, tìm nguồn nhanh hơn)"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="company-note">Ghi chú</Label>
            <Textarea id="company-note" value={form.note} onChange={set("note")} rows={2} />
          </div>
          <div className="flex items-center gap-2.5">
            <Checkbox id="create-enqueue" checked={enqueue} onCheckedChange={(v) => setEnqueue(v === true)} />
            <Label htmlFor="create-enqueue" className="font-normal">
              Đưa vào hàng đợi ngay sau khi thêm
            </Label>
          </div>
          {mutation.error && (
            <p role="alert" className="text-meta font-medium text-danger-ink">
              {errorMessage(mutation.error)}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Huỷ
          </Button>
          <Button type="submit" form="create-form" disabled={!name || mutation.isPending}>
            <Plus /> {mutation.isPending ? "Đang thêm…" : "Thêm doanh nghiệp"}
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
  const [clientId, setClientId] = useState(NO_CLIENT)
  const clients = useGetApiClients({ limit: 100, sortBy: "name", sortOrder: "ASC" }, { query: { enabled: open } })
  const choices = clients.data?.data ?? []
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
            <code className="font-mono">website</code>, <code className="font-mono">careers_url</code>,{" "}
            <code className="font-mono">note</code>. Nạp lại cùng file không tạo trùng doanh nghiệp.
          </DialogDescription>
        </DialogHeader>
        <form
          id="import-form"
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (file)
              mutation.mutate({ data: { file, enqueue, ...(clientId !== NO_CLIENT ? { client_id: clientId } : {}) } })
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
          {choices.length > 0 && (
            <div className="space-y-1.5">
              <Label htmlFor="import-client">Gắn mã cho hệ thống ngoài</Label>
              <Select value={clientId} onValueChange={setClientId}>
                <SelectTrigger id="import-client" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_CLIENT}>Không (nạp thường)</SelectItem>
                  {choices.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {clientId !== NO_CLIENT && (
                <p className="text-meta text-muted-foreground">
                  Mỗi dòng cần cột <code className="font-mono">external_id</code> (mã doanh nghiệp bên hệ thống đó); cột{" "}
                  <code className="font-mono">active</code> = 0 để ngừng theo dõi.
                </p>
              )}
            </div>
          )}
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
        <DialogFooter className="sm:justify-between">
          <Button variant="ghost" asChild>
            <a href={SAMPLE_CSV_URL} download title="50 doanh nghiệp lớn của Việt Nam (tên + website), đúng định dạng để nạp">
              <Download /> Tải CSV mẫu
            </a>
          </Button>
          <div className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {r ? "Đóng" : "Huỷ"}
            </Button>
            <Button type="submit" form="import-form" disabled={!file || mutation.isPending}>
              <Upload /> {mutation.isPending ? "Đang nạp…" : "Nạp danh sách"}
            </Button>
          </div>
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
  if (!row.last_status) return <span className="text-meta text-muted-foreground">Chưa build</span>
  return (
    <div className="flex flex-col items-start gap-1">
      <RunStatusBadge value={row.last_status} />
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
  const [search, setSearch] = useState(filters.search)
  useEffect(() => setSearch(filters.search), [filters.search])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [buildOpen, setBuildOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const rowLink = useRowLink()

  const meta = useGetMeta({ query: { staleTime: 300_000 } })
  const sort: SortState<GetCompaniesSortBy> = {
    sortBy: filters.sortBy as GetCompaniesSortBy,
    sortOrder: filters.sortOrder === "ASC" ? "ASC" : "DESC",
  }
  const { pageSize, ...rest } = filters
  const query = useGetCompanies(
    { ...rest, ...sort, page, limit: pageLimit(pageSize) },
    { query: { placeholderData: (prev) => prev } },
  )
  const onSort = (column: GetCompaniesSortBy) => update(nextSort(sort, column))

  // Đổi bộ lọc thì bỏ chọn (các dòng đã chọn không còn trên trang); chuyển trang thì giữ.
  const update = (changes: FilterChanges<keyof typeof FILTERS>) => {
    setFilters(changes)
    if (!("page" in changes)) setSelected(new Set())
  }

  const enqueue = useEnqueue(() => setSelected(new Set()))

  const items = query.data?.data ?? []
  const allChecked = items.length > 0 && items.every((r) => selected.has(r.company.id))
  const someChecked = !allChecked && items.some((r) => selected.has(r.company.id))
  // Chỉ thêm / bỏ các dòng của trang này: dòng đã chọn ở trang khác giữ nguyên.
  const toggleAll = () =>
    setSelected((prev) => {
      const next = new Set(prev)
      for (const r of items) {
        if (allChecked) next.delete(r.company.id)
        else next.add(r.company.id)
      }
      return next
    })
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const filtered = isFiltered("pageSize", "sortBy", "sortOrder")

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
            <Button variant="outline" onClick={() => setCreateOpen(true)}>
              <Plus /> Thêm doanh nghiệp
            </Button>
            <Button variant="outline" onClick={() => setImportOpen(true)}>
              <Upload /> Nạp danh sách
            </Button>
            <Button onClick={() => setBuildOpen(true)}>
              <Play /> Build…
            </Button>
          </>
        }
      />
      <BuildDialog open={buildOpen} onOpenChange={setBuildOpen} />
      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} />

      <Section bodyClassName="p-0">
        <FilterBar
          search={
            <SearchBox
              value={search}
              onChange={setSearch}
              onSubmit={(v) => update({ search: (v ?? search).trim() })}
              placeholder="Tìm theo tên, MST, domain hoặc tên gọi khác"
            />
          }
          end={<SortSelect sort={sort} onChange={update} options={SORTS} />}
        >
          <FilterSelect
            label="KYC"
            value={filters.kyc}
            onChange={(v) => update({ kyc: v })}
            options={[...(meta.data?.kyc_statuses ?? []), "none"]}
          />
          <FilterSelect
            label="Build gần nhất"
            labels={runStatusLabel}
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
                            <RunStatusBadge value={row.last_status} />
                            <span className="text-meta text-muted-foreground">{cap(ago(row.last_started))}</span>
                            <IncompleteSources sources={row.last_incomplete} />
                          </>
                        ) : (
                          <span className="text-meta text-muted-foreground">Chưa build</span>
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
                    <SortHead column="name" sort={sort} onSort={onSort}>
                      Doanh nghiệp
                    </SortHead>
                    <TableHead>KYC</TableHead>
                    <SortHead column="last_run_at" sort={sort} onSort={onSort}>
                      Build gần nhất
                    </SortHead>
                    <SortHead column="active_jobs" sort={sort} onSort={onSort} className="text-right">
                      Việc đang mở
                    </SortHead>
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
                  <div className="flex flex-wrap justify-center gap-2">
                    <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)}>
                      <Plus /> Thêm doanh nghiệp
                    </Button>
                    <Button size="sm" onClick={() => setImportOpen(true)}>
                      <Upload /> Nạp danh sách
                    </Button>
                  </div>
                )
              }
            />
          ))}
        {query.data && (
          <Pager {...query.data} onPage={(p) => update({ page: p })} onLimit={(n) => update({ pageSize: String(n) })} />
        )}
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
              <Play /> Build ngay
            </Button>
          </div>
        </div>
      )}
    </>
  )
}
