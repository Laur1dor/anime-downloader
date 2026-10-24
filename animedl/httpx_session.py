"""aiogram-сессия поверх httpx.

Зачем: в некоторых сетях (напр. с DPI-троттлингом Telegram) стандартный
aiohttp-транспорт aiogram получает TLS ConnectionReset на api.telegram.org,
тогда как httpx проходит. Этот транспорт использует httpx и решает проблему без
прокси. Файлы грузятся целиком в память — это ок для облачного лимита (~49 МБ);
для локального Bot API сервера (до 2 ГБ) используйте штатный AiohttpSession.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

import httpx

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods.base import TelegramType

if TYPE_CHECKING:
    from aiogram.client.bot import Bot
    from aiogram.methods import TelegramMethod


class HttpxSession(BaseSession):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=self.timeout,
                                      write=900.0, pool=30.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _build(self, bot: Bot, method: TelegramMethod[TelegramType]):
        data: dict[str, Any] = {}
        input_files: dict[str, Any] = {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=input_files)
            if not prepared:
                continue
            data[key] = prepared
        files: dict[str, tuple] = {}
        for key, input_file in input_files.items():
            chunks = [chunk async for chunk in input_file.read(bot)]
            files[key] = (input_file.filename or key, b"".join(chunks))
        return data, (files or None)

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None,
    ) -> TelegramType:
        client = self._get_client()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        data, files = await self._build(bot, method)
        try:
            resp = await client.post(
                url, data=data, files=files,
                timeout=None if timeout is None else float(timeout),
            )
            raw = resp.text
        except httpx.TimeoutException as e:
            raise TelegramNetworkError(method=method, message="Request timeout error") from e
        except httpx.HTTPError as e:
            raise TelegramNetworkError(method=method, message=f"{type(e).__name__}: {e}") from e
        response = self.check_response(
            bot=bot, method=method, status_code=resp.status_code, content=raw
        )
        return cast(TelegramType, response.result)

    async def stream_content(
        self, url: str, headers: dict[str, Any] | None = None,
        timeout: int = 30, chunk_size: int = 65536, raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        client = self._get_client()
        async with client.stream("GET", url, headers=headers or {}, timeout=timeout) as resp:
            if raise_for_status:
                resp.raise_for_status()
            async for chunk in resp.aiter_bytes(chunk_size):
                yield chunk
