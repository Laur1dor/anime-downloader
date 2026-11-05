import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from animedl.error_messages import error_explanation
from animedl.observability import error_details
from animedl.telegram_client import build_bot


class CallbackAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_ack_does_not_abort_next_handler_action(self):
        # Exercise the registered middleware through an actual Bot session.
        cfg = SimpleNamespace(tg_api_base_url='http://localhost:8081', tg_api_local_mode=True,
                              bot_token='123456:TEST')
        bot = build_bot(cfg)
        ack = AnswerCallbackQuery(callback_query_id='old')
        error = TelegramBadRequest(method=ack,
            message='Bad Request: query is too old and response timeout expired or query ID is invalid')
        bot.session.make_request = AsyncMock(side_effect=error)
        after_ack = AsyncMock()
        try:
            await bot(ack)
            await after_ack()
            after_ack.assert_awaited_once()
            bot.session.make_request.assert_awaited_once()
        finally:
            await bot.session.close()

    async def test_unrelated_bad_requests_are_not_hidden(self):
        from animedl.telegram_client import CallbackAcknowledgementMiddleware
        middleware = CallbackAcknowledgementMiddleware()
        for method, text in ((SendMessage(chat_id=1, text='Hi'), 'query is too old'),
                             (AnswerCallbackQuery(callback_query_id='id'), 'message text is empty')):
            with self.subTest(method=type(method).__name__):
                error = TelegramBadRequest(method=method, message=text)
                with self.assertRaises(TelegramBadRequest):
                    await middleware(AsyncMock(side_effect=error), None, method)

    def test_expired_reason_is_safe_and_quality_probe_is_not_total_failure(self):
        error = TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id='old'),
                                  message='query ID is invalid SECRET')
        details = error_details(error)
        self.assertEqual(details['reason'], 'callback_expired')
        self.assertNotIn('SECRET', str(details))
        text = ' '.join(error_explanation(details, 'handler_error'))
        self.assertIn('не означает сбой скачивания', text)
        text = ' '.join(error_explanation({'error_class': 'ExtractError'}, 'quality_probe_error'))
        self.assertIn('не означает', text)
