from sibyl.auth.api_keys import api_key_prefix, hash_api_key, verify_api_key


def test_api_key_hash_roundtrip() -> None:
    salt, h = hash_api_key("sk_live_test", iterations=1_000)
    assert verify_api_key("sk_live_test", salt_hex=salt, hash_hex=h, iterations=1_000) is True
    assert verify_api_key("sk_live_nope", salt_hex=salt, hash_hex=h, iterations=1_000) is False


def test_api_key_prefix() -> None:
    assert api_key_prefix("abc", length=2) == "ab"
    assert api_key_prefix("abc", length=999) == "abc"
