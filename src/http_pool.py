"""Shared pooled requests.Session factory for the scrapers.

With thousands of boards scraped concurrently, per-call requests.get() pays a
fresh TCP+TLS handshake every time; a pooled session reuses connections across
the thread pool (urllib3's pool is thread-safe).

Cookies are refused: every job-board API used here is public, and one shared
session collecting cookies from ~1,700 Workday tenants turned each request
into a copy of a jar of thousands of cookies (measured 2026-10-01: most of a
run's CPU, quadratic in boards scraped).
"""

from __future__ import annotations

from http import cookiejar

import requests


class _NoCookies(cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def make_session(headers: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(headers)
    session.cookies.set_policy(_NoCookies())
    # pool_connections = hosts kept warm; above the board count, so a host's
    # connection isn't evicted (and re-handshaken) between its requests.
    adapter = requests.adapters.HTTPAdapter(pool_connections=2048, pool_maxsize=64)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
