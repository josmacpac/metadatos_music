#!/usr/bin/env python3
"""tagmusic - etiqueta lotes de FLAC (y MP3) a partir del nombre de archivo.

Uso rápido:
    ./tagmusic.py "~/Música/Guns N' Roses - [1987] - Appetite for Destruction" \
        "Guns N' Roses" "Appetite for Destruction" --cover

Si omites el artista o el álbum, el script los pregunta por consola. Por
defecto NO escribe nada: muestra los cambios y, al final, pregunta si quieres
aplicarlos. Usa --apply para escribir sin preguntar.

Con --cover se incrusta una carátula en cada archivo. Si solo pasas --cover
(sin artista, álbum ni otros datos) únicamente se toca la carátula. Sin valor,
busca cover/folder/front/album/portada en la carpeta.

FLAC se edita con metaflac; MP3 con mutagen (pip install -r requirements.txt).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

VERSION = "1.3.0"

DEFAULT_PATTERN = (
    r"^(?:(?P<track>\d+)\s*[.)\-–_]\s*)?"
    r"(?:(?P<artist>.+?)\s+-\s+)?"
    r"(?P<title>.+)$"
)

DEFAULT_EXTS = (".flac", ".mp3")

MP3_EXTS = (".mp3",)

# Valor de --cover sin argumento: buscar la imagen en la carpeta.
COVER_AUTO = "__auto__"

# Nombres de archivo que se reconocen al buscar la carátula automáticamente.
COVER_NAMES = (
    "cover",
    "folder",
    "front",
    "album",
    "albumart",
    "portada",
    "caratula",
    "carátula",
)

MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png"}

FRONT_COVER = 3


class TagError(RuntimeError):
    """Error controlado del etiquetado."""


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


@dataclass
class Track:
    path: Path
    track: int | None = None
    artist: str | None = None
    title: str | None = None


@dataclass
class Plan:
    track: Track
    changes: dict[str, str] = field(default_factory=dict)
    pending: dict[str, tuple[list[str], str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cover: Path | None = None
    cover_current: str = ""
    cover_pending: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.pending) or self.cover_pending


# --------------------------------------------------------------------------
# Lectura / escritura de etiquetas
# --------------------------------------------------------------------------


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise TagError(
            f"no se encontró '{name}' en PATH. Instálalo (paquete 'flac') para editar FLAC."
        )
    return path


def require_module(name: str) -> types.ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise TagError(
            f"para MP3 hace falta el módulo '{name}'. Instala con "
            "'pip install -r requirements.txt' o 'sudo pacman -S python-mutagen'."
        ) from exc


def is_mp3(path: Path) -> bool:
    return path.suffix.lower() in MP3_EXTS


def read_tags(path: Path) -> dict[str, list[str]]:
    """Devuelve {CAMPO: [valores]} conservando el orden del archivo."""
    if is_mp3(path):
        return read_tags_mp3(path)
    metaflac = require_tool("metaflac")
    result = subprocess.run(
        [metaflac, "--export-tags-to=-", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TagError(f"{path.name}: metaflac falló: {result.stderr.strip()}")

    tags: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition("=")
        tags.setdefault(key.upper(), []).append(value)
    return tags


def is_flac(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"fLaC"
    except OSError:
        return False


def is_audio(path: Path) -> bool:
    if is_mp3(path):
        try:
            with path.open("rb") as fh:
                head = fh.read(3)
        except OSError:
            return False
        # ID3v2 o sincronía MPEG (0xFF 0xEx/0xFx)
        return head == b"ID3" or head[:1] == b"\xff"
    return is_flac(path)


# --------------------------------------------------------------------------
# MP3 (ID3v2) mediante mutagen
# --------------------------------------------------------------------------

ID3_TEXT_FRAMES = {
    "TITLE": "TIT2",
    "ARTIST": "TPE1",
    "ALBUMARTIST": "TPE2",
    "ALBUM": "TALB",
    "GENRE": "TCON",
}


def load_id3(path: Path) -> types.ModuleType:
    id3 = require_module("mutagen.id3")
    try:
        return id3.ID3(str(path))
    except id3.ID3NoHeaderError:
        return id3.ID3()


def read_tags_mp3(path: Path) -> dict[str, list[str]]:
    id3 = load_id3(path)
    tags: dict[str, list[str]] = {}
    for key, frame_name in ID3_TEXT_FRAMES.items():
        values = [str(f.text[0]) for f in id3.getall(frame_name) if f.text]
        if values:
            tags[key] = values

    date = id3.getall("TDRC") or id3.getall("TYER")
    if date and date[0].text:
        tags["DATE"] = [str(date[0].text[0])]

    for frame_name, number_key, total_key in (
        ("TRCK", "TRACKNUMBER", "TRACKTOTAL"),
        ("TPOS", "DISCNUMBER", "DISCTOTAL"),
    ):
        frame = id3.get(frame_name)
        if frame is None or not frame.text:
            continue
        number, _, total = str(frame.text[0]).partition("/")
        if number:
            tags[number_key] = [number]
        if total:
            tags[total_key] = [total]
    return tags


def set_id3_pair(
    id3: types.ModuleType,
    frame_name: str,
    tags: dict[str, str],
    number_key: str,
    total_key: str,
) -> None:
    existing = id3.get(frame_name)
    current = str(existing.text[0]) if existing is not None and existing.text else ""
    number, _, total = current.partition("/")
    number = tags.get(number_key, number)
    if total_key in tags:
        total = tags[total_key]
    value = f"{number}/{total}" if total else number
    id3.delall(frame_name)
    if value:
        frame = getattr(require_module("mutagen.id3"), frame_name)
        id3.add(frame(encoding=3, text=value))


def write_tags_mp3(path: Path, tags: dict[str, str]) -> None:
    id3mod = require_module("mutagen.id3")
    id3 = load_id3(path)

    for key, frame_name in ID3_TEXT_FRAMES.items():
        if key not in tags:
            continue
        id3.delall(frame_name)
        frame = getattr(id3mod, frame_name)
        id3.add(frame(encoding=3, text=tags[key]))

    if "DATE" in tags:
        id3.delall("TDRC")
        id3.delall("TYER")
        id3.add(id3mod.TDRC(encoding=3, text=tags["DATE"]))

    if "TRACKNUMBER" in tags or "TRACKTOTAL" in tags:
        set_id3_pair(id3, "TRCK", tags, "TRACKNUMBER", "TRACKTOTAL")
    if "DISCNUMBER" in tags:
        set_id3_pair(id3, "TPOS", tags, "DISCNUMBER", "DISCTOTAL")

    id3.save(str(path), v2_version=3)


def run_metaflac(args: list[str], path: Path) -> subprocess.CompletedProcess:
    metaflac = require_tool("metaflac")
    result = subprocess.run([metaflac, *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TagError(f"{path.name}: metaflac falló: {result.stderr.strip()}")
    return result


# --------------------------------------------------------------------------
# Carátulas (bloques PICTURE)
# --------------------------------------------------------------------------


@dataclass
class Picture:
    block: int
    type: int
    mime: str = ""
    description: str = ""
    width: int = 0
    height: int = 0
    depth: int = 0
    data_length: int = 0

    @property
    def is_front(self) -> bool:
        return self.type == FRONT_COVER

    def describe(self) -> str:
        parts = [self.mime or "imagen"]
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        parts.append(f"{self.data_length / 1024:.0f} KiB")
        return " ".join(parts)


def list_pictures_mp3(path: Path) -> list[Picture]:
    id3 = load_id3(path)
    pictures = []
    for index, apic in enumerate(id3.getall("APIC")):
        pictures.append(
            Picture(
                block=index,
                type=int(apic.type or 0),
                mime=str(apic.mime or ""),
                description=str(apic.desc or ""),
                data_length=len(apic.data or b""),
            )
        )
    return pictures


def export_picture(path: Path, picture: Picture, target: Path) -> None:
    if is_mp3(path):
        apics = load_id3(path).getall("APIC")
        if picture.block >= len(apics):
            raise TagError(f"{path.name}: no existe la imagen {picture.block}")
        target.write_bytes(apics[picture.block].data or b"")
        return
    run_metaflac(
        [
            f"--export-picture-to={target}",
            f"--block-number={picture.block}",
            str(path),
        ],
        path,
    )


def list_pictures(path: Path) -> list[Picture]:
    """Devuelve las imágenes incrustadas (bloques PICTURE o marcos APIC)."""
    if is_mp3(path):
        return list_pictures_mp3(path)
    result = run_metaflac(["--list", "--block-type=PICTURE", str(path)], path)

    pictures: list[Picture] = []
    block: int | None = None
    fields: dict[str, object] = {}
    type_fields = 0

    def flush() -> None:
        nonlocal block, fields, type_fields
        if block is not None and type_fields >= 2:
            pictures.append(
                Picture(
                    block=block,
                    type=int(fields.get("type", 0)),
                    mime=str(fields.get("mime", "")),
                    description=str(fields.get("description", "")),
                    width=int(fields.get("width", 0)),
                    height=int(fields.get("height", 0)),
                    depth=int(fields.get("depth", 0)),
                    data_length=int(fields.get("data_length", 0)),
                )
            )
        block, fields, type_fields = None, {}, 0

    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("METADATA block #"):
            flush()
            block = int(line.split("#", 1)[1])
            continue
        if block is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "type":
            type_fields += 1
            if type_fields == 2:  # el primero es el tipo de bloque (6)
                fields["type"] = int(value.split()[0])
        elif key == "MIME type":
            fields["mime"] = value
        elif key == "description":
            fields["description"] = value
        elif key == "data length":
            fields["data_length"] = int(value.split()[0])
        elif key in ("width", "height", "depth"):
            fields[key] = int(value.split()[0])
    flush()
    return pictures


def cover_mime(image: Path) -> str:
    mime = MIME_BY_EXT.get(image.suffix.lower())
    if not mime:
        raise TagError(
            f"carátula no soportada: {image.name} (usa .jpg, .jpeg o .png)"
        )
    return mime


def find_cover_in(directory: Path) -> Path | None:
    """Busca cover/folder/front/... con extensión de imagen en una carpeta."""
    try:
        entries = {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in COVER_NAMES:
        for ext in MIME_BY_EXT:
            found = entries.get(name + ext)
            if found is not None:
                return found
    return None


def cover_for(path: Path, root: Path, choice: str | None) -> Path | None:
    if choice is None:
        return None
    if choice != COVER_AUTO:
        return Path(choice).expanduser()
    for directory in (path.parent, root):
        found = find_cover_in(directory)
        if found is not None:
            return found
    return None


def cover_status(path: Path, image: Path) -> tuple[bool, str]:
    """(¿hay que escribirla?, descripción de la carátula actual)."""
    fronts = [p for p in list_pictures(path) if p.is_front]
    if not fronts:
        current = "(sin carátula)"
    elif len(fronts) > 1:
        current = f"{len(fronts)} carátulas frontales {fronts[0].describe()}"
    else:
        current = fronts[0].describe()

    if len(fronts) == 1:
        front = fronts[0]
        mime = cover_mime(image)
        if front.mime == mime and front.data_length == image.stat().st_size:
            with tempfile.TemporaryDirectory(prefix="tagmusic-cover-") as tmp:
                exported = Path(tmp) / "front"
                export_picture(path, front, exported)
                try:
                    if exported.read_bytes() == image.read_bytes():
                        return False, current
                except OSError:
                    pass
    return True, current


def remove_front_covers(path: Path) -> None:
    """Quita solo las carátulas frontales; deja el resto de imágenes."""
    if is_mp3(path):
        id3 = load_id3(path)
        remaining = [a for a in id3.getall("APIC") if int(a.type or 0) != FRONT_COVER]
        if len(remaining) != len(id3.getall("APIC")):
            id3.setall("APIC", remaining)
            id3.save(str(path), v2_version=3)
        return
    fronts = sorted(
        (p for p in list_pictures(path) if p.is_front),
        key=lambda p: p.block,
        reverse=True,
    )
    for picture in fronts:
        run_metaflac(
            ["--remove", f"--block-number={picture.block}", str(path)], path
        )


def embed_cover(path: Path, image: Path) -> None:
    mime = cover_mime(image)
    if is_mp3(path):
        id3mod = require_module("mutagen.id3")
        remove_front_covers(path)
        id3 = load_id3(path)
        id3.add(
            id3mod.APIC(
                encoding=3,
                mime=mime,
                type=FRONT_COVER,
                desc="",
                data=image.read_bytes(),
            )
        )
        id3.save(str(path), v2_version=3)
        return
    remove_front_covers(path)
    run_metaflac(
        [f"--import-picture-from={FRONT_COVER}|{mime}|||{image}", str(path)], path
    )


def verify_cover(path: Path, image: Path) -> list[str]:
    fronts = [p for p in list_pictures(path) if p.is_front]
    if len(fronts) != 1:
        return [f"carátula: se esperaba 1 frontal, hay {len(fronts)}"]
    front = fronts[0]
    problems = []
    mime = cover_mime(image)
    if front.mime != mime:
        problems.append(f"carátula: tipo esperado {mime}, leído {front.mime}")
    if front.data_length != image.stat().st_size:
        problems.append(
            f"carátula: tamaño esperado {image.stat().st_size}, "
            f"leído {front.data_length}"
        )
    return problems


def backup_pictures(path: Path, backup_dir: Path) -> list[Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for picture in list_pictures(path):
        ext = EXT_BY_MIME.get(picture.mime, ".bin")
        target = backup_dir / f"{path.stem}.picture-{picture.block}{ext}"
        export_picture(path, picture, target)
        written.append(target)
    return written


def write_tags(path: Path, tags: dict[str, str]) -> None:
    """Escribe los campos indicados sin tocar el resto de etiquetas."""
    if is_mp3(path):
        write_tags_mp3(path, tags)
        return
    metaflac = require_tool("metaflac")
    existing = read_tags(path)

    args = [metaflac]
    # Borra el nombre canónico y cualquier variante con otra capitalización.
    for key in tags:
        args.append(f"--remove-tag={key}")
        for name in existing:
            if name == key.upper() and name != key:
                args.append(f"--remove-tag={name}")
    for key, value in tags.items():
        args.append(f"--set-tag={key}={value}")
    args.append(str(path))

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise TagError(f"{path.name}: metaflac falló: {result.stderr.strip()}")


def backup_tags(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    if is_mp3(path):
        target = backup_dir / f"{path.stem}.id3.txt"
        lines = [
            f"{key}={value}"
            for key, values in read_tags_mp3(path).items()
            for value in values
        ]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return target
    metaflac = require_tool("metaflac")
    target = backup_dir / f"{path.stem}.vorbiscomment.txt"
    result = subprocess.run(
        [metaflac, f"--export-tags-to={target}", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TagError(f"{path.name}: no se pudo respaldar: {result.stderr.strip()}")
    return target


# --------------------------------------------------------------------------
# Análisis de nombres de archivo
# --------------------------------------------------------------------------


def natural_key(path: Path) -> list:
    parts = re.split(r"(\d+)", str(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]


def discover(root: Path, recursive: bool, exts: tuple[str, ...]) -> list[Path]:
    it = root.rglob("*") if recursive else root.glob("*")
    files = [
        p
        for p in it
        if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
    ]
    return sorted(files, key=natural_key)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t.-_")


def parse_name(path: Path, pattern: re.Pattern[str]) -> Track:
    track = Track(path=path)
    match = pattern.match(path.stem)
    if not match:
        return track
    groups = match.groupdict()
    if groups.get("track"):
        track.track = int(groups["track"])
    if groups.get("artist"):
        track.artist = clean(groups["artist"])
    if groups.get("title"):
        track.title = clean(groups["title"])
    return track


# --------------------------------------------------------------------------
# Construcción del plan
# --------------------------------------------------------------------------


def has_tag_input(args: argparse.Namespace) -> bool:
    """¿El usuario pidió datos de etiquetas (además de la carátula)?"""
    return bool(
        args.artist
        or args.album
        or args.albumartist
        or args.date
        or args.genre
        or args.disc is not None
        or args.total is not None
    )


def cover_only(args: argparse.Namespace) -> bool:
    """Con --cover y sin datos de etiquetas, solo se toca la carátula."""
    return args.cover is not None and not has_tag_input(args)


def build_plan(
    root: Path,
    args: argparse.Namespace,
    pattern: re.Pattern[str],
    exts: tuple[str, ...],
) -> list[Plan]:
    files = discover(root, args.recursive, exts)
    if not files:
        raise TagError(f"no se encontraron archivos {', '.join(exts)} en {root}")

    only_cover = cover_only(args)
    plans: list[Plan] = []
    for path in files:
        track = parse_name(path, pattern)
        plan = Plan(track=track)

        if not is_audio(path):
            plan.errors.append("no parece un archivo de audio válido (FLAC o MP3)")
            plans.append(plan)
            continue

        if only_cover:
            tags: dict[str, str] = {}
        else:
            if track.track is None:
                plan.errors.append("no se pudo deducir el número de pista del nombre")
            if track.title is None and not args.no_title:
                plan.errors.append("no se pudo deducir el título del nombre")

            artist = args.artist or track.artist
            tags = {}
            if not args.no_title and track.title:
                tags["TITLE"] = f"{args.title_prefix}{track.title}"
            if artist:
                tags["ARTIST"] = artist
                tags["ALBUMARTIST"] = args.albumartist or artist
            elif args.albumartist:
                tags["ALBUMARTIST"] = args.albumartist
            if args.album:
                tags["ALBUM"] = args.album
            if track.track is not None:
                tags["TRACKNUMBER"] = str(track.track + args.track_offset)
            if args.total:
                tags["TRACKTOTAL"] = str(args.total)
            if args.disc is not None:
                tags["DISCNUMBER"] = str(args.disc)
            if args.date:
                tags["DATE"] = args.date
            if args.genre:
                tags["GENRE"] = args.genre

        plan.changes = tags
        try:
            current = read_tags(path)
        except TagError as exc:
            plan.errors.append(str(exc))
        else:
            plan.pending = {
                key: (current.get(key, []), value)
                for key, value in tags.items()
                if current.get(key, []) != [value]
            }

        cover = cover_for(path, root, args.cover)
        if cover is None and args.cover == COVER_AUTO:
            plan.errors.append(
                "no se encontró ninguna carátula en la carpeta (--cover)"
            )
        elif cover is not None:
            plan.cover = cover
            try:
                pending_cover, current = cover_status(path, cover)
            except TagError as exc:
                plan.errors.append(str(exc))
            else:
                plan.cover_pending = pending_cover
                plan.cover_current = current

        plans.append(plan)
    return plans


# --------------------------------------------------------------------------
# Entrada interactiva
# --------------------------------------------------------------------------


def is_interactive(args: argparse.Namespace) -> bool:
    return not args.no_interactive and sys.stdin.isatty() and sys.stdout.isatty()


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def ask_yes_no(label: str, default: bool = False) -> bool:
    hint = "S/n" if default else "s/N"
    try:
        answer = input(f"{label} ({hint}): ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"s", "si", "sí", "y", "yes"}


def gather_missing(args: argparse.Namespace, root: Path) -> None:
    """Pide por consola los datos que falten (solo si hay terminal)."""
    if not is_interactive(args):
        return
    if cover_only(args):
        return
    if args.artist and args.album:
        return

    print(f"Carpeta: {root}")
    print("Pulsa Enter para dejar un campo vacío / aceptar el valor propuesto.\n")
    if not args.artist:
        args.artist = ask("Artista") or None
    if not args.album:
        args.album = ask("Álbum") or None
    if not args.date:
        args.date = ask("Año (opcional)") or None
    if not args.genre:
        args.genre = ask("Género (opcional)") or None
    print()


# --------------------------------------------------------------------------
# Salida
# --------------------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


def print_plan(plans: list[Plan], sty: Style, show_unchanged: bool) -> None:
    for plan in plans:
        path = plan.track.path
        if plan.errors:
            print(f"{sty.red('✗')} {path.name}")
            for err in plan.errors:
                print(f"    {sty.red(err)}")
            continue

        if not plan.has_changes:
            if show_unchanged:
                print(f"{sty.dim(path.name)} {sty.dim('(sin cambios)')}")
            continue

        print(f"{sty.bold(path.name)}")
        for key, (old, new) in plan.pending.items():
            old_txt = sty.dim(", ".join(old) if old else "(vacío)")
            print(f"    {key:<12} {old_txt} {sty.green('→')} {new}")
        if plan.cover_pending and plan.cover is not None:
            new_cover = f"{plan.cover.name} ({cover_mime(plan.cover)})"
            print(
                f"    {'CARÁTULA':<12} {sty.dim(plan.cover_current)} "
                f"{sty.green('→')} {new_cover}"
            )


def verify(path: Path, expected: dict[str, str]) -> list[str]:
    current = read_tags(path)
    problems = []
    for key, value in expected.items():
        if current.get(key) != [value]:
            problems.append(f"{key}: esperado {value!r}, leído {current.get(key)!r}")
    return problems


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tagmusic",
        description=(
            "Etiqueta lotes de FLAC y MP3 usando el nombre del archivo para el "
            "número de pista y el título. Sin --apply solo simula los cambios."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  tagmusic.py ~/Música/disco \"Guns N' Roses\" \"Appetite for Destruction\"\n"
            "  tagmusic.py ~/Música/disco                # pregunta artista/álbum\n"
            "  tagmusic.py ~/Música/disco --cover --apply  # solo carátula\n"
            "  tagmusic.py ~/Música --recursive --artist \"X\" --album \"Y\" --cover --apply\n"
        ),
    )
    parser.add_argument("directorio", type=Path, help="carpeta con los archivos de audio")
    parser.add_argument("artista", nargs="?", help="artista a escribir en ARTIST")
    parser.add_argument("album_pos", nargs="?", metavar="album", help="álbum a escribir en ALBUM")

    extra = parser.add_argument_group("datos opcionales")
    extra.add_argument("--artist", dest="artist_flag", help="igual que la posición ARTISTA")
    extra.add_argument("--album", dest="album_flag", help="igual que la posición ALBUM")
    extra.add_argument("--albumartist", help="valor de ALBUMARTIST (por defecto, el artista)")
    extra.add_argument("--date", "--year", dest="date", help="valor de DATE, p. ej. 1987")
    extra.add_argument("--genre", help="valor de GENRE")
    extra.add_argument("--disc", type=int, help="valor de DISCNUMBER")
    extra.add_argument("--total", type=int, help="valor de TRACKTOTAL")
    extra.add_argument(
        "--track-offset",
        type=int,
        default=0,
        help="suma este número a la pista deducida del nombre (por defecto 0)",
    )
    extra.add_argument(
        "--title-prefix", default="", help="prefijo añadido al título deducido del nombre"
    )

    art = parser.add_argument_group("carátula")
    art.add_argument(
        "--cover",
        nargs="?",
        const=COVER_AUTO,
        metavar="IMAGEN",
        help="incrustar IMAGEN como carátula frontal; sin valor, busca "
        "cover/folder/front/album/portada (.jpg, .jpeg, .png) en la carpeta",
    )

    scan = parser.add_argument_group("cómo leer los nombres")
    scan.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="expresión regular con grupos 'track', 'artist' y 'title'",
    )
    scan.add_argument(
        "--no-title", action="store_true", help="no escribir TITLE (conserva el existente)"
    )
    scan.add_argument(
        "--ext",
        action="append",
        metavar="EXT",
        help="extensión a procesar (repetible). Por defecto .flac y .mp3",
    )
    scan.add_argument("-r", "--recursive", action="store_true", help="incluir subcarpetas")

    write = parser.add_argument_group("escritura")
    mode = write.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", dest="apply", action="store_true", help="escribir los cambios"
    )
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="solo simular (comportamiento por defecto)",
    )
    parser.set_defaults(apply=False)
    write.add_argument(
        "-y", "--yes", action="store_true", help="no pedir confirmación antes de escribir"
    )
    write.add_argument(
        "--backup-dir",
        type=Path,
        help="dónde guardar las etiquetas originales "
        "(por defecto ~/.local/state/tagmusic/backups/<fecha>)",
    )
    write.add_argument(
        "--no-backup", action="store_true", help="no respaldar las etiquetas originales"
    )
    write.add_argument(
        "--report", type=Path, help="guardar un informe JSON con los cambios aplicados"
    )

    salida = parser.add_argument_group("salida")
    salida.add_argument(
        "--show-unchanged", action="store_true", help="listar también archivos sin cambios"
    )
    salida.add_argument(
        "--no-interactive",
        action="store_true",
        help="no hacer preguntas por consola (arte no interactivo)",
    )
    salida.add_argument("-q", "--quiet", action="store_true", help="menos salida")
    salida.add_argument("--version", action="version", version=f"tagmusic {VERSION}")
    return parser


def default_backup_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "tagmusic" / "backups" / stamp


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sty = Style(sys.stdout.isatty())

    args.artist = args.artist_flag or args.artista
    args.album = args.album_flag or args.album_pos

    root = args.directorio.expanduser()
    if not root.is_dir():
        print(f"{sty.red('Error:')} {root} no es una carpeta", file=sys.stderr)
        return 2

    try:
        pattern = re.compile(args.pattern)
    except re.error as exc:
        print(f"{sty.red('Error:')} patrón inválido: {exc}", file=sys.stderr)
        return 2
    if not {"track", "artist", "title"} <= set(pattern.groupindex):
        print(
            f"{sty.red('Error:')} el patrón debe definir los grupos 'track', 'artist' y 'title'",
            file=sys.stderr,
        )
        return 2

    exts = tuple(
        e.lower() if e.startswith(".") else f".{e.lower()}" for e in (args.ext or DEFAULT_EXTS)
    )

    if args.cover is not None and args.cover != COVER_AUTO:
        cover_path = Path(args.cover).expanduser()
        if not cover_path.is_file():
            print(
                f"{sty.red('Error:')} la carátula {cover_path} no existe",
                file=sys.stderr,
            )
            return 2
        try:
            cover_mime(cover_path)
        except TagError as exc:
            print(f"{sty.red('Error:')} {exc}", file=sys.stderr)
            return 2
        args.cover = str(cover_path)

    gather_missing(args, root)

    try:
        plans = build_plan(root, args, pattern, exts)
    except TagError as exc:
        print(f"{sty.red('Error:')} {exc}", file=sys.stderr)
        return 1

    invalid = [p for p in plans if p.errors]
    print_plan(plans, sty, args.show_unchanged)

    pending = [p for p in plans if p.has_changes and not p.errors]

    if not args.apply:
        print()
        if not pending:
            print("Nada que cambiar.")
            return 0 if not invalid else 1
        print(
            f"Simulación: {len(pending)} archivo(s) cambiarían. "
            f"Añade {sty.bold('--apply')} para escribir."
        )
        if invalid:
            print(
                sty.yellow(f"{len(invalid)} archivo(s) con problemas; se omitirían al aplicar.")
            )
            return 1
        if is_interactive(args) and not args.yes:
            if not ask_yes_no("¿Aplicar los cambios ahora?"):
                return 0
        else:
            return 0

    if invalid:
        print(
            sty.red(
                f"Abortado: {len(invalid)} archivo(s) con problemas. Corrígelos o usa otro patrón."
            ),
            file=sys.stderr,
        )
        return 1
    if not pending:
        print("Nada que cambiar.")
        return 0

    backup_dir = None
    if not args.no_backup:
        backup_dir = (args.backup_dir or default_backup_dir()).expanduser()

    applied: list[dict] = []
    failures = 0
    for plan in pending:
        path = plan.track.path
        try:
            if backup_dir is not None:
                backup_tags(path, backup_dir)
                if plan.cover_pending:
                    backup_pictures(path, backup_dir)
            if plan.pending:
                write_tags(path, {k: v for k, (_, v) in plan.pending.items()})
            problems = verify(path, {k: v for k, (_, v) in plan.pending.items()})
            if plan.cover_pending and plan.cover is not None:
                embed_cover(path, plan.cover)
                problems += verify_cover(path, plan.cover)
            if problems:
                failures += 1
                print(f"{sty.red('✗')} {path.name}: verificación fallida")
                for problem in problems:
                    print(f"    {sty.red(problem)}")
                continue
            entry = {
                "archivo": path.name,
                "cambios": {k: v for k, (_, v) in plan.pending.items()},
            }
            if plan.cover_pending and plan.cover is not None:
                entry["caratula"] = str(plan.cover)
            applied.append(entry)
            if not args.quiet:
                print(f"{sty.green('✓')} {path.name}")
        except TagError as exc:
            failures += 1
            print(f"{sty.red('✗')} {exc}", file=sys.stderr)

    print()
    print(f"Aplicado en {len(applied)} archivo(s), {failures} error(es).")
    if backup_dir is not None and applied:
        print(f"Respaldo de etiquetas originales: {backup_dir}")

    if args.report and applied:
        report = args.report.expanduser()
        report.write_text(
            json.dumps(
                {
                    "fecha": datetime.now().isoformat(timespec="seconds"),
                    "carpeta": str(root),
                    "cambios": applied,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Informe: {report}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
