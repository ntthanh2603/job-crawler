// Trang lỗi của router: trang không tồn tại, trang hỏng khi hiển thị, chunk cũ sau khi triển khai.
import { House, RotateCw } from "lucide-react"
import { isRouteErrorResponse, Link, useRouteError } from "react-router"

import { Empty, ErrorState, PageHeader } from "@/components/common"
import { Button } from "@/components/ui/button"
import { isChunkLoadError } from "@/lib/chunk-error"

function ReloadButton() {
  return (
    <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
      <RotateCw /> Tải lại trang
    </Button>
  )
}

export function NotFound() {
  return (
    <>
      <PageHeader title="Không tìm thấy trang" />
      <Empty
        title="Đường dẫn này không tồn tại hoặc đã bị đổi"
        action={
          <Button asChild size="sm">
            <Link to="/">
              <House /> Về trang Tổng quan
            </Link>
          </Button>
        }
      />
    </>
  )
}

function RouteErrorContent({ error }: { error: unknown }) {
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />
  if (isChunkLoadError(error))
    return (
      <>
        <PageHeader title="Cần tải lại trang" />
        <ErrorState
          error={error}
          title="Phiên bản mới đã được triển khai — tải lại trang để tiếp tục"
          message="Trang đang mở thuộc phiên bản cũ nên không tải được phần giao diện còn thiếu."
          action={<ReloadButton />}
        />
      </>
    )
  return (
    <>
      <PageHeader title="Trang gặp lỗi" />
      <ErrorState
        error={isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : error}
        title="Không hiển thị được trang này"
        action={<ReloadButton />}
      />
    </>
  )
}

/** Lỗi của một trang: hiện trong khung ứng dụng (thanh bên vẫn dùng được để sang trang khác). */
export function PageError() {
  return <RouteErrorContent error={useRouteError()} />
}

/** Lỗi ở chính khung ứng dụng: hiện riêng giữa màn hình. */
export function RootError() {
  return (
    <main className="mx-auto flex min-h-svh w-full max-w-3xl flex-col justify-center px-4 py-10 sm:px-6">
      <RouteErrorContent error={useRouteError()} />
    </main>
  )
}
