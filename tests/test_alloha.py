import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from animedl.models import PlayerButton, VideoVariant
from animedl.players.alloha import AllohaExtractor, manifest_heights, verify_selection
from animedl.players.base import ExtractError
from animedl.players.registry import extract_variants, provider_supported


class AllohaTests(unittest.IsolatedAsyncioTestCase):
    def test_host_is_exact(self):
        extractor = AllohaExtractor()
        self.assertTrue(extractor.can_handle('//alloha.yani.tv/?episode=1'))
        self.assertFalse(extractor.can_handle('https://alloha.yani.tv.evil.example/'))

    def test_real_manifest_resolutions(self):
        self.assertEqual(manifest_heights('#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\na.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\nb.m3u8'), [1080, 720])
        self.assertEqual(manifest_heights('<html>RESOLUTION=1920x1080</html>'), [])
        self.assertEqual(manifest_heights('#EXTM3U\n#EXTINF:6\nsegment.ts'), [])

    def test_selected_episode_and_voice_must_match(self):
        markup = '''<script>const fileList = JSON.parse('{"active":{"seasons":2,"episode":1,"id_translation":10}}');</script>'''
        verify_selection(markup, 'https://alloha.yani.tv/?season=2&episode=1&translation=10')
        for query in ('season=1', 'episode=0', 'translation=11'):
            with self.subTest(query=query), self.assertRaises(ExtractError):
                verify_selection(markup, 'https://alloha.yani.tv/?' + query)
        with self.assertRaises(ExtractError):
            verify_selection('<html></html>', 'https://alloha.yani.tv/')

    async def test_registry_uses_alloha_instead_of_generic(self):
        button = PlayerButton('AniLibria', 'Alloha', '//alloha.yani.tv/?episode=1')
        variants = [VideoVariant(1080, 'https://cdn.example/master.m3u8', direct=True)]
        self.assertTrue(provider_supported(button))
        with patch.object(AllohaExtractor, '_browser_variants', AsyncMock(return_value=variants)) as browser:
            self.assertEqual(await extract_variants(button), variants)
            browser.assert_awaited_once_with('https://alloha.yani.tv/?episode=1')

    async def test_error_does_not_expose_signed_links(self):
        with patch.object(AllohaExtractor, '_browser_variants', AsyncMock(side_effect=RuntimeError('https://cdn.example/?secret=signed'))):
            with self.assertRaises(ExtractError) as caught:
                await AllohaExtractor().extract('https://alloha.yani.tv/')
        self.assertNotIn('secret', str(caught.exception))

    async def test_timeout_is_reported(self):
        with patch.object(AllohaExtractor, '_browser_variants', AsyncMock(side_effect=asyncio.TimeoutError)):
            with self.assertRaisesRegex(ExtractError, 'время'):
                await AllohaExtractor().extract('https://alloha.yani.tv/')


if __name__ == '__main__':
    unittest.main()
