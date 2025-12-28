from sibyl.auth.invitations import generate_invite_token


def test_generate_invite_token() -> None:
    t1 = generate_invite_token()
    t2 = generate_invite_token()
    assert isinstance(t1, str)
    assert len(t1) >= 32
    assert t1 != t2
