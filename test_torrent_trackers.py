import unittest
from src import api, database

class TestTorrentTrackers(unittest.TestCase):
    def test_process_raw_streams_preserves_sources(self):
        raw = [{
            "infoHash": "aef9a66e462f9560f80d381d1a9183553c649743",
            "title": "Jaadugar: A Witch in Mongolia (S01E05)\n👤 14 💾 711 MB",
            "fileIdx": -1,
            "sources": [
                "tracker:udp://tracker.opentrackr.org:1337/announce",
                "tracker:udp://open.stealth.si:80/announce"
            ]
        }]
        processed = api.process_raw_streams(raw)
        self.assertEqual(len(processed), 1)
        st = processed[0]
        self.assertEqual(st.get("sources"), [
            "tracker:udp://tracker.opentrackr.org:1337/announce",
            "tracker:udp://open.stealth.si:80/announce"
        ])

    def test_build_stremio_stream_url(self):
        hash_val = "aef9a66e462f9560f80d381d1a9183553c649743"
        sources = [
            "tracker:udp://tracker.opentrackr.org:1337/announce",
            "tracker:udp://open.stealth.si:80/announce"
        ]
        url = api.build_stremio_stream_url(hash_val, file_index=-1, sources=sources)
        self.assertTrue(url.startswith("http://127.0.0.1:11470/aef9a66e462f9560f80d381d1a9183553c649743/-1?"))
        self.assertIn("tr=tracker%3Audp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce", url)
        self.assertIn("tr=tracker%3Audp%3A%2F%2Fopen.stealth.si%3A80%2Fannounce", url)

    def test_build_magnet_with_sources(self):
        hash_val = "aef9a66e462f9560f80d381d1a9183553c649743"
        sources = ["tracker:udp://tracker.opentrackr.org:1337/announce"]
        mag = api.build_magnet(hash_val, "Jaadugar", sources=sources)
        self.assertIn("xt=urn:btih:aef9a66e462f9560f80d381d1a9183553c649743", mag)
        self.assertIn("tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce", mag)

    def test_database_normalization(self):
        st = {
            "infoHash": "aef9a66e462f9560f80d381d1a9183553c649743",
            "sources": ["tracker:udp://tracker.opentrackr.org:1337/announce"]
        }
        norm = database._normalize_stream_for_storage(st)
        self.assertEqual(norm.get("sources"), ["tracker:udp://tracker.opentrackr.org:1337/announce"])

if __name__ == "__main__":
    unittest.main()
