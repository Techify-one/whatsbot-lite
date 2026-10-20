"""Built-in Chat: WhatsBot-Lite help and persistent plugin development projects."""

from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import io
import importlib.util
import inspect
import json
import logging
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import File, Request, UploadFile
from fastapi.responses import StreamingResponse
from agno.models.message import Message

from agent.costing import current_model_pricing, number
from agent.plugin_chat import build_agent, history_messages, metrics_dict
from db.repositories import chat_repo, plugin_repo
from plugins.manifest import load_manifest
from plugins.dep_check import undeclared_dependencies
from plugins.migrator import _validate_sql_prefix, run_pending_migrations
from plugins.restart import schedule_restart
from server.helpers import _err, _ok
from server.routes.usage import _get_model_pricing_details

logger = logging.getLogger(__name__)

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_MESSAGE_CHARS = 50_000
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_AUTO_COMPACT_CHARS = 60_000
_CHAT_RUN_TIMEOUT_SECONDS = 30 * 60
_CHAT_HEARTBEAT_SECONDS = 15
_STREAM_HEARTBEAT = object()
_SCAFFOLD_MARKER = ".whatsbot-scaffold"
_active_agents: dict[str, object] = {}
_active_conversation_runs: dict[str, dict] = {}
_background_chat_tasks: set[asyncio.Task] = set()
_project_locks: dict[str, asyncio.Lock] = {}
_PLUGIN_INTENT_RE = re.compile(
    r"(?:\b(?:criar|fazer|montar|desenvolver|alterar|editar|atualizar|modificar)\b.{0,100}\bplugin\b)"
    r"|(?:\bplugin\b.{0,100}\b(?:criar|fazer|montar|desenvolver|alterar|editar|atualizar|modificar)\b)",
    re.IGNORECASE | re.DOTALL,
)
_INSTALL_CONFIRM_RE = re.compile(
    r"^\s*(?:sim[,.!]?\s*)?(?:por\s+favor[,.]?\s*)?(?:pode\s+)?(?:instale|instalar|instala|faça\s+a\s+instalação|"
    r"quero\s+instalar|atualize|atualizar|atualiza)(?:\s+(?:o\s+)?plugin)?(?:\s+por\s+favor)?[.!]?\s*$",
    re.IGNORECASE,
)


def _public_base_url(request: Request) -> str:
    """Build links using the host and port through which the user connected."""
    headers = getattr(request, "headers", {}) or {}
    request_url = getattr(request, "url", None)
    forwarded_host = headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    forwarded_proto = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    host = forwarded_host or headers.get("host", "").strip() or getattr(request_url, "netloc", "localhost:8080")
    scheme = forwarded_proto or getattr(request_url, "scheme", "http")
    return f"{scheme}://{host}".rstrip("/")


_HELP_LINK_RULES = (
    (re.compile(r"religar (?:a )?ia|reativar (?:a )?ia|ia (?:volt|ligar|religa)\w* (?:sozinha|autom[áa]tic)|ligar (?:a )?ia (?:de novo|automatic)", re.I), "/painel?aba=agente#ai-reactivation", "religar a IA automaticamente"),
    (re.compile(r"prompt|instru(?:ç|c)ões? do agente|personalidade|comportamento da ia", re.I), "/painel?aba=agente#prompt", "instruções do agente"),
    (re.compile(r"apagar (?:arquivo|foto|v[íi]deo|m[íi]dia)|liberar espa(?:ç|c)o|espa(?:ç|c)o (?:em disco|usado)|gerenciador de arquivos|armazenamento", re.I), "/arquivos", "arquivos e espaço usado"),
    (re.compile(r"download autom[áa]tico|baixar (?:automaticamente|m[íi]dia|foto|v[íi]deo)|n[ãa]o baixar|guardar (?:foto|v[íi]deo|arquivo)", re.I), "/painel?aba=modelos-midia#download-midia", "download automático de mídia"),
    (re.compile(r"transcri(?:ç|c)[ãa]o?.*(?:document|pdf)|document.*transcri|ler documento", re.I), "/painel?aba=modelos-midia#document-transcription", "leitura de documentos"),
    (re.compile(r"transcri(?:ç|c)[ãa]o?.*(?:[áa]udio|voz)|[áa]udio|microfone", re.I), "/painel?aba=modelos-midia#audio-transcription", "transcrição de áudio"),
    (re.compile(r"imagem|foto", re.I), "/painel?aba=modelos-midia#image-transcription", "descrição de imagens"),
    (re.compile(r"modelo|model", re.I), "/painel?aba=modelos-midia#model", "modelo de IA"),
    (re.compile(r"chave|api", re.I), "/painel?aba=modelos-midia#api-key", "chave de API"),
    (re.compile(r"grupo|grupos", re.I), "/painel?aba=agente#groups", "respostas em grupos"),
    (re.compile(r"senha|password", re.I), "/painel?aba=sistema#password", "senha do painel"),
    (re.compile(r"banco|postgres|sqlite|dados", re.I), "/painel?aba=sistema#database", "banco de dados"),
    (re.compile(r"proxy.*(?:whatsapp|gowa)|(?:whatsapp|gowa).*proxy", re.I), "/painel?aba=sistema#gowa-proxy", "proxy do WhatsApp"),
    (re.compile(r"gowa|motor do whatsapp", re.I), "/painel?aba=sistema#gowa-version", "GOWA"),
    (re.compile(r"atualiza(?:r|ção)|vers[ãa]o nova", re.I), "/painel?aba=sistema#update", "atualizações"),
    (re.compile(r"custo|gasto|cr[ée]dito|quanto.*pago", re.I), "/costs", "custos de IA"),
    (re.compile(r"execuç(?:ão|ões)|execucao|execucoes", re.I), "/executions", "execuções"),
    (re.compile(r"ferramenta|tools", re.I), "/tools", "ferramentas"),
    (re.compile(r"plugin|instal(?:ar|ação)", re.I), "/plugins", "plugins instalados"),
    (re.compile(r"resposta autom[áa]tica|ativar a ia|agente responder", re.I), "/painel?aba=agente#auto-reply", "respostas automáticas"),
    (re.compile(r"contexto|hist[óo]rico", re.I), "/painel?aba=agente#context", "mensagens de contexto"),
    (re.compile(r"agrupar|juntar mensagens", re.I), "/painel?aba=agente#batch", "agrupamento de mensagens"),
    (re.compile(r"dividir|picad|separar mensagens", re.I), "/painel?aba=agente#split-messages", "divisão das respostas"),
)


def _ensure_system_help_link(user_content: str, assistant_text: str, base_url: str) -> str:
    """Add the most specific known panel link if the model omitted it."""
    if not assistant_text or not base_url:
        return assistant_text
    for pattern, path, label in _HELP_LINK_RULES:
        if not pattern.search(user_content or ""):
            continue
        target = f"{base_url.rstrip('/')}{path}"
        if target in assistant_text:
            return assistant_text
        return f"{assistant_text.rstrip()}\n\n[Abrir {label}]({target})"
    return assistant_text


def _is_plugin_intent(content: str) -> bool:
    return bool(_PLUGIN_INTENT_RE.search(content or ""))


def _is_install_confirmation(content: str) -> bool:
    """Recognize an explicit request to install/update the current plugin."""
    return bool(_INSTALL_CONFIRM_RE.search((content or "").strip()))


def _pending_install_offer(messages: list[dict]) -> dict | None:
    """Return the newest unresolved install offer in a conversation."""
    for message in reversed(messages):
        if message.get("kind") != "install_offer":
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("status", "pending") == "pending":
            return message
    return None


def _format_discovery_response(content: str) -> str:
    """Keep end-user discovery questions readable even if a model flattens them."""
    text = html_lib.unescape(str(content or "")).strip()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if text.count("?") > 1:
        text = re.sub(r"\?\s+(?=\S)", "?\n\n", text)
    return text


_number = number
_current_model_pricing = current_model_pricing

def _add_cost_estimate(
    metrics: dict, model_id: str, pricing: dict, at: datetime | None = None,
) -> dict:
    """Add a price snapshot and estimated USD cost to one model run."""
    result = dict(metrics or {})
    if not pricing:
        result.update({"model": model_id, "cost_estimate_unavailable": True})
        return result
    active = _current_model_pricing(pricing, at)
    prompt_price = _number(active.get("prompt"))
    completion_price = _number(active.get("completion"))
    cache_read_price = _number(active.get("input_cache_read")) or prompt_price
    cache_write_price = _number(active.get("input_cache_write")) or prompt_price
    input_tokens = int(result.get("input_tokens", 0) or 0)
    output_tokens = int(result.get("output_tokens", 0) or 0)
    cache_read_tokens = int(result.get("cache_read_tokens", 0) or 0)
    cache_write_tokens = int(result.get("cache_write_tokens", 0) or 0)
    regular_input_tokens = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)
    cost = (
        regular_input_tokens * prompt_price
        + cache_read_tokens * cache_read_price
        + cache_write_tokens * cache_write_price
        + output_tokens * completion_price
    )
    result.update({
        "model": model_id,
        "estimated_cost_usd": cost,
        "cost_is_estimate": True,
        "pricing": {
            "prompt": prompt_price,
            "completion": completion_price,
            "input_cache_read": cache_read_price,
            "input_cache_write": cache_write_price,
        },
    })
    return result


