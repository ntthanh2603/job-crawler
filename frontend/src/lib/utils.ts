import { type ClassValue, clsx } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// Cỡ chữ riêng của thang (index.css): khai báo để `text-meta` không bị coi là màu chữ và bị
// `text-muted-foreground` ghi đè khi gộp class.
const twMerge = extendTailwindMerge({
  extend: { theme: { text: ["caption", "meta", "title", "prose", "page", "stat"] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
