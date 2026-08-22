from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("page-scraper.paths")

_UNSAFE = re.compile(r"[^A-Za-z0-9._~-]+")


def parse_http_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        raise ValueError("empty URL")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"not an http(s) URL: {url}")
    return raw


def url_output_dir(root: Path, url: str) -> Path:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    if path in {"", "/"}:
        page = ":"
    else:
        page = path.strip("/")
        page = "/".join(_UNSAFE.sub("-", part) or "-" for part in page.split("/"))
    if parsed.query:
        query = _UNSAFE.sub("-", parsed.query)[:80]
        page = f"{page}__q_{query}" if page != ":" else f"__q_{query}"
    return root / host / page


def timestamp_label(when: datetime | None = None) -> str:
    when = when or datetime.now()
    return when.strftime("%Y-%m-%d %H-%M-%S")


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = directory / f"{stem} {i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate filename in {directory}")


def resolve_output_dir(root: Path, requested: str | None) -> Path:
    root = root.resolve()
    if not requested or requested in {".", "/output"}:
        return root
    raw = Path(requested)
    candidate = raw if raw.is_absolute() else (root / raw)
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("output dir must be inside the configured output root")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


_SIDECARS = {".DS_Store", "Thumbs.db", "desktop.ini", "meta.json"}


def _inside_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def resolve_output_file(root: Path, raw: str | None) -> Path | None:
    """Return a real file under root, or None if missing / outside the tree."""
    if not raw:
        return None
    path = Path(raw)
    if not _inside_root(path, root):
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _is_capture_media(name: str) -> bool:
    return name.startswith("screenshot ") or name.startswith("dom ") or name.startswith("video ")


def _sweep_stale_dir(directory: Path, root: Path) -> int:
    """Drop sidecar files (meta.json, Finder junk) when no captures remain; rmdir empty parents."""
    removed = 0
    try:
        current = directory.resolve()
        root = root.resolve()
    except OSError:
        return 0
    while current != root and root in current.parents:
        if not current.is_dir():
            break
        try:
            entries = list(current.iterdir())
        except OSError:
            break
        files = [path for path in entries if path.is_file()]
        subdirs = [path for path in entries if path.is_dir()]
        if any(_is_capture_media(path.name) for path in files):
            break
        leftovers = [path for path in files if path.name in _SIDECARS]
        unknown = [path for path in files if path.name not in _SIDECARS]
        for path in leftovers:
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.exception("could not delete %s", path)
                return removed
        if unknown or subdirs:
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
    return removed


def unlink_capture_files(root: Path, items: list[dict[str, Any]]) -> int:
    """Delete screenshot/DOM/video files recorded on items, then prune empty folders."""
    removed = 0
    dirs: set[Path] = set()
    for item in items:
        for key in ("screenshot_path", "dom_path", "video_path"):
            raw = item.get(key)
            if not raw:
                continue
            path = Path(raw)
            if not _inside_root(path, root):
                log.warning("refusing to delete path outside output root: %s", path)
                continue
            try:
                resolved = path.resolve()
                if resolved.is_file():
                    resolved.unlink()
                    removed += 1
                if resolved.parent != root.resolve():
                    dirs.add(resolved.parent)
            except OSError:
                log.exception("could not delete %s", path)
        raw_dir = item.get("output_path")
        if raw_dir:
            directory = Path(raw_dir)
            if _inside_root(directory, root):
                try:
                    dirs.add(directory.resolve())
                except OSError:
                    pass
    root = root.resolve()
    for directory in sorted(dirs, key=lambda path: len(path.parts), reverse=True):
        removed += _sweep_stale_dir(directory, root)
    return removed
