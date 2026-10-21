"""Cài đặt sửa ở giao diện (trang Cài đặt): danh mục, kiểm tra, lưu vào bảng `app_settings`.

Giá trị hiệu lực = `Settings()` của process (biến môi trường / .env) đè bởi các dòng
`app_settings` (key = tên field của `config.Settings`, value = JSON). Lưu xong thì tăng
`<REDIS_PREFIX>:settings:version` và PUBLISH `<REDIS_PREFIX>:settings` để process khác nạp lại,
rẻ nhất là so version:

    if await settings_version(redis, prefix) != last:
        settings = apply_overrides(base, await load_overrides(sessionmaker))

Mục `live=False` chỉ được đọc lúc dựng đối tượng dùng lâu (client LLM, proxy pool, cron, số job
song song của runner...): lưu mà đổi giá trị của chúng thì tăng thêm bộ đếm
`<REDIS_PREFIX>:settings:restart`; `jobcrawler serve` (container `backend`) thấy bộ đếm tăng thì
khởi động lại các process con rồi ghi `...:settings:restart_applied` (`restart_state`). Chạy từng
process riêng thì phải tự khởi động lại. Các field kết nối, bí mật hạ tầng, tên queue, gateway,
đăng nhập giao diện, log chỉ đặt được bằng biến môi trường (`ENV_ONLY`), không bao giờ trả ra API.

Cài đặt đặt bằng biến môi trường (`.env`) chép được vào bảng (`import_env`) để quản lý hẳn ở trang
Cài đặt.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import annotated_types as at
from croniter import croniter
from pydantic import SecretStr, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.common import sysres
from jobcrawler.common.config import Settings, parse_window
from jobcrawler.common.logging import get_logger
from jobcrawler.database.models import AppSetting
from jobcrawler.modules.audit import audit_service
from jobcrawler.services import secretbox
from jobcrawler.services.net.proxy_pool import ProxyParseError, parse_proxy_line

log = get_logger(__name__)

Kind = Literal["int", "float", "bool", "str", "enum", "secret", "list", "dict", "window", "cron"]
Source = Literal["default", "env", "ui"]

# Ô bí mật hiển thị thay cho giá trị thật; gửi lại đúng chuỗi này (hoặc chuỗi rỗng) = không đổi.
MASK = "••••••••"
# Mật khẩu trong proxy hiển thị thành chuỗi này (`http://user:•••@host:port`).
PROXY_MASK = "•••"

# Chỉ đặt bằng biến môi trường / .env / file bí mật (SECRETS_DIR): kết nối (cả Redis cache), bí
# mật hạ tầng, tên queue, gateway, đăng nhập giao diện (Basic, header SSO, đăng xuất), token số đo,
# watchdog (đọc một lần lúc process khởi động), log. Không sửa ở giao diện, không trả ra API.
ENV_ONLY: frozenset[str] = frozenset(
    {
        "database_url",
        "redis_url",
        "cache_redis_url",
        "db_pool_size",
        "db_max_overflow",
        "redis_prefix",
        "build_queue_name",
        "poll_queue_name",
        "board_queue_name",
        "gateway_host",
        "gateway_port",
        "gateway_token",
        "gateway_tls_cert",
        "gateway_tls_key",
        "ui_username",
        "ui_password",
        "jwt_secret",
        "encryption_keys",
        "auth_token_hours",
        "auth_proxy_user_header",
        "auth_proxy_email_header",
        "auth_logout_url",
        "watchdog_seconds",
        "demo_companies",
        "log_level",
        "log_format",
        "log_db_level",
        "proxies_file",
    }
)


class SettingsError(ValueError):
    """Giá trị không hợp lệ: `errors` là tên cài đặt → thông báo tiếng Việt."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str
    help: str | None = None


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Một cài đặt sửa được ở giao diện. `key` là tên field của `config.Settings`."""

    key: str
    kind: Kind
    label: str
    help: str
    # True: process đọc giá trị lúc dùng (qua `res.settings`), đổi là có hiệu lực ngay khi nạp lại;
    # False: chỉ đọc lúc dựng đối tượng, cần khởi động lại service.
    live: bool = True
    unit: str | None = None
    options: tuple[Option, ...] = ()
    placeholder: str | None = None
    # Kiểu giá trị của bảng `dict` ("str" | "float"), để giao diện vẽ ô nhập phù hợp.
    value_type: str | None = None
    # Kiểm tra / chuẩn hoá thêm sau khi qua kiểu của field (raise ValueError).
    check: Callable[[Any], Any] | None = field(default=None, compare=False, repr=False)
    group: str = ""

    @property
    def field(self) -> FieldInfo:
        return Settings.model_fields[self.key]

    @property
    def nullable(self) -> bool:
        return type(None) in typing.get_args(self.field.annotation)

    @property
    def min(self) -> float | None:
        return _bounds(self.field)[0]

    @property
    def max(self) -> float | None:
        return _bounds(self.field)[1]

    @property
    def min_exclusive(self) -> bool:
        return _bounds(self.field)[2]

    @property
    def max_exclusive(self) -> bool:
        return _bounds(self.field)[3]


@dataclass(frozen=True, slots=True)
class Section:
    """Một mục con trong nhóm (giao diện vẽ thành một khối có tiêu đề)."""

    title: str
    keys: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    title: str
    description: str
    items: tuple[SettingSpec, ...]
    sections: tuple[Section, ...] = ()


def _bounds(info: FieldInfo) -> tuple[float | None, float | None, bool, bool]:
    """(min, max, min loại trừ, max loại trừ) từ ràng buộc của field (`ge`, `gt`, `le`, `lt`)."""
    lo: float | None = None
    hi: float | None = None
    lo_ex = hi_ex = False
    for m in info.metadata:
        if isinstance(m, at.Ge):
            lo, lo_ex = float(m.ge), False  # pyright: ignore[reportArgumentType]
        elif isinstance(m, at.Gt):
            lo, lo_ex = float(m.gt), True  # pyright: ignore[reportArgumentType]
        elif isinstance(m, at.Le):
            hi, hi_ex = float(m.le), False  # pyright: ignore[reportArgumentType]
        elif isinstance(m, at.Lt):
            hi, hi_ex = float(m.lt), True  # pyright: ignore[reportArgumentType]
    return lo, hi, lo_ex, hi_ex


def literal_values(key: str) -> tuple[str, ...]:
    """Các giá trị của field kiểu `Literal[...]`."""
    annotation = Settings.model_fields[key].annotation
    if typing.get_origin(annotation) is not Literal:
        raise TypeError(f"{key} không phải Literal")
    return tuple(str(v) for v in typing.get_args(annotation))


# --- kiểm tra thêm theo loại --------------------------------------------------------------------


def _check_window(value: str) -> str:
    parse_window(value, "Khung giờ")
    start, end = (part.strip() for part in value.split("-"))
    return f"{start.zfill(5)}-{end.zfill(5)}"


def _check_cron(value: str) -> str:
    value = " ".join(value.split())
    if len(value.split(" ")) != 5 or not croniter.is_valid(value):
        raise ValueError("Biểu thức cron không hợp lệ (cần 5 trường: phút giờ ngày tháng thứ)")
    return value


def _check_timezone(value: str) -> str:
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Không có múi giờ {value!r} (ví dụ Asia/Ho_Chi_Minh)") from exc
    return value


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Phải là URL bắt đầu bằng http:// hoặc https://")
    return value


def _check_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("Không được để trống")
    return value


def _check_origin(value: str | None) -> str | None:
    value = _check_url(value)
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("Chỉ gồm scheme://host[:port], không có đường dẫn")
    try:
        parts.port  # noqa: B018 — cổng sai thì ValueError
    except ValueError as exc:
        raise ValueError("Cổng không hợp lệ") from exc
    return f"{parts.scheme}://{parts.netloc}"


def _check_image(value: str) -> str:
    value = value.strip()
    if not value or any(c.isspace() for c in value) or value.startswith("-"):
        raise ValueError("Tên image không hợp lệ, ví dụ ghcr.io/<owner>/jobcrawler-worker:main")
    return value


def _clean_lines(values: Iterable[str]) -> list[str]:
    return [v.strip() for v in values if v.strip()]


def _check_proxies(values: list[str]) -> list[str]:
    out: list[str] = []
    for i, raw in enumerate(values, start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if PROXY_MASK in line:
            raise ValueError(f"Dòng {i}: mật khẩu đang bị ẩn, hãy nhập lại đầy đủ proxy này")
        try:
            parse_proxy_line(line)
        except ProxyParseError as exc:
            raise ValueError(f"Dòng {i} ({mask_proxy(line)}): {exc}") from exc
        if line not in out:
            out.append(line)
    return out


def _check_boards(values: list[str]) -> list[str]:
    names = [o.value for o in board_options()]
    picked = list(dict.fromkeys(_clean_lines(values)))
    unknown = [v for v in picked if v not in names]
    if unknown:
        raise ValueError(f"Không có trang tuyển dụng: {', '.join(unknown)}")
    return [n for n in names if n in picked]  # theo thứ tự ưu tiên độ phủ


def _check_hosts[V](values: dict[str, V]) -> dict[str, V]:
    out: dict[str, V] = {}
    for host, value in values.items():
        name = host.strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
        if not name:
            raise ValueError("Tên miền không được để trống")
        if "/" in name or " " in name:
            raise ValueError(f"Tên miền không hợp lệ: {host!r}")
        out[name] = value
    return out


def _check_rate_limits(values: dict[str, float]) -> dict[str, float]:
    out = _check_hosts(values)
    for host, rate in out.items():
        if rate <= 0:
            raise ValueError(f"{host}: tốc độ phải lớn hơn 0")
    return out


def _check_routes(values: dict[str, str]) -> dict[str, str]:
    out = _check_hosts(values)
    for host, pool in out.items():
        if not pool.strip():
            raise ValueError(f"{host}: chưa nhập tên pool")
        out[host] = pool.strip()
    return out


@lru_cache
def board_options() -> tuple[Option, ...]:
    """Các trang tuyển dụng có adapter (theo thứ tự ưu tiên độ phủ)."""
    from jobcrawler.sources.boards import registry

    out: list[Option] = []
    for name, adapter in registry().items():
        note = adapter.note or None
        if not adapter.enabled:
            note = f"Đang tắt mặc định{': ' + adapter.note if adapter.note else ''}"
        out.append(Option(name, name, note))
    return tuple(out)


# --- danh mục ------------------------------------------------------------------------------------


def _s(key: str, kind: Kind, label: str, help: str, **kw: Any) -> SettingSpec:
    if kind == "window":
        kw.setdefault("check", _check_window)
        kw.setdefault("placeholder", "00:00-06:00")
    elif kind == "cron":
        kw.setdefault("check", _check_cron)
        kw.setdefault("placeholder", "*/5 * * * *")
    return SettingSpec(key=key, kind=kind, label=label, help=help, **kw)


def _enum(key: str, label: str, help: str, texts: Mapping[str, tuple[str, str | None]], **kw: Any):
    """Field `Literal[...]`: lựa chọn lấy từ chính kiểu của field, kèm nhãn / giải thích."""
    options = tuple(Option(v, *texts.get(v, (v, None))) for v in literal_values(key))
    return _s(key, "enum", label, help, options=options, **kw)


def _group(key: str, title: str, description: str, *items: SettingSpec) -> Group:
    return Group(key, title, description, tuple(replace(i, group=key) for i in items))


_CRON_HELP = (
    "Biểu thức cron 5 trường (phút giờ ngày tháng thứ), tính theo múi giờ thu thập "
    "(CRAWL_TIMEZONE)."
)

GROUPS: tuple[Group, ...] = (
    _group(
        "schedule",
        "Lịch thu thập",
        "Ban đêm mỗi doanh nghiệp được build lại bộ khung thu thập (trang danh sách, API của từng "
        "nguồn); ban ngày chỉ poll đúng các trang đó để phát hiện tin mới nhanh.",
        _s(
            "crawl_timezone",
            "str",
            "Múi giờ",
            "Múi giờ dùng để hiểu khung build, khung poll và lịch cron (tên IANA).",
            check=_check_timezone,
            placeholder="Asia/Ho_Chi_Minh",
            # Lịch cron của runner tính theo múi giờ đọc lúc khởi động.
            live=False,
        ),
        _s(
            "build_window",
            "window",
            "Khung giờ build",
            "Mỗi doanh nghiệp được build lại một lần trong khung này (KYC, tìm nguồn, đọc đủ danh "
            "sách và chi tiết), rải đều. Khung qua nửa đêm được, ví dụ 22:00–05:00.",
        ),
        _s(
            "poll_window",
            "window",
            "Khung giờ poll",
            "Trong khung này các doanh nghiệp được poll tin mới theo bộ khung. 00:00–24:00 là cả "
            "ngày.",
        ),
        _s(
            "poll_interval_seconds",
            "float",
            "Chu kỳ poll",
            "Mỗi doanh nghiệp được poll lại sau chừng này giây. Độ trễ phát hiện tin mới ≈ chu kỳ "
            "poll + thời gian một lượt poll (mục tiêu ≤ 3 phút).",
            unit="giây",
        ),
        _s(
            "poll_jitter_seconds",
            "float",
            "Lệch ngẫu nhiên",
            "Cộng / trừ ngẫu nhiên tối đa chừng này giây vào lịch poll để các doanh nghiệp không "
            "dồn vào cùng một thời điểm.",
            unit="giây",
        ),
        _s(
            "poll_deadline_seconds",
            "float",
            "Deadline mềm của lượt poll",
            "Hết chừng này giây thì lượt poll dừng các nguồn chưa xong và lưu phần đã có. Phải "
            "nhỏ hơn timeout cứng.",
            unit="giây",
        ),
        _s(
            "poll_timeout_seconds",
            "int",
            "Timeout cứng của lượt poll",
            "Job poll chạy quá chừng này giây thì bị huỷ.",
            unit="giây",
        ),
        _s(
            "poll_max_failures",
            "int",
            "Lỗi tạm liên tiếp trước khi build lại",
            "Lỗi tạm (timeout, lỗi máy chủ) lặp lại chừng này lượt poll liên tiếp thì build lại "
            "bộ khung của doanh nghiệp.",
            unit="lượt",
        ),
        _s(
            "plan_min_pages",
            "int",
            "Số trang tối thiểu mỗi nguồn",
            "Khi chia trang của bộ khung, mỗi nguồn (trang careers, bảng tin ATS) được ít nhất "
            "chừng này trang nếu site có đủ, trước khi nguồn khác được thêm.",
            unit="trang",
        ),
        _s(
            "plan_max_pages",
            "int",
            "Số trang tối đa mỗi doanh nghiệp",
            "Mỗi lượt poll đọc tối đa chừng này trang danh sách, trang danh mục và trang API của "
            "một doanh nghiệp, chia cho các nguồn ở lượt build đêm. Site nhiều trang hơn thì lượt "
            "poll chỉ thấy tin mới ở các trang đầu và không đóng việc; lượt build đêm đọc đủ.",
            unit="trang",
        ),
        _s(
            "poll_max_detail_pages",
            "int",
            "Trang chi tiết tối đa mỗi lượt poll",
            "Mỗi nguồn chỉ mở chừng này trang tin mới trong một lượt poll để lượt poll luôn nhanh; "
            "tin mới còn lại được mở ở lượt sau.",
            unit="trang",
        ),
        _s(
            "rebuild_on_error",
            "bool",
            "Build lại ngay khi bộ khung hỏng",
            "Poll gặp trang chính lỗi hoặc cấu trúc trang đổi thì build lại ngay, kể cả ban ngày.",
        ),
        _s(
            "build_refresh_discovery",
            "bool",
            "Tìm lại nguồn khi build",
            "Lượt build ban đêm dò lại trang tuyển dụng trên website thay vì dùng kết quả tìm "
            "nguồn đã lưu.",
        ),
        _s(
            "scheduler_batch",
            "int",
            "Số job mỗi nhịp xếp lịch",
            "Mỗi nhịp scheduler đưa tối đa chừng này job vào hàng đợi.",
            unit="job",
        ),
        _s(
            "build_queue_max_backlog",
            "int",
            "Ngưỡng tồn hàng đợi build",
            "Hàng đợi build còn nhiều việc chờ hơn mức này thì scheduler tạm không xếp thêm.",
            unit="job",
        ),
        _s(
            "poll_queue_max_backlog",
            "int",
            "Ngưỡng tồn hàng đợi poll",
            "Hàng đợi poll còn nhiều việc chờ hơn mức này thì scheduler tạm không xếp thêm.",
            unit="job",
        ),
    ),
    _group(
        "runner",
        "Runner & hàng đợi",
        "Runner lấy job build, poll và index trang tuyển dụng từ hàng đợi Redis rồi chạy; mỗi "
        "loại job có hàng đợi riêng để lượt poll không phải chờ sau lượt build dài.",
        _s(
            "runner_build_concurrency",
            "int",
            "Job build song song",
            "Mỗi process runner chạy cùng lúc tối đa chừng này lượt build. 0 = tự tính theo CPU / "
            "RAM của máy (2,5 mỗi CPU, không quá 2 mỗi GiB RAM, trong 4–20).",
            unit="job",
            live=False,
        ),
        _s(
            "runner_poll_concurrency",
            "int",
            "Job poll song song",
            "Mỗi process runner chạy cùng lúc tối đa chừng này lượt poll. 0 = tự tính theo máy "
            "(12 mỗi CPU, trong 20–100).",
            unit="job",
            live=False,
        ),
        _s(
            "runner_board_concurrency",
            "int",
            "Job index trang tuyển dụng song song",
            "Mỗi process runner index cùng lúc tối đa chừng này trang tuyển dụng. 0 = tự tính theo "
            "máy (1 mỗi CPU, trong 2–8).",
            unit="job",
            live=False,
        ),
        _s(
            "company_deadline_seconds",
            "float",
            "Deadline mềm của lượt build",
            "Hết chừng này giây thì lượt build dừng các nguồn chưa xong và lưu kết quả một phần. "
            "Phải nhỏ hơn timeout cứng.",
            unit="giây",
        ),
        _s(
            "company_timeout_seconds",
            "int",
            "Timeout cứng của lượt build",
            "Job build chạy quá chừng này giây thì bị huỷ.",
            unit="giây",
        ),
        _s(
            "company_job_retries",
            "int",
            "Số lần chạy tối đa một job build",
            "Runner dừng giữa chừng hoặc job chạm timeout cứng thì được chạy lại, tối đa chừng này "
            "lần (tính cả lần đầu).",
            unit="lần",
        ),
        _s(
            "company_max_continuations",
            "int",
            "Số lượt chạy tiếp tối đa",
            "Lượt build chưa xong hết (chạm giới hạn, hết giờ, tin chờ LLM) tự xếp lượt chạy tiếp, "
            "tối đa chừng này lần liên tiếp.",
            unit="lượt",
        ),
        _s(
            "company_continuation_delay_seconds",
            "float",
            "Chờ trước lượt chạy tiếp",
            "Lượt chạy tiếp được hẹn sau chừng này giây.",
            unit="giây",
        ),
        _s(
            "queue_reclaim_seconds",
            "float",
            "Thu hồi job treo sau",
            "Job của runner không báo sống chừng này giây thì runner khác thu hồi và chạy lại.",
            unit="giây",
            live=False,
        ),
    ),
    _group(
        "workers",
        "Worker lấy trang",
        "Worker (chạy ở máy nào cũng được, kèm proxy riêng) nhận việc lấy trang từ backend qua "
        "gateway. Các giá trị slot, giả lập trình duyệt, chu kỳ báo sống được đẩy xuống mọi worker "
        "đang kết nối.",
        _s(
            "gateway_http_slots",
            "int",
            "Slot HTTP mỗi worker",
            "Mức trần số request HTTP mỗi worker chạy cùng lúc: worker dùng số nhỏ hơn giữa giá "
            "trị này và cấu hình riêng của nó (máy nhỏ không bị đẩy lên). 0 = giữ cấu hình của "
            "worker.",
            unit="request",
        ),
        _s(
            "gateway_render_slots",
            "int",
            "Slot render mỗi worker",
            "Mức trần số trang trình duyệt (render JavaScript) mỗi worker mở cùng lúc: worker dùng "
            "số nhỏ hơn giữa giá trị này và cấu hình riêng của nó. 0 = giữ cấu hình của worker.",
            unit="trang",
        ),
        _s(
            "gateway_impersonate",
            "str",
            "Giả lập trình duyệt",
            "Dấu vân tay TLS / HTTP mà worker giả lập khi gửi request, ví dụ chrome, safari, "
            "firefox.",
            check=_check_text,
            placeholder="chrome",
        ),
        _s(
            "gateway_heartbeat_seconds",
            "int",
            "Chu kỳ báo sống",
            "Worker gửi trạng thái (slot đang dùng, số việc xong / lỗi) về gateway mỗi chừng này "
            "giây.",
            unit="giây",
        ),
        _s(
            "gateway_lease_wait_ms",
            "int",
            "Thời gian chờ việc",
            "Mỗi lần xin việc, worker chờ tối đa chừng này mili giây khi hàng việc đang trống.",
            unit="ms",
        ),
        _s(
            "gateway_worker_ttl_seconds",
            "float",
            "Coi worker đã rời sau",
            "Worker không báo sống chừng này giây thì coi là đã rời và không giao việc nữa.",
            unit="giây",
        ),
        _s(
            "fetch_queue_grace_seconds",
            "float",
            "Thời gian chờ xếp hàng tới worker",
            "Backend chờ thêm chừng này giây (ngoài timeout của request) cho việc xếp hàng tới "
            "worker trước khi coi là hết giờ.",
            unit="giây",
            live=False,
        ),
        _s(
            "fetch_queue_max_wait_seconds",
            "float",
            "Chờ xếp hàng tối đa khi hàng đông",
            "Hết thời gian chờ xếp hàng mà worker vẫn đang lấy việc trong hàng (chỉ là đông, không "
            "phải hỏng) thì backend chờ tiếp, tổng tối đa timeout của request + chừng này giây. "
            "Phải lớn hơn thời gian chờ xếp hàng tới worker.",
            unit="giây",
            live=False,
        ),
        _s(
            "fetch_max_body_bytes",
            "int",
            "Dung lượng trang tối đa",
            "Trang lớn hơn mức này bị cắt / bỏ để không làm đầy bộ nhớ.",
            unit="byte",
            live=False,
        ),
        _s(
            "fetch_routes",
            "dict",
            "Định tuyến domain tới pool worker",
            "Request tới domain (kể cả subdomain) đi qua pool worker được chỉ định, ví dụ "
            "topcv.vn → residential. Pool không còn worker nào sống thì dùng pool default.",
            value_type="str",
            check=_check_routes,
            live=False,
        ),
        _s(
            "gateway_public_url",
            "str",
            "Địa chỉ cho worker mới",
            "Địa chỉ giao diện mà máy worker ở nơi khác nối vào (https = TLS). Trống = địa chỉ "
            "đang mở giao diện.",
            check=_check_origin,
            placeholder="https://crawler.example.com",
        ),
        _s(
            "gateway_worker_image",
            "str",
            "Image worker",
            "Nên cùng bản với backend.",
            check=_check_image,
            placeholder="ghcr.io/ntthanh2603/jobcrawler-worker:main",
        ),
    ),
    _group(
        "proxy",
        "Proxy",
        "Ai chọn proxy cho từng request, và cách danh sách proxy chung được xoay vòng, cho nghỉ "
        "khi lỗi.",
        _enum(
            "proxy_mode",
            "Chế độ proxy",
            "Quyết định proxy cho request lấy trang.",
            {
                "worker": (
                    "Worker tự xoay",
                    "Worker tải danh sách proxy chung bên dưới về và tự xoay vòng: mỗi domain giữ "
                    "một proxy một lúc rồi đổi sang proxy kế tiếp, proxy bị chặn / lỗi thì nghỉ. "
                    "Máy worker có proxy riêng (WORKER_PROXIES) dùng proxy của nó; danh sách trống "
                    "và không có proxy riêng thì đi thẳng.",
                ),
                "central": (
                    "Backend chọn",
                    "Backend chọn proxy cho từng request từ danh sách chung bên dưới, gắn theo "
                    "domain và cho nghỉ khi lỗi; worker dùng đúng proxy được chỉ định.",
                ),
                "off": (
                    "Không dùng proxy",
                    "Mọi request đi thẳng từ worker, kể cả khi worker có proxy riêng.",
                ),
            },
            live=False,
        ),
        _s(
            "proxies",
            "list",
            "Danh sách proxy chung",
            "Proxy dùng chung (HTTP, HTTPS, SOCKS; có hoặc không tài khoản / mật khẩu). Chế độ "
            "“Worker tự xoay”: gateway gửi danh sách xuống worker (đổi danh sách thì worker nhận "
            "trong khoảng 15 giây); “Backend chọn”: backend xoay vòng cho từng request. Mật khẩu "
            "không bao giờ hiện lại sau khi lưu.",
            check=_check_proxies,
            placeholder="http://user:pass@1.2.3.4:8080",
            live=False,
        ),
        _s(
            "proxy_sticky_seconds",
            "int",
            "Giữ proxy theo domain",
            "Mỗi domain dùng cùng một proxy trong chừng này giây rồi mới đổi.",
            unit="giây",
            live=False,
        ),
        _s(
            "proxy_cooldown_seconds",
            "int",
            "Cho proxy nghỉ khi lỗi",
            "Proxy bị chặn hoặc lỗi được cho nghỉ chừng này giây.",
            unit="giây",
            live=False,
        ),
        _s(
            "proxy_max_consecutive_failures",
            "int",
            "Lỗi liên tiếp trước khi loại",
            "Proxy lỗi liên tiếp chừng này lần thì bị loại khỏi vòng xoay.",
            unit="lần",
            live=False,
        ),
        _s(
            "proxy_dead_seconds",
            "int",
            "Thời gian loại proxy",
            "Proxy bị loại nằm ngoài vòng xoay chừng này giây rồi được thử lại.",
            unit="giây",
            live=False,
        ),
        _s(
            "proxy_check_url",
            "str",
            "Trang kiểm tra proxy",
            "Trang dùng để kiểm tra proxy còn sống (nên nhỏ và trả nhanh).",
            check=_check_url,
            placeholder="https://www.cloudflare.com/cdn-cgi/trace",
        ),
    ),
    _group(
        "network",
        "HTTP & tốc độ truy cập",
        "Timeout, thử lại, cache của request; giới hạn số request mỗi giây theo domain và tuân thủ "
        "robots.txt để không làm quá tải hay bị website chặn.",
        _s(
            "http_timeout_seconds",
            "float",
            "Timeout mỗi request",
            "Thời gian tối đa chờ một trang trả về (trang render bằng trình duyệt được gấp đôi).",
            unit="giây",
            live=False,
        ),
        _s(
            "http_max_retries",
            "int",
            "Số lần thử lại",
            "Request lỗi tạm (timeout, lỗi máy chủ, 429) được thử lại tối đa chừng này lần.",
            unit="lần",
            live=False,
        ),
        _s(
            "http_backoff_base_seconds",
            "float",
            "Chờ trước lần thử lại đầu",
            "Thời gian chờ giữa các lần thử lại tăng gấp đôi sau mỗi lần, bắt đầu từ mức này.",
            unit="giây",
            live=False,
        ),
        _s(
            "http_backoff_cap_seconds",
            "float",
            "Chờ tối đa giữa hai lần thử",
            "Thời gian chờ giữa hai lần thử lại không vượt quá mức này.",
            unit="giây",
            live=False,
        ),
        _s(
            "http_max_retry_after_seconds",
            "float",
            "Retry-After tối đa chấp nhận",
            "Website yêu cầu chờ lâu hơn mức này (header Retry-After) thì dừng nguồn đó, coi như "
            "bị chặn, thay vì chờ.",
            unit="giây",
            live=False,
        ),
        _s(
            "http_cache_ttl_seconds",
            "int",
            "Cache trang",
            "Trang đã tải được dùng lại trong chừng này giây (kèm ETag / Last-Modified). 0 = không "
            "cache.",
            unit="giây",
            live=False,
        ),
        _s(
            "http_accept_language",
            "str",
            "Ngôn ngữ ưu tiên (Accept-Language)",
            "Header ngôn ngữ gửi kèm mọi request.",
            check=_check_text,
            live=False,
        ),
        _s(
            "rate_limit_per_second",
            "float",
            "Tốc độ mặc định mỗi domain",
            "Số request mỗi giây tối đa tới một domain, dùng chung cho mọi process và worker.",
            unit="request/giây",
            live=False,
        ),
        _s(
            "rate_limit_burst",
            "int",
            "Số request dồn tối đa",
            "Cho phép dồn chừng này request liền nhau trước khi phải chờ theo tốc độ.",
            unit="request",
            live=False,
        ),
        _s(
            "rate_limits",
            "dict",
            "Tốc độ riêng theo domain",
            "Ghi đè tốc độ cho từng domain, ví dụ www.topcv.vn → 0,5 request/giây.",
            unit="request/giây",
            value_type="float",
            check=_check_rate_limits,
            live=False,
        ),
        _s(
            "robots_cache_seconds",
            "int",
            "Cache robots.txt",
            "robots.txt của mỗi website được dùng lại trong chừng này giây.",
            unit="giây",
            live=False,
        ),
        _s(
            "robots_user_agents",
            "list",
            "Tên bot trong robots.txt",
            "Tên bot (product token) dùng để chọn nhóm luật trong robots.txt, ngoài nhóm *. Mỗi "
            "dòng một tên.",
            check=_clean_lines,
            placeholder="*",
            live=False,
        ),
    ),
    _group(
        "sources",
        "Nguồn & giới hạn",
        "Tìm trang tuyển dụng trên website doanh nghiệp và giới hạn số trang đọc mỗi nguồn trong "
        "một lượt.",
        _s(
            "discovery_cache_seconds",
            "int",
            "Dùng lại kết quả tìm nguồn",
            "Nguồn tìm thấy trên website được dùng lại trong chừng này giây.",
            unit="giây",
        ),
        _s(
            "discovery_negative_cache_seconds",
            "int",
            "Thử tìm lại khi không thấy nguồn",
            "Website không có trang tuyển dụng nào thì thử tìm lại sau chừng này giây.",
            unit="giây",
        ),
        _s(
            "discovery_timeout_seconds",
            "float",
            "Thời gian tìm nguồn tối đa",
            "Thời gian tối đa cho bước tìm nguồn trong một lượt build; phần còn lại dành cho thu "
            "thập.",
            unit="giây",
        ),
        _s(
            "source_max_jobs",
            "int",
            "Tin tối đa mỗi nguồn",
            "Mỗi nguồn đọc tối đa chừng này tin trong một lượt.",
            unit="tin",
        ),
        _s(
            "source_max_detail_pages",
            "int",
            "Trang chi tiết tối đa mỗi nguồn",
            "Số trang chi tiết tin tải tối đa mỗi nguồn trong một lượt.",
            unit="trang",
        ),
        _s(
            "source_max_listing_pages",
            "int",
            "Trang danh sách tối đa mỗi nguồn",
            "Số trang danh sách (phân trang) đọc tối đa mỗi nguồn.",
            unit="trang",
        ),
        _s(
            "source_max_rendered_pages",
            "int",
            "Trang render tối đa mỗi nguồn",
            "Số trang phải mở bằng trình duyệt (chạy JavaScript) tối đa mỗi nguồn.",
            unit="trang",
        ),
        _s(
            "source_max_sub_listings",
            "int",
            "Trang danh mục tối đa mỗi nguồn",
            "Trang danh mục / theo tỉnh của trang careers dẫn tới tin, tối đa mỗi nguồn.",
            unit="trang",
        ),
        _s(
            "source_max_api_pages",
            "int",
            "Trang API tối đa mỗi nguồn",
            "Số trang của API danh sách tin (trang careers dạng SPA) đọc tiếp tối đa, kể cả trang "
            "đầu.",
            unit="trang",
        ),
        _s(
            "job_reuse_days",
            "float",
            "Dùng lại tin đã tải",
            "Tin đã tải chi tiết trong chừng này ngày thì không tải lại. 0 = luôn tải lại.",
            unit="ngày",
        ),
        _s(
            "ats_rate_per_second",
            "float",
            "Tốc độ gọi API ATS",
            "Tốc độ tới API công khai của ATS (Greenhouse, Lever...), dùng chung cho mọi doanh "
            "nghiệp; tốc độ riêng theo domain vẫn được ưu tiên.",
            unit="request/giây",
        ),
    ),
    _group(
        "boards",
        "Trang tuyển dụng",
        "Index tin trên các trang tuyển dụng (TopCV, CareerLink...) và khớp profile nhà tuyển dụng "
        "với doanh nghiệp trong danh sách.",
        _s(
            "boards",
            "list",
            "Trang được index",
            "Chọn các trang tuyển dụng được index. Không chọn trang nào = index mọi trang đang "
            "bật.",
            check=_check_boards,
        ),
        _s(
            "board_index_cron",
            "cron",
            "Lịch index đủ",
            f"Lịch đọc toàn bộ danh sách tin của mỗi trang (thường ban đêm). {_CRON_HELP}",
            live=False,
        ),
        _s(
            "board_index_budget_seconds",
            "float",
            "Thời gian tối đa mỗi lượt index đủ",
            "Hết thời gian này thì dừng, phần còn lại để lượt sau.",
            unit="giây",
        ),
        _s(
            "board_max_jobs_per_run",
            "int",
            "Tin tối đa mỗi lượt index đủ",
            "Mỗi lượt index đủ tải tối đa chừng này trang chi tiết tin.",
            unit="tin",
        ),
        _s(
            "board_tail_cron",
            "cron",
            "Lịch index đuôi",
            "Lịch đọc phần mới nhất của danh sách tin (ban ngày) để bắt tin mới nhanh. "
            + _CRON_HELP,
            live=False,
        ),
        _s(
            "board_tail_budget_seconds",
            "float",
            "Thời gian tối đa mỗi lượt index đuôi",
            "Lượt index đuôi dừng sau chừng này giây.",
            unit="giây",
        ),
        _s(
            "board_tail_max_jobs",
            "int",
            "Tin tối đa mỗi lượt index đuôi",
            "Mỗi lượt index đuôi tải tối đa chừng này tin chưa có.",
            unit="tin",
        ),
        _s(
            "board_refresh_days",
            "float",
            "Tải lại tin không đổi sau",
            "Tin có ngày cập nhật (lastmod) không đổi vẫn được tải lại sau chừng này ngày.",
            unit="ngày",
        ),
        _s(
            "board_concurrency",
            "int",
            "Trang chi tiết tải song song",
            "Số trang chi tiết tải cùng lúc trong một lượt index.",
            unit="trang",
        ),
        _s(
            "board_stale_hours",
            "float",
            "Đọc trực tiếp profile khi index cũ hơn",
            "Khi build doanh nghiệp: profile trên trang tuyển dụng có lượt index cũ hơn chừng này "
            "giờ thì đọc trực tiếp trang profile.",
            unit="giờ",
        ),
        _s(
            "profile_match_trigram",
            "float",
            "Ngưỡng tên gần giống khi khớp profile",
            "Độ giống tên (0–1) tối thiểu để xét một profile có thể là doanh nghiệp.",
        ),
        _s(
            "profile_match_min_confidence",
            "float",
            "Độ tin cậy tối thiểu khi khớp profile",
            "LLM / luật phải chắc ít nhất mức này (0–1) mới nhận profile là của doanh nghiệp.",
        ),
        _s(
            "board_name_profiles_without_kyc",
            "bool",
            "Khớp theo tên khi chưa có website",
            "Doanh nghiệp KYC không tìm được website: vẫn dùng profile có tên trùng hẳn tên doanh "
            "nghiệp.",
        ),
        _s(
            "board_serp_lookup",
            "bool",
            "Tìm profile bằng công cụ tìm kiếm",
            "Doanh nghiệp chưa có profile trên một trang: tìm bằng tìm kiếm site: (cần khoá API "
            "tìm kiếm).",
        ),
        _s(
            "board_poll_companies",
            "bool",
            "Poll ngay doanh nghiệp có tin mới",
            "Sau mỗi lượt index, doanh nghiệp có profile vừa xuất hiện tin mới được poll ngay.",
        ),
        _s(
            "board_serp_lookup_seconds",
            "float",
            "Thời gian tối đa tìm profile",
            "Thời gian tối đa cho bước tìm profile bằng tìm kiếm trong một lượt build.",
            unit="giây",
        ),
    ),
    _group(
        "ai",
        "LLM & tìm kiếm",
        "Mô hình ngôn ngữ (endpoint chuẩn OpenAI, ví dụ vLLM) dùng cho KYC, chuẩn hoá và chấm tin; "
        "dịch vụ tìm kiếm dùng cho KYC và tìm profile.",
        _s(
            "llm_base_url",
            "str",
            "Endpoint LLM",
            "URL API chuẩn OpenAI, ví dụ http://vllm:8000/v1. Để trống thì mọi bước dùng LLM bị "
            "tắt.",
            check=_check_url,
            placeholder="http://vllm:8000/v1",
            live=False,
        ),
        _s(
            "llm_model",
            "str",
            "Tên mô hình",
            "Tên mô hình trên endpoint. Để trống thì mọi bước dùng LLM bị tắt.",
            live=False,
        ),
        _s(
            "llm_api_key",
            "secret",
            "Khoá API LLM",
            "Khoá gửi kèm request tới endpoint LLM (vLLM tự host thường không cần).",
            live=False,
        ),
        _s(
            "llm_disable_thinking",
            "bool",
            "Tắt phần suy luận (thinking)",
            "Yêu cầu mô hình trả lời thẳng, không sinh phần suy luận: nhanh và rẻ hơn.",
            live=False,
        ),
        _s(
            "llm_max_concurrency",
            "int",
            "Request LLM song song mỗi process",
            "Mỗi process gửi cùng lúc tối đa chừng này request tới LLM.",
            unit="request",
            live=False,
        ),
        _s(
            "llm_max_input_chars",
            "int",
            "Độ dài đầu vào tối đa",
            "Nội dung dài hơn được cắt bớt trước khi gửi LLM.",
            unit="ký tự",
            live=False,
        ),
        _s(
            "llm_timeout_seconds",
            "float",
            "Timeout LLM",
            "Thời gian tối đa chờ một câu trả lời của LLM.",
            unit="giây",
            live=False,
        ),
        _s(
            "llm_requests_per_minute",
            "float",
            "Giới hạn request mỗi phút",
            "Giới hạn chung cho mọi process (để trống = không giới hạn). Nên đặt thấp hơn giới hạn "
            "của máy chủ LLM một chút.",
            unit="request/phút",
            live=False,
        ),
        _enum(
            "serp_provider",
            "Dịch vụ tìm kiếm",
            "Nhà cung cấp API tìm kiếm.",
            {"serper": ("Serper (Google)", None), "brave": ("Brave Search", None)},
        ),
        _s(
            "serp_api_key",
            "secret",
            "Khoá API tìm kiếm",
            "Thiếu khoá thì bỏ qua các bước dùng tìm kiếm (KYC, tìm profile trên trang tuyển "
            "dụng).",
        ),
        _s(
            "serp_results",
            "int",
            "Số kết quả mỗi lần tìm",
            "Số kết quả lấy về cho mỗi truy vấn tìm kiếm.",
            unit="kết quả",
        ),
        _s(
            "serp_rate_per_second",
            "float",
            "Tốc độ gọi API tìm kiếm",
            "Số truy vấn tìm kiếm mỗi giây tối đa.",
            unit="request/giây",
        ),
        _s(
            "serp_cache_seconds",
            "int",
            "Dùng lại kết quả tìm kiếm",
            "Kết quả tìm kiếm được dùng lại trong chừng này giây để chạy lại không tốn thêm "
            "credit.",
            unit="giây",
        ),
        _s(
            "serp_timeout_seconds",
            "float",
            "Timeout tìm kiếm",
            "Thời gian tối đa chờ API tìm kiếm trả về.",
            unit="giây",
        ),
    ),
    _group(
        "quality",
        "Xác minh & KYC",
        "Ngưỡng nhận / loại tin và xác minh pháp nhân doanh nghiệp (tên pháp lý, MST, website).",
        _s(
            "verify_min_confidence",
            "float",
            "Độ tin cậy tối thiểu để nhận tin nghi ngờ",
            "Tin còn nghi ngờ phải được LLM chấm ít nhất mức này (0–1) mới được nhận.",
        ),
        _s(
            "verify_min_jd_chars",
            "int",
            "Độ dài mô tả tối thiểu",
            "Tin có mô tả + yêu cầu ngắn hơn mức này bị loại (mô tả trống).",
            unit="ký tự",
        ),
        _s(
            "verify_board_max_age_days",
            "float",
            "Tuổi tối đa của tin không có hạn nộp",
            "Tin trên trang tuyển dụng không ghi hạn nộp, đăng quá chừng này ngày thì coi là hết "
            "hạn.",
            unit="ngày",
        ),
        _s(
            "verify_llm_max_jobs",
            "int",
            "Tin tối đa LLM chấm mỗi doanh nghiệp",
            "Số tin nghi ngờ tối đa mỗi lượt được hỏi LLM; phần còn lại chờ lượt sau.",
            unit="tin",
        ),
        _s(
            "normalize_llm_max_jobs",
            "int",
            "Tin tối đa LLM chuẩn hoá mỗi doanh nghiệp",
            "Số tin tối đa mỗi lượt được hỏi LLM để phân loại trường mơ hồ (cấp bậc, hình "
            "thức...).",
            unit="tin",
        ),
        _s(
            "kyc_cache_days",
            "float",
            "Dùng lại kết quả KYC",
            "Kết quả xác minh doanh nghiệp được dùng lại trong chừng này ngày.",
            unit="ngày",
        ),
        _s(
            "kyc_trigram_threshold",
            "float",
            "Ngưỡng tên gần giống khi tra KYC đã có",
            "Độ giống tên (0–1) tối thiểu để dùng lại KYC của doanh nghiệp đã xác minh có tên gần "
            "giống.",
        ),
        _s(
            "kyc_min_confidence",
            "float",
            "Độ tin cậy tối thiểu để xác minh",
            "LLM phải chắc ít nhất mức này (0–1), cùng bằng chứng khớp mạnh, mới đánh dấu đã xác "
            "minh.",
        ),
        _s(
            "kyc_timeout_seconds",
            "float",
            "Thời gian KYC tối đa",
            "Thời gian tối đa cho KYC trong một lượt (tìm kiếm, đọc website, LLM).",
            unit="giây",
        ),
        _s(
            "kyc_max_sites",
            "int",
            "Số website ứng viên đọc",
            "Số website ứng viên mở để đọc chân trang (tên pháp lý, MST, địa chỉ).",
            unit="website",
        ),
    ),
    _group(
        "maintenance",
        "Bảo trì & lưu trữ",
        "Dọn dữ liệu cũ, tính sẵn số liệu trang tổng quan và nén các cột lớn bằng từ điển zstd.",
        _s(
            "maintenance_cron",
            "cron",
            "Lịch bảo trì",
            f"Dọn dữ liệu cũ, đánh dấu lượt chạy treo, huấn luyện từ điển nén. {_CRON_HELP}",
            live=False,
        ),
        _s(
            "overview_cron",
            "cron",
            "Lịch tính số liệu tổng quan",
            f"Tính sẵn số liệu phân bố cho trang Tổng quan. {_CRON_HELP}",
            live=False,
        ),
        _s(
            "source_fetch_retention_days",
            "float",
            "Giữ lịch sử gọi nguồn",
            "Lịch sử từng lần gọi nguồn cũ hơn chừng này ngày bị xoá.",
            unit="ngày",
        ),
        _s(
            "log_retention_days",
            "float",
            "Giữ log",
            "Log trong cơ sở dữ liệu cũ hơn chừng này ngày bị xoá.",
            unit="ngày",
        ),
        _s(
            "failed_page_retention_days",
            "float",
            "Giữ trang lỗi",
            "HTML của trang không bóc được tin cũ hơn chừng này ngày bị xoá.",
            unit="ngày",
        ),
        _s(
            "failed_pages_per_run",
            "int",
            "Trang lỗi lưu tối đa mỗi lượt",
            "Số trang không bóc được tin tối đa được lưu lại để xem, mỗi lượt chạy.",
            unit="trang",
        ),
        _s(
            "compression_level",
            "int",
            "Mức nén zstd",
            "Mức cao nén nhỏ hơn nhưng tốn CPU hơn khi ghi.",
            live=False,
        ),
        _s(
            "compression_dict_size",
            "int",
            "Kích thước từ điển nén",
            "Kích thước từ điển zstd huấn luyện từ dữ liệu thật.",
            unit="byte",
        ),
        _s(
            "compression_dict_min_samples",
            "int",
            "Mẫu tối thiểu để huấn luyện từ điển",
            "Có ít nhất chừng này giá trị mới mới huấn luyện từ điển.",
            unit="mẫu",
        ),
        _s(
            "compression_dict_max_samples",
            "int",
            "Mẫu tối đa khi huấn luyện",
            "Lấy mẫu tối đa chừng này giá trị để huấn luyện từ điển.",
            unit="mẫu",
        ),
        _s(
            "compression_dict_delay_seconds",
            "float",
            "Chờ trước khi dùng từ điển mới",
            "Từ điển mới chỉ dùng để nén sau chừng này giây, khi mọi process đã kịp nạp. Phải lớn "
            "hơn chu kỳ nạp từ điển.",
            unit="giây",
        ),
        _s(
            "compression_dict_refresh_seconds",
            "float",
            "Chu kỳ nạp từ điển",
            "Mỗi process kiểm tra từ điển nén mới sau chừng này giây.",
            unit="giây",
            live=False,
        ),
        _s(
            "compression_dict_retrain_days",
            "float",
            "Huấn luyện lại từ điển sau",
            "Từ điển đang dùng cũ hơn chừng này ngày thì huấn luyện lại.",
            unit="ngày",
        ),
        _s(
            "compression_recompress_rows",
            "int",
            "Dòng nén lại tối đa mỗi lượt bảo trì",
            "Số dòng tối đa được nén lại bằng từ điển mới mỗi lượt bảo trì.",
            unit="dòng",
        ),
    ),
)


def _sec(title: str, *keys: str, description: str = "") -> Section:
    return Section(title, keys, description)


# Mục con của từng nhóm, theo thứ tự hiển thị. Mỗi cài đặt nằm đúng một mục của nhóm mình
# (`tests/test_app_settings.py` kiểm tra).
_SECTIONS: dict[str, tuple[Section, ...]] = {
    "schedule": (
        _sec("Khung giờ", "crawl_timezone", "build_window", "poll_window"),
        _sec(
            "Nhịp poll",
            "poll_interval_seconds",
            "poll_jitter_seconds",
            "poll_deadline_seconds",
            "poll_timeout_seconds",
            description="Độ trễ phát hiện tin mới ≈ chu kỳ poll + thời gian một lượt poll.",
        ),
        _sec(
            "Bộ khung và giới hạn mỗi lượt poll",
            "plan_min_pages",
            "plan_max_pages",
            "poll_max_detail_pages",
            description="Lượt build đêm ghi bộ khung (trang / API cần đọc) của mỗi doanh nghiệp; "
            "ban ngày lượt poll chỉ đọc đúng bộ khung đó. Ít trang hơn thì poll nhanh và rẻ hơn; "
            "phần còn lại được đọc ở lượt sau hoặc lượt build đêm.",
        ),
        _sec(
            "Build lại bộ khung", "poll_max_failures", "rebuild_on_error", "build_refresh_discovery"
        ),
        _sec("Scheduler", "scheduler_batch", "build_queue_max_backlog", "poll_queue_max_backlog"),
    ),
    "runner": (
        _sec(
            "Job song song mỗi runner",
            "runner_build_concurrency",
            "runner_poll_concurrency",
            "runner_board_concurrency",
        ),
        _sec(
            "Lượt build",
            "company_deadline_seconds",
            "company_timeout_seconds",
            "company_job_retries",
            "company_max_continuations",
            "company_continuation_delay_seconds",
        ),
        _sec("Hàng đợi", "queue_reclaim_seconds"),
    ),
    "workers": (
        _sec(
            "Cấu hình gửi xuống worker",
            "gateway_http_slots",
            "gateway_render_slots",
            "gateway_impersonate",
            "gateway_heartbeat_seconds",
            "gateway_lease_wait_ms",
            description="Gateway gửi kèm khi worker đăng ký và mỗi lần báo sống; 0 / trống là giữ "
            "cấu hình của worker.",
        ),
        _sec(
            "Gateway",
            "gateway_worker_ttl_seconds",
            "fetch_queue_grace_seconds",
            "fetch_queue_max_wait_seconds",
            "fetch_max_body_bytes",
        ),
        _sec("Định tuyến", "fetch_routes"),
        _sec(
            "Thêm worker",
            "gateway_public_url",
            "gateway_worker_image",
            description="Dùng trong lệnh Docker của nút “Thêm worker” ở trang Workers.",
        ),
    ),
    "proxy": (
        _sec("Chế độ", "proxy_mode"),
        _sec(
            "Danh sách proxy chung",
            "proxies",
            "proxy_sticky_seconds",
            "proxy_cooldown_seconds",
            "proxy_max_consecutive_failures",
            "proxy_dead_seconds",
            "proxy_check_url",
            description="“Worker tự xoay”: worker tải danh sách này về. “Backend chọn”: backend "
            "chọn proxy cho từng request, theo các thời hạn bên dưới. Proxy riêng của một máy "
            "worker đặt ở máy đó.",
        ),
    ),
    "network": (
        _sec(
            "Request",
            "http_timeout_seconds",
            "http_max_retries",
            "http_backoff_base_seconds",
            "http_backoff_cap_seconds",
            "http_max_retry_after_seconds",
            "http_accept_language",
            "http_cache_ttl_seconds",
        ),
        _sec("Tốc độ theo domain", "rate_limit_per_second", "rate_limit_burst", "rate_limits"),
        _sec("robots.txt", "robots_cache_seconds", "robots_user_agents"),
    ),
    "sources": (
        _sec(
            "Tìm nguồn",
            "discovery_cache_seconds",
            "discovery_negative_cache_seconds",
            "discovery_timeout_seconds",
        ),
        _sec(
            "Giới hạn mỗi nguồn",
            "source_max_jobs",
            "source_max_detail_pages",
            "source_max_listing_pages",
            "source_max_rendered_pages",
            "source_max_sub_listings",
            "source_max_api_pages",
        ),
        _sec("Dùng lại & ATS", "job_reuse_days", "ats_rate_per_second"),
    ),
    "boards": (
        _sec("Trang được index", "boards"),
        _sec(
            "Index đủ",
            "board_index_cron",
            "board_index_budget_seconds",
            "board_max_jobs_per_run",
            description="Đọc toàn bộ danh sách tin của mỗi trang, thường ban đêm.",
        ),
        _sec(
            "Index đuôi",
            "board_tail_cron",
            "board_tail_budget_seconds",
            "board_tail_max_jobs",
            "board_poll_companies",
            description="Ban ngày chỉ đọc phần tin mới nhất rồi poll ngay doanh nghiệp có tin mới.",
        ),
        _sec("Tải tin", "board_refresh_days", "board_concurrency", "board_stale_hours"),
        _sec(
            "Khớp profile với doanh nghiệp",
            "profile_match_trigram",
            "profile_match_min_confidence",
            "board_name_profiles_without_kyc",
            "board_serp_lookup",
            "board_serp_lookup_seconds",
        ),
    ),
    "ai": (
        _sec("Mô hình LLM", "llm_base_url", "llm_model", "llm_api_key", "llm_disable_thinking"),
        _sec(
            "Giới hạn LLM",
            "llm_max_concurrency",
            "llm_max_input_chars",
            "llm_timeout_seconds",
            "llm_requests_per_minute",
        ),
        _sec(
            "Tìm kiếm (SERP)",
            "serp_provider",
            "serp_api_key",
            "serp_results",
            "serp_rate_per_second",
            "serp_cache_seconds",
            "serp_timeout_seconds",
        ),
    ),
    "quality": (
        _sec(
            "Xác minh tin",
            "verify_min_confidence",
            "verify_min_jd_chars",
            "verify_board_max_age_days",
            "verify_llm_max_jobs",
            "normalize_llm_max_jobs",
        ),
        _sec(
            "KYC doanh nghiệp",
            "kyc_cache_days",
            "kyc_trigram_threshold",
            "kyc_min_confidence",
            "kyc_timeout_seconds",
            "kyc_max_sites",
        ),
    ),
    "maintenance": (
        _sec("Lịch", "maintenance_cron", "overview_cron"),
        _sec(
            "Lưu trữ",
            "source_fetch_retention_days",
            "log_retention_days",
            "failed_page_retention_days",
            "failed_pages_per_run",
        ),
        _sec(
            "Nén dữ liệu",
            "compression_level",
            "compression_dict_size",
            "compression_dict_min_samples",
            "compression_dict_max_samples",
            "compression_dict_delay_seconds",
            "compression_dict_refresh_seconds",
            "compression_dict_retrain_days",
            "compression_recompress_rows",
        ),
    ),
}

GROUPS = tuple(replace(group, sections=_SECTIONS.get(group.key, ())) for group in GROUPS)

SPECS: list[SettingSpec] = [spec for group in GROUPS for spec in group.items]
SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}
# Cài đặt mà 0 = tự tính theo máy: giá trị tự động trên máy đang chạy API (cùng container với
# runner khi chạy `jobcrawler serve`), để trang Cài đặt hiện cho người sửa biết.
AUTO_VALUES: dict[str, Callable[[], int]] = {
    "runner_build_concurrency": lambda: sysres.auto_concurrency("build"),
    "runner_poll_concurrency": lambda: sysres.auto_concurrency("poll"),
    "runner_board_concurrency": lambda: sysres.auto_concurrency("boards"),
}
# Lưu trong bảng ở dạng mã hoá (`secretbox`): bí mật và danh sách proxy (có mật khẩu).
SENSITIVE: frozenset[str] = frozenset(
    {spec.key for spec in SPECS if spec.kind == "secret"} | {"proxies"}
)


# --- kiểm tra giá trị ----------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _adapter(key: str) -> TypeAdapter[Any]:
    """Kiểm tra một giá trị theo đúng kiểu + ràng buộc của field (không đọc biến môi trường)."""
    info = Settings.model_fields[key]
    annotation: Any = info.annotation
    if info.metadata:
        annotation = Annotated[(annotation, *info.metadata)]  # pyright: ignore[reportInvalidTypeForm]
    return TypeAdapter(annotation)


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", ".") if isinstance(value, int) else str(value)


_TYPE_MESSAGES = {
    "int_parsing": "Phải là số nguyên",
    "int_type": "Phải là số nguyên",
    "int_from_float": "Phải là số nguyên",
    "float_parsing": "Phải là số",
    "float_type": "Phải là số",
    "finite_number": "Phải là số hữu hạn",
    "bool_parsing": "Phải là bật hoặc tắt",
    "bool_type": "Phải là bật hoặc tắt",
    "string_type": "Phải là chuỗi",
    "list_type": "Phải là danh sách",
    "dict_type": "Phải là bảng tên miền → giá trị",
}
_BOUND_MESSAGES = {
    "greater_than_equal": ("ge", "Phải lớn hơn hoặc bằng"),
    "greater_than": ("gt", "Phải lớn hơn"),
    "less_than_equal": ("le", "Phải nhỏ hơn hoặc bằng"),
    "less_than": ("lt", "Phải nhỏ hơn"),
}


def _error_message(exc: Exception) -> str:
    """Lỗi kiểm tra → câu tiếng Việt ngắn (kèm vị trí: dòng của danh sách, khoá của bảng)."""
    if not isinstance(exc, ValidationError):
        return str(exc)
    err = exc.errors()[0]
    kind, ctx = err["type"], err.get("ctx") or {}
    if kind in _BOUND_MESSAGES:
        name, text = _BOUND_MESSAGES[kind]
        message = f"{text} {_num(ctx.get(name))}"
    elif kind == "literal_error":
        message = f"Phải là một trong: {ctx.get('expected', '')}"
    elif kind in _TYPE_MESSAGES:
        message = _TYPE_MESSAGES[kind]
    else:
        message = str(err["msg"]).removeprefix("Value error, ")
    loc = err["loc"]
    if loc and kind != "value_error":
        where = f"Dòng {loc[0] + 1}" if isinstance(loc[0], int) else str(loc[0])
        message = f"{where}: {message[0].lower()}{message[1:]}"
    return message


def validate_value(key: str, raw: Any) -> Any:
    """Kiểm tra + chuẩn hoá giá trị của một cài đặt; raise ValueError (thông báo tiếng Việt)."""
    spec = SPEC_BY_KEY[key]
    try:
        value = _adapter(key).validate_python(raw)
        if spec.check is not None and value is not None:
            value = spec.check(value)
            value = _adapter(key).validate_python(value)
    except ValueError as exc:  # ValidationError là ValueError
        raise ValueError(_error_message(exc)) from exc
    return value


def _jsonable(key: str, value: Any) -> Any:
    """Giá trị đã kiểm → JSON lưu trong `app_settings` (bí mật lưu nguyên văn)."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return _adapter(key).dump_python(value, mode="json")


