from sibyl.auth.organizations import slugify


def test_slugify() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("  ") == "org"
    assert slugify("A___B") == "a-b"
