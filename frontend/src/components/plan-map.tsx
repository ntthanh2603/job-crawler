// Bản đồ bộ khung của một nguồn: nguồn → trang danh sách / danh mục (trang sau gom dưới trang đầu)
// → API mà trang đó gọi → tập mã tin. Bộ khung chỉ ghi đường đi, không ghi vị trí tin trong trang.
import { ArrowDown, Globe, Layers } from "lucide-react"
import type { ReactNode } from "react"

import { ExternalLink, Tag } from "@/components/common"
import { apiText, Kind, pageText } from "@/components/plan-flow"
import { num, shortUrl, sourceLabel } from "@/lib/format"
import type { CrawlPlan } from "@/lib/types"
import { cn } from "@/lib/utils"

type PlanApi = CrawlPlan["apis"][number]
type PlanPage = CrawlPlan["listings"][number]

/** Một trang của bộ khung, các trang sau của nó và các API nó gọi. */
export type PageGroup = { page: PlanPage; more: PlanPage[]; apis: PlanApi[] }

export type PlanTree = { groups: PageGroup[]; loose: PlanApi[] }

// Tham số / đoạn đường dẫn số trang thường gặp (như PAGE_PARAMS ở backend/common/pages.py).
const PAGE_KEYS = new Set(["page", "p", "pg", "paged", "trang", "offset", "start"])
const PAGE_PATH = /\/page\/(\d+)\/?$/i

/** URL bỏ số trang ("?page=2", "/page/2/"): các trang sau cùng khoá với trang đầu. */
export function pageGroupKey(url: string): string {
  try {
    const u = new URL(url)
    for (const key of [...u.searchParams.keys()]) if (PAGE_KEYS.has(key.toLowerCase())) u.searchParams.delete(key)
    u.pathname = u.pathname.replace(PAGE_PATH, "").replace(/\/+$/, "") || "/"
    u.hash = ""
    return u.toString()
  } catch {
    return url
  }
}

/** "Trang 2" cho URL có số trang; không thì URL rút gọn. */
export function pageLabel(url: string): string {
  try {
    const u = new URL(url)
    for (const [key, value] of u.searchParams) if (PAGE_KEYS.has(key.toLowerCase()) && /^\d+$/.test(value)) return `Trang ${value}`
    const m = PAGE_PATH.exec(u.pathname)
    if (m) return `Trang ${m[1]}`
  } catch {
    // URL lạ: hiện nguyên văn.
  }
  return shortUrl(url)
}

/** Gom trang theo thứ tự đọc: trang sau vào nhóm của trang đầu, API vào trang đã gọi nó
 * (`listing`); API không rõ trang (hoặc trang không còn trong bộ khung) đứng riêng. */
export function planTree(plan: CrawlPlan): PlanTree {
  const groups: PageGroup[] = []
  const byKey = new Map<string, PageGroup>()
  const byUrl = new Map<string, PageGroup>()
  for (const page of plan.listings) {
    const key = `${page.hub ? "hub" : "list"} ${pageGroupKey(page.url)}`
    let group = byKey.get(key)
    if (group) group.more.push(page)
    else {
      group = { page, more: [], apis: [] }
      byKey.set(key, group)
      groups.push(group)
    }
    byUrl.set(page.url, group)
  }
  const loose: PlanApi[] = []
  for (const api of plan.apis) {
    const group = api.listing ? byUrl.get(api.listing) : undefined
    if (group) group.apis.push(api)
    else loose.push(api)
  }
  return { groups, loose }
}

// --- vẽ cây ----------------------------------------------------------------------------------------

/** Nhánh con: đường dọc bên trái, mỗi mục một đoạn ngang nối vào (mục cuối cắt đường dọc). */
function Branch({ children, className }: { children: ReactNode; className?: string }) {
  return <ul className={cn("mt-2 space-y-2", className)}>{children}</ul>
}

function Leaf({ children }: { children: ReactNode }) {
  return (
    // Đường dọc bắt đầu từ khe phía trên (`-top-2` = `mt-2` / `space-y-2`) để nối liền các mục.
    <li className="relative pl-6 before:absolute before:-top-2 before:bottom-0 before:left-2.5 before:border-l before:border-muted-foreground/35 after:absolute after:top-4.5 after:left-2.5 after:w-3 after:border-t after:border-muted-foreground/35 last:before:bottom-auto last:before:h-6.5">
      {children}
    </li>
  )
}

function Node({
  kind,
  url,
  text,
  tags,
  dim,
  children,
}: {
  kind: ReactNode
  url?: string
  text: ReactNode
  tags?: ReactNode
  dim?: boolean
  children?: ReactNode
}) {
  return (
    <div className={cn("min-w-0 rounded-lg border bg-card px-3 py-2 shadow-card", dim && "bg-muted/40")}>
      <div className="flex min-w-0 items-start gap-2.5">
        {kind}
        <div className="min-w-0 flex-1">
          {url ? (
            <ExternalLink href={url} title={url} className="block truncate text-meta font-medium">
              {shortUrl(url)}
            </ExternalLink>
          ) : null}
          <div className="text-caption text-muted-foreground">{text}</div>
          {tags && <div className="mt-1 flex flex-wrap gap-1">{tags}</div>}
          {children}
        </div>
      </div>
    </div>
  )
}

