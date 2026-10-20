// Trang đăng nhập (ngoài khung ứng dụng): tài khoản UI_USERNAME / UI_PASSWORD trong backend/.env,
// phiên là JWT trong cookie httpOnly do máy chủ đặt. Đăng nhập xong quay lại trang muốn mở (`next`).
import { useQueryClient } from "@tanstack/react-query"
import { Eye, EyeOff, LogIn } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError, errorMessage } from "@/lib/api"
import { useGetMe, useLogin } from "@/lib/gen/queries"

/** Chỉ quay lại đường dẫn trong ứng dụng (không mở link ngoài qua `next`). */
function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login") ? next : "/"
}

export function LoginPage() {
  const [params] = useSearchParams()
  const next = safeNext(params.get("next"))
  const navigate = useNavigate()
  const client = useQueryClient()
  const me = useGetMe({ query: { retry: false } })
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [show, setShow] = useState(false)
  const login = useLogin({
    mutation: {
      onSuccess: () => {
        client.clear()
        navigate(next, { replace: true })
      },
    },
  })

  useEffect(() => {
    document.title = "Đăng nhập · jobcrawler"
  }, [])

  // Đã đăng nhập (hoặc không cần đăng nhập): vào thẳng.
  useEffect(() => {
    if (me.data?.user) navigate(next, { replace: true })
  }, [me.data, navigate, next])

  const error = login.error
  const message =
    error instanceof ApiError && error.status === 401 ? "Sai tên đăng nhập hoặc mật khẩu" : error ? errorMessage(error) : null

  return (
    <main className="grid min-h-svh place-items-center bg-background px-4 py-10">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <img src="/logo-mark.png" alt="" width={48} height={48} className="size-12" />
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">Đăng nhập jobcrawler</h1>
            <p className="text-meta text-muted-foreground">Thu thập tin tuyển dụng</p>
          </div>
        </div>
        <form
          className="space-y-4 rounded-xl border bg-card p-6 shadow-card"
          onSubmit={(e) => {
            e.preventDefault()
            if (!username.trim() || !password || login.isPending) return
            login.mutate({ data: { username: username.trim(), password } })
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="login-user">Tên đăng nhập</Label>
            <Input
              id="login-user"
              value={username}
              autoFocus
              autoComplete="username"
              spellCheck={false}
              onChange={(e) => setUsername(e.target.value)}
              className="bg-card"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="login-pass">Mật khẩu</Label>
            <div className="relative">
              <Input
                id="login-pass"
                type={show ? "text" : "password"}
                value={password}
                autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
                className="bg-card pr-9"
              />
              <button
                type="button"
                aria-label={show ? "Ẩn mật khẩu" : "Hiện mật khẩu"}
                onClick={() => setShow((s) => !s)}
                className="absolute inset-y-0 right-0 grid w-9 place-items-center text-muted-foreground hover:text-foreground"
              >
                {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
          </div>
          {message && (
            <p role="alert" className="text-meta font-medium text-danger-ink">
              {message}
            </p>
          )}
          <Button type="submit" className="w-full" disabled={!username.trim() || !password || login.isPending}>
            <LogIn /> {login.isPending ? "Đang đăng nhập…" : "Đăng nhập"}
          </Button>
        </form>
      </div>
    </main>
  )
}
