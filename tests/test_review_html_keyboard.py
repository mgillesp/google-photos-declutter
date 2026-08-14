"""review.html keyboard-review support.

No browser test infrastructure lives in this repo, so this stays at the
level pytest can actually check: the generated HTML/JS contains the
keyboard-handling code, and the pre-existing click-handling code is still
there alongside it. The real interactive behaviour (focus moving, groups
being skipped correctly, the lightbox suppressing shortcuts) was exercised
by hand against a live fixture with a real DOM (jsdom) during development.
"""
from __future__ import annotations

from pathlib import Path

from conftest import make_test_image, requires_full_toolchain, run_script, write_sidecar


@requires_full_toolchain
def test_review_html_has_keyboard_and_click_handlers(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    photos = batch / "takeout" / "Photos"
    for i in range(2):
        p = photos / f"IMG_{i:04d}.jpg"
        make_test_image(p, color=(10 * i, 20, 30))
        write_sidecar(p, p.name, timestamp=1_000_000_000 + i)

    result = run_script("01_analyze.py", str(batch))
    assert result.returncode == 0, result.stderr

    html = (batch / "review.html").read_text()

    # Keyboard navigation state + functions.
    for needle in [
        "var allCards = []",
        "var focusedIndex = -1",
        "function cardUnit(card)",
        "function setFocus(index)",
        "function moveFocus(delta)",
        "function toggleFocused()",
        "function acceptGroupAndAdvance()",
        "function showHelp()",
        "function hideHelp()",
    ]:
        assert needle in html, f"missing keyboard-review code: {needle!r}"

    # The keydown listener wires up the documented shortcuts.
    assert 'document.addEventListener("keydown"' in html
    for key_case in ['"ArrowDown"', '"ArrowUp"', '"j"', '"k"', 'case " "',
                     '"Enter"', '"?"', '"Escape"']:
        assert key_case in html, f"keydown handler missing case for {key_case!r}"

    # Guard against firing in a text field / while the lightbox or help
    # overlay is open -- this is a hard requirement, not a nicety.
    assert "inTextField" in html
    assert "lightboxOpen" in html
    assert "helpOpen" in html

    # Help overlay markup + toggle button exist.
    assert 'id="help-overlay"' in html
    assert 'id="btn-help"' in html

    # Existing click-to-toggle behaviour must survive alongside the new code.
    assert "function toggle(" in html
    assert ".addEventListener(\"click\"" in html

    # localStorage persistence (pre-existing) must not have been removed.
    assert "localStorage" in html