function ApiNode({ api, replaces }: { api: PlanApi; replaces?: boolean }) {
  return (
    <Node
      kind={<Kind className="bg-info/12 text-info-ink">{api.embedded ? "JSON" : api.method}</Kind>}
      url={api.url}
      text={apiText(api)}
      dim={api.embedded}
      tags={replaces ? <Tag className="bg-info/12 text-info-ink">Thay cho trang</Tag> : undefined}
    />
  )
}

function PageNode({ group }: { group: PageGroup }) {
  const { page, more, apis } = group
  return (
    <>
      <Node
        kind={<Kind>{page.hub ? "Danh mục" : "Trang"}</Kind>}
        url={page.url}
        text={pageText(page)}
        dim={page.api_only}
        tags={
          page.rendered || page.api_only ? (
            <>
              {page.rendered && <Tag title="Lượt build phải mở bằng trình duyệt">Cần trình duyệt</Tag>}
              {page.api_only && <Tag title="Mọi tin có trong API bắt được">API thay</Tag>}
            </>
          ) : undefined
        }
      >
        {more.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-1 text-caption text-muted-foreground">
            <span>+{num(more.length)} trang sau:</span>
            {more.map((p) => (
              <ExternalLink key={p.url} href={p.url} title={p.url} className="rounded bg-secondary px-1.5 py-px text-secondary-foreground">
                {pageLabel(p.url)}
              </ExternalLink>
            ))}
          </div>
        )}
      </Node>
      {apis.length > 0 && (
        <Branch>
          {apis.map((a, i) => (
            <Leaf key={`${a.url}-${i}`}>
              <ApiNode api={a} replaces={page.api_only && !a.embedded} />
            </Leaf>
          ))}
        </Branch>
      )}
    </>
  )
}

/** Ô kết quả cuối bản đồ; bấm để mở tab tương ứng phía dưới. */
function Output({ children, onClick, className }: { children: ReactNode; onClick?: () => void; className?: string }) {
  const body = <span className="text-meta">{children}</span>
  if (!onClick) return <span className={cn("rounded-md border bg-card px-2.5 py-1", className)}>{body}</span>
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md border bg-card px-2.5 py-1 text-left transition-colors hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
        className,
      )}
    >
      {body}
    </button>
  )
}

/** Cây của bộ khung một nguồn. `onShow(tab)` mở tab chi tiết (mã tin, link bỏ qua, danh mục). */
export function PlanMap({
  plan,
  names,
  onShow,
}: {
  plan: CrawlPlan
  names: number
  onShow?: (tab: "jobs" | "ignored" | "names") => void
}) {
  const tree = planTree(plan)
  const target = plan.source.includes(":") ? plan.source.split(":").slice(1).join(":") : plan.source
  const hidden = plan.listings_total - plan.listings.length + (plan.apis_total - plan.apis.length)
  return (
    <div className="space-y-3" aria-label="Bản đồ bộ khung">
      <div>
        <Node
          kind={
            <span className="grid size-5 shrink-0 place-items-center rounded-md bg-brand/12 text-brand">
              {plan.ats ? <Layers className="size-3.5" /> : <Globe className="size-3.5" />}
            </span>
          }
          url={plan.url}
          text={
            plan.ats
              ? `Bảng tin ${sourceLabel(plan.ats)} · ${target}`
              : [
                  `Trang careers · ${target}`,
                  plan.listings_total ? `${num(plan.listings_total)} trang` : null,
                  plan.apis_total ? `${num(plan.apis_total)} API` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")
          }
        />
        <Branch>
          {plan.ats ? (
            <Leaf>
              <Node
                kind={<Kind className="bg-info/12 text-info-ink">API</Kind>}
                text={`Gọi API bảng tin của ${sourceLabel(plan.ats)} · cần ${num(Math.max(plan.pages_needed, 1))} trang để đọc trọn`}
              />
            </Leaf>
          ) : null}
          {tree.loose.map((a, i) => (
            <Leaf key={`loose-${a.url}-${i}`}>
              <ApiNode api={a} />
            </Leaf>
          ))}
          {tree.groups.map((g) => (
            <Leaf key={g.page.url}>
              <PageNode group={g} />
            </Leaf>
          ))}
        </Branch>
        {hidden > 0 && <p className="mt-2 pl-6 text-caption text-muted-foreground">+{num(hidden)} trang / API nữa (xem JSON gốc)</p>}
      </div>

      <div className="flex items-center gap-2 pl-1 text-caption text-muted-foreground">
        <ArrowDown className="size-4 shrink-0" />
        Mỗi lượt, tin trên các trang / API này được nhận ra lại bằng quy tắc chung (JSON-LD, dạng URL tin, mảng tin trong
        JSON), rồi so với tập mã tin
      </div>

      <div className="flex flex-wrap gap-2">
        <Output onClick={onShow && (() => onShow("jobs"))} className="border-brand/30 bg-brand/5">
          <span className="font-semibold text-foreground tabular-nums">{num(plan.jobs)}</span> mã tin
          {plan.indirect > 0 && <span className="text-muted-foreground"> · {num(plan.indirect)} gián tiếp</span>}
        </Output>
        {plan.ignored > 0 && (
          <Output onClick={onShow && (() => onShow("ignored"))}>
            <span className="font-semibold tabular-nums">{num(plan.ignored)}</span> link bỏ qua
          </Output>
        )}
        {names > 0 && (
          <Output onClick={onShow && (() => onShow("names"))}>
            <span className="font-semibold tabular-nums">{num(names)}</span> mã → tên
          </Output>
        )}
      </div>
    </div>
  )
}
