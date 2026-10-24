import unittest
from animedl.sites.animego import AnimeGoParser

class ScheduleNamesTests(unittest.TestCase):
    def test_names_are_joined_by_episode_number_not_reverse_display_order(self):
        markup = '''<div class="fw-bold">Visit!</div>
        <div data-episode="123" data-number="26">28 September</div>
        <div>25 серия</div><div class="fw-bold">Planning Discussion!</div>
        <div data-episode="124" data-number="25">21 September</div>
        <div data-episode="125" data-number="24">14 September</div>'''
        self.assertEqual(AnimeGoParser._schedule_names(markup), {26: 'Visit!', 25: 'Planning Discussion!'})
