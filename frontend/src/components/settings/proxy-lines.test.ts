import { describe, expect, it } from "vitest"

import {
  checkErrorText,
  parsePastedProxies,
  parseProxyLine,
  proxyIdentity,
  proxyRowError,
  toProxyLine,
  type ProxyRow,
} from "./proxy-lines"

describe("parseProxyLine", () => {
  it("đọc các định dạng máy chủ nhận", () => {
    expect(parseProxyLine("http://u%40x:p%3As@1.2.3.4:8080")).toEqual({
      scheme: "http",
      host: "1.2.3.4",
      port: "8080",
      username: "u@x",
      password: "p:s",
      masked: null,
    })
    expect(parseProxyLine("socks5://10.0.0.2:1080  # ghi chú")).toMatchObject({ scheme: "socks5", username: "", password: "" })
    expect(parseProxyLine("1.2.3.4:3128")).toMatchObject({ scheme: "http", host: "1.2.3.4", port: "3128", password: "" })
    expect(parseProxyLine("1.2.3.4:3128:bob:pw")).toMatchObject({ username: "bob", password: "pw" })
    expect(parseProxyLine("bob:pw@proxy.vd.vn:80")).toMatchObject({ host: "proxy.vd.vn", username: "bob", password: "pw" })
    expect(parseProxyLine("http://[::1]:8080")).toMatchObject({ host: "::1", port: "8080" })
    expect(parseProxyLine("# chỉ là chú thích")).toBeNull()
    expect(parseProxyLine("khong-hop-le")).toBeNull()
  })

  it("dòng đã che mật khẩu của máy chủ: giữ mật khẩu đã lưu", () => {
    const row = parseProxyLine("http://bob:•••@1.2.3.4:8080")!
    expect(row).toMatchObject({ username: "bob", password: null, masked: "http://bob:•••@1.2.3.4:8080" })
    expect(toProxyLine(row)).toBe("http://bob:•••@1.2.3.4:8080")
    expect(proxyRowError(row)).toBeNull()
    expect(parseProxyLine("1.2.3.4:80:bob:•••")).toMatchObject({ password: null, masked: "1.2.3.4:80:bob:•••" })
  })
})

describe("toProxyLine", () => {
  const row = (patch: Partial<ProxyRow>): ProxyRow => ({
    scheme: "http",
    host: "1.2.3.4",
    port: "8080",
    username: "",
    password: "",
    masked: null,
    ...patch,
  })

  it("mã hoá tài khoản / mật khẩu, bỏ phần đăng nhập khi trống", () => {
    expect(toProxyLine(row({}))).toBe("http://1.2.3.4:8080")
    expect(toProxyLine(row({ scheme: "socks5", username: "u@x", password: "p:s/1" }))).toBe("socks5://u%40x:p%3As%2F1@1.2.3.4:8080")
    expect(toProxyLine(row({ host: "::1" }))).toBe("http://[::1]:8080")
    expect(parseProxyLine(toProxyLine(row({ username: "bob", password: "mật khẩu" })))).toMatchObject({ username: "bob", password: "mật khẩu" })
  })

  it("báo lỗi ô không hợp lệ; đổi proxy đang ẩn mật khẩu thì phải nhập lại", () => {
    expect(proxyRowError(row({ host: "" }))).toBe("Thiếu host")
    expect(proxyRowError(row({ port: "70000" }))).toMatch(/Cổng/)
    expect(proxyRowError(row({ password: "pw" }))).toMatch(/tài khoản/)
    const hidden = parseProxyLine("http://bob:•••@1.2.3.4:8080")!
    expect(proxyRowError({ ...hidden, port: "9090" })).toBe("Đã đổi proxy: nhập lại mật khẩu")
    expect(proxyRowError({ ...hidden, port: "9090", password: "moi" })).toBeNull()
    expect(proxyIdentity(row({ host: "A.vn" }))).toBe(proxyIdentity(row({ host: "a.vn" })))
  })
})

describe("parsePastedProxies", () => {
  it("tách proxy hợp lệ và dòng không đọc được", () => {
    const out = parsePastedProxies("http://u:p@1.1.1.1:80\n\n# bỏ qua\n2.2.2.2:81:u:p\nrác\nhttp://u:•••@3.3.3.3:80\n4.4.4.4:99999")
    expect(out.rows.map(toProxyLine)).toEqual(["http://u:p@1.1.1.1:80", "http://u:p@2.2.2.2:81"])
    expect(out.invalid.map((l) => l.line)).toEqual([5, 6, 7])
  })
})

describe("checkErrorText", () => {
  it("lỗi kiểm tra thành câu ngắn", () => {
    expect(checkErrorText("timeout: Timeout: Failed to perform, curl: (28) ...")).toBe("Quá thời gian chờ")
    expect(checkErrorText("other: no_worker: không có worker")).toMatch(/worker/)
    expect(checkErrorText("http_407")).toBe("Trang kiểm tra trả HTTP 407")
    expect(checkErrorText(null)).toBe("Lỗi")
  })
})
