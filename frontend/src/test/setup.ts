import { cleanup } from "@testing-library/react"
import { afterEach, vi } from "vitest"

// jsdom không có ResizeObserver; Radix (Checkbox, Switch...) đo kích thước bằng nó.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Không bật `globals` của Vitest nên Testing Library không tự dọn DOM sau mỗi test.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})
