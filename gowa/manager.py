import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from gowa import binary as gowa_binary
from gowa import proxy as gowa_proxy

logger = logging.getLogger(__name__)

GOWA_LOG_MAX_BYTES = 10 * 1024 * 1024  # truncate above ~10 MB


def _get_gowa_binary() -> Path:
    """Locate the GOWA binary.

    Resolution (managed override vs. bundled baseline) lives in
    ``gowa/binary.py`` so the updater and the API share the exact same rules.
    """
    return gowa_binary.resolve_binary()[0]


def _gowa_log_path() -> Path:
    """Return path to the GOWA debug log file (created lazily)."""
    base = Path(__file__).resolve().parent.parent
    logs_dir = base / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "gowa.log"


def _debug_enabled() -> bool:
    return os.environ.get("WHATSBOT_GOWA_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


class GOWAManager:
    """Manages the GOWA subprocess lifecycle."""

    def __init__(self, port: int = 3000, data_dir: Path | None = None,
                 webhook_url: str | None = None, on_restart=None):
        self.port = port
        self.webhook_url = webhook_url
        self.data_dir = data_dir or Path.home() / ".config" / "WhatsBot"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._process: subprocess.Popen | None = None
        self._running = False
        self._watchdog_thread: threading.Thread | None = None
        self._restart_count = 0
        self._restart_window_start = 0.0
        self._max_restarts = 3
        self._restart_window_sec = 60
        self._on_restart = on_restart
        # Serializes start/stop/restart. The updater stops GOWA from a worker
        # thread while the watchdog may be trying to respawn it. Reentrant
        # because restart() calls stop() and start().
        self._lifecycle_lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def binary_path(self) -> Path:
        """Path of the binary this manager would launch right now."""
        return _get_gowa_binary()

    def start(self):
        """Start the GOWA process."""
        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self):
        if self.is_running:
            logger.info("GOWA already running (pid=%s)", self._process.pid)
            return

        binary = _get_gowa_binary()
        if not binary.exists():
            raise FileNotFoundError(
                f"GOWA binary not found at {binary}. Expected it either at "
                f"{gowa_binary.bundled_binary_path()} (shipped with the app) or at "
                f"{gowa_binary.managed_binary_path()} (installed from the panel)."
            )

        # A fresh start clears the crash budget. Without this, an update that
        # follows a few crashes inherits the old count and the watchdog gives
        # up after a single further failure.
        self._restart_count = 0
        self._restart_window_start = 0.0

        cmd = [
            str(binary),
            "rest",
            "--port", str(self.port),
        ]
        if self.webhook_url:
            cmd.extend(["--webhook", self.webhook_url])
        # Forward every event the webhook handler knows how to process. GOWA
        # only delivers events listed here; omitting one (e.g. group.participants)
        # silently drops it even though webhook.py has a handler — which is why
        # group join/leave, reactions, edits and revokes never fired before.
        cmd.extend(["--webhook-events", ",".join([
            "message",
            "message.reaction",
            "message.edited",
            "message.revoked",
            "message.deleted",
            "message.ack",
            "chat_presence",
            "group.participants",
            "group.joined",
            "call.offer",
        ])])
        # Must be "available" to receive typing events from contacts
        cmd.extend(["--presence-on-connect", "available"])
        cmd.extend(["--os", "Techify - WhatsBot-Lite"])

        debug_on = _debug_enabled()
        if debug_on:
            cmd.extend(["--debug=true"])

        # Outbound proxy for the WhatsApp WebSocket. Passed through the
        # environment on purpose: an older GOWA ignores an unknown env var, but
        # would abort on an unknown CLI flag, and argv would leak the password
        # to ``ps``. See gowa/proxy.py.
        env = os.environ.copy()
        proxy_url = gowa_proxy.current_url()
        if proxy_url:
            env[gowa_proxy.ENV_VAR] = proxy_url
            if gowa_proxy.supports_proxy():
                logger.info("GOWA usando proxy: %s", gowa_proxy.mask_url(proxy_url))
            else:
                logger.warning(
                    "Proxy configurado (%s) mas o GOWA %s é anterior a %s e vai ignorá-lo; "
                    "atualize o GOWA pelo painel.",
                    gowa_proxy.mask_url(proxy_url),
                    gowa_binary.installed_version() or "de versão não identificada",
                    gowa_proxy.MIN_GOWA_VERSION,
                )
        else:
            env.pop(gowa_proxy.ENV_VAR, None)

        logger.info("Starting GOWA (debug=%s): %s", debug_on, " ".join(cmd))
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        if debug_on:
            log_path = _gowa_log_path()
            # Rotate if too large
            try:
                if log_path.exists() and log_path.stat().st_size > GOWA_LOG_MAX_BYTES:
                    log_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Could not rotate gowa.log: %s", e)
            log_fh = open(log_path, "ab", buffering=0)
            self._log_fh = log_fh
            stdout_target = log_fh
            stderr_target = subprocess.STDOUT
            logger.info("GOWA debug logs -> %s", log_path)
        else:
            self._log_fh = None
            stdout_target = subprocess.DEVNULL
            stderr_target = subprocess.DEVNULL

        self._process = subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=stderr_target,
            creationflags=creation_flags,
            env=env,
        )
        self._running = True
        logger.info("GOWA started (pid=%s)", self._process.pid)

        # Start watchdog
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, daemon=True, name="gowa-watchdog"
        )
        self._watchdog_thread.start()

    def stop(self):
        """Stop the GOWA process gracefully."""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self):
        self._running = False
        if self._process is None:
            return

        logger.info("Stopping GOWA (pid=%s)...", self._process.pid)
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("GOWA did not stop gracefully, killing...")
            self._process.kill()
            self._process.wait(timeout=3)
        except Exception as e:
            logger.error("Error stopping GOWA: %s", e)
        finally:
            self._process = None
            if getattr(self, "_log_fh", None):
                try:
                    self._log_fh.close()
                except Exception:
                    pass
                self._log_fh = None
            logger.info("GOWA stopped.")

    def restart(self):
        """Stop and start GOWA."""
        with self._lifecycle_lock:
            self._stop_locked()
            time.sleep(1)
            self._start_locked()

    def _watchdog(self):
        """Watch the GOWA process and restart on crash."""
        while self._running:
            if self._process and self._process.poll() is not None:
                exit_code = self._process.returncode
                logger.warning("GOWA exited with code %s", exit_code)
                self._process = None

                if not self._running:
                    break

                # Rate-limit restarts
                now = time.time()
                if now - self._restart_window_start > self._restart_window_sec:
                    self._restart_count = 0
                    self._restart_window_start = now

                self._restart_count += 1
                if self._restart_count > self._max_restarts:
                    logger.error(
                        "GOWA crashed %d times in %ds, giving up.",
                        self._restart_count,
                        self._restart_window_sec,
                    )
                    self._running = False
                    break

                logger.info("Restarting GOWA in 5 seconds... (attempt %d/%d)",
                            self._restart_count, self._max_restarts)
                time.sleep(5)
                if self._running:
                    try:
                        self.start()
                        if self._on_restart:
                            try:
                                self._on_restart()
                            except Exception as cb_err:
                                logger.error("on_restart callback error: %s", cb_err)
                    except Exception as e:
                        logger.error("Failed to restart GOWA: %s", e)
                break  # New watchdog thread is started by start()
            time.sleep(2)
