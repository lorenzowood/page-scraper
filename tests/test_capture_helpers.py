import pytest

from pagecapture.capture import (
    CaptureResult,
    _is_nav_loss,
    _playback_fps,
    apply_memory_failure,
    eval_page,
    existing_output,
    is_oom_error,
    is_retryable_failure,
    should_continue_after_goto_error,
)


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Page.evaluate: Execution context was destroyed, most likely because of a navigation", True),
        ("Frame was detached", True),
        ("Target closed", True),
        ("HTTP 503", False),
        ("Timeout 30000ms exceeded", False),
    ],
)
def test_is_nav_loss(message: str, expected: bool):
    assert _is_nav_loss(RuntimeError(message)) is expected


@pytest.mark.parametrize(
    "reason, error, http_status, expected",
    [
        ("timeout", "capture watchdog fired", None, True),
        ("oom", "out of memory (container memory limit): PNG was not written", None, True),
        ("screenshot", "PNG was not written", None, True),
        ("load", "HTTP 403", 403, True),
        ("load", "Page.goto: Timeout 30000ms exceeded.", None, True),
        ("load", "net::ERR_HTTP2_PROTOCOL_ERROR", None, True),
        ("load", "browser error page (chrome-error://chromewebdata/)", 200, True),
        ("load", "HTTP 404", 404, False),
        ("load", "HTTP 500", 500, False),
        ("load", "net::ERR_NAME_NOT_RESOLVED", None, False),
    ],
)
def test_is_retryable_failure(reason, error, http_status, expected):
    assert is_retryable_failure(reason, error, http_status) is expected


def test_continue_after_goto_timeout_if_document_exists():
    exc = RuntimeError("Page.goto: Timeout 30000ms exceeded.")
    assert should_continue_after_goto_error(exc, "https://example.com/") is True
    assert should_continue_after_goto_error(exc, "chrome-error://chromewebdata/") is False
    assert should_continue_after_goto_error(RuntimeError("net::ERR_NAME_NOT_RESOLVED"), "https://x/") is False


def test_playback_fps_uses_wall_clock_span():
    assert _playback_fps(1, 0.0, 1.0, 5) == 5.0
    assert _playback_fps(6, 100.0, 101.0, 5) == pytest.approx(5.0)
    # first-to-last 4s, n/(n-1) correction → 3 / (4 * 3/2) = 0.5
    assert _playback_fps(3, 10.0, 14.0, 5) == pytest.approx(0.5)


class _FlakyPage:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, _expr, *_args):
        self.calls += 1
        if self.calls < 3:
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        return {"ok": True}

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None


async def test_eval_page_retries_after_navigation():
    page = _FlakyPage()
    assert await eval_page(page, "() => 1") == {"ok": True}
    assert page.calls == 3


async def test_eval_page_reraises_other_errors():
    class Boom:
        async def evaluate(self, *_args, **_kwargs):
            raise RuntimeError("HTTP 500")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await eval_page(Boom(), "() => 1")


def test_existing_output_requires_nonempty_file(tmp_path):
    missing = tmp_path / "nope.png"
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    ok = tmp_path / "ok.png"
    ok.write_bytes(b"png")
    assert existing_output(missing) is None
    assert existing_output(empty) is None
    assert existing_output(ok) == str(ok)


def test_is_oom_error_from_chromium_text():
    assert is_oom_error("RESULT_CODE_OOM") is True
    assert is_oom_error("cannot allocate memory") is True
    assert is_oom_error("Target closed") is False


def test_apply_memory_failure_promotes_crash_when_cgroup_kills(monkeypatch):
    monkeypatch.setattr("pagecapture.capture.read_cgroup_oom_kills", lambda: 3)
    result = CaptureResult(
        status="failed",
        reason="crash",
        error="Target closed",
    )
    out = apply_memory_failure(result, oom_before=2)
    assert out.reason == "oom"
    assert out.status == "failed"
    assert "out of memory" in (out.error or "")


def test_apply_memory_failure_hints_when_browser_dies():
    result = CaptureResult(
        status="failed",
        reason="crash",
        error="Target page, context or browser has been closed",
    )
    out = apply_memory_failure(result, oom_before=0)
    assert out.reason == "crash"
    assert "often out of memory" in (out.error or "")


def test_apply_memory_failure_keeps_complete_when_png_exists(monkeypatch):
    monkeypatch.setattr("pagecapture.capture.read_cgroup_oom_kills", lambda: 1)
    result = CaptureResult(
        status="complete",
        saved=True,
        screenshot_path="/output/shot.png",
    )
    out = apply_memory_failure(result, oom_before=0)
    assert out.status == "complete"
    assert out.reason is None


def _sof0_jpeg(width: int, height: int) -> bytes:
    payload = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes(
        [1, 1, 0x11, 0]
    )
    return b"\xff\xd8\xff\xc0" + (2 + len(payload)).to_bytes(2, "big") + payload + b"\xff\xd9"


def test_jpeg_size_reads_sof0():
    from pagecapture.capture import jpeg_size

    assert jpeg_size(_sof0_jpeg(1440, 5532)) == (1440, 5532)
    assert jpeg_size(b"not a jpeg") is None


def test_ffmpeg_scale_filter_pads_to_max_even_size():
    from pagecapture.capture import ffmpeg_scale_filter

    frames = [_sof0_jpeg(1440, 5200), _sof0_jpeg(1450, 5532)]
    vf = ffmpeg_scale_filter(frames)
    assert "pad=1450:5532" in vf
    assert ffmpeg_scale_filter([]) == "scale=trunc(iw/2)*2:trunc(ih/2)*2"
