// "Thêm worker": mở thì tạo khoá tham gia một lần và ghép lệnh Docker; đóng khi khoá chưa dùng thì huỷ.
import { CheckCircle2, Copy, KeyRound, RefreshCw, Settings2 } from "lucide-react"
import { Fragment, useEffect, useId, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router"
import { toast } from "sonner"

import { settingsSearch } from "@/components/settings-dialog"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { errorMessage } from "@/lib/api"
import { cn } from "@/lib/utils"
import { revokeJoinKey, useCreateJoinKey, useGetJoinKey, type JoinKeyOut } from "@/lib/gen/queries"
import {
  cleanPool,
  cleanSlots,
  DEFAULT_HTTP_SLOTS,
  DEFAULT_RENDER_SLOTS,
  IMPERSONATE_OPTIONS,
  isLoopback,
  parseSlots,
  workerCommandLines,
  type WorkerCommand,
} from "@/lib/worker-command"

function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
}

// Lệnh hiển thị: chỉ xuống dòng giữa các nhóm (cờ + giá trị; nhóm dài hơn cả dòng mới tự gãy), dòng
// tràn thụt vào để thấy là cùng một lệnh. Chữ giống hệt lệnh được chép (dấu xuống dòng giữa hai lệnh
// ẩn đi vì mỗi lệnh đã là một khối).
function CommandText({ lines, stale }: { lines: string[][]; stale: boolean }) {
  return (
    <pre
      aria-label="Lệnh chạy worker"
      className={cn(
        "max-h-64 overflow-auto p-3 font-mono text-meta leading-relaxed whitespace-pre-wrap",
        stale && "text-muted-foreground",
      )}
    >
      {lines.map((groups, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="hidden">{"\n"}</span>}
          <span className="block pl-[4ch] [text-indent:-4ch]">
            {groups.map((g, j) => (
              <Fragment key={j}>
                {j > 0 && " "}
                <span
                  className={cn("inline-block max-w-full [overflow-wrap:anywhere] [text-indent:0]", stale && "line-through")}
                >
                  {g}
                </span>
              </Fragment>
            ))}
          </span>
        </Fragment>
      ))}
    </pre>
  )
}

function copy(text: string, done: string) {
  void navigator.clipboard.writeText(text).then(
    () => toast.success(done),
    () => toast.error("Không chép được, hãy chọn và chép tay"),
  )
}

