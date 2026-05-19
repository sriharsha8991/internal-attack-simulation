"""Error type raised by every resource client when the BAS Platform answers >= 400."""

from __future__ import annotations


class BasClientError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body
