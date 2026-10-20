// Khoá tham gia dùng một lần cho máy worker mới (như "Connect worker" của open-asm): tạo khi mở hộp
// thoại, đếm ngược tới khi hết hạn (theo số giây máy chủ trả, không lệch đồng hồ), báo khi worker đã
// dùng khoá; đóng hộp thoại khi khoá chưa dùng thì huỷ khoá.
import { CheckCircle2, Copy, KeyRound, RefreshCw } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { errorMessage } from "@/lib/api"
import { cn } from "@/lib/utils"
import { revokeJoinKey, useCreateJoinKey, useGetJoinKey, type JoinKeyOut } from "@/lib/gen/queries"

function countdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
}

export function JoinKeyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [key, setKey] = useState<JoinKeyOut | null>(null)
  const [deadline, setDeadline] = useState(0) // performance.now() lúc hết hạn
  const [now, setNow] = useState(() => performance.now())
  const keyRef = useRef<JoinKeyOut | null>(null)
  keyRef.current = key

  const create = useCreateJoinKey({
    mutation: {
      onSuccess: (k) => {
        setKey(k)
        setDeadline(performance.now() + k.ttl_seconds * 1000)
      },
    },
  })
  const status = useGetJoinKey(encodeURIComponent(key?.id ?? ""), {
    query: { enabled: Boolean(key) && open, refetchInterval: (q) => (q.state.data?.status === "pending" ? 2000 : false) },
  })
  const used = key && status.data?.id === key.id && status.data.status === "used" ? status.data : null
  const left = (deadline - now) / 1000
  const expired = Boolean(key) && !used && left <= 0

  // Mở hộp thoại: tạo khoá; đóng: huỷ khoá chưa dùng.
  useEffect(() => {
    if (!open) return
    create.mutate()
    return () => {
      const k = keyRef.current
      if (k) void revokeJoinKey(encodeURIComponent(k.id)).catch(() => undefined)
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
    const k = keyRef.current
    if (k) void revokeJoinKey(encodeURIComponent(k.id)).catch(() => undefined)
    setKey(null)
    create.mutate()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Thêm worker</DialogTitle>
          <DialogDescription>
            Khoá tham gia dùng một lần cho một máy worker mới; hết hạn sau {countdown(key?.ttl_seconds ?? 300)} phút. Sau khi vào,
            worker nhận token riêng và không cần khoá này nữa.
          </DialogDescription>
        </DialogHeader>
        {create.isError && <p className="text-meta font-medium text-danger-ink">{errorMessage(create.error)}</p>}
        {key && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-lg border bg-muted/40 p-2 pl-3">
              <KeyRound className="size-4 shrink-0 text-muted-foreground" />
              <code className={cn("min-w-0 flex-1 truncate font-mono text-sm", (expired || used) && "text-muted-foreground line-through")}>
                {key.key}
              </code>
              <Button
                variant="outline"
                size="sm"
                disabled={expired || Boolean(used)}
                onClick={() =>
                  void navigator.clipboard.writeText(key.key).then(
                    () => toast.success("Đã chép khoá"),
                    () => toast.error("Không chép được, hãy chọn và chép tay"),
                  )
                }
              >
                <Copy /> Chép
              </Button>
            </div>
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
          {(expired || used) && (
            <Button variant="outline" onClick={renew} disabled={create.isPending}>
              <RefreshCw /> Tạo khoá khác
            </Button>
          )}
          <Button onClick={() => onOpenChange(false)}>{used ? "Xong" : "Đóng"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
