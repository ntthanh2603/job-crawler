import { cleanup } from "@testing-library/react"
import { afterEach, vi } from "vitest"

// Không bật `globals` của Vitest nên Testing Library không tự dọn DOM sau mỗi test.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})
