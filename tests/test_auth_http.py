from sibyl.auth.http import extract_bearer_token, select_access_token


def test_extract_bearer_token() -> None:
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token("Basic abc") is None
    assert extract_bearer_token("Bearer") is None
    assert extract_bearer_token("Bearer ") is None
    assert extract_bearer_token("Bearer abc") == "abc"
    assert extract_bearer_token("bearer   abc  ") == "abc"


def test_select_access_token() -> None:
    assert select_access_token(authorization=None, cookie_token=None) is None
    assert select_access_token(authorization="Bearer a", cookie_token="b") == "a"
    assert select_access_token(authorization=None, cookie_token="  b ") == "b"
