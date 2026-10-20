// Giá trị trên trang Cài đặt: chuyển giữa giá trị API và giá trị đang sửa trong ô nhập, kiểm tra
// nhanh phía trình duyệt, mô tả khung giờ / cron / thời lượng bằng chữ.
import { bytes, cap, dec, num, sourceLabel } from "@/lib/format"
import type { SettingGroup, SettingItem, SettingValue } from "@/lib/types"

/**
 * Giá trị đang sửa của một ô: số và chuỗi giữ nguyên chữ người dùng gõ, danh sách nhiều dòng là
 * chuỗi, danh sách chọn (trang tuyển dụng) là mảng, bảng là các cặp [khoá, giá trị].
 * `null` = về giá trị mặc định / biến môi trường khi lưu.
 */
export type Raw = string | boolean | string[] | [string, string][] | null

export type Draft = Record<string, Raw>

/** Giá trị dùng khi bỏ cài đặt ở giao diện: từ biến môi trường nếu có, không thì mặc định. */
export function fallbackValue(item: SettingItem): SettingValue {
  return item.env_value ?? item.default
}

export function toRaw(item: SettingItem, value: SettingValue): Exclude<Raw, null> {
  switch (item.kind) {
    case "int":
    case "float":
      return value === null || value === undefined ? "" : String(value)
    case "bool":
      return Boolean(value)
    case "list":
      // Trang tuyển dụng (ô đánh dấu) và proxy (bảng) giữ mảng; danh sách khác là ô nhiều dòng.
      return item.options.length || item.key === "proxies"
        ? [...((value as string[] | null) ?? [])]
        : ((value as string[] | null) ?? []).join("\n")
    case "dict":
      return Object.entries((value as Record<string, string | number> | null) ?? {}).map(([k, v]): [string, string] => [k, String(v)])
    case "secret":
      return ""
    default:
      return value === null || value === undefined ? "" : String(value)
  }
}

/** Giá trị đang hiển thị của ô (theo bản nháp nếu đã sửa). */
export function currentRaw(item: SettingItem, draft: Draft): Exclude<Raw, null> {
  if (!(item.key in draft)) return toRaw(item, item.value)
  const raw = draft[item.key]
  return raw === null ? toRaw(item, fallbackValue(item)) : raw
}

/** Giá trị gửi API (số → number, danh sách nhiều dòng → mảng, bảng → object). */
export function toPayload(item: SettingItem, raw: Raw): unknown {
  if (raw === null) return null
  switch (item.kind) {
    case "int":
    case "float": {
      const text = String(raw).trim().replace(",", ".")
      return text === "" ? "" : Number(text)
    }
    case "list":
      return Array.isArray(raw)
        ? raw
        : String(raw)
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean)
    case "dict":
      return Object.fromEntries(
        (raw as [string, string][])
          .filter(([k]) => k.trim())
          .map(([k, v]) => {
            const text = v.trim().replace(",", ".")
            return [k.trim(), item.value_type === "float" && text !== "" && !Number.isNaN(Number(text)) ? Number(text) : v.trim()]
          }),
      )
    default:
      return raw
  }
}

function stable(value: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))))
  }
  return JSON.stringify(value ?? null)
}

/** Ô có thay đổi cần lưu không. */
export function isDirty(item: SettingItem, draft: Draft): boolean {
  if (!(item.key in draft)) return false
  const raw = draft[item.key]
  if (raw === null) return item.source === "ui"
  if (item.kind === "secret") return raw !== ""
  return stable(toPayload(item, raw)) !== stable(item.value)
}

/** Kiểm tra nhanh trước khi gửi (máy chủ vẫn kiểm đủ). Trả thông báo lỗi hoặc null. */
export function localError(item: SettingItem, raw: Raw): string | null {
  if (raw === null) return null
  if (item.kind === "int" || item.kind === "float") {
    const text = String(raw).trim().replace(",", ".")
    if (text === "") return item.nullable ? null : "Không được để trống"
    const value = Number(text)
    if (Number.isNaN(value)) return "Phải là số"
    if (item.kind === "int" && !Number.isInteger(value)) return "Phải là số nguyên"
    if (item.min !== null && (item.min_exclusive ? value <= item.min : value < item.min))
      return `Phải ${item.min_exclusive ? "lớn hơn" : "lớn hơn hoặc bằng"} ${dec(item.min, item.min % 1 ? 1 : 0)}`
    if (item.max !== null && (item.max_exclusive ? value >= item.max : value > item.max))
      return `Phải ${item.max_exclusive ? "nhỏ hơn" : "nhỏ hơn hoặc bằng"} ${dec(item.max, item.max % 1 ? 1 : 0)}`
  }
  if (item.kind === "window" && !parseWindow(String(raw))) return "Khung giờ phải có dạng HH:MM-HH:MM"
  if (item.kind === "cron" && String(raw).trim().split(/\s+/).length !== 5)
    return "Biểu thức cron cần 5 trường: phút giờ ngày tháng thứ"
  if (item.kind === "dict" && item.value_type === "float") {
    for (const [k, v] of raw as [string, string][]) {
      if (!k.trim()) continue
      const value = Number(v.trim().replace(",", "."))
      if (v.trim() === "" || Number.isNaN(value)) return `${k.trim()}: phải là số`
      if (value <= 0) return `${k.trim()}: phải lớn hơn 0`
    }
  }
  return null
}