def _add_conversation_cost_totals(messages: list[dict], model_id: str, pricing: dict) -> list[dict]:
    """Enrich legacy metric rows and add a running conversation total."""
    total = 0.0
    enriched = []
    for message in messages:
        item = dict(message)
        if item.get("kind") == "metrics":
            metadata = dict(item.get("metadata") or {})
            if "estimated_cost_usd" not in metadata:
                created_at = item.get("created_at")
                at = datetime.fromtimestamp(created_at, timezone.utc) if created_at else None
                metadata = _add_cost_estimate(
                    metadata, metadata.get("model") or model_id, pricing, at,
                )
            total += _number(metadata.get("estimated_cost_usd"))
            metadata["conversation_estimated_cost_usd"] = total
            item["metadata"] = metadata
        enriched.append(item)
    return enriched


async def _with_timeout(
    iterator, seconds: float = _CHAT_RUN_TIMEOUT_SECONDS,
    heartbeat_seconds: float = _CHAT_HEARTBEAT_SECONDS,
):
    """Yield run events plus heartbeats without cancelling a slow upstream read."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    async_iterator = iterator.__aiter__()
    next_event = None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"a execução excedeu o limite de {int(seconds // 60)} minutos"
                )
            if next_event is None:
                next_event = asyncio.create_task(anext(async_iterator))
            done, _ = await asyncio.wait(
                {next_event}, timeout=min(heartbeat_seconds, remaining),
            )
            if not done:
                yield _STREAM_HEARTBEAT
                continue
            try:
                item = next_event.result()
            except StopAsyncIteration:
                return
            next_event = None
            yield item
    finally:
        if next_event is not None and not next_event.done():
            next_event.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await next_event


def _slug(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    if not value or not value[0].isalpha():
        value = "plugin_" + value
    return value[:32].rstrip("_") or "meu_plugin"


def _scaffold_plugin_workspace(workspace: Path, plugin_id: str, name: str) -> None:
    """Create a deterministic starting point for inexpensive creator models.

    Requiring the model to invent the first tool call made some otherwise
    capable models stop after acknowledging the task. The marker keeps this
    starter from ever validating as a finished plugin.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    manifest = workspace / "plugin.yaml"
    init_file = workspace / "__init__.py"
    marker = workspace / _SCAFFOLD_MARKER
    if not manifest.exists():
        manifest.write_text(
            "\n".join((
                f"id: {plugin_id}",
                f"name: {json.dumps(name, ensure_ascii=False)}",
                "version: 1.0.0",
                'whatsbot_api_version: \">=1.2,<2.0\"',
                "description: Projeto em construção pelo Criador de Plugins.",
                "author: WhatsBot-Lite",
                "entry: {}",
                "permissions: []",
                "dependencies: []",
                "",
            )),
            encoding="utf-8",
        )
    if not init_file.exists():
        init_file.write_text('"""Plugin em construção."""\n', encoding="utf-8")
    marker.write_text(
        "Estrutura inicial criada pelo WhatsBot-Lite. Implemente o pedido completo, "
        "atualize o manifesto e apague este arquivo antes de validar.\n",
        encoding="utf-8",
    )


def _ndjson(event: str, data=None) -> bytes:
    return (json.dumps({"event": event, "data": data or {}}, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _merge_metrics(total: dict, current: dict) -> dict:
    """Accumulate all model rounds into the one usage row shown to users."""
    result = dict(total or {})
    for key in (
        "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens",
    ):
        result[key] = int(result.get(key, 0) or 0) + int((current or {}).get(key, 0) or 0)
    return result


def _project_workspace(project: dict) -> Path | None:
    raw = project.get("workspace_path") or ""
    return Path(raw).resolve() if raw else None


def _safe_workspace_file(project: dict, relative: str) -> Path:
    root = _project_workspace(project)
    if root is None:
        raise ValueError("este projeto não possui arquivos")
    target = (root / (relative or "")).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("caminho fora do projeto") from exc
    return target


def _zip_workspace(workspace: Path, destination: Path | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or any(part in {"__pycache__", ".pytest_cache", ".git"} for part in path.parts):
                continue
            if path.suffix == ".pyc":
                continue
            archive.write(path, path.relative_to(workspace).as_posix())
    payload = buffer.getvalue()
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    return payload


def _run_simple_tests_in_process(workspace: Path, tests: list[Path]) -> tuple[int, str]:
    """Small zero-dependency runner for plain ``test_*`` functions.

    It keeps testing available in an existing installation that has not yet
    installed the newly declared pytest dependency. Full pytest remains the
    preferred runner whenever it is available.
    """
    stdout, stderr = io.StringIO(), io.StringIO()
    failures = 0
    previous_cwd = Path.cwd()
    inserted = False
    try:
        os.chdir(workspace)
        if str(workspace) not in sys.path:
            sys.path.insert(0, str(workspace))
            inserted = True
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for test_file in tests:
                try:
                    namespace = runpy.run_path(str(test_file), run_name=f"plugin_test_{test_file.stem}")
                    found = 0
                    for name, function in namespace.items():
                        if not name.startswith("test_") or not callable(function):
                            continue
                        found += 1
                        if inspect.signature(function).parameters:
                            raise RuntimeError(f"{test_file.name}:{name} requer fixtures; instale pytest")
                        function()
                        print(f"PASS {test_file.name}:{name}")
                    if not found:
                        print(f"SKIP {test_file.name}: nenhuma função test_* sem framework")
                except Exception as exc:
                    failures += 1
                    print(f"FAIL {test_file.name}: {type(exc).__name__}: {exc}")
    finally:
        if inserted and str(workspace) in sys.path:
            sys.path.remove(str(workspace))
        os.chdir(previous_cwd)
    return failures, (stdout.getvalue() + stderr.getvalue())[-6000:]


_SIMPLE_TEST_RUNNER_CODE = r"""
import contextlib
import inspect
import io
import json
import os
import runpy
import sys
import traceback
from pathlib import Path

from db import init_db
from db.repositories import plugin_repo
from plugins.manifest import load_manifest
from plugins.migrator import run_pending_migrations

workspace = Path(sys.argv[1]).resolve()
database = Path(sys.argv[2]).resolve()
tests = [Path(value).resolve() for value in sys.argv[3:]]
init_db(database)
manifest = load_manifest(workspace)
plugin_repo.upsert(manifest.id, manifest.version, enabled=True)
run_pending_migrations(manifest, workspace)
sys.path.insert(0, str(workspace))

stdout, stderr = io.StringIO(), io.StringIO()
failures = 0
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    for test_file in tests:
        try:
            namespace = runpy.run_path(str(test_file), run_name=f"plugin_test_{test_file.stem}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test_file.name}: import: {type(exc).__name__}: {exc}")
            print(traceback.format_exc(limit=3))
            continue
        found = 0
        for name, function in namespace.items():
            if not name.startswith("test_") or not callable(function):
                continue
            found += 1
            try:
                if inspect.signature(function).parameters:
                    raise RuntimeError("requer fixtures; remova os parâmetros ou declare pytest")
                function()
                print(f"PASS {test_file.name}:{name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {test_file.name}:{name}: {type(exc).__name__}: {exc}")
                print(traceback.format_exc(limit=3))
        if not found:
            print(f"SKIP {test_file.name}: nenhuma função test_* sem framework")

captured = (stdout.getvalue() + stderr.getvalue())[-10000:]
print(captured, end="")
print(json.dumps({"failures": failures}, ensure_ascii=False))
"""


def _run_simple_tests(workspace: Path, tests: list[Path]) -> tuple[int, str]:
    """Run framework-free plugin tests in a disposable process and database."""
    if getattr(sys, "frozen", False):
        return _run_simple_tests_in_process(workspace, tests)
    project_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="whatsbot-plugin-tests-") as temporary:
        database = Path(temporary) / "plugin-tests.db"
        environment = os.environ.copy()
        environment["DATABASE_URL"] = f"sqlite:///{database}"
        python_path = [str(project_root), str(workspace)]
        if environment.get("PYTHONPATH"):
            python_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
        result = subprocess.run(
            [
                sys.executable, "-c", _SIMPLE_TEST_RUNNER_CODE,
                str(workspace), str(database), *(str(path) for path in tests),
            ],
            cwd=str(project_root), env=environment,
            capture_output=True, text=True, timeout=180,
        )
    output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))[-10000:]
    if result.returncode:
        return 1, output
    json_lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
    if not json_lines:
        return 1, output + "\nFAIL executor de testes não devolveu resultado estruturado"
    return int(json.loads(json_lines[-1]).get("failures") or 0), output


