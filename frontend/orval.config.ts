// Sinh hàm gọi API + hook React Query + kiểu từ spec của backend (như console/ của open-asm):
// ../backend/openapi.json (bản commit, backend ghi bằng `backend/scripts/export_openapi.py`)
// → src/lib/gen/queries.ts. `npm run gen:api` (`task gen-api`). File sinh ra không sửa tay.
//
// `httpClient: "axios"` chỉ để chọn dạng sinh ra (hàm nhận `{ url, method, params, data }`, trả thẳng
// dữ liệu); gọi thật qua `apiClient` (fetch) trong src/lib/api.ts, không dùng axios.
import { defineConfig, type InputTransformerFn } from "orval"

/** Spec do backend sở hữu (bên sinh hợp đồng); đường dẫn tính từ frontend/. */
export const OPENAPI_SPEC = "../backend/openapi.json"

/**
 * Bỏ các endpoint Server-Sent Events (`text/event-stream`, vd. `GET /api/events`) trước khi orval
 * kiểm và sinh: giao diện mở chúng bằng EventSource (src/lib/live.ts), không phải hàm / hook fetch;
 * spec ghi `itemSchema` (OpenAPI 3.2) mà bộ kiểm 3.1 của orval không nhận. Kiểu dữ liệu sự kiện
 * (`LiveOut`) vẫn được sinh như mọi schema.
 */
export const withoutEventStreams: InputTransformerFn = (spec) => {
  const paths = spec.paths ?? {}
  for (const [path, item] of Object.entries(paths)) {
    const ops = item as Record<string, { responses?: Record<string, { content?: Record<string, unknown> }> }>
    for (const [method, op] of Object.entries(ops)) {
      if (op?.responses?.["200"]?.content?.["text/event-stream"]) delete ops[method]
    }
    if (!Object.keys(ops).length) delete paths[path]
  }
  return spec
}

/**
 * Bỏ API cho hệ thống ngoài (`/api/integration/v1/...`): chỉ gọi được bằng khoá API, giao diện không
 * dùng. Mô hình của chúng chỉ còn khi giao diện có dùng ở chỗ khác.
 */
export const withoutIntegrationApi: InputTransformerFn = (spec) => {
  const paths = spec.paths ?? {}
  for (const path of Object.keys(paths)) {
    if (path.startsWith("/api/integration/")) delete paths[path]
  }
  return spec
}

export default defineConfig({
  api: {
    input: {
      target: OPENAPI_SPEC,
      override: { transformer: async (spec) => withoutIntegrationApi(await withoutEventStreams(spec)) },
    },
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
