"""Ký webhook, lùi khi lỗi, kiểm URL (`modules/integration/webhook_service.py`), không cần mạng."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from jobcrawler.modules.api_clients.api_clients_service import WebhookConfigError, clean_webhook_url
from jobcrawler.modules.integration import webhook_service


def test_signature_is_hmac_of_timestamp_dot_body() -> None:
    secret, body = "whsec_abc", b'{"events":[]}'
    expected = hmac.new(secret.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert webhook_service.signature(secret, "1700000000", body) == f"v1={expected}"
    headers = webhook_service.headers(secret, body, delivery="d-1", now=1_700_000_000.9)
    assert headers["X-JC-Timestamp"] == "1700000000"
    assert headers["X-JC-Signature"] == f"v1={expected}"
    assert headers["X-JC-Delivery"] == "d-1"
    assert headers["Content-Type"] == "application/json"


def test_backoff_doubles_up_to_five_minutes() -> None:
    assert 0.8 <= webhook_service.backoff(1) <= 1.2
    assert 6.4 <= webhook_service.backoff(4) <= 9.6
    assert webhook_service.backoff(30) <= webhook_service.BACKOFF_MAX_SECONDS * 1.2


def test_new_secret_and_url_hint() -> None:
    secret = webhook_service.new_secret()
    assert secret.startswith("whsec_")
    assert secret != webhook_service.new_secret()
    assert webhook_service.url_hint("https://doi-tac.vn/hooks/jobs?token=x") == (
        "https://doi-tac.vn/hooks/jobs?…"
    )
    assert webhook_service.url_hint("https://doi-tac.vn/hooks") == "https://doi-tac.vn/hooks"
    assert webhook_service.url_hint(None) is None


def test_webhook_url_must_be_http() -> None:
    assert clean_webhook_url("  https://doi-tac.vn/hooks ") == "https://doi-tac.vn/hooks"
    assert clean_webhook_url("") is None
    for bad in ("ftp://doi-tac.vn", "doi-tac.vn/hooks", "https://", "https://a b.vn"):
        with pytest.raises(WebhookConfigError):
            clean_webhook_url(bad)
