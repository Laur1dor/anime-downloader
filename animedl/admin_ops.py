"""Админские операции: перезагрузка docker-контейнеров и управление лимитом скорости."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from dataclasses import dataclass

log = logging.getLogger(__name__)


def _restart_sync(names: list[str]) -> list[str]:
    """Перезапустить контейнеры по именам. Возвращает список сообщений о результате.

    Контейнеры перезапускаются в переданном порядке — свой (bot) обычно последним,
    т.к. его рестарт убьёт текущий процесс (docker поднимет заново по restart-policy).
    """
    import docker  # локальный импорт — тяжёлая зависимость
    client = docker.from_env()
    out: list[str] = []
    for name in names:
        try:
            c = client.containers.get(name)
            c.restart(timeout=10)
            out.append(f"✅ {name}")
        except Exception as e:  # noqa: BLE001
            out.append(f"⚠ {name}: {e}")
            log.warning("restart %s: %s", name, e)
    return out


async def restart_containers(names: list[str]) -> list[str]:
    if not names:
        return ["⚠ не задан список контейнеров (RELOAD_CONTAINERS)"]
    return await asyncio.to_thread(_restart_sync, names)


def read_bandwidth(rate_file: Path) -> int:
    try:
        raw = rate_file.read_text(encoding="utf-8").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else 0
    except (OSError, ValueError):
        return 0


def write_bandwidth(rate_file: Path, mbit: int) -> None:
    rate_file.parent.mkdir(parents=True, exist_ok=True)
    rate_file.write_text(str(max(0, mbit)), encoding="utf-8")
    log.info("Лимит скорости выставлен: %d Мбит/с (файл %s)", mbit, rate_file)


# ---------- медиатека (NAS/SMB -> Infuse) ----------

_INVALID = '<>:"/\\|?*'


def _sanitize(name: str) -> str:
    """Безопасное имя файла/папки (для SMB/NAS и Infuse)."""
    cleaned = "".join("_" if ch in _INVALID else ch for ch in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:150] or "anime"


def _flag_path(rate_file: Path) -> Path:
    return rate_file.parent / "library_on"


def library_enabled(rate_file: Path) -> bool:
    try:
        return _flag_path(rate_file).read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def set_library(rate_file: Path, on: bool) -> None:
    p = _flag_path(rate_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1" if on else "0", encoding="utf-8")


def list_library(library_dir: Path | None) -> list[tuple[str, int, int]]:
    """Список тайтлов в медиатеке: (имя_папки, суммарный_размер, кол-во_серий)."""
    if not library_dir or not library_dir.exists():
        return []
    out: list[tuple[str, int, int]] = []
    for d in sorted(library_dir.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        files = [f for f in d.rglob("*.mp4") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        out.append((d.name, size, len(files)))
    return out


def delete_library_title(library_dir: Path | None, name: str) -> bool:
    """Удалить папку тайтла из медиатеки. С защитой от path traversal."""
    import shutil
    if not library_dir:
        return False
    base = library_dir.resolve()
    target = (library_dir / name).resolve()
    if target.parent != base or not target.is_dir():
        return False
    shutil.rmtree(target)
    log.info("Из медиатеки удалено: %s", target)
    return True


def library_ready(library_dir: Path | None) -> bool:
    """Marker lives on the mounted share, so a vanished mount fails closed."""
    return bool(library_dir and library_dir.is_dir()
                and (library_dir / ".animedl-library").is_file())


def category_ready(cfg, category: str) -> bool:
    folder = cfg.category_dir(category)
    if cfg.media_root:
        if not library_ready(cfg.media_root) or folder.resolve().parent != cfg.media_root.resolve():
            return False
    return library_ready(folder)


def list_media_library(cfg) -> list[tuple[str, int, int]]:
    from .models import MEDIA_CATEGORIES
    return [(f"{category}/{name}", size, count)
            for category in MEDIA_CATEGORIES if category_ready(cfg, category)
            for name, size, count in list_library(cfg.category_dir(category))]


def delete_media_title(cfg, name: str) -> bool:
    category, sep, title = name.partition("/")
    from .models import MEDIA_CATEGORIES
    if not sep or category not in MEDIA_CATEGORIES or not category_ready(cfg, category):
        return False
    return delete_library_title(cfg.category_dir(category), title)


def _limit_utf8(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def _write_nfo(path: Path, root_tag: str, fields: dict[str, str]) -> None:
    import os
    import tempfile
    import xml.etree.ElementTree as ET
    root = ET.Element(root_tag)
    for name, value in fields.items():
        ET.SubElement(root, name).text = value
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    fd, raw = tempfile.mkstemp(prefix=".metadata_", suffix=".part", dir=path.parent)
    staging = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


_LIBRARY_LOCK = threading.RLock()


@dataclass
class ArchiveOutcome:
    updated: bool = True
    quality: int = 0
    upgraded: bool = False
    metadata_updated: bool = False


def update_episode_name(path: Path, name: str) -> bool:
    """Update only the title, retaining existing stream and source metadata."""
    import xml.etree.ElementTree as ET
    import os
    import tempfile
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "episodedetails" or root.findtext("title") == name:
        return False
    element = root.find("title")
    if element is None:
        element = ET.SubElement(root, "title")
    element.text = name
    fd, raw = tempfile.mkstemp(prefix=".incoming_nfo_", dir=path.parent)
    os.close(fd)
    try:
        tree.write(raw, encoding="utf-8", xml_declaration=True)
        os.replace(raw, path)
    finally:
        Path(raw).unlink(missing_ok=True)
    return True


def _existing_archive(folder: Path, episode: int, season: int | None,
                      voice: str, movie: bool) -> tuple[Path, int] | None:
    """Read provenance from NFO, including metadata rewritten by Jellyfin."""
    import re
    import xml.etree.ElementTree as ET
    candidates = []
    for video in folder.glob("*.mp4"):
        try:
            root = ET.parse(video.with_suffix(".nfo")).getroot()
            if movie:
                if root.tag != "movie":
                    continue
            elif (root.tag != "episodedetails" or root.findtext("season") != str(season)
                  or root.findtext("episode") != str(episode)):
                continue
            plot = root.findtext("plot") or ""
            dub = re.search(r"Озвучка:\s*(.*?);\s*провайдер:", plot)
            resolution = re.search(r"качество:\s*(\d+)p", plot)
            if dub and _normalized_name(dub.group(1)) == _normalized_name(voice) and resolution:
                candidates.append((video, int(resolution.group(1))))
        except (OSError, ET.ParseError):
            continue
    return max(candidates, key=lambda item: item[1]) if candidates else None


def _normalized_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _nfo_title(path: Path) -> str:
    import xml.etree.ElementTree as ET
    try:
        return ET.parse(path).getroot().findtext("title") or ""
    except (OSError, ET.ParseError):
        return ""


def resolve_library_show(library_dir: Path, title: str, identity: str = "") -> Path:
    """Reuse a unique NFO title match, including legacy source-hash directories."""
    import hashlib
    if not library_ready(library_dir):
        raise OSError("Медиатека не подключена")
    with _LIBRARY_LOCK:
        matches = [p for p in library_dir.iterdir() if p.is_dir()
                   and _normalized_name(_nfo_title(p / "tvshow.nfo")) == _normalized_name(title)]
        if len(matches) > 1:
            raise ValueError("Несколько папок с таким названием сериала: проверь медиатеку")
        if matches:
            return matches[0]
        title_name = _limit_utf8(_sanitize(title), 90)
        title_hash = hashlib.sha256((identity or title).encode()).hexdigest()[:10]
        show_folder = library_dir / f"{title_name} [{title_hash}]"
        show_folder.mkdir(parents=True, exist_ok=True)
        _write_nfo(show_folder / "tvshow.nfo", "tvshow", {"title": title, "tag": "anime"})
        return show_folder


def resolve_library_season(show_folder: Path, season: int | None = None,
                           season_name: str = "") -> tuple[int, Path]:
    """Persist named-season indices in NFO; callers may use this for in-place migration."""
    import re
    season_name = " ".join(season_name.split())
    if season is not None and (type(season) is not int or not 1 <= season <= 999):
        raise ValueError("Номер сезона должен быть от 1 до 999")
    if season is None and not season_name:
        raise ValueError("Укажи название или номер сезона")
    with _LIBRARY_LOCK:
        show_folder.mkdir(parents=True, exist_ok=True)
        folders = {}
        for path in show_folder.iterdir():
            match = re.fullmatch(r"Season (\d+)", path.name)
            if path.is_dir() and match and int(match[1]) > 0:
                folders[int(match[1])] = path
        if season_name:
            matches = [(index, path) for index, path in folders.items()
                       if _normalized_name(_nfo_title(path / "season.nfo")) == _normalized_name(season_name)]
            if len(matches) > 1:
                raise ValueError("Несколько папок с таким названием сезона")
            if matches:
                if season is not None and matches[0][0] != season:
                    raise ValueError("Название сезона уже связано с другим номером")
                return matches[0]
        if season is None:
            season = next((n for n in range(1, 1000) if n not in folders), None)
            if season is None:
                raise ValueError("Нет свободного номера сезона")
        folder = folders.get(season, show_folder / f"Season {season:02d}")
        existing_title = _nfo_title(folder / "season.nfo")
        default_title = f"Сезон {season}"
        if season_name and existing_title and _normalized_name(existing_title) not in (
                _normalized_name(default_title), _normalized_name(season_name)):
            raise ValueError("Этот номер уже занят другим именованным сезоном")
        label = season_name or existing_title or default_title
        folder.mkdir(parents=True, exist_ok=True)
        _write_nfo(folder / "season.nfo", "season", {"title": label, "seasonnumber": str(season)})
        return season, folder


def archive_to_library(src: Path, library_dir: Path, title: str, episode: int,
                       *, season: int | None = None, season_name: str = "", voice: str = "", quality: int = 0, provider: str = "",
                       identity: str = "", source_identity: str = "", source_url: str = "", media_tag: str = "anime",
                       outcome: ArchiveOutcome | None = None, episode_title: str = "") -> Path:
    """Publish media and clean Jellyfin metadata for an owner-confirmed series/season."""
    import hashlib
    import os
    import shutil
    import tempfile
    if not library_ready(library_dir):
        raise OSError("Медиатека не подключена или отсутствует маркер .animedl-library")
    from .models import validate_library_details, is_movie
    validate_library_details(title, season, season_name, media_tag)
    with _LIBRARY_LOCK:
        title_name = _limit_utf8(_sanitize(title), 90)
        movie = is_movie(media_tag)
        if movie:
            title_hash = hashlib.sha256((identity or title).encode()).hexdigest()[:10]
            matches = [p for p in library_dir.iterdir() if p.is_dir()
                       and _normalized_name(_nfo_title(p / "movie.nfo")) == _normalized_name(title)]
            if len(matches) > 1:
                raise ValueError("Несколько папок с таким названием фильма: проверь медиатеку")
            show_folder = matches[0] if matches else library_dir / f"{title_name} [{title_hash}]"
            show_folder.mkdir(parents=True, exist_ok=True)
            folder = show_folder
        else:
            show_folder = resolve_library_show(library_dir, title, identity)
            season, folder = resolve_library_season(show_folder, season, season_name)
        existing = _existing_archive(folder, episode, season, voice, movie)
        from .models import episode_name
        display_name = episode_name(episode_title, episode)
        if existing and not movie and display_name == f"Серия {episode}":
            display_name = _nfo_title(existing[0].with_suffix(".nfo")) or display_name
        if outcome is not None:
            outcome.quality = existing[1] if existing else quality
            outcome.updated = not existing or quality > existing[1]
            outcome.upgraded = bool(existing and quality > existing[1])
        if existing and quality <= existing[1]:
            if not movie:
                changed = update_episode_name(existing[0].with_suffix(".nfo"), display_name)
                if outcome is not None:
                    outcome.metadata_updated = changed
            return existing[0]
        variant = hashlib.sha256(_normalized_name(voice).encode()).hexdigest()[:10]
        # Preserve an existing path so Jellyfin retains episode IDs and viewing progress.
        dst = folder / (f"{title_name} - [{variant}].mp4" if movie else f"{title_name} - S{season:02d}E{episode:02d} - [{variant}].mp4")
        if existing:
            dst = existing[0]
        fd, raw = tempfile.mkstemp(prefix=".incoming_", suffix=".part", dir=folder)
        os.close(fd)
        staging = Path(raw)
        try:
            shutil.copy2(src, staging)
            provenance = (f"Озвучка: {voice}; провайдер: {provider}; качество: {quality}p; "
                          f"источник: {source_identity}; {source_url}")
            if movie:
                _write_nfo(folder / "movie.nfo", "movie", {"title": title, "tag": media_tag, "plot": provenance})
                _write_nfo(dst.with_suffix(".nfo"), "movie", {"title": title, "tag": media_tag, "plot": provenance})
            else:
                _write_nfo(show_folder / "tvshow.nfo", "tvshow", {"title": title, "tag": media_tag})
                _write_nfo(dst.with_suffix(".nfo"), "episodedetails", {
                "title": display_name, "showtitle": title,
                "season": str(season), "episode": str(episode), "plot": provenance,
                })
            os.replace(staging, dst)
        finally:
            staging.unlink(missing_ok=True)
        if outcome is not None:
            outcome.quality = quality
        log.info("В медиатеку: %s", dst)
        return dst
