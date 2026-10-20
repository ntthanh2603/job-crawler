"""Mô hình trả về của `/api/meta`."""

from __future__ import annotations

from jobcrawler.common.api import ApiOut


class MetaOut(ApiOut):
    provinces: list[str]
    levels: list[str]
    employment_types: list[str]
    work_modes: list[str]
    sources: list[str]
    kyc_statuses: list[str]
    run_statuses: list[str]
    boards: list[str]