export function JoinKeyDialog({
  open,
  onOpenChange,
  pools = [],
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  pools?: string[]
}) {
  const [key, setKey] = useState<JoinKeyOut | null>(null)
  const [deadline, setDeadline] = useState(0) // performance.now() lúc hết hạn
  const [now, setNow] = useState(() => performance.now())
  const [pool, setPool] = useState("default")
  const [httpSlots, setHttpSlots] = useState(String(DEFAULT_HTTP_SLOTS))
  const [renderSlots, setRenderSlots] = useState(String(DEFAULT_RENDER_SLOTS))
  const [impersonate, setImpersonate] = useState<string>(IMPERSONATE_OPTIONS[0])
  const keyRef = useRef<JoinKeyOut | null>(null)
  keyRef.current = key
  const openRef = useRef(open)
  openRef.current = open
  const poolId = useId()
  const navigate = useNavigate()
  const location = useLocation()

  const create = useCreateJoinKey({
    mutation: {
      onSuccess: (k) => {
        // Đã đóng hộp thoại trước khi khoá về: huỷ ngay, không để khoá sống 15 phút không ai thấy.
        if (!openRef.current) {
          void revokeJoinKey(encodeURIComponent(k.id)).catch(() => undefined)
          return
        }
        setKey(k)
        setDeadline(performance.now() + k.ttl_seconds * 1000)
      },
      onError: () => {}, // hiện trong hộp thoại thay cho toast chung
    },
  })
  const status = useGetJoinKey(encodeURIComponent(key?.id ?? ""), {
    query: { enabled: Boolean(key) && open, refetchInterval: (q) => (q.state.data?.status === "pending" ? 2000 : false) },
  })
  const used = key && status.data?.id === key.id && status.data.status === "used" ? status.data : null
  const usedRef = useRef(used)
  usedRef.current = used
  const left = (deadline - now) / 1000
  const expired = Boolean(key) && !used && left <= 0
  // Worker nối gRPC qua chính cổng của giao diện.
  const gatewayUrl = key?.gateway_url ?? window.location.origin
  const spec: WorkerCommand | null = key
    ? {
        image: key.worker_image,
        gatewayUrl,
        joinKey: key.key,
        pool,
        name: `jobcrawler-worker-${key.id.slice(0, 6)}`,
        httpSlots: parseSlots(httpSlots, 1),
        renderSlots: parseSlots(renderSlots, 0),
        impersonate,
      }
    : null
  const lines = spec ? workerCommandLines(spec) : []
  const command = lines.map((groups) => groups.join(" ")).join("\n")
  const local = isLoopback(gatewayUrl)

  // Huỷ khoá đang hiện nếu worker chưa dùng (khoá đã dùng thì không còn gì để huỷ).
  const revokeUnused = () => {
    const k = keyRef.current
    if (k && !usedRef.current) void revokeJoinKey(encodeURIComponent(k.id)).catch(() => undefined)
  }

  // Mở hộp thoại: tạo khoá; đóng: huỷ khoá chưa dùng.
  useEffect(() => {
    if (!open) return
    create.mutate()
    return () => {
      revokeUnused()
      setKey(null)
    }
    // Chỉ theo lúc mở / đóng (create / revoke không đổi giữa các lần render).
  }, [open])

  useEffect(() => {
    if (!key || used) return
    const timer = setInterval(() => setNow(performance.now()), 500)
    return () => clearInterval(timer)
  }, [key, used])

  useEffect(() => {
    if (used) toast.success(`Worker ${used.worker_id ?? ""} đã tham gia`)
  }, [used])

  const renew = () => {
    revokeUnused()
    setKey(null)
    create.mutate()
  }

  const openSettings = (key = "") => {
    onOpenChange(false)
    navigate({ search: settingsSearch(location.search, "workers", key) })
  }

  const stale = expired || Boolean(used)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Thêm worker</DialogTitle>
          <DialogDescription>
            Khoá trong lệnh dùng một lần, hết hạn sau {Math.round((key?.ttl_seconds ?? 900) / 60)} phút; đóng hộp thoại
            trước khi worker vào là huỷ khoá.
          </DialogDescription>
        </DialogHeader>
        {create.isError && <p className="text-meta font-medium text-danger-ink">{errorMessage(create.error)}</p>}
        {key && (
          <div className="min-w-0 space-y-3">
            <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
              <div className="grid gap-1.5">
                <Label htmlFor={poolId}>Pool</Label>
                <Input
                  id={poolId}
                  className="h-8 w-40 font-mono"
                  value={pool}
                  list={`${poolId}-list`}
                  onChange={(e) => setPool(cleanPool(e.target.value))}
                  placeholder="default"
                  spellCheck={false}
                />
                <datalist id={`${poolId}-list`}>
                  {[...new Set(["default", ...pools])].map((p) => (
                    <option key={p} value={p} />
                  ))}
                </datalist>
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor={`${poolId}-http`}>Slot HTTP</Label>
                <Input
                  id={`${poolId}-http`}
                  className="h-8 w-28 font-mono tabular-nums"
                  inputMode="numeric"
                  value={httpSlots}
                  onChange={(e) => setHttpSlots(cleanSlots(e.target.value))}
                  placeholder="tự tính"
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor={`${poolId}-render`}>Slot render</Label>
                <Input
                  id={`${poolId}-render`}
                  className="h-8 w-28 font-mono tabular-nums"
                  inputMode="numeric"
                  value={renderSlots}
                  onChange={(e) => setRenderSlots(cleanSlots(e.target.value))}
                  placeholder="tự tính"
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor={`${poolId}-impersonate`}>Giả lập trình duyệt</Label>
                <Select value={impersonate} onValueChange={setImpersonate}>
                  <SelectTrigger id={`${poolId}-impersonate`} size="sm" className="w-32 font-mono">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {IMPERSONATE_OPTIONS.map((o) => (
                      <SelectItem key={o} value={o} className="font-mono">
                        {o}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p className="text-meta text-muted-foreground">
              Cấu hình riêng của worker này, nằm trong lệnh. Slot là số việc worker làm cùng lúc tối đa (request HTTP /
              trang trình duyệt): worker chỉ nhận đúng số việc còn trống, runner nhận thêm job theo tổng slot các worker. Số
              điền sẵn hợp với máy 4 CPU / 8 GB; render 0 = không mở trình duyệt, xoá trống = worker tự tính theo máy.
            </p>
            <div className="overflow-hidden rounded-lg border bg-muted/40">
              <div className="flex items-center justify-between gap-2 border-b py-1.5 pr-1.5 pl-3">
                <span className="text-meta text-muted-foreground">Lệnh Docker</span>
                <Button size="sm" disabled={stale} onClick={() => copy(command, "Đã chép lệnh")}>
                  <Copy /> Chép lệnh
                </Button>
              </div>
              <CommandText lines={lines} stale={stale} />
            </div>
            <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 text-meta">
              <span className="text-muted-foreground">Địa chỉ</span>
              <span className="truncate font-mono" title={gatewayUrl}>
                {gatewayUrl}
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="row-span-2 h-auto self-stretch text-meta"
                title="Đổi địa chỉ và image trong Cài đặt"
                onClick={() => openSettings()}
              >
                <Settings2 /> Đổi
              </Button>
              <span className="text-muted-foreground">Image</span>
              <span className="truncate font-mono" title={key.worker_image}>
                {key.worker_image}
              </span>
            </div>
            {local && !stale && (
              <p className="rounded-md border border-warning/40 bg-warning/8 px-3 py-2 text-meta text-warning-ink dark:bg-warning/10">
                Đây là địa chỉ của máy này: worker ở máy khác không nối vào được.{" "}
                <button
                  type="button"
                  className="font-medium underline underline-offset-2"
                  onClick={() => openSettings("gateway_public_url")}
                >
                  Đặt địa chỉ cho worker mới
                </button>
              </p>
            )}
            {used ? (
              <p className="flex items-center gap-1.5 text-meta font-medium text-success-ink">
                <CheckCircle2 className="size-4" /> Worker {used.worker_id} đã dùng khoá và tham gia.
              </p>
            ) : expired ? (
              <p className="text-meta text-warning-ink">Khoá đã hết hạn.</p>
            ) : (
              <p className="text-meta text-muted-foreground tabular-nums">Còn {countdown(left)} · chờ worker dùng khoá…</p>
            )}
          </div>
        )}
        <DialogFooter className="flex-row flex-wrap items-center">
          {key && !stale && (
            <Button variant="ghost" className="mr-auto" onClick={() => copy(key.key, "Đã chép khoá")}>
              <KeyRound /> Chỉ chép khoá
            </Button>
          )}
          {stale && (
            <Button variant="outline" onClick={renew} disabled={create.isPending}>
              <RefreshCw /> Tạo khoá khác
            </Button>
          )}
          <Button variant={key && !stale ? "outline" : "default"} onClick={() => onOpenChange(false)}>
            {used ? "Xong" : "Đóng"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
