from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


CONFIG_FILE_ENV = "CAMADMIRAL_CONFIG_FILE"
DEFAULT_CONFIG_FILE = Path("/etc/camadmiral/config.yaml")
MAX_CONFIG_BYTES = 256 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class ConfigurationError(RuntimeError):
    pass


class SecretConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerSettings:
    listen: str = "0.0.0.0"
    port: int = 18080


@dataclass(frozen=True)
class StorageSettings:
    database: Path = Path("/var/lib/camadmiral/camadmiral.db")

    @property
    def inventory(self) -> Path:
        return self.database.parent / "inventory.json"


@dataclass(frozen=True)
class SecretSettings:
    master_key_file: Path = Path("/run/secrets/camadmiral_master_key")
    api_token_file: Path = Path("/run/secrets/camadmiral_api_token")
    admin_password_file: Path = Path("/run/secrets/camadmiral_admin_password")
    master_key_file_explicit: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True)
class FrigateTargetSettings:
    target_id: str
    name: str
    api_url: str
    sync_cameras: bool = False


@dataclass(frozen=True)
class FrigateIntegrationSettings:
    targets: tuple[FrigateTargetSettings, ...] = ()
    default_target: str | None = None


@dataclass(frozen=True)
class IntegrationSettings:
    frigate: FrigateIntegrationSettings = field(default_factory=FrigateIntegrationSettings)


@dataclass(frozen=True)
class CamAdmiralSettings:
    version: int = 1
    server: ServerSettings = field(default_factory=ServerSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    secrets: SecretSettings = field(default_factory=SecretSettings)
    integrations: IntegrationSettings = field(default_factory=IntegrationSettings)


def _mapping(value: object, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{context} must be a mapping")
    return value


def _only(mapping: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown {context} setting: {unknown[0]}")


def _absolute_path(value: object, default: Path, context: str) -> Path:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{context} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(f"{context} must be an absolute path")
    return path


def _loopback_http_url(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("Frigate api_url must be a string")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("Frigate api_url must be a plain loopback HTTP URL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        raise ConfigurationError("Frigate api_url must include a loopback host and port")
    if parsed.path not in {"", "/"}:
        raise ConfigurationError("Frigate api_url must not include a path")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{parsed.port}"


def _parse_server(root: dict[str, Any]) -> ServerSettings:
    section = _mapping(root.get("server"), "server")
    _only(section, {"listen", "port"}, "server")
    listen = section.get("listen", "0.0.0.0")
    port = section.get("port", 18080)
    if not isinstance(listen, str) or not listen.strip() or len(listen) > 255:
        raise ConfigurationError("server.listen is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigurationError("server.port must be between 1 and 65535")
    return ServerSettings(listen.strip(), port)


def _parse_storage(root: dict[str, Any]) -> StorageSettings:
    section = _mapping(root.get("storage"), "storage")
    _only(section, {"database"}, "storage")
    defaults = StorageSettings()
    return StorageSettings(
        _absolute_path(section.get("database"), defaults.database, "storage.database")
    )


def _parse_secrets(root: dict[str, Any]) -> SecretSettings:
    section = _mapping(root.get("secrets"), "secrets")
    _only(section, {"master_key_file", "api_token_file", "admin_password_file"}, "secrets")
    defaults = SecretSettings()
    return SecretSettings(
        master_key_file=_absolute_path(
            section.get("master_key_file"),
            defaults.master_key_file,
            "secrets.master_key_file",
        ),
        api_token_file=_absolute_path(
            section.get("api_token_file"),
            defaults.api_token_file,
            "secrets.api_token_file",
        ),
        admin_password_file=_absolute_path(
            section.get("admin_password_file"),
            defaults.admin_password_file,
            "secrets.admin_password_file",
        ),
        master_key_file_explicit="master_key_file" in section,
    )


def _parse_frigate(section: dict[str, Any]) -> FrigateIntegrationSettings:
    _only(section, {"targets", "default_target"}, "integrations.frigate")
    raw_targets = section.get("targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > 8:
        raise ConfigurationError("Frigate targets must be a list with at most eight entries")
    targets: list[FrigateTargetSettings] = []
    target_ids: set[str] = set()
    for raw_target in raw_targets:
        target = _mapping(raw_target, "Frigate target")
        _only(target, {"id", "name", "api_url", "sync_cameras"}, "Frigate target")
        target_id = target.get("id")
        name = target.get("name")
        sync_cameras = target.get("sync_cameras", False)
        if not isinstance(target_id, str) or not SAFE_ID.fullmatch(target_id):
            raise ConfigurationError("Invalid Frigate target id")
        if target_id in target_ids:
            raise ConfigurationError("Duplicate Frigate target id")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ConfigurationError("Invalid Frigate target name")
        if not isinstance(sync_cameras, bool):
            raise ConfigurationError("Frigate sync_cameras must be true or false")
        target_ids.add(target_id)
        targets.append(
            FrigateTargetSettings(
                target_id,
                name.strip(),
                _loopback_http_url(target.get("api_url")),
                sync_cameras,
            )
        )
    default_target = section.get("default_target")
    if default_target is None and len(targets) == 1:
        default_target = targets[0].target_id
    if default_target is not None and (
        not isinstance(default_target, str) or default_target not in target_ids
    ):
        raise ConfigurationError("integrations.frigate.default_target is invalid")
    if len(targets) > 1 and default_target is None:
        raise ConfigurationError("Multiple Frigate targets require default_target")
    return FrigateIntegrationSettings(tuple(targets), default_target)


def _parse_integrations(root: dict[str, Any]) -> IntegrationSettings:
    section = _mapping(root.get("integrations"), "integrations")
    _only(section, {"frigate"}, "integrations")
    frigate = _parse_frigate(_mapping(section.get("frigate"), "integrations.frigate"))
    return IntegrationSettings(frigate)


def parse_settings(payload: object) -> CamAdmiralSettings:
    if payload is None:
        return CamAdmiralSettings()
    root = _mapping(payload, "configuration")
    if not root:
        return CamAdmiralSettings()
    _only(root, {"version", "server", "storage", "secrets", "integrations"}, "top-level")
    if root.get("version") != 1:
        raise ConfigurationError("Configuration version must be 1")
    return CamAdmiralSettings(
        version=1,
        server=_parse_server(root),
        storage=_parse_storage(root),
        secrets=_parse_secrets(root),
        integrations=_parse_integrations(root),
    )


@lru_cache(maxsize=1)
def settings() -> CamAdmiralSettings:
    configured = os.environ.get(CONFIG_FILE_ENV, "").strip()
    explicit = bool(configured)
    path = Path(configured) if explicit else DEFAULT_CONFIG_FILE
    if not path.exists():
        if explicit:
            raise ConfigurationError("Configured CamAdmiral config file does not exist")
        return CamAdmiralSettings()
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigurationError("CamAdmiral config file is too large")
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError("Cannot read CamAdmiral config file") from exc
    return parse_settings(payload)


def reset_settings_cache() -> None:
    settings.cache_clear()


def read_secret_file(path: Path, *, required: bool = False) -> bytes | None:
    try:
        value = path.read_bytes().strip()
    except FileNotFoundError as exc:
        if not required:
            return None
        raise SecretConfigurationError("Configured secret file does not exist") from exc
    except OSError as exc:
        raise SecretConfigurationError("Cannot read configured secret file") from exc
    if not value:
        raise SecretConfigurationError("Configured secret file is empty")
    return value


def database_path() -> Path:
    return settings().storage.database
