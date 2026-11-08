from __future__ import annotations

import ldap3
import pytest

from beets_ui.auth import RateLimiter, authenticate
from beets_ui.console import ConsoleError, parse
from beets_ui.settings import Settings, SettingsError


def _settings(monkeypatch, **env):
    for k in ("BEETSUI_PASSWORD", "BEETSUI_LDAP_ENABLED", "LDAP_URL", "LDAP_BASE_DN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def test_password_required_without_ldap(monkeypatch):
    with pytest.raises(SettingsError):
        _settings(monkeypatch)
    s = _settings(monkeypatch, BEETSUI_PASSWORD="pw")
    assert authenticate(s, "", "pw") == "admin"
    assert authenticate(s, "", "bad") is None


def test_ldap_settings_validation(monkeypatch):
    with pytest.raises(SettingsError):
        _settings(monkeypatch, BEETSUI_LDAP_ENABLED="true")


def test_ldap_search_then_bind(monkeypatch):
    s = _settings(
        monkeypatch,
        BEETSUI_LDAP_ENABLED="true",
        LDAP_URL="ldap://fake",
        LDAP_BASE_DN="ou=people,dc=example,dc=org",
        LDAP_BIND_DN="cn=svc,dc=example,dc=org",
        LDAP_BIND_PASSWORD="svcpw",
        LDAP_USER_FILTER="(&(uid={username})(memberOf=cn=music,ou=groups,dc=example,dc=org))",
    )
    server = ldap3.Server("fake")
    setup = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    setup.strategy.add_entry("cn=svc,dc=example,dc=org", {"userPassword": "svcpw"})
    setup.strategy.add_entry(
        "uid=niki,ou=people,dc=example,dc=org",
        {
            "uid": "niki",
            "userPassword": "hunter2",
            "cn": "Niki",
            "memberOf": "cn=music,ou=groups,dc=example,dc=org",
            "objectClass": "person",
        },
    )
    setup.strategy.add_entry(
        "uid=other,ou=people,dc=example,dc=org",
        {"uid": "other", "userPassword": "pw", "cn": "Other", "objectClass": "person"},
    )
    factory = lambda: server  # noqa: E731
    assert authenticate(s, "niki", "hunter2", factory) == "Niki"
    assert authenticate(s, "niki", "wrong", factory) is None
    assert authenticate(s, "other", "pw", factory) is None  # not in the group
    assert authenticate(s, "*", "x", factory) is None  # filter injection is escaped


def test_rate_limiter():
    rl = RateLimiter(limit=2, window=60)
    assert not rl.blocked("ip")
    rl.fail("ip")
    rl.fail("ip")
    assert rl.blocked("ip")
    rl.reset("ip")
    assert not rl.blocked("ip")


def test_console_parse():
    assert parse("beet ls -a 'albumartist:Foo Bar'") == ["ls", "-a", "albumartist:Foo Bar"]
    assert parse("update") == ["update"]
    for bad in ("", "beet", "config -e", "edit", "ls 'unterminated"):
        with pytest.raises(ConsoleError):
            parse(bad)
