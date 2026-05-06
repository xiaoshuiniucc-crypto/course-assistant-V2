from __future__ import annotations

import atexit
import json
import logging
import msvcrt
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("runtime.single_instance")


class SingleInstanceError(RuntimeError):
    """Raised when another live service instance already owns the lock."""

    def __init__(self, message: str, owner_info: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.owner_info = owner_info or {}


@dataclass
class SingleInstanceGuard:
    """Keeps a process-wide lock file open for the lifetime of the service."""

    project_root: Path
    service_name: str = "course_assistant"

    def __post_init__(self):
        self.project_root = Path(self.project_root)
        self.log_dir = self.project_root / "run_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.log_dir / f"{self.service_name}.lock"
        self.record_path = self.log_dir / f"{self.service_name}.instance.json"
        self.history_path = self.log_dir / f"{self.service_name}.instance.log"
        self._handle = None
        self._acquired = False
        self._registered_atexit = False

    def acquire(self, extra: Optional[Dict[str, Any]] = None) -> "SingleInstanceGuard":
        if self._acquired:
            return self

        self._handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            owner_info = self._read_record()
            self._append_history("lock_denied", owner_info=owner_info)
            self._handle.close()
            self._handle = None
            owner_text = self._format_owner(owner_info)
            raise SingleInstanceError(
                f"Another service instance is already running. {owner_text}",
                owner_info=owner_info,
            ) from exc

        self._acquired = True
        payload = self._build_payload(extra=extra)
        self._write_record(payload)
        self._append_history("started", owner_info=payload)
        logger.info(
            "Single-instance lock acquired: pid=%s record=%s",
            payload["pid"],
            self.record_path,
        )

        if not self._registered_atexit:
            atexit.register(self.release)
            self._registered_atexit = True
        return self

    def release(self):
        if not self._acquired or self._handle is None:
            return

        payload = self._read_record()
        payload["stopped_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_record(payload)
        self._append_history("stopped", owner_info=payload)

        try:
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.debug("Single-instance unlock skipped due to prior handle state.")
        finally:
            self._handle.close()
            self._handle = None
            self._acquired = False
            logger.info("Single-instance lock released")

    def _build_payload(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "service": self.service_name,
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "python": sys.executable,
            "cwd": str(Path.cwd()),
            "project_root": str(self.project_root),
            "hostname": socket.gethostname(),
            "argv": sys.argv,
        }
        if extra:
            payload.update(extra)
        return payload

    def _read_record(self) -> Dict[str, Any]:
        if not self.record_path.exists():
            return {}
        try:
            return json.loads(self.record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_record(self, payload: Dict[str, Any]):
        self.record_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _append_history(self, event: str, owner_info: Optional[Dict[str, Any]] = None):
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "pid": os.getpid(),
            "service": self.service_name,
        }
        if owner_info:
            record["owner"] = owner_info
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")

    @staticmethod
    def _format_owner(owner_info: Optional[Dict[str, Any]]) -> str:
        if not owner_info:
            return "Lock owner details are unavailable."
        pid = owner_info.get("pid", "unknown")
        started_at = owner_info.get("started_at", "unknown")
        appid = owner_info.get("qq_appid", "unknown")
        sandbox = owner_info.get("sandbox", "unknown")
        return (
            f"Owner pid={pid}, started_at={started_at}, "
            f"qq_appid={appid}, sandbox={sandbox}."
        )
