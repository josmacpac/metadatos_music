"""Pruebas de tagmusic. Generan FLAC de 1 segundo con ffmpeg; no tocan música real."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tagmusic  # noqa: E402


def make_flac(path: Path, tags: dict[str, str] | None = None) -> None:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
    ]
    for key, value in (tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True)


def make_cover(path: Path, color: str = "red", size: int = 64) -> None:
    """Genera una imagen pequeña de prueba (JPEG o PNG según la extensión)."""
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}x{size}:d=1",
            "-frames:v",
            "1",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class TagmusicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("ffmpeg no está instalado")
        tagmusic.require_tool("metaflac")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tagmusic-test-"))
        self.album = self.tmp / "album"
        self.album.mkdir()
        make_flac(self.album / "01. Guns N' Roses - Welcome To The Jungle.flac")
        make_flac(
            self.album / "02. Guns N' Roses - It's So Easy.flac",
            {"RATING": "0", "ENCODER": "prueba"},
        )
        make_flac(self.album / "03. Guns N' Roses - Nightrain.flac")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = tagmusic.main([str(self.album), *args])
        return code, buffer.getvalue()

    def run_cli_capture(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tagmusic.main([str(self.album), *args])
        return code, out.getvalue(), err.getvalue()

    def test_parse_name(self) -> None:
        import re

        pattern = re.compile(tagmusic.DEFAULT_PATTERN)
        track = tagmusic.parse_name(
            Path("07. Guns N' Roses - My Michelle.flac"), pattern
        )
        self.assertEqual((track.track, track.artist, track.title), (7, "Guns N' Roses", "My Michelle"))

        track = tagmusic.parse_name(Path("05 - Mr. Brownstone.flac"), pattern)
        self.assertEqual((track.track, track.title), (5, "Mr. Brownstone"))

        track = tagmusic.parse_name(Path("Sweet Child O' Mine.flac"), pattern)
        self.assertEqual((track.track, track.artist, track.title), (None, None, "Sweet Child O' Mine"))

    def test_dry_run_by_default(self) -> None:
        code, output = self.run_cli("Guns N' Roses", "Appetite for Destruction")
        self.assertEqual(code, 0)
        self.assertIn("Simulación", output)
        tags = tagmusic.read_tags(self.album / "01. Guns N' Roses - Welcome To The Jungle.flac")
        self.assertNotIn("ARTIST", tags)

    def test_apply_writes_tags_and_preserves_others(self) -> None:
        code, _ = self.run_cli(
            "Guns N' Roses",
            "Appetite for Destruction",
            "--apply",
            "--yes",
            "--no-backup",
        )
        self.assertEqual(code, 0)

        tags = tagmusic.read_tags(self.album / "01. Guns N' Roses - Welcome To The Jungle.flac")
        self.assertEqual(tags["ARTIST"], ["Guns N' Roses"])
        self.assertEqual(tags["ALBUMARTIST"], ["Guns N' Roses"])
        self.assertEqual(tags["ALBUM"], ["Appetite for Destruction"])
        self.assertEqual(tags["TITLE"], ["Welcome To The Jungle"])
        self.assertEqual(tags["TRACKNUMBER"], ["1"])

        second = tagmusic.read_tags(self.album / "02. Guns N' Roses - It's So Easy.flac")
        self.assertEqual(second["TRACKNUMBER"], ["2"])
        self.assertEqual(second.get("RATING"), ["0"])

    def test_backup_created(self) -> None:
        backup = self.tmp / "backup"
        code, _ = self.run_cli("Guns N' Roses", "Album", "--apply", "--yes", "--backup-dir", str(backup))
        self.assertEqual(code, 0)
        self.assertTrue(any(backup.glob("*.vorbiscomment.txt")))

    def test_track_offset_and_date(self) -> None:
        code, _ = self.run_cli(
            "Guns N' Roses",
            "Use Your Illusion II",
            "--date",
            "1991",
            "--track-offset",
            "10",
            "--apply",
            "--yes",
            "--no-backup",
        )
        self.assertEqual(code, 0)
        tags = tagmusic.read_tags(self.album / "03. Guns N' Roses - Nightrain.flac")
        self.assertEqual(tags["TRACKNUMBER"], ["13"])
        self.assertEqual(tags["DATE"], ["1991"])

    def test_aborts_when_a_file_cannot_be_parsed(self) -> None:
        (self.album / "portada.jpg").write_bytes(b"no soy audio")
        make_flac(self.album / "pista sin numero.flac")
        code, output = self.run_cli("Guns N' Roses", "Album", "--apply", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no se pudo deducir el número de pista", output)
        tags = tagmusic.read_tags(self.album / "01. Guns N' Roses - Welcome To The Jungle.flac")
        self.assertNotIn("ALBUM", tags)

    def test_non_flac_is_rejected(self) -> None:
        fake = self.album / "01. Guns N' Roses - Fake.flac"
        fake.write_bytes(b"ID3 basura")
        code, _ = self.run_cli("Guns N' Roses", "Album", "--apply", "--yes")
        self.assertEqual(code, 1)

    # -- carátula ---------------------------------------------------------

    def first_track(self) -> Path:
        return self.album / "01. Guns N' Roses - Welcome To The Jungle.flac"

    def test_cover_embedded_as_front_picture(self) -> None:
        cover = self.album / "portada.jpg"
        make_cover(cover)
        code, output = self.run_cli(
            "Guns N' Roses", "Album", "--cover", str(cover), "--apply", "--yes", "--no-backup"
        )
        self.assertEqual(code, 0, output)

        pictures = tagmusic.list_pictures(self.first_track())
        self.assertEqual(len(pictures), 1)
        self.assertEqual(pictures[0].type, 3)
        self.assertEqual(pictures[0].mime, "image/jpeg")
        self.assertEqual(pictures[0].data_length, cover.stat().st_size)

    def test_cover_is_detected_in_folder(self) -> None:
        make_cover(self.album / "cover.png")
        code, output = self.run_cli(
            "Guns N' Roses", "Album", "--cover", "--apply", "--yes", "--no-backup"
        )
        self.assertEqual(code, 0, output)
        pictures = tagmusic.list_pictures(self.first_track())
        self.assertEqual([p.type for p in pictures], [3])
        self.assertEqual(pictures[0].mime, "image/png")

    def test_cover_only_change_is_applied_and_idempotent(self) -> None:
        cover = self.album / "cover.jpg"
        make_cover(cover)
        args = (
            "Guns N' Roses",
            "Album",
            "--cover",
            str(cover),
            "--apply",
            "--yes",
            "--no-backup",
        )
        self.assertEqual(self.run_cli(*args)[0], 0)
        code, output = self.run_cli(*args)
        self.assertEqual(code, 0)
        self.assertIn("Nada que cambiar.", output)
        self.assertEqual(len(tagmusic.list_pictures(self.first_track())), 1)

    def test_cover_replaces_existing_and_keeps_other_pictures(self) -> None:
        metaflac = tagmusic.require_tool("metaflac")
        old = self.album / "vieja.jpg"
        back = self.album / "trasera.jpg"
        make_cover(old)
        make_cover(back)
        subprocess.run(
            [metaflac, f"--import-picture-from=3|image/jpeg|||{old}", str(self.first_track())],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                metaflac,
                f"--import-picture-from=4|image/jpeg|trasera||{back}",
                str(self.first_track()),
            ],
            check=True,
            capture_output=True,
        )

        cover = self.album / "nueva.jpg"
        make_cover(cover, color="blue", size=128)
        code, output = self.run_cli(
            "Guns N' Roses", "Album", "--cover", str(cover), "--apply", "--yes", "--no-backup"
        )
        self.assertEqual(code, 0, output)

        pictures = tagmusic.list_pictures(self.first_track())
        self.assertEqual(sorted(p.type for p in pictures), [3, 4])
        front = [p for p in pictures if p.type == 3][0]
        self.assertEqual(front.data_length, cover.stat().st_size)
        self.assertEqual(front.width, 128)
        self.assertEqual([p for p in pictures if p.type == 4][0].description, "trasera")

    def test_missing_cover_aborts(self) -> None:
        code, output = self.run_cli("Guns N' Roses", "Album", "--cover", "--apply", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no se encontró ninguna carátula", output)

    def test_unsupported_cover_format_is_rejected(self) -> None:
        bad = self.album / "caratula.gif"
        bad.write_bytes(b"GIF89a")
        code, _, err = self.run_cli_capture(
            "Guns N' Roses", "Album", "--cover", str(bad), "--apply", "--yes"
        )
        self.assertEqual(code, 2)
        self.assertIn("carátula no soportada", err)

    def test_cover_backup_saves_pictures(self) -> None:
        metaflac = tagmusic.require_tool("metaflac")
        old = self.album / "vieja.jpg"
        new = self.album / "cover.jpg"
        make_cover(old)
        make_cover(new, color="blue", size=128)
        subprocess.run(
            [metaflac, f"--import-picture-from=3|image/jpeg|||{old}", str(self.first_track())],
            check=True,
            capture_output=True,
        )

        backup = self.tmp / "backup"
        code, _ = self.run_cli(
            "Guns N' Roses",
            "Album",
            "--cover",
            str(new),
            "--apply",
            "--yes",
            "--backup-dir",
            str(backup),
        )
        self.assertEqual(code, 0)
        saved = list(backup.glob("*.picture-*.jpg"))
        self.assertTrue(saved)
        self.assertEqual(saved[0].stat().st_size, old.stat().st_size)


if __name__ == "__main__":
    unittest.main()
