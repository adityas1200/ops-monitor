"""Unit tests for Snowflake key-pair helpers."""
from app.connectors.snowflake_connector import (
    _normalize_pem_text,
    sanitize_snowflake_account,
)


def test_normalize_pem_literal_newlines():
    raw = "-----BEGIN ENCRYPTED PRIVATE KEY-----\\nABC\\n-----END ENCRYPTED PRIVATE KEY-----"
    out = _normalize_pem_text(raw)
    assert "\\n" not in out
    assert out.splitlines()[0] == "-----BEGIN ENCRYPTED PRIVATE KEY-----"
    assert out.splitlines()[-1] == "-----END ENCRYPTED PRIVATE KEY-----"


def test_sanitize_account_from_url():
    assert (
        sanitize_snowflake_account("https://bayer_cphcdp_nonprod.us-east-1.snowflakecomputing.com")
        == "bayer_cphcdp_nonprod.us-east-1"
    )
    assert sanitize_snowflake_account("bay-phcphamericasnonprod") == "bay-phcphamericasnonprod"
