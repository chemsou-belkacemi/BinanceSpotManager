"""Bot Process Manager — demarrage/arret du worker depuis l'interface.

Le worker est un process Python separe de Streamlit (section 9). Ce module
fournit au Dashboard :
- le demarrage detache (sans fenetre visible sous Windows) ;
- l'arret propre via drapeau, avec arret force en secours ;
- le verrou anti double-worker (section 79) ;
- la lecture du heartbeat et du PID.

Le verrou est base sur un fichier contenant le PID : au demarrage, si le PID
enregistre est encore vivant, le nouveau worker refuse de demarrer.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import (
    BOT_LOCK_FILE,
    BOT_STOP_FLAG,
    PROJECT_ROOT,
    Settings,
    get_settings,
)
from .event_store import EventStore, log_error
from .models import BotRuntime, EventType, WorkerState, utcnow
from .position_store import RuntimeStore, atomic_write_json, read_json

logger = logging.getLogger("bsm.process")

WORKER_SCRIPT = PROJECT_ROOT / "scripts" / "bot_worker.py"


# ==========================================================================
# Verification de process
# ==========================================================================


def pid_is_alive(pid: Optional[int]) -> bool:
    """Vrai si ce PID correspond a un process en cours d'execution."""
    if not pid or pid <= 0:
        return False

    if os.name == "nt":  # Windows
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(pid) in (result.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass
class WorkerStatus:
    """Vue consolidee de l'etat du worker pour l'interface."""

    running: bool = False
    state: str = WorkerState.STOPPED.value
    pid: Optional[int] = None
    pid_alive: bool = False
    heartbeat_age: Optional[float] = None
    loop_count: int = 0
    started_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    positions_monitored: int = 0
    last_message: str = ""
    last_error: str = ""
    stop_flag_present: bool = False
    lock_owner_pid: Optional[int] = None
    environment: str = "DEMO"
    run_mode: str = "DRY_RUN"

    @property
    def is_stale(self) -> bool:
        return self.heartbeat_age is not None and self.heartbeat_age > 60

    @property
    def label(self) -> str:
        if self.running and self.pid_alive:
            return "Worker actif"
        if self.stop_flag_present:
            return "Worker en arret"
        if self.state == WorkerState.ERROR.value:
            return "Worker en erreur"
        return "Worker inactif"


# ==========================================================================
# Verrou anti double-worker
# ==========================================================================


class WorkerLock:
    """Verrou simple et robuste base sur un fichier + PID."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else BOT_LOCK_FILE

    def read_owner(self) -> Optional[int]:
        payload = read_json(self.path)
        if isinstance(payload, dict):
            pid = payload.get("pid")
            return int(pid) if isinstance(pid, int) else None
        return None

    def is_held_by_other(self, my_pid: Optional[int] = None) -> bool:
        owner = self.read_owner()
        if owner is None:
            return False
        if my_pid is not None and owner == my_pid:
            return False
        return pid_is_alive(owner)

    def acquire(self, pid: Optional[int] = None) -> bool:
        """Prend le verrou. False si un autre worker vivant le detient."""
        my_pid = pid or os.getpid()
        if self.is_held_by_other(my_pid):
            return False
        atomic_write_json(
            self.path,
            {"pid": my_pid, "acquired_at": utcnow().isoformat()},
        )
        return True

    def release(self, pid: Optional[int] = None) -> None:
        my_pid = pid or os.getpid()
        owner = self.read_owner()
        if owner is None or owner == my_pid:
            self.path.unlink(missing_ok=True)

    def force_release(self) -> None:
        """Libere un verrou orphelin (PID mort). Jamais sur un worker vivant."""
        owner = self.read_owner()
        if owner is None or not pid_is_alive(owner):
            self.path.unlink(missing_ok=True)
            return
        raise RuntimeError(
            f"Verrou detenu par un worker vivant (PID {owner}) : arreter le worker d'abord"
        )


# ==========================================================================
# Gestionnaire
# ==========================================================================


class BotProcessManager:
    """Pilote le cycle de vie du worker depuis Streamlit."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        runtime_store: Optional[RuntimeStore] = None,
        lock: Optional[WorkerLock] = None,
        events: Optional[EventStore] = None,
        worker_script: Optional[Path] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime_store = runtime_store or RuntimeStore()
        self.lock = lock or WorkerLock()
        self.events = events or EventStore()
        self.worker_script = Path(worker_script) if worker_script else WORKER_SCRIPT

    # ------------------------------------------------------------------
    # Etat
    # ------------------------------------------------------------------

    def status(self) -> WorkerStatus:
        runtime = self.runtime_store.load()
        pid_alive = pid_is_alive(runtime.pid)
        age = runtime.heartbeat_age()

        return WorkerStatus(
            running=bool(runtime.pid and pid_alive),
            state=runtime.state.value,
            pid=runtime.pid,
            pid_alive=pid_alive,
            heartbeat_age=age,
            loop_count=runtime.loop_count,
            started_at=runtime.started_at.isoformat() if runtime.started_at else None,
            heartbeat_at=runtime.heartbeat_at.isoformat() if runtime.heartbeat_at else None,
            positions_monitored=runtime.positions_monitored,
            last_message=runtime.last_message,
            last_error=runtime.last_error,
            stop_flag_present=BOT_STOP_FLAG.exists(),
            lock_owner_pid=self.lock.read_owner(),
            environment=runtime.environment or self.settings.environment.value,
            run_mode=runtime.run_mode or self.settings.run_mode.value,
        )

    def is_running(self) -> bool:
        return self.status().running

    # ------------------------------------------------------------------
    # Demarrage
    # ------------------------------------------------------------------

    def start(self, *, wait_seconds: float = 3.0) -> tuple[bool, str]:
        """Demarre le worker en process detache. Retourne (succes, message)."""
        if not self.worker_script.exists():
            return False, f"Script worker introuvable : {self.worker_script}"

        status = self.status()
        if status.running and status.pid_alive:
            return False, f"Worker deja actif (PID {status.pid})"

        # Nettoie un verrou orphelin d'un ancien crash, puis verifie.
        if self.lock.read_owner() and not pid_is_alive(self.lock.read_owner()):
            self.lock.force_release()
        if self.lock.is_held_by_other():
            return False, (
                f"Verrou detenu par le PID {self.lock.read_owner()} : "
                f"arret force requis avant relance"
            )

        BOT_STOP_FLAG.unlink(missing_ok=True)

        command = [sys.executable, str(self.worker_script)]
        kwargs: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            # Process totalement detache, sans console visible.
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            log_error(f"Demarrage worker impossible : {exc}", context="bot_process_manager")
            return False, f"Demarrage impossible : {exc}"

        time.sleep(max(wait_seconds, 0.0))

        runtime = self.runtime_store.load()
        if runtime.pid and pid_is_alive(runtime.pid):
            self.events.append(
                EventType.WORKER_STARTED,
                f"Worker demarre (PID {runtime.pid}) depuis le Dashboard",
            )
            return True, f"Worker demarre (PID {runtime.pid})"

        if pid_is_alive(process.pid):
            return True, (
                f"Worker lance (PID {process.pid}) mais sans heartbeat encore — "
                f"verifier logs/bot.log"
            )
        return False, "Le worker s'est arrete immediatement — voir logs/bot.log"

    # ------------------------------------------------------------------
    # Arret
    # ------------------------------------------------------------------

    def request_stop(self) -> tuple[bool, str]:
        """Demande un arret propre : le worker le constate a sa prochaine boucle."""
        status = self.status()
        if not status.running:
            return False, "Worker deja inactif"

        try:
            BOT_STOP_FLAG.write_text(
                f"stop requested at {utcnow().isoformat()}\n", encoding="utf-8"
            )
        except OSError as exc:
            return False, f"Impossible d'ecrire le drapeau d'arret : {exc}"

        return True, "Arret demande : le worker s'arretera a la fin de sa boucle"

    def wait_for_stop(self, timeout_seconds: float = 15.0) -> bool:
        """Attend l'arret effectif du worker."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.5)
        return not self.is_running()

    def stop(
        self, *, force: bool = False, timeout_seconds: float = 15.0
    ) -> tuple[bool, str]:
        """Arret propre, avec arret force en secours si demande.

        `force=True` tue le process — a n'utiliser que sur un worker bloque.
        """
        status = self.status()
        if not status.running:
            self.lock.force_release() if not pid_is_alive(self.lock.read_owner()) else None
            return False, "Worker deja inactif"

        pid = status.pid
        self.request_stop()

        if not force and self.wait_for_stop(timeout_seconds):
            self.lock.release(pid or 0)
            self._mark_stopped()
            self.events.append(EventType.WORKER_STOPPED, f"Worker arrete proprement (PID {pid})")
            return True, f"Worker arrete (PID {pid})"

        if force and pid and pid_is_alive(pid):
            killed = self._kill(pid)
            if killed:
                self.lock.force_release()
                self._mark_stopped()
                self.events.append(
                    EventType.WORKER_STOPPED,
                    f"Worker arrete de force (PID {pid})",
                    level="WARNING",
                )
                return True, f"Worker tue de force (PID {pid})"
            return False, f"Impossible de tuer le process {pid}"

        return False, (
            "Le worker ne s'est pas arrete dans le delai — relancer avec "
            "l'arret force si necessaire"
        )

    def _kill(self, pid: int) -> bool:
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return result.returncode == 0
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Arret force du PID %s impossible : %s", pid, exc)
            return False

    def _mark_stopped(self) -> None:
        runtime = self.runtime_store.load()
        runtime.state = WorkerState.STOPPED
        runtime.pid = None
        runtime.last_message = "Arrete depuis le Dashboard"
        self.runtime_store.save(runtime)
        BOT_STOP_FLAG.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear_orphan_state(self) -> str:
        """Nettoie un etat incoherent apres un crash (PID mort, verrou orphelin)."""
        actions: list[str] = []

        runtime = self.runtime_store.load()
        if runtime.pid and not pid_is_alive(runtime.pid):
            runtime.pid = None
            runtime.state = WorkerState.STOPPED
            runtime.last_error = ""
            self.runtime_store.save(runtime)
            actions.append("PID orphelin nettoye")

        owner = self.lock.read_owner()
        if owner and not pid_is_alive(owner):
            self.lock.force_release()
            actions.append("Verrou orphelin libere")

        if BOT_STOP_FLAG.exists() and not self.is_running():
            BOT_STOP_FLAG.unlink(missing_ok=True)
            actions.append("Drapeau d'arret obsolete supprime")

        return " ; ".join(actions) if actions else "Aucun nettoyage necessaire"

    def runtime_snapshot(self) -> BotRuntime:
        return self.runtime_store.load()