// --- mô tả bằng chữ ----------------------------------------------------------------------------------

/** Số giây → "2,5 phút", "1 ngày"... (null nếu dưới 2 phút: số giây đã đủ dễ đọc). */
export function humanSeconds(seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds < 120) return null
  const units: [number, string][] = [
    [86_400, "ngày"],
    [3600, "giờ"],
    [60, "phút"],
  ]
  for (const [size, unit] of units) {
    if (seconds >= size) {
      const value = seconds / size
      return `${Number.isInteger(value) ? num(value) : dec(value)} ${unit}`
    }
  }
  return null
}

export type WindowInfo = { start: string; end: string; minutes: number; overnight: boolean; allDay: boolean }

export function parseWindow(value: string): WindowInfo | null {
  const m = /^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$/.exec(value)
  if (!m) return null
  const [h1, m1, h2, m2] = m.slice(1).map(Number)
  const start = h1 * 60 + m1
  const end = h2 * 60 + m2
  if (m1 >= 60 || m2 >= 60 || start > 1440 || end > 1440) return null
  const s = start % 1440
  const allDay = s === end % 1440
  const minutes = allDay ? 1440 : (end - s + 1440) % 1440 || 1440
  const pad = (n: number) => String(n).padStart(2, "0")
  return {
    start: `${pad(h1)}:${pad(m1)}`,
    end: `${pad(h2)}:${pad(m2)}`,
    minutes,
    overnight: !allDay && end < s,
    allDay,
  }
}

function hoursText(minutes: number): string {
  const hours = minutes / 60
  return `${Number.isInteger(hours) ? num(hours) : dec(hours)} giờ`
}

/** "00:00 → 06:00 (6 giờ)", "22:00 → 05:00 hôm sau (7 giờ)", "Cả ngày". */
export function describeWindow(value: string): string {
  const w = parseWindow(value)
  if (!w) return "Khung giờ chưa hợp lệ"
  if (w.allDay) return "Cả ngày (24 giờ)"
  return `${w.start} → ${w.end}${w.overnight ? " hôm sau" : ""} (${hoursText(w.minutes)})`
}

const DOW = ["Chủ nhật", "thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật"]

/** Mô tả các mẫu cron hay gặp: "*\/2 6-23 * * *" → "Mỗi 2 phút, 6h–23h". */
export function describeCron(expr: string): string | null {
  const f = expr.trim().split(/\s+/)
  if (f.length !== 5) return null
  const [mi, h, dom, mon, dow] = f
  const isNum = (s: string) => /^\d+$/.test(s)
  const step = (s: string) => /^\*\/(\d+)$/.exec(s)?.[1]
  const range = (s: string) => /^(\d+)-(\d+)$/.exec(s)
  const pad = (s: string) => s.padStart(2, "0")
  const parts: string[] = []
  const daily = dom === "*" && mon === "*" && dow === "*"
  if (isNum(mi) && isNum(h)) {
    parts.push(`${daily ? "mỗi ngày " : ""}lúc ${pad(h)}:${pad(mi)}`)
  } else {
    if (mi === "*") parts.push("mỗi phút")
    else if (step(mi)) parts.push(`mỗi ${step(mi)} phút`)
    else if (isNum(mi)) parts.push(h === "*" ? `mỗi giờ, vào phút ${mi}` : `vào phút ${mi}`)
    else parts.push(`phút ${mi}`)
    const r = range(h)
    if (r) parts.push(`${Number(r[1])}h–${Number(r[2])}h`)
    else if (step(h)) parts.push(`mỗi ${step(h)} giờ`)
    else if (h !== "*") parts.push(`giờ ${h}`)
  }
  if (dom !== "*") parts.push(`ngày ${dom}`)
  if (mon !== "*") parts.push(`tháng ${mon}`)
  if (dow !== "*") parts.push(isNum(dow) && Number(dow) < 8 ? DOW[Number(dow)] : `thứ ${dow}`)
  return cap(parts.join(", "))
}

