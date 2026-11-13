"""Schedule an in-process restart so plugin state changes take effect.

Strategy:

- In Docker (``WHATSBOT_DOCKER=1``) we rely on the container's ``restart: unless-stopped``
  policy: we exit with status 0 after a small delay, the container respawns.
- Locally we touch a ``.py`` trigger file inside a watched dir so
  ``uvicorn --reload`` restarts the worker cleanly. The exit below is a
  belt-and-suspenders fallback for hosts where the file watcher misses the
  event (Windows network drives, etc).
- PyInstaller EXE: ``update.py`` is the supervisor and relaunches on exit.

The trigger lives at ``server/_reload_trigger.py`` because:
- ``server/`` is always passed via ``--reload-dir`` in dev;
- uvicorn's default include pattern is ``*.py``, so a ``.py`` extension is
  required for watchfiles to fire on the change;
- the leading underscore makes the intent explicit and keeps it out of any
  package-discovery scans.

The delay gives the current HTTP response time to flush before the process dies.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: Um id por PROCESSO. ``/health`` o devolve: quem espera o servidor voltar de um reinício (a tela do
#: Importar/Exportar) detecta a TROCA do id. "Ficou fora do ar e voltou" engana com ``--reload`` e com
#: proxy que responde no lugar do servidor.
BOOT_ID = uuid.uuid4().hex

def is_supervised() -> bool:
    """Alguém religa o servidor depois de um ``os._exit``? Docker (``restart: unless-stopped``), o serviço
    systemd (``WHATSBOT_SERVICE=1``) ou um launcher que exporta ``WHATSBOT_SUPERVISED=1``
    (``linux_start.sh``, ``macos_start.command``, ``windows_start.bat``). Sem supervisor o servidor cai e
    NÃO volta: por isso o reinício automático só é oferecido quando isto é verdadeiro."""
    return (os.environ.get("WHATSBOT_DOCKER") == "1" or os.environ.get("WHATSBOT_SUPERVISED") == "1"
            or os.environ.get("WHATSBOT_SERVICE") == "1" or Path("/.dockerenv").exists()
            or _under_reloader())

def _under_reloader() -> bool:
    """Este processo é o worker de um ``uvicorn --reload``? Então o reloader (pai) religa o worker quando
    ``schedule_restart`` toca o gatilho, mesmo sem a variável no ambiente (instância subida na mão).
    Só Linux (lê ``/proc``); qualquer dúvida ⇒ ``False``."""
    try:
        raw = Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
    except OSError:
        return False
    parts = raw.decode("utf-8", "replace").split("\0")
    return any(p == "uvicorn" or p.endswith("/uvicorn") for p in parts) and "--reload" in parts

_RESTART_DELAY_SECONDS = 1.5
_RESTART_PENDING = False
_LOCK = threading.Lock()

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Watched by uvicorn --reload (server/ is in --reload-dir, .py matches default include).
_RELOAD_TRIGGER = _REPO_ROOT / "server" / "_reload_trigger.py"


def schedule_restart(reason: str = "") -> None:
    """Touch the reload trigger and schedule ``os._exit(0)`` shortly after.

    Idempotent: multiple concurrent calls only restart once.
    """
    global _RESTART_PENDING
    with _LOCK:
        if _RESTART_PENDING:
            logger.info("Restart already pending; ignoring extra request: %s", reason)
            return
        _RESTART_PENDING = True

    try:
        # First touch creates the file; subsequent touches bump mtime, which
        # is what watchfiles uses to detect changes.
        if not _RELOAD_TRIGGER.exists():
            _RELOAD_TRIGGER.write_text(
                "# Touched by plugins.restart.schedule_restart() to trigger uvicorn --reload.\n",
                encoding="utf-8",
            )
        else:
            _RELOAD_TRIGGER.touch()
    except Exception as e:
        logger.warning("Could not touch reload trigger: %s", e)

    logger.warning("Scheduling restart in %.1fs: %s", _RESTART_DELAY_SECONDS, reason)

    def _exit_later():
        time.sleep(_RESTART_DELAY_SECONDS)
        logger.warning("Restarting now (%s)", reason)
        # Some environments need a hard exit so background tasks don't block;
        # ``os._exit`` skips finalizers but is the only reliable way out of an
        # asyncio event loop holding sockets open. In dev with --reload, the
        # uvicorn parent has typically already started replacing this worker
        # because of the trigger touch above; this just ensures the old one
        # is gone if the watcher missed it.
        os._exit(0)

    threading.Thread(target=_exit_later, daemon=True).start()
