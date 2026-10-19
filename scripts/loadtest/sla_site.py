"""Site careers giả để đo độ trễ phát hiện tin mới đầu-cuối (scheduler → runner → gateway → worker
→ site → lưu), không phụ thuộc site thật có đăng tin hay không.

    uv run python scripts/loadtest/sla_site.py --port 8765 --every 90 --count 10 --out sla.jsonl

Site có trang chủ (link "Tuyển dụng"), trang danh sách `/tuyen-dung`, trang từng tin (JSON-LD
JobPosting). Lúc đầu có sẵn 3 tin; sau đó cứ khoảng `--every` giây (± 50%) đăng thêm một tin và
ghi `{"url", "title", "published_at"}` vào `--out`. Nạp doanh nghiệp với website trỏ tới site này
(trong Docker: `http://host.docker.internal:<port>/`), chờ build xong, rồi đo bằng
`scripts/loadtest/sla_report.py`.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "Công ty TNHH Đo Độ Trễ Tuyển Dụng"
JD = (
    "Mô tả công việc: phát triển và vận hành hệ thống quản lý kho cho các cửa hàng của công ty, "
    "viết mã Python bất đồng bộ, thiết kế cơ sở dữ liệu PostgreSQL, phối hợp với nhóm sản phẩm. "
    "Yêu cầu: ít nhất hai năm kinh nghiệm, hiểu HTTP, biết đọc tài liệu tiếng Anh. Quyền lợi: "
    "lương tháng 13, bảo hiểm đầy đủ, du lịch hằng năm, làm việc lai từ xa hai ngày mỗi tuần."
)
TITLES = [
    "Kỹ sư phần mềm Python",
    "Chuyên viên kế toán tổng hợp",
    "Nhân viên kinh doanh khu vực",
    "Chuyên viên tuyển dụng",
    "Kỹ sư kiểm thử phần mềm",
    "Trưởng nhóm vận hành kho",
    "Chuyên viên phân tích dữ liệu",
    "Nhân viên chăm sóc khách hàng",
    "Kỹ sư hạ tầng hệ thống",
    "Chuyên viên truyền thông nội bộ",
    "Kỹ sư phát triển giao diện",
    "Chuyên viên pháp chế",
    "Nhân viên thu mua",
    "Kỹ sư dữ liệu",
    "Chuyên viên nhân sự tổng hợp",
]


class Board:
    def __init__(self, out: Path, name: str, start_id: int = 1000) -> None:
        self.lock = threading.Lock()
        self.jobs: list[tuple[str, str]] = []  # (path, tiêu đề)
        self.out = out
        self.name = name
        self.next_id = start_id

    def publish(self, *, log: bool = True) -> tuple[str, str]:
        with self.lock:
            self.next_id += 1
            title = f"{TITLES[self.next_id % len(TITLES)]} {self.next_id}"
            path = f"/tuyen-dung/vi-tri-{self.next_id}"
            self.jobs.append((path, title))
        if log:
            line = {"path": path, "title": title, "published_at": datetime.now(UTC).isoformat()}
            with self.out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            print("đăng", path, title, flush=True)
        return path, title

    def detail(self, path: str) -> str | None:
        with self.lock:
            title = next((t for p, t in self.jobs if p == path), None)
        if title is None:
            return None
        posting = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": title,
            "description": f"<p>{JD}</p><p>Vị trí: {title}.</p>",
            "datePosted": datetime.now(UTC).date().isoformat(),
            "validThrough": "2027-12-31",
            "employmentType": "FULL_TIME",
            "hiringOrganization": {"@type": "Organization", "name": self.name},
            "jobLocation": {
                "@type": "Place",
                "address": {"@type": "PostalAddress", "addressLocality": "Hà Nội"},
            },
        }
        return (
            f"<!doctype html><html lang=vi><head><meta charset=utf-8><title>{title}</title>"
            f'<script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}'
            f"</script></head><body><header><a href=/>Trang chủ</a></header><main><h1>{title}"
            f"</h1><p>{JD}</p><p>Địa điểm làm việc: Hà Nội. Hạn nộp hồ sơ: 31/12/2027.</p>"
            "</main><footer>© " + self.name + "</footer></body></html>"
        )

    def listing(self) -> str:
        with self.lock:
            items = "".join(
                f'<li class="job-item"><a href="{p}">{t}</a> — Hà Nội</li>'
                for p, t in reversed(self.jobs)
            )
        return (
            "<!doctype html><html lang=vi><head><meta charset=utf-8><title>Tuyển dụng</title>"
            "</head><body><header><a href=/>Trang chủ</a></header><main>"
            "<h1>Cơ hội nghề nghiệp tại " + self.name + "</h1>"
            "<p>Chúng tôi đang tìm kiếm những đồng nghiệp mới cho các vị trí dưới đây. Ứng viên "
            "quan tâm vui lòng xem chi tiết từng vị trí và nộp hồ sơ trực tuyến.</p>"
            f'<ul class="jobs">{items}</ul></main><footer>© {self.name}</footer></body></html>'
        )

    def home(self) -> str:
        return (
            "<!doctype html><html lang=vi><head><meta charset=utf-8><title>"
            f"{self.name}</title></head><body><nav><a href=/gioi-thieu>Giới thiệu</a> "
            "<a href=/tuyen-dung>Tuyển dụng</a></nav><main><h1>"
            f"{self.name}</h1><p>Doanh nghiệp phân phối hàng tiêu dùng.</p></main>"
            f"<footer>{self.name} — Mã số thuế 0109999999</footer></body></html>"
        )


def handler_for(board: Board) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/":
                self._send(200, board.home())
            elif path == "/robots.txt":
                self._send(200, "User-agent: *\nAllow: /\n", "text/plain")
            elif path == "/tuyen-dung":
                self._send(200, board.listing())
            elif (page := board.detail(path)) is not None:
                self._send(200, page)
            else:
                self._send(404, "<html><body>Không tìm thấy</body></html>")

    return Handler


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--every", type=float, default=90, help="giây giữa hai tin mới (± 50%%)")
    parser.add_argument("--count", type=int, default=10, help="số tin mới đăng rồi dừng đăng")
    parser.add_argument("--wait", type=float, default=0, help="giây chờ trước tin mới đầu tiên")
    parser.add_argument("--name", default=NAME)
    parser.add_argument(
        "--start-id", type=int, default=1000, help="mã tin đầu (chạy lại thì đổi để không trùng)"
    )
    parser.add_argument("--out", type=Path, default=Path("sla.jsonl"))
    args = parser.parse_args()
    board = Board(args.out, args.name, args.start_id)
    for _ in range(3):
        board.publish(log=False)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(board))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"site ở http://{args.host}:{args.port}/ — ghi tin mới vào {args.out}", flush=True)
    time.sleep(args.wait)
    for _ in range(args.count):
        time.sleep(args.every * random.uniform(0.5, 1.5))
        board.publish()
    print("đã đăng đủ; site vẫn chạy (Ctrl+C để dừng)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
