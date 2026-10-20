// Popup Cài đặt: mở trên trang đang xem theo tham số URL `?settings=<nhóm>` (thanh bên, đường dẫn
// cũ /settings, link chia sẻ), đóng = bỏ tham số. Nội dung tải lười (chỉ khi mở lần đầu).
import { lazy, Suspense } from "react"
import { useSearchParams } from "react-router"

import { PageLoading } from "@/components/common"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"

/** Tham số URL mở popup Cài đặt: `?settings=<nhóm>` (trống = nhóm đầu). */
export const SETTINGS_PARAM = "settings"

const SettingsPanel = lazy(() => import("@/pages/settings").then((m) => ({ default: m.SettingsPanel })))

/** Tham số tìm kiếm mở popup trên trang hiện tại (giữ các tham số khác). */
export function settingsSearch(search: string, group = ""): string {
  const params = new URLSearchParams(search)
  params.set(SETTINGS_PARAM, group)
  return `?${params.toString()}`
}

export function SettingsDialog() {
  const [params, setParams] = useSearchParams()
  const open = params.has(SETTINGS_PARAM)
  const close = () => {
    const next = new URLSearchParams(params)
    next.delete(SETTINGS_PARAM)
    setParams(next)
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent
        className="flex h-[min(56rem,calc(100svh-2rem))] flex-col gap-0 overflow-hidden p-0 sm:max-w-6xl max-sm:top-0 max-sm:left-0 max-sm:h-svh max-sm:max-h-svh max-sm:max-w-full max-sm:translate-x-0 max-sm:translate-y-0 max-sm:rounded-none max-sm:border-0"
      >
        {open && (
          <Suspense
            fallback={
              <div className="p-6">
                <DialogTitle className="sr-only">Cài đặt</DialogTitle>
                <PageLoading />
              </div>
            }
          >
            <SettingsPanel initialGroup={params.get(SETTINGS_PARAM) || null} />
          </Suspense>
        )}
      </DialogContent>
    </Dialog>
  )
}
