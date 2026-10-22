import { Download, ExternalLinkIcon, History } from "lucide-react"
import { Link, useParams } from "react-router"

import { ErrorState, ExternalLink, Meta, PageHeader, PageLoading, Section, StatusBadge } from "@/components/common"
import { runName } from "@/components/crawl"
import { Button } from "@/components/ui/button"
import { useGetFailedPage } from "@/lib/gen/queries"
import { dt, failReason, num, sourceLabel } from "@/lib/format"

export function FailedPagePage() {
  const { id = "" } = useParams()
  const query = useGetFailedPage(id)
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  if (!query.data) return <PageLoading />
  const p = query.data

  return (
    <>
      <PageHeader
        crumbs={p.run_id ? [{ to: `/runs/${p.run_id}`, label: runName(p.run_kind) }] : undefined}
        title="Trang parse lỗi"
        description={failReason(p.reason)}
        actions={
          <>
            {p.run_id && (
              <Button variant="outline" asChild>
                <Link to={`/runs/${p.run_id}`}>
                  <History /> Xem {runName(p.run_kind).toLowerCase()}
                </Link>
              </Button>
            )}
            <Button variant="outline" asChild>
              <a href={p.url} target="_blank" rel="noopener noreferrer">
                <ExternalLinkIcon /> Trang gốc
              </a>
            </Button>
            <Button variant="outline" asChild>
              <a href={`/api/failed-pages/${encodeURIComponent(p.id)}/raw`}>
                <Download /> Tải HTML
              </a>
            </Button>
          </>
        }
      >
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-sm text-muted-foreground">
          <StatusBadge
            value={null}
            tone={p.http_status && p.http_status < 400 ? "muted" : "bad"}
            text={p.http_status ? `HTTP ${p.http_status}` : "Không có mã HTTP"}
          />
          <span>{sourceLabel(p.source)}</span>
          <span>· {dt(p.created_at)}</span>
        </div>
      </PageHeader>
      <div className="space-y-6">
        <Section>
          <Meta
            items={[
              [
                "URL",
                <ExternalLink href={p.url} className="break-all">
                  {p.url}
                </ExternalLink>,
              ],
              ["Lý do", failReason(p.reason)],
              ["Mã lý do", <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-meta">{p.reason.split(":")[0]}</code>],
              ["Kích thước", `${num(p.html.length)} ký tự, lưu nén zstd và tự xoá sau 7 ngày`],
            ]}
          />
        </Section>
        <Section title="Mã HTML" description="Hiển thị dạng chữ, không chạy nội dung của trang" bodyClassName="p-0">
          <pre className="max-h-[70vh] overflow-auto bg-muted/40 p-5 font-mono text-meta leading-relaxed break-all whitespace-pre-wrap">
            {p.html.slice(0, 200_000)}
          </pre>
        </Section>
      </div>
    </>
  )
}
