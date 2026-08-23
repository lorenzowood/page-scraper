from datetime import datetime
from pathlib import Path

import pytest

from pagecapture.paths import (
    parse_http_url,
    resolve_output_dir,
    resolve_output_file,
    timestamp_label,
    unique_path,
    unlink_capture_files,
    url_output_dir,
)


def test_parse_http_url_accepts_http_and_https():
    assert parse_http_url(" https://example.com/a ") == "https://example.com/a"
    assert parse_http_url("http://localhost:8081/") == "http://localhost:8081/"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com"),
        ("Example.COM/a", "https://Example.COM/a"),
        ("localhost:8081", "https://localhost:8081"),
        ("//cdn.example/x", "https://cdn.example/x"),
    ],
)
def test_parse_http_url_defaults_https(raw: str, expected: str):
    assert parse_http_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "ftp://example.com", "javascript:alert(1)", "https://"],
)
def test_parse_http_url_rejects_junk(raw: str):
    with pytest.raises(ValueError):
        parse_http_url(raw)


def test_url_output_dir_homepage_uses_colon(tmp_path: Path):
    assert url_output_dir(tmp_path, "https://Example.COM/") == tmp_path / "example.com" / ":"


def test_url_output_dir_sanitizes_path_and_query(tmp_path: Path):
    got = url_output_dir(tmp_path, "https://host.example/foo bar/baz?x=1&y=2")
    assert got == tmp_path / "host.example" / "foo-bar/baz__q_x-1-y-2"


def test_timestamp_label_is_sortable():
    label = timestamp_label(datetime(2026, 8, 23, 3, 5, 9))
    assert label == "2026-08-23 03-05-09"


def test_unique_path_increments(tmp_path: Path):
    first = unique_path(tmp_path, "screenshot 2026-01-01 00-00-00", ".png")
    first.write_bytes(b"a")
    second = unique_path(tmp_path, "screenshot 2026-01-01 00-00-00", ".png")
    assert second.name == "screenshot 2026-01-01 00-00-00 2.png"


def test_resolve_output_dir_stays_inside_root(tmp_path: Path):
    root = tmp_path / "out"
    root.mkdir()
    inside = resolve_output_dir(root, "client-a")
    assert inside == (root / "client-a").resolve()
    with pytest.raises(ValueError):
        resolve_output_dir(root, "../escape")


def test_resolve_output_file_rejects_outside_and_missing(tmp_path: Path):
    root = tmp_path / "out"
    root.mkdir()
    inside = root / "shot.png"
    inside.write_bytes(b"png")
    assert resolve_output_file(root, str(inside)) == inside.resolve()
    assert resolve_output_file(root, str(tmp_path / "secret.png")) is None
    assert resolve_output_file(root, str(root / "missing.png")) is None


def test_unlink_capture_files_removes_media_and_empty_dirs(tmp_path: Path):
    root = tmp_path / "out"
    folder = root / "example.com" / ":"
    folder.mkdir(parents=True)
    shot = folder / "screenshot 2026-01-01 00-00-00.png"
    dom = folder / "dom 2026-01-01 00-00-00.html"
    meta = folder / "meta.json"
    shot.write_bytes(b"p")
    dom.write_text("<html></html>")
    meta.write_text("{}")
    removed = unlink_capture_files(
        root,
        [
            {
                "screenshot_path": str(shot),
                "dom_path": str(dom),
                "video_path": None,
                "output_path": str(folder),
            }
        ],
    )
    assert removed >= 2
    assert not shot.exists()
    assert not folder.exists()
    assert root.exists()
