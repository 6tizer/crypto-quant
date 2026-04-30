"""Verify proxy_url has no f-string double-brace bug."""

from config.settings import Settings


def test_proxy_url_exact_value():
    settings = Settings()
    assert settings.proxy_url == "http://127.0.0.1:7897"


def test_proxy_url_starts_with_http():
    settings = Settings()
    assert settings.proxy_url.startswith("http://")


def test_proxy_url_no_curly_braces():
    settings = Settings()
    assert "{" not in settings.proxy_url
    assert "}" not in settings.proxy_url
