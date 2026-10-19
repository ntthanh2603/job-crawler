from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_KIND_UNSPECIFIED: _ClassVar[TaskKind]
    TASK_KIND_HTTP: _ClassVar[TaskKind]
    TASK_KIND_RENDER: _ClassVar[TaskKind]

class ErrorKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ERROR_KIND_NONE: _ClassVar[ErrorKind]
    ERROR_KIND_TIMEOUT: _ClassVar[ErrorKind]
    ERROR_KIND_PROXY: _ClassVar[ErrorKind]
    ERROR_KIND_CONNECT: _ClassVar[ErrorKind]
    ERROR_KIND_OTHER: _ClassVar[ErrorKind]
    ERROR_KIND_EXPIRED: _ClassVar[ErrorKind]
    ERROR_KIND_TOO_LARGE: _ClassVar[ErrorKind]
    ERROR_KIND_UNSUPPORTED: _ClassVar[ErrorKind]
    ERROR_KIND_WORKER_LOST: _ClassVar[ErrorKind]
TASK_KIND_UNSPECIFIED: TaskKind
TASK_KIND_HTTP: TaskKind
TASK_KIND_RENDER: TaskKind
ERROR_KIND_NONE: ErrorKind
ERROR_KIND_TIMEOUT: ErrorKind
ERROR_KIND_PROXY: ErrorKind
ERROR_KIND_CONNECT: ErrorKind
ERROR_KIND_OTHER: ErrorKind
ERROR_KIND_EXPIRED: ErrorKind
ERROR_KIND_TOO_LARGE: ErrorKind
ERROR_KIND_UNSUPPORTED: ErrorKind
ERROR_KIND_WORKER_LOST: ErrorKind

class WorkerInfo(_message.Message):
    __slots__ = ("worker_id", "hostname", "version", "pool", "http_slots", "render_slots", "local_proxies", "labels", "started_at_ms")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    POOL_FIELD_NUMBER: _ClassVar[int]
    HTTP_SLOTS_FIELD_NUMBER: _ClassVar[int]
    RENDER_SLOTS_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PROXIES_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    hostname: str
    version: str
    pool: str
    http_slots: int
    render_slots: int
    local_proxies: int
    labels: _containers.ScalarMap[str, str]
    started_at_ms: int
    def __init__(self, worker_id: _Optional[str] = ..., hostname: _Optional[str] = ..., version: _Optional[str] = ..., pool: _Optional[str] = ..., http_slots: _Optional[int] = ..., render_slots: _Optional[int] = ..., local_proxies: _Optional[int] = ..., labels: _Optional[_Mapping[str, str]] = ..., started_at_ms: _Optional[int] = ...) -> None: ...

class WorkerConfig(_message.Message):
    __slots__ = ("version", "http_slots", "render_slots", "impersonate", "max_body_bytes", "heartbeat_seconds", "lease_wait_ms", "use_local_proxies")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    HTTP_SLOTS_FIELD_NUMBER: _ClassVar[int]
    RENDER_SLOTS_FIELD_NUMBER: _ClassVar[int]
    IMPERSONATE_FIELD_NUMBER: _ClassVar[int]
    MAX_BODY_BYTES_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    LEASE_WAIT_MS_FIELD_NUMBER: _ClassVar[int]
    USE_LOCAL_PROXIES_FIELD_NUMBER: _ClassVar[int]
    version: int
    http_slots: int
    render_slots: int
    impersonate: str
    max_body_bytes: int
    heartbeat_seconds: int
    lease_wait_ms: int
    use_local_proxies: bool
    def __init__(self, version: _Optional[int] = ..., http_slots: _Optional[int] = ..., render_slots: _Optional[int] = ..., impersonate: _Optional[str] = ..., max_body_bytes: _Optional[int] = ..., heartbeat_seconds: _Optional[int] = ..., lease_wait_ms: _Optional[int] = ..., use_local_proxies: _Optional[bool] = ...) -> None: ...

