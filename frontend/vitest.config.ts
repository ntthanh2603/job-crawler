import { defineConfig, mergeConfig } from "vitest/config"

import viteConfig from "./vite.config.ts"

// Dùng lại cấu hình Vite (alias `@/`, plugin React); test chạy trong jsdom.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{ts,tsx}"],
      setupFiles: ["./src/test/setup.ts"],
      clearMocks: true,
      restoreMocks: true,
      unstubGlobals: true,
    },
  }),
)
