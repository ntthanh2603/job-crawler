import { QueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { ApiError, errorMessage } from "@/lib/api"

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        // Lỗi 4xx (không tồn tại, sai tham số) không thử lại; lỗi mạng / 5xx thử lại một lần.
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
        refetchOnWindowFocus: false,
      },
      mutations: {
        // Mọi thao tác lỗi đều báo bằng toast; mutation nào cần xử lý riêng (vd. lỗi theo ô ở
        // trang Cài đặt) khai báo `onError` của mình, thay cho cái này.
        onError: (e) => toast.error(errorMessage(e)),
      },
    },
  })
}
