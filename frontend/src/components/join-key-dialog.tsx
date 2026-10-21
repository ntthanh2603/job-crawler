// "Thêm worker": mở thì tạo khoá tham gia một lần và ghép lệnh Docker; đóng khi khoá chưa dùng thì huỷ.
import { CheckCircle2, Copy, KeyRound, RefreshCw, Settings2 } from "lucide-react"
import { useEffect, useId, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router"
import { toast } from "sonner"

import { settingsSearch } from "@/components/settings-dialog"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { errorMessage } from "@/lib/api"
import { cn } from "@/lib/utils"
import { revokeJoinKey, useCreateJoinKey, useGetJoinKey, type JoinKeyOut } from "@/lib/gen/queries"
import { cleanPool, isLoopback, workerRunCommand } from "@/lib/worker-command"

function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
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
  const command = key
    ? workerRunCommand({
        image: key.worker_image,
        gatewayUrl,
        joinKey: key.key,
        pool,
        name: `jobcrawler-worker-${key.id.slice(0, 6)}`,
      })
    : ""
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

  const openSettings = () => {
    onOpenChange(false)
    navigate({ search: settingsSearch(location.search, "workers") })
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
            <div className="flex items-center gap-3">
              <Label htmlFor={poolId}>Pool</Label>
              <Input
                id={poolId}
                className="h-8 w-48 font-mono"
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
            <div className="relative rounded-lg border bg-muted/40">
              <pre
                aria-label="Lệnh chạy worker"
                className={cn(
                  "max-h-64 overflow-auto p-3 pr-28 font-mono text-meta leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]",
                  stale && "text-muted-foreground line-through",
                )}
              >
                {command}
              </pre>
              <Button
                className="absolute top-2 right-2"
                size="sm"
                disabled={stale}
                onClick={() => copy(command, "Đã chép lệnh")}
              >
                <Copy /> Chép lệnh
              </Button>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-meta text-muted-foreground">
              <span>
                Địa chỉ <span className="font-mono text-foreground">{gatewayUrl}</span>
              </span>
              <span className="min-w-0 truncate">
                Image <span className="font-mono text-foreground">{key.worker_image}</span>
              </span>
              <Button variant="link" size="sm" className="h-auto p-0 text-meta has-[>svg]:px-0" onClick={openSettings}>
                <Settings2 /> Đổi
              </Button>
            </div>
            {local && !stale && (
              <p className="text-meta text-warning-ink">
                Đây là địa chỉ của máy này: worker ở máy khác không nối vào được.
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
        <DialogFooter>
          {key && !stale && (
            <Button variant="ghost" className="sm:mr-auto" onClick={() => copy(key.key, "Đã chép khoá")}>
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
