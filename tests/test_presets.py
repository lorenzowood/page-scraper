import pytest

from pagecapture.presets import known_presets, merge_preset


def test_known_presets_include_desktop_and_iphone():
    names = known_presets()
    assert names[:2] == ["desktop", "iphone"]
    assert "no-css" in names and "no-js" in names


def test_merge_preset_overlays_job_options():
    merged = merge_preset(
        "iphone",
        {
            "stable_ms": 5000,
            "viewport": {"width": 400},
            "javascript": None,
        },
    )
    assert merged["preset"] == "iphone"
    assert merged["is_mobile"] is True
    assert merged["viewport"]["width"] == 400
    assert merged["viewport"]["height"] == 844
    assert merged["javascript"] is True
    assert merged["stable_ms"] == 5000


def test_merge_preset_unknown():
    with pytest.raises(ValueError, match="unknown preset"):
        merge_preset("tablet", {})
