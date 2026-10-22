// Trang Tích hợp: hệ thống ngoài (backend của đối tác) đẩy doanh nghiệp vào và nhận việc làm mới qua API
// tích hợp (`/api/integration/v1`) bằng khoá API riêng; webhook ký HMAC đẩy sự kiện việc làm tới bên kia.
// Trang chỉ có trạng thái và cài đặt; cách gọi API cho đội bên kia ở docs/INTEGRATION.md.
import { useQueryClient } from "@tanstack/react-query"
import { Copy, KeyRound, Plug, Plus, Send, Trash2, Webhook } from "lucide-react"
import { useEffect, useId, useState, type ReactNode } from "react"
import { toast } from "sonner"

import { ConfirmButton, Empty, ErrorState, PageHeader, PageLoading, Section, StatusBadge, Tag } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { errorMessage } from "@/lib/api"
import { ago, cap, dt, num } from "@/lib/format"
import {
  getGetApiClientQueryKey,
  getGetApiClientsQueryKey,
  useDeleteApiClient,
  useDeleteApiClientKey,
  useGetApiClient,
  useGetApiClients,
  usePatchApiClient,
  usePostApiClient,
  usePostApiClientKey,
  usePostApiClientWebhookSecret,
  usePostApiClientWebhookTest,
  type ApiClientDetailOut,
  type ApiClientInScopesItem,
  type ApiClientOut,
  type ApiClientUpdateIn,
} from "@/lib/gen/queries"
import { cn } from "@/lib/utils"

/** Đường gốc của API tích hợp (hệ thống ngoài gọi qua cùng địa chỉ với giao diện). */
const INTEGRATION_PATH = "/api/integration/v1"

const SCOPES: { value: ApiClientInScopesItem; label: string; hint: string }[] = [
  { value: "companies:write", label: "Đẩy doanh nghiệp", hint: "Thêm / sửa / tắt doanh nghiệp, yêu cầu thu thập ngay" },
  { value: "jobs:read", label: "Đọc việc làm", hint: "Snapshot việc làm, feed thay đổi, nhận webhook" },
]

function copy(text: string, done: string) {
  void navigator.clipboard.writeText(text).then(
    () => toast.success(done),
    () => toast.error("Không chép được, hãy chọn và chép tay"),
  )
}

