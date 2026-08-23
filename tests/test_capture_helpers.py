import pytest

from pagecapture.capture import _is_nav_loss, _playback_fps, eval_page


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
