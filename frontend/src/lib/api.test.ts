import { describe, expect, expectTypeOf, it, vi } from "vitest"

import type { JobDetail, Overview, QueuePage } from "@/lib/types"

import {
  drainWorker,
  getJob,
  getJobs,
  getMe,
  getQueue,
  getRunLogs,
  postEnqueue,
  postImport,
  postMaintenance,
  putSettings,
} from "@/lib/gen/queries"

import { ApiError, errorMessage, qs } from "./api"

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" }, ...init })
}

function stubFetch(response: Response) {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(response)
  vi.stubGlobal("fetch", fetch)
  return fetch
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => null,
    (e: unknown) => e,
  )
  expect(error).toBeInstanceOf(ApiError)
  return error as ApiError
}

describe("qs", () => {
  it("bỏ giá trị rỗng / null / false, mã hoá phần còn lại", () => {
    expect(qs({ a: 1, b: "", c: null, d: undefined, e: false, f: true, g: "x y&z" })).toBe("?a=1&f=true&g=x+y%26z")
  })

  it("không có tham số thì chuỗi rỗng", () => {
    expect(qs({})).toBe("")
    expect(qs({ a: "" })).toBe("")
  })
})

describe("đọc lỗi API", () => {
  it("detail là chuỗi", async () => {
    stubFetch(json({ detail: "Không có job này" }, { status: 404, statusText: "Not Found" }))
    const e = await failure(getJob("x"))
    expect(e.status).toBe(404)
    expect(e.message).toBe("Không có job này")
    expect(e.fieldErrors).toBeNull()
  })

  it("detail là danh sách lỗi kiểm tra của FastAPI", async () => {
    stubFetch(json({ detail: [{ msg: "field required" }, { msg: "value is not a valid integer" }] }, { status: 422 }))
    const e = await failure(getMe())
    expect(e.message).toBe("field required; value is not a valid integer")
  })

  it("detail.errors theo từng ô (trang Cài đặt)", async () => {
    const errors = { proxy_url: "URL không hợp lệ", poll_interval: "Phải lớn hơn 0" }
    stubFetch(json({ detail: { errors } }, { status: 422 }))
    const e = await failure(putSettings({ values: {} }))
    expect(e.message).toBe("URL không hợp lệ; Phải lớn hơn 0")
    expect(e.fieldErrors).toEqual(errors)
    expect(e.detail).toEqual({ errors })
  })

  it("thân không phải JSON: dùng mã + chữ trạng thái", async () => {
    stubFetch(new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }))
    const e = await failure(getMe())
    expect(e.message).toBe("502 Bad Gateway")
    expect(e.detail).toBeUndefined()
  })
})

describe("apiClient (hàm orval sinh ra gọi qua đây)", () => {
  it("GET cùng origin, query bỏ giá trị rỗng; tham số path đi nguyên văn (người gọi tự mã hoá)", async () => {
    const fetch = stubFetch(json({ ok: true }))
    await expect(getMe()).resolves.toEqual({ ok: true })
    expect(fetch.mock.calls[0][0]).toBe("/api/me")
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: "GET", credentials: "same-origin", body: undefined })
    stubFetch(json({ data: [] }))
    await getRunLogs("r1", { page: 2 })
    expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe("/api/runs/r1/logs?page=2")
    stubFetch(json({ draining: true }))
    await drainWorker(encodeURIComponent("máy 1/a"))
    expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe("/api/workers/m%C3%A1y%201%2Fa/drain")
    stubFetch(json({ data: [] }))
    await getJobs({ search: "", province: "Hà Nội", salary_from: null, page: 1 })
    expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe("/api/jobs?province=H%C3%A0+N%E1%BB%99i&page=1")
  })

  it("POST gửi JSON; không có body thì không gửi", async () => {
    const fetch = stubFetch(json({ queued: true }))
    await postEnqueue({ all: true })
    expect(fetch.mock.calls[0][1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"all":true}',
    })
    stubFetch(json({ queued: true }))
    await postMaintenance()
    expect(vi.mocked(globalThis.fetch).mock.calls[0][1]?.body).toBeUndefined()
  })

  it("tải file lên: gửi FormData, để trình duyệt tự đặt Content-Type (boundary)", async () => {
    const fetch = stubFetch(json({ rows: 1 }))
    await postImport({ file: new Blob(["name,website\nA,a.vn\n"]), enqueue: true })
    const init = fetch.mock.calls[0][1]!
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).get("enqueue")).toBe("true")
    expect(init.headers).toBeUndefined()
  })

  it("kiểu phản hồi lấy từ spec (tsc kiểm, không chạy)", () => {
    expectTypeOf(getQueue).returns.resolves.toEqualTypeOf<QueuePage>()
    expectTypeOf(getJob).returns.resolves.toEqualTypeOf<JobDetail>()
    const typeOnly = () => {
      // @ts-expect-error: /api/queue trả QueuePage, không phải Overview
      const wrong: Promise<Overview> = getQueue()
      // @ts-expect-error: thiếu id
      void getJob()
      // @ts-expect-error: query không có trong spec
      void getQueue({ pagee: 1 })
      // @ts-expect-error: body sai kiểu
      void putSettings({ value: {} })
      return wrong
    }
    expect(typeof typeOnly).toBe("function")
  })
})

describe("errorMessage", () => {
  it("lỗi API: thông báo của máy chủ, viết hoa đầu", () => {
    expect(errorMessage(new ApiError(400, "sai tham số"))).toBe("Sai tham số")
    expect(errorMessage(new ApiError(500, ""))).toBe("Lỗi 500")
  })

  it("lỗi mạng thành câu tiếng Việt", () => {
    expect(errorMessage(new TypeError("Failed to fetch"))).toBe("Không kết nối được máy chủ")
    expect(errorMessage(new TypeError("NetworkError when attempting to fetch resource."))).toBe("Không kết nối được máy chủ")
  })

  it("lỗi khác", () => {
    expect(errorMessage(new Error("boom"))).toBe("Boom")
    expect(errorMessage("hỏng")).toBe("Hỏng")
    expect(errorMessage(null)).toBe("Đã có lỗi xảy ra")
  })
})
