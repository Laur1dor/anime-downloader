"""Bounded disk cache of completed downloads whose delivery failed."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .download.downloader import DownloadResult

_LOCK = threading.RLock()


class RecoveryStore:
    def __init__(self, root: Path, max_bytes: int, ttl_seconds: int):
        self.root, self.max_bytes, self.ttl = root, max_bytes, ttl_seconds

    def _entries(self):
        if not self.root.exists():
            return []
        return [p for p in self.root.iterdir() if p.is_dir() and len(p.name) == 32
                and all(c in "0123456789abcdef" for c in p.name)]

    def prune(self):
        with _LOCK:
            entries = []
            for p in self._entries():
                video = p / "video.mp4"
                size = video.stat().st_size if video.exists() else 0
                age = p.stat().st_mtime
                if time.time() - age >= self.ttl:
                    shutil.rmtree(p)
                else:
                    entries.append((age, size, p))
            total = sum(size for _, size, _ in entries)
            for _, size, p in sorted(entries):
                if total <= self.max_bytes:
                    break
                shutil.rmtree(p)
                total -= size

    def load(self, key: str, destination: Path) -> tuple[DownloadResult, str, int] | None:
        with _LOCK:
            self.prune()
            for entry in self._entries():
                try:
                    data = json.loads((entry / "info.json").read_text(encoding="utf-8"))
                    if data["key"] != key:
                        continue
                    source = entry / "video.mp4"
                    if source.stat().st_size != data["size_bytes"]:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source, destination)
                    except OSError:
                        shutil.copyfile(source, destination)
                    return (DownloadResult(destination, data["width"], data["height"],
                        data["duration"], data["size_bytes"], data.get("meta", {})),
                        data["provider"], data["quality"])
                except (OSError, ValueError, KeyError):
                    continue
        return None

    def save(self, key: str, result: DownloadResult, provider: str, quality: int) -> bool:
        with _LOCK:
            self.prune()
            if self.max_bytes <= 0 or result.size_bytes > self.max_bytes:
                return False
            self.root.mkdir(parents=True, exist_ok=True)
            # Reserve before copying; never keep more than the configured media budget.
            entries = sorted(self._entries(), key=lambda p: p.stat().st_mtime)
            used = sum((p / "video.mp4").stat().st_size for p in entries if (p / "video.mp4").exists())
            for p in entries:
                if (used + result.size_bytes <= self.max_bytes and
                        shutil.disk_usage(self.root).free >= result.size_bytes + 256 * 1024 * 1024):
                    break
                used -= (p / "video.mp4").stat().st_size if (p / "video.mp4").exists() else 0
                shutil.rmtree(p)
            if shutil.disk_usage(self.root).free < result.size_bytes + 256 * 1024 * 1024:
                return False
            entry = self.root / uuid.uuid4().hex
            entry.mkdir()
            try:
                shutil.copyfile(result.path, entry / "video.mp4")
                data = dict(key=key, width=result.width, height=result.height,
                    duration=result.duration, size_bytes=result.size_bytes, meta=result.meta,
                    provider=provider, quality=quality)
                (entry / "info.json").write_text(json.dumps(data), encoding="utf-8")
            except BaseException:
                shutil.rmtree(entry)
                raise
            self.prune()
            return entry.exists()

    def discard(self, key: str):
        with _LOCK:
            for p in self._entries():
                try:
                    if json.loads((p / "info.json").read_text(encoding="utf-8"))["key"] == key:
                        shutil.rmtree(p)
                except (OSError, ValueError, KeyError):
                    continue
