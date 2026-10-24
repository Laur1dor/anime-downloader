"""Единый async HTTP-клиент (httpx) с общим User-Agent."""
from __future__ import annotations

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def new_client(**kwargs) -> httpx.AsyncClient:
    headers = dict(DEFAULT_HEADERS)
    headers.update(kwargs.pop("headers", {}) or {})
    return httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=kwargs.pop("timeout", 30.0),
        **kwargs,
    )
