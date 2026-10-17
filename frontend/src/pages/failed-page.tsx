import { useQuery } from "@tanstack/react-query"
import { Download, ExternalLinkIcon } from "lucide-react"
import { Link, useParams } from "react-router"

import { ErrorState, Loading, Meta, PageHeader, Section } from "@/components/common"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { dt, num } from "@/lib/format"
import type { FailedPage } from "@/lib/types"

export function FailedPagePage() {
  const { id } = useParams()
  const query = useQuery({ queryKey: ["failed-page", id], queryFn: () => api.get<FailedPage>(`/failed-pages/${id}`) })
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return <Loading />
  const p = query.data

  return (
    <>
      <PageHeader
        title={`Trang parse lỗi #${p.id}`}
        description={`${p.source} · ${dt(p.created_at)} · HTTP ${p.http_status ?? "–"}`}
        actions={
          <>
            {p.run_id && (
              <Button variant="outline" size="sm" asChild>
                <Link to={`/runs/${p.run_id}`}>Lượt chạy</Link>
              </Button>
            )}
            <Button variant="outline" size="sm" asChild>
              <a href={p.url} target="_blank" rel="noopener noreferrer">
                <ExternalLinkIcon /> Trang gốc
              </a>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <a href={`/api/failed-pages/${p.id}/raw`}>
                <Download /> Tải HTML
              </a>
            </Button>
          </>
        }
      />
      <div className="space-y-6">
        <Section>
          <Meta
            items={[
              ["URL", <span className="break-all">{p.url}</span>],
              ["Lý do", <code className="text-xs">{p.reason}</code>],
              ["Kích thước", `${num(p.html.length)} ký tự (lưu nén zstd, xoá sau 7 ngày)`],
            ]}
          />
        </Section>
        <Section title="HTML" description="Hiển thị dạng chữ, không chạy nội dung trang">
          <pre className="max-h-[70vh] overflow-auto rounded-md bg-muted p-3 font-mono text-xs break-all whitespace-pre-wrap">
            {p.html.slice(0, 200_000)}
          </pre>
        </Section>
      </div>
    </>
  )
}
