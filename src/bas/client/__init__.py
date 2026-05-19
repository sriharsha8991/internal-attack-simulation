"""BAS Platform client — split by concern.

Public surface:
    from bas.client import BasClient, BasClientError

Internal modules:
    transport       HTTP wiring (httpx + bearer + raise-on-error + pause)
    dry_run         deterministic UUIDs and canned fixtures for offline runs
    errors          BasClientError
    auth            POST /auth/login
    environments    /environments
    agents          /environments/{id}/agents
    payloads        /payloads
    abilities       /abilities + /abilities/{id}/stages
    adversaries     /adversaries + /adversaries/{aid}/abilities/{abid}
    facade          BasClient — composes the resource clients
"""

from .errors import BasClientError
from .facade import BasClient

__all__ = ["BasClient", "BasClientError"]
