import { useMutation, useQuery } from "@tanstack/react-query"
import { Download, FolderOpen, Play } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import {
  Empty,
  FilePicker,
  PageHeader,
  Section,
  StatCard,
  StatGrid,
  TextLink,
} from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { cap, label, pct, sourceLabel } from "@/lib/format"
import type { EvalReport } from "@/lib/types"

export function EvalPage() {
  const [companies, setCompanies] = useState<File | null>(null)
  const [jobs, setJobs] = useState<File | null>(null)
  const [inactive, setInactive] = useState(false)
  const server = useQuery({
    queryKey: ["eval-server"],
    queryFn: () => api.get<{ available: boolean; path: string }>("/eval/server"),
  })
  const run = useMutation({
    mutationFn: (useServer: boolean) => {
      const form = new FormData()
      if (!useServer && companies && jobs) {
        form.append("companies", companies)
        form.append("jobs", jobs)
      }
      form.append("include_inactive", String(inactive))
      return api.form<EvalReport>("/eval", form)
    },
    onError: (e) => toast.error(cap(e instanceof Error ? e.message : String(e))),
  })
  const r = run.data

  const download = () => {
    if (!r) return
    const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" })
    const a = document.createElement("a")
    a.href = URL.createObjectURL(blob)
    a.download = "eval.md"
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <>
      <PageHeader
        title="Golden set"
        description="Precision và recall theo doanh nghiệp và theo nguồn, so với danh sách việc làm đúng do người kiểm cung cấp."
        actions={
          r && (
            <Button variant="outline" size="sm" onClick={download}>
              <Download /> Tải báo cáo
            </Button>
          )
        }
      />

      <Section title="Chạy đánh giá" className="mb-6" bodyClassName="space-y-4">
        <div className="flex flex-wrap items-end gap-4">
          <FilePicker
            file={companies}
            onChange={setCompanies}
            title="Chọn companies.csv"
            hint="Danh sách doanh nghiệp trong golden set"
            className="w-64"
          />
          <FilePicker
            file={jobs}
            onChange={setJobs}
            title="Chọn jobs.csv"
            hint="Các việc làm đúng của từng doanh nghiệp"
            className="w-64"
          />
          <div className="flex items-center gap-2 pb-2">
            <Checkbox id="inactive" checked={inactive} onCheckedChange={(v) => setInactive(v === true)} />
            <Label htmlFor="inactive" className="font-normal">
              So cả việc đã hết hạn hoặc đã gỡ
            </Label>
          </div>
          <Button onClick={() => run.mutate(false)} disabled={!companies || !jobs || run.isPending}>
            <Play /> {run.isPending ? "Đang đánh giá…" : "Đánh giá"}
          </Button>
          {server.data?.available && (
            <Button variant="outline" onClick={() => run.mutate(true)} disabled={run.isPending}>
              <FolderOpen /> Dùng thư mục {server.data.path}/ trên máy chủ
            </Button>
          )}
        </div>
        <p className="max-w-3xl text-meta leading-relaxed text-muted-foreground">
          File <code>companies.csv</code> giống file đầu vào (name, tax_code, website). File <code>jobs.csv</code> gồm cột{" "}
          <code>company</code> (tên hoặc MST) và <code>url</code> hoặc <code>title</code> của các việc làm đúng. Hai việc khớp
          nhau khi URL trùng hoặc tiêu đề giống nhau từ 90% trở lên. Chưa có golden set thì dùng trang{" "}
          <TextLink to="/review" className="text-brand">
            Kiểm tay
          </TextLink>
          .
        </p>
      </Section>

      {!r && !run.isPending && <Empty>Tải golden set lên để xem precision và recall.</Empty>}

      {r && (
        <div className="space-y-6">
          <StatGrid className="grid-cols-2 lg:grid-cols-5">
            <StatCard
              label="Precision"
              value={pct(r.precision)}
              hint={`Mục tiêu ≥ ${pct(r.target_precision, 0)}`}
              status={
                r.precision === null
                  ? undefined
                  : r.precision >= r.target_precision
                    ? { tone: "good", text: "Đạt" }
                    : { tone: "bad", text: "Chưa đạt" }
              }
            />
            <StatCard
              label="Recall"
              value={pct(r.recall)}
              hint={`Mục tiêu ≥ ${pct(r.target_recall, 0)}`}
              status={
                r.recall === null
                  ? undefined
                  : r.recall >= r.target_recall
                    ? { tone: "good", text: "Đạt" }
                    : { tone: "bad", text: "Chưa đạt" }
              }
            />
            <StatCard label="Golden / tìm được / khớp" value={`${r.golden} / ${r.predicted} / ${r.matched}`} hint={`${r.companies.length} doanh nghiệp trong golden set`} />
            {Object.entries(r.perf).map(([k, v]) => (
              <StatCard key={k} label={k} value={v} />
            ))}
          </StatGrid>

          <Section title="Theo doanh nghiệp" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Doanh nghiệp</TableHead>
                  <TableHead>Lượt cuối</TableHead>
                  <TableHead className="text-right">Golden</TableHead>
                  <TableHead className="text-right">Tìm được</TableHead>
                  <TableHead className="text-right">Khớp</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="text-right">Recall</TableHead>
                  <TableHead>Thiếu</TableHead>
                  <TableHead>Thừa</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {r.companies.map((c) => (
                  <TableRow key={c.name}>
                    <TableCell>
                      {c.company_id ? <TextLink to={`/companies/${c.company_id}`}>{cap(c.name)}</TextLink> : cap(c.name)}
                    </TableCell>
                    <TableCell className="text-meta">{c.run_status ? label(c.run_status) : "–"}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.golden}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.predicted}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.matched}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(c.precision)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(c.recall)}</TableCell>
                    <TableCell className="max-w-72 text-meta whitespace-normal">{c.missing.join("; ")}</TableCell>
                    <TableCell className="max-w-72 text-meta whitespace-normal text-muted-foreground">{c.extra.join("; ")}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          <Section title="Theo nguồn" description="Tin trước gộp trùng" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Nguồn</TableHead>
                  <TableHead className="text-right">Tin</TableHead>
                  <TableHead className="text-right">Khớp</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="text-right">Recall (độ phủ)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {r.sources.map((s) => (
                  <TableRow key={s.source}>
                    <TableCell>{sourceLabel(s.source)}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.predicted}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.matched}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(s.precision)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(s.recall)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          {r.warnings.length > 0 && (
            <Alert>
              <AlertTitle>Cảnh báo</AlertTitle>
              <AlertDescription>
                <ul className="list-disc pl-4 text-meta">
                  {r.warnings.map((w) => (
                    <li key={w}>{cap(w)}</li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
        </div>
      )}
    </>
  )
}
