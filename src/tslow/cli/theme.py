"""Tema "TSlow Blueprint": solo nero puro e bianco puro, gerarchia con maiuscolo/grassetto/bordi.

Usato dallo schema colori di Windows Terminal (`install.py`, M5) che mappa tutti i 16 colori ANSI su bianco e lo sfondo su nero.
"""

from __future__ import annotations

BLACK = "#000000"
WHITE = "#FFFFFF"

# Windows Terminal's 16 ANSI colors, minus the two black ones that stay black.
_ANSI_COLOR_NAMES = (
    "red", "green", "yellow", "blue", "purple", "cyan", "white",
    "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightPurple", "brightCyan", "brightWhite",
)

WINDOWS_TERMINAL_COLOR_SCHEME = {
    "name": "TSlow Blueprint",
    "background": BLACK,
    "foreground": WHITE,
    "cursorColor": WHITE,
    "selectionBackground": WHITE,
    "black": BLACK,
    "brightBlack": BLACK,
    **{name: WHITE for name in _ANSI_COLOR_NAMES},
}