_FIELD_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def _cross_errors(s: Settings) -> list[tuple[tuple[str, ...], str]]:
    """Lỗi giữa nhiều cài đặt: [(các key liên quan, thông báo)]. Giống `Settings._check_deadlines`,
    kèm lượt kiểm cuối bằng chính validator của Settings (bắt cả quy tắc thêm sau này)."""
    out: list[tuple[tuple[str, ...], str]] = []
    if s.poll_deadline_seconds >= s.poll_timeout_seconds:
        out.append(
            (
                ("poll_deadline_seconds", "poll_timeout_seconds"),
                f"Deadline mềm của lượt poll ({_num(s.poll_deadline_seconds)} giây) phải nhỏ hơn "
                f"timeout cứng ({_num(s.poll_timeout_seconds)} giây)",
            )
        )
    if s.company_deadline_seconds >= s.company_timeout_seconds:
        out.append(
            (
                ("company_deadline_seconds", "company_timeout_seconds"),
                f"Deadline mềm của lượt build ({_num(s.company_deadline_seconds)} giây) phải nhỏ "
                f"hơn timeout cứng ({_num(s.company_timeout_seconds)} giây)",
            )
        )
    if s.gateway_worker_ttl_seconds < 3 * s.gateway_heartbeat_seconds:
        out.append(
            (
                ("gateway_worker_ttl_seconds", "gateway_heartbeat_seconds"),
                f"Hạn báo sống của worker ({_num(s.gateway_worker_ttl_seconds)} giây) phải ít nhất "
                f"gấp 3 nhịp báo sống ({_num(s.gateway_heartbeat_seconds)} giây): worker lỡ một "
                "nhịp không bị coi là đã chết",
            )
        )
    if s.compression_dict_delay_seconds and (
        s.compression_dict_delay_seconds <= s.compression_dict_refresh_seconds
    ):
        out.append(
            (
                ("compression_dict_delay_seconds", "compression_dict_refresh_seconds"),
                "Thời gian chờ trước khi dùng từ điển mới phải lớn hơn chu kỳ nạp từ điển",
            )
        )
    if out:
        return out
    # Truyền đủ mọi field nên giá trị khởi tạo luôn đè biến môi trường / .env.
    try:
        Settings.model_validate({name: getattr(s, name) for name in Settings.model_fields})
    except ValidationError as exc:
        for err in exc.errors():
            message = str(err["msg"]).removeprefix("Value error, ")
            if err["loc"]:
                keys = (str(err["loc"][0]),)
            else:
                names = {t.lower() for t in _FIELD_TOKEN.findall(message)}
                keys = tuple(k for k in Settings.model_fields if k in names) or ("_",)
            out.append((keys, message))
    return out


