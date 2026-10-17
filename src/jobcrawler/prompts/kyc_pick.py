"""KYC: chọn website chính thức và bản ghi đăng ký của doanh nghiệp trong danh sách ứng viên.

LLM chỉ chọn theo mã ứng viên (W1, R1...) và chép thông tin kèm trích đoạn nguyên văn; mọi
trích đoạn được kiểm lại bằng rule (phải có trong ứng viên) trước khi dùng làm bằng chứng.
Bằng chứng đứng trước lựa chọn để model đọc lại ứng viên rồi mới quyết định.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class KycQuote(BaseModel):
    candidate: str = Field(description='Mã ứng viên chứa trích đoạn, ví dụ "W1", "R2"')
    quote: str = Field(description="Trích nguyên văn ngắn (dưới 150 ký tự) từ đúng ứng viên đó")


class KycPick(BaseModel):
    evidence: list[KycQuote] = Field(
        description="Tối đa 5 trích đoạn làm bằng chứng cho lựa chọn và các trường bên dưới"
    )
    website: str | None = Field(
        description='Mã ứng viên website chính thức của đúng doanh nghiệp này, ví dụ "W1"; null '
        "nếu không có ứng viên W nào đúng"
    )
    registry: str | None = Field(
        description='Mã ứng viên tra cứu MST/đăng ký đúng doanh nghiệp này, ví dụ "R1"; null nếu '
        "không có"
    )
    legal_name: str | None = Field(
        description="Tên pháp lý đầy đủ (Công ty..., Ngân hàng...), chép nguyên văn; null nếu "
        "không thấy"
    )
    tax_code: str | None = Field(description="Mã số thuế / mã số doanh nghiệp; null nếu không thấy")
    address: str | None = Field(description="Địa chỉ trụ sở chính, chép nguyên văn")
    short_name: str | None = Field(description="Tên viết tắt chính thức; null nếu không thấy")
    brand_name: str | None = Field(description="Tên thương hiệu thường dùng")
    english_name: str | None = Field(
        description="Tên tiếng Anh / tên quốc tế chép nguyên văn; không tự dịch"
    )
    several_companies: bool = Field(
        description="true nếu nhiều doanh nghiệp khác nhau cùng khớp tên đầu vào mà không phân "
        "biệt được"
    )
    confidence: float = Field(ge=0, le=1, description="Mức chắc chắn 0..1 về lựa chọn")
    reason: str = Field(description="Lý do, tối đa 25 từ")


PROMPT = Prompt(
    name="kyc_pick",
    version="2026-10-17.2",
    system=(
        "Bạn xác minh danh tính doanh nghiệp tại Việt Nam. Cho tên doanh nghiệp đầu vào và danh "
        "sách ứng viên: W = website (trang chủ, chân trang, JSON-LD, kết quả tìm kiếm), R = trang "
        "tra cứu mã số thuế/doanh nghiệp, C = nguồn khác. Chọn website chính thức (mã W) và bản "
        "ghi đăng ký (mã R) của ĐÚNG doanh nghiệp đầu vào; chỉ chọn mã có trong danh sách. "
        "Website của công ty mẹ/con là pháp nhân khác, đại lý, nhà phân phối, báo, trang tuyển "
        "dụng, sàn thương mại điện tử không phải website chính thức. Chép tên pháp lý, mã số "
        "thuế, địa chỉ, tên viết tắt, thương hiệu, tên tiếng Anh đúng như trong ứng viên; không "
        "suy đoán, không dịch, thiếu thì null. Bằng chứng là trích đoạn nguyên văn kèm mã ứng "
        "viên. Nhiều doanh nghiệp khác nhau cùng khớp tên mà không phân biệt được thì "
        "several_companies=true. Không chắc thì confidence thấp. Chỉ trả về JSON đúng schema, "
        "ngắn gọn."
    ),
    user=(
        "Doanh nghiệp cần xác minh: $name\n"
        "Mã số thuế đầu vào: $tax_code\n"
        "Website đầu vào: $website\n\n"
        "Ứng viên:\n$candidates"
    ),
)
