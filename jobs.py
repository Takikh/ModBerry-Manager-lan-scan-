#!/usr/bin/env python3
"""
jobs.py - Gestionnaire de taches longues (deploiement Edge) avec logs en direct.

Chaque job est identifie par une cle (ex: "deploy:10.0.0.26") et conserve :
  - son etat (running / success / error),
  - ses lignes de logs (accessibles par offset pour du polling incremental).
"""

import threading
import time
import uuid

MAX_LOG_LINES = 4000


class Job:
    def __init__(self, key, title, meta=None):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.title = title
        self.meta = meta or {}
        self.lines = []
        self.state = "running"
        self.message = ""
        self.started_at = time.time()
        self.finished_at = None
        self.lock = threading.Lock()

    def log(self, line):
        if line is None:
            return
        with self.lock:
            self.lines.append(f"[{time.strftime('%H:%M:%S')}] {line}" if not line.startswith("[") else line)
            if len(self.lines) > MAX_LOG_LINES:
                del self.lines[: len(self.lines) - MAX_LOG_LINES]

    def finish(self, ok, message):
        with self.lock:
            self.state = "success" if ok else "error"
            self.message = message
            self.finished_at = time.time()
        self.log(("OK: " if ok else "ECHEC: ") + message)

    def snapshot(self, offset=0):
        with self.lock:
            offset = max(0, min(offset, len(self.lines)))
            return {
                "id": self.id,
                "key": self.key,
                "title": self.title,
                "meta": self.meta,
                "state": self.state,
                "message": self.message,
                "running": self.state == "running",
                "lines": self.lines[offset:],
                "next_offset": len(self.lines),
                "total_lines": len(self.lines),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration": round((self.finished_at or time.time()) - self.started_at, 1),
            }


class JobManager:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self._jobs.get(key)

    def is_running(self, key):
        job = self.get(key)
        return bool(job and job.state == "running")

    def start(self, key, title, target, meta=None):
        """
        target(job) -> (ok, message)
        Retourne (started, job)
        """
        with self._lock:
            existing = self._jobs.get(key)
            if existing and existing.state == "running":
                return False, existing
            job = Job(key, title, meta)
            self._jobs[key] = job

        def runner():
            try:
                ok, message = target(job)
                job.finish(bool(ok), message or "")
            except Exception as exception:  # pragma: no cover
                job.finish(False, f"Exception: {exception}")

        threading.Thread(target=runner, daemon=True).start()
        return True, job

    def snapshot(self, key, offset=0):
        job = self.get(key)
        if not job:
            return None
        return job.snapshot(offset)

    def all_keys(self):
        with self._lock:
            return list(self._jobs.keys())


manager = JobManager()
