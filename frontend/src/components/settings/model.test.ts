import { describe, expect, it } from "vitest"

import type { SettingItem } from "@/lib/types"

import {
  describeCron,
  describeWindow,
  fold,
  formatValue,
  humanSeconds,
  isDirty,
  localError,
  parseWindow,
  toPayload,
  windowSegments,
} from "./model"

function item(over: Partial<SettingItem> = {}): SettingItem {
  return {
    key: "k",
    label: "K",
    help: "",
    kind: "int",
    options: [],
    unit: null,
    min: null,
    max: null,
    min_exclusive: false,
    max_exclusive: false,
    nullable: false,
    placeholder: null,
    value_type: null,
    live: true,
    value: 5,
    default: 5,
    env_value: null,
    source: "default",
    secret: false,
    is_set: null,
    env_is_set: null,
    auto_value: null,
    ...over,
  }
}

describe("isDirty", () => {
  it("chưa sửa thì không đổi", () => {
    expect(isDirty(item(), {})).toBe(false)
  })

  it("số: so theo giá trị, không theo chữ gõ", () => {
    expect(isDirty(item(), { k: "5" })).toBe(false)
    expect(isDirty(item(), { k: " 5 " })).toBe(false)
    expect(isDirty(item(), { k: "6" })).toBe(true)
    expect(isDirty(item({ kind: "float", value: 2.5 }), { k: "2,5" })).toBe(false)
  })

  it("về mặc định (null) chỉ là thay đổi khi đang đặt ở giao diện", () => {
    expect(isDirty(item({ source: "ui" }), { k: null })).toBe(true)
    expect(isDirty(item({ source: "env" }), { k: null })).toBe(false)
    expect(isDirty(item({ source: "default" }), { k: null })).toBe(false)
  })

  it("bí mật: ô trống là giữ nguyên", () => {
    const secret = item({ kind: "secret", secret: true, value: null })
    expect(isDirty(secret, { k: "" })).toBe(false)
    expect(isDirty(secret, { k: "sk-new" })).toBe(true)
  })

  it("danh sách nhiều dòng và bảng không phụ thuộc thứ tự khoá / dòng trống", () => {
    const list = item({ kind: "list", value: ["a", "b"] })
    expect(isDirty(list, { k: "a\n b \n\n" })).toBe(false)
    expect(isDirty(list, { k: "a" })).toBe(true)
    const dict = item({ kind: "dict", value_type: "float", value: { b: 1, a: 2 } })
    expect(
      isDirty(dict, {
        k: [
          ["a", "2"],
          ["b", "1"],
        ],
      }),
    ).toBe(false)
    expect(isDirty(dict, { k: [["a", "3"]] })).toBe(true)
  })
})

describe("toPayload", () => {
  it("null giữ nguyên (về mặc định)", () => {
    expect(toPayload(item(), null)).toBeNull()
  })

  it("số: chuỗi → number, dấu phẩy thập phân, ô trống giữ chuỗi rỗng", () => {
    expect(toPayload(item(), "12")).toBe(12)
    expect(toPayload(item({ kind: "float" }), "1,5")).toBe(1.5)
    expect(toPayload(item(), "  ")).toBe("")
  })

  it("danh sách: nhiều dòng → mảng đã cắt khoảng trắng; danh sách chọn giữ mảng", () => {
    expect(toPayload(item({ kind: "list" }), " a \n\nb\n")).toEqual(["a", "b"])
    expect(toPayload(item({ kind: "list" }), ["topcv", "itviec"])).toEqual(["topcv", "itviec"])
  })

  it("bảng: bỏ dòng không khoá, số với value_type float", () => {
    const rows: [string, string][] = [
      [" a ", "1,5"],
      ["", "9"],
      ["b", " x "],
    ]
    expect(toPayload(item({ kind: "dict", value_type: "float" }), rows)).toEqual({ a: 1.5, b: "x" })
    expect(toPayload(item({ kind: "dict", value_type: "str" }), [["a", "1"]])).toEqual({ a: "1" })
  })

  it("kiểu khác giữ nguyên", () => {
    expect(toPayload(item({ kind: "bool" }), true)).toBe(true)
    expect(toPayload(item({ kind: "str" }), "x")).toBe("x")
  })
})

