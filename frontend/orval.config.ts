// Sinh hàm gọi API + hook React Query + kiểu từ spec của backend (như console/ của open-asm):
// ../backend/openapi.json (bản commit, backend ghi bằng `backend/scripts/export_openapi.py`)
// → src/lib/gen/queries.ts. `npm run gen:api` (`task gen-api`). File sinh ra không sửa tay.
//
// `httpClient: "axios"` chỉ để chọn dạng sinh ra (hàm nhận `{ url, method, params, data }`, trả thẳng
// dữ liệu); gọi thật qua `apiClient` (fetch) trong src/lib/api.ts, không dùng axios.
import { defineConfig } from "orval"

/** Spec do backend sở hữu (bên sinh hợp đồng); đường dẫn tính từ frontend/. */
export const OPENAPI_SPEC = "../backend/openapi.json"

export default defineConfig({
  api: {
    input: { target: OPENAPI_SPEC },
    output: {
      target: "src/lib/gen/queries.ts",
      clean: true,
      client: "react-query",
      httpClient: "axios",
      override: {
        useTypeOverInterfaces: true,
        mutator: { path: "src/lib/api.ts", name: "apiClient" },
      },
    },
  },
})
