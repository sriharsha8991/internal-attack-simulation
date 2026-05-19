"""Payloads resource — read-only in M1.

Payload binaries live on the backend disk; M1 only browses the metadata index.
Upload (POST /payloads multipart) lands in M2.
"""

from __future__ import annotations

from ..models import PayloadMetadata
from .transport import HttpTransport


class PayloadsApi:
    def __init__(self, transport: HttpTransport, *, dry_run: bool = False) -> None:
        self._t = transport
        self._dry = dry_run

    def list(self) -> list[PayloadMetadata]:
        if self._dry:
            return []
        data = self._t.get_json("/payloads")
        return [PayloadMetadata.model_validate(x) for x in self._t.unwrap_list(data)]
