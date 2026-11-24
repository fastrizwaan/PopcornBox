import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import http_downloader


class TestHttpDownloaderProcess(unittest.TestCase):
    def test_hls_ffmpeg_stderr_cannot_block_worker(self):
        response = MagicMock()
        response.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        response.geturl.return_value = "https://cdn.example.com/video.m3u8"
        process = MagicMock()
        process.poll.return_value = 0
        process.returncode = 0

        with tempfile.TemporaryDirectory() as temp_dir, \
             patch("src.http_downloader.urllib.request.urlopen", return_value=response), \
             patch("src.http_downloader.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("src.http_downloader.subprocess.Popen", return_value=process) as popen, \
             patch("src.http_downloader.database.update_http_download_status"):
            http_downloader._start_worker_thread(
                "http_test_hls", "https://cdn.example.com/video.m3u8",
                str(Path(temp_dir) / "video.mp4"),
            )
            worker = http_downloader._active_downloads["http_test_hls"]["thread"]
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive(), "HLS worker unexpectedly remained blocked")
        self.assertIs(popen.call_args.kwargs["stderr"], http_downloader.subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
