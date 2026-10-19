import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { ArrowRight, ChevronRight, History, RotateCw, X } from "lucide-react"
import { useEffect, useMemo, useState, type MouseEvent, type ReactNode } from "react"

import {
  Empty,
  ErrorState,
  FilterBar,
  FilterSelect,
  Hint,
  Loading,
  Meta,
  PageHeader,
  SearchBox,
  Section,
  TextLink,
} from "@/components/common"
import { formatValue } from "@/components/settings/model"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, qs } from "@/lib/api"
import {
  actionLabel,
  actorLabel,
  fieldLabel,
  formatAuditValue,
  hasDetails,
  isMasked,
  KNOWN_ACTIONS,
  RESOURCE_TYPES,
  resourceHref,
  resourceName,
  resourceTypeLabel,
  shortId,
} from "@/lib/audit"
import { ago, cap, dt, num } from "@/lib/format"
import { settingsQuery } from "@/lib/queries"
import type { AuditChange, AuditEvent, AuditPage as AuditData, SettingItem, SettingValue } from "@/lib/types"
import { useUrlFilters } from "@/lib/url-filters"
import { cn } from "@/lib/utils"

/** Bộ lọc trên URL (mặc định: không lọc). `resource_id` chỉ đặt qua link, có chip để bỏ. */
const FILTERS = { action: "", resource_type: "", resource_id: "", actor: "" }
const LIMIT = 50

/** Cài đặt theo khoá (nhãn + cách hiện giá trị) cho dòng nhật ký của trang Cài đặt. */
function useSettingItems(enabled: boolean): Map<string, SettingItem> {
  const query = useQuery({ ...settingsQuery(), enabled })
  return useMemo(
    () => new Map((query.data?.groups ?? []).flatMap((g) => g.items.map((i) => [i.key, i] as const))),
    [query.data],
  )
}

function Resource({ event }: { event: AuditEvent }) {
  if (!event.resource_type) return <span className="text-muted-foreground">–</span>
  const href = resourceHref(event)
  const name = resourceName(event)
  const id = event.resource_id ?? undefined
  const mono = event.resource_type !== "board" && name !== id
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className="text-muted-foreground">{resourceTypeLabel(event.resource_type)}</span>
      {name &&
        (href ? (
          <TextLink to={href} className={cn("text-meta font-medium", mono && "font-mono")}>
            <span title={id}>{name}</span>
          </TextLink>
        ) : (
          <span title={id} className={cn("text-meta", mono && "font-mono")}>
            {name}
          </span>
        ))}
    </span>
  )
}

