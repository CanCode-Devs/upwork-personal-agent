from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.models import McpCatalogDrift, McpCatalogFile, McpDriftCounts, McpToolChange, McpToolSignature

CATALOG_FILENAME = "upwork_mcp_catalog.json"
_VOLATILE_KEYS = frozenset({"trace_id", "traceId"})


def catalog_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / CATALOG_FILENAME


def _drop_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _drop_volatile(item) for key, item in value.items() if key not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_drop_volatile(item) for item in value]
    return value


def _canonical(value: Any) -> Any:
    cleaned = _drop_volatile(value)
    if isinstance(cleaned, dict):
        return {key: _canonical(cleaned[key]) for key in sorted(cleaned)}
    if isinstance(cleaned, list):
        return [_canonical(item) for item in cleaned]
    return cleaned


def _schema_dict(raw: Any) -> dict[str, Any]:
    schema = raw
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    if not isinstance(schema, dict):
        return {}
    canonical = _canonical(schema)
    return canonical if isinstance(canonical, dict) else {}


def _enum_values(schema: dict[str, Any], key: str) -> set[str]:
    props = schema.get("properties")
    if not isinstance(props, dict):
        return set()
    node = props.get(key)
    if not isinstance(node, dict):
        return set()
    raw = node.get("enum")
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item is not None}


def _schema_notes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    before_props = before.get("properties") if isinstance(before.get("properties"), dict) else {}
    after_props = after.get("properties") if isinstance(after.get("properties"), dict) else {}
    added = sorted(set(after_props) - set(before_props))
    removed = sorted(set(before_props) - set(after_props))
    if added:
        notes.append(f"properties added: {', '.join(added)}")
    if removed:
        notes.append(f"properties removed: {', '.join(removed)}")
    before_actions = _enum_values(before, "action")
    after_actions = _enum_values(after, "action")
    action_added = sorted(after_actions - before_actions)
    action_removed = sorted(before_actions - after_actions)
    if action_added:
        notes.append(f"action added: {', '.join(action_added)}")
    if action_removed:
        notes.append(f"action removed: {', '.join(action_removed)}")
    if before != after and not notes:
        notes.append("input schema updated")
    return notes


def signature_from_tool(tool: Any) -> McpToolSignature:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    return McpToolSignature(
        name=str(getattr(tool, "name", "") or "").strip(),
        description=str(getattr(tool, "description", "") or "").strip(),
        input_schema=_schema_dict(schema),
    )


def signatures_from_tools(tools: list[Any]) -> list[McpToolSignature]:
    found = [signature_from_tool(tool) for tool in tools]
    return [item for item in found if item.name]


def load_catalog(settings: Settings | None = None) -> McpCatalogFile:
    path = catalog_path(settings)
    if not path.exists():
        return McpCatalogFile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return McpCatalogFile()
    if not isinstance(raw, dict):
        return McpCatalogFile()
    try:
        return McpCatalogFile.model_validate(raw)
    except ValidationError:
        return McpCatalogFile()


def save_catalog(store: McpCatalogFile, settings: Settings | None = None) -> None:
    path = catalog_path(settings)
    path.write_text(store.model_dump_json(indent=2), encoding="utf-8")


def compare_catalog(accepted: list[McpToolSignature], live: list[McpToolSignature]) -> McpCatalogDrift:
    old = {item.name: item for item in accepted if item.name}
    new = {item.name: item for item in live if item.name}
    added = [new[name] for name in sorted(set(new) - set(old))]
    removed = [old[name] for name in sorted(set(old) - set(new))]
    changed: list[McpToolChange] = []
    for name in sorted(set(old) & set(new)):
        before = old[name]
        after = new[name]
        if before.description == after.description and before.input_schema == after.input_schema:
            continue
        notes: list[str] = []
        if before.description != after.description:
            notes.append("description updated")
        notes.extend(_schema_notes(before.input_schema, after.input_schema))
        changed.append(
            McpToolChange(
                name=name,
                before=before,
                after=after,
                notes=notes or ["updated"],
            )
        )
    return McpCatalogDrift(
        checked_at=datetime.now(UTC),
        added=added,
        removed=removed,
        changed=changed,
    )


def sync_catalog_after_connect(live: list[McpToolSignature], settings: Settings | None = None) -> McpCatalogFile:
    store = load_catalog(settings)
    now = datetime.now(UTC)
    ordered = sorted(live, key=lambda item: item.name)
    if not store.tools:
        store.tools = ordered
        store.accepted_at = now
        store.pending = None
        save_catalog(store, settings)
        return store
    drift = compare_catalog(store.tools, ordered)
    store.pending = drift if drift.has_changes() else None
    save_catalog(store, settings)
    return store


def accept_pending(settings: Settings | None = None) -> McpCatalogFile:
    store = load_catalog(settings)
    pending = store.pending
    if pending is None or not pending.has_changes():
        return store
    by_name = {item.name: item for item in store.tools}
    for item in pending.removed:
        by_name.pop(item.name, None)
    for item in pending.added:
        by_name[item.name] = item
    for change in pending.changed:
        by_name[change.name] = change.after
    store.tools = sorted(by_name.values(), key=lambda item: item.name)
    store.accepted_at = datetime.now(UTC)
    store.pending = None
    save_catalog(store, settings)
    return store


def pending_drift_counts(settings: Settings | None = None) -> McpDriftCounts | None:
    store = load_catalog(settings)
    pending = store.pending
    if pending is None or not pending.has_changes():
        return None
    return McpDriftCounts(
        added=len(pending.added),
        removed=len(pending.removed),
        changed=len(pending.changed),
    )
