"""Tests for the vocal-mode choice, stem classification and video ingest (no models needed;
ingest tests need ffmpeg on PATH and are skipped otherwise)."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mv_karaoke.vocals import VocalMode, VocalModeRequired, ask_mode, parse_mode, resolve_mode

try:
    import numpy  # noqa: F401
    import soundfile  # noqa: F401
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False


class TTY(io.StringIO):
    def isatty(self):
        return True


class TestVocalChoice(unittest.TestCase):
    def test_no_silent_default(self):
        with self.assertRaises(VocalModeRequired):
            resolve_mode(None, interactive=False)

    def test_explicit(self):
        self.assertIs(resolve_mode("lead", interactive=False), VocalMode.LEAD)
        self.assertIs(parse_mode("tüm"), VocalMode.ALL)
        with self.assertRaises(ValueError):
            parse_mode("some")

    def test_interactive_prompt(self):
        out = io.StringIO()
        self.assertIs(ask_mode("tr", TTY("x\n2\n"), out), VocalMode.LEAD)
        self.assertIn("Vokaller nasıl silinsin?", out.getvalue())
        self.assertIs(ask_mode("en", TTY("1\n"), io.StringIO()), VocalMode.ALL)

    def test_interactive_empty_answer_aborts(self):
        with self.assertRaises(VocalModeRequired):
            ask_mode("tr", TTY("\n"), io.StringIO())


class TestClassify(unittest.TestCase):
    def test_by_name(self):
        from mv_karaoke.separate import classify_outputs
        inst, voc = classify_outputs(["/t/song_(Vocals)_m.wav", "/t/song_(Instrumental)_m.wav"])
        self.assertEqual((inst.name, voc.name), ("song_(Instrumental)_m.wav", "song_(Vocals)_m.wav"))

    @unittest.skipUnless(HAVE_AUDIO, "numpy/soundfile not installed")
    def test_ambiguous_names_use_bass_energy(self):
        import numpy as np
        import soundfile as sf
        from mv_karaoke.separate import classify_outputs
        sr = 44100
        t = np.arange(sr * 12) / sr
        bassy = 0.5 * np.sin(2 * np.pi * 60 * t) + 0.1 * np.sin(2 * np.pi * 900 * t)
        voice = 0.4 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 1300 * t)
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "s_(Karaoke)_m.wav", Path(d) / "s_(Other)_m.wav"
            sf.write(a, voice.astype("float32"), sr)
            sf.write(b, bassy.astype("float32"), sr)
            inst, voc = classify_outputs([str(a), str(b)])
            self.assertEqual((inst, voc), (b, a))


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class TestFileLink(unittest.TestCase):
    """A non-YouTube link is downloaded as a plain file (local HTTP server, no internet)."""

    @classmethod
    def setUpClass(cls):
        import functools
        import http.server
        import os
        import threading
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "Sanatçı - Şarkı.mp4")], check=True)
        (root / "page.html").write_text("<html>not a video</html>", encoding="utf-8")
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        handler = functools.partial(Quiet, directory=str(root))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._noproxy = os.environ.get("no_proxy"), os.environ.get("NO_PROXY")
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

    @classmethod
    def tearDownClass(cls):
        import os
        cls.server.shutdown()
        cls.dir.cleanup()
        for k, v in zip(("no_proxy", "NO_PROXY"), cls._noproxy):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_video_link(self):
        import urllib.parse
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "w"
            logs = []
            m = ingest(f"{self.base}/{urllib.parse.quote('Sanatçı - Şarkı.mp4')}", work, log=logs.append)
            self.assertEqual((m.width, m.height), (320, 240))
            self.assertEqual((m.artist, m.title), ("Sanatçı", "Şarkı"))
            self.assertEqual(m.video.name, "original.mp4")
            self.assertTrue(any("downloading file" in x for x in logs))
            self.assertFalse(any("yt-dlp" in x for x in logs))
            logs.clear()
            ingest(f"{self.base}/{urllib.parse.quote('Sanatçı - Şarkı.mp4')}", work, log=logs.append)
            self.assertTrue(any("already downloaded" in x for x in logs))

    def test_web_page_link_is_refused(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                ingest(f"{self.base}/page.html", Path(d) / "w", log=lambda *_: None)
            self.assertIn("not a video file", str(cm.exception))

    def test_missing_file(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                ingest(f"{self.base}/yok.mp4", Path(d) / "w", log=lambda *_: None)


class TestDownloadHelpers(unittest.TestCase):
    def test_canonical_url(self):
        from mv_karaoke.ingest import canonical_url, fingerprint
        want = "https://www.youtube.com/watch?v=abcdefghijk"
        for u in ["https://youtu.be/abcdefghijk?si=XYZ", "https://www.youtube.com/watch?v=abcdefghijk&t=42s",
                  "https://m.youtube.com/watch?feature=share&v=abcdefghijk", "https://youtube.com/shorts/abcdefghijk",
                  "https://www.youtube.com/watch?v=abcdefghijk&list=PL123&index=3"]:
            self.assertEqual(canonical_url(u), want, u)
        self.assertEqual(fingerprint("https://youtu.be/abcdefghijk?si=1"), fingerprint(want))
        self.assertEqual(canonical_url("https://vimeo.com/123"), "https://vimeo.com/123")

    def test_format_selector(self):
        from mv_karaoke.ingest import format_selector
        self.assertEqual(format_selector(None), "bv*+ba/b")
        self.assertIn("height<=720", format_selector(720))


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class TestDownload(unittest.TestCase):
    """yt-dlp replaced by a fake module that writes a small real video: no network needed."""

    def setUp(self):
        import sys
        import types
        self.calls = []
        test = self

        class DownloadError(Exception):
            pass

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=True):
                test.calls.append((url, self.opts))
                if "fail" in url:
                    raise DownloadError("ERROR: [youtube] fail: Sign in to confirm your age")
                out = self.opts["outtmpl"]["default"].replace("%(ext)s", "mkv")
                subprocess.run(["ffmpeg", "-y", "-v", "error",
                                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                                "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out], check=True)
                return {"id": "abcdefghijk", "title": "Bir Klip (Official Video)", "uploader": "Sanatçı - Topic",
                        "webpage_url": url, "height": 240, "duration": 2}

        fake = types.ModuleType("yt_dlp")
        fake.YoutubeDL = YoutubeDL
        self._saved = sys.modules.get("yt_dlp")
        sys.modules["yt_dlp"] = fake

    def tearDown(self):
        import sys
        if self._saved is not None:
            sys.modules["yt_dlp"] = self._saved
        else:
            sys.modules.pop("yt_dlp", None)

    def test_link_is_downloaded_once(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "w"
            m = ingest("https://youtu.be/abcdefghijk?si=A", work, log=lambda *_: None, max_height=720)
            self.assertEqual((m.width, m.height), (320, 240))
            self.assertEqual(m.artist, "Sanatçı")
            self.assertEqual(m.source_url, "https://www.youtube.com/watch?v=abcdefghijk")
            self.assertTrue(m.audio.exists())
            opts = self.calls[0][1]
            self.assertIn("height<=720", opts["format"])
            self.assertTrue(opts["noplaylist"])
            # same video, different share link: no second download
            ingest("https://www.youtube.com/watch?v=abcdefghijk&t=10", work, log=lambda *_: None)
            self.assertEqual(len(self.calls), 1)

    def test_other_link_clears_previous_download(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "w"
            ingest("https://youtu.be/abcdefghijk", work, log=lambda *_: None)
            ingest("https://youtu.be/zyxwvutsrqp", work, log=lambda *_: None)
            self.assertEqual(len(self.calls), 2)

    def test_failure_explains_what_to_do(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                ingest("https://www.youtube.com/watch?v=failfailfai", Path(d) / "w", log=lambda *_: None)
            self.assertIn("pip install -U yt-dlp", str(cm.exception))

    def test_only_youtube_links_use_ytdlp(self):
        from mv_karaoke.ingest import is_youtube
        for u in ["https://youtu.be/abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk",
                  "https://m.youtube.com/shorts/abcdefghijk", "https://music.youtube.com/watch?v=abcdefghijk"]:
            self.assertTrue(is_youtube(u), u)
        for u in ["https://example.com/klip.mp4", "https://notyoutube.com/x.mp4",
                  "https://cdn.site.com/youtube.com/x.mp4"]:
            self.assertFalse(is_youtube(u), u)


@unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg not installed")
class TestIngest(unittest.TestCase):
    def make_video(self, d: Path, audio_delay: float = 0.0) -> Path:
        """3 s test video; with audio_delay the audio stream starts later than the video."""
        out = d / "Sanatçı - Şarkı.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3"]
        if audio_delay:                       # container-level offset: audio start_time > 0
            cmd += ["-itsoffset", str(audio_delay)]
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
        subprocess.run(cmd, check=True)
        return out

    def test_local_file(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            v = self.make_video(Path(d))
            m = ingest(str(v), Path(d) / "work", log=lambda *_: None)
            self.assertEqual((m.width, m.height, round(m.fps)), (320, 240, 25))
            self.assertEqual((m.artist, m.title), ("Sanatçı", "Şarkı"))
            self.assertTrue(m.audio.exists())
            meta = json.loads((Path(d) / "work" / "media.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["title"], "Şarkı")

    @unittest.skipUnless(HAVE_AUDIO, "numpy/soundfile not installed")
    def test_audio_start_offset_is_applied(self):
        import numpy as np
        import soundfile as sf
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            v = self.make_video(Path(d), audio_delay=0.5)
            m = ingest(str(v), Path(d) / "work", log=lambda *_: None)
            self.assertGreater(m.audio_offset, 0.4)
            x, sr = sf.read(str(m.audio), always_2d=True)
            onset = int(np.argmax(np.abs(x[:, 0]) > 0.05)) / sr
            self.assertAlmostEqual(onset, m.audio_offset, delta=0.06)   # tone starts where the video says

    def test_changed_input_clears_cache(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            v = self.make_video(d)
            work = d / "work"
            ingest(str(v), work, log=lambda *_: None)
            (work / "stems").mkdir()
            (work / "stems" / "x.wav").write_bytes(b"old")
            (work / "lyrics.txt").write_text("kullanıcının dosyası", encoding="utf-8")
            v2 = d / "other.mp4"
            shutil.copy(v, v2)
            ingest(str(v2), work, log=lambda *_: None)
            self.assertFalse((work / "stems").exists())
            self.assertEqual((work / "lyrics.txt").read_text(encoding="utf-8"), "kullanıcının dosyası")

    def test_rejects_non_video(self):
        from mv_karaoke.ingest import ingest
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "song.mp3"
            p.write_bytes(b"x")
            with self.assertRaises(SystemExit):
                ingest(str(p), Path(d) / "w", log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
