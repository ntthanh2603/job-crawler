"""Danh mục cài đặt sửa ở giao diện: khớp `config.Settings`, kiểm tra giá trị, ẩn bí mật."""

from __future__ import annotations

import json
import typing
from typing import Any

import pytest
from pydantic import SecretStr

from jobcrawler.common.config import Settings
from jobcrawler.modules.settings import app_settings as a
from jobcrawler.modules.settings.app_settings import SettingsError

pytestmark = pytest.mark.usefixtures("clean_env")


def base(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # pyright: ignore[reportCallIssue]


def items(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for g in groups for item in g["items"]}


def test_every_spec_is_a_settings_field_and_not_env_only() -> None:
    assert a.SPECS, "danh mục rỗng"
    for spec in a.SPECS:
        assert spec.key in Settings.model_fields, spec.key
        assert spec.key not in a.ENV_ONLY, spec.key
        assert spec.group, spec.key
    assert len(a.SPEC_BY_KEY) == len(a.SPECS), "key trùng"
    assert set(Settings.model_fields) >= a.ENV_ONLY
    # Kết nối, bí mật hạ tầng, đăng nhập giao diện không bao giờ sửa ở giao diện.
    for key in ("database_url", "redis_url", "redis_prefix", "gateway_token", "ui_password"):
        assert key not in a.SPEC_BY_KEY


def test_groups_in_order() -> None:
    assert [g.key for g in a.GROUPS] == [
        "schedule",
        "runner",
        "workers",
        "proxy",
        "network",
        "sources",
        "boards",
        "ai",
        "quality",
        "maintenance",
    ]
    assert all(g.title and g.description and g.items for g in a.GROUPS)
    assert a.SPEC_BY_KEY["poll_interval_seconds"].group == "schedule"
    assert a.SPEC_BY_KEY["llm_api_key"].group == "ai"


def test_every_setting_is_in_exactly_one_section_of_its_group() -> None:
    for group in a.GROUPS:
        assert group.sections, group.key
        keys = [k for s in group.sections for k in s.keys]
        assert len(keys) == len(set(keys)), f"{group.key}: trùng mục"
        assert set(keys) == {spec.key for spec in group.items}, group.key
        assert all(s.title for s in group.sections)


def test_describe_lists_sections() -> None:
    s = base()
    groups = a.describe(s, {}, s)
    schedule = next(g for g in groups if g["group"] == "schedule")
    assert [sec["title"] for sec in schedule["sections"]][:2] == ["Khung giờ", "Nhịp poll"]
    assert schedule["sections"][0]["keys"] == ["crawl_timezone", "build_window", "poll_window"]


def test_enum_options_come_from_literal() -> None:
    for key in ("proxy_mode", "serp_provider"):
        spec = a.SPEC_BY_KEY[key]
        assert spec.kind == "enum"
        expected = typing.get_args(Settings.model_fields[key].annotation)
        assert tuple(o.value for o in spec.options) == expected
    assert all(o.help for o in a.SPEC_BY_KEY["proxy_mode"].options)


def test_bounds_and_kinds() -> None:
    poll = a.SPEC_BY_KEY["poll_interval_seconds"]
    assert (poll.min, poll.max, poll.min_exclusive, poll.unit) == (10, None, False, "giây")
    serp = a.SPEC_BY_KEY["serp_results"]
    assert (serp.min, serp.max) == (1, 20)
    assert a.SPEC_BY_KEY["llm_timeout_seconds"].min_exclusive
    assert a.SPEC_BY_KEY["llm_requests_per_minute"].nullable
    assert not a.SPEC_BY_KEY["poll_interval_seconds"].nullable
    assert a.SPEC_BY_KEY["build_window"].kind == "window"
    assert a.SPEC_BY_KEY["board_tail_cron"].kind == "cron"
    assert a.SPEC_BY_KEY["serp_api_key"].kind == "secret"


def test_live_flags() -> None:
    live = {s.key: s.live for s in a.SPECS}
    assert live["poll_interval_seconds"]
    assert live["build_window"]
    assert live["kyc_min_confidence"]
    assert live["gateway_http_slots"]  # đẩy xuống worker qua gateway
    assert not live["llm_base_url"]
    assert not live["llm_api_key"]
    assert not live["runner_poll_concurrency"]
    assert not live["compression_level"]
    assert not live["board_tail_cron"]
    assert not live["proxy_mode"]


def test_validate_value_checks_type_bounds_and_format() -> None:
    assert a.validate_value("poll_interval_seconds", "120") == 120.0
    with pytest.raises(ValueError, match="lớn hơn hoặc bằng 10"):
        a.validate_value("poll_interval_seconds", 5)
    with pytest.raises(ValueError, match="số nguyên"):
        a.validate_value("gateway_http_slots", 1.5)
    assert a.validate_value("build_window", "6:00-24:00") == "06:00-24:00"
    with pytest.raises(ValueError, match="HH:MM"):
        a.validate_value("poll_window", "sáng")
    assert a.validate_value("board_tail_cron", " */2  6-23 * * * ") == "*/2 6-23 * * *"
    with pytest.raises(ValueError, match="cron"):
        a.validate_value("maintenance_cron", "mỗi giờ")
    with pytest.raises(ValueError, match="múi giờ"):
        a.validate_value("crawl_timezone", "Sao/Hoả")
    assert a.validate_value("rate_limits", {" WWW.TopCV.vn ": "0.5"}) == {"www.topcv.vn": 0.5}
    with pytest.raises(ValueError, match="lớn hơn 0"):
        a.validate_value("rate_limits", {"a.vn": 0})
    assert a.validate_value("boards", ["itviec", "topcv", "topcv"]) == ["topcv", "itviec"]
    with pytest.raises(ValueError, match="khong-co"):
        a.validate_value("boards", ["khong-co"])
    with pytest.raises(ValueError, match="Dòng 2"):
        a.validate_value("proxies", ["1.2.3.4:8080", "rác"])
    with pytest.raises(ValueError, match="http"):
        a.validate_value("proxy_check_url", "ftp://x")
    with pytest.raises(ValueError, match="một trong"):
        a.validate_value("proxy_mode", "tor")


def test_apply_overrides_skips_invalid_and_unknown() -> None:
    s = base()
    assert a.apply_overrides(s, {}) is s
    eff = a.apply_overrides(
        s,
        {
            "poll_interval_seconds": 120,
            "rebuild_on_error": False,
            "poll_jitter_seconds": "không phải số",
            "database_url": "postgresql://hack",  # chỉ đặt bằng biến môi trường
            "khong_co": 1,
        },
    )
    assert eff.poll_interval_seconds == 120
    assert eff.rebuild_on_error is False
    assert eff.poll_jitter_seconds == s.poll_jitter_seconds
    assert eff.database_url == s.database_url
    assert s.poll_interval_seconds == 150  # base không bị sửa


def test_apply_overrides_drops_conflicting_values() -> None:
    eff = a.apply_overrides(base(), {"poll_deadline_seconds": 100, "rebuild_on_error": False})
    assert eff.poll_deadline_seconds == 60  # vượt timeout cứng 90: bỏ
    assert eff.rebuild_on_error is False


def test_prepare_maps_cross_field_errors_to_changed_keys() -> None:
    s = base()
    with pytest.raises(SettingsError) as exc:
        a.prepare(s, {}, {"poll_deadline_seconds": 100})
    assert set(exc.value.errors) == {"poll_deadline_seconds"}
    assert "nhỏ hơn" in exc.value.errors["poll_deadline_seconds"]

    with pytest.raises(SettingsError) as exc:
        a.prepare(s, {}, {"company_deadline_seconds": 200, "company_timeout_seconds": 150})
    assert set(exc.value.errors) == {"company_deadline_seconds", "company_timeout_seconds"}

    # Nhịp báo sống của worker dài quá hạn sống / 3: worker lỡ một nhịp là bị coi như đã chết.
    with pytest.raises(SettingsError) as exc:
        a.prepare(s, {}, {"gateway_heartbeat_seconds": 11})
    assert set(exc.value.errors) == {"gateway_heartbeat_seconds"}
    assert "gấp 3" in exc.value.errors["gateway_heartbeat_seconds"]

    # Đổi cả hai cùng lúc cho hợp lệ thì được.
    ok = a.prepare(s, {}, {"poll_deadline_seconds": 100, "poll_timeout_seconds": 120})
    assert (ok.after.poll_deadline_seconds, ok.after.poll_timeout_seconds) == (100, 120)

    # Về mặc định một giá trị làm hỏng ràng buộc với giá trị đang lưu: lỗi ở key vừa đổi.
    current = {"poll_deadline_seconds": 100, "poll_timeout_seconds": 120}
    with pytest.raises(SettingsError) as exc:
        a.prepare(s, current, {"poll_timeout_seconds": None})
    assert set(exc.value.errors) == {"poll_timeout_seconds"}


def test_prepare_rejects_unknown_and_env_only_keys() -> None:
    with pytest.raises(SettingsError) as exc:
        a.prepare(base(), {}, {"database_url": "x", "khong_co": 1, "poll_interval_seconds": 5})
    errors = exc.value.errors
    assert errors["database_url"] == "Chỉ đặt được bằng biến môi trường"
    assert errors["khong_co"] == "Không có cài đặt này"
    assert "10" in errors["poll_interval_seconds"]


def test_prepare_secrets_resets_and_nullable() -> None:
    s = base(serp_api_key="sk-env")
    p = a.prepare(s, {"serp_api_key": "sk-ui"}, {"serp_api_key": a.MASK, "llm_api_key": ""})
    assert p.upserts == {}
    assert p.resets == set()
    assert p.after.serp_api_key == SecretStr("sk-ui")

    p = a.prepare(s, {"serp_api_key": "sk-ui"}, {"serp_api_key": None})
    assert p.resets == {"serp_api_key"}
    assert p.after.serp_api_key == SecretStr("sk-env")

    p = a.prepare(s, {}, {"llm_requests_per_minute": "", "llm_base_url": ""})
    assert p.upserts == {"llm_requests_per_minute": None, "llm_base_url": None}
    assert not p.after.llm_enabled


def test_prepare_keeps_masked_proxy_passwords() -> None:
    s = base(proxies=["http://u:pw@1.2.3.4:8080"])
    shown = items(a.describe(s, {}, s))["proxies"]["value"]
    assert shown == ["http://u:•••@1.2.3.4:8080"]
    p = a.prepare(s, {}, {"proxies": [*shown, "5.6.7.8:3128"]})
    assert p.upserts["proxies"] == ["http://u:pw@1.2.3.4:8080", "5.6.7.8:3128"]
    with pytest.raises(SettingsError) as exc:
        a.prepare(s, {}, {"proxies": ["http://u:•••@9.9.9.9:1"]})
    assert "nhập lại" in exc.value.errors["proxies"]


def test_describe_hides_secrets_and_reports_source() -> None:
    s = base(
        serp_api_key="sk-env-secret",
        proxies=["http://user:pw-proxy@1.2.3.4:8080", "5.6.7.8:3128:u2:pw-two"],
        poll_interval_seconds=120,
    )
    overrides = {"llm_api_key": "sk-ui-secret", "rebuild_on_error": False}
    eff = a.apply_overrides(s, overrides)
    groups = a.describe(s, overrides, eff)
    text = json.dumps(groups, ensure_ascii=False)
    for secret in ("sk-env-secret", "sk-ui-secret", "pw-proxy", "pw-two", "EMPTY"):
        assert secret not in text
    by_key = items(groups)
    assert [g["group"] for g in groups] == [g.key for g in a.GROUPS]
    serp = by_key["serp_api_key"]
    assert (serp["value"], serp["default"], serp["env_value"]) == (None, None, None)
    assert (serp["secret"], serp["is_set"], serp["env_is_set"], serp["source"]) == (
        True,
        True,
        True,
        "env",
    )
    assert (by_key["llm_api_key"]["source"], by_key["llm_api_key"]["is_set"]) == ("ui", True)
    assert by_key["proxies"]["value"] == ["http://user:•••@1.2.3.4:8080", "5.6.7.8:3128:u2:•••"]
    poll = by_key["poll_interval_seconds"]
    assert (poll["source"], poll["value"], poll["default"], poll["env_value"]) == (
        "env",
        120,
        150,
        120,
    )
    assert by_key["rebuild_on_error"]["source"] == "ui"
    assert by_key["rebuild_on_error"]["value"] is False
    assert by_key["poll_jitter_seconds"]["source"] == "default"
    assert by_key["poll_jitter_seconds"]["env_value"] is None
    assert by_key["robots_user_agents"]["default"] == ["*"]  # default_factory
    boards = by_key["boards"]["options"]
    assert boards[0]["value"] == "topcv"
    assert len(boards) >= 5
    assert [o["value"] for o in by_key["proxy_mode"]["options"]] == ["worker", "central", "off"]
    assert not any(k in by_key for k in a.ENV_ONLY)


def test_restart_required_lists_changed_non_live_keys() -> None:
    before = base()
    after = before.model_copy(
        update={"llm_model": "qwen", "poll_interval_seconds": 120, "compression_level": 9}
    )
    keys = ["llm_model", "poll_interval_seconds", "compression_level"]
    assert a.restart_required(before, after, keys) == ["llm_model"]


def test_env_overrides_lists_env_values_not_in_db() -> None:
    s = base(
        llm_model="qwen3",
        llm_api_key=SecretStr("sk-1"),
        board_concurrency=7,
        poll_interval_seconds=Settings.model_fields["poll_interval_seconds"].default,
        ui_password=SecretStr("pw"),
    )
    # Trùng mặc định thì không cần chép; ENV_ONLY (ui_password) không bao giờ vào bảng.
    assert a.env_overrides(s, {}) == {
        "llm_model": "qwen3",
        "llm_api_key": "sk-1",
        "board_concurrency": 7,
    }
    # Đã có dòng trong bảng thì giá trị ở giao diện thắng: không chép đè.
    assert a.env_overrides(s, {"llm_model": "khac"}) == {
        "llm_api_key": "sk-1",
        "board_concurrency": 7,
    }


def test_mask_proxy_formats() -> None:
    assert a.mask_proxy("socks5://u:p@h.vn:1080") == "socks5://u:•••@h.vn:1080"
    assert a.mask_proxy("http://h.vn:3128") == "http://h.vn:3128"
    assert a.mask_proxy("1.2.3.4:80") == "1.2.3.4:80"
    assert a.mask_proxy("1.2.3.4:80:user:pass") == "1.2.3.4:80:user:•••"
    assert a.mask_proxy("a:b:c") == "•••"