def _valid_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    good: dict[str, Any] = {}
    for key, raw in overrides.items():
        if key not in SPEC_BY_KEY:
            continue
        try:
            good[key] = validate_value(key, raw)
        except ValueError as exc:
            log.warning("settings.invalid_override", key=key, error=str(exc))
    return good


def apply_overrides(base: Settings, overrides: Mapping[str, Any]) -> Settings:
    """Settings hiệu lực = `base` (env / .env) đè bởi cài đặt từ giao diện. Giá trị lưu không còn
    hợp lệ bị bỏ qua (kèm cảnh báo) chứ không làm process dừng."""
    good = _valid_overrides(overrides)
    if not good:
        return base
    effective = base.model_copy(update=good)
    errors = _cross_errors(effective)
    if errors:
        bad = {k for keys, _ in errors for k in keys if k in good}
        log.warning(
            "settings.conflicting_overrides",
            keys=sorted(bad) or sorted(good),
            errors=[message for _, message in errors],
        )
        good = {k: v for k, v in good.items() if k not in bad} if bad else {}
        effective = base.model_copy(update=good)
        if good and _cross_errors(effective):
            return base
    return effective


def restart_required(before: Settings, after: Settings, keys: Iterable[str]) -> list[str]:
    """Các key `live=False` trong `keys` có giá trị đổi giữa `before` và `after`."""
    out: list[str] = []
    for key in keys:
        spec = SPEC_BY_KEY.get(key)
        if spec is None or spec.live:
            continue
        if _jsonable(key, getattr(before, key)) != _jsonable(key, getattr(after, key)):
            out.append(key)
    return out


