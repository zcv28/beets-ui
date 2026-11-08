"""Login: a single shared password, or LDAP search-then-bind."""

from __future__ import annotations

import hmac
import logging
import threading
import time

from .settings import Settings

log = logging.getLogger("beets_ui.auth")


class AuthError(Exception):
    pass


class RateLimiter:
    """Slow down brute force: max `limit` failures per key per window."""

    def __init__(self, limit: int = 10, window: float = 300.0):
        self.limit = limit
        self.window = window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            fails = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = fails
            return len(fails) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


def check_password(settings: Settings, password: str) -> bool:
    return bool(settings.password) and hmac.compare_digest(
        password.encode(), settings.password.encode()
    )


def ldap_authenticate(settings: Settings, username: str, password: str, server_factory=None):
    """Return the user's display name if the credentials are valid, else None.

    Search for the user with the service account (or anonymously when no bind
    DN is configured), then bind as the user's DN with their password.
    """
    import ldap3
    from ldap3.utils.conv import escape_filter_chars

    if not username or not password:
        return None
    make_server = server_factory or (lambda: ldap3.Server(settings.ldap_url, get_info=ldap3.NONE))
    server = make_server()
    filt = settings.ldap_user_filter.replace("{username}", escape_filter_chars(username))
    try:
        svc = ldap3.Connection(
            server,
            user=settings.ldap_bind_dn or None,
            password=settings.ldap_bind_password or None,
            auto_bind=False,
            raise_exceptions=False,
            **_strategy_kwargs(server_factory),
        )
        if settings.ldap_start_tls and server_factory is None:
            svc.open()
            svc.start_tls()
        if not svc.bind():
            log.error("LDAP service bind failed: %s", svc.result)
            raise AuthError("LDAP service bind failed")
        svc.search(settings.ldap_base_dn, filt, attributes=["cn", "displayName"], size_limit=2)
        entries = list(svc.entries)
        svc.unbind()
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001
        log.error("LDAP error: %s", e)
        raise AuthError(f"LDAP error: {e}") from e
    if len(entries) != 1:
        return None
    entry = entries[0]
    user_dn = entry.entry_dn
    conn = ldap3.Connection(
        server,
        user=user_dn,
        password=password,
        raise_exceptions=False,
        **_strategy_kwargs(server_factory),
    )
    if settings.ldap_start_tls and server_factory is None:
        conn.open()
        conn.start_tls()
    ok = conn.bind()
    conn.unbind()
    if not ok:
        return None
    for attr in ("displayName", "cn"):
        if attr in entry and entry[attr].value:
            return str(entry[attr].value)
    return username


def _strategy_kwargs(server_factory) -> dict:
    if server_factory is None:
        return {}
    import ldap3

    return {"client_strategy": ldap3.MOCK_SYNC}


def authenticate(settings: Settings, username: str, password: str, server_factory=None):
    """Return the session user name, or None on bad credentials."""
    if settings.ldap_enabled:
        return ldap_authenticate(settings, username.strip(), password, server_factory)
    if check_password(settings, password):
        return username.strip() or "admin"
    return None
