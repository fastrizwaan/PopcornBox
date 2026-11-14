import unittest
import tempfile
import os
import sys
import io
import json
import zipfile
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("mpv", MagicMock())

schema_temp_dir = tempfile.TemporaryDirectory()
schema_path = Path("data/io.github.fastrizwaan.PopcornBox.gschema.xml")
if schema_path.exists():
    import subprocess
    dest = Path(schema_temp_dir.name) / "io.github.fastrizwaan.PopcornBox.gschema.xml"
    dest.write_text(schema_path.read_text())
    subprocess.run(["glib-compile-schemas", schema_temp_dir.name], check=True)
    os.environ["GSETTINGS_SCHEMA_DIR"] = schema_temp_dir.name

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib

res_file = Path("build-dir/files/share/popcorn-box/cine.gresource")
if res_file.exists():
    try:
        res = Gio.Resource.load(str(res_file))
        Gio.resources_register(res)
    except Exception:
        pass

from src import database
from src import cloudstream
from src import api


class TestCloudStream(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "test_data.json"
        self.patcher_db = patch.object(database, "DB_FILE", self.db_file)
        self.patcher_db.start()
        database._json_cache = None
        database._json_cache_valid = False

        # Redirect package storage to temp dir
        self.pkg_dir = Path(self.temp_dir.name) / "cloudstream_packages"
        self.patcher_pkg = patch.object(cloudstream, "get_packages_dir", return_value=self.pkg_dir)
        self.patcher_pkg.start()

    def tearDown(self):
        database._json_cache = None
        database._json_cache_valid = False
        self.patcher_pkg.stop()
        self.patcher_db.stop()
        self.temp_dir.cleanup()

    def test_normalize_repository_url(self):
        expected = "https://raw.githubusercontent.com/Kraptor123/cs-kraptor/master/repo.json"
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("https://github.com/Kraptor123/cs-kraptor"),
            expected,
        )
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("cloudstreamrepo://github.com/Kraptor123/cs-kraptor"),
            expected,
        )
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("https://cs.repo/?https://github.com/Kraptor123/cs-kraptor"),
            expected,
        )
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("https://raw.githubusercontent.com/Kraptor123/cs-kraptor/refs/heads/master/repo.json"),
            expected,
        )
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("https://example.com/repo"),
            "https://example.com/repo/repo.json",
        )
        self.assertEqual(
            cloudstream.normalize_cloudstream_repository_url("https://example.com/custom/repo.json"),
            "https://example.com/custom/repo.json",
        )

    def test_resolve_cloudstream_url(self):
        base = "https://example.com/a/b/repo.json"
        self.assertEqual(
            cloudstream.resolve_cloudstream_url(base, "https://cdn.example/plugin.cs3"),
            "https://cdn.example/plugin.cs3",
        )
        self.assertEqual(
            cloudstream.resolve_cloudstream_url(base, "/plugins.json"),
            "https://example.com/plugins.json",
        )
        self.assertEqual(
            cloudstream.resolve_cloudstream_url(base, "../plugins.json"),
            "https://example.com/a/plugins.json",
        )
        self.assertEqual(
            cloudstream.resolve_cloudstream_url(base, "plugins.json"),
            "https://example.com/a/b/plugins.json",
        )

    def test_parse_repository_manifest(self):
        payload = json.dumps({
            "name": "Kraptor Repo",
            "description": "Turkish streaming plugins",
            "manifestVersion": 1,
            "pluginLists": ["plugins.json", "../extra/plugins.json"],
        })
        manifest = cloudstream.parse_repository_manifest("https://example.com/repos/repo.json", payload)
        self.assertEqual(manifest["name"], "Kraptor Repo")
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(
            manifest["plugin_list_urls"],
            [
                "https://example.com/repos/plugins.json",
                "https://example.com/extra/plugins.json",
            ],
        )

    def test_parse_plugin_list_and_merge(self):
        payload1 = json.dumps([
            {
                "url": "../builds/KickTR.cs3",
                "status": 1,
                "version": 15,
                "name": "KickTR",
                "internalName": "KickTR",
                "authors": ["Kraptor"],
                "language": "tr",
                "tvTypes": ["Live"],
                "apiVersion": 1,
                "fileHash": "sha256-60c0dc079c6929347fbf7d6e273d14507712775f4631d15e430065d50d374f6a",
            }
        ])
        payload2 = json.dumps([
            {
                "url": "../builds/KickTR.cs3",
                "status": 1,
                "version": 16,
                "name": "KickTR",
                "internalName": "KickTR",
                "authors": ["Kraptor"],
                "language": "tr",
                "tvTypes": ["Live"],
                "apiVersion": 1,
                "fileHash": "sha256-60c0dc079c6929347fbf7d6e273d14507712775f4631d15e430065d50d374f6a",
            }
        ])

        repo_url = "https://example.com/repo.json"
        list_url = "https://example.com/lists/plugins.json"

        p1 = cloudstream.parse_plugin_list(repo_url, list_url, payload1)
        p2 = cloudstream.parse_plugin_list(repo_url, list_url, payload2)

        self.assertEqual(len(p1), 1)
        self.assertEqual(p1[0]["version"], 15)
        self.assertEqual(p1[0]["package_url"], "https://example.com/builds/KickTR.cs3")

        merged = cloudstream.merge_plugin_lists([p1, p2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["version"], 16)

    def test_cs3_package_inspection_and_safety(self):
        # Create a valid test ZIP
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"pluginClassName": "com.example.Plugin", "version": 1}))
            zf.writestr("classes.dex", b"\x00" * 32)
        valid_bytes = bio.getvalue()

        manifest, entries = cloudstream.inspect_cs3_package(valid_bytes)
        self.assertEqual(manifest.get("pluginClassName"), "com.example.Plugin")
        self.assertIn("manifest.json", entries)
        self.assertIn("classes.dex", entries)

        # Missing manifest.json should fail
        bio_no_manifest = io.BytesIO()
        with zipfile.ZipFile(bio_no_manifest, "w") as zf:
            zf.writestr("classes.dex", b"\x00" * 32)
        with self.assertRaises(ValueError):
            cloudstream.inspect_cs3_package(bio_no_manifest.getvalue())

        # Path traversal should fail
        bio_traverse = io.BytesIO()
        with zipfile.ZipFile(bio_traverse, "w") as zf:
            zf.writestr("manifest.json", "{}")
            zf.writestr("../escape.txt", "evil")
        with self.assertRaises(ValueError):
            cloudstream.inspect_cs3_package(bio_traverse.getvalue())

    def test_file_hash_verification(self):
        data = b"Hello CloudStream!"
        expected_hex = hashlib.sha256(data).hexdigest()
        hash_obj = cloudstream.parse_cloudstream_file_hash(f"sha256-{expected_hex}")
        self.assertIsNotNone(hash_obj)
        self.assertTrue(cloudstream.matches_file_hash(data, hash_obj))

        wrong_hash = cloudstream.parse_cloudstream_file_hash(f"sha256-{'0' * 64}")
        self.assertFalse(cloudstream.matches_file_hash(data, wrong_hash))

    def test_route_id_encoding_and_parsing(self):
        provider_id = "https://raw.githubusercontent.com/Kraptor123/cs-kraptor/master/repo.json#KickTR"
        data = "kick:channel:elraenn"
        route = cloudstream.cloudstream_route_id(provider_id, data)
        self.assertTrue(route.startswith("cloudstream:"))

        parsed = cloudstream.parse_cloudstream_route_id(route)
        self.assertIsNotNone(parsed)
        p_id, d = parsed
        self.assertEqual(p_id, provider_id)
        self.assertEqual(d, data)

    def test_model_conversions(self):
        provider_id = "https://example.com/repo.json#TestProvider"
        search_item = {
            "provider_id": provider_id,
            "data": "channel:test",
            "name": "Test Stream",
            "type": cloudstream.CloudStreamTvType.Live,
            "poster_url": "https://example.com/poster.jpg",
            "background_url": "https://example.com/bg.jpg",
            "description": "Live gaming stream",
            "year": 2026,
        }
        card = cloudstream.search_item_to_card_dict(search_item)
        self.assertEqual(card["title"], "Test Stream")
        self.assertEqual(card["type"], "tv")
        self.assertTrue(card["id"].startswith("cloudstream:"))

        load_item = {
            "provider_id": provider_id,
            "data": "channel:test",
            "name": "Test Stream",
            "type": cloudstream.CloudStreamTvType.Live,
            "poster_url": "https://example.com/poster.jpg",
            "background_url": "https://example.com/bg.jpg",
            "description": "Live gaming stream",
            "tags": ["Gaming", "Live"],
            "episodes": [],
        }
        details = cloudstream.load_item_to_details_dict(load_item)
        self.assertEqual(details["title"], "Test Stream")
        self.assertEqual(details["runtime"], "Live")
        self.assertEqual(len(details["videos"]), 1)

        playback_source = {
            "name": "Kick Live HLS",
            "url": "https://video.kick.com/stream.m3u8",
            "referer": "https://kick.com/test",
            "headers": {"Referer": "https://kick.com/test", "Origin": "https://kick.com"},
            "subtitles": [],
            "is_hls": True,
        }
        stream = cloudstream.playback_source_to_stream_dict(playback_source, "KickTR", provider_id)
        self.assertEqual(stream["name"], "Kick Live HLS")
        self.assertEqual(stream["stream_type"], "hls")
        self.assertEqual(stream["url"], "https://video.kick.com/stream.m3u8")
        self.assertEqual(stream["headers"]["Referer"], "https://kick.com/test")

    def test_database_repository_lifecycle(self):
        manifest_url = "https://example.com/test-repo/repo.json"
        repo_data = {
            "manifest_url": manifest_url,
            "name": "Test Repo",
            "description": "Test CloudStream Repo",
            "plugin_count": 1,
        }
        plugins_data = [{
            "id": f"{manifest_url}#TestPlugin",
            "storage_key": "abc123def456",
            "internal_name": "TestPlugin",
            "name": "Test Plugin",
            "repository_manifest_url": manifest_url,
            "package_url": "https://example.com/TestPlugin.cs3",
            "version": 1,
            "enabled": True,
        }]

        database.save_cloudstream_repository(repo_data)
        database.save_cloudstream_plugins(plugins_data)

        repos = cloudstream.get_installed_repositories()
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "Test Repo")

        plugins = cloudstream.get_all_plugins()
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0]["name"], "Test Plugin")
        self.assertTrue(plugins[0]["enabled"])

        # Test toggle
        cloudstream.toggle_plugin(f"{manifest_url}#TestPlugin", False)
        plugins = cloudstream.get_all_plugins()
        self.assertFalse(plugins[0]["enabled"])

        # Test remove repository
        cloudstream.remove_repository(manifest_url)
        self.assertEqual(len(cloudstream.get_installed_repositories()), 0)
        self.assertEqual(len(cloudstream.get_all_plugins()), 0)

    def test_kick_tr_provider_adapter(self):
        provider = cloudstream.KickTRCloudStreamProvider()
        mock_search_resp = {
            "channels": [
                {
                    "slug": "elraenn",
                    "user": {"username": "Elraenn", "bio": "Turkish streamer"},
                    "profile_picture": "https://example.com/elraenn.jpg",
                }
            ]
        }
        with patch.object(provider, "_fetch_api", return_value=mock_search_resp):
            results = provider.search("elraenn")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["name"], "Elraenn")
            self.assertEqual(results[0]["data"], "kick:channel:elraenn")
            self.assertEqual(results[0]["poster_url"], "https://example.com/elraenn.jpg")

        mock_channel_resp = {
            "slug": "elraenn",
            "user": {"username": "Elraenn", "bio": "Live stream bio"},
            "profile_picture": "https://example.com/elraenn.jpg",
            "livestream": {
                "session_title": "Grand Theft Auto V",
                "categories": {"name": "GTA V"},
            },
            "playback_url": "https://video.kick.com/elraenn/stream.m3u8",
        }
        with patch.object(provider, "_fetch_api", return_value=mock_channel_resp):
            metadata = provider.load("kick:channel:elraenn")
            self.assertEqual(metadata["name"], "Elraenn")
            self.assertIn("GTA V", metadata["tags"])

            sources = provider.load_links("kick:channel:elraenn")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["url"], "https://video.kick.com/elraenn/stream.m3u8")
            self.assertTrue(sources[0]["is_hls"])

    def test_api_cloudstream_details_and_streams(self):
        from src import api
        provider_id = "https://raw.githubusercontent.com/Kraptor123/cs-kraptor/master/repo.json#KickTR"
        route_id = cloudstream.cloudstream_route_id(provider_id, "kick:channel:elraenn")

        mock_channel_resp = {
            "slug": "elraenn",
            "user": {"username": "Elraenn", "bio": "Live stream bio"},
            "profile_picture": "https://example.com/elraenn.jpg",
            "livestream": {
                "session_title": "Grand Theft Auto V",
                "categories": {"name": "GTA V"},
            },
            "playback_url": "https://video.kick.com/elraenn/stream.m3u8",
        }
        provider = cloudstream.get_provider(provider_id)
        self.assertIsNotNone(provider)

        with patch.object(provider, "_fetch_api", return_value=mock_channel_resp):
            # Test details fetching
            details = api.fetch_movie_details(route_id, "tv")
            self.assertEqual(details["id"], route_id)
            self.assertEqual(details["title"], "Elraenn")
            self.assertEqual(details["type"], "tv")
            self.assertEqual(len(details["videos"]), 1)

            # Test streams fetching
            streams = api.get_torrents(route_id, "tv")
            self.assertEqual(len(streams), 1)
            stream = streams[0]
            self.assertEqual(stream["name"], "Kick Live HLS")
            self.assertEqual(stream["addon_name"], "KickTR")
            self.assertEqual(stream["stream_type"], "hls")
            self.assertEqual(stream["url"], "https://video.kick.com/elraenn/stream.m3u8")
            self.assertEqual(stream["headers"]["Referer"], "https://kick.com/elraenn")

            # Also verify get_torrents_streamed
            callback_results = []
            api.get_torrents_streamed(
                route_id,
                "tv",
                callback=lambda res, is_cached, is_complete: callback_results.extend(res),
            )
            self.assertEqual(len(callback_results), 1)
            self.assertEqual(callback_results[0]["url"], "https://video.kick.com/elraenn/stream.m3u8")

    def test_mega_provider_adapter(self):
        provider = cloudstream.get_provider("https://raw.githubusercontent.com/self-similarity/MegaRepo/builds/repo.json#MegaProvider")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "MegaProvider")

        mock_repos_db = json.dumps([
            "https://raw.githubusercontent.com/test/repo1/repo.json",
            {"url": "https://raw.githubusercontent.com/test/repo2/repo.json"},
        ])
        with patch("src.cloudstream._http_get_text", return_value=mock_repos_db), \
             patch("src.cloudstream.add_repository", return_value=(True, "ok", {})) as mock_add:
            added, total, errors = provider.sync_repositories()
            self.assertEqual(added, 2)
            self.assertEqual(total, 2)
            self.assertEqual(mock_add.call_count, 2)

    def test_additional_cloudstream_providers_and_fallback(self):
        # Twitch
        twitch = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/builds/TwitchProvider.cs3#TwitchProvider")
        self.assertIsNotNone(twitch)
        t_items = twitch.search("shroud")
        self.assertEqual(len(t_items), 1)
        self.assertIn("shroud", t_items[0]["data"])
        t_sources = twitch.load_links("twitch:channel:shroud")
        self.assertEqual(len(t_sources), 1)
        self.assertTrue(t_sources[0]["is_hls"])

        # InternetArchive
        ia = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/builds/InternetArchiveProvider.cs3#InternetArchiveProvider")
        self.assertIsNotNone(ia)

        # Dailymotion
        dm = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/builds/DailymotionProvider.cs3#DailymotionProvider")
        self.assertIsNotNone(dm)

        # YouTube
        yt = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/builds/YoutubeProvider.cs3#YoutubeProvider")
        self.assertIsNotNone(yt)

        # Generic Fallback for unknown plugin is NOT runnable (Android DEX only)
        generic = cloudstream.get_provider("https://example.com/repo.json#UnknownPlugin")
        self.assertIsNotNone(generic)
        self.assertEqual(generic.name, "UnknownPlugin")
        self.assertFalse(cloudstream.is_provider_runnable({"id": "https://example.com/repo.json#UnknownPlugin"}))

    def test_hdhub4u_provider(self):
        provider = cloudstream.get_provider("https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#HDhub4u")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.name, "HDhub4u")

        # Mock pingora search response
        mock_pingora = json.dumps({
            "found": 1,
            "hits": [{
                "document": {
                    "post_title": "Inception (2010) Dual Audio",
                    "permalink": "https://new12.hdhub4u.cl/inception-2010/",
                    "post_thumbnail": "https://example.com/inception.jpg"
                }
            }]
        })

        with patch.object(provider, "_get_base_url", return_value="https://new12.hdhub4u.cl"), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_pingora.encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            items = provider.search("Inception")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "Inception (2010) Dual Audio")
            self.assertEqual(items[0]["year"], 2010)

        # Mock post page with HubCDN and HubDrive
        post_html = """
        <html>
            <h1>Inception (2010) Dual Audio</h1>
            <a href="https://hubcdn.wiki/file/abc1234">480p Download</a>
            <a href="https://hubdrive.blog/file/xyz5678">1080p Download</a>
        </html>
        """
        hubcdn_html = 'var reurl = "https://inventoryidea.com/?r=aHR0cHM6Ly9odWJjZG4uY2x1Yi9kbC8/bGluaz1odHRwczovL3ZpZGVvLmNvbS9pbmNlcHRpb24ubWt2";'

        with patch("src.cloudstream._http_get_text", return_value=post_html), \
             patch("src.cloudstream._extract_hubcdn_direct", return_value="https://video.com/inception.mkv"):
            sources = provider.load_links("hdhub4u:https://new12.hdhub4u.cl/inception-2010/")
            self.assertTrue(any("inception.mkv" in s.get("url", "") for s in sources))

    def test_moviesdrive_and_cinestream_providers(self):
        md = cloudstream.get_provider("https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#MoviesDrive")
        self.assertIsNotNone(md)
        self.assertEqual(md.name, "MoviesDrive")

        cs = cloudstream.get_provider("https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#CineStream")
        self.assertIsNotNone(cs)
        self.assertEqual(cs.name, "CineStream")

        yts = cloudstream.get_provider("https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#YTS")
        self.assertIsNotNone(yts)
        self.assertEqual(yts.name, "YTS")
        self.assertIn("torrent", yts.supported_types)

    def test_url_spaces_and_control_characters(self):
        url_with_space = "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC Verse.cs3"
        safe = cloudstream._safe_url(url_with_space)
        self.assertEqual(
            safe,
            "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC%20Verse.cs3",
        )
        resolved = cloudstream.resolve_cloudstream_url(
            "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/repo.json",
            "CNC Verse.cs3",
        )
        self.assertEqual(
            resolved,
            "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC%20Verse.cs3",
        )

    def test_yts_torrent_stream_handling(self):
        yts = cloudstream.get_provider("YTS")
        self.assertIsNotNone(yts)

        fake_api_data = {
            "status": "ok",
            "data": {
                "movie_count": 1,
                "movies": [{
                    "id": 1606,
                    "imdb_code": "tt1375666",
                    "title": "Inception",
                    "title_long": "Inception (2010)",
                    "year": 2010,
                    "torrents": [
                        {
                            "hash": "CE9156EB497762F8B7577B71C0647A4B0C3423E1",
                            "quality": "720p",
                            "type": "bluray",
                            "video_codec": "x264",
                            "seeds": 81,
                            "peers": 12,
                            "size": "1.07 GB",
                            "size_bytes": 1148903752,
                        },
                        {
                            "hash": "224BF45881252643DFC2E71ABC7B2660A21C68C4",
                            "quality": "1080p",
                            "type": "bluray",
                            "video_codec": "x264",
                            "seeds": 100,
                            "peers": 20,
                            "size": "1.85 GB",
                            "size_bytes": 1986422374,
                        },
                    ],
                }],
            },
        }

        with patch.object(yts, "_request_json", return_value=fake_api_data):
            results = yts.search("Inception", imdb_id="tt1375666")
            self.assertEqual(len(results), 1)
            item = results[0]
            self.assertEqual(item["name"], "Inception (2010)")

            sources = yts.load_links(item["data"])
            self.assertEqual(len(sources), 2)
            first_src = sources[0]
            self.assertTrue(first_src["is_torrent"])
            self.assertFalse(first_src["is_http"])
            self.assertEqual(first_src["info_hash"], "ce9156eb497762f8b7577b71c0647a4b0c3423e1")
            self.assertEqual(first_src["seeds"], 81)

            stream_dict = cloudstream.playback_source_to_stream_dict(first_src, "YTS", yts.id)
            self.assertTrue(stream_dict["is_torrent"])
            self.assertFalse(stream_dict["is_http"])
            self.assertEqual(stream_dict["stream_type"], "torrent")
            self.assertTrue(stream_dict["magnet"].startswith("magnet:?xt=urn:btih:ce9156eb497762f8b7577b71c0647a4b0c3423e1"))
            self.assertEqual(stream_dict["infoHash"], "ce9156eb497762f8b7577b71c0647a4b0c3423e1")
            self.assertEqual(stream_dict["seeders"], 81)

    def test_cloudstream_stream_resolution_in_api(self):
        from src import api
        mock_stream = [{
            "name": "HDhub4u 1080P",
            "title": "HDhub4u 1080P · 1080p",
            "url": "https://video.example.com/inception_1080p.mkv",
            "quality": "1080p",
            "headers": {},
            "subtitles": [],
            "behaviorHints": {},
            "addon_name": "HDhub4u",
            "addon_names": ["HDhub4u"],
            "is_cloudstream": True,
            "is_http": True,
            "stream_type": "direct",
            "cloudstream_provider_id": "hdhub4u",
        }]

        with patch("src.cloudstream.is_cloudstream_enabled", return_value=True), \
             patch("src.cloudstream.get_all_plugins", return_value=[{"id": "hdhub4u", "enabled": True}]), \
             patch("src.cloudstream.is_provider_runnable", return_value=True), \
             patch("src.cloudstream.fetch_cloudstream_streams_for_media", return_value=mock_stream):
            results = api.get_torrents_streamed("tt1375666", media_type="movie", title="Inception", expected_year="2010")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["addon_name"], "HDhub4u")
            self.assertEqual(results[0]["url"], "https://video.example.com/inception_1080p.mkv")
            self.assertTrue(results[0]["is_http"])
            self.assertTrue(results[0]["is_cloudstream"])

    def test_torrentio_provider(self):
        prov = cloudstream.get_provider("https://raw.githubusercontent.com/DieGon7771/ItaliaInStreaming/builds/repo.json#Torrentio")
        self.assertIsNotNone(prov)
        self.assertIsInstance(prov, cloudstream.TorrentioCloudStreamProvider)
        self.assertEqual(cloudstream.get_provider("Torrentio"), prov)

        mock_torrentio_json = json.dumps({
            "streams": [
                {
                    "name": "Torrentio\n1080p",
                    "title": "Inception 2010 1080p BluRay x264\n👤 250 💾 2.15 GB ⚙️ 1337x",
                    "infoHash": "2849211f150e966ef7924f43aec22727a644e10d",
                    "fileIdx": 0,
                    "behaviorHints": {
                        "bingeGroup": "torrentio|1080p",
                        "filename": "Inception.2010.1080p.mkv"
                    }
                }
            ]
        })

        with patch.object(cloudstream, "_http_get_text", return_value=mock_torrentio_json):
            sources = prov.get_streams_for_media("Inception", "movie", imdb_id="tt1375666")
            self.assertEqual(len(sources), 1)
            s = sources[0]
            self.assertTrue(s["is_torrent"])
            self.assertFalse(s["is_http"])
            self.assertEqual(s["info_hash"], "2849211f150e966ef7924f43aec22727a644e10d")
            self.assertEqual(s["seeds"], 250)
            self.assertEqual(s["size"], "2.15 GB")
            self.assertEqual(s["file_idx"], 0)

            stream_dict = cloudstream.playback_source_to_stream_dict(s, "Torrentio", prov.id)
            self.assertTrue(stream_dict["is_torrent"])
            self.assertFalse(stream_dict["is_http"])
            self.assertEqual(stream_dict["stream_type"], "torrent")
            self.assertEqual(stream_dict["infoHash"], "2849211f150e966ef7924f43aec22727a644e10d")
            self.assertEqual(stream_dict["seeders"], 250)
            self.assertEqual(stream_dict["file_idx"], 0)

    def test_cncverse_provider(self):
        prov1 = cloudstream.get_provider("https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CNC Verse")
        self.assertIsNotNone(prov1)
        self.assertIsInstance(prov1, cloudstream.CNCVerseCloudStreamProvider)

        prov2 = cloudstream.get_provider("https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CNC Verse Mobile")
        self.assertIsNotNone(prov2)
        self.assertIsInstance(prov2, cloudstream.CNCVerseCloudStreamProvider)

        mock_search_json = json.dumps({
            "searchResult": [{"id": "0HBUB1JMA08CJWY8V76RCRPTSP", "t": "Inception", "y": "2010"}]
        }).encode("utf-8")

        mock_player_json = json.dumps({
            "status": "otp",
            "ott": "pv",
            "video_link": "https://tv.imgcdn.kim/newtv/hls/pv/0HBUB1JMA08CJWY8V76RCRPTSP.m3u8?in=dummy_sig",
            "referer": "https://net52.cc"
        }).encode("utf-8")

        class MockResponse:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def mock_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "search.php" in url:
                return MockResponse(mock_search_json)
            if "player.php" in url:
                return MockResponse(mock_player_json)
            return MockResponse(b"{}")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen), \
             patch.object(prov1, "_get_cookie", return_value="t_hash_t=dummy_cookie"):
            sources = prov1.get_streams_for_media("Inception", "movie", year="2010")
            self.assertEqual(len(sources), 1)
            s = sources[0]
            self.assertTrue(s["is_hls"])
            self.assertTrue(s["is_http"])
            self.assertFalse(s["is_torrent"])
            self.assertEqual(s["url"], "https://tv.imgcdn.kim/newtv/hls/pv/0HBUB1JMA08CJWY8V76RCRPTSP.m3u8?in=dummy_sig")
            self.assertEqual(s["headers"]["Referer"], "https://net52.cc/")

            stream_dict = cloudstream.playback_source_to_stream_dict(s, "CNC Verse", prov1.id)
            self.assertTrue(stream_dict["is_http"])
            self.assertFalse(stream_dict["is_torrent"])
            self.assertEqual(stream_dict["stream_type"], "hls")

            # Verify that non-matching searches (e.g. The Dog Stars) or Top Searches fallback NEVER return wrong streams
            top_searches_json = json.dumps({
                "status": "n",
                "head": "Top Searches",
                "searchResult": [{"id": "80122209", "t": "Unrelated Trending Movie"}]
            }).encode("utf-8")

            def mock_urlopen_nomatch(req, timeout=None):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "search.php" in url:
                    return MockResponse(top_searches_json)
                return MockResponse(b"{}")

            with patch("urllib.request.urlopen", side_effect=mock_urlopen_nomatch):
                no_match_sources = prov1.get_streams_for_media("The Dog Stars", "movie", year="2026")
                self.assertEqual(no_match_sources, [])

    def test_dudefilms_and_zinkmovies_providers(self):
        dudefilms = cloudstream.get_provider("DudeFilms")
        self.assertIsNotNone(dudefilms)
        self.assertIsInstance(dudefilms, cloudstream.DudeFilmsCloudStreamProvider)

        zinkmovies = cloudstream.get_provider("Zinkmovies")
        self.assertIsNotNone(zinkmovies)
        self.assertIsInstance(zinkmovies, cloudstream.ZinkmoviesCloudStreamProvider)

        fake_dudefilms_html = (
            '<article><h2><a href="https://new1.dudefilms.church/inception-2010/">'
            'Inception (2010) Dual Audio Hindi-English 720p</a></h2></article>'
        )
        with patch.object(cloudstream, "_http_get_text", return_value=fake_dudefilms_html):
            results = dudefilms.search("Inception")
            self.assertEqual(len(results), 1)
            self.assertIn("Inception", results[0]["name"])

    def test_ninekmovies_provider(self):
        ninek = cloudstream.get_provider("9kMovies")
        self.assertIsNotNone(ninek)
        self.assertIsInstance(ninek, cloudstream.NineKMoviesCloudStreamProvider)
        self.assertNotEqual(ninek.id, cloudstream.DudeFilmsCloudStreamProvider.id)

        repo_key = "https://raw.githubusercontent.com/redowan99/Redowan-CloudStream/master/repo.json#9kMovies"
        prov_by_repo = cloudstream.get_provider(repo_key)
        self.assertIsInstance(prov_by_repo, cloudstream.NineKMoviesCloudStreamProvider)

        fake_figure_html = (
            '<figure>'
            '<img src="https://example.com/poster.jpg" alt="Stree 2 (2024) Hindi HDRip 1080p Download">'
            '<figcaption>'
            '<a href="https://the9kmovies.com/stree-2-2024-hindi-1080p/"><p>Stree 2 (2024) Hindi HDRip 1080p Download</p></a>'
            '</figcaption>'
            '</figure>'
        )
        with patch.object(cloudstream, "_http_get_text", return_value=fake_figure_html):
            results = ninek.search("Stree 2")
            self.assertEqual(len(results), 1)
            self.assertIn("Stree 2", results[0]["name"])
            self.assertEqual(results[0]["year"], 2024)
            self.assertEqual(results[0]["type"], cloudstream.CloudStreamTvType.Movie)
            self.assertEqual(results[0]["poster_url"], "https://example.com/poster.jpg")
            self.assertIn("9kmovies:", results[0]["data"])

            loaded = ninek.load(results[0]["data"])
            self.assertIn("Stree 2", loaded["name"])

    def test_strict_title_match_and_search_scoring(self):
        # Strict title matching logic
        self.assertTrue(cloudstream._is_strict_title_match("The Dog Stars", "The Dog Stars (2026) 1080p"))
        self.assertTrue(cloudstream._is_strict_title_match("Inception", "Inception (2010) Dual Audio"))
        self.assertTrue(cloudstream._is_strict_title_match("Wednesday", "Wednesday (Season 1)"))
        self.assertTrue(cloudstream._is_strict_title_match("It", "It (2017) Bluray"))

        # Non-matching titles should be rejected
        self.assertFalse(cloudstream._is_strict_title_match("The Dog Stars", "Ohh My Dog (2026)"))
        self.assertFalse(cloudstream._is_strict_title_match("The Dog Stars", "Up Among The Stars"))
        self.assertFalse(cloudstream._is_strict_title_match("The Dog Stars", "7 Dogs (2026)"))
        self.assertFalse(cloudstream._is_strict_title_match("Inception", "Interstellar (2014)"))


    def test_dailymotion_provider(self):
        dm = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/master/repo.json#DailymotionProvider")
        self.assertIsNotNone(dm)
        self.assertIsInstance(dm, cloudstream.DailymotionCloudStreamProvider)
        self.assertEqual(dm.name, "DailymotionProvider")

        # Test ID extraction
        self.assertEqual(cloudstream._extract_dailymotion_id("dm:x9hgu82"), "x9hgu82")
        self.assertEqual(cloudstream._extract_dailymotion_id("https://www.dailymotion.com/video/x9hgu82"), "x9hgu82")
        self.assertEqual(cloudstream._extract_dailymotion_id("https://www.dailymotion.com/embed/video/x9hgu82?autoplay=1"), "x9hgu82")
        self.assertEqual(cloudstream._extract_dailymotion_id("x9hgu82"), "x9hgu82")

        # Mock search API response
        mock_search = json.dumps({
            "list": [
                {
                    "id": "x9hgu82",
                    "title": "Test Video Title",
                    "description": "A sample description",
                    "thumbnail_360_url": "https://example.com/thumb.jpg"
                }
            ]
        })
        with patch.object(cloudstream, "_http_get_text", return_value=mock_search):
            items = dm.search("test")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["data"], "dm:x9hgu82")
            self.assertEqual(items[0]["name"], "Test Video Title")

            main_page = dm.get_main_page(1)
            self.assertEqual(len(main_page), 1)
            self.assertEqual(main_page[0][0], "Popular")
            self.assertEqual(len(main_page[0][1]), 1)

        # Mock load API response
        mock_load = json.dumps({
            "id": "x9hgu82",
            "title": "Test Video Title",
            "description": "A sample description",
            "thumbnail_720_url": "https://example.com/poster.jpg"
        })
        with patch.object(cloudstream, "_http_get_text", return_value=mock_load):
            loaded = dm.load("https://www.dailymotion.com/video/x9hgu82")
            self.assertEqual(loaded["name"], "Test Video Title")
            self.assertEqual(loaded["poster_url"], "https://example.com/poster.jpg")

        # Test load_links with mock player metadata
        mock_meta = json.dumps({
            "qualities": {
                "auto": [{"type": "application/x-mpegURL", "url": "https://cdndirector.dailymotion.com/cdn/manifest/video/x9hgu82.m3u8"}]
            },
            "subtitles": {
                "data": {
                    "en": {"label": "English", "urls": ["https://example.com/en.srt"]}
                }
            }
        }).encode("utf-8")

        mock_master_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
            "https://vod2.cf.dmcdn.net/sec2(abc)/cloud/stream.m3u8\n"
        ).encode("utf-8")

        class MockResp:
            def __init__(self, data):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def mock_open(req, *args, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "metadata" in url:
                return MockResp(mock_meta)
            if "manifest" in url:
                return MockResp(mock_master_m3u8)
            return MockResp(b"")

        with patch("shutil.which", return_value=None), \
             patch("http.cookiejar.CookieJar"), \
             patch("urllib.request.build_opener") as mock_bo:
            mock_opener = MagicMock()
            mock_opener.open.side_effect = mock_open
            mock_bo.return_value = mock_opener

            links = dm.load_links("dm:x9hgu82")
            self.assertEqual(len(links), 1)
            self.assertTrue(links[0]["is_hls"])
            self.assertIn(".m3u8", links[0]["url"])
            self.assertEqual(len(links[0]["subtitles"]), 1)
            self.assertEqual(links[0]["subtitles"][0]["language"], "English")


    def test_youtube_provider(self):
        yt = cloudstream.get_provider("https://raw.githubusercontent.com/recloudstream/extensions/master/repo.json#YoutubeProvider")
        self.assertIsNotNone(yt)
        self.assertIsInstance(yt, cloudstream.YoutubeCloudStreamProvider)
        self.assertEqual(yt.name, "YoutubeProvider")

        # YouTube must not intercept standard movie or series playback
        self.assertNotIn("movie", yt.supported_types)
        self.assertNotIn("series", yt.supported_types)

        # Test ID extraction
        self.assertEqual(cloudstream._extract_youtube_id("yt:6MTk5Fr5_h4"), "6MTk5Fr5_h4")
        self.assertEqual(cloudstream._extract_youtube_id("https://www.youtube.com/watch?v=6MTk5Fr5_h4"), "6MTk5Fr5_h4")
        self.assertEqual(cloudstream._extract_youtube_id("https://youtu.be/6MTk5Fr5_h4"), "6MTk5Fr5_h4")
        self.assertEqual(cloudstream._extract_youtube_id("yt:search:Spider-Man: Brand New Day"), "")
        self.assertEqual(cloudstream._extract_youtube_id("Brand New Day"), "")

        # Test load_links on invalid data
        invalid_links = yt.load_links("yt:search:Spider-Man: Brand New Day")
        self.assertEqual(invalid_links, [])

        # Test load_links on valid data
        valid_links = yt.load_links("yt:6MTk5Fr5_h4")
        self.assertEqual(len(valid_links), 1)
        self.assertIn("6MTk5Fr5_h4", valid_links[0]["url"])

        # Test playback_source_to_stream_dict drops invalid YouTube URLs
        bad_source = {"name": "YouTube", "url": "https://www.youtube.com/watch?v=Brand%20New%20Day"}
        stream_dict = cloudstream.playback_source_to_stream_dict(bad_source, "YouTube", yt.id)
        self.assertIsNone(stream_dict)

    def test_cloudstream_catalogs_list_on_selection_only(self):
        from src.window import CineWindow
        win = MagicMock(spec=CineWindow)
        win.current_sort = 'Recent'
        win.current_genre = None
        win.current_year = None
        win.current_language = None
        win.current_country = None
        win.current_network = None
        win._discover_catalog_list_cache = {}
        win._discover_catalog_list_cache_timestamps = {}

        # 1. Do not list CloudStream catalogs with "all"
        rows_all = CineWindow._get_discover_catalog_list(win, filter_media_type='all', filter_addon_url=None)
        cs_rows_all = [r for r in rows_all if r.get('_source') == 'cloudstream' or str(r.get('manifest_url', '')).startswith('cloudstream://')]
        self.assertEqual(len(cs_rows_all), 0, "CloudStream catalogs must not be listed with all")

        # 2. List CloudStream catalogs on selection
        cats = cloudstream.get_catalogs('all')
        if cats:
            target_url = cats[0]['manifest_url']
            rows_sel = CineWindow._get_discover_catalog_list(win, filter_media_type='all', filter_addon_url=target_url)
            cs_rows_sel = [r for r in rows_sel if r.get('_source') == 'cloudstream' or str(r.get('manifest_url', '')).startswith('cloudstream://')]
            self.assertGreater(len(cs_rows_sel), 0, "CloudStream catalogs must be listed when provider is selected")
            for r in cs_rows_sel:
                self.assertEqual(r.get('manifest_url'), target_url)

    def test_doh_redirect_image_fetch(self):
        from src import api
        # YTS image redirects with 301 from yts.gg to img.yts.gg
        url = 'https://yts.gg/assets/images/movies/the_future_2023/large-cover.jpg'
        data = api._doh_fetch(url, timeout=6.0)
        self.assertIsNotNone(data)
        self.assertGreater(len(data), 1000)
        self.assertEqual(data[:2], b'\xff\xd8')

    def test_hdhub4u_and_yts_catalog_sections(self):
        hdhub = cloudstream.HDhub4uCloudStreamProvider()
        main_page = hdhub.get_main_page(1)
        self.assertIsInstance(main_page, list)
        self.assertGreater(len(main_page), 0)

        yts = cloudstream.YTSCloudStreamProvider()
        yts_page = yts.get_main_page(1)
        self.assertIsInstance(yts_page, list)
        self.assertGreater(len(yts_page), 0)
        for _, items in yts_page:
            for item in items:
                p_url = item.get("poster_url") or ""
                self.assertNotIn("://yts.gg/assets/images/", p_url)

    def test_clean_cloudstream_title(self):
        clean, year = cloudstream.clean_cloudstream_title(
            "Duplicity (2009) BluRay [Hindi (DD5.1) & English] 1080p 720p & 480p Dual Audio [x264] | Full Movie"
        )
        self.assertEqual(clean, "Duplicity")
        self.assertEqual(year, "2009")

        clean2, year2 = cloudstream.clean_cloudstream_title(
            "The Dark Heaven (2026) WEB-DL [Hindi (DD2.0) &#038; Tamil] 1080p 720p &#038; 480p Dual Audio [x264/HEVC] | Full Movie"
        )
        self.assertEqual(clean2, "The Dark Heaven")
        self.assertEqual(year2, "2026")

        clean3, year3 = cloudstream.clean_cloudstream_title(
            "Bigg Boss (Season 20) WEB-DL [Hindi 2.0] 1080p 720p &#038; 480p [x264] HD | [JioHotStar Series] [EP-63 Added] [7th Nov]"
        )
        self.assertEqual(clean3, "Bigg Boss")
        self.assertIsNone(year3)

    def test_search_item_to_card_dict_preserves_catalog_title(self):
        raw_name = "Duplicity (2009) BluRay [Hindi (DD5.1) & English] 1080p 720p & 480p Dual Audio [x264] | Full Movie"
        item = {
            "provider_id": "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#HDhub4u",
            "data": "https://new12.hdhub4u.cl/duplicity-2009-hindi-bluray-full-movie/",
            "name": raw_name,
            "type": cloudstream.CloudStreamTvType.Movie,
            "poster_url": "https://example.com/poster.jpg",
            "background_url": "https://example.com/bg.jpg",
            "description": "Duplicity synopsis",
            "year": 2009,
        }
        card = cloudstream.search_item_to_card_dict(item)
        self.assertEqual(card["title"], "Duplicity")
        self.assertEqual(card["catalog_title"], raw_name)
        self.assertEqual(card["raw_title"], raw_name)
        self.assertEqual(card["year"], "2009")
        self.assertTrue(card["is_cloudstream"])

    def test_hdhub4u_greenmotors_short_link_resolution(self):
        # Mock greenmotors HTML with base64/rot13 s('o', ...) payload
        import base64, codecs, json
        target_hblinks = "https://hblinks.lol/archives/119061"
        payload_dict = {"w": 10, "l": "https://greenmotors.cc/homelander/", "o": base64.b64encode(target_hblinks.encode()).decode()}
        d3 = base64.b64encode(json.dumps(payload_dict).encode()).decode()
        d2 = codecs.encode(d3, 'rot_13')
        d1 = base64.b64encode(d2.encode()).decode()
        raw_b64 = base64.b64encode(d1.encode()).decode()

        mock_html = f"<html><body><script>s('o','{raw_b64}',180000);</script></body></html>"
        with patch.object(cloudstream, "_http_get_text", return_value=mock_html):
            dest = cloudstream._resolve_hdhub4u_short_link("https://greenmotors.club/?id=test12345")
            self.assertEqual(dest, target_hblinks)

    def test_hdhub4u_load_links_with_greenmotors(self):
        hdhub = cloudstream.HDhub4uCloudStreamProvider()
        page_html = """
        <html><body>
        <div class="page-body">
            <h3><a href="https://greenmotors.club/?id=test12345">1080p x264 [2.2GB]</a></h3>
        </div>
        </body></html>
        """
        with patch.object(cloudstream, "_http_get_text", return_value=page_html), \
             patch.object(cloudstream, "_resolve_hdhub4u_short_link", return_value="https://hblinks.lol/archives/119061"), \
             patch.object(cloudstream, "_extract_hblinks_sources", return_value=[{"name": "Server : 10Gbps", "url": "https://stream.example.com/video.mkv"}]):
            sources = hdhub.load_links("hdhub4u:https://new12.hdhub4u.cl/duplicity-2009-hindi-bluray-full-movie/")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["quality"], 1080)
            self.assertIn("1080P", sources[0]["name"])
            self.assertEqual(sources[0]["url"], "https://stream.example.com/video.mkv")

    def test_provider_catalog_sections_defined(self):
        providers = [
            cloudstream.BollyflixCloudStreamProvider(),
            cloudstream.MoviesDriveCloudStreamProvider(),
            cloudstream.VegaMoviesCloudStreamProvider(),
            cloudstream.MoviesmodCloudStreamProvider(),
            cloudstream.NineKMoviesCloudStreamProvider(),
            cloudstream.DudeFilmsCloudStreamProvider(),
            cloudstream.ZinkmoviesCloudStreamProvider(),
            cloudstream.CineStreamCloudStreamProvider(),
            cloudstream.HDhub4uCloudStreamProvider(),
            cloudstream.YTSCloudStreamProvider(),
        ]
        for p in providers:
            secs = getattr(p, "catalog_sections", ())
            self.assertTrue(len(secs) >= 1, f"Provider {p.name} should define catalog_sections")
            sec_ids = [s.get("id") for s in secs]
            if p.name != "YTS":
                self.assertIn("latest-movies", sec_ids, f"{p.name} missing latest-movies")
                self.assertIn("latest-series", sec_ids, f"{p.name} missing latest-series")

    def test_cloudstream_catalogs_suffix(self):
        mock_plugins = [
            {"id": "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#Bollyflix", "name": "Bollyflix", "enabled": True},
            {"id": "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#MoviesDrive", "name": "MoviesDrive", "enabled": True},
            {"id": "https://raw.githubusercontent.com/SaurabhKaperwan/CSX/builds/CS.json#VegaMovies", "name": "VegaMovies", "enabled": True},
            {"id": "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#HDhub4u", "name": "HDhub4u", "enabled": True},
            {"id": "https://raw.githubusercontent.com/phisher98/cloudstream-extensions-phisher/builds/repo.json#YTS", "name": "YTS", "enabled": True},
        ]
        with patch("src.cloudstream.is_cloudstream_enabled", return_value=True), \
             patch("src.cloudstream.get_all_plugins", return_value=mock_plugins):
            cats = cloudstream.get_catalogs("all")
            self.assertTrue(len(cats) > 0)
            for c in cats:
                addon_name = c.get("addon_name") or ""
                self.assertTrue(addon_name.endswith(" (CloudStream)"), f"{addon_name} missing (CloudStream) suffix")
                self.assertFalse(addon_name.endswith(" (CloudStream) (CloudStream)"), f"Duplicate suffix in {addon_name}")

    def test_clean_cloudstream_title_boilerplate_stripping(self):
        t1, y1 = cloudstream.clean_cloudstream_title("Download Jailer 2 (2026) Dual Audio {Hindi-Tamil} Movie 480p | 720p | 1080p HDTC")
        self.assertEqual(t1, "Jailer 2")
        self.assertEqual(y1, "2026")

        t2, y2 = cloudstream.clean_cloudstream_title("Permanent Link to The Dark Heaven (2026) Dual audio {Hindi-Tamil} 480p 720p 1080p")
        self.assertEqual(t2, "The Dark Heaven")
        self.assertEqual(y2, "2026")

        t3, y3 = cloudstream.clean_cloudstream_title("Download Other Mommy (2026) PrimeVideo : Dual Audio {Hindi-English} 480p HD 720p & 1080p x264 WEB-DL")
        self.assertEqual(t3, "Other Mommy")
        self.assertEqual(y3, "2026")

    def test_is_cloudstream_series(self):
        self.assertTrue(cloudstream.is_cloudstream_series("Love in Disguise (Season 1)"))
        self.assertTrue(cloudstream.is_cloudstream_series("Loki S02 1080p"))
        self.assertTrue(cloudstream.is_cloudstream_series("Game of Thrones Season 1-8"))
        self.assertTrue(cloudstream.is_cloudstream_series("Show Name", "https://example.com/shows/season-2"))
        self.assertTrue(cloudstream.is_cloudstream_series("My Hero Academia", catalog_type="series"))
        self.assertFalse(cloudstream.is_cloudstream_series("Inception (2010)", "https://example.com/movies/inception", "movie"))

    def test_clean_cloudstream_title_series(self):
        t1, y1 = cloudstream.clean_cloudstream_title("Love in Disguise (Season 1) (2026)")
        self.assertEqual(t1, "Love in Disguise")
        self.assertEqual(y1, "2026")

        t2, y2 = cloudstream.clean_cloudstream_title("House of the Dragon Season 2 [1080p]")
        self.assertEqual(t2, "House of the Dragon")

    def test_parse_archive_episodes(self):
        sample_html = """
        <div class="entry-content">
            <h5>EP01 - 720p</h5>
            <a href="https://hubcloud.club/drive/ep1">HubCloud 720p</a>
            <a href="https://pixeldrain.dev/u/ep1pixel">PixelDrain</a>
            <h5>EP02 - 720p</h5>
            <a href="https://hubcloud.club/drive/ep2">HubCloud 720p</a>
        </div>
        """
        eps = cloudstream.parse_archive_episodes(sample_html)
        self.assertIn(1, eps)
        self.assertIn(2, eps)
        self.assertEqual(len(eps[1]), 2)
        self.assertEqual(len(eps[2]), 1)
        self.assertEqual(eps[1][0][0], "https://hubcloud.club/drive/ep1")

    def test_load_item_to_details_dict_series_fallback(self):
        item = {
            "provider_id": "test_prov",
            "data": "test_data",
            "name": "Love in Disguise (Season 1)",
            "type": cloudstream.CloudStreamTvType.Other,
            "episodes": [],
        }
        details = cloudstream.load_item_to_details_dict(item)
        self.assertEqual(details["type"], "series")
        self.assertEqual(details["title"], "Love in Disguise")
        self.assertEqual(len(details["videos"]), 1)
        self.assertEqual(details["videos"][0]["season"], 1)
        self.assertEqual(details["videos"][0]["episode"], 1)

    def test_immediate_cloudstream_route_fetch(self):
        route_id = "cloudstream:80:https://example.com/repo.json#MoviesDrivemoviesdrive:https://example.com/item"
        mock_streams = [{
            "name": "MoviesDrive S1E2 1080p",
            "url": "https://example.com/stream.mp4",
            "is_http": True,
            "quality": "1080p",
        }]
        with patch("src.cloudstream.get_streams_for_route", return_value=mock_streams) as mock_get_streams:
            callback_batches = []
            res = api.get_torrents_streamed(
                "tt9999999:1:2",
                "series",
                1,
                2,
                callback=lambda batch, is_cached, is_complete: callback_batches.append((batch, is_complete)),
                source_scope="direct",
                cloudstream_route_id=route_id,
                title="Test Show",
            )
            mock_get_streams.assert_called_once_with(route_id, season=1, episode=2)
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["url"], "https://example.com/stream.mp4")
            self.assertTrue(len(callback_batches) >= 1)
            self.assertTrue(callback_batches[0][1])

    def test_js_unpacker(self):
        packed = "eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+c+'\\\\b','g'),k[c]);return p}('0 1=2;',3,3,'var|foo|bar'.split('|'),0,{}))"
        self.assertTrue(cloudstream.JsUnpacker.detect(packed))
        unpacked = cloudstream.JsUnpacker.unpack(packed)
        self.assertIn("var foo=bar;", unpacked)

    def test_pixeldrain_extractor(self):
        url = "https://pixeldrain.com/u/abcdef12"
        streams = cloudstream.extract_pixeldrain_stream(url)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["url"], "https://pixeldrain.com/api/file/abcdef12?download")
        self.assertEqual(streams[0]["name"], "PixelDrain")

    def test_streamtape_extractor(self):
        mock_html = """
        <script>
        document.getElementById('botlink').innerHTML = '//streamtape.com/get_video?id=xyz123&expires=99999&ip=1.2.3.4&token=abcd';
        </script>
        """
        with patch.object(cloudstream, "_http_get_text", return_value=mock_html):
            streams = cloudstream.extract_streamtape("https://streamtape.com/v/xyz123")
            self.assertEqual(len(streams), 1)
            self.assertTrue(streams[0]["url"].startswith("https://streamtape.com/get_video"))
            self.assertIn("&stream=1", streams[0]["url"])

    def test_mixdrop_extractor(self):
        packed_payload = "eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+c+'\\\\b','g'),k[c]);return p}('0.1=\"//2.3/4.5\";',6,6,'wurl|src|s-delivery|mixdrop|video|mp4'.split('|'),0,{}))"
        mock_html = f"<html><script>{packed_payload}</script></html>"
        with patch.object(cloudstream, "_http_get_text", return_value=mock_html):
            streams = cloudstream.extract_mixdrop("https://mixdrop.co/f/xyz")
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0]["url"], "https://s-delivery.mixdrop/video.mp4")

    def test_extract_video_streams_dispatcher(self):
        streams = cloudstream.extract_video_streams("https://pixeldrain.com/u/sample789")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0]["url"], "https://pixeldrain.com/api/file/sample789?download")

    def test_cloudstream_tv_type_int_and_wire(self):
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire(1), cloudstream.CloudStreamTvType.Movie)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire("1"), cloudstream.CloudStreamTvType.Movie)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire(3), cloudstream.CloudStreamTvType.TvSeries)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire("3"), cloudstream.CloudStreamTvType.TvSeries)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire(5), cloudstream.CloudStreamTvType.Anime)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire(10), cloudstream.CloudStreamTvType.Live)
        self.assertEqual(cloudstream.CloudStreamTvType.from_wire("Live"), cloudstream.CloudStreamTvType.Live)

    def test_parse_cloudstream_file_hash(self):
        raw_hex = "a" * 64
        h1 = cloudstream.parse_cloudstream_file_hash(raw_hex)
        self.assertEqual(h1["algorithm"], "sha256")
        self.assertEqual(h1["hex"], raw_hex)
        h2 = cloudstream.parse_cloudstream_file_hash(f"sha256-{raw_hex}")
        self.assertEqual(h2["algorithm"], "sha256")
        self.assertEqual(h2["hex"], raw_hex)

    def test_parse_archive_episodes_high_and_diverse_tags(self):
        html = """
        <div>
            <h2>Episode 105 - Finale</h2>
            <a href="https://example.com/ep105.mkv">Download</a>
            <p><strong>EP 500</strong></p>
            <a href="https://example.com/ep500.mkv">Download 500</a>
        </div>
        """
        eps = cloudstream.parse_archive_episodes(html)
        self.assertIn(105, eps)
        self.assertIn(500, eps)
        self.assertEqual(eps[105][0][0], "https://example.com/ep105.mkv")
        self.assertEqual(eps[500][0][0], "https://example.com/ep500.mkv")

    def test_clean_cloudstream_title_codecs(self):
        t1, y1 = cloudstream.clean_cloudstream_title("Sample Movie (2024) 1080p HEVC x265 ESub")
        self.assertEqual(t1, "Sample Movie")
        self.assertEqual(y1, "2024")

        t2, _ = cloudstream.clean_cloudstream_title("Series Title S01 720p H264 AAC Multi Subs")
        self.assertEqual(t2, "Series Title")

    def test_load_item_to_details_dict_live(self):
        item = {
            "provider_id": "live_prov",
            "data": "live_data",
            "name": "Live Channel",
            "type": cloudstream.CloudStreamTvType.Live,
            "episodes": [],
        }
        details = cloudstream.load_item_to_details_dict(item)
        self.assertEqual(details["type"], "tv")
        self.assertEqual(details["videos"][0]["title"], "Live Stream")

    def test_fetch_cloudstream_streams_for_single_season_show(self):
        mock_provider = MagicMock()
        mock_provider.id = "mock_provider"
        mock_provider.name = "Mock Provider"
        mock_provider.supported_types = ["series"]
        mock_provider.search.return_value = [{"data": "mock_data", "name": "Solo Leveling", "type": "series"}]
        mock_provider.load.return_value = {
            "name": "Solo Leveling",
            "type": "series",
            "episodes": [
                {"name": "Episode 1", "episode": 1, "season": None, "data": "ep1_data"},
                {"name": "Episode 2", "episode": 2, "season": None, "data": "ep2_data"},
            ]
        }
        mock_provider.load_links.return_value = [{"name": "Stream 1", "url": "https://example.com/ep2.mp4"}]

        mock_plugin = {"id": "mock_provider", "name": "Mock Provider", "enabled": True}

        with patch.object(cloudstream, "is_cloudstream_enabled", return_value=True), \
             patch.object(cloudstream, "get_all_plugins", return_value=[mock_plugin]), \
             patch.object(cloudstream, "is_provider_runnable", return_value=True), \
             patch.object(cloudstream, "get_provider", return_value=mock_provider), \
             patch.object(cloudstream, "get_plugin", return_value=mock_plugin):

            streams = cloudstream.fetch_cloudstream_streams_for_media(
                title="Solo Leveling",
                media_type="series",
                season=1,
                episode=2,
            )
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0]["url"], "https://example.com/ep2.mp4")

    def test_get_streams_for_route_filters_none(self):
        mock_provider = MagicMock()
        mock_provider.name = "Mock Provider"
        mock_provider.load_links.return_value = [
            {"name": "Bad URL", "url": ""},
            {"name": "Valid Stream", "url": "https://example.com/valid.mp4"},
        ]
        with patch.object(cloudstream, "get_provider", return_value=mock_provider), \
             patch.object(cloudstream, "get_plugin", return_value={"name": "Mock Provider"}):
            route_id = "cloudstream:13:mock_provider#item_data"
            streams = cloudstream.get_streams_for_route(route_id)
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0]["url"], "https://example.com/valid.mp4")

    def test_clean_cloudstream_title_bollyflix_and_languages(self):
        cases = [
            ("The Vvaan (2026) Hindi Movie", ("The Vvaan", "2026")),
            ("Download The Vvaan (2026) Hindi Movie ~ BollyFlix", ("The Vvaan", "2026")),
            ("Dune: Part Two (2024) 4K Ultra HD Dual Audio (Hindi-English) - MoviesDrive", ("Dune: Part Two", "2024")),
            ("Squid Game (2021) Season 2 Complete Hindi Dubbed Web Series ~ MoviesMod", ("Squid Game", "2021")),
            ("Leo (2023) Tamil Movie [1080p WEB-DL]", ("Leo", "2023")),
            ("Panchayat S03 (2024) Hindi Web Series - HDHub4u", ("Panchayat", "2024")),
            ("Scary Movie (2000)", ("Scary Movie", "2000")),
        ]
        for raw, expected in cases:
            res = cloudstream.clean_cloudstream_title(raw)
            self.assertEqual(res, expected, f"Failed for raw title: {raw!r}")

    def test_playback_source_to_stream_dict_drops_relative_and_category_urls(self):
        # Category/navigation URL must be rejected
        cat_src = {"name": "Bollyflix 1080p", "url": "/movies-by-quality/1080p-movies-download-mkv/"}
        res1 = cloudstream.playback_source_to_stream_dict(cat_src, "Bollyflix", "bollyflix_id")
        self.assertIsNone(res1)

        # Non-HTTP/non-magnet URL must be rejected
        bad_src = {"name": "Bad URL", "url": "ftp://files.example.com/video.mp4"}
        res2 = cloudstream.playback_source_to_stream_dict(bad_src, "Bollyflix", "bollyflix_id")
        self.assertIsNone(res2)

        # Valid HTTP stream must be accepted
        good_src = {"name": "Bollyflix 1080p (MixDrop)", "url": "https://server.example.com/stream.mp4", "quality": 1080}
        res3 = cloudstream.playback_source_to_stream_dict(good_src, "Bollyflix", "bollyflix_id")
        self.assertIsNotNone(res3)
        self.assertEqual(res3["url"], "https://server.example.com/stream.mp4")

    def test_extract_video_streams_excludes_locker_page_urls(self):
        # Locker web pages ending in .mkv must not be treated as direct playable streams
        locker_url = "https://megaup.net/12345/The_Vvaan_(2026)_Hindi_1080p.mkv"
        res = cloudstream.extract_video_streams(locker_url)
        self.assertEqual(res, [])

        # Direct media files on normal CDNs must be recognized
        direct_url = "https://cdn.example.com/media/file.mp4"
        res2 = cloudstream.extract_video_streams(direct_url)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["url"], direct_url)

    def test_bollyflix_load_links_omits_category_navigation(self):
        provider = cloudstream.BollyflixCloudStreamProvider()
        fake_html = """
        <html>
        <body>
            <a href="/movies-by-quality/1080p-movies-download-mkv/">1080p</a>
            <a href="/how-to-download/">How To Download</a>
            <h2>The Vvaan (2026) Hindi 1080p [2.7GB]</h2>
            <a href="https://linksmod.top/view/fake123">🔗 Download Links</a>
        </body>
        </html>
        """
        fake_linksmod_html = """
        <html>
        <body>
            <a href="https://multiup.io/en/mirror/fake456">MultiUp Mirror</a>
        </body>
        </html>
        """
        def mock_get_text(url, *args, **kwargs):
            if "bollyflix" in url:
                return fake_html
            if "linksmod" in url:
                return fake_linksmod_html
            return ""

        with patch.object(cloudstream, "_http_get_text", side_effect=mock_get_text), \
             patch.object(cloudstream, "_extract_multiup_mirrors", return_value=[{
                 "name": "MixDrop",
                 "url": "https://server.mxcontent.net/v2/stream.mp4",
                 "headers": {"User-Agent": "Mozilla/5.0"},
                 "is_hls": False,
             }]):
            sources = provider.load_links("bollyflix:https://bollyflix.church/the-vvaan-2026-hindi-movie/")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]["url"], "https://server.mxcontent.net/v2/stream.mp4")
            self.assertIn("1080p", sources[0]["name"])
            self.assertNotIn("/movies-by-quality/", [s["url"] for s in sources])

    def test_cinedoze_provider_registered_and_runnable(self):
        prov = cloudstream.get_provider("CineDozeProvider")
        self.assertIsNotNone(prov)
        self.assertIsInstance(prov, cloudstream.CineDozeCloudStreamProvider)
        self.assertTrue(cloudstream.is_provider_runnable({"id": "CineDozeProvider"}))

        fake_html = """
        <html><body>
            <article>
                <a href="https://cinedoze.tv/movies/jailer-2-2026-hindi/">
                    <img src="https://cinedoze.tv/poster.jpg" alt="Jailer 2 (2026) Dual Audio" />
                </a>
                <div class="title"><a href="https://cinedoze.tv/movies/jailer-2-2026-hindi/">Jailer 2 (2026) Dual Audio</a></div>
            </article>
        </body></html>
        """
        with patch.object(cloudstream, "_http_get_text", return_value=fake_html):
            sections = prov.get_main_page(1)
            self.assertTrue(len(sections) > 0)
            self.assertEqual(sections[0][0], "Latest Movies")
            items = sections[0][1]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "Jailer 2 (2026) Dual Audio")
            self.assertEqual(items[0]["year"], 2026)

    def test_cinetv_provider_registered_and_runnable(self):
        prov = cloudstream.get_provider("CineTvProvider")
        self.assertIsNotNone(prov)
        self.assertIsInstance(prov, cloudstream.CNCVerseCloudStreamProvider)
        self.assertTrue(cloudstream.is_provider_runnable({"id": "CineTvProvider"}))
        self.assertTrue(cloudstream.is_provider_runnable({
            "id": "https://raw.githubusercontent.com/NivinCNC/CNCVerse-Cloud-Stream-Extension/builds/CNC.json#CineTvProvider"
        }))

    def test_cinevood_provider_registered_and_runnable(self):
        prov = cloudstream.get_provider("CineVoodProvider")
        self.assertIsNotNone(prov)
        self.assertIsInstance(prov, cloudstream.CineVoodCloudStreamProvider)
        self.assertTrue(cloudstream.is_provider_runnable({"id": "CineVoodProvider"}))

    def test_multicloudlinks_extractor(self):
        page_html = """
        <html><head>
        <script>
            const streamSrc = "https://dr1.multidownload.click/d/test1234?eid=ABC&action=watch";
        </script>
        </head><body>
        <a href="https://new3.multicloudlinks.com/player.php?id=xyz">Player</a>
        </body></html>
        """
        with patch.object(cloudstream, "_http_get_text", return_value=page_html):
            sources = cloudstream._extract_multicloudlinks_sources("https://new3.multicloudlinks.com/view/test")
            self.assertTrue(len(sources) >= 1)
            self.assertEqual(sources[0]["url"], "https://dr1.multidownload.click/d/test1234?eid=ABC&action=watch")
            self.assertIn("Direct Stream", sources[0]["name"])

    def test_generic_cloudstream_provider_base_url_discovery(self):
        plugin_dict = {
            "id": "https://example.com/repo.json#TestPlugin",
            "name": "TestPlugin",
            "icon_url": "https://custom-movies.site/logo.png",
            "is_installed": True,
        }
        with patch.object(cloudstream, "get_plugin", return_value=plugin_dict):
            prov = cloudstream.get_provider("https://example.com/repo.json#TestPlugin")
            self.assertEqual(prov.get_base_url(), "https://custom-movies.site")
            self.assertEqual(prov.catalog_sections, ())
            self.assertFalse(cloudstream.is_provider_runnable(plugin_dict))

        # When no base URL is discoverable and no provider class is registered
        empty_plugin = {
            "id": "https://example.com/repo.json#EmptyPlugin",
            "name": "EmptyPlugin",
            "icon_url": "https://github.com/logo.png",
            "is_installed": False,
        }
        with patch.object(cloudstream, "get_plugin", return_value=empty_plugin):
            prov2 = cloudstream.get_provider("https://example.com/repo.json#EmptyPlugin")
            self.assertIsNone(prov2.get_base_url())
            self.assertEqual(prov2.catalog_sections, ())
            self.assertFalse(cloudstream.is_provider_runnable(empty_plugin))

    def test_cloudstream_provider_health_and_offline_exclusion(self):
        pid = "CineDozeProvider"
        self.assertTrue(cloudstream.is_provider_online(pid))

        # Mark offline
        cloudstream.mark_provider_status(pid, False)
        self.assertFalse(cloudstream.is_provider_online(pid))

        # An unrunnable generic provider is never online
        self.assertFalse(cloudstream.is_provider_online("nonexistent_generic_provider"))

        # Restore online
        cloudstream.mark_provider_status(pid, True)
        self.assertTrue(cloudstream.is_provider_online(pid))

    def test_install_plugin_auto_refresh_on_hash_mismatch(self):
        # Create a valid minimal cs3 zip package
        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as z:
            z.writestr("manifest.json", json.dumps({"name": "TestPlugin", "pluginClassName": "com.test.TestPlugin"}))
            z.writestr("classes.dex", b"dex\n035\x00" + b"http://example.com")
        pkg_bytes = bio.getvalue()
        real_hash = hashlib.sha256(pkg_bytes).hexdigest()

        stale_plugin = {
            "id": "https://example.com/repo.json#TestPlugin",
            "name": "TestPlugin",
            "package_url": "https://example.com/TestPlugin.cs3",
            "repository_manifest_url": "https://example.com/repo.json",
            "storage_key": "test_storage_key",
            "version": 1,
            "file_hash": {"algorithm": "sha256", "hex": "0000000000000000000000000000000000000000000000000000000000000000"},
            "is_installed": False,
        }

        refreshed_plugin = dict(stale_plugin)
        refreshed_plugin["version"] = 2
        refreshed_plugin["file_hash"] = {"algorithm": "sha256", "hex": real_hash}

        current_plugin = [stale_plugin]

        def mock_get_plugin(pid):
            return current_plugin[0]

        def mock_refresh_repo(url):
            current_plugin[0] = refreshed_plugin
            return True, "Refreshed", {}

        with patch.object(cloudstream, "_http_get_bytes", return_value=pkg_bytes), \
             patch.object(cloudstream, "get_plugin", side_effect=mock_get_plugin), \
             patch.object(cloudstream, "refresh_repository", side_effect=mock_refresh_repo), \
             patch.object(database, "update_cloudstream_plugin") as mock_update, \
             patch.object(cloudstream, "save_package_atomically"):
            ok, msg = cloudstream.install_plugin("https://example.com/repo.json#TestPlugin")
            self.assertTrue(ok)
            self.assertIn("Installed", msg)
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0][1]
            self.assertEqual(call_args["installed_version"], 2)
            self.assertTrue(call_args["is_installed"])


if __name__ == "__main__":
    unittest.main()