_PLUGIN_RUNTIME_VALIDATION_CODE = r"""
import json
import sys
from pathlib import Path

from db import init_db
from db.repositories import plugin_repo
from plugins.loader import _load_plugin_module
from plugins.manifest import load_manifest
from plugins.migrator import run_pending_migrations

workspace = Path(sys.argv[1]).resolve()
database = Path(sys.argv[2]).resolve()
init_db(database)
manifest = load_manifest(workspace)
plugin_repo.upsert(manifest.id, manifest.version, enabled=True)
applied = run_pending_migrations(manifest, workspace)
loaded = _load_plugin_module(manifest, workspace)

checks = {
    "tools": len(loaded.tools),
    "prompts": len(loaded.prompt_fragments),
    "events": len(loaded.event_handlers),
    "filters": len(loaded.filters),
    "routes": loaded.router is not None,
    "settings": loaded.settings_cls is not None,
}
for entry_name in manifest.entry:
    if entry_name in ("tools", "prompts", "routes", "settings") and not checks[entry_name]:
        raise RuntimeError(f"entry {entry_name!r} foi declarado, mas não exporta o contrato esperado")
print(json.dumps({"ok": True, "checks": checks, "migrations_applied": applied}))
"""


def _validate_plugin_runtime(workspace: Path) -> dict:
    """Load entries and apply migrations in an isolated Python process/DB."""
    if getattr(sys, "frozen", False):
        # Frozen builds need a dedicated executable worker mode. Keep the
        # existing in-process syntax/tests path there until that package entry
        # point is available; never relaunch the GUI executable with ``-c``.
        return {"skipped": True, "reason": "runtime isolado indisponível nesta distribuição"}
    project_root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="whatsbot-plugin-validation-") as temporary:
        database = Path(temporary) / "plugin-validation.db"
        environment = os.environ.copy()
        # Match the real loader: the plugin is imported as
        # ``whatsbot_plugins.<id>`` and its directory is not a top-level
        # import root. This catches accidental ``from settings import ...``
        # imports that must instead be package-relative (``from .settings``).
        python_path = [str(project_root)]
        if environment.get("PYTHONPATH"):
            python_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
        environment["DATABASE_URL"] = f"sqlite:///{database}"
        result = subprocess.run(
            [sys.executable, "-c", _PLUGIN_RUNTIME_VALIDATION_CODE, str(workspace), str(database)],
            cwd=str(project_root), env=environment, capture_output=True, text=True, timeout=120,
        )
        output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))[-8000:]
        if result.returncode:
            raise ValueError("plugin não carregou no runtime isolado:\n" + output)
        json_lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
        if not json_lines:
            raise ValueError("runtime isolado não devolveu resultado estruturado:\n" + output)
        return json.loads(json_lines[-1])


def _validate_frontend_quality(component_path: Path) -> dict:
    """Check the minimum UI contract that can be verified without a browser.

    A static check cannot judge taste, but it can prevent the recurring output
    failures that prompted this contract: narrow generic wrappers, unreadable
    form controls, missing responsive behavior and incomplete request states.
    The error text is intentionally actionable because it is fed back to the
    creator model during an automatic repair round.
    """
    source = component_path.read_text(encoding="utf-8", errors="replace")
    compact = re.sub(r"\s+", " ", source)
    issues: list[str] = []

    has_fetch = bool(re.search(r"\bfetch\s*\(", source))
    has_fields = bool(re.search(r"<(?:input|textarea|select)\b", source, re.I))
    has_responsive = bool(
        re.search(r"@media\s*\(", source, re.I)
        or re.search(r"\b(?:sm|md|lg|xl|2xl):[A-Za-z]", source)
    )
    has_theme = bool(
        re.search(r"\b(?:bg|text|border)-wa-", source)
        or "var(--wa-" in source
        or re.search(r"html\.dark\b", source)
    )
    has_focus = bool(re.search(r"(?:focus-visible|:focus\b|\bfocus:)", source))
    has_full_width = bool(
        re.search(r"\b(?:w-full|min-w-0)\b", source)
        or re.search(r"width\s*:\s*100%", source)
    )
    has_heading = bool(re.search(r"<h[12]\b", source, re.I))
    has_accessible_fields = not has_fields or bool(
        re.search(r"<label\b", source, re.I)
        or re.search(r"aria-label\s*=", source, re.I)
    )
    has_loading_state = bool(re.search(r"\b(?:loading|carregando)\b", source, re.I))
    has_error_state = bool(re.search(r"\b(?:error|erro)\b", source, re.I))
    has_empty_state = bool(
        re.search(r"(?:length\s*===?\s*0|!\s*\w+\.length|Nenhum|Nenhuma|vazi[oa])", source, re.I)
    )

    def color_luminance(value: str) -> float:
        value = value.lstrip("#")
        if len(value) == 3:
            value = "".join(char * 2 for char in value)
        rgb = [int(value[index:index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4 for part in rgb]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(first: str, second: str) -> float:
        one, two = color_luminance(first), color_luminance(second)
        return (max(one, two) + 0.05) / (min(one, two) + 0.05)

    color_contrast_checked = False
    for theme_block in re.findall(r"(?:html\.dark[^\{]*|\.[\w-]*page)\{([^}]+)\}", source, re.I | re.S):
        properties = {
            name.lower(): value.lower()
            for name, value in re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})(?![0-9a-fA-F])", theme_block)
        }
        by_suffix = {
            suffix: next((value for name, value in properties.items() if name.endswith(suffix)), None)
            for suffix in ("ink", "muted", "surface", "line")
        }
        if by_suffix["surface"] and by_suffix["line"]:
            color_contrast_checked = True
            ratio = contrast(by_suffix["surface"], by_suffix["line"])
            if ratio < 3:
                issues.append(
                    f"a borda dos campos tem contraste {ratio:.1f}:1; aumente para pelo menos 3:1 "
                    "contra a superfície nos temas claro e escuro"
                )
        if by_suffix["surface"] and by_suffix["muted"]:
            color_contrast_checked = True
            ratio = contrast(by_suffix["surface"], by_suffix["muted"])
            if ratio < 4.5:
                issues.append(
                    f"o texto secundário tem contraste {ratio:.1f}:1; aumente para pelo menos 4.5:1 "
                    "contra a superfície nos temas claro e escuro"
                )

    has_repeated_textarea = bool(re.search(r"\.map\s*\(", source) and re.search(r"<textarea\b", source, re.I))
    has_compact_note_editor = bool(
        re.search(r"(?:editing|expanded|openNote|editingNote|noteState)", source, re.I)
    )
    if has_repeated_textarea and not has_compact_note_editor:
        issues.append(
            "não mantenha uma textarea aberta em cada registro; mostre a nota compacta e abra a edição "
            "sob demanda para preservar densidade no computador e no celular"
        )

    if re.search(r"max-w-(?:3xl|4xl|5xl)\s+mx-auto", compact):
        issues.append(
            "remova o limite genérico max-w-* da raiz; a tela do plugin deve usar a largura disponível "
            "e limitar apenas colunas internas quando a leitura exigir"
        )
    if not has_full_width:
        issues.append("declare largura completa na raiz (`width: 100%`, `w-full` ou `min-w-0`)")
    if not has_responsive:
        issues.append("adicione breakpoint responsivo (`@media` ou classes sm:/md:/lg:)")
    if not has_theme:
        issues.append("use cores semânticas wa-* ou variáveis próprias com equivalente `html.dark`")
    if not has_focus:
        issues.append("adicione foco visível para navegação por teclado (`:focus-visible` ou `focus:`)")
    if not has_heading:
        issues.append("inclua um título semântico h1 ou h2 para estabelecer a hierarquia da página")
    if has_fields and "wa-field" not in source:
        issues.append("aplique `wa-field` nos campos para garantir fundo, texto e placeholder legíveis")
    if not has_accessible_fields:
        issues.append("associe label ou aria-label aos campos do formulário")
    if has_fetch:
        if not has_loading_state:
            issues.append("represente o estado de carregamento da consulta")
        if not has_error_state:
            issues.append("represente o erro da consulta e permita nova tentativa")
        if not has_empty_state:
            issues.append("represente o estado vazio com orientação para a pessoa")

    if issues:
        relative = component_path.name
        details = "\n".join(f"- {item}" for item in issues)
        raise ValueError(
            f"qualidade visual insuficiente em {relative}. Corrija todos os itens:\n{details}\n"
            "Depois revise densidade, hierarquia, contraste claro/escuro e a tela em 1366px e 360px."
        )
    return {
        "component": component_path.name,
        "responsive": True,
        "theme": True,
        "full_width": True,
        "focus_visible": True,
        "form_contrast": not has_fields or "wa-field" in source,
        "color_contrast_checked": color_contrast_checked,
        "compact_repeated_forms": not has_repeated_textarea or has_compact_note_editor,
        "request_states": not has_fetch or (
            has_loading_state and has_error_state and has_empty_state
        ),
    }