/** Giá trị dạng chữ ngắn (dòng "Mặc định: …", "Biến môi trường: …"). */
export function formatValue(item: SettingItem, value: SettingValue): string {
  if (item.kind === "secret") return "Ẩn"
  if (value === null || value === undefined || value === "") return "Trống"
  switch (item.kind) {
    case "bool":
      return value ? "Bật" : "Tắt"
    case "enum":
      return item.options.find((o) => o.value === value)?.label ?? String(value)
    case "int":
    case "float": {
      const n = Number(value)
      if (item.unit === "byte") return bytes(n)
      const text = Number.isInteger(n) ? num(n) : dec(n, String(n).split(".")[1]?.length ?? 1)
      return item.unit ? `${text} ${item.unit}` : text
    }
    case "list": {
      const values = value as string[]
      if (!values.length) return item.key === "boards" ? "Mọi trang đang bật" : "Trống"
      return values.map((v) => (item.key === "boards" ? sourceLabel(v) : v)).join(", ")
    }
    case "dict": {
      const entries = Object.entries(value as Record<string, string | number>)
      if (!entries.length) return "Trống"
      return entries.map(([k, v]) => `${k} → ${typeof v === "number" ? dec(v, v % 1 ? 1 : 0) : v}`).join(", ")
    }
    case "window":
      return describeWindow(String(value))
    default:
      return String(value)
  }
}

/** Tên biến môi trường của cài đặt (pydantic-settings: tên field viết hoa, không tiền tố). */
export function envName(item: SettingItem): string {
  return item.key.toUpperCase()
}

/** Giá trị đang dùng khác mặc định (đặt bằng biến môi trường hoặc ở giao diện, và khác giá trị
 * mặc định; biến môi trường đặt trùng mặc định không tính). */
export function isCustom(item: SettingItem): boolean {
  if (item.source === "default") return false
  if (item.secret) return Boolean(item.is_set)
  return stable(item.value) !== stable(item.default)
}

export type SectionView = { title: string; description: string; items: SettingItem[] }

/** Các mục con của nhóm kèm cài đặt; cài đặt không thuộc mục nào (API cũ) gom vào một mục cuối. */
export function sectionsOf(group: SettingGroup, keep: (item: SettingItem) => boolean = () => true): SectionView[] {
  const byKey = new Map(group.items.map((i) => [i.key, i]))
  const placed = new Set<string>()
  const out: SectionView[] = []
  for (const s of group.sections ?? []) {
    const items = s.keys.flatMap((k) => {
      const item = byKey.get(k)
      if (!item) return []
      placed.add(k)
      return keep(item) ? [item] : []
    })
    if (items.length) out.push({ title: s.title, description: s.description, items })
  }
  const rest = group.items.filter((i) => !placed.has(i.key) && keep(i))
  if (rest.length) out.push({ title: out.length ? "Khác" : "", description: "", items: rest })
  return out
}

/** Phút trong ngày của "HH:MM" (24:00 → 1440). */
function minuteOf(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number)
  return h * 60 + m
}

/** Các đoạn [bắt đầu, kết thúc) (phút trong ngày) của khung giờ, để vẽ trên dải 0–24 giờ. */
export function windowSegments(value: string): [number, number][] {
  const w = parseWindow(value)
  if (!w) return []
  if (w.allDay) return [[0, 1440]]
  const start = minuteOf(w.start) % 1440
  const end = minuteOf(w.end)
  if (!w.overnight) return [[start, end || 1440]]
  const parts: [number, number][] = [
    [start, 1440],
    [0, end],
  ]
  return parts.filter(([a, b]) => b > a)
}

/** Độ dài khung giờ bằng chữ: "6 giờ", "7 giờ, qua nửa đêm", "Cả ngày". */
export function windowLength(value: string): string | null {
  const w = parseWindow(value)
  if (!w) return null
  if (w.allDay) return "Cả ngày"
  return `${hoursText(w.minutes)}${w.overnight ? ", qua nửa đêm" : ""}`
}

/** Giờ hiện tại ("14:35") theo múi giờ IANA; null nếu tên múi không hợp lệ. */
export function nowIn(timeZone: string, at: Date = new Date()): string | null {
  try {
    const text = new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(at)
    return /^\d{2}:\d{2}$/.test(text) ? text : null
  } catch {
    return null
  }
}

/** Bỏ dấu tiếng Việt, chữ thường: để tìm "do tre" ra "độ trễ". */
export function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase()
}
