"""A tiny JSON file that remembers which jobs were seen and what became of them.

One record per job id: status (pending, go, skip or filtered), the title, the
link, the saved draft and the Telegram message id. Old records are dropped
after a while. Writes are atomic (temp file, then rename) and guarded by a lock.

Standard library only.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional


class Store:
    def __init__(self, path: str, keep_days: int = 14, clock: Callable[[], float] = time.time):
        self.path = path
        self.keep_seconds = keep_days * 86400
        self._clock = clock
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except ValueError:
            # A damaged file is moved aside rather than silently overwritten.
            os.replace(self.path, self.path + ".damaged")

    def _save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def has(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._data

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.get(job_id)
            return dict(rec) if rec else None

    def put(self, job_id: str, record: Dict[str, Any]) -> None:
        with self._lock:
            record = dict(record)
            record.setdefault("created", self._clock())
            self._data[job_id] = record
            self._prune()
            self._save()

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id in self._data:
                self._data[job_id].update(fields)
                self._save()

    def _prune(self) -> None:
        limit = self._clock() - self.keep_seconds
        for key in [k for k, v in self._data.items() if v.get("created", 0) < limit]:
            del self._data[key]