def validate_workspace(project: dict, *, save_version: bool = True) -> dict:
    workspace = _project_workspace(project)
    if project.get("kind") != "plugin" or workspace is None:
        raise ValueError("o projeto de ajuda do WhatsBot-Lite não é instalável")
    if not workspace.is_dir():
        raise ValueError("pasta do projeto não encontrada")
    if (workspace / _SCAFFOLD_MARKER).is_file():
        raise ValueError(
            "o projeto ainda contém apenas a estrutura inicial. Não liste nem releia o esqueleto: "
            "a próxima ação deve ser write_file em plugin.yaml. Depois crie os arquivos do pedido, "
            "os testes e a tela solicitada, e apague .whatsbot-scaffold antes de validar"
        )

    manifest = load_manifest(workspace)
    expected_id = project.get("plugin_id")
    if expected_id and manifest.id != expected_id:
        raise ValueError(f"o manifest usa id '{manifest.id}', mas o projeto pertence a '{expected_id}'")
    if not (workspace / "__init__.py").is_file():
        raise ValueError("arquivo __init__.py ausente")

    for entry_name, module_name in manifest.entry.items():
        module_path = workspace / f"{module_name}.py"
        if not module_path.is_file():
            raise ValueError(f"entry {entry_name!r} aponta para arquivo ausente: {module_name}.py")
    raw_screens = manifest.raw.get("screens") or []
    if len(manifest.screens) != len(raw_screens):
        raise ValueError("uma ou mais telas do manifesto não possuem path/component válidos")
    frontend_checks = []
    for screen in manifest.screens:
        expected_prefix = f"/plugins/{manifest.id}/"
        component = screen["component"]
        if not component.startswith(expected_prefix):
            raise ValueError(
                f"componente da tela deve começar com {expected_prefix}: {component}"
            )
        component_path = workspace / component[len(expected_prefix):]
        if not component_path.is_file():
            raise ValueError(f"componente da tela não encontrado: {component_path.relative_to(workspace)}")
        frontend_checks.append(_validate_frontend_quality(component_path))

    for dependency in manifest.dependencies:
        if (
            len(dependency) > 200 or dependency.startswith("-")
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~].+)?", dependency)
        ):
            raise ValueError(f"dependência inválida ou insegura no manifest: {dependency!r}")
    undeclared = undeclared_dependencies(workspace, manifest.dependencies)
    if undeclared:
        details = ", ".join(f"{module} → {package}" for module, package in undeclared)
        raise ValueError(f"imports de terceiros não declarados em dependencies: {details}")

    checked_migrations = []
    if manifest.migrations:
        migration_dir = workspace / manifest.migrations
        if not migration_dir.is_dir():
            raise ValueError(f"pasta de migrations ausente: {manifest.migrations}")
        for sql_path in sorted(migration_dir.glob("*.sql")):
            sql = sql_path.read_text(encoding="utf-8")
            _validate_sql_prefix(sql, manifest.id, f"plugin_{manifest.id}_", sql_path.name)
            # Creator updates are deliberately additive so restoring the old
            # code remains possible after a failed upgrade.
            if re.search(r"\b(?:DROP\s+(?:TABLE|COLUMN|INDEX)|TRUNCATE\b|ALTER\s+TABLE.+\bRENAME\b)", sql, re.I | re.S):
                raise ValueError(f"{sql_path.name}: atualização destrutiva não permitida; use migration compatível e aditiva")
            checked_migrations.append(sql_path.name)

    # Compile source directly so syntax validation also works in PyInstaller
    # builds, where ``sys.executable -m compileall`` would relaunch WhatsBot-Lite.
    for source in workspace.rglob("*.py"):
        if "__pycache__" in source.parts:
            continue
        try:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        except (SyntaxError, UnicodeError) as exc:
            raise ValueError(f"falha de sintaxe em {source.relative_to(workspace)}: {exc}") from exc

    runtime_validation = _validate_plugin_runtime(workspace)

    tests = list(workspace.rglob("test_*.py"))
    test_output = "Nenhum teste automatizado encontrado."
    if tests:
        pytest_available = importlib.util.find_spec("pytest") is not None
        if getattr(sys, "frozen", False) and pytest_available:
            # The packaged executable already embeds Python. Run the bundled
            # pytest in-process instead of requiring Python/WSL/Git Bash.
            import pytest
            stdout, stderr = io.StringIO(), io.StringIO()
            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    return_code = pytest.main(["-q", str(workspace)])
            finally:
                os.chdir(previous_cwd)
            test_output = (stdout.getvalue() + stderr.getvalue())[-6000:]
            if return_code:
                raise ValueError("testes falharam:\n" + test_output)
        elif pytest_available:
            environment = os.environ.copy()
            project_root = Path(__file__).resolve().parents[2]
            python_path = [str(project_root), str(workspace)]
            if environment.get("PYTHONPATH"):
                python_path.append(environment["PYTHONPATH"])
            environment["PYTHONPATH"] = os.pathsep.join(python_path)
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"], cwd=str(workspace),
                env=environment, capture_output=True, text=True, timeout=180,
            )
            test_output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else ""))[-6000:]
            if result.returncode:
                raise ValueError("testes falharam:\n" + test_output)
        else:
            failures, test_output = _run_simple_tests(workspace, tests)
            if failures:
                raise ValueError("testes falharam:\n" + test_output)

    zip_path = None
    if save_version:
        creator_root = workspace.parent.parent
        version_dir = creator_root / "versions"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_path = version_dir / f"{manifest.id}-{manifest.version}-{stamp}.zip"
        _zip_workspace(workspace, zip_path)

    return {
        "valid": True,
        "plugin_id": manifest.id,
        "name": manifest.name,
        "version": manifest.version,
        "dependencies": manifest.dependencies,
        "dependencies_checked": True,
        "migrations": checked_migrations,
        "runtime": runtime_validation,
        "frontend": frontend_checks,
        "tests": len(tests),
        "test_output": test_output,
        "zip_path": str(zip_path) if zip_path else None,
        "installed": plugin_repo.get(manifest.id) is not None,
    }


async def _compact(conversation: dict, settings, *, automatic: bool = False) -> dict:
    rows = chat_repo.list_messages(conversation["id"])
    previous_through = conversation.get("compacted_through_id")
    message_rows = [
        r for r in rows
        if r["kind"] == "message" and r["role"] in ("user", "assistant")
        and (previous_through is None or r["id"] > previous_through)
    ]
    if len(message_rows) < 6:
        return {"compacted": False, "reason": "A conversa ainda é curta."}
    # Keep up to the latest four exchanges verbatim. Original rows are never
    # deleted, so users can still inspect the full transcript after compaction.
    keep = 8 if len(message_rows) > 8 else 4
    old = message_rows[:-keep]
    if not old:
        return {"compacted": False, "reason": "Não há histórico antigo para compactar."}
    transcript = "\n\n".join(f"{r['role'].upper()}: {r['content']}" for r in old)
    previous = conversation.get("summary") or ""
    prompt = (
        "Atualize o resumo técnico desta conversa. Preserve requisitos, decisões, arquivos, "
        "mudanças feitas, testes, erros ainda relevantes e próximos passos. Não invente.\n\n"
        f"RESUMO ANTERIOR:\n{previous or '(nenhum)'}\n\nHISTÓRICO:\n{transcript[:180000]}"
    )
    model_id = conversation.get("model") or settings.get("model", "deepseek/deepseek-v4.1-flash")
    agent = build_agent(
        api_key=settings.get("openrouter_api_key", ""), model_id=model_id,
        reasoning=conversation.get("reasoning") or "", project_kind="summary",
        workspace=None, project_root=settings.data_dir,
    )
    output = await asyncio.wait_for(agent.arun(prompt, stream=False), timeout=180)
    summary = str(getattr(output, "content", "") or "").strip()
    if not summary:
        raise RuntimeError("o modelo não retornou um resumo")
    through = old[-1]["id"]
    chat_repo.update_conversation(conversation["id"], summary=summary, compacted_through_id=through)
    return {"compacted": True, "automatic": automatic, "through_id": through, "summary": summary}


