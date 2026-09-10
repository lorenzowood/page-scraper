from __future__ import annotations

from typing import Any

def chrome_desktop_ua(version: str) -> str:
    """Desktop Chrome UA matching the bundled Chromium, not HeadlessChrome."""
    ver = (version or "151.0.0.0").split()[0]
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{ver} Safari/537.36"
    )

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

PRESETS: dict[str, dict[str, Any]] = {
    "desktop": {
        "viewport": {"width": 1440, "height": 900},
        "javascript": True,
        "css": True,
        "is_mobile": False,
        "has_touch": False,
        "device_scale_factor": 1,
    },
    "iphone": {
        "viewport": {"width": 390, "height": 844},
        "user_agent": IPHONE_UA,
        "javascript": True,
        "css": True,
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 1,
    },
    "no-css": {
        "javascript": True,
        "css": False,
    },
    "no-js": {
        "javascript": False,
        "css": True,
    },
}


def known_presets() -> list[str]:
    return list(PRESETS)


def merge_preset(name: str, base: dict[str, Any]) -> dict[str, Any]:
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name}")
    merged: dict[str, Any] = dict(PRESETS[name])
    merged["preset"] = name
    for key, value in base.items():
        if value is None or value == []:
            continue
        if key == "viewport" and isinstance(value, dict):
            current = dict(merged.get("viewport") or {})
            current.update({k: v for k, v in value.items() if v is not None})
            merged["viewport"] = current
        else:
            merged[key] = value
    return merged
