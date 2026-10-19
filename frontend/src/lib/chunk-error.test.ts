import { describe, expect, it } from "vitest"

import { isChunkLoadError } from "./chunk-error"

describe("isChunkLoadError", () => {
  it("nhận ra lỗi import động của các trình duyệt và Vite", () => {
    for (const message of [
      "Failed to fetch dynamically imported module: https://x/assets/jobs-abc123.js",
      "error loading dynamically imported module: https://x/assets/jobs-abc123.js",
      "Importing a module script failed.",
      "'text/html' is not a valid JavaScript MIME type.",
      "Unable to preload CSS for /assets/overview-abc.css",
    ])
      expect(isChunkLoadError(new TypeError(message))).toBe(true)
    const named = new Error("x")
    named.name = "ChunkLoadError"
    expect(isChunkLoadError(named)).toBe(true)
  })

  it("lỗi khác không phải", () => {
    expect(isChunkLoadError(new TypeError("Failed to fetch"))).toBe(false)
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
  })
})