def _tool_payload(event) -> dict:
    tool = getattr(event, "tool", None)
    if tool is None:
        return {}
    return {
        "tool_call_id": str(
            getattr(tool, "tool_call_id", None)
            or getattr(event, "tool_call_id", None)
            or ""
        ),
        "name": getattr(tool, "tool_name", None) or "ferramenta",
        "args": getattr(tool, "tool_args", None) or {},
        "result": getattr(tool, "result", None),
        "error": bool(getattr(tool, "tool_call_error", False)),
    }


def _shared_project_context(project_id: str, current_conversation_id: str) -> str:
    """Carry essential decisions between separate conversations in a project."""
    parts: list[str] = []
    for conversation in chat_repo.list_conversations(project_id):
        if conversation["id"] == current_conversation_id:
            continue
        summary = (conversation.get("summary") or "").strip()
        if summary:
            parts.append(f"Conversa “{conversation['title']}”:\n{summary}")
            continue
        rows = [
            row for row in chat_repo.list_messages(conversation["id"])
            if row["kind"] == "message" and row["role"] in ("user", "assistant")
        ][-4:]
        if rows:
            excerpt = "\n".join(f"{row['role']}: {row['content'][:1200]}" for row in rows)
            parts.append(f"Trecho recente de “{conversation['title']}”:\n{excerpt}")
    if not parts:
        return ""
    return "Contexto essencial compartilhado por outras conversas deste projeto:\n\n" + "\n\n".join(parts[-6:])


