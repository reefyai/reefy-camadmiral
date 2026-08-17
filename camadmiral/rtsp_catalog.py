from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).with_name("rtsp_catalog.json")
MAX_RULES = 32
MAX_PATHS_PER_RULE = 4
MAX_PROBE_LIMIT = 12


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogCandidate:
    label: str
    uri: str
    rule_id: str
    catalog_revision: str
    source_url: str


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CatalogError("RTSP compatibility catalog could not be loaded") from exc
    revision = catalog.get("revision")
    rules = catalog.get("rules")
    probe_limit = catalog.get("probe_limit")
    if not isinstance(revision, str) or not revision.strip():
        raise CatalogError("RTSP compatibility catalog has no revision")
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise CatalogError("RTSP compatibility catalog has an invalid rule count")
    if not isinstance(probe_limit, int) or not 1 <= probe_limit <= MAX_PROBE_LIMIT:
        raise CatalogError("RTSP compatibility catalog has an invalid probe limit")
    seen_rules: set[str] = set()
    for rule in rules:
        rule_id = rule.get("id")
        aliases = rule.get("manufacturer_aliases")
        source_url = rule.get("source_url")
        paths = rule.get("paths")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_rules:
            raise CatalogError("RTSP compatibility catalog has a duplicate or invalid rule ID")
        seen_rules.add(rule_id)
        if not isinstance(aliases, list) or not all(isinstance(value, str) and value for value in aliases):
            raise CatalogError(f"RTSP catalog rule {rule_id} has invalid aliases")
        parsed_source = urllib.parse.urlsplit(str(source_url or ""))
        if parsed_source.scheme != "https" or not parsed_source.hostname:
            raise CatalogError(f"RTSP catalog rule {rule_id} has invalid provenance")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS_PER_RULE:
            raise CatalogError(f"RTSP catalog rule {rule_id} has an invalid path count")
        for entry in paths:
            path_value = entry.get("path")
            if (
                not isinstance(entry.get("label"), str)
                or not isinstance(path_value, str)
                or not path_value.startswith("/")
                or "@" in path_value
                or "#" in path_value
            ):
                raise CatalogError(f"RTSP catalog rule {rule_id} has an invalid path")
    return catalog


def _candidate_hints(candidate: dict[str, Any]) -> str:
    onvif = candidate.get("onvif") or {}
    values = [
        candidate.get("display_name"),
        onvif.get("name"),
        onvif.get("model"),
        *(onvif.get("scopes") or []),
    ]
    return " ".join(str(value).lower() for value in values if value)


def catalog_candidates(
    candidate: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> list[CatalogCandidate]:
    catalog = catalog or load_catalog()
    hints = _candidate_hints(candidate)
    rules = list(catalog["rules"])
    matching = [
        rule
        for rule in rules
        if any(alias.lower() in hints for alias in rule["manufacturer_aliases"])
    ]
    ordered = matching + [rule for rule in rules if rule not in matching]
    rtsp_endpoints = candidate.get("rtsp") or []
    port = next(
        (int(endpoint["port"]) for endpoint in rtsp_endpoints if endpoint.get("port")),
        554,
    )
    host = str(candidate.get("ip") or "")
    if not host:
        return []
    netloc = f"[{host}]" if ":" in host else host
    if port != 554:
        netloc = f"{netloc}:{port}"
    resolved: list[CatalogCandidate] = []
    seen_uris: set[str] = set()
    for rule in ordered:
        for entry in rule["paths"]:
            parsed = urllib.parse.urlsplit(entry["path"])
            uri = urllib.parse.urlunsplit(("rtsp", netloc, parsed.path, parsed.query, ""))
            if uri in seen_uris:
                continue
            seen_uris.add(uri)
            resolved.append(
                CatalogCandidate(
                    label=str(entry["label"]),
                    uri=uri,
                    rule_id=str(rule["id"]),
                    catalog_revision=str(catalog["revision"]),
                    source_url=str(rule["source_url"]),
                )
            )
            if len(resolved) >= int(catalog["probe_limit"]):
                return resolved
    return resolved