describe("localError", () => {
  it("số: trống, không phải số, không nguyên", () => {
    expect(localError(item(), "")).toBe("Không được để trống")
    expect(localError(item({ nullable: true }), "")).toBeNull()
    expect(localError(item(), "abc")).toBe("Phải là số")
    expect(localError(item(), "1.5")).toBe("Phải là số nguyên")
    expect(localError(item({ kind: "float" }), "1,5")).toBeNull()
  })

  it("số: giới hạn trên / dưới, có và không tính biên", () => {
    expect(localError(item({ min: 1 }), "0")).toBe("Phải lớn hơn hoặc bằng 1")
    expect(localError(item({ min: 1 }), "1")).toBeNull()
    expect(localError(item({ min: 0, min_exclusive: true }), "0")).toBe("Phải lớn hơn 0")
    expect(localError(item({ max: 10 }), "11")).toBe("Phải nhỏ hơn hoặc bằng 10")
    expect(localError(item({ max: 10, max_exclusive: true }), "10")).toBe("Phải nhỏ hơn 10")
    expect(localError(item({ kind: "float", min: 0.5 }), "0,4")).toBe("Phải lớn hơn hoặc bằng 0,5")
  })

  it("khung giờ và cron", () => {
    expect(localError(item({ kind: "window" }), "00:00-06:00")).toBeNull()
    expect(localError(item({ kind: "window" }), "sáng")).toBe("Khung giờ phải có dạng HH:MM-HH:MM")
    expect(localError(item({ kind: "cron" }), "*/2 6-23 * * *")).toBeNull()
    expect(localError(item({ kind: "cron" }), "* * *")).toBe("Biểu thức cron cần 5 trường: phút giờ ngày tháng thứ")
  })

  it("bảng số", () => {
    const dict = item({ kind: "dict", value_type: "float" })
    expect(localError(dict, [["a", "x"]])).toBe("a: phải là số")
    expect(localError(dict, [["a", "0"]])).toBe("a: phải lớn hơn 0")
    expect(localError(dict, [["", "x"]])).toBeNull()
  })

  it("null (về mặc định) luôn hợp lệ", () => {
    expect(localError(item(), null)).toBeNull()
  })
})

describe("parseWindow", () => {
  it("khung trong ngày", () => {
    expect(parseWindow("00:00-06:00")).toEqual({ start: "00:00", end: "06:00", minutes: 360, overnight: false, allDay: false })
    expect(parseWindow(" 8:30 - 17:45 ")).toMatchObject({ start: "08:30", end: "17:45", minutes: 555 })
  })

  it("khung qua nửa đêm", () => {
    expect(parseWindow("22:00-05:00")).toMatchObject({ minutes: 420, overnight: true, allDay: false })
  })

  it("cả ngày", () => {
    expect(parseWindow("00:00-24:00")).toMatchObject({ minutes: 1440, allDay: true, overnight: false })
    expect(parseWindow("06:00-06:00")).toMatchObject({ minutes: 1440, allDay: true })
  })

  it("sai dạng", () => {
    expect(parseWindow("6:60-7:00")).toBeNull()
    expect(parseWindow("25:00-26:00")).toBeNull()
    expect(parseWindow("sáng")).toBeNull()
  })
})

describe("windowSegments", () => {
  it("chia khung giờ thành các đoạn trong ngày", () => {
    expect(windowSegments("00:00-06:00")).toEqual([[0, 360]])
    expect(windowSegments("22:00-05:00")).toEqual([
      [1320, 1440],
      [0, 300],
    ])
    expect(windowSegments("22:00-24:00")).toEqual([[1320, 1440]])
    expect(windowSegments("22:00-00:00")).toEqual([[1320, 1440]])
    expect(windowSegments("00:00-24:00")).toEqual([[0, 1440]])
    expect(windowSegments("sai")).toEqual([])
  })

  it("describeWindow", () => {
    expect(describeWindow("22:00-05:00")).toBe("22:00 → 05:00 hôm sau (7 giờ)")
    expect(describeWindow("00:00-06:30")).toBe("00:00 → 06:30 (6,5 giờ)")
    expect(describeWindow("00:00-24:00")).toBe("Cả ngày (24 giờ)")
    expect(describeWindow("x")).toBe("Khung giờ chưa hợp lệ")
  })
})

describe("describeCron", () => {
  it("các mẫu hay gặp", () => {
    expect(describeCron("*/2 6-23 * * *")).toBe("Mỗi 2 phút, 6h–23h")
    expect(describeCron("30 2 * * *")).toBe("Mỗi ngày lúc 02:30")
    expect(describeCron("0 3 * * 1")).toBe("Lúc 03:00, thứ Hai")
    expect(describeCron("15 * * * *")).toBe("Mỗi giờ, vào phút 15")
    expect(describeCron("* * * * *")).toBe("Mỗi phút")
    expect(describeCron("0 */6 * * *")).toBe("Vào phút 0, mỗi 6 giờ")
    expect(describeCron("0 0 1 * *")).toBe("Lúc 00:00, ngày 1")
  })

  it("không đủ 5 trường thì không mô tả", () => {
    expect(describeCron("* * *")).toBeNull()
  })
})

describe("khác", () => {
  it("humanSeconds", () => {
    expect(humanSeconds(90)).toBeNull()
    expect(humanSeconds(150)).toBe("2,5 phút")
    expect(humanSeconds(7200)).toBe("2 giờ")
    expect(humanSeconds(86_400)).toBe("1 ngày")
  })

  it("formatValue", () => {
    expect(formatValue(item({ kind: "bool" }), true)).toBe("Bật")
    expect(formatValue(item({ kind: "secret" }), "x")).toBe("Ẩn")
    expect(formatValue(item({ unit: "giây" }), 1500)).toBe("1.500 giây")
    expect(formatValue(item({ unit: "byte" }), 2048)).toBe("2,0 KB")
    expect(formatValue(item({ kind: "list", key: "boards" }), [])).toBe("Mọi trang đang bật")
    expect(formatValue(item({ kind: "list", key: "boards" }), ["topcv", "itviec"])).toBe("TopCV, ITviec")
    expect(formatValue(item({ kind: "str" }), "")).toBe("Trống")
  })

  it("fold bỏ dấu để tìm", () => {
    expect(fold("Độ trễ phát hiện")).toBe("do tre phat hien")
  })
})
