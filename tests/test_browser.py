"""Browser URL policy: the browser must not bypass the filesystem sandbox."""

from __future__ import annotations

import pytest

from executor.browser import normalise_url


def test_http_and_bare_hosts_are_allowed():
    assert normalise_url("https://example.com/a") == "https://example.com/a"
    assert normalise_url("http://example.com") == "http://example.com"
    assert normalise_url("example.com/path") == "https://example.com/path"
    assert normalise_url("about:blank") == "about:blank"


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "FILE:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "ftp://example.com/file",
        "mailto:someone@example.com",
    ],
)
def test_dangerous_schemes_are_rejected(url):
    with pytest.raises(ValueError):
        normalise_url(url)
