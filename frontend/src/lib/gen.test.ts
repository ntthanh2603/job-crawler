// @vitest-environment node
import { mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"

import { generate } from "orval"
import { afterAll, describe, expect, it } from "vitest"

import config, { OPENAPI_SPEC } from "../../orval.config"

const ROOT = resolve(import.meta.dirname, "../..")
const tmp = mkdtempSync(join(tmpdir(), "jobcrawler-orval-"))
afterAll(() => rmSync(tmp, { recursive: true, force: true }))

/** Đường dẫn import của mutator đổi theo chỗ ghi file: bỏ khi so sánh. */
const normalize = (text: string) => text.replaceAll("\r\n", "\n").replace(/^import \{ apiClient \} from .*$/m, "")

describe("src/lib/gen/queries.ts", () => {
  it("khớp ../backend/openapi.json (không thì chạy npm run gen:api)", async () => {
    // Cùng cấu hình (và cùng spec) với orval.config.ts, chỉ ghi ra thư mục tạm.
    const { output } = (config as unknown as Record<"api", { output: { override: object } }>).api
    const target = join(tmp, "queries.ts")
    await generate(
      {
        input: { target: resolve(ROOT, OPENAPI_SPEC) },
        output: {
          ...output,
          target,
          override: { ...output.override, mutator: { path: join(ROOT, "src/lib/api.ts"), name: "apiClient" } },
        },
      },
      ROOT,
    )
    const fresh = normalize(readFileSync(target, "utf8"))
    const current = normalize(readFileSync(join(ROOT, "src/lib/gen/queries.ts"), "utf8"))
    expect(current === fresh, "src/lib/gen/queries.ts đã cũ: chạy npm run gen:api").toBe(true)
  }, 60_000)
})
