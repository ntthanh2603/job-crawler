// Ô nhập của từng loại cài đặt (số, bật/tắt, lựa chọn, danh sách, bảng, khung giờ, cron, bí mật).
import { KeyRound, Plus, Trash2 } from "lucide-react"
import { useId, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { bytes, dec, num, sourceLabel } from "@/lib/format"
import type { SettingItem } from "@/lib/types"
import { cn } from "@/lib/utils"

import { describeCron, describeWindow, humanSeconds, parseWindow, type Raw } from "./model"

type FieldProps = {
  item: SettingItem
  value: Exclude<Raw, null>
  onChange: (value: Raw) => void
  id: string
  invalid: boolean
}

/** Ô nhập theo loại cài đặt. */
export function SettingField(props: FieldProps) {
  switch (props.item.kind) {
    case "int":
    case "float":
      return <NumberField {...props} />
    case "bool":
      return <BoolField {...props} />
    case "enum":
      return <EnumField {...props} />
    case "list":
      return props.item.options.length ? <ChoiceListField {...props} /> : <LinesField {...props} />
    case "dict":
      return <DictField {...props} />
    case "window":
      return <WindowField {...props} />
    case "cron":
      return <CronField {...props} />
    case "secret":
      return <SecretField {...props} />
    default:
      return (
        <Input
          id={props.id}
          value={String(props.value)}
          placeholder={props.item.placeholder ?? (props.item.nullable ? "Để trống" : undefined)}
          aria-invalid={props.invalid || undefined}
          onChange={(e) => props.onChange(e.target.value)}
          className="bg-card"
          spellCheck={false}
        />
      )
  }
}

function Hint({ children }: { children: ReactNode }) {
  return <p className="text-meta text-muted-foreground">{children}</p>
}

function boundsText(item: SettingItem): string | null {
  const fmt = (n: number) => (Number.isInteger(n) ? num(n) : dec(n, String(n).split(".")[1]?.length ?? 1))
  const parts: string[] = []
  if (item.min !== null) parts.push(`${item.min_exclusive ? "lớn hơn" : "tối thiểu"} ${fmt(item.min)}`)
  if (item.max !== null) parts.push(`${item.max_exclusive ? "nhỏ hơn" : "tối đa"} ${fmt(item.max)}`)
  return parts.length ? parts.join(" · ") : null
}

function NumberField({ item, value, onChange, id, invalid }: FieldProps) {
  const text = String(value)
  const n = Number(text.replace(",", "."))
  const valid = text.trim() !== "" && !Number.isNaN(n)
  const extra =
    valid && item.unit === "giây"
      ? humanSeconds(n)
      : valid && item.unit === "byte" && n >= 1024
        ? bytes(n)
        : valid && item.unit === "ngày" && n > 0 && n < 1
          ? `${num(n * 24)} giờ`
          : null
  const bounds = boundsText(item)
  return (
    <div className="space-y-1.5">
      <div
        className={cn(
          "flex h-9 w-full items-center overflow-hidden rounded-md border border-input bg-card shadow-xs transition-[box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50 dark:bg-input/30",
          invalid && "border-destructive ring-destructive/20 focus-within:border-destructive focus-within:ring-destructive/20",
        )}
      >
        <input
          id={id}
          type="text"
          inputMode={item.kind === "int" ? "numeric" : "decimal"}
          value={text}
          placeholder={item.nullable ? "Không giới hạn" : undefined}
          aria-invalid={invalid || undefined}
          onChange={(e) => onChange(e.target.value)}
          className="h-full min-w-0 flex-1 bg-transparent px-3 text-sm tabular-nums outline-none placeholder:text-muted-foreground"
        />
        {item.unit && (
          <span className="flex h-full shrink-0 items-center border-l bg-muted/50 px-3 text-meta whitespace-nowrap text-muted-foreground">
            {item.unit}
          </span>
        )}
      </div>
      {(extra || bounds) && (
        <Hint>
          {extra && <span className="text-foreground/80">= {extra}</span>}
          {extra && bounds && " · "}
          {bounds && <span>{bounds.charAt(0).toUpperCase() + bounds.slice(1)}</span>}
        </Hint>
      )}
    </div>
  )
}

function BoolField({ value, onChange, id }: FieldProps) {
  const on = Boolean(value)
  return (
    <label htmlFor={id} className="inline-flex h-9 cursor-pointer items-center gap-2.5 text-sm">
      <Switch id={id} checked={on} onCheckedChange={(v) => onChange(v)} />
      <span className={on ? "font-medium text-foreground" : "text-muted-foreground"}>{on ? "Bật" : "Tắt"}</span>
    </label>
  )
}

function EnumField({ item, value, onChange, id }: FieldProps) {
  const withHelp = item.options.some((o) => o.help)
  if (withHelp) {
    return (
      <RadioGroup id={id} value={String(value)} onValueChange={(v) => onChange(v)} className="gap-2" aria-label={item.label}>
        {item.options.map((o) => (
          <label
            key={o.value}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border bg-card p-3 transition-colors hover:bg-muted/50",
              value === o.value && "border-primary/50 bg-primary/5 hover:bg-primary/8 dark:bg-primary/10",
            )}
          >
            <RadioGroupItem value={o.value} className="mt-0.5" />
            <span className="min-w-0 space-y-0.5">
              <span className="block text-sm font-medium">{o.label}</span>
              {o.help && <span className="block text-meta text-muted-foreground">{o.help}</span>}
            </span>
          </label>
        ))}
      </RadioGroup>
    )
  }
  return (
    <Select value={String(value)} onValueChange={(v) => onChange(v)}>
      <SelectTrigger id={id} className="w-full bg-card">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start">
        {item.options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function LinesField({ item, value, onChange, id, invalid }: FieldProps) {
  const text = String(value)
  const count = text.split("\n").filter((s) => s.trim()).length
  return (
    <div className="space-y-1.5">
      <Textarea
        id={id}
        value={text}
        rows={Math.min(10, Math.max(3, text.split("\n").length + 1))}
        placeholder={item.placeholder ?? "Mỗi dòng một giá trị"}
        aria-invalid={invalid || undefined}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        className="max-h-72 bg-card font-mono text-meta md:text-meta"
      />
      <Hint>{count ? `${num(count)} dòng` : "Trống"} · mỗi dòng một giá trị</Hint>
    </div>
  )
}

/** Danh sách chọn từ các lựa chọn có sẵn (trang tuyển dụng): lưới ô đánh dấu. */
function ChoiceListField({ item, value, onChange, id }: FieldProps) {
  const picked = new Set(value as string[])
  const toggle = (name: string) => {
    const next = new Set(picked)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    // Giữ thứ tự của danh sách lựa chọn (thứ tự ưu tiên).
    onChange(item.options.map((o) => o.value).filter((v) => next.has(v)))
  }
  return (
    <div className="space-y-1.5">
      <div id={id} role="group" aria-label={item.label} className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 md:grid-cols-2 xl:grid-cols-3">
        {item.options.map((o) => (
          <label
            key={o.value}
            title={o.help ?? undefined}
            className={cn(
              "flex min-w-0 cursor-pointer items-center gap-2 rounded-md border bg-card px-2.5 py-2 text-sm transition-colors hover:bg-muted/50",
              picked.has(o.value) && "border-primary/50 bg-primary/5 dark:bg-primary/10",
            )}
          >
            <Checkbox checked={picked.has(o.value)} onCheckedChange={() => toggle(o.value)} />
            <span className="min-w-0 leading-tight">
              <span className="block truncate">{sourceLabel(o.label)}</span>
              {o.help && <span className="block truncate text-caption text-muted-foreground">{o.help}</span>}
            </span>
          </label>
        ))}
      </div>
      <Hint>
        {picked.size ? `Đã chọn ${num(picked.size)} / ${num(item.options.length)} trang` : "Không chọn trang nào: index mọi trang đang bật"}
        {picked.size > 0 && (
          <>
            {" · "}
            <button type="button" className="text-brand hover:underline" onClick={() => onChange([])}>
              Bỏ chọn hết
            </button>
          </>
        )}
      </Hint>
    </div>
  )
}

function DictField({ item, value, onChange, id, invalid }: FieldProps) {
  const rows = value as [string, string][]
  const set = (i: number, at: 0 | 1, text: string) =>
    onChange(rows.map((r, j) => (j === i ? ((at === 0 ? [text, r[1]] : [r[0], text]) as [string, string]) : r)))
  const valueLabel = item.value_type === "float" ? (item.unit ?? "Giá trị") : "Pool"
  return (
    <div id={id} className="space-y-2">
      {rows.length > 0 && (
        <div className="space-y-1.5">
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,9rem)_2rem] gap-1.5 px-0.5 text-caption font-medium text-muted-foreground">
            <span>Tên miền</span>
            <span>{valueLabel}</span>
            <span />
          </div>
          {rows.map(([k, v], i) => (
            <div key={i} className="grid grid-cols-[minmax(0,1fr)_minmax(0,9rem)_2rem] items-center gap-1.5">
              <Input
                value={k}
                // Dòng vừa thêm (trống) nhận focus ngay.
                autoFocus={i === rows.length - 1 && !k && !v}
                placeholder="www.topcv.vn"
                aria-label={`Tên miền dòng ${i + 1}`}
                aria-invalid={invalid || undefined}
                onChange={(e) => set(i, 0, e.target.value)}
                className="h-8 bg-card text-meta md:text-meta"
                spellCheck={false}
              />
              <Input
                value={v}
                inputMode={item.value_type === "float" ? "decimal" : undefined}
                placeholder={item.value_type === "float" ? "0,5" : "residential"}
                aria-label={`${valueLabel} dòng ${i + 1}`}
                aria-invalid={invalid || undefined}
                onChange={(e) => set(i, 1, e.target.value)}
                className="h-8 bg-card text-meta tabular-nums md:text-meta"
                spellCheck={false}
              />
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Xoá dòng ${i + 1}`}
                onClick={() => onChange(rows.filter((_, j) => j !== i))}
                className="text-muted-foreground hover:text-destructive"
              >
                <Trash2 />
              </Button>
            </div>
          ))}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="xs" onClick={() => onChange([...rows, ["", ""]])}>
          <Plus /> Thêm tên miền
        </Button>
        {!rows.length && <span className="text-meta text-muted-foreground">Chưa có tên miền nào</span>}
      </div>
    </div>
  )
}

const TIMES = Array.from({ length: 49 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`)

function TimeSelect({ value, onChange, label, id }: { value: string; onChange: (v: string) => void; label: string; id?: string }) {
  const options = TIMES.includes(value) ? TIMES : [...TIMES, value].sort()
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger id={id} aria-label={label} className="w-full bg-card tabular-nums">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="max-h-72">
        {options.map((t) => (
          <SelectItem key={t} value={t} className="tabular-nums">
            {t}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function WindowField({ value, onChange, id }: FieldProps) {
  const text = String(value)
  const w = parseWindow(text)
  const [start, end] = w ? [w.start, w.end] : ["00:00", "24:00"]
  return (
    <div className="space-y-1.5">
      <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2">
        <TimeSelect id={id} label="Bắt đầu" value={start} onChange={(v) => onChange(`${v}-${end}`)} />
        <span className="text-muted-foreground" aria-hidden>
          →
        </span>
        <TimeSelect label="Kết thúc" value={end} onChange={(v) => onChange(`${start}-${v}`)} />
      </div>
      <Hint>{describeWindow(text)}</Hint>
    </div>
  )
}

function CronField({ item, value, onChange, id, invalid }: FieldProps) {
  const text = String(value)
  const description = describeCron(text)
  return (
    <div className="space-y-1.5">
      <Input
        id={id}
        value={text}
        placeholder={item.placeholder ?? "*/5 * * * *"}
        aria-invalid={invalid || undefined}
        onChange={(e) => onChange(e.target.value)}
        className="bg-card font-mono text-meta md:text-meta"
        spellCheck={false}
      />
      <Hint>{description ? `${description} · giờ UTC` : "Cần 5 trường: phút giờ ngày tháng thứ"}</Hint>
    </div>
  )
}

function SecretField({ item, value, onChange, id, invalid }: FieldProps) {
  const hintId = useId()
  return (
    <div className="space-y-1.5">
      <div className="relative">
        <KeyRound className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          id={id}
          type="password"
          autoComplete="new-password"
          value={String(value)}
          placeholder={item.is_set ? "Nhập để thay khoá khác" : "Chưa đặt — nhập khoá"}
          aria-invalid={invalid || undefined}
          aria-describedby={hintId}
          onChange={(e) => onChange(e.target.value)}
          className="bg-card pl-9"
          spellCheck={false}
        />
      </div>
      <p id={hintId} className="text-meta text-muted-foreground">
        Giá trị đã lưu không bao giờ hiện lại; để trống là giữ nguyên.
      </p>
    </div>
  )
}
