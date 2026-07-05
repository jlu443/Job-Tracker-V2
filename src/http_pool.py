"""Shared pooled requests.Session factory for the scrapers.

With thousands of boards scraped concurrently, per-call requests.get() pays a
fresh TCP+TLS handshake every time; a pooled session reuses connections across
the thread pool (urllib3's pool is thread-safe).
"""

from __future__ import annotations

import requests


def make_session(headers: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)
    adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
