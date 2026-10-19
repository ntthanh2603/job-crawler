// Lỗi tải chunk JS/CSS của trang (import động) sau khi triển khai bản mới: trình duyệt còn giữ
// index.html cũ, trỏ tới file có hash cũ đã bị xoá khỏi máy chủ.

const CHUNK_ERROR = new RegExp(
  [
    "Failed to fetch dynamically imported module", // Chrome, Edge
    "error loading dynamically imported module", // Firefox
    "Importing a module script failed", // Safari
    "is not a valid JavaScript MIME type", // Safari, máy chủ trả index.html thay cho file .js
    "Unable to preload CSS", // Vite (CSS của chunk)
    "Loading (CSS )?chunk [\\w-]+ failed",
  ].join("|"),
  "i",
)

export function isChunkLoadError(error: unknown): boolean {
  if (error instanceof Error) return error.name === "ChunkLoadError" || CHUNK_ERROR.test(error.message)
  return typeof error === "string" && CHUNK_ERROR.test(error)
}
