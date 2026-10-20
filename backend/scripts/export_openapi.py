"""Ghi spec OpenAPI của API (như `/api/openapi.json`) ra backend/openapi.json (commit cùng mã);
giao diện (frontend/) sinh hàm + kiểu TypeScript từ `../backend/openapi.json` (`npm run gen:api`).

Chạy trong backend/:
uv run python scripts/export_openapi.py           # ghi lại (task api)
uv run python scripts/export_openapi.py --check   # chỉ kiểm tra đã khớp mã (task api:check, CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jobcrawler.bootstrap.app import openapi_document

TARGET = Path(__file__).resolve().parents[1] / "openapi.json"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="chỉ so sánh, không ghi")
    args = parser.parse_args()
    fresh = openapi_document()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if current == fresh:
        print(f"{TARGET} đã khớp mã")
        return 0
    if args.check:
        print(f"{TARGET} đã cũ: chạy `task api` trong backend/", file=sys.stderr)
        return 1
    TARGET.write_text(fresh, encoding="utf-8", newline="\n")
    print(f"đã ghi {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
