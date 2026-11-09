import unittest
from animedl.players.kodik import manifest_quality
class KodikQualityTests(unittest.TestCase):
 def test_cdn_alias_is_not_720(self):
  self.assertEqual(manifest_quality('#EXTM3U\n#EXTINF:6,\n./480.mp4:hls:seg-1.ts\n./480.mp4:hls:seg-2.ts',720),480)
 def test_no_guess_or_upgrade(self):
  for text in ['not a playlist','#EXTM3U\nsegment.ts','#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n480.mp4','#EXTM3U\n480.mp4:hls:s\n720.mp4:hls:s']:
   self.assertEqual(manifest_quality(text,720),720)
  self.assertEqual(manifest_quality('#EXTM3U\n720.mp4:hls:s',480),480)
