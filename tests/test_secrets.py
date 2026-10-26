"""Test redaksi rahasia."""
from __future__ import annotations

from shortsgen.util.secrets import SecretRedactor, mask


def test_mask_short_and_long() -> None:
    assert mask(None) == "<none>"
    assert mask("abc") == "*** (len=3)"
    masked = mask("sk-abcdefghijklmnop")
    assert masked.startswith("sk-")
    assert "abcdefghij" not in masked


def test_redactor_registered_secret() -> None:
    secret = "sk-verysecretvalue1234567890"
    redactor = SecretRedactor([secret])
    text = f"Authorization: Bearer {secret} gagal"
    redacted = redactor.redact(text)
    assert secret not in redacted
    assert "***REDACTED***" in redacted


def test_redactor_generic_patterns() -> None:
    redactor = SecretRedactor()
    samples = [
        "api_key=sk-abcdefghijklmnopqrst",
        "AIzaSyA1234567890abcdefghijklmnop",
        "Bearer tokenabcdefghijklmnopqrstuv",
        "ya29.a0AX07Cmvr1Ep5sq6JlNh06_MYVPjdJgNMMlE0dFqBYmQkeBw",
    ]
    for sample in samples:
        out = redactor.redact(sample)
        assert "***REDACTED***" in out, out


def test_redactor_nested_objects() -> None:
    secret = "sk-nestedsecret12345678"
    redactor = SecretRedactor([secret])
    payload = {"headers": {"auth": secret}, "list": [secret, "aman"], "n": 5}
    out = redactor.redact_obj(payload)
    assert secret not in str(out)
    assert out["list"][1] == "aman"
    assert out["n"] == 5


def test_redactor_ignores_short_secrets() -> None:
    redactor = SecretRedactor(["abc"])
    assert redactor.redact("abc") == "abc"  # terlalu pendek -> tidak dianggap rahasia
