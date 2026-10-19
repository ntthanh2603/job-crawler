// Sinh hàm gọi API + hook React Query + kiểu từ spec của backend (như console/ của open-asm):
// openapi.json (bản commit, `scripts/export_openapi.py` ở gốc repo) → src/lib/gen/queries.ts.
// `npm run gen:api` (hoặc `task api` ở gốc repo, ghi spec trước). File sinh ra không sửa tay.
//
// `httpClient: "axios"` chỉ để chọn dạng sinh ra (hàm nhận `{ url, method, params, data }`, trả thẳng
// dữ liệu); gọi thật qua `apiClient` (fetch) trong src/lib/api.ts, không dùng axios.
import { defineConfig } from "orval"

export default defineConfig({
  api: {
    input: { target: "./openapi.json" },
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