function Changes({ changes, settings }: { changes: Record<string, AuditChange>; settings: Map<string, SettingItem> }) {
  const show = (key: string, value: unknown) => {
    if (value === null || value === undefined || value === "") return "Trống"
    if (isMasked(value)) return "Ẩn"
    const item = settings.get(key)
    return item ? formatValue(item, value as SettingValue) : formatAuditValue(value, key)
  }
  return (
    <ul className="divide-y rounded-lg border bg-card">
      {Object.entries(changes).map(([key, change]) => {
        // Trường chưa có nhãn (cài đặt mới, trường lạ): hiện nguyên tên khoá.
        const label = settings.get(key)?.label ?? (fieldLabel(key) !== key ? fieldLabel(key) : null)
        const { before, after } = (change ?? {}) as Partial<AuditChange>
        return (
          <li key={key} className="grid gap-1 px-3.5 py-2.5 sm:grid-cols-[minmax(9rem,15rem)_minmax(0,1fr)] sm:gap-5">
            <div className="min-w-0">
              {label && <div className="text-sm font-medium break-words">{cap(label)}</div>}
              <code
                className={cn(
                  "font-mono break-all",
                  label ? "text-caption text-muted-foreground" : "text-meta font-medium text-foreground",
                )}
              >
                {key}
              </code>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <span className="break-words text-muted-foreground [overflow-wrap:anywhere]">{show(key, before)}</span>
              <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-label="thành" />
              <span className="font-medium break-words [overflow-wrap:anywhere]">{show(key, after)}</span>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

function Details({ event }: { event: AuditEvent }) {
  const settings = useSettingItems(event.resource_type === "settings")
  const changes = event.changes && Object.keys(event.changes).length ? event.changes : null
  const meta: [string, ReactNode][] = Object.entries(event.metadata ?? {}).map(([key, value]): [string, ReactNode] => {
    if (key === "company_ids" && Array.isArray(value))
      return [
        fieldLabel(key),
        <span className="inline-flex flex-wrap gap-x-2 gap-y-1">
          {value.map((id) => (
            <TextLink key={String(id)} to={`/companies/${id}`} className="font-mono text-meta">
              <span title={String(id)}>{shortId(String(id))}</span>
            </TextLink>
          ))}
        </span>,
      ]
    if (key === "reset" && Array.isArray(value))
      return [fieldLabel(key), <span>{value.map((k) => settings.get(String(k))?.label ?? String(k)).join(", ")}</span>]
    return [fieldLabel(key), <span className="[overflow-wrap:anywhere]">{formatAuditValue(value, key)}</span>]
  })
  if (event.request_id) meta.push(["Mã request", <code className="font-mono text-meta break-all">{event.request_id}</code>])
  return (
    <div className="space-y-4">
      {changes && (
        <div className="space-y-2">
          <div className="text-meta font-medium text-muted-foreground">Thay đổi</div>
          <Changes changes={changes} settings={settings} />
        </div>
      )}
      {meta.length > 0 && (
        <div className="space-y-2">
          <div className="text-meta font-medium text-muted-foreground">Thông tin kèm theo</div>
          <Meta items={meta} />
        </div>
      )}
    </div>
  )
}

function EventRow({ event, open, onToggle }: { event: AuditEvent; open: boolean; onToggle: () => void }) {
  const details = hasDetails(event)
  const toggleRow = (e: MouseEvent) => {
    if (!details || (e.target as HTMLElement).closest("a,button")) return
    if (window.getSelection()?.toString()) return
    onToggle()
  }
  return (
    <>
      <TableRow className={cn(details && "cursor-pointer")} onClick={toggleRow}>
        <TableCell className="w-10 pr-0 align-top">
          {details && (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-expanded={open}
              aria-label={open ? "Thu gọn chi tiết" : "Xem chi tiết"}
              onClick={onToggle}
            >
              <ChevronRight className={cn("transition-transform", open && "rotate-90")} />
            </Button>
          )}
        </TableCell>
        <TableCell className="align-top whitespace-nowrap tabular-nums">
          <Hint content={dt(event.occurred_at)}>{cap(ago(event.occurred_at))}</Hint>
        </TableCell>
        <TableCell className="align-top whitespace-normal md:min-w-56">
          <div className="font-medium">{actionLabel(event.action)}</div>
          <code className="font-mono text-caption text-muted-foreground">{event.action}</code>
          {/* Màn hình hẹp: người thực hiện và đối tượng nằm dưới tên thao tác. */}
          <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-meta md:hidden">
            <span title={event.actor}>{actorLabel(event.actor)}</span>
            <Resource event={event} />
          </div>
        </TableCell>
        <TableCell className="align-top max-md:hidden">
          <span title={event.actor} className={cn(event.actor === "system" || event.actor === "anonymous" ? "text-muted-foreground" : "")}>
            {actorLabel(event.actor)}
          </span>
        </TableCell>
        <TableCell className="align-top whitespace-normal max-md:hidden">
          <Resource event={event} />
        </TableCell>
      </TableRow>
      {open && details && (
        <TableRow className="bg-muted/30 hover:bg-muted/30">
          <TableCell colSpan={5} className="px-4 py-4 whitespace-normal sm:px-5 md:pl-14">
            <Details event={event} />
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

export function AuditPage() {
  const { filters, update, reset, isFiltered } = useUrlFilters(FILTERS)
  const [actor, setActor] = useState(filters.actor)
  useEffect(() => setActor(filters.actor), [filters.actor])
  const [open, setOpen] = useState<Set<string>>(new Set())

  const actions = useQuery({
    queryKey: ["audit-actions"],
    queryFn: () => api.get<string[]>("/audit/actions"),
    staleTime: 300_000,
  })
  const query = useInfiniteQuery({
    queryKey: ["audit", filters],
    queryFn: ({ pageParam }) => api.get<AuditData>(`/audit${qs({ limit: LIMIT, before: pageParam, ...filters })}`),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next,
  })

  const items = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data])
  const actionOptions = [...new Set([...(actions.data ?? KNOWN_ACTIONS), filters.action].filter(Boolean))].map(
    (a) => [a, actionLabel(a)] as const,
  )
  const typeOptions = [...new Set([...Object.keys(RESOURCE_TYPES), filters.resource_type].filter(Boolean))].map(
    (t) => [t, resourceTypeLabel(t)] as const,
  )
  const filtered = isFiltered()
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <>
      <PageHeader
        title="Nhật ký thao tác"
        description="Ai đã làm gì qua giao diện và API: lưu cài đặt, đưa việc vào hàng đợi, nạp danh sách, kiểm tay… Mới nhất trước."
        actions={
          <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
            <RotateCw className={cn(query.isRefetching && !query.isFetchingNextPage && "animate-spin")} /> Làm mới
          </Button>
        }
      />

      <Section bodyClassName="p-0">
        <FilterBar
          search={
            <SearchBox
              value={actor}
              onChange={setActor}
              onSubmit={(v) => update({ actor: (v ?? actor).trim() })}
              placeholder="Lọc theo tên đăng nhập"
            />
          }
        >
          <FilterSelect label="Thao tác" value={filters.action} onChange={(v) => update({ action: v })} options={actionOptions} />
          <FilterSelect
            label="Đối tượng"
            value={filters.resource_type}
            onChange={(v) => update({ resource_type: v })}
            options={typeOptions}
          />
          {filters.resource_id && (
            <span className="inline-flex h-9 items-center gap-1 rounded-md border border-primary/50 bg-primary/6 pr-1 pl-3 text-sm dark:bg-primary/12">
              <span className="text-muted-foreground">Mã đối tượng</span>
              <span className="max-w-48 truncate font-mono text-meta font-medium" title={filters.resource_id}>
                {shortId(filters.resource_id)}
              </span>
              <Button variant="ghost" size="icon-xs" aria-label="Bỏ lọc theo đối tượng" onClick={() => update({ resource_id: "" })}>
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

        {query.isError && !query.data && (
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
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10">
                    <span className="sr-only">Mở chi tiết</span>
                  </TableHead>
                  <TableHead>Thời điểm</TableHead>
                  <TableHead>Thao tác</TableHead>
                  <TableHead className="max-md:hidden">Người thực hiện</TableHead>
                  <TableHead className="max-md:hidden">Đối tượng</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((e) => (
                  <EventRow key={e.id} event={e} open={open.has(e.id)} onToggle={() => toggle(e.id)} />
                ))}
              </TableBody>
            </Table>
          ) : (
            <Empty
              icon={<History />}
              title={filtered ? "Không có thao tác nào khớp bộ lọc" : "Chưa có thao tác nào được ghi"}
              action={
                filtered && (
                  <Button variant="outline" size="sm" onClick={reset}>
                    Xoá bộ lọc
                  </Button>
                )
              }
            >
              {filtered ? undefined : "Các thao tác như lưu cài đặt, poll ngay, kiểm tay sẽ hiện ở đây."}
            </Empty>
          ))}

        {query.isFetchNextPageError && (
          <div className="border-t p-5">
            <ErrorState error={query.error} onRetry={() => query.fetchNextPage()} />
          </div>
        )}
        {query.data && items.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3 text-meta text-muted-foreground">
            <span className="tabular-nums">
              <span className="font-medium text-foreground">{num(items.length)}</span> thao tác
              {!query.hasNextPage && " · đã hiện hết"}
            </span>
            {query.hasNextPage && (
              <Button variant="outline" size="sm" disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>
                {query.isFetchingNextPage ? "Đang tải…" : "Tải thêm"}
              </Button>
            )}
          </div>
        )}
      </Section>
    </>
  )
}
