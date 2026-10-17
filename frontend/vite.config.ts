import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// API FastAPI (`jobcrawler ui`) chạy ở cổng 8000; dev server Vite chuyển /api sang đó.
const API = process.env.JOBCRAWLER_API ?? "http://127.0.0.1:8000"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    port: 5173,
    proxy: {
      // Đặt Origin bằng địa chỉ API để qua kiểm tra chống CSRF của API.
      "/api": { target: API, changeOrigin: true, headers: { origin: API } },
    },
  },
  build: {
    // FastAPI phục vụ bản build từ trong package Python.
    outDir: path.resolve(import.meta.dirname, "../src/jobcrawler/web/dist"),
    emptyOutDir: true,
    // Công cụ nội bộ: chunk chung (React, Radix, React Query) ~500 KB là chấp nhận được.
    chunkSizeWarningLimit: 700,
  },
})
