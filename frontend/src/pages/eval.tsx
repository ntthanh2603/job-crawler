import { useMutation, useQuery } from "@tanstack/react-query"
import { Download, FolderOpen, Play } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { Empty, Field, PageHeader, Section, StatCard } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { pct } from "@/lib/format"
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
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
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
        description="Precision/recall theo doanh nghiệp và theo nguồn, so với danh sách job đúng do người kiểm cung cấp."
        actions={
          r && (
            <Button variant="outline" size="sm" onClick={download}>
              <Download /> Báo cáo .md
            </Button>
          )
        }
      />

      <Section title="Chạy đánh giá" className="mb-6" bodyClassName="space-y-4">
        <div className="flex flex-wrap items-end gap-4">
          <Field label="companies.csv">
            <Input type="file" accept=".csv,text/csv" onChange={(e) => setCompanies(e.target.files?.[0] ?? null)} className="w-72" />
          </Field>
          <Field label="jobs.csv">
            <Input type="file" accept=".csv,text/csv" onChange={(e) => setJobs(e.target.files?.[0] ?? null)} className="w-72" />
          </Field>
          <div className="flex items-center gap-2 pb-2">
            <Checkbox id="inactive" checked={inactive} onCheckedChange={(v) => setInactive(v === true)} />
            <Label htmlFor="inactive" className="font-normal">
              So cả job đã hết hạn/gỡ
            </Label>
          </div>
          <Button onClick={() => run.mutate(false)} disabled={!companies || !jobs || run.isPending}>
            <Play /> {run.isPending ? "Đang đánh giá…" : "Đánh giá"}
          </Button>
          {server.data?.available && (
            <Button variant="outline" onClick={() => run.mutate(true)} disabled={run.isPending}>
              <FolderOpen /> Dùng {server.data.path}/ trên máy chủ
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          <code>companies.csv</code> như file đầu vào (name, tax_code, website); <code>jobs.csv</code> gồm <code>company</code>{" "}
          (tên hoặc MST) và <code>url</code> và/hoặc <code>title</code> của các job đúng. Khớp khi URL trùng hoặc tiêu đề gần
          như trùng (≥ 90%). Chưa có golden set: dùng trang <Link to="/review" className="text-primary hover:underline">Kiểm tay</Link>.
        </p>
      </Section>

      {!r && !run.isPending && <Empty>Tải golden set lên để xem precision/recall.</Empty>}

      {r && (
        <div className="space-y-6">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="Precision"
              value={pct(r.precision)}
              hint={`mục tiêu ≥ ${pct(r.target_precision, 0)}`}
              tone={r.precision !== null && r.precision >= r.target_precision ? "good" : "bad"}
            />
            <StatCard
              label="Recall"
              value={pct(r.recall)}
              hint={`mục tiêu ≥ ${pct(r.target_recall, 0)}`}
              tone={r.recall !== null && r.recall >= r.target_recall ? "good" : "bad"}
            />
            <StatCard label="Golden / tìm được / khớp" value={`${r.golden} / ${r.predicted} / ${r.matched}`} hint={`${r.companies.length} doanh nghiệp`} />
            {Object.entries(r.perf).map(([k, v]) => (
              <StatCard key={k} label={k} value={v} />
            ))}
          </div>

          <Section title="Theo doanh nghiệp" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Doanh nghiệp</TableHead>
                  <TableHead>Lượt cuối</TableHead>
                  <TableHead className="text-right">Golden</TableHead>
                  <TableHead className="text-right">Tìm được</TableHead>
                  <TableHead className="text-right">Khớp</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="text-right">Recall</TableHead>
                  <TableHead>Thiếu</TableHead>
                  <TableHead className="pr-4">Thừa</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {r.companies.map((c) => (
                  <TableRow key={c.name}>
                    <TableCell className="pl-4">
                      {c.company_id ? (
                        <Link to={`/companies/${c.company_id}`} className="hover:underline">
                          {c.name}
                        </Link>
                      ) : (
                        c.name
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{c.run_status ?? "–"}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.golden}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.predicted}</TableCell>
                    <TableCell className="text-right tabular-nums">{c.matched}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(c.precision)}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(c.recall)}</TableCell>
                    <TableCell className="max-w-72 text-xs whitespace-normal">{c.missing.join("; ")}</TableCell>
                    <TableCell className="max-w-72 pr-4 text-xs whitespace-normal text-muted-foreground">{c.extra.join("; ")}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          <Section title="Theo nguồn" description="Tin trước gộp trùng" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-4">Nguồn</TableHead>
                  <TableHead className="text-right">Tin</TableHead>
                  <TableHead className="text-right">Khớp</TableHead>
                  <TableHead className="text-right">Precision</TableHead>
                  <TableHead className="pr-4 text-right">Recall (độ phủ)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {r.sources.map((s) => (
                  <TableRow key={s.source}>
                    <TableCell className="pl-4">{s.source}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.predicted}</TableCell>
                    <TableCell className="text-right tabular-nums">{s.matched}</TableCell>
                    <TableCell className="text-right tabular-nums">{pct(s.precision)}</TableCell>
                    <TableCell className="pr-4 text-right tabular-nums">{pct(s.recall)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Section>

          {r.warnings.length > 0 && (
            <Alert>
              <AlertTitle>Cảnh báo</AlertTitle>
              <AlertDescription>
                <ul className="list-disc pl-4 text-xs">
                  {r.warnings.map((w) => (
                    <li key={w}>{w}</li>
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
