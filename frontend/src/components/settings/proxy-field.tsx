// Bảng proxy chung (cài đặt `proxies`): thêm / sửa / xoá từng proxy, dán nhiều dòng, kiểm tra qua
// worker, trạng thái xoay vòng của pool (điểm, bị loại, đang nghỉ, đang gắn domain).
import { CheckCircle2, ClipboardPaste, Eye, EyeOff, KeyRound, Loader2, Pencil, Plus, ShieldCheck, Trash2, XCircle } from "lucide-react"
import { useMemo, useState, type ReactNode } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { errorMessage } from "@/lib/api"
import { num } from "@/lib/format"
import { useCheckProxies, useGetProxies } from "@/lib/gen/queries"
import type { ProxyCheck, ProxyStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

import { humanSeconds, type Raw } from "./model"
import {
  checkErrorText,
  PROXY_SCHEMES,
  parsePastedProxies,
  parseProxyLine,
  proxyIdentity,
  proxyRowError,
  toProxyLine,
  type ProxyRow,
} from "./proxy-lines"

const CHECK_BATCH = 20

/** Danh sách này được dùng thế nào theo chế độ proxy đang lưu. */
const MODE_TEXT: Record<string, { text: string; warn: boolean }> = {
  worker: {
    text: "Chế độ “Worker tự xoay”: mỗi worker tải danh sách này về và tự xoay vòng (máy có proxy riêng dùng proxy của nó). Trạng thái bên dưới cập nhật khi bấm Kiểm tra.",
    warn: false,
  },
  central: { text: "Chế độ “Backend chọn”: backend xoay vòng các proxy này cho từng request.", warn: false },
  off: { text: "Chế độ “Không dùng proxy”: danh sách này chưa được dùng.", warn: true },
}

function Tag({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-flex h-5 items-center gap-1 rounded-md px-1.5 text-caption font-medium whitespace-nowrap", className)}
    >
      {children}
    </span>
  )
}

function duration(seconds: number): string {
  return humanSeconds(seconds) ?? `${num(seconds)} giây`
}

/** Trạng thái xoay vòng của một proxy đã lưu. */
function RotationStatus({ status }: { status: ProxyStatus }) {
  if (status.dead)
    return (
      <Tag className="bg-destructive/10 text-danger-ink" title={status.dead_reason ?? undefined}>
        Bị loại{status.dead_seconds ? ` · thử lại sau ${duration(status.dead_seconds)}` : ""}
      </Tag>
    )
  return (
    <>
      <Tag className="bg-success/12 text-success-ink" title={`Điểm ${status.score}/100: điểm cao thì hay được chọn hơn`}>
        Trong vòng xoay · điểm {num(status.score)}
      </Tag>
      {status.cooling_domains > 0 && (
        <Tag className="bg-warning/16 text-warning-ink" title="Bị chặn (403 / 429) ở các domain này, đang cho nghỉ">
          Nghỉ với {num(status.cooling_domains)} domain
        </Tag>
      )}
      {status.sticky_domains > 0 && (
        <Tag className="bg-info/12 text-info-ink" title="Số domain đang được gắn với proxy này">
          Đang dùng cho {num(status.sticky_domains)} domain
        </Tag>
      )}
      {status.fails > 0 && <Tag className="bg-warning/16 text-warning-ink">{num(status.fails)} lỗi liên tiếp</Tag>}
    </>
  )
}

function CheckResult({ result }: { result: ProxyCheck }) {
  if (!result.ok)
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-meta text-danger-ink" title={result.error ?? undefined}>
        <XCircle className="size-3.5 shrink-0" />
        <span className="truncate">{checkErrorText(result.error)}</span>
      </span>
    )
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-meta text-success-ink">
      <CheckCircle2 className="size-3.5 shrink-0" />
      <span className="truncate tabular-nums">
        {result.latency_ms !== null && `${num(result.latency_ms)} ms`}
        {result.exit_ip && ` · IP ${result.exit_ip}`}
        {result.location && ` · ${result.location}`}
      </span>
    </span>
  )
}

const EMPTY_ROW: ProxyRow = { scheme: "http", host: "", port: "", username: "", password: "", masked: null }