class FetchTask(_message.Message):
    __slots__ = ("task_id", "kind", "method", "url", "headers", "body", "proxy", "proxy_id", "timeout_seconds", "allow_redirects", "wait_until", "capture_json", "deadline_ms", "source", "direct", "priority")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    PROXY_FIELD_NUMBER: _ClassVar[int]
    PROXY_ID_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_REDIRECTS_FIELD_NUMBER: _ClassVar[int]
    WAIT_UNTIL_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_JSON_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_MS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DIRECT_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    kind: TaskKind
    method: str
    url: str
    headers: _containers.ScalarMap[str, str]
    body: bytes
    proxy: str
    proxy_id: str
    timeout_seconds: float
    allow_redirects: bool
    wait_until: str
    capture_json: bool
    deadline_ms: int
    source: str
    direct: bool
    priority: int
    def __init__(self, task_id: _Optional[str] = ..., kind: _Optional[_Union[TaskKind, str]] = ..., method: _Optional[str] = ..., url: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ..., body: _Optional[bytes] = ..., proxy: _Optional[str] = ..., proxy_id: _Optional[str] = ..., timeout_seconds: _Optional[float] = ..., allow_redirects: _Optional[bool] = ..., wait_until: _Optional[str] = ..., capture_json: _Optional[bool] = ..., deadline_ms: _Optional[int] = ..., source: _Optional[str] = ..., direct: _Optional[bool] = ..., priority: _Optional[int] = ...) -> None: ...

class CapturedJson(_message.Message):
    __slots__ = ("url", "method", "status", "body", "request_json")
    URL_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    REQUEST_JSON_FIELD_NUMBER: _ClassVar[int]
    url: str
    method: str
    status: int
    body: bytes
    request_json: str
    def __init__(self, url: _Optional[str] = ..., method: _Optional[str] = ..., status: _Optional[int] = ..., body: _Optional[bytes] = ..., request_json: _Optional[str] = ...) -> None: ...

class FetchResult(_message.Message):
    __slots__ = ("task_id", "status", "final_url", "headers", "body", "body_zstd", "captured", "error_kind", "error", "proxy_id", "duration_ms", "worker_id")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FINAL_URL_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    BODY_ZSTD_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_FIELD_NUMBER: _ClassVar[int]
    ERROR_KIND_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    PROXY_ID_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    status: int
    final_url: str
    headers: _containers.ScalarMap[str, str]
    body: bytes
    body_zstd: bool
    captured: _containers.RepeatedCompositeFieldContainer[CapturedJson]
    error_kind: ErrorKind
    error: str
    proxy_id: str
    duration_ms: int
    worker_id: str
    def __init__(self, task_id: _Optional[str] = ..., status: _Optional[int] = ..., final_url: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ..., body: _Optional[bytes] = ..., body_zstd: _Optional[bool] = ..., captured: _Optional[_Iterable[_Union[CapturedJson, _Mapping]]] = ..., error_kind: _Optional[_Union[ErrorKind, str]] = ..., error: _Optional[str] = ..., proxy_id: _Optional[str] = ..., duration_ms: _Optional[int] = ..., worker_id: _Optional[str] = ...) -> None: ...

class RegisterRequest(_message.Message):
    __slots__ = ("info",)
    INFO_FIELD_NUMBER: _ClassVar[int]
    info: WorkerInfo
    def __init__(self, info: _Optional[_Union[WorkerInfo, _Mapping]] = ...) -> None: ...

class RegisterResponse(_message.Message):
    __slots__ = ("config",)
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    config: WorkerConfig
    def __init__(self, config: _Optional[_Union[WorkerConfig, _Mapping]] = ...) -> None: ...

class LeaseRequest(_message.Message):
    __slots__ = ("worker_id", "http_slots", "render_slots", "wait_ms")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    HTTP_SLOTS_FIELD_NUMBER: _ClassVar[int]
    RENDER_SLOTS_FIELD_NUMBER: _ClassVar[int]
    WAIT_MS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    http_slots: int
    render_slots: int
    wait_ms: int
    def __init__(self, worker_id: _Optional[str] = ..., http_slots: _Optional[int] = ..., render_slots: _Optional[int] = ..., wait_ms: _Optional[int] = ...) -> None: ...

