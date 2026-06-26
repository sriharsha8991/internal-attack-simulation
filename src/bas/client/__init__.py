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
    feedback        POST /ai/operation-feedback
    facade          BasClient — composes the resource clients
"""

from .errors import BasClientError
from .facade import BasClient
from .kali import KaliClient, KaliError

__all__ = ["BasClient", "BasClientError", "KaliClient", "KaliError"]
