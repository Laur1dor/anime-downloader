import unittest
from animedl.models import source_mal_id
from animedl.handlers.flow import _title_from_state

class MetadataIdentityTests(unittest.TestCase):
    def test_only_same_exact_catalogue_links_identify_title(self):
        self.assertEqual(source_mal_id('<a href="//myanimelist.net/anime/60636">MAL</a><a href="https://shikimori.io/animes/60636">Shiki</a>'), 60636)
        self.assertIsNone(source_mal_id('<a href="https://myanimelist.net.evil.test/anime/60636">Fake</a>'))
        self.assertIsNone(source_mal_id('<a href="https://myanimelist.net/anime/1">A</a><a href="https://myanimelist.net/anime/2">B</a>'))

    def test_exact_identity_survives_bot_state(self):
        title = _title_from_state(dict(site='yummyanime', url='url', anime_id='id', title='Title', episodes=[], mal_id=60636))
        self.assertEqual(title.mal_id, 60636)
