
from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# Qt's video backend prints the ffmpeg stream banner — codec, bitrate, the
# whole signed googlevideo URL — to the console for every stream it opens. That
# is two screenfuls per video, it looks like an error to anyone reading the log,
# and it puts a URL carrying the viewer's IP address into a terminal people
# paste into bug reports. Silenced here, before Qt initialises its logging.
# An explicit setting from the user is left alone.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping SYNC from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

try:
    from core.avatar import HoloAvatar
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    HoloAvatar = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "S.Y.N.C"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#0e0f0e"   # deep midnight obsidian glass base
    PANEL     = "#151716"   # smoked frosted glass surface tint
    PANEL2    = "#1d211e"   # elevated frosted glass surface tint
    BORDER    = "#2f3d34"   # crisp glass frame border
    BORDER_A  = "#3e5746"   # lit glass border / card outline
    BORDER_B  = "#537a60"   # high-contrast specular / active highlight
    PRI       = "#00ff50"   # vibrant electric cyber green / neobrutalist primary
    PRI_DIM   = "#09b840"   # saturated neon midtone
    PRI_GHO   = "#0b3819"   # translucent neon glass glow
    ACC       = "#ff9900"   # neobrutalist electric amber / gold
    ACC2      = "#d946ef"   # neobrutalist electric magenta / violet
    GREEN     = "#00ff50"   # neobrutalist neon cyber lime / green
    GREEN_D   = "#059669"   # deep emerald
    RED       = "#ff2a55"   # high-voltage crimson
    MUTED_C   = "#ff2a6d"   # electric neon rose
    TEXT      = "#ffffff"   # pure, crisp 100% white for maximum punch
    TEXT_DIM  = "#5a708c"   # frosted muted slate
    TEXT_MED  = "#9db2cb"   # frosted cool silver
    WHITE     = "#ffffff"   # pure white
    DARK      = "#090a09"   # sunken deep obsidian
    BAR_BG    = "#212622"   # smoked glass track bar background


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = "#00ff50"


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Applies the chosen accent color across the app with bold brutalist prominence.
    The accent prominently drives PRI, PRI_DIM, PRI_GHO, and gives card borders
    and structural lines an architectural, high-definition presence, while keeping
    typography (TEXT, WHITE, TEXT_MED, TEXT_DIM) pure and high-contrast.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    def _rgb_hex(h: float, s: float, v: float) -> str:
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
        return "#{:02x}{:02x}{:02x}".format(int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))

    acc_h, acc_s, acc_v = _hsv(accent_hex)
    grey = acc_s < 0.08

    # 1. Neobrutalist primary accents (hyper-vibrant, electric)
    C.PRI = accent_hex
    C.PRI_DIM = _rgb_hex(acc_h, min(1.0, acc_s * 0.95), max(0.55, acc_v * 0.72))
    C.PRI_GHO = _rgb_hex(acc_h, min(0.80, acc_s * 0.85), 0.22) if not grey else "#16202c"

    # 2. Lit glass borders with neobrutalist structural presence
    b_sat = 0.0 if grey else min(0.38, acc_s * 0.32)
    C.BORDER   = _rgb_hex(acc_h, b_sat * 0.70, 0.24)
    C.BORDER_A = _rgb_hex(acc_h, b_sat * 0.88, 0.34)
    C.BORDER_B = _rgb_hex(acc_h, b_sat, 0.48)

    # 3. Smoked frosted glass surfaces with subtle cool refraction
    bg_sat = 0.0 if grey else min(0.10, acc_s * 0.08)
    panel_sat = 0.0 if grey else min(0.15, acc_s * 0.12)
    C.DARK   = _rgb_hex(acc_h, bg_sat * 0.8, 0.038)
    C.BG     = _rgb_hex(acc_h, bg_sat, 0.058)
    C.PANEL  = _rgb_hex(acc_h, panel_sat, 0.092)
    C.PANEL2 = _rgb_hex(acc_h, panel_sat * 1.1, 0.13)
    C.BAR_BG = _rgb_hex(acc_h, panel_sat * 1.2, 0.15)

    # 4. Pure, crisp typography for maximum punch and legibility
    C.WHITE    = "#ffffff"
    C.TEXT     = "#ffffff"
    C.TEXT_MED = "#9db2cb"
    C.TEXT_DIM = "#5a708c"

    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c

