"""Nhiều site careers giả trong một process (phân biệt theo Host) để thử tải build / poll ở quy mô
hàng trăm – hàng nghìn doanh nghiệp, và đo độ trễ phát hiện tin mới khi hệ thống đang tải.

    uv run python scripts/loadtest/fake_sites.py --sites 500 --jobs 8 --port 8766 \
        --domain 192-168-65-254.nip.io --churn 2 --out churn.jsonl --csv sites.csv

- Site thứ i có host `s<i>.<domain>` (nip.io trỏ về IP trong tên: từ container Docker Desktop
  dùng IP của `host.docker.internal`, vd. 192.168.65.254). Mỗi site: trang chủ → `/tuyen-dung` →
  `--jobs` tin (JSON-LD).
- `--csv`: ghi danh sách doanh nghiệp để `jobcrawler import`.
- `--churn N`: mỗi giây đăng trung bình N tin mới trên các site ngẫu nhiên (và gỡ một tin cũ để
  số tin không phình), ghi `{"url", "published_at"}` vào `--out` cho `sla_report.py`.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JD = (
    "Mô tả công việc: phát triển và vận hành hệ thống quản lý kho cho các cửa hàng của công ty, "
    "phối hợp với nhóm sản phẩm, viết tài liệu kỹ thuật. Yêu cầu: ít nhất hai năm kinh nghiệm "
    "ở vị trí tương đương, chủ động, cẩn thận. Quyền lợi: lương tháng 13, bảo hiểm đầy đủ, du "
    "lịch hằng năm, làm việc lai từ xa hai ngày mỗi tuần, đào tạo nội bộ."
)
TITLES = (
    "Kỹ sư phần mềm",
    "Chuyên viên kế toán",
    "Nhân viên kinh doanh",
    "Chuyên viên tuyển dụng",
    "Kỹ sư kiểm thử",
    "Trưởng nhóm vận hành",
    "Chuyên viên phân tích dữ liệu",
    "Nhân viên chăm sóc khách hàng",
)


def company_name(i: int) -> str:
    return f"Công ty TNHH Thử Tải Số {i}"


class Sites:
    def __init__(self, count: int, jobs: int) -> None:
        self.lock = threading.Lock()
        self.next_id = 0
        self.jobs: list[dict[int, str]] = []  # site → {mã tin: tiêu đề}
        for _ in range(count):
            self.jobs.append({self._id(): self._title() for _ in range(jobs)})

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _title(self) -> str:
        return f"{random.choice(TITLES)} {random.randint(100, 999)}"

    def publish(self, site: int) -> int:
        with self.lock:
            job_id = self._id()
            self.jobs[site][job_id] = self._title()
            if len(self.jobs[site]) > 12:
                del self.jobs[site][min(self.jobs[site])]
            return job_id

    def page(self, site: int, path: str) -> str | None:
        name = company_name(site)
        with self.lock:
            jobs = dict(self.jobs[site])
        if path == "/":
            return (
                f"<!doctype html><html lang=vi><head><meta charset=utf-8><title>{name}</title>"
                "</head><body><nav><a href=/gioi-thieu>Giới thiệu</a> <a href=/tuyen-dung>"
                f"Tuyển dụng</a></nav><h1>{name}</h1><p>Phân phối hàng tiêu dùng.</p></body></html>"
            )
        if path == "/tuyen-dung":
            items = "".join(
                f'<li><a href="/tuyen-dung/vi-tri-{j}">{t}</a> — Hà Nội</li>'
                for j, t in sorted(jobs.items(), reverse=True)
            )
            return (
                "<!doctype html><html lang=vi><head><meta charset=utf-8><title>Tuyển dụng</title>"
                f"</head><body><h1>Cơ hội nghề nghiệp tại {name}</h1><p>Các vị trí đang tuyển, "
                f"mời xem chi tiết và nộp hồ sơ trực tuyến.</p><ul>{items}</ul></body></html>"
            )
        if path.startswith("/tuyen-dung/vi-tri-"):
            try:
                job_id = int(path.rsplit("-", 1)[1])
            except ValueError:
                return None
            title = jobs.get(job_id)
            if title is None:
                return None
            posting = {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": title,
                "description": f"<p>{JD}</p>",
                "datePosted": datetime.now(UTC).date().isoformat(),
                "validThrough": "2027-12-31",
                "employmentType": "FULL_TIME",
                "hiringOrganization": {"@type": "Organization", "name": name},
                "jobLocation": {
                    "@type": "Place",
                    "address": {"@type": "PostalAddress", "addressLocality": "Hà Nội"},
                },
            }
            return (
                f"<!doctype html><html lang=vi><head><meta charset=utf-8><title>{title}</title>"
                f'<script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}'
                f"</script></head><body><h1>{title}</h1><p>{JD}</p></body></html>"
            )
        return None


def handler_for(sites: Sites) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            host = (self.headers.get("Host") or "").split(":")[0]
            label = host.split(".", 1)[0]
            path = self.path.split("?")[0].rstrip("/") or "/"
            body: str | None = None
            if label.startswith("s") and label[1:].isdigit() and int(label[1:]) < len(sites.jobs):
                if path == "/robots.txt":
                    body = "User-agent: *\nAllow: /\n"
                else:
                    body = sites.page(int(label[1:]), path)
            data = (body or "<html><body>Không tìm thấy</body></html>").encode()
            self.send_response(200 if body is not None else 404)
            ctype = "text/plain" if path == "/robots.txt" else "text/html"
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", type=int, default=500)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--domain", default="192-168-65-254.nip.io")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--churn", type=float, default=0.0, help="tin mới mỗi giây (trung bình)")
    parser.add_argument("--wait", type=float, default=0.0, help="giây chờ trước tin mới đầu tiên")
    parser.add_argument("--duration", type=float, default=0.0, help="giây đăng tin mới (0 = mãi)")
    parser.add_argument("--out", type=Path, default=Path("churn.jsonl"))
    args = parser.parse_args()
    sites = Sites(args.sites, args.jobs)
    if args.csv:
        with args.csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "tax_code", "website", "note"])
            for i in range(args.sites):
                site = f"http://s{i}.{args.domain}:{args.port}/"
                w.writerow([company_name(i), "", site, "thử tải"])
    server = ThreadingHTTPServer((args.host, args.port), handler_for(sites))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"{args.sites} site ở http://s<i>.{args.domain}:{args.port}/", flush=True)
    if args.churn <= 0:
        while True:
            time.sleep(3600)
    time.sleep(args.wait)
    started = time.monotonic()
    with args.out.open("a", encoding="utf-8") as out:
        while not args.duration or time.monotonic() - started < args.duration:
            time.sleep(random.expovariate(args.churn))
            site = random.randrange(args.sites)
            job_id = sites.publish(site)
            url = f"http://s{site}.{args.domain}:{args.port}/tuyen-dung/vi-tri-{job_id}"
            out.write(json.dumps({"url": url, "published_at": datetime.now(UTC).isoformat()}))
            out.write("\n")
            out.flush()
    print("đã đăng xong; site vẫn chạy", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
