import unittest
from unittest.mock import patch
import httpx
from animedl.search_sources import parse_animego_results, parse_yummy_results, search_sources


CARD = '''<div class="ani-grid__item"><img src="//img.cdngos.com/a.jpg"><div class="ani-grid__item-body"><div class="fw-lighter">Grand Blue Season 2</div><div class="ani-grid__item-title"><a href="/anime/neobyatnyy-okean-2-3000">Необъятный океан 2</a></div></div></div>'''


class SearchParsingTests(unittest.TestCase):
    def test_animego_keeps_distinct_season_aliases(self):
        hits = parse_animego_results(CARD + CARD)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].aliases, ("Grand Blue Season 2",))
        self.assertEqual(hits[0].poster, "https://img.cdngos.com/a.jpg")

    def test_animego_rejects_external_and_character_links(self):
        self.assertEqual(parse_animego_results(CARD.replace('/anime/neobyatnyy-okean-2-3000', 'https://evil.example/anime/fake-10')), [])
        self.assertEqual(parse_animego_results(CARD.replace('/anime/neobyatnyy-okean-2-3000', '/character/fake-10')), [])

    def test_yummy_identity_comes_from_source(self):
        hits = parse_yummy_results({'response': [{'anime_url':'grand-blue-3','title':'Океан 3','remote_ids':{'myanimelist_id':62542},'poster':{'big':'//static.yani.tv/3.webp'}}]})
        self.assertEqual(hits[0].mal_id, 62542)
        self.assertEqual(hits[0].url, 'https://old.yummyani.me/catalog/item/grand-blue-3')
        self.assertEqual(hits[0].poster, 'https://static.yani.tv/3.webp')

    def test_yummy_malformed_rows_and_path(self):
        self.assertEqual(parse_yummy_results({'response':[None,{'anime_url':'../../login','title':'bad'}]}), [])
        with self.assertRaises(ValueError):
            parse_yummy_results({'response': {}})


class SearchNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_keeps_other_results_and_direct_transport(self):
        def transport(request):
            if request.url.host == 'old.yummyani.me':
                return httpx.Response(503)
            if request.url.host == 'animego.me':
                return httpx.Response(200, text=CARD)
            return httpx.Response(200, json=[])
        options = []
        def client_factory(**kwargs):
            options.append(kwargs)
            return httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with patch('animedl.search_sources.new_client', side_effect=client_factory):
            result = await search_sources('Grand Blue')
        self.assertEqual(result.hits[0].site, 'animego')
        self.assertIn('yummyanime', result.errors)
        self.assertFalse(options[0]['trust_env'])

    async def test_catalogue_variants_do_not_relabel_unrelated_results(self):
        def transport(request):
            if request.url.host == 'shikimori.io':
                return httpx.Response(200, json=[{'id':37105,'name':'Grand Blue','russian':'Необъятный океан'}])
            if request.url.host == 'animego.me':
                return httpx.Response(200, text='')
            rows = [] if request.url.params.get('q') == 'Grand Blue' else [{'anime_url':'grand-blue-3','title':'Океан 3','remote_ids':{'myanimelist_id':62542}}]
            return httpx.Response(200, json={'response':rows})
        with patch('animedl.search_sources.new_client', return_value=httpx.AsyncClient(transport=httpx.MockTransport(transport))):
            result = await search_sources('Grand Blue')
        self.assertEqual(result.hits[0].mal_id, 62542)
        self.assertEqual(result.hits[0].aliases, ())

    async def test_empty_query_never_requests(self):
        with patch('animedl.search_sources.new_client') as factory:
            self.assertEqual((await search_sources(' ')).hits, [])
            factory.assert_not_called()