# --- DB + Redis ----------------------------------------------------------------------------------


def version_key(prefix: str) -> str:
    return f"{prefix}:settings:version"


def channel(prefix: str) -> str:
    return f"{prefix}:settings"


async def settings_version(redis: Redis, prefix: str) -> int:
    """Version cài đặt hiện tại (tăng mỗi lần lưu); 0 khi chưa lưu lần nào."""
    raw = await redis.get(version_key(prefix))
    return int(raw or 0)


def restart_key(prefix: str) -> str:
    """Bộ đếm: tăng mỗi lần lưu đổi một cài đặt `live=False`."""
    return f"{prefix}:settings:restart"


def restart_applied_key(prefix: str) -> str:
    """Giá trị `restart_key` mà `jobcrawler serve` đã áp dụng (khởi động lại process con xong)."""
    return f"{prefix}:settings:restart_applied"


def supervisor_key(prefix: str) -> str:
    """Có (TTL ngắn, `jobcrawler serve` ghi lại liên tục) = cài đặt `live=False` được tự áp dụng."""
    return f"{prefix}:serve:alive"


@dataclass(frozen=True, slots=True)
class RestartState:
    auto: bool  # có `jobcrawler serve` đang chạy: tự khởi động lại để áp dụng
    pending: bool  # đã lưu cài đặt cần khởi động lại mà chưa áp dụng xong