/** Thêm / sửa một proxy. */
function ProxyDialog({
  open,
  initial,
  others,
  onOpenChange,
  onSubmit,
}: {
  open: boolean
  initial: ProxyRow | null
  others: ProxyRow[]
  onOpenChange: (open: boolean) => void
  onSubmit: (row: ProxyRow) => void
}) {
  const [row, setRow] = useState<ProxyRow>(initial ?? EMPTY_ROW)
  const [show, setShow] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const keeping = row.password === null
  const set = (patch: Partial<ProxyRow>) => {
    setRow((r) => ({ ...r, ...patch }))
    setError(null)
  }
  const submit = () => {
    const clean = { ...row, host: row.host.trim(), port: row.port.trim(), username: row.username.trim() }
    const problem =
      proxyRowError(clean) ??
      (others.some((o) => proxyIdentity(o) === proxyIdentity(clean)) ? "Proxy này đã có trong danh sách" : null)
    if (problem) {
      setError(problem)
      return
    }
    onSubmit(clean)
    onOpenChange(false)
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{initial ? "Sửa proxy" : "Thêm proxy"}</DialogTitle>
          <DialogDescription>Proxy có đăng nhập thì điền tài khoản và mật khẩu; mật khẩu không bao giờ hiện lại sau khi lưu.</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4 sm:grid-cols-[8.5rem_minmax(0,1fr)_7rem]"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="proxy-scheme">Loại</Label>
            <Select value={row.scheme} onValueChange={(v) => set({ scheme: v })}>
              <SelectTrigger id="proxy-scheme" className="w-full bg-card">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                {PROXY_SCHEMES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s.toUpperCase()}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="proxy-host">Host</Label>
            <Input
              id="proxy-host"
              value={row.host}
              autoFocus
              placeholder="1.2.3.4 hoặc proxy.vd.vn"
              spellCheck={false}
              autoComplete="off"
              className="bg-card"
              onChange={(e) => set({ host: e.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="proxy-port">Cổng</Label>
            <Input
              id="proxy-port"
              value={row.port}
              inputMode="numeric"
              placeholder="8080"
              autoComplete="off"
              className="bg-card tabular-nums"
              onChange={(e) => set({ port: e.target.value })}
            />
          </div>
          <div className="space-y-1.5 sm:col-span-3 sm:grid sm:grid-cols-2 sm:gap-4 sm:space-y-0">
            <div className="space-y-1.5">
              <Label htmlFor="proxy-user">Tài khoản</Label>
              <Input
                id="proxy-user"
                value={row.username}
                placeholder="Để trống nếu không cần"
                spellCheck={false}
                autoComplete="off"
                className="bg-card"
                onChange={(e) => set({ username: e.target.value })}
              />
            </div>
            <div className="space-y-1.5 max-sm:pt-4">
              <Label htmlFor="proxy-pass">Mật khẩu</Label>
              <div className="relative">
                <Input
                  id="proxy-pass"
                  type={show ? "text" : "password"}
                  value={row.password ?? ""}
                  placeholder={keeping ? "Giữ mật khẩu đã lưu" : "Để trống nếu không cần"}
                  autoComplete="new-password"
                  className="bg-card pr-9"
                  onChange={(e) => set({ password: e.target.value === "" && row.masked ? null : e.target.value })}
                />
                <button
                  type="button"
                  aria-label={show ? "Ẩn mật khẩu" : "Hiện mật khẩu"}
                  onClick={() => setShow((s) => !s)}
                  className="absolute inset-y-0 right-0 grid w-9 place-items-center text-muted-foreground hover:text-foreground"
                >
                  {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </div>
          </div>
          {error && (
            <p role="alert" className="text-meta font-medium text-danger-ink sm:col-span-3">
              {error}
            </p>
          )}
          <DialogFooter className="sm:col-span-3">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Huỷ
            </Button>
            <Button type="submit">{initial ? "Cập nhật" : "Thêm"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Dán nhiều proxy, mỗi dòng một proxy. */
function PasteDialog({
  open,
  existing,
  onOpenChange,
  onSubmit,
}: {
  open: boolean
  existing: ProxyRow[]
  onOpenChange: (open: boolean) => void
  onSubmit: (rows: ProxyRow[]) => void
}) {
  const [text, setText] = useState("")
  const parsed = useMemo(() => parsePastedProxies(text), [text])
  const known = new Set(existing.map(proxyIdentity))
  const fresh: ProxyRow[] = []
  for (const row of parsed.rows) {
    const key = proxyIdentity(row)
    if (known.has(key)) continue
    known.add(key)
    fresh.push(row)
  }
  const duplicates = parsed.rows.length - fresh.length
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) setText("")
        onOpenChange(o)
      }}
    >
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Dán nhiều proxy</DialogTitle>
          <DialogDescription>
            Mỗi dòng một proxy: <code className="font-mono">http://user:pass@host:port</code>,{" "}
            <code className="font-mono">socks5://host:port</code>, <code className="font-mono">host:port</code> hoặc{" "}
            <code className="font-mono">host:port:user:pass</code>. Dòng bắt đầu bằng # được bỏ qua.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          value={text}
          autoFocus
          rows={10}
          spellCheck={false}
          placeholder={"http://user:pass@1.2.3.4:8080\n5.6.7.8:3128:user:pass"}
          onChange={(e) => setText(e.target.value)}
          className="max-h-80 bg-card font-mono text-meta md:text-meta"
        />
        <div className="space-y-1 text-meta">
          <p className="text-muted-foreground tabular-nums">
            {num(fresh.length)} proxy mới
            {duplicates > 0 && ` · ${num(duplicates)} đã có, bỏ qua`}
            {parsed.invalid.length > 0 && ` · ${num(parsed.invalid.length)} dòng không đọc được`}
          </p>
          {parsed.invalid.length > 0 && (
            <ul className="max-h-24 overflow-y-auto font-mono text-caption text-danger-ink">
              {parsed.invalid.slice(0, 20).map((l) => (
                <li key={l.line} className="truncate">
                  Dòng {l.line}: {l.text.replace(/:[^:@/]*@/, ":•••@")}
                </li>
              ))}
            </ul>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Huỷ
          </Button>
          <Button
            disabled={!fresh.length}
            onClick={() => {
              onSubmit(fresh)
              setText("")
              onOpenChange(false)
            }}
          >
            <Plus /> Thêm {num(fresh.length)} proxy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

type Entry = { line: string; row: ProxyRow | null }

export function ProxyListField({ value, onChange, id }: { value: Exclude<Raw, null>; onChange: (value: Raw) => void; id: string }) {
  const lines = Array.isArray(value) ? (value as string[]) : String(value).split("\n").filter((s) => s.trim())
  const entries: Entry[] = lines.map((line) => ({ line, row: parseProxyLine(line) }))
  const rows = entries.flatMap((e) => (e.row ? [e.row] : []))
  const status = useGetProxies({ query: { refetchInterval: 15_000 } })
  const check = useCheckProxies()
  const [results, setResults] = useState<Record<string, ProxyCheck>>({})
  const [checking, setChecking] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<{ index: number | null } | null>(null)
  const [pasting, setPasting] = useState(false)

  const byLine = new Map((status.data?.items ?? []).filter((s) => s.source === "settings").map((s) => [s.line, s]))
  const fromFile = (status.data?.items ?? []).filter((s) => s.source === "file")
  const savedIds = entries.flatMap((e) => {
    const s = byLine.get(e.line)
    return s ? [s.id] : []
  })
  const mode = status.data?.mode

  const runCheck = async (ids: string[]) => {
    if (!ids.length) return
    setChecking((c) => new Set([...c, ...ids]))
    try {
      let ok = 0
      for (let i = 0; i < ids.length; i += CHECK_BATCH) {
        const { results: got } = await check.mutateAsync({ data: { ids: ids.slice(i, i + CHECK_BATCH) } })
        ok += got.filter((r) => r.ok).length
        setResults((r) => ({ ...r, ...Object.fromEntries(got.map((x) => [x.proxy_id, x])) }))
      }
      if (ids.length > 1) toast.success(`${num(ok)}/${num(ids.length)} proxy hoạt động`)
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setChecking((c) => new Set([...c].filter((x) => !ids.includes(x))))
      void status.refetch()
    }
  }

  const replace = (index: number | null, row: ProxyRow) => {
    const line = toProxyLine(row)
    onChange(index === null ? [...lines, line] : lines.map((l, i) => (i === index ? line : l)))
  }
  const remove = (index: number) => onChange(lines.filter((_, i) => i !== index))
  const editingRow = editing?.index != null ? entries[editing.index]?.row ?? null : null

  return (
    <div id={id} className="space-y-3">
      {mode && MODE_TEXT[mode] && lines.length > 0 && (
        <p
          className={cn(
            "rounded-lg border px-3 py-2 text-meta",
            MODE_TEXT[mode].warn
              ? "border-warning/40 bg-warning/8 text-warning-ink dark:bg-warning/10"
              : "bg-muted/40 text-muted-foreground",
          )}
        >
          {MODE_TEXT[mode].text}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-auto text-meta text-muted-foreground tabular-nums">
          {lines.length ? `${num(lines.length)} proxy` : "Chưa có proxy nào"}
        </span>
        <Button type="button" variant="outline" size="sm" onClick={() => setPasting(true)}>
          <ClipboardPaste /> Dán nhiều dòng
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setEditing({ index: null })}>
          <Plus /> Thêm proxy
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!savedIds.length || checking.size > 0}
          onClick={() => void runCheck(savedIds)}
          title="Gọi trang kiểm tra qua từng proxy đã lưu, từ worker"
        >
          {checking.size > 0 ? <Loader2 className="animate-spin" /> : <ShieldCheck />} Kiểm tra tất cả
        </Button>
      </div>

      {lines.length > 0 && (
        <ul className="divide-y overflow-hidden rounded-lg border bg-card">
          {entries.map((entry, index) => {
            const row = entry.row
            const saved = byLine.get(entry.line)
            const result = saved ? results[saved.id] : undefined
            const busy = saved ? checking.has(saved.id) : false
            return (
              <li key={`${entry.line}-${index}`} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    {row ? (
                      <>
                        <Tag className="bg-muted font-mono text-muted-foreground uppercase">{row.scheme}</Tag>
                        <span className="min-w-0 truncate font-mono text-sm text-foreground">
                          {row.host}:{row.port}
                        </span>
                        {row.username && (
                          <span className="inline-flex min-w-0 items-center gap-1 text-meta text-muted-foreground">
                            <KeyRound className="size-3.5 shrink-0" aria-label="Có đăng nhập" />
                            <span className="truncate">{row.username}</span>
                            {(row.password === null || row.password) && <span aria-label="có mật khẩu">· ••••</span>}
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="font-mono text-sm text-danger-ink">{entry.line}</span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {saved ? (
                      <RotationStatus status={saved} />
                    ) : (
                      <Tag className="bg-primary/10 text-brand">Chưa lưu</Tag>
                    )}
                    {busy ? (
                      <span className="inline-flex items-center gap-1 text-meta text-muted-foreground">
                        <Loader2 className="size-3.5 animate-spin" /> Đang kiểm tra…
                      </span>
                    ) : (
                      result && <CheckResult result={result} />
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1 self-end sm:self-center">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={!saved || busy}
                    title={saved ? "Kiểm tra proxy này" : "Lưu trước rồi kiểm tra"}
                    onClick={() => saved && void runCheck([saved.id])}
                  >
                    <ShieldCheck /> Kiểm tra
                  </Button>
                  {row && (
                    <Button type="button" variant="ghost" size="icon-sm" aria-label="Sửa proxy" onClick={() => setEditing({ index })}>
                      <Pencil />
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Xoá proxy"
                    className="text-muted-foreground hover:text-danger-ink"
                    onClick={() => remove(index)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {fromFile.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-meta text-muted-foreground">
            Thêm {num(fromFile.length)} proxy từ file cấu hình của backend (chỉ xem ở đây):
          </p>
          <ul className="divide-y overflow-hidden rounded-lg border bg-muted/30">
            {fromFile.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <Tag className="bg-muted font-mono text-muted-foreground uppercase">{s.scheme}</Tag>
                <span className="font-mono text-sm">
                  {s.host}:{s.port}
                </span>
                {s.username && <span className="text-meta text-muted-foreground">{s.username}</span>}
                <RotationStatus status={s} />
                {results[s.id] && <CheckResult result={results[s.id]} />}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="text-meta text-pretty text-muted-foreground">
        Thêm, sửa, xoá xong bấm Lưu ở cuối trang. Kiểm tra chạy với proxy đã lưu, gọi trang kiểm tra qua worker: proxy sống được đưa lại
        vào vòng xoay, proxy lỗi bị loại.
      </p>

      {editing && (
        <ProxyDialog
          open
          initial={editingRow}
          others={rows.filter((r) => r !== editingRow)}
          onOpenChange={(o) => !o && setEditing(null)}
          onSubmit={(row) => replace(editing.index, row)}
        />
      )}
      <PasteDialog
        open={pasting}
        existing={rows}
        onOpenChange={setPasting}
        onSubmit={(fresh) => onChange([...lines, ...fresh.map(toProxyLine)])}
      />
    </div>
  )
}
