import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.fsm.states import DownloadFlow
from animedl.handlers import flow
from animedl.models import PlayerButton


class AutoQualityFlowTests(unittest.IsolatedAsyncioTestCase):
    def state(self, data):
        async def update(**values):
            data.update(values)
        return SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())

    def data(self):
        return dict(voices=['Voice'], voice='Voice', provider='Kodik',
            ref_buttons=[dict(voice='Voice',provider='Kodik',embed_url='kodik'),
                         dict(voice='Voice',provider='AniBoom',embed_url='boom'),
                         dict(voice='Other',provider='CVH',embed_url='cvh')],
            episodes=[dict(number=1,db_id='1'),dict(number=2,db_id='2')])

    async def test_voice_skips_provider_choice(self):
        data = self.data()
        state = self.state(data)
        cq = SimpleNamespace(data='voice:0', answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()))
        await flow.on_voice(cq, state)
        self.assertEqual(data['provider'], 'auto')
        state.set_state.assert_awaited_with(DownloadFlow.choosing_episodes)
        self.assertNotIn('Плеер', cq.message.edit_text.call_args.args[0])

    async def test_quality_probes_every_player_of_selected_voice(self):
        data = self.data()
        with patch.object(flow, 'qualities_for_buttons', AsyncMock(return_value=[1080,720])) as probe:
            self.assertEqual(await flow._resolve_quality_options(self.state(data)),[1080,720])
        self.assertEqual([b.provider for b in probe.call_args.args[0]], ['Kodik','AniBoom'])
        self.assertEqual(data['provider'],'auto')

    async def test_quality_uses_selected_episode_not_always_episode_one(self):
        data = self.data() | dict(ep_numbers=[2],site='animego',url='url',anime_id='title',title='Title')
        parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=[PlayerButton('Voice','CVH','cvh')]))
        with patch.object(flow,'get_parser',return_value=parser), \
             patch.object(flow,'qualities_for_buttons',AsyncMock(return_value=[1080])) as probe:
            self.assertEqual(await flow._resolve_quality_options(self.state(data)),[1080])
        self.assertEqual(parser.fetch_episode_players.call_args.args[1].number,2)
        self.assertEqual(probe.call_args.args[0][0].provider,'CVH')

    async def test_unchecked_player_is_visible_and_can_be_retried(self):
        data = self.data() | dict(quality_failed_providers=['Alloha'])
        text, kb = await flow._quality_menu(self.state(data),[720,480])
        self.assertIn('Alloha',text)
        self.assertIn('пока не показаны',text)
        self.assertIn('quality_retry',[b.callback_data for row in kb.inline_keyboard for b in row])
