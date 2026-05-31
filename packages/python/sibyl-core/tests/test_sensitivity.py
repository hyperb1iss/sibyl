from __future__ import annotations

import pytest

from sibyl_core.models.sources import SourcePrivacyClass, SourceRecord
from sibyl_core.services.sensitivity import classify_record


def _record(body: str, *, metadata: dict[str, object] | None = None) -> SourceRecord:
    return SourceRecord(
        adapter_record_id="record-1",
        source_id="source-record-1",
        source_type="fixture",
        title="Fixture",
        body=body,
        content_hash="hash-1",
        dedupe_key="dedupe-1",
        privacy_class=SourcePrivacyClass.PERSONAL,
        metadata=metadata or {},
    )


def test_classify_record_detects_pii_floor() -> None:
    result = classify_record(_record("Customer SSN 123-45-6789 and card 4111 1111 1111 1111"))

    assert result.contains_pii is True
    assert result.contains_secret is False
    assert set(result.sensitivity_flags) == {"ssn", "payment_card"}


def test_classify_record_detects_secret_floor() -> None:
    result = classify_record(
        _record(
            "Rotate AWS key AKIAIOSFODNN7EXAMPLE and verification code is 123456",
            metadata={"auth_token": "sk-abcDEF1234567890abcDEF"},
        )
    )

    assert result.contains_pii is False
    assert result.contains_secret is True
    assert "api_key" in result.sensitivity_flags
    assert "two_factor_code" in result.sensitivity_flags
    assert result.privacy_class is SourcePrivacyClass.SENSITIVE


def test_classify_record_detects_secret_metadata() -> None:
    result = classify_record(
        _record(
            "",
            metadata={
                "password": "correcthorsebatterystaple",
                "security": {"otp": 123456},
            },
        )
    )

    assert result.contains_secret is True
    assert "api_key" in result.sensitivity_flags
    assert "two_factor_code" in result.sensitivity_flags


def test_classify_record_detects_high_entropy_token() -> None:
    result = classify_record(_record("token aB3dE5gH7jK9mN2pQ4rS6tV8wX0yZ1kL2"))

    assert result.contains_secret is True
    assert result.sensitivity_flags == ("high_entropy_token",)


def test_classify_record_ignores_low_entropy_numbers() -> None:
    result = classify_record(_record("Order number 1111111111111111"))

    assert result.contains_sensitive is False
    assert result.sensitivity_flags == ()


def test_classify_record_ignores_high_entropy_paths() -> None:
    result = classify_record(
        _record(
            "",
            metadata={
                "source_path": "/private/var/folders/l8/wnmp8hs10rbbwl_cfyscrjbw0000gn/archive.mbox"
            },
        )
    )

    assert result.contains_sensitive is False
    assert result.sensitivity_flags == ()


@pytest.mark.parametrize(
    "body",
    [
        "Security 2024 roadmap",
        "login 2024",
        "error code 123456",
        "error code: 123456",
        "verification status 123456",
    ],
)
def test_classify_record_requires_explicit_two_factor_context(body: str) -> None:
    result = classify_record(_record(body))

    assert result.contains_secret is False
    assert "two_factor_code" not in result.sensitivity_flags
