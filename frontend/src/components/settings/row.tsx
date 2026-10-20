// Một dòng cài đặt: tên, giải thích, nguồn giá trị (chỉ khi khác mặc định) bên trái; ô nhập, lỗi,
// khôi phục bên phải.
import { RotateCcw, RotateCw } from "lucide-react"
import type { ReactNode } from "react"

import { Hint } from "@/components/common"
import { cap } from "@/lib/format"
import type { SettingItem } from "@/lib/types"
import { cn } from "@/lib/utils"

import { SettingField } from "./fields"
import { currentRaw, envName, fallbackValue, formatValue, isCustom, isDirty, type Draft, type Raw } from "./model"

export function Pill({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1 rounded-md px-1.5 text-caption font-medium whitespace-nowrap",
        className,
      )}
    >
      {children}
    </span>
  )
}

/** Biểu tượng "cần khởi động lại" kèm chú thích (đọc được bằng trình đọc màn hình). */
export function RestartMark({ auto }: { auto: boolean }) {
  return (
    <Hint
      content={
        auto
          ? "Lưu xong backend tự khởi động lại các process để áp dụng (vài giây)"
          : "Chỉ có hiệu lực sau khi backend khởi động lại"
      }
      className="inline-grid size-5 place-items-center rounded-md text-warning-ink"
    >
      <RotateCw className="size-3.5" aria-label="Cần khởi động lại" />
    </Hint>
  )
}

export function SettingRow({
  item,
  draft,
  error,
  timezone,
  autoRestart,
  onChange,
}: {
  item: SettingItem
  draft: Draft
  error: string | undefined
  timezone: string
  autoRestart: boolean
  onChange: (key: string, raw: Raw | undefined) => void
}) {
  const id = `setting-${item.key}`
  const raw = currentRaw(item, draft)
  const dirty = isDirty(item, draft)
  const resetting = draft[item.key] === null && item.source === "ui"
  const fallback = fallbackValue(item)
  const hasEnv = item.env_value !== null || Boolean(item.env_is_set)
  const secretSet = item.secret ? (resetting ? item.env_is_set : item.is_set) : null
  // Lưới chọn nhiều lựa chọn cần cả chiều ngang: ô nhập nằm dưới phần giải thích.
  const wide = item.kind === "list" && (item.options.length > 0 || item.key === "proxies")
  return (
    <div
      id={`row-${item.key}`}
      className={cn(
        "relative grid scroll-mt-24 gap-3 px-4 py-4 sm:px-5",
        !wide && "@2xl:grid-cols-[minmax(0,1fr)_minmax(0,21rem)] @2xl:gap-8 @4xl:grid-cols-[minmax(0,1fr)_minmax(0,25rem)]",
        dirty && "bg-primary/[0.035] before:absolute before:inset-y-0 before:left-0 before:w-0.5 before:bg-primary dark:bg-primary/[0.06]",
        error && "before:bg-destructive",
      )}
    >
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <label htmlFor={id} className="mr-0.5 text-sm font-medium text-foreground">
            {item.label}
          </label>
          {!item.live && <RestartMark auto={autoRestart} />}
          {dirty ? (
            <Pill className="bg-primary text-primary-foreground">{resetting ? "Sẽ khôi phục" : "Chưa lưu"}</Pill>
          ) : item.source === "ui" ? (
            <Pill title="Giá trị đặt ở trang này, đè lên cấu hình triển khai" className="bg-primary/10 text-brand">
              Đặt ở giao diện
            </Pill>
          ) : (
            item.source === "env" && (
              <Pill title="Giá trị lấy từ cấu hình triển khai (biến môi trường / file .env)" className="bg-info/12 text-info-ink">
                Biến môi trường
              </Pill>
            )
          )}
        </div>
        <p className="text-meta text-pretty text-muted-foreground">{item.help}</p>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-caption text-muted-foreground/90">
          {hasEnv && <code className="font-mono text-[0.6875rem] text-muted-foreground/80">{envName(item)}</code>}
          {isCustom(item) &&
            (item.secret ? (
              <span>· Mặc định: {item.key === "llm_api_key" ? "không cần khoá" : "chưa đặt"}</span>
            ) : (
              <span>· Mặc định: {formatValue(item, item.default)}</span>
            ))}
          {item.source === "ui" && hasEnv && (
            <span>· Biến môi trường: {item.secret ? "đã đặt" : formatValue(item, item.env_value)}</span>
          )}
        </p>
      </div>

      <div className="min-w-0 space-y-1.5">
        {item.secret && (
          <div>
            {secretSet ? (
              <Pill className="bg-success/12 text-success-ink">Đã có khoá</Pill>
            ) : (
              <Pill className="bg-muted text-muted-foreground ring-1 ring-border/70 ring-inset">Chưa có khoá</Pill>
            )}
          </div>
        )}
        <SettingField
          item={item}
          value={raw}
          onChange={(v) => onChange(item.key, v)}
          id={id}
          invalid={Boolean(error)}
          timezone={timezone}
        />
        {error && (
          <p role="alert" className="text-meta font-medium text-danger-ink">
            {cap(error)}
          </p>
        )}
        {resetting ? (
          <p className="flex flex-wrap items-center gap-x-2 text-meta text-muted-foreground">
            Khi lưu sẽ về {hasEnv ? "giá trị biến môi trường" : "giá trị mặc định"}
            {!item.secret && <>: {formatValue(item, fallback)}</>}
            <button type="button" className="text-brand hover:underline" onClick={() => onChange(item.key, undefined)}>
              Giữ giá trị đang đặt
            </button>
          </p>
        ) : (
          item.source === "ui" && (
            <button
              type="button"
              onClick={() => onChange(item.key, null)}
              className="inline-flex items-center gap-1 text-meta text-muted-foreground transition-colors hover:text-foreground"
            >
              <RotateCcw className="size-3.5" />
              {item.secret ? "Xoá khoá đặt ở giao diện" : `Về ${hasEnv ? "biến môi trường" : "mặc định"}`}
              {!item.secret && <span className="text-muted-foreground/80">({formatValue(item, fallback)})</span>}
            </button>
          )
        )}
      </div>
    </div>
  )
}
