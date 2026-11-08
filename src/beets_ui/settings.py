"""Environment-driven settings. Everything is configured through env vars."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


class SettingsError(RuntimeError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from e


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


DEFAULT_COVER_SOURCES = (
    "coverartarchive,fanarttv,theaudiodb,deezer,itunes,discogs,vocadb,touhoudb,utaitedb"
)
DEFAULT_ARTIST_SOURCES = "fanarttv,theaudiodb,deezer,discogs,vocadb,touhoudb,utaitedb"


@dataclass
class Settings:
    inbox: Path
    data_dir: Path
    beetsdir: Path
    auto_preview: bool
    scan_interval: int
    debounce: int
    delete_after_import: bool

    password: str
    secret_key: str
    ldap_enabled: bool
    ldap_url: str
    ldap_bind_dn: str
    ldap_bind_password: str
    ldap_base_dn: str
    ldap_user_filter: str
    ldap_start_tls: bool

    slskd_url: str
    slskd_api_key: str

    cover_sources: list[str] = field(default_factory=list)
    artist_sources: list[str] = field(default_factory=list)
    artwork_overwrite: bool = False
    min_cover_px: int = 500
    artist_image_names: list[str] = field(default_factory=lambda: ["folder.jpg"])
    fanarttv_api_key: str = ""
    theaudiodb_api_key: str = "2"
    discogs_token: str = ""

    @property
    def slskd_enabled(self) -> bool:
        return bool(self.slskd_url and self.slskd_api_key)

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(_str("BEETSUI_DATA", "/config/beets-ui"))
        s = cls(
            inbox=Path(_str("BEETSUI_INBOX", "/inbox")),
            data_dir=data_dir,
            beetsdir=Path(_str("BEETSDIR", "/config/beets")),
            auto_preview=_bool("BEETSUI_AUTO_PREVIEW", True),
            scan_interval=max(5, _int("BEETSUI_SCAN_INTERVAL", 30)),
            debounce=max(0, _int("BEETSUI_DEBOUNCE", 30)),
            delete_after_import=_bool("BEETSUI_DELETE_AFTER_IMPORT", False),
            password=os.environ.get("BEETSUI_PASSWORD", ""),
            secret_key=_str("BEETSUI_SECRET_KEY"),
            ldap_enabled=_bool("BEETSUI_LDAP_ENABLED", False),
            ldap_url=_str("LDAP_URL"),
            ldap_bind_dn=_str("LDAP_BIND_DN"),
            ldap_bind_password=os.environ.get("LDAP_BIND_PASSWORD", ""),
            ldap_base_dn=_str("LDAP_BASE_DN"),
            ldap_user_filter=_str("LDAP_USER_FILTER", "(uid={username})"),
            ldap_start_tls=_bool("LDAP_START_TLS", False),
            slskd_url=_str("SLSKD_URL").rstrip("/"),
            slskd_api_key=_str("SLSKD_API_KEY"),
            cover_sources=_list("BEETSUI_COVER_SOURCES", DEFAULT_COVER_SOURCES),
            artist_sources=_list("BEETSUI_ARTIST_SOURCES", DEFAULT_ARTIST_SOURCES),
            artwork_overwrite=_bool("BEETSUI_ARTWORK_OVERWRITE", False),
            min_cover_px=_int("BEETSUI_MIN_COVER_PX", 500),
            artist_image_names=_list("BEETSUI_ARTIST_IMAGE_NAME", "folder.jpg"),
            fanarttv_api_key=_str("FANARTTV_API_KEY"),
            theaudiodb_api_key=_str("THEAUDIODB_API_KEY", "2") or "2",
            discogs_token=_str("DISCOGS_TOKEN"),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.ldap_enabled:
            missing = [
                n
                for n, v in (("LDAP_URL", self.ldap_url), ("LDAP_BASE_DN", self.ldap_base_dn))
                if not v
            ]
            if missing:
                raise SettingsError(f"LDAP is enabled but {', '.join(missing)} is not set")
            if "{username}" not in self.ldap_user_filter:
                raise SettingsError("LDAP_USER_FILTER must contain '{username}'")
        elif not self.password:
            raise SettingsError(
                "BEETSUI_PASSWORD must be set (or enable LDAP with BEETSUI_LDAP_ENABLED=true)"
            )

    def ensure_secret_key(self) -> str:
        """Return the session secret, generating and persisting one if needed."""
        if self.secret_key:
            return self.secret_key
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "secret_key"
        if path.exists():
            self.secret_key = path.read_text().strip()
        else:
            self.secret_key = secrets.token_urlsafe(48)
            path.write_text(self.secret_key)
            path.chmod(0o600)
        return self.secret_key