/** Hộp thoại hiện một giá trị bí mật đúng một lần (khoá API, secret webhook). */
function SecretDialog({
  secret,
  title,
  description,
  onClose,
}: {
  secret: string | null
  title: string
  description: ReactNode
  onClose: () => void
}) {
  return (
    <Dialog open={secret !== null} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="overflow-hidden rounded-lg border bg-muted/40">
          <div className="flex items-center justify-between gap-2 border-b py-1.5 pr-1.5 pl-3">
            <span className="text-meta text-muted-foreground">Chỉ hiện một lần</span>
            <Button size="sm" onClick={() => secret && copy(secret, "Đã chép")}>
              <Copy /> Chép
            </Button>
          </div>
          <pre aria-label={title} className="p-3 font-mono text-meta break-all whitespace-pre-wrap select-all">
            {secret}
          </pre>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>Đã lưu, đóng</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function useRefresh(clientId?: string) {
  const client = useQueryClient()
  return () => {
    client.invalidateQueries({ queryKey: getGetApiClientsQueryKey() })
    if (clientId) client.invalidateQueries({ queryKey: getGetApiClientQueryKey(clientId) })
  }
}

/** Hộp thoại thêm hệ thống ngoài: tên, quyền, phạm vi đọc. */
function CreateClientDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const refresh = useRefresh()
  const id = useId()
  const [name, setName] = useState("")
  const [scopes, setScopes] = useState<ApiClientInScopesItem[]>(SCOPES.map((s) => s.value))
  const [allCompanies, setAllCompanies] = useState(false)
  const [description, setDescription] = useState(true)
  const create = usePostApiClient({
    mutation: {
      onError: () => {}, // hiện trong hộp thoại
      onSuccess: (r) => {
        toast.success(`Đã thêm ${r.name}: tạo khoá API để bên đó gọi vào`)
        refresh()
        close()
      },
    },
  })
  const close = () => {
    onOpenChange(false)
    setName("")
    setScopes(SCOPES.map((s) => s.value))
    setAllCompanies(false)
    setDescription(true)
    create.reset()
  }
  const trimmed = name.trim()
  return (
    <Dialog open={open} onOpenChange={(v) => (v ? onOpenChange(true) : close())}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Thêm hệ thống ngoài</DialogTitle>
          <DialogDescription>
            Mỗi hệ thống (vd. backend của đối tác) có khoá API riêng và chỉ thấy doanh nghiệp nó đã đẩy vào, trừ khi cho đọc
            mọi việc làm.
          </DialogDescription>
        </DialogHeader>
        <form
          id={`${id}-form`}
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (trimmed && scopes.length && !create.isPending)
              create.mutate({
                data: { name: trimmed, scopes, all_companies: allCompanies, include_description: description },
              })
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor={`${id}-name`}>Tên hệ thống</Label>
            <Input
              id={`${id}-name`}
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              required
              autoComplete="off"
              placeholder="Backend đối tác"
            />
          </div>
          <fieldset className="space-y-2">
            <legend className="mb-1.5 text-sm font-medium">Quyền</legend>
            {SCOPES.map((s) => (
              <div key={s.value} className="flex items-start gap-2.5">
                <Checkbox
                  id={`${id}-${s.value}`}
                  checked={scopes.includes(s.value)}
                  onCheckedChange={(v) =>
                    setScopes((cur) => (v === true ? [...cur, s.value] : cur.filter((x) => x !== s.value)))
                  }
                />
                <Label htmlFor={`${id}-${s.value}`} className="grid gap-0.5 font-normal">
                  <span>{s.label}</span>
                  <span className="text-meta text-muted-foreground">{s.hint}</span>
                </Label>
              </div>
            ))}
          </fieldset>
          <ToggleRow
            id={`${id}-all`}
            label="Đọc mọi việc làm"
            hint="Không chỉ việc làm của doanh nghiệp hệ thống này đã đẩy vào"
            checked={allCompanies}
            onChange={setAllCompanies}
          />
          <ToggleRow
            id={`${id}-desc`}
            label="Gửi kèm mô tả"
            hint="Mô tả, yêu cầu, quyền lợi của việc làm (tắt để gói tin nhỏ hơn)"
            checked={description}
            onChange={setDescription}
          />
          {create.error && (
            <p role="alert" className="text-meta font-medium text-danger-ink">
              {errorMessage(create.error)}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Huỷ
          </Button>
          <Button type="submit" form={`${id}-form`} disabled={!trimmed || !scopes.length || create.isPending}>
            <Plus /> {create.isPending ? "Đang thêm…" : "Thêm hệ thống"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ToggleRow({
  id,
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  id: string
  label: string
  hint: ReactNode
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <Label htmlFor={id} className="grid gap-0.5 font-normal">
        <span>{label}</span>
        <span className="text-meta text-muted-foreground">{hint}</span>
      </Label>
      <Switch id={id} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" />
    </div>
  )
}

function KeysTable({ client }: { client: ApiClientDetailOut }) {
  const refresh = useRefresh(client.id)
  const revoke = useDeleteApiClientKey({
    mutation: {
      onSuccess: () => {
        toast.success("Đã thu hồi khoá: hệ thống dùng khoá này nhận 401")
        refresh()
      },
    },
  })
  if (!client.keys.length) return <p className="text-sm text-muted-foreground">Chưa có khoá: bên kia chưa gọi vào được.</p>
  return (
    <div className="-mx-5 overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="pl-5">Khoá</TableHead>
            <TableHead>Tạo lúc</TableHead>
            <TableHead>Dùng lần cuối</TableHead>
            <TableHead>Trạng thái</TableHead>
            <TableHead className="pr-5 text-right" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {client.keys.map((k) => (
            <TableRow key={k.id} className={cn(k.revoked_at && "text-muted-foreground")}>
              <TableCell className="pl-5 font-mono">jca_{k.prefix}…</TableCell>
              <TableCell title={k.created_by ? `bởi ${k.created_by}` : undefined}>{dt(k.created_at)}</TableCell>
              <TableCell>{k.last_used_at ? cap(ago(k.last_used_at)) : "Chưa dùng"}</TableCell>
              <TableCell>
                {k.revoked_at ? (
                  <StatusBadge value={null} tone="muted" text={`Thu hồi ${dt(k.revoked_at)}`} />
                ) : (
                  <StatusBadge value={null} tone="good" text="Đang dùng" />
                )}
              </TableCell>
              <TableCell className="pr-5 text-right">
                {!k.revoked_at && (
                  <ConfirmButton
                    title="Thu hồi khoá này?"
                    description="Hệ thống ngoài dùng khoá này sẽ nhận 401 (sau tối đa 10 giây). Tạo khoá mới và đưa bên kia trước khi thu hồi để không gián đoạn."
                    confirm="Thu hồi"
                    variant="ghost"
                    size="xs"
                    disabled={revoke.isPending}
                    onConfirm={() => revoke.mutate({ clientId: client.id, keyId: k.id })}
                  >
                    Thu hồi
                  </ConfirmButton>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function WebhookPanel({ client, onSecret }: { client: ApiClientDetailOut; onSecret: (secret: string) => void }) {
  const refresh = useRefresh(client.id)
  const id = useId()
  const w = client.webhook
  const [url, setUrl] = useState("")
  const [editing, setEditing] = useState(false)
  const patch = usePatchApiClient({
    mutation: {
      onSuccess: () => {
        refresh()
        setEditing(false)
      },
    },
  })
  const secret = usePostApiClientWebhookSecret({
    mutation: {
      onSuccess: (r) => {
        refresh()
        onSecret(r.secret)
      },
    },
  })
  const test = usePostApiClientWebhookTest({
    mutation: {
      onSuccess: (r) =>
        r.ok
          ? toast.success(`Bên nhận trả ${r.status} sau ${num(r.ms)} ms`)
          : toast.error(`Gửi thử lỗi: ${r.error ?? "không rõ"}`),
    },
  })
  const save = (data: ApiClientUpdateIn) => patch.mutate({ clientId: client.id, data })
  const status = !w.enabled ? (
    <StatusBadge value={null} tone="muted" text="Tắt" />
  ) : w.failing_since ? (
    <StatusBadge value={null} tone="bad" text={`Lỗi từ ${ago(w.failing_since)}`} />
  ) : (
    <StatusBadge value={null} tone="good" text="Đang gửi" />
  )
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Webhook className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Webhook</span>
        {status}
        <div className="ml-auto flex items-center gap-2">
          <Label htmlFor={`${id}-on`} className="font-normal text-muted-foreground">
            Bật
          </Label>
          <Switch
            id={`${id}-on`}
            checked={w.enabled}
            disabled={patch.isPending || (!w.enabled && (!w.url || !w.secret_set))}
            onCheckedChange={(v) => save({ webhook_enabled: v })}
          />
        </div>
      </div>
      {editing || !w.url ? (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            save({ webhook_url: url.trim() })
          }}
        >
          <Input
            aria-label="URL nhận webhook"
            className="h-8 min-w-64 flex-1 font-mono"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://doi-tac.vn/hooks/jobcrawler"
            spellCheck={false}
            autoComplete="off"
          />
          <Button type="submit" size="sm" disabled={!url.trim() || patch.isPending}>
            Lưu URL
          </Button>
          {w.url && (
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Huỷ
            </Button>
          )}
        </form>
      ) : (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="min-w-0 truncate font-mono" title={w.url}>
            {w.url}
          </span>
          <Button size="xs" variant="ghost" onClick={() => (setUrl(""), setEditing(true))}>
            Đổi URL
          </Button>
        </div>
      )}
      {patch.error && <p className="text-meta font-medium text-danger-ink">{errorMessage(patch.error)}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {w.secret_set ? (
          <ConfirmButton
            title="Đổi secret webhook?"
            description="Secret cũ hết dùng ngay: bên nhận kiểm chữ ký bằng secret cũ sẽ từ chối cho tới khi cập nhật. Lô gửi lỗi trong lúc đổi được gửi lại."
            confirm="Đổi secret"
            disabled={secret.isPending}
            onConfirm={() => secret.mutate({ clientId: client.id })}
          >
            <KeyRound /> Đổi secret
          </ConfirmButton>
        ) : (
          <Button size="sm" variant="outline" disabled={secret.isPending} onClick={() => secret.mutate({ clientId: client.id })}>
            <KeyRound /> Tạo secret
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={!w.url || !w.secret_set || test.isPending}
          onClick={() => test.mutate({ clientId: client.id })}
        >
          <Send /> {test.isPending ? "Đang gửi…" : "Gửi thử"}
        </Button>
      </div>
      {w.enabled && (
        <dl className="grid grid-cols-[minmax(8rem,max-content)_1fr] gap-x-5 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Sự kiện chờ gửi</dt>
          <dd className="tabular-nums">{num(w.lag)}</dd>
          <dt className="text-muted-foreground">Gửi được lần cuối</dt>
          <dd>{w.last_ok_at ? cap(ago(w.last_ok_at)) : "Chưa"}</dd>
          {w.last_error && (
            <>
              <dt className="text-muted-foreground">Lỗi gần nhất</dt>
              <dd className="break-words text-danger-ink">{w.last_error}</dd>
            </>
          )}
        </dl>
      )}
    </div>
  )
}

function ClientCard({ summary }: { summary: ApiClientOut }) {
  const refresh = useRefresh(summary.id)
  const id = useId()
  const detail = useGetApiClient(summary.id)
  const client = detail.data
  const [shown, setShown] = useState<{ title: string; value: string; description: ReactNode } | null>(null)
  const key = usePostApiClientKey({
    mutation: {
      onSuccess: (r) => {
        refresh()
        setShown({
          title: "Khoá API mới",
          value: r.key,
          description: (
            <>
              Gửi khoá này cho {summary.name} qua kênh bí mật; hệ thống đó gửi kèm mỗi request trong header{" "}
              <code className="font-mono">Authorization: Bearer …</code>. Đóng hộp thoại là không xem lại được.
            </>
          ),
        })
      },
    },
  })
  const patch = usePatchApiClient({ mutation: { onSuccess: () => refresh() } })
  const remove = useDeleteApiClient({
    mutation: {
      onSuccess: () => {
        toast.success(`Đã xoá ${summary.name}`)
        refresh()
      },
    },
  })
  return (
    <Section
      title={
        <span className="flex flex-wrap items-center gap-2">
          {summary.name}
          {summary.scopes.map((s) => (
            <Tag key={s}>{SCOPES.find((x) => x.value === s)?.label ?? s}</Tag>
          ))}
        </span>
      }
      description={`${num(summary.companies)} doanh nghiệp đã đẩy · ${num(summary.active_keys)} khoá đang dùng · ${
        summary.last_used_at ? `gọi lần cuối ${ago(summary.last_used_at)}` : "chưa gọi lần nào"
      }`}
      actions={
        <>
          <Button size="sm" onClick={() => key.mutate({ clientId: summary.id })} disabled={key.isPending}>
            <KeyRound /> Tạo khoá
          </Button>
          <ConfirmButton
            title={`Xoá ${summary.name}?`}
            description="Mọi khoá của hệ thống này hết dùng, mã doanh nghiệp bên đó bị xoá; doanh nghiệp và việc làm vẫn giữ."
            confirm="Xoá"
            variant="ghost"
            onConfirm={() => remove.mutate({ clientId: summary.id })}
          >
            <Trash2 /> Xoá
          </ConfirmButton>
        </>
      }
      bodyClassName="space-y-6"
    >
      {detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} />}
      {client && (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            <ToggleRow
              id={`${id}-all`}
              label="Đọc mọi việc làm"
              hint="Tắt: chỉ việc làm của doanh nghiệp hệ thống này đã đẩy vào"
              checked={client.all_companies}
              disabled={patch.isPending}
              onChange={(v) => patch.mutate({ clientId: client.id, data: { all_companies: v } })}
            />
            <ToggleRow
              id={`${id}-desc`}
              label="Gửi kèm mô tả"
              hint="Mô tả, yêu cầu, quyền lợi trong feed / webhook"
              checked={client.include_description}
              disabled={patch.isPending}
              onChange={(v) => patch.mutate({ clientId: client.id, data: { include_description: v } })}
            />
          </div>
          <div className="space-y-2">
            <div className="text-sm font-medium">Khoá API</div>
            <KeysTable client={client} />
          </div>
          <WebhookPanel
            client={client}
            onSecret={(value) =>
              setShown({
                title: "Secret ký webhook",
                value,
                description:
                  "Bên nhận dùng secret này kiểm header X-JC-Signature của mỗi request webhook. Đóng hộp thoại là không xem lại được.",
              })
            }
          />
        </>
      )}
      <SecretDialog
        secret={shown?.value ?? null}
        title={shown?.title ?? ""}
        description={shown?.description}
        onClose={() => setShown(null)}
      />
    </Section>
  )
}

export function IntegrationPage() {
  const [creating, setCreating] = useState(false)
  const query = useGetApiClients({ limit: 100, sortBy: "created_at", sortOrder: "ASC" })
  const [origin, setOrigin] = useState("")
  useEffect(() => setOrigin(window.location.origin), [])
  const base = `${origin}${INTEGRATION_PATH}`
  const clients = query.data?.data ?? []
  return (
    <>
      <PageHeader
        title="Tích hợp"
        description="Hệ thống ngoài (vd. backend của đối tác) đẩy doanh nghiệp vào và nhận việc làm mới: qua webhook ký HMAC hoặc đọc feed thay đổi. Mỗi hệ thống một khoá API riêng."
        actions={
          <Button onClick={() => setCreating(true)}>
            <Plus /> Thêm hệ thống
          </Button>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-meta text-muted-foreground">
          <span>Địa chỉ API</span>
          <code className="font-mono text-foreground">{base}</code>
          <Button size="xs" variant="ghost" onClick={() => copy(base, "Đã chép địa chỉ")}>
            <Copy /> Chép
          </Button>
        </div>
      </PageHeader>
      <CreateClientDialog open={creating} onOpenChange={setCreating} />
      {query.isError && !query.data && <ErrorState error={query.error} onRetry={() => query.refetch()} />}
      {query.isLoading && <PageLoading />}
      {query.data && !clients.length && (
        <Section>
          <Empty
            icon={<Plug />}
            title="Chưa có hệ thống ngoài nào"
            action={
              <Button onClick={() => setCreating(true)}>
                <Plus /> Thêm hệ thống
              </Button>
            }
          >
            Thêm hệ thống rồi tạo khoá API để bên đó đẩy doanh nghiệp và nhận việc làm mới.
          </Empty>
        </Section>
      )}
      <div className="space-y-6">
        {clients.map((c) => (
          <ClientCard key={c.id} summary={c} />
        ))}
      </div>
    </>
  )
}
