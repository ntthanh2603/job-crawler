import { describe, expect, it, vi } from "vitest"

import { api, ApiError, errorMessage, qs } from "./api"

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
    const e = await failure(api.get("/jobs/x"))
    expect(e.status).toBe(404)
    expect(e.message).toBe("Không có job này")
    expect(e.fieldErrors).toBeNull()
  })

  it("detail là danh sách lỗi kiểm tra của FastAPI", async () => {
    stubFetch(json({ detail: [{ msg: "field required" }, { msg: "value is not a valid integer" }] }, { status: 422 }))
    const e = await failure(api.get("/x"))
    expect(e.message).toBe("field required; value is not a valid integer")
  })

  it("detail.errors theo từng ô (trang Cài đặt)", async () => {
    const errors = { proxy_url: "URL không hợp lệ", poll_interval: "Phải lớn hơn 0" }
    stubFetch(json({ detail: { errors } }, { status: 422 }))
    const e = await failure(api.put("/settings", { values: {} }))
    expect(e.message).toBe("URL không hợp lệ; Phải lớn hơn 0")
    expect(e.fieldErrors).toEqual(errors)
    expect(e.detail).toEqual({ errors })
  })

  it("thân không phải JSON: dùng mã + chữ trạng thái", async () => {
    stubFetch(new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }))
    const e = await failure(api.get("/x"))
    expect(e.message).toBe("502 Bad Gateway")
    expect(e.detail).toBeUndefined()
  })
})

describe("gọi API", () => {
  it("GET cùng origin dưới /api, trả JSON", async () => {
    const fetch = stubFetch(json({ ok: true }))
    await expect(api.get("/me")).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledWith("/api/me", { credentials: "same-origin" })
  })

  it("POST gửi JSON; không có body thì không gửi", async () => {
    const fetch = stubFetch(json({ queued: true }))
    await api.post("/enqueue", { all: true })
    expect(fetch.mock.calls[0][1]).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"all":true}',
    })
    stubFetch(json({ queued: true }))
    const second = vi.mocked(globalThis.fetch)
    await api.post("/maintenance")
    expect(second.mock.calls[0][1]?.body).toBeUndefined()
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