def register_routes(app, deps):
    creator_root = deps.settings.data_dir / "storages" / "plugin_creator"
    projects_root = creator_root / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    chat_repo.ensure_system_project()

    async def _install_plugin_project(project: dict, conversation_id: str = "") -> dict:
        """Validate and install a project after an explicit user confirmation."""
        try:
            validation = await asyncio.to_thread(validate_workspace, project)
        except Exception as exc:
            raise ValueError(f"plugin não passou na validação: {exc}") from exc

        workspace = _project_workspace(project)
        plugin_id = validation["plugin_id"]
        target = deps.plugins_dir / plugin_id
        backup_root = creator_root / "backups" / plugin_id
        backup = backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        staging = deps.plugins_dir / f".{plugin_id}.chat-staging"

        def _install():
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(workspace, staging)
            existed = target.is_dir()
            old_row = plugin_repo.get(plugin_id)
            if existed:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target, backup)
                # Keep conventional user-owned folders if the generated update
                # does not contain them. DB rows and plugin settings live outside
                # the code folder and are preserved independently.
                for name in ("data", "uploads", "media", "storage", ".data"):
                    source = target / name
                    destination = staging / name
                    if source.exists() and not destination.exists():
                        shutil.copytree(source, destination) if source.is_dir() else shutil.copy2(source, destination)
            replaced = deps.plugins_dir / f".{plugin_id}.chat-replaced"
            shutil.rmtree(replaced, ignore_errors=True)
            try:
                if existed:
                    os.replace(target, replaced)
                os.replace(staging, target)
                plugin_repo.upsert(plugin_id, validation["version"], enabled=True)
                run_pending_migrations(load_manifest(target), target)
            except Exception:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                if replaced.exists():
                    os.replace(replaced, target)
                if old_row:
                    plugin_repo.upsert(plugin_id, old_row["version"], enabled=bool(old_row["enabled"]))
                else:
                    plugin_repo.delete(plugin_id)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(replaced, ignore_errors=True)
            return existed

        try:
            updated = await asyncio.to_thread(_install)
        except Exception as exc:
            logger.exception("chat plugin installation failed")
            raise RuntimeError(f"instalação revertida após falha: {exc}") from exc

        result_message = "Plugin atualizado com sucesso." if updated else "Plugin instalado com sucesso."
        result = {
            "plugin_id": plugin_id,
            "version": validation["version"],
            "updated": updated,
            "restarting": True,
            "message": result_message,
        }
        if conversation_id:
            conversation = await asyncio.to_thread(chat_repo.get_conversation, conversation_id)
            if conversation and conversation["project_id"] == project["id"]:
                rows = await asyncio.to_thread(chat_repo.list_messages, conversation_id)
                offer = _pending_install_offer(rows)
                if offer:
                    metadata = dict(offer.get("metadata") or {})
                    metadata["status"] = "updated" if updated else "installed"
                    await asyncio.to_thread(
                        chat_repo.update_message, offer["id"],
                        content=offer.get("content") or "", metadata=metadata,
                    )
                result["message_row"] = await asyncio.to_thread(
                    chat_repo.add_message, conversation_id, "assistant",
                    result_message + " O WhatsBot-Lite será reiniciado para carregar o plugin.",
                )
        schedule_restart(reason=f"plugin {plugin_id} {'updated' if updated else 'installed'} from Chat")
        return result

    @app.get("/api/chat")
    async def bootstrap():
        projects = await asyncio.to_thread(chat_repo.list_projects)
        installed = await asyncio.to_thread(plugin_repo.list_all)
        for project in projects:
            project["conversations"] = await asyncio.to_thread(chat_repo.list_conversations, project["id"])
        return _ok({
            "projects": projects,
            "installed_plugins": installed,
            "default_model": deps.settings.get("model", "deepseek/deepseek-v4.1-flash"),
        })

    @app.post("/api/chat/projects")
    async def create_project(body: dict):
        source_id = (body.get("plugin_id") or "").strip()
        name = (body.get("name") or source_id or "Novo plugin").strip()[:100]
        plugin_id = source_id or _slug(name)
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("id de plugin inválido; use letras minúsculas, números e underscore")
        existing_project = await asyncio.to_thread(chat_repo.get_project_by_plugin, plugin_id)
        if existing_project:
            return _ok(existing_project)
        deleted_project = await asyncio.to_thread(
            chat_repo.get_project_by_plugin, plugin_id, include_deleted=True,
        )
        if deleted_project and deleted_project.get("deleted_at") is not None:
            await asyncio.to_thread(
                chat_repo.update_project, deleted_project["id"], deleted_at=None, name=name,
            )
            return _ok(await asyncio.to_thread(chat_repo.get_project, deleted_project["id"]))

        project_id = uuid.uuid4().hex
        workspace = projects_root / project_id / "workspace" / plugin_id
        installed_dir = deps.plugins_dir / plugin_id
        try:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            if installed_dir.is_dir():
                shutil.copytree(installed_dir, workspace)
                try:
                    name = load_manifest(installed_dir).name
                except Exception:
                    pass
            else:
                _scaffold_plugin_workspace(workspace, plugin_id, name)
        except Exception as exc:
            return _err(f"não foi possível criar a pasta do projeto: {exc}")

        # Repository accepts generated IDs; persist this project with its
        # already-created workspace, then rename the root if IDs differ.
        project = await asyncio.to_thread(chat_repo.create_project, name, "plugin", plugin_id, str(workspace))
        generated_root = projects_root / project["id"]
        desired_root = projects_root / project_id
        if generated_root != desired_root and desired_root.exists():
            final_root = generated_root / "workspace" / plugin_id
            final_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(workspace), str(final_root))
            shutil.rmtree(desired_root, ignore_errors=True)
            workspace = final_root
            await asyncio.to_thread(chat_repo.update_project, project["id"], workspace_path=str(workspace))
            project["workspace_path"] = str(workspace)
        return _ok(project)

    @app.put("/api/chat/projects/{project_id}")
    async def update_project(project_id: str, body: dict):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project or project.get("deleted_at") is not None:
            return _err("projeto não encontrado", 404)
        name = (body.get("name") or "").strip()
        if not name:
            return _err("informe um nome para o projeto")
        await asyncio.to_thread(chat_repo.update_project, project_id, name=name[:100])
        return _ok(await asyncio.to_thread(chat_repo.get_project, project_id))

    @app.post("/api/chat/projects/reorder")
    async def reorder_projects(body: dict):
        project_ids = body.get("project_ids")
        if not isinstance(project_ids, list) or not all(isinstance(item, str) for item in project_ids):
            return _err("ordem de projetos inválida")
        active = await asyncio.to_thread(chat_repo.list_projects)
        active_ids = [project["id"] for project in active]
        if len(project_ids) != len(set(project_ids)) or set(project_ids) != set(active_ids):
            return _err("a ordem deve conter todos os projetos ativos uma única vez")
        await asyncio.to_thread(chat_repo.reorder_projects, project_ids)
        return _ok({"project_ids": project_ids})

    @app.delete("/api/chat/projects/{project_id}")
    async def delete_project(project_id: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project or project.get("deleted_at") is not None:
            return _err("projeto não encontrado", 404)
        if project.get("kind") == "system":
            return _err("o projeto de ajuda do WhatsBot-Lite não pode ser apagado")
        await asyncio.to_thread(chat_repo.soft_delete_project, project_id)
        return _ok({
            "project_id": project_id,
            "soft_deleted": True,
            "plugin_preserved": True,
            "workspace_preserved": True,
        })

    @app.post("/api/chat/projects/{project_id}/conversations")
    async def create_conversation(project_id: str, body: dict):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        existing = await asyncio.to_thread(chat_repo.list_conversations, project_id)
        inherited = existing[0] if existing else {}
        model = (
            body.get("model") or inherited.get("model")
            or deps.settings.get("model", "deepseek/deepseek-v4.1-flash")
        ).strip()
        reasoning = (
            body.get("reasoning") if "reasoning" in body else inherited.get("reasoning", "")
        ) or ""
        reasoning = reasoning.strip()
        if reasoning not in ("", "low", "medium", "high"):
            return _err("nível de reasoning inválido")
        conv = await asyncio.to_thread(chat_repo.create_conversation, project_id, model, reasoning)
        return _ok(conv)

    @app.get("/api/chat/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str):
        conv = await asyncio.to_thread(chat_repo.get_conversation, conversation_id)
        if not conv:
            return _err("conversa não encontrada", 404)
        project = await asyncio.to_thread(chat_repo.get_project, conv["project_id"])
        messages = await asyncio.to_thread(chat_repo.list_messages, conversation_id)
        pricing = await asyncio.to_thread(
            _get_model_pricing_details, conv.get("model") or "",
            deps.settings.get("openrouter_api_key", ""),
        )
        messages = _add_conversation_cost_totals(messages, conv.get("model") or "", pricing)
        active = _active_conversation_runs.get(conversation_id)
        active_run = None if not active else {
            "run_id": active["run_id"],
            "status": active.get("status") or "Trabalhando",
        }
        return _ok({
            "conversation": conv,
            "project": project,
            "messages": messages,
            "active_run": active_run,
        })

    @app.get("/api/chat/conversations/{conversation_id}/activity")
    async def get_conversation_activity(conversation_id: str):
        if not await asyncio.to_thread(chat_repo.get_conversation, conversation_id):
            return _err("conversa não encontrada", 404)
        messages = await asyncio.to_thread(chat_repo.list_messages, conversation_id)
        active = _active_conversation_runs.get(conversation_id)
        active_run = None if not active else {
            "run_id": active["run_id"],
            "status": active.get("status") or "Trabalhando",
        }
        return _ok({"messages": messages, "active_run": active_run})

    @app.put("/api/chat/conversations/{conversation_id}")
    async def update_conversation(conversation_id: str, body: dict):
        if not await asyncio.to_thread(chat_repo.get_conversation, conversation_id):
            return _err("conversa não encontrada", 404)
        values = {}
        for key in ("model", "reasoning", "title"):
            if key in body and isinstance(body[key], str):
                values[key] = body[key].strip()[:200]
        if values.get("reasoning", "") not in ("", "low", "medium", "high"):
            return _err("nível de reasoning inválido")
        if values:
            await asyncio.to_thread(chat_repo.update_conversation, conversation_id, **values)
        return _ok(await asyncio.to_thread(chat_repo.get_conversation, conversation_id))

    @app.delete("/api/chat/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        conversation = await asyncio.to_thread(chat_repo.get_conversation, conversation_id)
        if not conversation:
            return _err("conversa não encontrada", 404)
        await asyncio.to_thread(chat_repo.delete_conversation, conversation_id)
        return _ok({"conversation_id": conversation_id, "deleted": True})

    @app.post("/api/chat/conversations/{conversation_id}/transcribe-audio")
    async def transcribe_chat_audio(
        conversation_id: str,
        audio: UploadFile = File(...),
    ):
        """Transcribe a temporary voice note with the configured WhatsBot-Lite audio model."""
        if not await asyncio.to_thread(chat_repo.get_conversation, conversation_id):
            return _err("conversa não encontrada", 404)
        if not deps.settings.get("openrouter_api_key", ""):
            return _err("configure a chave de API no Painel antes de usar o áudio")
        content = await audio.read(_MAX_AUDIO_BYTES + 1)
        if not content:
            return _err("o áudio gravado está vazio")
        if len(content) > _MAX_AUDIO_BYTES:
            return _err("áudio grande demais (limite de 25 MB)")
        suffix = Path(audio.filename or "voice.ogg").suffix.lower()
        if suffix not in {".ogg", ".oga", ".opus", ".mp3", ".wav"}:
            suffix = ".ogg"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="whatsbot-chat-", suffix=suffix, delete=False) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            transcription = await asyncio.to_thread(
                deps.agent_handler.transcribe_audio, str(temporary_path), "",
            )
            transcription = (transcription or "").strip()
            if not transcription:
                return _err("não foi possível transcrever o áudio", 502)
            return _ok({"transcription": transcription})
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    @app.post("/api/chat/conversations/{conversation_id}/compact")
    async def compact_conversation(conversation_id: str):
        conv = await asyncio.to_thread(chat_repo.get_conversation, conversation_id)
        if not conv:
            return _err("conversa não encontrada", 404)
        if not deps.settings.get("openrouter_api_key", ""):
            return _err("configure a chave de API antes de compactar")
        try:
            result = await _compact(conv, deps.settings)
            return _ok(result)
        except Exception as exc:
            logger.exception("chat compaction failed")
            return _err(f"falha ao compactar: {exc}", 502)

    @app.post("/api/chat/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: str, body: dict, request: Request):
        conv = await asyncio.to_thread(chat_repo.get_conversation, conversation_id)
        if not conv:
            return _err("conversa não encontrada", 404)
        project = await asyncio.to_thread(chat_repo.get_project, conv["project_id"])
        content = (body.get("content") or "").strip()
        if not content:
            return _err("mensagem vazia")
        if len(content) > _MAX_MESSAGE_CHARS:
            return _err(f"mensagem grande demais (limite {_MAX_MESSAGE_CHARS} caracteres)")
        api_key = deps.settings.get("openrouter_api_key", "")
        install_confirmation = project["kind"] == "plugin" and _is_install_confirmation(content)
        if not api_key and not install_confirmation and not (
            project["kind"] == "system" and _is_plugin_intent(content)
        ):
            return _err("configure a chave de API no Painel antes de usar o Chat")
        run_id = uuid.uuid4().hex
        panel_base_url = _public_base_url(request)

        async def stream():
            yield _ndjson("run_started", {"run_id": run_id})
            project_lock = None
            if project["kind"] == "plugin":
                project_lock = _project_locks.setdefault(project["id"], asyncio.Lock())
                if project_lock.locked():
                    yield _ndjson("run_failed", {
                        "run_id": run_id,
                        "error": "este projeto já possui uma execução em andamento",
                    })
                    return
                await project_lock.acquire()
            try:
                if content == "/compact":
                    result = await _compact(conv, deps.settings)
                    text = "Contexto compactado com sucesso." if result.get("compacted") else result.get("reason", "Nada para compactar.")
                    saved = await asyncio.to_thread(chat_repo.add_message, conversation_id, "assistant", text)
                    yield _ndjson("message", saved)
                    yield _ndjson("run_completed", {"run_id": run_id, "compaction": result})
                    return

                user_row = await asyncio.to_thread(chat_repo.add_message, conversation_id, "user", content)
                if conv.get("title") == "Nova conversa":
                    title = content.replace("\n", " ")[:70]
                    await asyncio.to_thread(chat_repo.update_conversation, conversation_id, title=title)
                    yield _ndjson("conversation_updated", {"title": title})

                rows = await asyncio.to_thread(chat_repo.list_messages, conversation_id)
                if install_confirmation:
                    yield _ndjson("status", {"label": "Validando e instalando plugin"})
                    result = await _install_plugin_project(project, conversation_id)
                    message_row = result.pop("message_row", None)
                    if message_row:
                        yield _ndjson("message_saved", message_row)
                    yield _ndjson("install_completed", result)
                    yield _ndjson("run_completed", {
                        "run_id": run_id,
                        "installed": True,
                        "installation": result,
                    })
                    return
                if project["kind"] == "system" and _is_plugin_intent(content):
                    chat_url = f"{panel_base_url}/chat"
                    assistant_text = (
                        "Para criar ou alterar um plugin, clique no botão **+** no topo da barra lateral "
                        f"esquerda do Chat, ou [abra o Chat]({chat_url}), crie um projeto e descreva ali, "
                        "com suas palavras, o que você deseja."
                    )
                    saved = await asyncio.to_thread(
                        chat_repo.add_message, conversation_id, "assistant", assistant_text,
                    )
                    yield _ndjson("message_saved", saved)
                    yield _ndjson("run_completed", {"run_id": run_id, "redirected_to_plugin_project": True})
                    return

                active_rows = [r for r in rows if not conv.get("compacted_through_id") or r["id"] > conv["compacted_through_id"]]
                if sum(len(r.get("content") or "") for r in active_rows) > _AUTO_COMPACT_CHARS and len(active_rows) > 12:
                    yield _ndjson("status", {"label": "Compactando contexto"})
                    result = await _compact(conv, deps.settings, automatic=True)
                    if result.get("compacted"):
                        conv.update(summary=result["summary"], compacted_through_id=result["through_id"])
                        yield _ndjson("compacted", result)
                        rows = await asyncio.to_thread(chat_repo.list_messages, conversation_id)

                after = conv.get("compacted_through_id")
                context_rows = [r for r in rows if after is None or r["id"] > after]
                shared_context = await asyncio.to_thread(
                    _shared_project_context, project["id"], conversation_id,
                )
                summary_context = "\n\n".join(
                    value for value in (shared_context, conv.get("summary") or "") if value
                )
                model_id = conv.get("model") or deps.settings.get("model", "deepseek/deepseek-v4.1-flash")
                # The plugin prompt itself decides whether essential details are
                # missing. Character count used to force every short first
                # request through a separate discovery turn, even when the user
                # had already provided a complete brief.
                agent_kind = project["kind"]
                agent = build_agent(
                    api_key=api_key, model_id=model_id, reasoning=conv.get("reasoning") or "",
                    project_kind=agent_kind, workspace=_project_workspace(project),
                    project_root=deps.settings.data_dir,
                    panel_base_url=panel_base_url,
                )
                active_run = {"agent": agent, "current_run_id": run_id}
                _active_agents[run_id] = active_run
                assistant_text = ""
                final_metrics = {}
                active_action_messages: dict[str, dict] = {}
                yield _ndjson("status", {"label": "Analisando"})
                prompt_messages = history_messages(summary_context, context_rows)

                async def consume_round(round_input, current_run_id: str, state: dict):
                    active_run["current_run_id"] = current_run_id
                    events = agent.arun(
                        input=round_input, stream=True, stream_events=True,
                        run_id=current_run_id, session_id=conversation_id,
                    )
                    async for event in _with_timeout(events):
                        if event is _STREAM_HEARTBEAT:
                            yield _ndjson("heartbeat", {"run_id": run_id})
                            continue
                        event_name = getattr(event, "event", "")
                        if event_name == "RunContent":
                            delta = getattr(event, "content", None)
                            if delta:
                                state["text"] += str(delta)
                                yield _ndjson("content", {"delta": str(delta)})
                        elif event_name == "ToolCallStarted":
                            payload = _tool_payload(event)
                            saved = await asyncio.to_thread(
                                chat_repo.add_message, conversation_id, "assistant",
                                payload.get("name", "Ação"), kind="action", metadata={"status": "running", **payload},
                            )
                            action_key = payload.get("tool_call_id") or json.dumps(
                                [payload.get("name"), payload.get("args")], sort_keys=True, default=str,
                            )
                            active_action_messages[action_key] = saved
                            yield _ndjson("action_started", saved)
                        elif event_name in ("ToolCallCompleted", "ToolCallError"):
                            payload = _tool_payload(event)
                            action_key = payload.get("tool_call_id") or json.dumps(
                                [payload.get("name"), payload.get("args")], sort_keys=True, default=str,
                            )
                            action_content = str(payload.get("result") or getattr(event, "error", "") or "Concluído")
                            semantic_error = action_content.lstrip().lower().startswith("error:")
                            action_metadata = {
                                "status": "failed" if event_name.endswith("Error") or semantic_error else "completed",
                                **payload,
                            }
                            active_message = active_action_messages.pop(action_key, None)
                            if active_message:
                                saved = await asyncio.to_thread(
                                    chat_repo.update_message, active_message["id"],
                                    content=action_content, metadata=action_metadata,
                                )
                            else:
                                saved = await asyncio.to_thread(
                                    chat_repo.add_message, conversation_id, "assistant", action_content,
                                    kind="action", metadata=action_metadata,
                                )
                            yield _ndjson("action_completed", saved)
                        elif event_name in ("RunCompleted", "RunContentCompleted"):
                            maybe_metrics = metrics_dict(getattr(event, "metrics", None))
                            if any(maybe_metrics.values()):
                                state["metrics"] = _merge_metrics(state["metrics"], maybe_metrics)
                        elif event_name == "RunError":
                            raise RuntimeError(str(getattr(event, "content", None) or "erro na execução do agente"))
                        elif event_name == "RunCancelled":
                            state["cancelled"] = True
                            yield _ndjson("run_cancelled", {"run_id": run_id})
                            return

                validation = None
                validation_error = ""
                round_input = prompt_messages
                max_rounds = 3 if project["kind"] == "plugin" else 1
                for round_number in range(max_rounds):
                    round_state = {"text": "", "metrics": {}, "cancelled": False}
                    current_run_id = run_id if round_number == 0 else f"{run_id}-repair-{round_number}"
                    async for chunk in consume_round(round_input, current_run_id, round_state):
                        yield chunk
                    if round_state["cancelled"]:
                        return
                    assistant_text = round_state["text"] or assistant_text
                    final_metrics = _merge_metrics(final_metrics, round_state["metrics"])

                    if project["kind"] != "plugin":
                        break
                    workspace = _project_workspace(project)
                    manifest_exists = bool(workspace and (workspace / "plugin.yaml").is_file())
                    # A question is a legitimate end state while requirements
                    # are missing. Once files exist, validation owns readiness.
                    if not manifest_exists and "?" in assistant_text:
                        break
                    yield _ndjson("status", {"label": "Validando plugin"})
                    try:
                        validation = await asyncio.to_thread(
                            validate_workspace, project, save_version=False,
                        )
                        validation_error = ""
                        break
                    except Exception as exc:
                        validation_error = str(exc)
                        yield _ndjson("validation_failed", {
                            "error": validation_error,
                            "will_retry": round_number + 1 < max_rounds,
                        })
                    if round_number + 1 >= max_rounds:
                        break
                    yield _ndjson("status", {"label": "Corrigindo plugin"})
                    repair_instruction = (
                        "O esqueleto inicial já é conhecido. Não chame read_file ou list_files. "
                        "Sua próxima ação obrigatória é write_file em plugin.yaml; continue com write_file "
                        "para os outros arquivos até implementar o pedido completo. Apague "
                        ".whatsbot-scaffold no fim e então valide."
                        if _SCAFFOLD_MARKER in validation_error else
                        "Leia apenas os arquivos diretamente ligados ao erro, corrija-os agora e valide novamente."
                    )
                    round_input = [
                        *prompt_messages,
                        Message(role="assistant", content=assistant_text),
                        Message(
                            role="user",
                            content=(
                                "A validação independente encontrou o erro abaixo. Corrija os arquivos agora, "
                                "execute validate_plugin_project novamente e só conclua quando retornar valid=true.\n"
                                f"INSTRUÇÃO DE REPARO: {repair_instruction}\n\n"
                                f"ERRO DE VALIDAÇÃO:\n{validation_error}"
                            ),
                        ),
                    ]

                if project["kind"] == "plugin" and validation_error:
                    raise RuntimeError(
                        "o plugin continuou inválido após as tentativas automáticas: " + validation_error
                    )

                if project["kind"] == "plugin" and assistant_text.count("?") > 1:
                    assistant_text = _format_discovery_response(assistant_text)
                if project["kind"] == "system":
                    assistant_text = _ensure_system_help_link(
                        content, assistant_text, _public_base_url(request),
                    )
                assistant_text = assistant_text.strip()
                if not assistant_text:
                    assistant_text = "Execução concluída sem resposta textual."
                saved = await asyncio.to_thread(chat_repo.add_message, conversation_id, "assistant", assistant_text)
                yield _ndjson("message_saved", saved)

                if validation is not None:
                    validation = await asyncio.to_thread(validate_workspace, project)
                    install_offer = {
                        "plugin_id": validation["plugin_id"],
                        "name": validation["name"],
                        "version": validation["version"],
                        "tests": validation["tests"],
                        "installed": validation["installed"],
                        "status": "pending",
                    }
                    offer_row = await asyncio.to_thread(
                        chat_repo.add_message, conversation_id, "assistant", "",
                        kind="install_offer", metadata=install_offer,
                    )
                    install_offer["offer_message_id"] = offer_row["id"]
                    yield _ndjson("install_ready", install_offer)
                if final_metrics:
                    pricing = await asyncio.to_thread(
                        _get_model_pricing_details, model_id, api_key,
                    )
                    final_metrics = _add_cost_estimate(final_metrics, model_id, pricing)
                    previous_messages = await asyncio.to_thread(chat_repo.list_messages, conversation_id)
                    previous_messages = _add_conversation_cost_totals(
                        previous_messages, model_id, pricing,
                    )
                    final_metrics["conversation_estimated_cost_usd"] = sum(
                        _number((row.get("metadata") or {}).get("estimated_cost_usd"))
                        for row in previous_messages if row.get("kind") == "metrics"
                    ) + _number(final_metrics.get("estimated_cost_usd"))
                    metric_row = await asyncio.to_thread(
                        chat_repo.add_message, conversation_id, "assistant", "",
                        kind="metrics", metadata=final_metrics,
                    )
                    yield _ndjson("metrics", metric_row)
                yield _ndjson("run_completed", {"run_id": run_id, "metrics": final_metrics, "validation": validation})
            except asyncio.CancelledError:
                yield _ndjson("run_cancelled", {"run_id": run_id})
            except Exception as exc:
                logger.exception("plugin chat run failed")
                await asyncio.to_thread(chat_repo.add_message, conversation_id, "assistant", f"Falha: {exc}", kind="system")
                yield _ndjson("run_failed", {"run_id": run_id, "error": str(exc)})
            finally:
                for active_message in active_action_messages.values() if "active_action_messages" in locals() else ():
                    metadata = dict(active_message.get("metadata") or {})
                    metadata["status"] = "failed"
                    await asyncio.to_thread(
                        chat_repo.update_message, active_message["id"],
                        content="Execução interrompida antes de a ação terminar.", metadata=metadata,
                    )
                _active_agents.pop(run_id, None)
                if project_lock is not None and project_lock.locked():
                    project_lock.release()

        queue: asyncio.Queue = asyncio.Queue()
        channel = {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "project_id": project["id"],
            "status": "Iniciando",
            "stream_open": True,
        }
        _active_conversation_runs[conversation_id] = channel

        async def pump_in_background():
            try:
                async for chunk in stream():
                    try:
                        event = json.loads(chunk)
                        event_name = event.get("event")
                        event_data = event.get("data") or {}
                        if event_name == "status":
                            channel["status"] = event_data.get("label") or "Trabalhando"
                        elif event_name == "install_ready":
                            channel["status"] = "Pronto para instalar"
                        elif event_name in ("run_failed", "run_cancelled", "run_completed"):
                            channel["status"] = event_name.removeprefix("run_")
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass
                    if channel.get("stream_open"):
                        queue.put_nowait(chunk)
            except asyncio.CancelledError:
                logger.info("background chat run cancelled: %s", run_id)
            except Exception:
                logger.exception("background chat run crashed: %s", run_id)
            finally:
                current = _active_conversation_runs.get(conversation_id)
                if current is channel:
                    _active_conversation_runs.pop(conversation_id, None)
                if channel.get("stream_open"):
                    queue.put_nowait(None)

        task = asyncio.create_task(pump_in_background(), name=f"plugin-chat-{run_id}")
        channel["task"] = task
        _background_chat_tasks.add(task)
        task.add_done_callback(_background_chat_tasks.discard)

        async def subscribe():
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        return
                    yield chunk
            finally:
                # Closing the page only detaches this viewer. The background
                # task keeps writing files, validating and persisting results.
                channel["stream_open"] = False

        return StreamingResponse(
            subscribe(), media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Chat-Run-ID": run_id},
        )

    @app.post("/api/chat/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        active = _active_agents.get(run_id)
        if not active:
            background = next(
                (item for item in _active_conversation_runs.values() if item["run_id"] == run_id),
                None,
            )
            task = background.get("task") if background else None
            if task and not task.done():
                task.cancel()
                return _ok({"cancelled": True})
            return _ok({"cancelled": False})
        if isinstance(active, dict):
            agent = active["agent"]
            current_run_id = active.get("current_run_id") or run_id
        else:
            agent = active
            current_run_id = run_id
        cancelled = await agent.acancel_run(current_run_id)
        return _ok({"cancelled": bool(cancelled)})

    @app.get("/api/chat/projects/{project_id}/files")
    async def list_project_files(project_id: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        root = _project_workspace(project)
        files = [] if root is None else [
            p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ]
        return _ok({"files": files})

    @app.get("/api/chat/projects/{project_id}/file")
    async def read_project_file(project_id: str, path: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        try:
            target = _safe_workspace_file(project, path)
            if not target.is_file() or target.stat().st_size > 2_000_000:
                return _err("arquivo não encontrado ou grande demais", 404)
            return _ok({"path": path, "content": target.read_text(encoding="utf-8", errors="replace")})
        except ValueError as exc:
            return _err(str(exc))

    @app.post("/api/chat/projects/{project_id}/validate")
    async def validate_project(project_id: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        try:
            return _ok(await asyncio.to_thread(validate_workspace, project))
        except Exception as exc:
            return _err(str(exc))

    @app.get("/api/chat/projects/{project_id}/export")
    async def export_project(project_id: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        try:
            validation = await asyncio.to_thread(validate_workspace, project, save_version=False)
            workspace = _project_workspace(project)
            payload = await asyncio.to_thread(_zip_workspace, workspace)
            return StreamingResponse(
                io.BytesIO(payload), media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{validation["plugin_id"]}-{validation["version"]}-plugin.zip"'},
            )
        except Exception as exc:
            return _err(str(exc))

    @app.post("/api/chat/projects/{project_id}/install")
    async def install_project(project_id: str, body: dict):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project:
            return _err("projeto não encontrado", 404)
        try:
            result = await _install_plugin_project(
                project, (body or {}).get("conversation_id") or "",
            )
        except Exception as exc:
            return _err(str(exc))
        result.pop("message_row", None)
        return _ok(result)

    @app.post("/api/chat/projects/{project_id}/rollback")
    async def rollback_project(project_id: str):
        project = await asyncio.to_thread(chat_repo.get_project, project_id)
        if not project or project.get("kind") != "plugin":
            return _err("projeto de plugin não encontrado", 404)
        plugin_id = project["plugin_id"]
        backup_root = creator_root / "backups" / plugin_id
        backups = sorted((p for p in backup_root.iterdir() if p.is_dir()), reverse=True) if backup_root.is_dir() else []
        if not backups:
            return _err("nenhum backup disponível")
        target = deps.plugins_dir / plugin_id
        failed = deps.plugins_dir / f".{plugin_id}.failed-{int(time.time())}"
        try:
            if target.exists():
                os.replace(target, failed)
            shutil.copytree(backups[0], target)
            manifest = load_manifest(target)
            plugin_repo.upsert(plugin_id, manifest.version)
            schedule_restart(reason=f"plugin {plugin_id} rolled back from Chat")
            return _ok({"plugin_id": plugin_id, "version": manifest.version, "restarting": True})
        except Exception as exc:
            if not target.exists() and failed.exists():
                os.replace(failed, target)
            return _err(f"falha ao restaurar backup: {exc}")

    @app.post("/api/chat/cache-test")
    async def cache_test(body: dict):
        model_id = (body.get("model") or deps.settings.get("model", "")).strip()
        reasoning = (body.get("reasoning") or "").strip()
        api_key = deps.settings.get("openrouter_api_key", "")
        if not api_key:
            return _err("chave de API não configurada")
        stable = ("Teste de cache do WhatsBot-Lite. Responda apenas OK. " * 180)[:9000]
        results = []
        try:
            for _ in range(2):
                agent = build_agent(
                    api_key=api_key, model_id=model_id, reasoning=reasoning,
                    project_kind="system", workspace=None, project_root=deps.settings.data_dir,
                )
                output = await asyncio.wait_for(
                    agent.arun(stable, stream=False, session_id="whatsbot-cache-test"),
                    timeout=180,
                )
                results.append(metrics_dict(getattr(output, "metrics", None)))
            hit = any(r.get("cache_read_tokens", 0) > 0 for r in results)
            return _ok({
                "supported": hit, "calls": results,
                "status": "Cache comprovado por tokens reutilizados." if hit else "Cache não comprovado: nenhuma reutilização foi informada pela API.",
                "critical": not hit,
            })
        except Exception as exc:
            return _err(f"teste de cache falhou: {exc}", 502)
