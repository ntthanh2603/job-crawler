"""Sinh mã Python từ proto/jcproto/v1/*.proto vào chính thư mục đó (commit cùng file .proto).

uv run python scripts/gen_proto.py           # sinh lại
uv run python scripts/gen_proto.py --check   # chỉ kiểm tra mã đã khớp .proto (CI / test)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parents[1] / "proto"
PROTOS = sorted((ROOT / "jcproto" / "v1").glob("*.proto"))
GENERATED = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py")


def generate(out: Path) -> None:
    include = Path(protoc.__file__).parent / "_proto"
    for proto in PROTOS:
        args = [
            "protoc",
            f"-I{ROOT}",
            f"-I{include}",
            f"--python_out={out}",
            f"--pyi_out={out}",
            f"--grpc_python_out={out}",
            str(proto),
        ]
        if protoc.main(args) != 0:
            raise SystemExit(f"protoc lỗi với {proto}")


def outputs(base: Path) -> list[Path]:
    return [
        base / proto.relative_to(ROOT).parent / (proto.stem + suffix)
        for proto in PROTOS
        for suffix in GENERATED
    ]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="chỉ so sánh, không ghi")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp))
        stale = []
        for fresh, current in zip(outputs(Path(tmp)), outputs(ROOT), strict=True):
            if not current.exists() or current.read_bytes() != fresh.read_bytes():
                stale.append(current)
                if not args.check:
                    shutil.copyfile(fresh, current)
    if args.check and stale:
        print("Mã sinh từ proto đã cũ:", *map(str, stale), sep="\n  ", file=sys.stderr)
        return 1
    print("đã sinh lại:" if stale else "mã sinh từ proto đã khớp", *map(str, stale), sep="\n  ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
