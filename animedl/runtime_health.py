"""Independent liveness file and a drain gate for controlled releases."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from aiogram import BaseMiddleware


class RuntimeHealth(BaseMiddleware):
    def __init__(self, data_dir: Path):
        self.directory = data_dir
        self.active = 0
        self.ready = False
        self.started = time.time()
        self.task = None

    async def __call__(self, handler, event, data):
        if (self.directory / "release.drain").exists():
            # Do not start new work while the current handlers finish.
            try:
                await event.answer("Обновление загрузчика. Повтори запрос через минуту.")
            except Exception:
                pass
            return
        self.active += 1
        try:
            return await handler(event, data)
        finally:
            self.active -= 1

    def write(self):
        from animedl.jobcontrol import ACTIVE
        value = dict(ts=time.time(), started=self.started, pid=os.getpid(),
                     ready=self.ready, active=self.active + len(ACTIVE),
                     draining=(self.directory / "release.drain").exists())
        temporary = self.directory / "runtime-health.tmp"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.directory / "runtime-health.json")

    async def run(self):
        while True:
            try:
                self.write()
            except OSError:
                pass
            await asyncio.sleep(2)

    async def start(self, **kwargs):
        self.ready = True
        self.task = asyncio.create_task(self.run(), name="runtime-health")

    async def close(self):
        self.ready = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.write()
