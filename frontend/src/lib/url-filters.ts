// Bộ lọc của trang danh sách lưu trên URL (`?q=…&status=…&page=2`): giữ khi tải lại trang, gửi
// link được. Giá trị mặc định không ghi lên URL; đổi bộ lọc thì về trang 1.
import { useMemo } from "react"
import { useSearchParams } from "react-router"

/** Số trang trên URL → số nguyên ≥ 1 ("x", "0", "-2" → 1; "2.7" → 2). */
export function parsePage(raw: string | null | undefined): number {
  const n = Math.floor(Number(raw))
  return Number.isFinite(n) && n >= 1 ? n : 1
}

/** Đọc các bộ lọc có trong `defaults`; thiếu hoặc rỗng thì lấy giá trị mặc định. */
export function parseFilters<K extends string>(params: URLSearchParams, defaults: Record<K, string>): Record<K, string> {
  const out = {} as Record<K, string>
  for (const key of Object.keys(defaults) as K[]) out[key] = params.get(key) || defaults[key]
  return out
}

/** Bộ lọc → tham số URL: bỏ giá trị rỗng, giá trị mặc định và trang 1; khoá lạ không ghi. */
export function filtersToParams<K extends string>(
  values: Partial<Record<K, string>>,
  defaults: Record<K, string>,
  page = 1,
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const key of Object.keys(defaults) as K[]) {
    const value = values[key] ?? ""
    if (value && value !== defaults[key]) out[key] = value
  }
  const p = parsePage(String(page))
  if (p > 1) out.page = String(p)
  return out
}

export type FilterChanges<K extends string> = Partial<Record<K, string>> & { page?: number }

/**
 * Bộ lọc của trang theo URL. `defaults` liệt kê mọi bộ lọc kèm giá trị mặc định (khai báo ngoài
 * component để giữ nguyên tham chiếu).
 *
 * - `update({ status: "expired" })` đổi bộ lọc và về trang 1; `update({ page: 3 })` chỉ đổi trang.
 * - `reset()` xoá mọi bộ lọc (kể cả sắp xếp).
 * - `isFiltered("sort")` có bộ lọc nào khác mặc định không (bỏ qua các khoá truyền vào).
 */
export function useUrlFilters<K extends string>(defaults: Record<K, string>) {
  const [params, setParams] = useSearchParams()
  const filters = useMemo(() => parseFilters(params, defaults), [params, defaults])
  const page = parsePage(params.get("page"))

  const update = (changes: FilterChanges<K>) => {
    const values: Partial<Record<K, string>> = changes
    const next = { ...filters }
    for (const key of Object.keys(defaults) as K[]) if (key in changes) next[key] = values[key] ?? ""
    setParams(filtersToParams(next, defaults, changes.page ?? 1))
  }
  const reset = () => setParams({})
  const isFiltered = (...ignore: K[]) =>
    (Object.keys(defaults) as K[]).some((key) => !ignore.includes(key) && filters[key] !== defaults[key])

  return { filters, page, update, reset, isFiltered }
}
