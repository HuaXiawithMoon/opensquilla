from __future__ import annotations

import re
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui.themes import (
    DEFAULT_THEME,
    THEME_NAMES,
    handle_theme_command,
)
from opensquilla.cli.ui import console

_THEME_MJS = (
    Path(__file__).resolve().parents[4] / "src/opensquilla/cli/tui/opentui/package/src/theme.mjs"
)


def _js_palette_names() -> list[str]:
    text = _THEME_MJS.read_text(encoding="utf-8")
    block = text.split("PALETTES = Object.freeze({", 1)[1].split("});", 1)[0]
    # Theme entries are at 2-space indent with an object value (`: {`); the inner
    # tokens are deeper-indented string values, so they do not match.
    return re.findall(r'^ {2}"?([a-z][a-z0-9-]*)"?:\s*\{', block, re.M)


def test_theme_names_match_js_registry() -> None:
    js_names = _js_palette_names()
    assert js_names, "could not parse PALETTES from theme.mjs"
    assert list(THEME_NAMES) == js_names
    assert DEFAULT_THEME in THEME_NAMES


@pytest.mark.asyncio
async def test_theme_command_lists_available_themes() -> None:
    with console.capture() as capture:
        await handle_theme_command("/theme", None)
    out = capture.get()
    for name in THEME_NAMES:
        assert name in out


@pytest.mark.asyncio
async def test_theme_command_switches_via_send_message() -> None:
    class _FakeOutput:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _FakeOutput()
    with console.capture():
        await handle_theme_command("/theme midnight", output)
    assert output.sent == [("theme.set", {"name": "midnight"})]


@pytest.mark.asyncio
async def test_theme_command_rejects_unknown_theme() -> None:
    class _FakeOutput:
        def __init__(self) -> None:
            self.sent: list[tuple[str, dict[str, object]]] = []

        async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
            self.sent.append((message_type, payload))

    output = _FakeOutput()
    with console.capture() as capture:
        await handle_theme_command("/theme nope", output)
    assert output.sent == []
    assert "Unknown theme" in capture.get()


@pytest.mark.asyncio
async def test_theme_command_explains_opentui_only_on_native() -> None:
    # A native output handle has no send_message; the command must not crash and
    # should explain that themes are OpenTUI-only.
    class _NativeOutput:
        async def write_through(self, payload: str) -> None:
            return None

    with console.capture() as capture:
        await handle_theme_command("/theme ember", _NativeOutput())
    assert "OpenTUI backend" in capture.get()