class LeaseResponse(_message.Message):
    __slots__ = ("tasks", "config_version", "drain")
    TASKS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    DRAIN_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[FetchTask]
    config_version: int
    drain: bool
    def __init__(self, tasks: _Optional[_Iterable[_Union[FetchTask, _Mapping]]] = ..., config_version: _Optional[int] = ..., drain: _Optional[bool] = ...) -> None: ...

class CompleteRequest(_message.Message):
    __slots__ = ("worker_id", "results")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    results: _containers.RepeatedCompositeFieldContainer[FetchResult]
    def __init__(self, worker_id: _Optional[str] = ..., results: _Optional[_Iterable[_Union[FetchResult, _Mapping]]] = ...) -> None: ...

class CompleteResponse(_message.Message):
    __slots__ = ("accepted", "dropped")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    DROPPED_FIELD_NUMBER: _ClassVar[int]
    accepted: int
    dropped: int
    def __init__(self, accepted: _Optional[int] = ..., dropped: _Optional[int] = ...) -> None: ...

class WorkerStats(_message.Message):
    __slots__ = ("http_done", "http_failed", "render_done", "render_failed", "http_inflight", "render_inflight", "bytes_in", "cpu_percent", "rss_bytes", "proxies_alive", "render_disabled_reason", "render_retry_at_ms")
    HTTP_DONE_FIELD_NUMBER: _ClassVar[int]
    HTTP_FAILED_FIELD_NUMBER: _ClassVar[int]
    RENDER_DONE_FIELD_NUMBER: _ClassVar[int]
    RENDER_FAILED_FIELD_NUMBER: _ClassVar[int]
    HTTP_INFLIGHT_FIELD_NUMBER: _ClassVar[int]
    RENDER_INFLIGHT_FIELD_NUMBER: _ClassVar[int]
    BYTES_IN_FIELD_NUMBER: _ClassVar[int]
    CPU_PERCENT_FIELD_NUMBER: _ClassVar[int]
    RSS_BYTES_FIELD_NUMBER: _ClassVar[int]
    PROXIES_ALIVE_FIELD_NUMBER: _ClassVar[int]
    RENDER_DISABLED_REASON_FIELD_NUMBER: _ClassVar[int]
    RENDER_RETRY_AT_MS_FIELD_NUMBER: _ClassVar[int]
    http_done: int
    http_failed: int
    render_done: int
    render_failed: int
    http_inflight: int
    render_inflight: int
    bytes_in: int
    cpu_percent: float
    rss_bytes: int
    proxies_alive: int
    render_disabled_reason: str
    render_retry_at_ms: int
    def __init__(self, http_done: _Optional[int] = ..., http_failed: _Optional[int] = ..., render_done: _Optional[int] = ..., render_failed: _Optional[int] = ..., http_inflight: _Optional[int] = ..., render_inflight: _Optional[int] = ..., bytes_in: _Optional[int] = ..., cpu_percent: _Optional[float] = ..., rss_bytes: _Optional[int] = ..., proxies_alive: _Optional[int] = ..., render_disabled_reason: _Optional[str] = ..., render_retry_at_ms: _Optional[int] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("worker_id", "stats")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    STATS_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    stats: WorkerStats
    def __init__(self, worker_id: _Optional[str] = ..., stats: _Optional[_Union[WorkerStats, _Mapping]] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("config_version", "drain")
    CONFIG_VERSION_FIELD_NUMBER: _ClassVar[int]
    DRAIN_FIELD_NUMBER: _ClassVar[int]
    config_version: int
    drain: bool
    def __init__(self, config_version: _Optional[int] = ..., drain: _Optional[bool] = ...) -> None: ...

class LeaveRequest(_message.Message):
    __slots__ = ("worker_id",)
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    def __init__(self, worker_id: _Optional[str] = ...) -> None: ...

class LeaveResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
