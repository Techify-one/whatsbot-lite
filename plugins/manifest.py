"""Plugin manifest parsing and validation.

Each plugin ships a ``plugin.yaml`` (or ``plugin.json``) at the root of its
folder. To avoid an extra dependency, we hand-write a tiny YAML reader for the
restricted subset we actually use (top-level scalars, lists, nested mappings up
to two levels). If ``pyyaml`` is installed it is preferred.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 1.3.0: ADDITIVE — ``frontend_extends`` in the manifest: a plugin may ship an ES
# module (under its own ``/plugins/<id>/`` static path) that the panel imports once
# at boot. Its default export receives ``{pluginId, apiBase, addSlot}`` and can fill
# named UI slots (today: ``chat.header.actions``, in the conversation header). A
# plugin that needs it declares ``">=1.3,<2.0"``; everything else is unaffected.
#
# 1.2.0: ADDITIVE — public plugin runtime helpers
# ``send_whatsapp_message`` and ``get_plugin_setting``. Plugins can now send
# through the active GOWA client and read their declarative settings without
# importing core internals.
#
# 1.1.0: ADDITIVE — the provisioning seams ``filter.provisioning.number`` and
# ``filter.provisioning.message`` (both ``str``), applied by
# ``server/routes/setup.fetch_provision_target``. They come as a symmetric pair
# because the phrase IS the trigger the destination recognizes: a plugin that
# points the send at another number without being able to swap the message
# delivers a text the other side silently ignores. ``None``/``""`` on either one
# aborts and the wizard refuses to send. A plugin that needs them declares
# ``">=1.1,<2.0"``; everything else stays on ``">=1.0,<2.0"``.
WHATSBOT_API_VERSION = "1.3.0"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclasses.dataclass
class PluginManifest:
    """In-memory representation of a parsed plugin manifest."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    whatsbot_api_version: str = "*"
    entry: dict[str, str] = dataclasses.field(default_factory=dict)
    migrations: str | None = None
    screens: list[dict] = dataclasses.field(default_factory=list)
    permissions: list[str] = dataclasses.field(default_factory=list)
    dependencies: list[str] = dataclasses.field(default_factory=list)
    # Documentation-only declarations. The loader does not enforce that the
    # plugin actually exports a handler for every event/filter listed here;
    # they exist so ``/api/plugins/manifest`` can show which surface the plugin
    # touches.
    events: list[str] = dataclasses.field(default_factory=list)
    filters: list[str] = dataclasses.field(default_factory=list)
    # ES module the panel imports at boot to fill UI slots (API >= 1.3).
    frontend_extends: str = ""
    raw: dict = dataclasses.field(default_factory=dict)

    def to_public_dict(self) -> dict:
        """Serializable view exposed by ``/api/plugins`` endpoints."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "whatsbot_api_version": self.whatsbot_api_version,
            "screens": self.screens,
            "permissions": self.permissions,
            "dependencies": self.dependencies,
            "events": self.events,
            "filters": self.filters,
            "frontend_extends": self.frontend_extends,
        }


def find_manifest_file(plugin_dir: Path) -> Path | None:
    for name in ("plugin.yaml", "plugin.yml", "plugin.json"):
        path = plugin_dir / name
        if path.is_file():
            return path
    return None


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """Parse the manifest file inside ``plugin_dir``.

    Raises ``ValueError`` on parse error or missing required fields.
    """
    path = find_manifest_file(plugin_dir)
    if path is None:
        raise ValueError(f"manifest not found in {plugin_dir} (expected plugin.yaml or plugin.json)")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        data = _parse_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return _build_manifest(data, plugin_dir)


def _build_manifest(data: dict, plugin_dir: Path) -> PluginManifest:
    pid = data.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        raise ValueError(
            "manifest 'id' must be snake_case starting with a letter, "
            "32 chars max (got %r)" % pid
        )
    if pid != plugin_dir.name:
        raise ValueError(
            f"manifest id '{pid}' must match plugin folder name '{plugin_dir.name}'"
        )

    name = data.get("name") or pid
    version = str(data.get("version") or "0.0.0")
    if not _is_semver(version):
        raise ValueError(f"manifest 'version' must be semver (got {version!r})")

    api_range = str(data.get("whatsbot_api_version") or "*")
    if not check_api_compat(api_range):
        raise ValueError(
            f"plugin {pid} requires WhatsBot-Lite API {api_range}, "
            f"running {WHATSBOT_API_VERSION}"
        )

    entry = data.get("entry") or {}
    if not isinstance(entry, dict):
        raise ValueError("manifest 'entry' must be a mapping")
    entry_str = {k: str(v) for k, v in entry.items() if isinstance(v, str)}

    migrations = data.get("migrations")
    migrations_str = str(migrations) if migrations else None

    screens = data.get("screens") or []
    if not isinstance(screens, list):
        raise ValueError("manifest 'screens' must be a list")
    cleaned_screens = []
    for s in screens:
        if not isinstance(s, dict):
            continue
        if not s.get("path") or not s.get("component"):
            logger.warning("plugin %s: screen missing path/component, skipped: %s", pid, s)
            continue
        cleaned_screens.append({
            "id": str(s.get("id") or s["path"].lstrip("/")),
            "title": str(s.get("title") or s["path"]),
            "path": str(s["path"]),
            "icon": str(s.get("icon") or ""),
            "component": str(s["component"]),
            # When true, the screen is the plugin's configuration UI: shown in the
            # Plugins tab "Configurar" modal instead of as a standalone gear-menu page.
            "config": bool(s.get("config", False)),
        })

    frontend_extends = data.get("frontend_extends") or ""
    if frontend_extends:
        # Same rule as screen components: a plugin may only load code from its own
        # static folder, never an arbitrary URL.
        expected_prefix = f"/plugins/{pid}/static/"
        if not isinstance(frontend_extends, str) or not frontend_extends.startswith(expected_prefix):
            raise ValueError(
                f"manifest 'frontend_extends' must start with {expected_prefix} "
                f"(got {frontend_extends!r})"
            )

    permissions = [str(p) for p in (data.get("permissions") or []) if isinstance(p, str)]
    deps = [str(d) for d in (data.get("dependencies") or []) if isinstance(d, str)]
    events_declared = [str(e) for e in (data.get("events") or []) if isinstance(e, str)]
    filters_declared = [str(f) for f in (data.get("filters") or []) if isinstance(f, str)]

    return PluginManifest(
        id=pid,
        name=str(name),
        version=version,
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        whatsbot_api_version=api_range,
        entry=entry_str,
        migrations=migrations_str,
        screens=cleaned_screens,
        permissions=permissions,
        dependencies=deps,
        events=events_declared,
        filters=filters_declared,
        frontend_extends=str(frontend_extends),
        raw=data,
    )


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+].*)?$")


def _is_semver(value: str) -> bool:
    return bool(_SEMVER_RE.match(value))


def _parse_simple_semver(value: str) -> tuple[int, int, int]:
    """Parse ``MAJOR.MINOR.PATCH`` ignoring prerelease/build."""
    core = re.split(r"[-+]", value, maxsplit=1)[0]
    parts = core.split(".")
    if len(parts) < 3:
        parts += ["0"] * (3 - len(parts))
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 0, 0, 0


def check_api_compat(spec: str, current: str = WHATSBOT_API_VERSION) -> bool:
    """Check whether ``current`` satisfies the constraint expression ``spec``.

    Supports ``*``, plain version (``1.0.0``), and comma-separated comparators
    ``>=, <=, >, <, ==, !=`` such as ``">=1.0,<2.0"``.
    """
    spec = (spec or "*").strip()
    if spec in ("", "*"):
        return True
    cur = _parse_simple_semver(current)
    # plain version → exact match on MAJOR.MINOR.PATCH
    if _is_semver(spec):
        return cur == _parse_simple_semver(spec)
    parts = [p.strip() for p in spec.split(",")]
    for part in parts:
        m = re.match(r"^(>=|<=|>|<|==|!=)\s*(\d+(?:\.\d+){0,2}(?:[-+].*)?)$", part)
        if not m:
            logger.warning("Unrecognized version constraint: %r", part)
            return False
        op, ver = m.group(1), m.group(2)
        target = _parse_simple_semver(ver)
        if op == ">=" and not cur >= target: return False
        if op == "<=" and not cur <= target: return False
        if op == ">"  and not cur >  target: return False
        if op == "<"  and not cur <  target: return False
        if op == "==" and not cur == target: return False
        if op == "!=" and not cur != target: return False
    return True


# ---------------------------------------------------------------------------
# Tiny YAML subset parser (avoids hard pyyaml dependency).
# Supports: mappings (key: value), nested mappings via indentation, lists with
# ``-`` items (scalars or inline mappings), plain scalars (string/int/float/
# bool/null), comments with ``#``, double/single quoted strings.
# Does NOT support: anchors, multi-line strings (``|`` / ``>``), flow sequences
# ``[a, b]`` or flow mappings ``{a: 1}`` mid-line, complex types.
# ---------------------------------------------------------------------------


def _parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    return _parse_yaml_fallback(text)


def _parse_yaml_fallback(text: str) -> Any:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # strip inline comments only when not inside a quoted string (cheap)
        if "#" in line:
            in_str = False
            quote = ""
            for i, ch in enumerate(line):
                if ch in ('"', "'"):
                    if not in_str:
                        in_str = True
                        quote = ch
                    elif quote == ch:
                        in_str = False
                elif ch == "#" and not in_str:
                    line = line[:i].rstrip()
                    break
        lines.append(line)
    if not lines:
        return None
    pos = [0]

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse_block(indent: int) -> Any:
        items = None  # decided lazily: list or dict
        while pos[0] < len(lines):
            line = lines[pos[0]]
            cur_indent = indent_of(line)
            if cur_indent < indent:
                return items
            stripped = line[indent:]
            if stripped.startswith("- "):
                if items is None:
                    items = []
                pos[0] += 1
                value_part = stripped[2:].strip()
                if not value_part:
                    items.append(parse_block(indent + 2))
                elif ":" in value_part and not (value_part.startswith('"') or value_part.startswith("'")):
                    # inline mapping start — treat as nested mapping
                    key, _, rest = value_part.partition(":")
                    rest = rest.strip()
                    nested: dict = {}
                    if rest:
                        nested[key.strip()] = _scalar(rest)
                    extra = parse_block(indent + 2)
                    if isinstance(extra, dict):
                        nested.update(extra)
                    items.append(nested)
                else:
                    items.append(_scalar(value_part))
            elif ":" in stripped:
                if items is None:
                    items = {}
                key, _, rest = stripped.partition(":")
                key = key.strip()
                rest = rest.strip()
                pos[0] += 1
                if rest:
                    items[key] = _scalar(rest)
                else:
                    # nested block: indent of the next non-empty line
                    if pos[0] < len(lines) and indent_of(lines[pos[0]]) > cur_indent:
                        items[key] = parse_block(indent_of(lines[pos[0]]))
                    else:
                        items[key] = None
            else:
                # bare scalar at this level — unusual; skip
                pos[0] += 1
        return items

    return parse_block(0)


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    low = text.lower()
    if low == "true": return True
    if low == "false": return False
    if low in ("null", "~", ""): return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text
