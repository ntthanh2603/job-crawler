// Biểu đồ dùng chung (theo skill dataviz): một chuỗi → một màu; ≥ 2 chuỗi → có chú giải; cột
// ≤ 24px, đầu bo 4px, khe 2px màu nền giữa các phần chồng nhau; lưới mảnh liền nét; chữ luôn dùng
// màu chữ (không dùng màu dữ liệu); mục "không rõ" màu xám nhạt để không tranh với dữ liệu.
import type { ReactNode } from "react"
import { Link } from "react-router"
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { num } from "@/lib/format"
import { cn } from "@/lib/utils"

const compactFmt = new Intl.NumberFormat("vi-VN", { notation: "compact", maximumFractionDigits: 1 })

/** Số gọn cho trục: 1,2 N / 3,4 Tr. */
export function compact(value: number): string {
  return Math.abs(value) < 1000 ? String(value) : compactFmt.format(value)
}

export type BarItem = {
  key: string
  label: string
  value: number
  href?: string
  /** Mục "không rõ / không ghi": vẽ xám nhạt. */
  muted?: boolean
  hint?: string
}

/**
 * Danh sách thanh ngang (một chuỗi): nhãn trái, giá trị và tỉ lệ ở đầu thanh. Thanh dài theo giá
 * trị lớn nhất; `total` để tính phần trăm (mặc định tổng các mục).
 */
export function BarList({
  items,
  total,
  empty = "Chưa có dữ liệu.",
  format = num,
  className,
}: {
  items: BarItem[]
  total?: number
  empty?: ReactNode
  /** Định dạng giá trị (mặc định số đếm); khác `num` thì không hiện phần trăm. */
  format?: (value: number) => string
  className?: string
}) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>
  const max = Math.max(...items.map((i) => i.value), 1)
  const sum = total ?? items.reduce((n, i) => n + i.value, 0)
  const showShare = format === num
  return (
    <ul className={cn("space-y-2", className)}>
      {items.map((item) => {
        const share = sum ? item.value / sum : 0
        const row = (
          <>
            <div className="flex items-baseline justify-between gap-3 text-sm">
              <span className={cn("min-w-0 truncate", item.muted ? "text-muted-foreground" : "text-foreground")}>
                {item.label}
              </span>
              <span className="shrink-0 tabular-nums">
                <span className="font-medium text-foreground">{format(item.value)}</span>
                {showShare && (
                  <span className="ml-1.5 inline-block w-8 text-right text-meta text-muted-foreground">
                    {Math.round(share * 100)}%
                  </span>
                )}
              </span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full transition-[width] duration-500"
                style={{
                  width: `${Math.max((item.value / max) * 100, item.value ? 1.5 : 0)}%`,
                  background: item.muted ? "var(--viz-muted)" : "var(--viz-1)",
                }}
              />
            </div>
          </>
        )
        const title = `${item.label}: ${format(item.value)}${showShare ? ` (${(share * 100).toFixed(1)}%)` : ""}${item.hint ? ` · ${item.hint}` : ""}`
        return (
          <li key={item.key} title={title}>
            {item.href ? (
              <Link
                to={item.href}
                className="-mx-2 block rounded-lg px-2 py-1.5 transition-colors hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                {row}
              </Link>
            ) : (
              <div className="-mx-2 px-2 py-1.5">{row}</div>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * Cột xếp chồng theo thời gian. `config`: thứ tự khoá = thứ tự xếp từ dưới lên (đã kiểm định để
 * hai màu kề nhau phân biệt được khi mù màu); `label` đổi giá trị trục X thành chữ hiển thị.
 */
export function StackedColumns({
  data,
  config,
  x,
  tick,
  tooltipLabel,
  height = "h-64",
}: {
  data: object[]
  config: ChartConfig
  x: string
  tick: (value: string) => string
  tooltipLabel?: (value: string) => string
  height?: string
}) {
  const keys = Object.keys(config)
  return (
    <ChartContainer config={config} className={cn("aspect-auto w-full", height)}>
      <BarChart data={data} margin={{ left: 0, right: 4, top: 8 }} barCategoryGap="18%">
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis
          dataKey={x}
          tickLine={false}
          axisLine={false}
          fontSize={12}
          tickMargin={8}
          minTickGap={18}
          tickFormatter={tick}
        />
        <YAxis tickLine={false} axisLine={false} allowDecimals={false} fontSize={12} width={40} tickFormatter={compact} />
        <ChartTooltip
          cursor={{ fill: "var(--muted)", fillOpacity: 0.7 }}
          content={<ChartTooltipContent labelFormatter={(v) => (tooltipLabel ?? tick)(String(v))} />}
        />
        <ChartLegend content={<ChartLegendContent />} itemSorter={null} />
        {keys.map((key, i) => (
          <Bar
            key={key}
            dataKey={key}
            stackId="a"
            fill={`var(--color-${key})`}
            stroke="var(--card)"
            strokeWidth={1.5}
            maxBarSize={24}
            radius={i === keys.length - 1 ? [4, 4, 0, 0] : 0}
          />
        ))}
      </BarChart>
    </ChartContainer>
  )
}

export type ShareItem = { key: string; label: string; value: number; color: string }

/** Một thanh tỉ lệ (phần của tổng) + chú giải có số và phần trăm. Tối đa ~6 phần. */
export function ShareBar({ items }: { items: ShareItem[] }) {
  const sum = items.reduce((n, i) => n + i.value, 0)
  if (!sum) return <p className="text-sm text-muted-foreground">Chưa có dữ liệu.</p>
  return (
    <div className="space-y-4">
      <div className="flex h-3.5 gap-[2px] overflow-hidden rounded-full" role="img" aria-label="Tỉ lệ">
        {items
          .filter((i) => i.value)
          .map((i) => (
            <div
              key={i.key}
              title={`${i.label}: ${num(i.value)} (${((i.value / sum) * 100).toFixed(1)}%)`}
              style={{ width: `${(i.value / sum) * 100}%`, background: i.color }}
            />
          ))}
      </div>
      <ul className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {items.map((i) => (
          <li key={i.key} className="flex items-center gap-2 text-sm">
            <span className="size-2.5 shrink-0 rounded-[3px]" style={{ background: i.color }} aria-hidden />
            <span className="min-w-0 truncate text-muted-foreground">{i.label}</span>
            <span className="ml-auto font-medium tabular-nums">{num(i.value)}</span>
            <span className="w-9 text-right text-meta text-muted-foreground tabular-nums">
              {Math.round((i.value / sum) * 100)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
