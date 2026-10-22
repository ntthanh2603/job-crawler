from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from string import Template

# Thứ tự lấy lượt khi các lời gọi LLM phải chờ nhau (số nhỏ trước): tìm nguồn / bóc tin quyết định
# doanh nghiệp có tin hay không và chặn deadline của lượt build / poll; xác minh quyết định nhận
# tin; phân loại chỉ điền thêm trường còn trống nên chỉ dùng phần còn dư.
PRIORITY_COLLECT = 0
PRIORITY_VERIFY = 1
PRIORITY_ENRICH = 2


@dataclass(frozen=True, slots=True)
class Prompt:
    """Một prompt có tên và version (ghi vào log mỗi lời gọi để truy vết khi đổi prompt).

    `user` là `string.Template` (`$bien`), không dùng `str.format` để ví dụ JSON trong
    prompt không phải escape ngoặc nhọn.
    """

    name: str
    version: str
    system: str
    user: str
    priority: int = PRIORITY_COLLECT

    def render(self, values: Mapping[str, str]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": Template(self.user).substitute(values)},
        ]

    def template_chars(self) -> int:
        """Độ dài phần cố định của prompt người dùng (không tính giá trị biến)."""
        return len(Template(self.user).safe_substitute({}))
