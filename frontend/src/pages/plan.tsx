// Trang một bộ khung (`/plans/:id`): bản đồ trang / API, đường một lượt poll, các mã tin (kèm tin đã
// lưu), link bỏ qua, danh mục mã → tên và JSON gốc của `crawl_plans.spec`.
import { Link2Off, ListTree } from "lucide-react"
import { useState } from "react"
import { useParams } from "react-router"

import {
  Empty,
  ErrorState,
  ExternalLink,
  PageHeader,
  PageLoading,
  Section,
  StatusBadge,
  Tag,
  TabBar,
  TextLink,
  useTabParam,
  type Tone,
} from "@/components/common"
import { PLAN_STATUS } from "@/components/crawl"
import { PlanFlow } from "@/components/plan-flow"
import { PlanMap } from "@/components/plan-map"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { ago, cap, num, shortUrl, sourceLabel } from "@/lib/format"
import { useGetCrawlPlan } from "@/lib/gen/queries"
import type { PlanDetail } from "@/lib/types"

const TABS = ["jobs", "ignored", "names", "json"] as const

const STEP = 100

function PlanJobs({ d }: { d: PlanDetail }) {
  const [shown, setShown] = useState(STEP)
  if (!d.jobs.length)
    return (
      <Section>
        <Empty icon={<ListTree />} title="Chưa có mã tin">
          Lượt đọc trọn gần nhất của nguồn này không thấy tin nào.
        </Empty>
      </Section>
    )
  const saved = d.jobs.filter((j) => j.url !== null).length
  return (
    <Section
      bodyClassName="p-0"
      title="Mã tin trong bộ khung"
      description={`Lượt poll so tập mã này với lượt trước để biết tin mới / tin gỡ · ${num(saved)}/${num(d.jobs.length)} mã có tin đã lưu`}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Tin</TableHead>
            <TableHead>Trạng thái</TableHead>
            <TableHead>Mã ở nguồn</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {d.jobs.slice(0, shown).map((j) => (
            <TableRow key={j.key}>
              <TableCell className="max-w-[28rem] min-w-56 whitespace-normal">
                {j.job_id ? (
                  <TextLink to={`/jobs/${j.job_id}`} className="font-medium">
                    {cap(j.title || "Việc không có tiêu đề")}
                  </TextLink>
                ) : (
                  <span className="text-muted-foreground">{j.url ? "Chưa gộp thành việc" : "Chưa lưu tin"}</span>
                )}
                {j.url && (
                  <ExternalLink href={j.url} title={j.url} className="block truncate text-meta">
                    {shortUrl(j.url)}
                  </ExternalLink>
                )}
              </TableCell>
              <TableCell className="align-top">
                <span className="inline-flex flex-wrap items-center gap-1">
                  {j.accepted === false ? (
                    <StatusBadge value="failed" tone="bad" text="Bị loại" />
                  ) : j.status ? (
                    <StatusBadge value={j.status} />
                  ) : (
                    <span className="text-muted-foreground">–</span>
                  )}
                  {j.indirect && (
                    <Tag title="Chỉ tới được qua trang tin khác: lượt poll không thấy lại vẫn giữ, lượt build đêm mới đóng">
                      Gián tiếp
                    </Tag>
                  )}
                </span>
              </TableCell>
              <TableCell className="align-top font-mono text-caption text-muted-foreground">{j.key}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {(d.jobs.length > shown || d.jobs_total > d.jobs.length) && (
        <div className="flex flex-wrap items-center gap-3 border-t px-5 py-3 text-meta text-muted-foreground">
          {d.jobs.length > shown && (
            <Button variant="outline" size="sm" onClick={() => setShown((n) => n + STEP)}>
              Xem thêm {num(Math.min(STEP, d.jobs.length - shown))} mã
            </Button>
          )}
          {d.jobs_total > d.jobs.length && <span>+{num(d.jobs_total - d.jobs.length)} mã nữa không hiện ở đây</span>}
        </div>
      )}
    </Section>
  )
}

function PlanIgnored({ links }: { links: string[] }) {
  if (!links.length)
    return (
      <Section>
        <Empty icon={<Link2Off />} title="Không có link bỏ qua">
          Lượt build chưa gặp link nào trên trang danh sách mà mở ra không phải tin.
        </Empty>
      </Section>
    )
  return (
    <Section
      bodyClassName="p-0"
      title="Link bỏ qua"
      description="Link trên trang danh sách mà lượt build đã mở và thấy không phải tin: lượt poll không mở lại"
    >
      <ul className="divide-y">
        {links.map((u) => (
          <li key={u} className="min-w-0 px-5 py-2">
            <ExternalLink href={u} title={u} className="block truncate text-meta">
              {shortUrl(u)}
            </ExternalLink>
          </li>
        ))}
      </ul>
    </Section>
  )
}

function PlanNames({ d }: { d: PlanDetail }) {
  if (!d.names.length)
    return (
      <Section>
        <Empty icon={<ListTree />} title="Không có danh mục mã → tên">
          Trang không tải kèm danh mục nào (tỉnh, phòng ban...) để đọc tin từ API.
        </Empty>
      </Section>
    )
  return (
    <Section
      bodyClassName="p-0"
      title="Danh mục mã → tên"
      description={`Trang tải kèm khi build; lượt poll gọi thẳng API nên đọc nơi làm / phòng ban của tin mới theo đây${
        d.names_total > d.names.length ? ` · hiện ${num(d.names.length)}/${num(d.names_total)}` : ""
      }`}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Loại</TableHead>
            <TableHead>Mã</TableHead>
            <TableHead>Tên</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {d.names.map((n, i) => (
            <TableRow key={`${n.kind}-${n.id}-${i}`}>
              <TableCell className="text-meta text-muted-foreground">{n.kind}</TableCell>
              <TableCell className="font-mono text-caption">{n.id}</TableCell>
              <TableCell className="whitespace-normal">{n.name}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Section>
  )
}

export function PlanPage() {
  const { id = "" } = useParams()
  const [tab, setTab] = useTabParam(TABS, "jobs")
  const query = useGetCrawlPlan(id, { query: { refetchInterval: 15_000 } })
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data) return <PageLoading />
  const d = query.data
  const p = d.plan
  const st = PLAN_STATUS[p.status] ?? { label: cap(p.status), tone: "muted" as Tone }
  const target = p.source.includes(":") ? p.source.split(":").slice(1).join(":") : ""
  const show = (t: string) => {
    setTab(t)
    document.getElementById("plan-memory")?.scrollIntoView({ behavior: "smooth", block: "start" })
  }
  return (
    <>
      <PageHeader
        crumbs={[
          { to: "/companies", label: "Doanh nghiệp" },
          { to: `/companies/${d.company_id}?tab=crawl`, label: d.company_name },
        ]}
        title={`Bộ khung ${sourceLabel(p.kind || p.source)}${target ? ` · ${target}` : ""}`}
        description={
          <ExternalLink href={p.url} className="break-all">
            {shortUrl(p.url)}
          </ExternalLink>
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-meta text-muted-foreground">
          <StatusBadge value={p.status} tone={st.tone} text={st.label} />
          <span>Build {ago(p.built_at)}</span>
          <span>{p.polled_at ? `Poll ${ago(p.polled_at)}` : "Chưa poll"}</span>
          {p.changed_at && <span>Thay đổi {ago(p.changed_at)}</span>}
          {p.failures > 0 && <span className="font-medium text-warning-ink">{num(p.failures)} lỗi liên tiếp</span>}
        </div>
      </PageHeader>

      <div className="space-y-6">
        <Section
          title="Bản đồ bộ khung"
          description="Lượt build ghi đường đi: trang nào mở, API nào gọi, lật trang thế nào. Không ghi vị trí tin trong trang (không có XPath / selector)."
        >
          <PlanMap plan={p} names={d.names_total} onShow={show} />
        </Section>

        <Section title="Đường một lượt poll" description="Lượt poll ban ngày đi theo bộ khung này">
          {p.status === "retired" ? (
            <p className="text-sm text-muted-foreground">Lượt build gần nhất không còn thấy nguồn này nên lượt poll không đọc nữa.</p>
          ) : p.readable ? (
            <PlanFlow plan={p} limits={d.limits} />
          ) : (
            <p className="text-sm text-warning-ink">
              Bộ khung ghi theo phiên bản cũ nên không đọc lại được: lượt poll tới đánh hỏng và doanh nghiệp được build lại.
            </p>
          )}
          {p.last_error && (
            <pre className="mt-4 overflow-x-auto rounded-lg bg-destructive/6 p-2.5 font-mono text-meta whitespace-pre-wrap text-danger-ink [overflow-wrap:anywhere]">
              {p.last_error}
            </pre>
          )}
        </Section>

        <Tabs id="plan-memory" value={tab} onValueChange={setTab} className="min-w-0 scroll-mt-4 gap-4">
          <TabBar
            items={[
              { value: "jobs", label: "Mã tin", count: d.jobs_total },
              { value: "ignored", label: "Link bỏ qua", count: d.ignored_links.length },
              { value: "names", label: "Mã → tên", count: d.names_total },
              { value: "json", label: "JSON gốc" },
            ]}
          />
          <TabsContent value="jobs">
            <PlanJobs d={d} />
          </TabsContent>
          <TabsContent value="ignored">
            <PlanIgnored links={d.ignored_links} />
          </TabsContent>
          <TabsContent value="names">
            <PlanNames d={d} />
          </TabsContent>
          <TabsContent value="json">
            <Section title="JSON gốc" description="Cột `spec` của bảng crawl_plans, đúng như lượt build ghi">
              <pre className="max-h-[36rem] overflow-auto rounded-lg bg-muted p-3 font-mono text-caption">
                {JSON.stringify(d.spec, null, 2)}
              </pre>
            </Section>
          </TabsContent>
        </Tabs>
      </div>
    </>
  )
}
