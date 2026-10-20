import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// API của backend chạy ở cổng 8000 (đổi bằng JOBCRAWLER_API); dev server Vite chuyển /api sang đó.
const API = process.env.JOBCRAWLER_API ?? "http://127.0.0.1:8000"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    port: 5173,
    proxy: {
      // Đặt Origin bằng địa chỉ API để qua kiểm tra chống CSRF của API (so Origin với Host).
      "/api": { target: API, changeOrigin: true, headers: { origin: API } },
    },
  },
  build: {
    // Bản build ở frontend/dist (mặc định); image nginx (Dockerfile) phục vụ thư mục này.
    // Công cụ nội bộ: chunk chung (React, Radix, React Query) ~500 KB là chấp nhận được.
    chunkSizeWarningLimit: 700,
  },
})
