"""gui/style.py — the Mark LVII look: type, colour mixing, shapes, button styles.

Everything visual that more than one panel needs lives here, so the HUD has
one voice instead of forty slightly different ones.

Type. No font files ship with the app, and none are downloaded: each role
names a list of families and Qt takes the first one the system has. Windows
has Bahnschrift, a DIN-style face that suits a HUD; macOS and Linux fall
through to their own sans. Numbers use a monospace so they do not jitter
while they change.

Colour. Stylesheets only ever use the palette's own hex values, because the
live re-theme (theme.retheme_all_widgets) works by replacing those exact
strings. Anything mixed or translucent is painted, and painting reads the
palette at paint time, so it re-themes for free.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QFont, QPolygonF

from gui.theme import C

DISPLAY_FAMILIES = ["Bahnschrift", "Inter", "Segoe UI", "SF Pro Display", "Helvetica Neue",
                    "Arial", "sans-serif"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono",
                 "Courier New", "monospace"]
DISPLAY_CSS = ("'Bahnschrift', 'Inter', 'Segoe UI', 'SF Pro Display', 'Helvetica Neue', "
               "'Arial', sans-serif")
MONO_CSS = "'Cascadia Mono', 'Consolas', 'SF Mono', 'Menlo', 'DejaVu Sans Mono', monospace"


def _apply_weight(f: QFont, weight) -> None:
    if weight is None:
        return
    try:
        if int(getattr(weight, "value", weight)) < 0:
            return
    except (TypeError, ValueError):
        return
    f.setWeight(weight)


def hud_font(size: float, weight=None, italic: bool = False) -> QFont:
    """The display face at a point size — the drop-in for the old
    QFont("Courier New", size, weight). One point larger, because Courier
    New's letters are wide and light and the same size reads bigger."""
    f = QFont()
    f.setFamilies(DISPLAY_FAMILIES)
    f.setPointSizeF(float(size) + 1.0)
    _apply_weight(f, weight)
    f.setItalic(bool(italic))
    return f


def mono_font(size: float, weight=None) -> QFont:
    """A monospace at a point size, for keys, codes and columns of figures."""
    f = QFont()
    f.setFamilies(MONO_FAMILIES)
    f.setPointSizeF(float(size))
    _apply_weight(f, weight)
    return f


def px_font(px: float, weight=QFont.Weight.Normal, mono: bool = False,
            spacing: float = 0.0) -> QFont:
    """A face at a pixel size, for painted widgets that lay text out by pixel."""
    f = QFont()
    f.setFamilies(MONO_FAMILIES if mono else DISPLAY_FAMILIES)
    f.setPixelSize(max(1, int(px)))
    _apply_weight(f, weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(spacing))
    return f


def blend(col, a: float, base=None) -> QColor:
    """`col` mixed onto the background (or `base`) by `a` — opaque, so it is
    drawn on the raster engine's fast path."""
    col, bg = QColor(col), QColor(base if base is not None else C.BG)
    k = max(0.0, min(1.0, float(a)))
    return QColor(int(bg.red() + (col.red() - bg.red()) * k),
                  int(bg.green() + (col.green() - bg.green()) * k),
                  int(bg.blue() + (col.blue() - bg.blue()) * k))


def with_alpha(col, a: float) -> QColor:
    c = QColor(col)
    c.setAlphaF(max(0.0, min(1.0, float(a))))
    return c


def chamfer(r: QRectF, cut: float, corners=("tl", "br")) -> QPolygonF:
    """A rectangle with the named corners cut at 45° — the HUD's panel shape."""
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    pts = [QPointF(x0 + cut, y0)] if "tl" in corners else [QPointF(x0, y0)]
    pts += ([QPointF(x1 - cut, y0), QPointF(x1, y0 + cut)] if "tr" in corners
            else [QPointF(x1, y0)])
    pts += ([QPointF(x1, y1 - cut), QPointF(x1 - cut, y1)] if "br" in corners
            else [QPointF(x1, y1)])
    pts += ([QPointF(x0 + cut, y1), QPointF(x0, y1 - cut)] if "bl" in corners
            else [QPointF(x0, y1)])
    if "tl" in corners:
        pts.append(QPointF(x0, y0 + cut))
    return QPolygonF(pts)


def btn_css(kind: str = "dim", align: str = "left") -> str:
    """Crisp Neobrutalist button styling with solid 2px borders, punchy contrast and tactile press."""
    pad = "padding: 5px 12px;" if align == "left" else "padding: 5px 8px;"
    base = (f"text-align: {align}; {pad} border-radius: 2px; font-weight: 800; "
            f"letter-spacing: 0.8px; text-transform: uppercase;")
    pressed = "QPushButton:pressed { margin-top: 2px; margin-left: 2px; }"
    if kind == "primary":
        return f"""
            QPushButton {{ background: {C.PRI}; color: #000000;
                border: 2px solid #ffffff; {base} }}
            QPushButton:hover {{ background: #ffffff; color: #000000; border-color: {C.PRI}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; background: {C.PANEL}; }}
            {pressed}"""
    if kind == "on":
        return f"""
            QPushButton {{ background: {C.GREEN}; color: #000000;
                border: 2px solid #ffffff; {base} }}
            QPushButton:hover {{ background: #ffffff; color: #000000; border-color: {C.GREEN}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
            {pressed}"""
    if kind == "choice":
        return f"""
            QPushButton {{ background: rgba(18, 26, 40, 0.95); color: {C.PRI};
                border: 2px solid {C.PRI}; {base} }}
            QPushButton:hover {{ background: {C.PRI}; color: #000000; border-color: #ffffff; }}
            {pressed}"""
    # "dim" and "off"
    return f"""
        QPushButton {{ background: rgba(14, 20, 30, 0.88); color: {C.TEXT_MED if kind == 'dim' else C.TEXT_DIM};
            border: 2px solid {C.BORDER_A}; {base} }}
        QPushButton:hover {{ color: #ffffff; border-color: {C.WHITE}; background: rgba(28, 40, 60, 0.95); }}
        QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        {pressed}"""


def scroll_css() -> str:
    return f"""
        QScrollBar:vertical {{ background: rgba(6, 8, 13, 0.65); width: 6px; border: 1px solid {C.BORDER}; border-radius: 3px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: {C.BORDER_A}; min-height: 24px; border-radius: 3px; }}
        QScrollBar::handle:vertical:hover {{ background: {C.PRI}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QScrollBar:horizontal {{ height: 0; }}"""
