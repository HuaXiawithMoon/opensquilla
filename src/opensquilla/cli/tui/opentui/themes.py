"""Theme catalog and the ``/theme`` command for the OpenTUI footer host.

The themes themselves live in the JS host (``package/src/theme.mjs``). This module
only mirrors the theme NAMES so the CLI can list/validate them and drive live
switching by sending a ``theme.set`` IPC message through the OpenTUI output handle.
The name list is kept in sync with ``theme.mjs`` by a conformance test.
"""

from __future__ import annotations

# Must equal the keys of PALETTES in
# src/opensquilla/cli/tui/opentui/package/src/theme.mjs (enforced by
# tests/unit/cli/tui/test_opentui_themes.py::test_theme_names_match_js_registry).
THEME_NAMES: tuple[str, ...] = (
    "opensquilla-dark",
    "opensquilla-light",
    "midnight",
    "ember",
    "slate",
    "high-contrast",
    "nord",
    "mono",
)
DEFAULT_THEME = "opensquilla-dark"
THEME_ENV_VAR = "OPENSQUILLA_TUI_THEME"


async def handle_theme_command(cmd: str, tui_output: object | None) -> None:
    """Handle ``/theme`` (list) and ``/theme <name>`` (live switch).

    Switching is OpenTUI-only: it sends ``theme.set`` through the host output
    handle. On the native backend (no ``send_message``) it explains that themes
    apply to the OpenTUI backend.
    """
    from opensquilla.cli.ui import console  # noqa: PLC0415 - keep module import-light

    names = ", ".join(THEME_NAMES)
    parts = cmd.split()
    if len(parts) == 1:
        console.print(f"[dim]Themes:[/dim] {names}")
        console.print(
            "[dim]Switch with[/dim] /theme <name>  [dim](OpenTUI backend only).[/dim]"
        )
        return

    name = parts[1].strip().lower()
    if name not in THEME_NAMES:
        console.print(f"[yellow]Unknown theme '{name}'.[/yellow] [dim]Available:[/dim] {names}")
        return

    send_message = getattr(tui_output, "send_message", None)
    if not callable(send_message):
        console.print(
            "[yellow]Themes apply to the OpenTUI backend "
            "(set OPENSQUILLA_TUI_BACKEND=opentui).[/yellow]"
        )
        return

    await send_message("theme.set", {"name": name})
    console.print(f"[green]Theme:[/green] {name}")