async def restart_state(redis: Redis, prefix: str) -> RestartState:
    auto = bool(await redis.exists(supervisor_key(prefix)))
    wanted, applied = await redis.mget(restart_key(prefix), restart_applied_key(prefix))
    return RestartState(auto=auto, pending=auto and int(wanted or 0) > int(applied or 0))


def _sealed(key: str, value: Any) -> Any:
    """Giá trị ghi vào bảng: trường nhạy cảm (`SENSITIVE`) được mã hoá (`secretbox`)."""
    if key not in SENSITIVE or value is None:
        return value
    return secretbox.current().encrypt(key, value)


def _opened(key: str, value: Any) -> tuple[bool, Any]:
    """(đọc được?, giá trị) của một dòng: giải mã nếu đã mã hoá; hỏng / thiếu khoá thì bỏ dòng."""
    if not secretbox.is_encrypted(value):
        return True, value
    try:
        return True, secretbox.current().decrypt(key, value)
    except (secretbox.DecryptError, RuntimeError) as exc:
        log.error("settings.decrypt_failed", key=key, error=str(exc))
        return False, None


async def load_overrides(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Các dòng `app_settings` còn trong danh mục (key → giá trị JSON đã giải mã, chưa kiểm).
    Dòng không giải mã được (mất khoá) bị bỏ qua: cài đặt đó về biến môi trường / mặc định."""
    async with sessionmaker() as session:
        rows = (await session.execute(select(AppSetting.key, AppSetting.value))).all()
    out: dict[str, Any] = {}
    for key, value in rows:
        if key in SPEC_BY_KEY:
            ok, opened = _opened(key, value)
            if ok:
                out[key] = opened
    return out


async def seal_stored(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Mã hoá (lại) các dòng nhạy cảm còn lưu nguyên văn hoặc bằng khoá cũ (sau khi đổi
    ENCRYPTION_KEYS); giá trị không đổi nên không tăng version. Trả số dòng đã ghi lại."""
    box = secretbox.current()
    changed = 0
    async with sessionmaker.begin() as session:
        rows = (
            await session.execute(
                select(AppSetting).where(AppSetting.key.in_(sorted(SENSITIVE))).with_for_update()
            )
        ).scalars()
        for row in rows:
            if row.value is None or box.is_current(row.value):
                continue
            ok, opened = _opened(row.key, row.value)
            if ok:
                row.value = box.encrypt(row.key, opened)
                changed += 1
    if changed:
        log.info("settings.sealed", rows=changed, key_id=box.active)
    return changed


async def load_effective(
    sessionmaker: async_sessionmaker[AsyncSession], base: Settings
) -> Settings:
    return apply_overrides(base, await load_overrides(sessionmaker))


def _unmask_proxies(values: Any, current: list[str]) -> Any:
    """Dòng proxy còn mật khẩu ẩn (`user:•••@`) → dòng gốc đang lưu, để sửa danh sách mà không
    phải nhập lại mọi mật khẩu."""
    if not isinstance(values, list):
        return values
    known = {mask_proxy(p): p for p in current}
    out: list[Any] = []
    for value in values:
        if isinstance(value, str) and PROXY_MASK in value:
            out.append(known.get(value.split("#", 1)[0].strip(), value))
        else:
            out.append(value)
    return out


@dataclass(frozen=True, slots=True)
class Prepared:
    """Thay đổi đã kiểm: giá trị cần ghi, key cần xoá, Settings hiệu lực trước / sau."""

    upserts: dict[str, Any]
    resets: set[str]
    before: Settings
    after: Settings


def prepare(base: Settings, current: Mapping[str, Any], changes: Mapping[str, Any]) -> Prepared:
    """Kiểm tra cùng lúc mọi thay đổi trên nền các dòng đang lưu (`current`), kể cả ràng buộc
    giữa nhiều cài đặt. `None` = về giá trị env / mặc định; ô bí mật rỗng hoặc bằng `MASK` = giữ
    nguyên; với field cho phép trống, chuỗi rỗng = để trống. Lỗi → `SettingsError`."""
    applied = _valid_overrides(current)
    before = apply_overrides(base, current)

    errors: dict[str, str] = {}
    upserts: dict[str, Any] = {}
    resets: set[str] = set()
    for key, raw in changes.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            known = key in Settings.model_fields
            errors[key] = "Chỉ đặt được bằng biến môi trường" if known else "Không có cài đặt này"
            continue
        if raw is None:
            resets.add(key)
            continue
        if spec.kind == "secret" and raw in ("", MASK):
            continue
        if key == "proxies":
            raw = _unmask_proxies(raw, before.proxies)
        if raw == "" and spec.nullable:
            upserts[key] = None
            continue
        try:
            upserts[key] = validate_value(key, raw)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise SettingsError(errors)

    merged = {k: v for k, v in applied.items() if k not in resets} | upserts
    after = base.model_copy(update=merged) if merged else base
    for keys, message in _cross_errors(after):
        targets = [k for k in keys if k in changes] or list(keys)
        for key in targets:
            errors.setdefault(key, message)
    if errors:
        raise SettingsError(errors)
    return Prepared(upserts, resets, before, after)


async def save(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    prefix: str,
    base: Settings,
    changes: Mapping[str, Any],
    *,
    user: str | None,
    action: str | None = None,
) -> Settings:
    """Kiểm tra (`prepare`) rồi lưu mọi thay đổi trong một transaction; lỗi → `SettingsError`,
    không lưu gì. Có thay đổi thì tăng version + PUBLISH để process khác nạp lại. Trả Settings
    hiệu lực mới.

    Nhật ký (`audit_events`, cùng transaction): mỗi lần lưu có thay đổi thật ghi một dòng,
    `actor` = `user`. `changes` = {key: {"before", "after"}} — giá trị hiệu lực trước / sau (về
    giá trị env / mặc định thì `after` là giá trị đó), che như trang Cài đặt: bí mật thành `MASK`
    khi có đặt, `None` khi không; proxy ẩn mật khẩu (`audit_changes`). `action` =
    `settings.reset` khi mọi key đổi đều là về env / mặc định, còn lại `settings.update` (bên gọi
    đặt `action` thì dùng tên đó, vd. `settings.import_env`); `metadata.reset` liệt kê các key về
    env / mặc định (khi có)."""
    current = await load_overrides(sessionmaker)
    prepared = prepare(base, current, changes)
    upserts, resets, effective = prepared.upserts, prepared.resets, prepared.after
    if not upserts and not resets:
        return effective
    changed = 0
    async with sessionmaker.begin() as session:
        diff = audit_changes(current, prepared)
        if diff:
            reset = sorted(k for k in diff if k in resets)
            await audit_service.record(
                session,
                action=action
                or (
                    audit_service.SETTINGS_RESET
                    if len(reset) == len(diff)
                    else audit_service.SETTINGS_UPDATE
                ),
                actor=user,
                resource_type="settings",
                changes=diff,
                metadata={"reset": reset} if reset else None,
            )
        if resets:
            result = await session.execute(delete(AppSetting).where(AppSetting.key.in_(resets)))
            changed += result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
        for key, value in upserts.items():
            stmt = pg_insert(AppSetting).values(
                key=key, value=_sealed(key, _jsonable(key, value)), updated_by=user
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={
                    "value": stmt.excluded.value,
                    "updated_by": stmt.excluded.updated_by,
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            changed += 1
    if changed:
        version = await redis.incr(version_key(prefix))
        restart = restart_required(prepared.before, effective, [*upserts, *resets])
        if restart:
            await redis.incr(restart_key(prefix))
        await redis.publish(channel(prefix), str(version))
        log.info(
            "settings.saved",
            user=user,
            version=version,
            changed=sorted(upserts),
            reset=sorted(resets),
            restart=restart,
        )
    return effective


def env_overrides(base: Settings, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Cài đặt sửa được ở giao diện mà biến môi trường / `.env` của process đặt khác mặc định và
    chưa có dòng trong bảng: key → giá trị dạng JSON (bí mật nguyên văn), như gửi lên `save`."""
    out: dict[str, Any] = {}
    for spec in SPECS:
        key = spec.key
        if key in overrides or key not in base.model_fields_set:
            continue
        value = _jsonable(key, getattr(base, key))
        default = _jsonable(key, spec.field.get_default(call_default_factory=True))
        if value != default:
            out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class Imported:
    keys: list[str]  # đã chép vào bảng
    skipped: dict[str, str]  # không hợp lệ ở giao diện: key → lý do (vẫn lấy từ biến môi trường)


async def import_env(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    prefix: str,
    base: Settings,
    *,
    user: str | None,
) -> Imported:
    """Chép `env_overrides` vào bảng (như lưu ở giao diện: kiểm tra, nhật ký, tăng version) để
    trang Cài đặt là nơi quản lý chúng; giá trị hiệu lực không đổi. Giá trị không qua kiểm tra
    của giao diện thì bỏ qua (giữ ở biến môi trường), phần còn lại vẫn chép."""
    values = env_overrides(base, await load_overrides(sessionmaker))
    skipped: dict[str, str] = {}
    while values:
        try:
            await save(
                sessionmaker,
                redis,
                prefix,
                base,
                values,
                user=user,
                action=audit_service.SETTINGS_IMPORT_ENV,
            )
            break
        except SettingsError as exc:
            bad = {k: v for k, v in exc.errors.items() if k in values} or dict.fromkeys(values, "")
            skipped |= bad
            values = {k: v for k, v in values.items() if k not in bad}
    if skipped:
        log.warning("settings.import_env_skipped", keys=sorted(skipped))
    return Imported(sorted(values), skipped)


def _audit_value(spec: SettingSpec, value: Any) -> Any:
    if spec.kind == "secret":
        return MASK if _secret_set(spec, value) else None
    return _display(spec, value)


def audit_changes(current: Mapping[str, Any], prepared: Prepared) -> dict[str, dict[str, Any]]:
    """Thay đổi ghi nhật ký của một lần lưu: key → {"before", "after"} (giá trị hiệu lực, đã che
    — không bao giờ có giá trị bí mật). Bỏ qua key lưu lại đúng giá trị đang lưu và key về env /
    mặc định khi vốn không có dòng `app_settings`."""
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(prepared.upserts.keys() | prepared.resets):
        if key in prepared.resets:
            if key not in current:
                continue
        elif key in current and current[key] == _jsonable(key, prepared.upserts[key]):
            continue
        spec = SPEC_BY_KEY[key]
        out[key] = {
            "before": _audit_value(spec, getattr(prepared.before, key)),
            "after": _audit_value(spec, getattr(prepared.after, key)),
        }
    return out


# --- hiển thị ------------------------------------------------------------------------------------


def mask_proxy(line: str) -> str:
    """Ẩn mật khẩu trong một dòng proxy, giữ scheme, user, host:port."""
    text = line.split("#", 1)[0].strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.password is None and "@" not in parts.netloc:
            return text
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        user = parts.username or ""
        return f"{parts.scheme}://{user}:{PROXY_MASK}@{parts.hostname or ''}{port}"
    pieces = text.split(":")
    if len(pieces) <= 2:
        return text
    if len(pieces) == 4:
        return f"{pieces[0]}:{pieces[1]}:{pieces[2]}:{PROXY_MASK}"
    return PROXY_MASK


def _secret_set(spec: SettingSpec, value: Any) -> bool:
    if value is None:
        return False
    default = spec.field.get_default(call_default_factory=True)
    text = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    default_text = default.get_secret_value() if isinstance(default, SecretStr) else default
    return bool(text) and text != default_text


def _display(spec: SettingSpec, value: Any) -> Any:
    """Giá trị trả ra API: bí mật không bao giờ lộ, proxy ẩn mật khẩu."""
    if spec.kind == "secret":
        return None
    if spec.key == "proxies":
        return [mask_proxy(p) for p in value or []]
    return _jsonable(spec.key, value)


def describe(
    base: Settings, overrides: Mapping[str, Any], effective: Settings
) -> list[dict[str, Any]]:
    """Danh mục theo nhóm kèm giá trị hiệu lực, mặc định, giá trị từ biến môi trường và nguồn
    (`default` | `env` | `ui`) của từng cài đặt, cho trang Cài đặt."""
    out: list[dict[str, Any]] = []
    for group in GROUPS:
        items: list[dict[str, Any]] = []
        for spec in group.items:
            key = spec.key
            from_env = key in base.model_fields_set
            source: Source = "ui" if key in overrides else "env" if from_env else "default"
            options = board_options() if key == "boards" else spec.options
            secret = spec.kind == "secret"
            items.append(
                {
                    "key": key,
                    "label": spec.label,
                    "help": spec.help,
                    "kind": spec.kind,
                    "options": [
                        {"value": o.value, "label": o.label, "help": o.help} for o in options
                    ],
                    "unit": spec.unit,
                    "min": spec.min,
                    "max": spec.max,
                    "min_exclusive": spec.min_exclusive,
                    "max_exclusive": spec.max_exclusive,
                    "nullable": spec.nullable,
                    "placeholder": spec.placeholder,
                    "value_type": spec.value_type,
                    "live": spec.live,
                    "value": _display(spec, getattr(effective, key)),
                    "default": _display(spec, spec.field.get_default(call_default_factory=True)),
                    "env_value": _display(spec, getattr(base, key)) if from_env else None,
                    "source": source,
                    "secret": secret,
                    "is_set": _secret_set(spec, getattr(effective, key)) if secret else None,
                    "env_is_set": _secret_set(spec, getattr(base, key)) if secret else None,
                    "auto_value": AUTO_VALUES[key]() if key in AUTO_VALUES else None,
                }
            )
        out.append(
            {
                "group": group.key,
                "title": group.title,
                "description": group.description,
                "sections": [
                    {"title": s.title, "description": s.description, "keys": list(s.keys)}
                    for s in group.sections
                ],
                "items": items,
            }
        )
    return out
