import sys
import os
import shutil
import subprocess

# Wayland won't let an app position its own windows, which breaks cat dragging
# and bubble placement. Force xcb (X11/XWayland), which honors move().
if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "xcb"

import csv
import math
import time
import ctypes
import sqlite3
from datetime import datetime, timedelta

from PySide6.QtWidgets import (QApplication, QWidget, QLineEdit, QListWidget,
                               QComboBox, QSystemTrayIcon, QMenu, QFileDialog,
                               QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                               QCheckBox, QMessageBox)
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QPolygonF, QFont, QIcon, QPixmap

# ============================================================
#  CONFIG / PALETTES
# ============================================================
if getattr(sys, "frozen", False):          # compiled by PyInstaller
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

FUR_COLORS = {
    "white": "#f2f2f6", "gray": "#9aa0a6", "black": "#3a3a44",
    "brown": "#9c6b3f", "orange": "#e8943a",
}
CAT_NAMES = list(FUR_COLORS)
PINK = QColor("#f6c4cb")

SIZE_LEVELS = {"Small": 0.75, "Medium": 1.0, "Large": 1.35}

ACCENT = QColor("#7aa2f7")
GREEN  = QColor("#4caf7d")
RED    = QColor("#e05c6e")

_LIGHT = dict(bg="#ffffff",  text="#2a2a32", muted="#8a8a96",
              btn="#f0f0f4", border="#d8d8e0", bar_track="#e6e6ec")
_DARK  = dict(bg="#0d0d14",  text="#e2e8ff", muted="#9399b2",
              btn="#1a1a28", border="#3a3c52", bar_track="#252535")

BUB_BG = BUB_TEXT = BUB_MUTED = BUB_BTN = BUB_BORDER = BAR_TRACK = None
SHADOW   = QColor(0, 0, 0, 40)
_IS_DARK = False

FONT_NAME = "Segoe UI"


def apply_palette(dark: bool):
    global BUB_BG, BUB_TEXT, BUB_MUTED, BUB_BTN, BUB_BORDER, BAR_TRACK, SHADOW, _IS_DARK
    _IS_DARK = dark
    src        = _DARK if dark else _LIGHT
    BUB_BG     = QColor(src["bg"])
    BUB_TEXT   = QColor(src["text"])
    BUB_MUTED  = QColor(src["muted"])
    BUB_BTN    = QColor(src["btn"])
    BUB_BORDER = QColor(src["border"])
    BAR_TRACK  = QColor(src["bar_track"])
    SHADOW     = QColor(0, 0, 0, 80 if dark else 40)


apply_palette(False)  # overridden in __main__ once settings load


def shade(hexc, f):
    r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
    if f <= 1:
        r, g, b = int(r * f), int(g * f), int(b * f)
    else:
        t = f - 1
        r, g, b = int(r + (255-r)*t), int(g + (255-g)*t), int(b + (255-b)*t)
    return QColor(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def palette_for(hexc):
    bright = (int(hexc[1:3], 16) + int(hexc[3:5], 16) + int(hexc[5:7], 16)) / 3
    if bright < 90:
        return QColor(hexc), shade(hexc, 1.8), QColor("#f2c84a")
    return QColor(hexc), shade(hexc, 0.42), QColor("#2a2a30")


def poly(*pts):
    return QPolygonF([QPointF(x, y) for x, y in pts])


# ============================================================
#  DATABASE
# ============================================================
conn = sqlite3.connect(os.path.join(SCRIPT_DIR, "clocker_history.db"))
cur  = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS sessions (stamp TEXT, minutes REAL)")
cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
conn.commit()
if "project" not in [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]:
    cur.execute("ALTER TABLE sessions ADD COLUMN project TEXT DEFAULT 'General'")
    conn.commit()

_cache: dict = {}


def get_setting(key, default):
    if key not in _cache:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        _cache[key] = row[0] if row else default
    return _cache[key]


def save_setting(key, value):
    _cache[key] = str(value)
    cur.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
    conn.commit()


def get_projects():
    items = [p for p in get_setting("projects", "General").split("||") if p.strip()]
    return items if items else ["General"]


def save_projects(items):
    save_setting("projects", "||".join(items))


def get_goal_hours():
    try:
        return float(get_setting("daily_goal", "8"))
    except ValueError:
        return 8.0


def today_minutes():
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COALESCE(SUM(minutes),0) FROM sessions WHERE substr(stamp,1,10)=?",
                (today,))
    return cur.fetchone()[0] or 0.0


def log_session(minutes, project):
    if minutes < 0.1:
        return
    cur.execute("INSERT INTO sessions (stamp, minutes, project) VALUES (?,?,?)",
                (datetime.now().strftime("%Y-%m-%d  %H:%M"), minutes, project))
    conn.commit()


def project_totals():
    now     = datetime.now()
    today_s = now.strftime("%Y-%m-%d")
    monday  = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT COALESCE(project,'General'),
               SUM(minutes),
               SUM(CASE WHEN substr(stamp,1,10) >= ? THEN minutes ELSE 0 END),
               SUM(CASE WHEN substr(stamp,1,10) =  ? THEN minutes ELSE 0 END)
        FROM sessions GROUP BY COALESCE(project,'General') ORDER BY SUM(minutes) DESC
    """, (monday, today_s))
    return [(p, (t or 0)/60, (w or 0)/60, (d or 0)/60)
            for p, t, w, d in cur.fetchall()]


def export_csv(path):
    cur.execute("SELECT stamp, minutes, COALESCE(project,'General') FROM sessions ORDER BY rowid")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Project", "Date", "Time", "Minutes"])
        for stamp, minutes, proj in cur.fetchall():
            parts    = stamp.strip().split()
            date     = parts[0] if len(parts) >= 1 else stamp
            time_str = parts[-1] if len(parts) >= 2 else ""
            w.writerow([proj, date, time_str, round(minutes, 2)])
    return path


# ============================================================
#  STARTUP  (Windows only — HKCU Run registry key, no admin needed)
# ============================================================
_RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "Clocker9000"


def startup_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY)
        winreg.QueryValueEx(key, _APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_startup(enable):
    if sys.platform != "win32":
        return
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
    else:
        try:
            winreg.DeleteValue(key, _APP_NAME)
        except OSError:
            pass
    winreg.CloseKey(key)


# ============================================================
#  AFK DETECTION  (Windows only — uses GetLastInputInfo)
# ============================================================
def _afk_threshold():
    try:
        m = max(0, int(get_setting("afk_minutes", "2")))
        s = max(0, min(59, int(get_setting("afk_secs", "0"))))
        return max(10, m * 60 + s)
    except ValueError:
        return 120


if sys.platform == "win32":
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _get_idle_seconds():
    if sys.platform != "win32":
        return 0.0
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)


# ============================================================
#  THE CAT OVERLAY
# ============================================================
class CatOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.start_time    = None
        self.frame         = 0
        self._drag         = None
        self._moved        = False
        self._press        = None
        self._hover_frames = -1
        self._was_afk      = False
        self.tray          = None  # set after construction
        self.uninstall_cb  = None  # set from __main__ when running as compiled exe

        self.cat_name = get_setting("cat_name", "gray")
        if self.cat_name not in CAT_NAMES:
            self.cat_name = "gray"
        self.size_name = get_setting("cat_size", "Medium")
        if self.size_name not in SIZE_LEVELS:
            self.size_name = "Medium"

        self.projects        = get_projects()
        cp                   = get_setting("current_project", self.projects[0])
        self.current_project = cp if cp in self.projects else self.projects[0]

        saved_start = get_setting("session_start", "")
        if saved_start:
            try:
                t = float(saved_start)
                if datetime.fromtimestamp(t).date() == datetime.now().date():
                    self.start_time = t
                else:
                    save_setting("session_start", "")
            except (ValueError, OSError):
                save_setting("session_start", "")

        self._apply_size()

        px = int(get_setting("pos_x", -1))
        py = int(get_setting("pos_y", -1))
        if px < 0 or py < 0:
            sg = QApplication.primaryScreen().geometry()
            px = (sg.width()  - self.width())  // 2
            py = (sg.height() - self.height()) // 2
        self.move(px, py)

        self.bubble = Bubble(self)
        self.timer  = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)

    def _apply_size(self):
        self.scale = SIZE_LEVELS[self.size_name]
        px = int(150 * self.scale)
        self.resize(px, px)

    @property
    def working(self):
        return self.start_time is not None

    def toggle_clock(self):
        self._was_afk = False  # manual action overrides AFK tracking
        if self.start_time is None:
            self.start_time = time.time()
            save_setting("session_start", str(self.start_time))
        else:
            log_session((time.time() - self.start_time) / 60, self.current_project)
            self.start_time = None
            save_setting("session_start", "")
        self.update()

    def switch_project(self, name):
        if self.start_time is not None and name != self.current_project:
            log_session((time.time() - self.start_time) / 60, self.current_project)
            self.start_time = time.time()
            save_setting("session_start", str(self.start_time))
        self.current_project = name
        save_setting("current_project", name)

    def _tick(self):
        self.frame += 1
        if self._hover_frames >= 0:
            self._hover_frames += 1
        if self.frame % 83 == 0:   # ~every 5 seconds
            self._check_afk()
        self.update()
        if self.bubble.isVisible():
            self.bubble.update()

    def _check_afk(self):
        if get_setting("afk_auto_clock", "0") != "1":
            self._was_afk = False
            return
        idle      = _get_idle_seconds()
        threshold = _afk_threshold()
        if idle >= threshold and self.working and not self._was_afk:
            afk_start  = time.time() - idle
            logged_min = max(0.0, (afk_start - self.start_time) / 60)
            log_session(logged_min, self.current_project)
            self.start_time = None
            save_setting("session_start", "")
            self._was_afk   = True
            self.update()
            if self.bubble.isVisible():
                self.bubble.update()
            if self.tray:
                idle_str = (f"{int(idle)//60}m {int(idle)%60}s"
                            if idle >= 60 else f"{int(idle)}s")
                self.tray.showMessage(
                    "Clocker 9000",
                    f"AFK for {idle_str} — clocked out ({logged_min:.0f} min logged)",
                    QSystemTrayIcon.MessageIcon.Information, 4000)
        elif idle < threshold and self._was_afk:
            self.start_time = time.time()
            save_setting("session_start", str(self.start_time))
            self._was_afk   = False
            self.update()
            if self.bubble.isVisible():
                self.bubble.update()
            if self.tray:
                self.tray.showMessage(
                    "Clocker 9000", "Welcome back — clocked in",
                    QSystemTrayIcon.MessageIcon.Information, 2000)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.scale(self.scale, self.scale)
            self._draw_cat(p)
        finally:
            p.end()

    def _draw_cat(self, p):
        fur, outline, eye = palette_for(FUR_COLORS[self.cat_name])
        pen = QPen(outline, 3)
        p.setPen(pen)

        if self.working:
            sw   = math.sin(self.frame * 0.15) * 10
            tail = QPainterPath()
            tail.moveTo(96, 118)
            tail.cubicTo(120, 116, 130+sw, 96, 124+sw, 78)
            p.setPen(QPen(fur, 9, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(tail)
            p.setPen(pen)
            body = QPainterPath()
            body.addEllipse(40, 70, 70, 72)
            p.setBrush(fur)
            p.drawPath(body)
            p.drawEllipse(50, 34, 50, 50)
            for pts in [[(56,44),(60,14),(76,40)], [(74,40),(90,14),(94,44)]]:
                p.drawPolygon(poly(*pts))
            p.setPen(Qt.NoPen)
            p.setBrush(PINK)
            for pts in [[(62,40),(64,24),(72,39)], [(78,39),(86,24),(88,40)]]:
                p.drawPolygon(poly(*pts))
            p.setPen(pen)
            p.setBrush(fur)
            p.drawEllipse(64, 132, 13, 13)
            p.drawEllipse(76, 132, 13, 13)
            blink = (self.frame % 45) < 3
            p.setBrush(eye)
            p.setPen(Qt.NoPen)
            if not blink:
                p.drawEllipse(63, 54, 7, 9)
                p.drawEllipse(80, 54, 7, 9)
            else:
                p.setPen(QPen(eye, 2))
                p.drawLine(63, 59, 70, 59)
                p.drawLine(80, 59, 87, 59)
            p.setPen(Qt.NoPen)
            p.setBrush(eye)
            p.drawPolygon(poly((72,66),(78,66),(75,70)))
            p.setPen(QPen(outline, 2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            m  = QPainterPath(); m.moveTo(75, 71);  m.quadTo(71, 75, 67, 72);  p.drawPath(m)
            m2 = QPainterPath(); m2.moveTo(75, 71); m2.quadTo(79, 75, 83, 72); p.drawPath(m2)
        else:
            p.setBrush(fur)
            p.drawEllipse(41, 99, 68, 46)
            p.drawEllipse(55, 93, 40, 40)
            for pts in [[(61,101),(63,79),(77,99)], [(75,99),(89,79),(91,101)]]:
                p.drawPolygon(poly(*pts))
            p.setPen(Qt.NoPen)
            p.setBrush(PINK)
            for pts in [[(65,98),(67,86),(74,97)], [(78,97),(85,86),(87,98)]]:
                p.drawPolygon(poly(*pts))
            peek = 0 <= self._hover_frames < 17
            if peek:
                p.setPen(Qt.NoPen)
                p.setBrush(eye)
                for ex in (65, 77):
                    p.setClipRect(QRectF(ex, 109, 7, 8))
                    p.drawEllipse(ex, 105, 7, 8)
                p.setClipping(False)
            else:
                p.setPen(QPen(eye, 2, Qt.SolidLine, Qt.RoundCap))
                e1 = QPainterPath(); e1.moveTo(65, 111); e1.quadTo(69, 114, 73, 111); p.drawPath(e1)
                e2 = QPainterPath(); e2.moveTo(77, 111); e2.quadTo(81, 114, 85, 111); p.drawPath(e2)
            p.setPen(Qt.NoPen)
            p.setBrush(eye)
            p.drawPolygon(poly((72,117),(78,117),(75,121)))
            p.setPen(QPen(outline, 2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            mm  = QPainterPath(); mm.moveTo(75, 122);  mm.quadTo(71, 125, 67, 123);  p.drawPath(mm)
            mm2 = QPainterPath(); mm2.moveTo(75, 122); mm2.quadTo(79, 125, 83, 123); p.drawPath(mm2)
            bob = int(math.sin(self.frame * 0.1) * 2)
            p.setPen(QColor("#cfd3ff"))
            p.setFont(QFont(FONT_NAME, 11, QFont.Bold))
            p.drawText(104, 77 + bob, "z Z")

    def enterEvent(self, e):
        self._hover_frames = 0

    def leaveEvent(self, e):
        self._hover_frames = -1

    def mousePressEvent(self, e):
        self._drag  = e.globalPosition().toPoint() - self.pos()
        self._press = e.globalPosition().toPoint()
        self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        if (e.globalPosition().toPoint() - self._press).manhattanLength() > 4:
            self._moved = True
            if self.bubble.isVisible():
                self.bubble.hide()
        pos    = e.globalPosition().toPoint() - self._drag
        screen = QApplication.primaryScreen().geometry()
        x = max(screen.left(), min(pos.x(), screen.right()  - self.width()))
        y = max(screen.top(),  min(pos.y(), screen.bottom() - self.height()))
        self.move(x, y)

    def mouseReleaseEvent(self, e):
        if not self._moved:
            self.toggle_bubble()
        else:
            save_setting("pos_x", self.x())
            save_setting("pos_y", self.y())
        self._drag = None

    def toggle_bubble(self):
        if self.bubble.isVisible():
            self.bubble.hide()
        else:
            self.bubble.show_near_cat()

    def cycle_cat(self):
        i = (CAT_NAMES.index(self.cat_name) + 1) % len(CAT_NAMES)
        self.cat_name = CAT_NAMES[i]
        save_setting("cat_name", self.cat_name)
        self._update_tray_icon()
        self.update()

    def _update_tray_icon(self):
        if self.tray:
            self.tray.setIcon(QIcon(_make_tray_pixmap(FUR_COLORS[self.cat_name])))

    def cycle_size(self):
        sizes = list(SIZE_LEVELS)
        self.size_name = sizes[(sizes.index(self.size_name) + 1) % len(sizes)]
        save_setting("cat_size", self.size_name)
        self._apply_size()
        self._clamp_to_screen()
        self.update()

    def _clamp_to_screen(self):
        screen = QApplication.primaryScreen().geometry()
        x = max(screen.left(), min(self.x(), screen.right()  - self.width()))
        y = max(screen.top(),  min(self.y(), screen.bottom() - self.height()))
        if x != self.x() or y != self.y():
            self.move(x, y)
            save_setting("pos_x", x)
            save_setting("pos_y", y)


# ============================================================
#  THE SPEECH BUBBLE  (frameless translucent, pointer at bottom)
# ============================================================
class Bubble(QWidget):
    def __init__(self, cat):
        super().__init__()
        self.cat = cat
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.view         = "main"
        self._buttons     = []
        self.bw           = 230
        self.point_h      = 12
        self.point_up     = True
        self.point_side   = None   # "left" or "right" when cat is near a screen edge
        self._x_off       = 0     # horizontal content offset when tail is on the left
        self.body_h       = 200
        self._proj_totals = []
        self._today_min   = 0.0   # cached today_minutes() value
        self._today_min_ts = 0.0  # time.time() of last cache refresh
        QApplication.instance().applicationStateChanged.connect(self._on_app_state)

        self.goal_edit = QLineEdit(self)
        self.goal_edit.setAlignment(Qt.AlignCenter)
        self.goal_edit.setFont(QFont(FONT_NAME, 14, QFont.Bold))
        self.goal_edit.editingFinished.connect(self._goal_typed)
        self.goal_edit.hide()

        self.afk_min_edit = QLineEdit(self)
        self.afk_min_edit.setAlignment(Qt.AlignCenter)
        self.afk_min_edit.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        self.afk_min_edit.editingFinished.connect(self._afk_min_typed)
        self.afk_min_edit.hide()

        self.afk_sec_edit = QLineEdit(self)
        self.afk_sec_edit.setAlignment(Qt.AlignCenter)
        self.afk_sec_edit.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        self.afk_sec_edit.editingFinished.connect(self._afk_sec_typed)
        self.afk_sec_edit.hide()

        self.proj_edit = QLineEdit(self)
        self.proj_edit.setPlaceholderText("New project…")
        self.proj_edit.setFont(QFont(FONT_NAME, 10))
        self.proj_edit.returnPressed.connect(self._add_project)
        self.proj_edit.hide()

        self.manage_proj_combo = QComboBox(self)
        self.manage_proj_combo.setFont(QFont(FONT_NAME, 10))
        self.manage_proj_combo.hide()

        self.proj_combo = QComboBox(self)
        self.proj_combo.setFont(QFont(FONT_NAME, 10))
        self.proj_combo.activated.connect(self._combo_picked)
        self.proj_combo.hide()

        self.hist_list = QListWidget(self)
        self.hist_list.setFont(QFont("Consolas", 8))
        self.hist_list.hide()

        self._inputs = [self.goal_edit, self.afk_min_edit, self.afk_sec_edit,
                        self.proj_edit, self.manage_proj_combo, self.proj_combo, self.hist_list]
        self._update_widget_styles()

    def show_near_cat(self):
        self.view = "main"
        self._hide_inputs()
        self._reposition()
        self.show()
        self.raise_()
        self.activateWindow()
        self.update()

    def _hide_inputs(self):
        for w in self._inputs:
            w.hide()

    def _update_widget_styles(self):
        bg, text, btn, bord, acc = (
            BUB_BG.name(), BUB_TEXT.name(), BUB_BTN.name(),
            BUB_BORDER.name(), ACCENT.name()
        )
        field = (f"QLineEdit{{background:{btn};border:1px solid {bord};"
                 f"border-radius:8px;padding:4px 8px;color:{text};"
                 f"selection-background-color:{acc};}}")
        arrow_col = text.replace("#", "%23")
        arrow_svg = (f"url(\"data:image/svg+xml,"
                     f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 6'>"
                     f"<polygon points='0,0 10,0 5,6' fill='{arrow_col}'/>"
                     f"</svg>\")")
        combo = (f"QComboBox{{background:{btn};border:1px solid {bord};"
                 f"border-radius:8px;padding:4px 10px;color:{text};}}"
                 f"QComboBox::drop-down{{border:none;width:20px;}}"
                 f"QComboBox::down-arrow{{image:{arrow_svg};width:10px;height:6px;}}"
                 f"QComboBox QAbstractItemView{{background:{bg};color:{text};"
                 f"selection-background-color:{acc};selection-color:white;"
                 f"border:1px solid {bord};outline:none;}}")
        lst   = (f"QListWidget{{background:{btn};border:none;border-radius:8px;"
                 f"color:{text};padding:2px;font-family:Consolas;}}"
                 f"QListWidget::item{{padding:2px 6px;}}")
        self.goal_edit.setStyleSheet(field)
        self.afk_min_edit.setStyleSheet(field)
        self.afk_sec_edit.setStyleSheet(field)
        self.proj_edit.setStyleSheet(field)
        self.manage_proj_combo.setStyleSheet(combo)
        self.proj_combo.setStyleSheet(combo)
        self.hist_list.setStyleSheet(lst)

    def _layout(self):
        heights = {
            "main":          152,
            "goal":          134,
            "confirm_clear": 110,
            "projects":      190,
            "settings":      320 if sys.platform == "win32" else 280,
            "history":       48 + max(len(self._proj_totals), 1) * 22 + 218,
        }
        self.body_h = heights.get(self.view, 200)
        if self.point_side:
            self.resize(self.bw + self.point_h, self.body_h)
        else:
            self.resize(self.bw, self.body_h + self.point_h)

    def _reposition(self):
        cg     = self.cat.frameGeometry()
        screen = QApplication.primaryScreen().geometry()
        thresh = self.bw // 2 + 8   # if cat center is within this of an edge, go sideways

        if screen.right() - cg.center().x() < thresh:
            self.point_side = "right"   # bubble left of cat, tail points right
        elif cg.center().x() - screen.left() < thresh:
            self.point_side = "left"    # bubble right of cat, tail points left
        else:
            self.point_side = None

        self._layout()

        if self.point_side == "right":
            x = cg.left() - self.width() - 4
            y = cg.center().y() - self.height() // 2
        elif self.point_side == "left":
            x = cg.right() + 4
            y = cg.center().y() - self.height() // 2
        else:
            x = cg.center().x() - self.width() // 2
            y = cg.top() - self.height() - 4
            x = max(4, min(x, screen.width() - self.width() - 4))
            if y < 4:
                y, self.point_up = cg.bottom() + 4, False
            else:
                self.point_up = True

        y = max(4, min(y, screen.bottom() - self.height() - 4))
        x = max(4, min(x, screen.width()  - self.width()  - 4))
        self.move(x, y)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            self._buttons = []

            if self.point_side:
                # Side tail: bubble sits left or right of the cat
                xo  = self.point_h if self.point_side == "left" else 0
                cy  = self.body_h / 2
                p.setPen(Qt.NoPen)
                p.setBrush(SHADOW)
                p.drawRoundedRect(QRectF(8+xo, 5, self.bw-12, self.body_h-4), 16, 16)
                p.setBrush(BUB_BG)
                p.setPen(QPen(BUB_BORDER, 1))
                p.drawRoundedRect(QRectF(6+xo, 2, self.bw-12, self.body_h-4), 16, 16)
                p.setPen(Qt.NoPen)
                p.setBrush(BUB_BG)
                if self.point_side == "right":   # tail tip points right toward cat
                    tri = [QPointF(self.bw+xo-8, cy-9),
                           QPointF(self.bw+xo-8, cy+9),
                           QPointF(self.bw+self.point_h, cy)]
                else:                            # tail tip points left toward cat
                    tri = [QPointF(self.point_h+8, cy-9),
                           QPointF(self.point_h+8, cy+9),
                           QPointF(0, cy)]
                p.drawPolygon(QPolygonF(tri))
                top = 0
            else:
                # Up/down tail: bubble sits above or below the cat
                xo  = 0
                top = 0 if self.point_up else self.point_h
                p.setPen(Qt.NoPen)
                p.setBrush(SHADOW)
                p.drawRoundedRect(QRectF(8, top+5, self.bw-12, self.body_h-4), 16, 16)
                p.setBrush(BUB_BG)
                p.setPen(QPen(BUB_BORDER, 1))
                p.drawRoundedRect(QRectF(6, top+2, self.bw-12, self.body_h-4), 16, 16)
                cx = self.bw / 2
                p.setPen(Qt.NoPen)
                p.setBrush(BUB_BG)
                if self.point_up:
                    tri = [QPointF(cx-9, top+self.body_h-3),
                           QPointF(cx+9, top+self.body_h-3),
                           QPointF(cx,   self.body_h+self.point_h)]
                else:
                    tri = [QPointF(cx-9, self.point_h+2),
                           QPointF(cx+9, self.point_h+2),
                           QPointF(cx,   0)]
                p.drawPolygon(QPolygonF(tri))

            self._x_off = xo
            if xo:
                p.translate(xo, 0)

            paint_fn = {
                "main":          self._paint_main,
                "goal":          self._paint_goal,
                "settings":      self._paint_settings,
                "history":       self._paint_history,
                "projects":      self._paint_projects,
                "confirm_clear": self._paint_confirm_clear,
            }
            if self.view in paint_fn:
                paint_fn[self.view](p, top)
        finally:
            p.end()

    def _btn(self, p, rect, text, cb, bg=None, fg=None, bold=False):
        if bg is None: bg = BUB_BTN
        if fg is None: fg = BUB_TEXT
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8, 8)
        p.setPen(fg)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold if bold else QFont.Normal))
        p.drawText(rect, Qt.AlignCenter, text)
        self._buttons.append((rect, cb, "btn"))

    def _link(self, p, x, y, text, cb, color=None, anchor=Qt.AlignLeft):
        if color is None: color = BUB_MUTED
        p.setPen(color)
        p.setFont(QFont(FONT_NAME, 9))
        if anchor == Qt.AlignHCenter:
            r = QRectF(0, y, self.bw, 18)
            p.drawText(r, Qt.AlignCenter, text)
        else:
            r = QRectF(x, y, 80, 18)
            p.drawText(r, anchor | Qt.AlignVCenter, text)
        self._buttons.append((r, cb, "link"))

    # ---------- MAIN ----------
    def _paint_main(self, p, top):
        cx = self.bw / 2
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 11))
        p.drawText(QRectF(0, top+16, self.bw, 18), Qt.AlignCenter,
                   datetime.now().strftime("%I:%M %p").lstrip("0"))
        self._draw_gear(p, self.bw - 26, top + 25, 8)
        self._buttons.append((QRectF(self.bw-40, top+14, 30, 30),
                               lambda: self._go("settings"), "link"))
        self._btn(p, QRectF(cx-70, top+40, 140, 34),
                  "Clock Out" if self.cat.working else "Clock In",
                  self._do_clock,
                  bg=RED if self.cat.working else GREEN,
                  fg=QColor("white"), bold=True)
        if not self.proj_combo.view().isVisible():
            self.proj_combo.blockSignals(True)
            self.proj_combo.clear()
            self.proj_combo.addItems(self.cat.projects)
            self.proj_combo.insertSeparator(len(self.cat.projects))
            self.proj_combo.addItem("Manage projects…")
            idx = self.cat.projects.index(self.cat.current_project) \
                if self.cat.current_project in self.cat.projects else 0
            self.proj_combo.setCurrentIndex(idx)
            self.proj_combo.blockSignals(False)
        self.proj_combo.setGeometry(int(cx-80) + self._x_off, int(top+82), 160, 28)
        self.proj_combo.show()
        now = time.time()
        if now - self._today_min_ts >= 1.0:
            self._today_min    = today_minutes()
            self._today_min_ts = now
        done = self._today_min / 60.0
        goal = get_goal_hours()
        frac = max(0.0, min(1.0, done / goal)) if goal > 0 else 0
        gy   = top + 118
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 9))
        gr = QRectF(0, gy, self.bw, 16)
        p.drawText(gr, Qt.AlignCenter, f"{done:.1f} / {goal:.1f} hrs today")
        self._buttons.append((gr, lambda: self._go("goal"), "link"))
        p.setPen(Qt.NoPen)
        p.setBrush(BAR_TRACK)
        p.drawRoundedRect(QRectF(24, gy+18, self.bw-48, 6), 3, 3)
        if frac > 0:
            p.setBrush(GREEN if frac >= 1 else ACCENT)
            p.drawRoundedRect(QRectF(24, gy+18, (self.bw-48)*frac, 6), 3, 3)

    def _draw_gear(self, p, cx, cy, r):
        p.setPen(Qt.NoPen)
        p.setBrush(BUB_MUTED)
        for i in range(8):
            a    = i * math.pi / 4
            x, y = cx + math.cos(a)*(r+2), cy + math.sin(a)*(r+2)
            p.drawRect(QRectF(x-1.6, y-1.6, 3.2, 3.2))
        p.drawEllipse(QRectF(cx-r, cy-r, 2*r, 2*r))
        p.setBrush(BUB_BG)
        p.drawEllipse(QRectF(cx-r/2.2, cy-r/2.2, r/1.1, r/1.1))

    # ---------- GOAL ----------
    def _paint_goal(self, p, top):
        cx = self.bw / 2
        self._link(p, 14, top+8, "Back", lambda: self._go("main"), color=ACCENT)
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top+8, self.bw, 20), Qt.AlignCenter, "Daily goal (hours)")
        self._btn(p, QRectF(cx-80, top+36, 30, 30), "−", lambda: self._bump_goal(-0.5), bold=True)
        self._btn(p, QRectF(cx+50, top+36, 30, 30), "+", lambda: self._bump_goal( 0.5), bold=True)
        self.goal_edit.setGeometry(int(cx-42) + self._x_off, int(top+36), 84, 30)
        if not self.goal_edit.hasFocus():
            self.goal_edit.setText(f"{get_goal_hours():g}")
        self.goal_edit.show()
        self._btn(p, QRectF(cx-50, top+84, 100, 30), "Done", lambda: self._go("main"),
                  bg=GREEN, fg=QColor("white"), bold=True)

    # ---------- SETTINGS ----------
    def _paint_settings(self, p, top):
        cx = self.bw / 2
        self._link(p, 14, top+8, "Back", lambda: self._go("main"), color=ACCENT)
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top+8, self.bw, 20), Qt.AlignCenter, "Settings")
        self._btn(p, QRectF(cx-90, top+34,  180, 30),
                  "History & Totals", lambda: self._go("history"))
        self._btn(p, QRectF(cx-90, top+74,  180, 30),
                  f"Kitty color:  {self.cat.cat_name}", self._cycle_color)
        self._btn(p, QRectF(cx-90, top+114, 180, 30),
                  f"Kitty size:  {self.cat.size_name}", self._cycle_size)
        self._btn(p, QRectF(cx-90, top+154, 180, 30),
                  f"Dark mode:  {'ON' if _IS_DARK else 'OFF'}",
                  self._toggle_dark,
                  bg=ACCENT if _IS_DARK else BUB_BTN,
                  fg=QColor("white") if _IS_DARK else BUB_TEXT)
        afk_on = get_setting("afk_auto_clock", "0") == "1"
        self._btn(p, QRectF(cx-90, top+194, 180, 30),
                  f"Clock out when idle:  {'ON' if afk_on else 'OFF'}",
                  self._toggle_afk,
                  bg=GREEN if afk_on else BUB_BTN,
                  fg=QColor("white") if afk_on else BUB_TEXT)
        m_val = max(0, int(get_setting("afk_minutes", "2")))
        s_val = max(0, min(59, int(get_setting("afk_secs", "0"))))
        self.afk_min_edit.setGeometry(int(cx-81) + self._x_off, int(top+232), 48, 26)
        if not self.afk_min_edit.hasFocus():
            self.afk_min_edit.setText(str(m_val))
        self.afk_min_edit.show()
        p.setPen(BUB_MUTED)
        p.setFont(QFont(FONT_NAME, 8))
        p.drawText(QRectF(cx-31, top+232, 30, 26), Qt.AlignCenter, "min")
        self.afk_sec_edit.setGeometry(int(cx+3) + self._x_off, int(top+232), 48, 26)
        if not self.afk_sec_edit.hasFocus():
            self.afk_sec_edit.setText(str(s_val))
        self.afk_sec_edit.show()
        p.drawText(QRectF(cx+53, top+232, 30, 26), Qt.AlignCenter, "sec")
        if sys.platform == "win32":
            on = startup_enabled()
            self._btn(p, QRectF(cx-90, top+268, 180, 30),
                      f"Run on system startup:  {'ON' if on else 'OFF'}",
                      self._toggle_startup,
                      bg=GREEN if on else BUB_BTN,
                      fg=QColor("white") if on else BUB_TEXT)

    # ---------- HISTORY ----------
    def _paint_history(self, p, top):
        self._link(p, 14, top+8, "Back", lambda: self._go("main"), color=ACCENT)
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top+8, self.bw, 20), Qt.AlignCenter, "History & Totals")
        p.setFont(QFont(FONT_NAME, 8, QFont.Bold))
        p.setPen(BUB_MUTED)
        if self._proj_totals:
            p.drawText(QRectF(16,            top+30, 70, 14), Qt.AlignLeft,  "Project")
            p.drawText(QRectF(self.bw - 156, top+30, 40, 14), Qt.AlignRight, "Total")
            p.drawText(QRectF(self.bw - 112, top+30, 40, 14), Qt.AlignRight, "Week")
            p.drawText(QRectF(self.bw - 68,  top+30, 40, 14), Qt.AlignRight, "Today")
        else:
            p.setFont(QFont(FONT_NAME, 9))
            p.drawText(QRectF(0, top+30, self.bw, 20), Qt.AlignCenter,
                       "No sessions recorded yet.")
        y = top + 46
        for proj, total, week, tod in self._proj_totals:
            name = proj if len(proj) <= 11 else proj[:10] + "…"
            p.setPen(BUB_TEXT)
            p.setFont(QFont(FONT_NAME, 9, QFont.Bold))
            p.drawText(QRectF(16, y, 90, 18), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setFont(QFont(FONT_NAME, 9))
            for val, rx, color in [(f"{total:.1f}", self.bw-156, ACCENT),
                                   (f"{week:.1f}",  self.bw-112, BUB_TEXT),
                                   (f"{tod:.1f}",   self.bw-68,  BUB_TEXT)]:
                p.setPen(color)
                p.drawText(QRectF(rx, y, 40, 18), Qt.AlignRight | Qt.AlignVCenter, val)
            y += 22
        y += 6
        p.setPen(BUB_MUTED)
        p.setFont(QFont(FONT_NAME, 8, QFont.Bold))
        p.drawText(QRectF(16, y, 100, 14), Qt.AlignLeft, "Sessions")
        y += 18
        self.hist_list.setGeometry(16 + self._x_off, int(y), self.bw-32,
                                   max(40, int(top + self.body_h - y - 50)))
        self.hist_list.show()
        hw = (self.bw - 40) // 2
        self._btn(p, QRectF(16, top+self.body_h-38, hw, 26),
                  "Export CSV", self._export_csv, bg=ACCENT, fg=QColor("white"))
        self._btn(p, QRectF(24+hw, top+self.body_h-38, hw, 26),
                  "Clear history", lambda: self._go("confirm_clear"),
                  bg=RED, fg=QColor("white"))

    # ---------- PROJECTS ----------
    def _paint_projects(self, p, top):
        self._link(p, 14, top+8, "Back", lambda: self._go("main"), color=ACCENT)
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top+8, self.bw, 20), Qt.AlignCenter, "Manage Projects")
        self.manage_proj_combo.setGeometry(20 + self._x_off, int(top+32), self.bw-40, 30)
        self.manage_proj_combo.show()
        fy = top + 74
        self.proj_edit.setGeometry(20 + self._x_off, int(fy), self.bw-90, 30)
        self.proj_edit.show()
        self._btn(p, QRectF(self.bw-62, fy, 42, 30), "Add",
                  self._add_project, bg=GREEN, fg=QColor("white"))
        self._btn(p, QRectF(20, fy+40, self.bw-40, 30), "Remove selected",
                  self._remove_selected, bg=RED, fg=QColor("white"))
        if len(self.cat.projects) <= 1:
            p.setPen(BUB_MUTED)
            p.setFont(QFont(FONT_NAME, 8))
            p.drawText(QRectF(0, fy+78, self.bw, 16), Qt.AlignCenter,
                       "At least one project is required.")

    # ---------- CONFIRM CLEAR ----------
    def _paint_confirm_clear(self, p, top):
        cx = self.bw / 2
        self._link(p, 14, top+8, "Back", lambda: self._go("history"), color=ACCENT)
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top+8, self.bw, 20), Qt.AlignCenter, "Are you sure about that?")
        p.setFont(QFont(FONT_NAME, 9))
        p.setPen(BUB_MUTED)
        p.drawText(QRectF(0, top+32, self.bw, 16), Qt.AlignCenter, "This can't be undone.")
        self._btn(p, QRectF(cx-90, top+58, 85, 30), "Yeah",
                  self._clear_history, bg=RED, fg=QColor("white"))
        self._btn(p, QRectF(cx+5,  top+58, 85, 30), "Nah",
                  lambda: self._go("history"))

    def _fill_proj_list(self):
        self.manage_proj_combo.blockSignals(True)
        self.manage_proj_combo.clear()
        self.manage_proj_combo.addItems(self.cat.projects)
        self.manage_proj_combo.blockSignals(False)

    def _go(self, view):
        self._hide_inputs()
        self.view = view
        if view == "projects":
            self._fill_proj_list()
        elif view == "history":
            self._proj_totals = project_totals()
            self._fill_hist_list()
        self._reposition()
        self.update()

    def _fill_hist_list(self):
        self.hist_list.clear()
        cur.execute("SELECT stamp, minutes, COALESCE(project,'General') "
                    "FROM sessions ORDER BY rowid DESC")
        for stamp, minutes, proj in cur.fetchall():
            try:
                dt   = datetime.strptime(stamp.strip(), "%Y-%m-%d  %H:%M")
                disp = dt.strftime("%m/%d %I:%M%p").lstrip("0")
            except ValueError:
                disp = stamp
            self.hist_list.addItem(f"{disp}  {minutes:.0f}m  {proj}")

    def _do_clock(self):
        self.cat.toggle_clock()
        self.update()

    def _bump_goal(self, delta):
        g = max(0.5, min(24.0, get_goal_hours() + delta))
        save_setting("daily_goal", g)
        self.goal_edit.setText(f"{g:g}")
        self.update()

    def _goal_typed(self):
        try:
            g = max(0.5, min(24.0, float(self.goal_edit.text())))
            save_setting("daily_goal", g)
            self.goal_edit.setText(f"{g:g}")
        except ValueError:
            self.goal_edit.setText(f"{get_goal_hours():g}")
        self.update()

    def _export_csv(self):
        default = os.path.join(os.path.expanduser("~"), "Desktop", "clocker_export.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", default, "CSV files (*.csv)")
        if not path:
            return
        export_csv(path)
        if self.cat.tray:
            self.cat.tray.showMessage(
                "Clocker 9000", f"Exported → {os.path.basename(path)}",
                QSystemTrayIcon.MessageIcon.Information, 3000)

    def _clear_history(self):
        cur.execute("DELETE FROM sessions")
        conn.commit()
        self._go("history")

    def _cycle_color(self):
        self.cat.cycle_cat()
        self.update()

    def _toggle_afk(self):
        on = get_setting("afk_auto_clock", "0") != "1"
        save_setting("afk_auto_clock", "1" if on else "0")
        if not on:
            self.cat._was_afk = False
        self.update()

    def _afk_min_typed(self):
        try:
            m = max(0, int(self.afk_min_edit.text()))
            save_setting("afk_minutes", str(m))
            self.afk_min_edit.setText(str(m))
        except ValueError:
            self.afk_min_edit.setText(str(max(0, int(get_setting("afk_minutes", "2")))))
        self.update()

    def _afk_sec_typed(self):
        try:
            s = max(0, min(59, int(self.afk_sec_edit.text())))
            save_setting("afk_secs", str(s))
            self.afk_sec_edit.setText(str(s))
        except ValueError:
            self.afk_sec_edit.setText(str(max(0, min(59, int(get_setting("afk_secs", "0"))))))
        self.update()

    def _toggle_startup(self):
        set_startup(not startup_enabled())
        self.update()

    def _toggle_dark(self):
        dark = not _IS_DARK
        save_setting("dark_mode", "1" if dark else "0")
        apply_palette(dark)
        self._update_widget_styles()
        self.update()
        self.cat.update()

    def _cycle_size(self):
        self.cat.cycle_size()
        self._reposition()
        self.update()

    def _add_project(self):
        name = self.proj_edit.text().strip()
        if name and name not in self.cat.projects:
            self.cat.projects.append(name)
            save_projects(self.cat.projects)
            self.proj_edit.clear()
            self._fill_proj_list()
            self.manage_proj_combo.setCurrentIndex(self.cat.projects.index(name))
            self._reposition()
            self.update()

    def _combo_picked(self, idx):
        if idx == len(self.cat.projects) + 1:  # "Manage projects…" (after separator)
            self.proj_combo.blockSignals(True)
            prev_idx = self.cat.projects.index(self.cat.current_project) \
                if self.cat.current_project in self.cat.projects else 0
            self.proj_combo.setCurrentIndex(prev_idx)
            self.proj_combo.blockSignals(False)
            self._go("projects")
            return
        if 0 <= idx < len(self.cat.projects):
            self.cat.switch_project(self.cat.projects[idx])
        self.update()

    def _remove_selected(self):
        name = self.manage_proj_combo.currentText()
        if name and len(self.cat.projects) > 1:
            self.cat.projects.remove(name)
            save_projects(self.cat.projects)
            if self.cat.current_project == name:
                self.cat.current_project = self.cat.projects[0]
                save_setting("current_project", self.cat.current_project)
            self._fill_proj_list()
            self._reposition()
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position() - QPointF(self._x_off, 0)
        for rect, cb, _ in self._buttons:
            if rect.contains(pos):
                cb()
                return

    def _on_app_state(self, state):
        if state != Qt.ApplicationState.ApplicationActive and self.isVisible():
            self.hide()

    def focusOutEvent(self, e):
        if any(w.hasFocus() for w in self._inputs):
            return
        if self.proj_combo.view().isVisible() or self.manage_proj_combo.view().isVisible():
            return
        self.hide()


def _create_shortcut(target, shortcut_path):
    if sys.platform != "win32":
        return
    ps = (
        f'$s=(New-Object -COM WScript.Shell).CreateShortcut("{shortcut_path}");'
        f'$s.TargetPath="{target}";'
        f'$s.WorkingDirectory="{os.path.dirname(target)}";'
        f'$s.Save()'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


class _SetupWizard(QDialog):
    def __init__(self, default_path, mode="install"):
        super().__init__()
        self.setWindowTitle("Clocker 9000  —  " + ("Setup" if mode == "install" else "Move"))
        self.setFixedSize(480, 300)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
        self.setStyleSheet("""
            QDialog    { background: #f8f8fc; }
            QLabel     { color: #2a2a32; }
            QLabel#ttl { font-size: 15pt; font-weight: bold; }
            QLabel#sub { font-size: 10pt; color: #5a5a6a; }
            QLineEdit  { border: 1px solid #d0d0d8; border-radius: 6px;
                         padding: 5px 8px; font-size: 10pt;
                         background: white; color: #2a2a32; }
            QPushButton { border: 1px solid #d0d0d8; border-radius: 6px;
                          padding: 6px 16px; font-size: 10pt;
                          background: #f0f0f4; color: #2a2a32; }
            QPushButton:hover   { background: #e4e4ec; }
            QPushButton#install { background: #4caf7d; color: white;
                                  border-color: #3d9e6e; font-weight: bold; }
            QPushButton#install:hover { background: #3d9e6e; }
            QCheckBox { font-size: 10pt; color: #2a2a32; spacing: 6px; }
        """)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(28, 22, 28, 22)

        ttl = QLabel("Welcome to Clocker 9000" if mode == "install" else "Move Clocker 9000")
        ttl.setObjectName("ttl")
        root.addWidget(ttl)

        sub = QLabel(
            "Choose where to install Clocker 9000.\n"
            "The app and its session data will be kept in this folder."
            if mode == "install" else
            "Choose a new folder for Clocker 9000.\n"
            "Your session history will be moved with it."
        )
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        root.addWidget(sub)

        root.addSpacing(4)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit(default_path)
        path_row.addWidget(self._path_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        root.addLayout(path_row)

        self._desktop_cb = QCheckBox("Create Desktop shortcut")
        self._desktop_cb.setChecked(True)
        root.addWidget(self._desktop_cb)

        self._startmenu_cb = QCheckBox("Add to Start Menu")
        self._startmenu_cb.setChecked(True)
        root.addWidget(self._startmenu_cb)

        root.addSpacing(4)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        install = QPushButton("Install" if mode == "install" else "Move")
        install.setObjectName("install")
        install.setDefault(True)
        install.clicked.connect(self.accept)
        btn_row.addWidget(install)
        root.addLayout(btn_row)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose install folder", self._path_edit.text())
        if folder:
            self._path_edit.setText(folder)

    def chosen_path(self):
        return self._path_edit.text().strip()

    def wants_desktop(self):
        return self._desktop_cb.isChecked()

    def wants_startmenu(self):
        return self._startmenu_cb.isChecked()


def _make_tray_pixmap(fur_color: str):
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(0, 0, 0, 90), 1.5))
        p.setBrush(QColor(fur_color))
        p.drawEllipse(QRectF(3, 8, 26, 21))
        p.drawPolygon(QPolygonF([QPointF(3,13),  QPointF(7,2),   QPointF(14,10)]))
        p.drawPolygon(QPolygonF([QPointF(29,13), QPointF(25,2),  QPointF(18,10)]))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(8,  13, 5, 5))
        p.drawEllipse(QRectF(19, 13, 5, 5))
        p.setBrush(QColor("#1e1e2e"))
        p.drawEllipse(QRectF(9.5,  14.5, 2, 2))
        p.drawEllipse(QRectF(20.5, 14.5, 2, 2))
    finally:
        p.end()
    return pm


# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    if sys.platform == "win32":
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Clocker9000_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # ── First-run installer (compiled exe only) ──────────────────────────────
    if getattr(sys, "frozen", False):
        _marker = os.path.join(SCRIPT_DIR, ".clocker_installed")
        if not os.path.exists(_marker):
            _default = os.path.join(
                os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Clocker9000"
            ) if sys.platform == "win32" else os.path.expanduser("~/Clocker9000")
            _wiz = _SetupWizard(_default)
            if _wiz.exec() != QDialog.Accepted:
                sys.exit(0)
            _folder = _wiz.chosen_path()
            os.makedirs(_folder, exist_ok=True)
            _dest = os.path.join(_folder, os.path.basename(sys.executable))
            _same = os.path.abspath(_dest) == os.path.abspath(sys.executable)
            if not _same:
                shutil.copy2(sys.executable, _dest)
            with open(os.path.join(_folder, ".clocker_installed"), "w"): pass
            if sys.platform == "win32":
                if _wiz.wants_desktop():
                    _desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                    _create_shortcut(_dest, os.path.join(_desktop, "Clocker 9000.lnk"))
                if _wiz.wants_startmenu():
                    _sm = os.path.join(os.environ.get("APPDATA", ""),
                                       "Microsoft", "Windows", "Start Menu", "Programs")
                    if os.path.isdir(_sm):
                        _create_shortcut(_dest, os.path.join(_sm, "Clocker 9000.lnk"))
            if not _same:
                subprocess.Popen([_dest])
                sys.exit(0)

    apply_palette(get_setting("dark_mode", "0") == "1")
    cat = CatOverlay()

    # ── System tray ──
    tray = QSystemTrayIcon(QIcon(_make_tray_pixmap(FUR_COLORS[cat.cat_name])), app)
    tray_menu = QMenu()
    tray_menu.addAction("Show Cat", lambda: (cat.show(), cat.raise_(), cat.activateWindow()))
    clock_action = tray_menu.addAction("Clock In")
    clock_action.triggered.connect(cat.toggle_clock)
    tray_menu.aboutToShow.connect(
        lambda: clock_action.setText("Clock Out" if cat.working else "Clock In"))
    tray_menu.addSeparator()

    def _quit():
        if cat.start_time is not None:
            log_session((time.time() - cat.start_time) / 60, cat.current_project)
        save_setting("session_start", "")
        app.quit()

    def _uninstall():
        reply = QMessageBox.question(
            None, "Uninstall Clocker 9000",
            "This will remove Clocker 9000 and all its shortcuts.\n\n"
            "Your session history will also be deleted. Export it first\n"
            "from History & Totals if you want to keep it.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        if cat.start_time is not None:
            log_session((time.time() - cat.start_time) / 60, cat.current_project)
        save_setting("session_start", "")
        if sys.platform == "win32":
            for lnk in [
                os.path.join(os.path.expanduser("~"), "Desktop", "Clocker 9000.lnk"),
                os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                             "Start Menu", "Programs", "Clocker 9000.lnk"),
            ]:
                try:
                    os.remove(lnk)
                except OSError:
                    pass
            set_startup(False)
            ps = (f'Start-Sleep -Seconds 2; '
                  f'Remove-Item -Path "{SCRIPT_DIR}" -Recurse -Force '
                  f'-ErrorAction SilentlyContinue')
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        app.quit()

    def _move_install():
        if not getattr(sys, "frozen", False):
            return
        _wiz = _SetupWizard(SCRIPT_DIR, mode="move")
        if _wiz.exec() != QDialog.Accepted:
            return
        _folder = _wiz.chosen_path()
        if os.path.abspath(_folder) == os.path.abspath(SCRIPT_DIR):
            return
        os.makedirs(_folder, exist_ok=True)
        _dest = os.path.join(_folder, os.path.basename(sys.executable))
        shutil.copy2(sys.executable, _dest)
        _db = os.path.join(SCRIPT_DIR, "clocker_history.db")
        if os.path.exists(_db):
            shutil.copy2(_db, os.path.join(_folder, "clocker_history.db"))
        with open(os.path.join(_folder, ".clocker_installed"), "w"): pass
        if sys.platform == "win32":
            if _wiz.wants_desktop():
                _create_shortcut(_dest, os.path.join(
                    os.path.expanduser("~"), "Desktop", "Clocker 9000.lnk"))
            if _wiz.wants_startmenu():
                _sm = os.path.join(os.environ.get("APPDATA", ""),
                                   "Microsoft", "Windows", "Start Menu", "Programs")
                if os.path.isdir(_sm):
                    _create_shortcut(_dest, os.path.join(_sm, "Clocker 9000.lnk"))
        if cat.start_time is not None:
            log_session((time.time() - cat.start_time) / 60, cat.current_project)
        save_setting("session_start", "")
        subprocess.Popen([_dest])
        app.quit()

    if getattr(sys, "frozen", False):
        tray_menu.addAction("Move install location…", _move_install)
        tray_menu.addAction("Uninstall…", _uninstall)
        tray_menu.addSeparator()
    tray_menu.addAction("Quit", _quit)
    tray.setContextMenu(tray_menu)
    tray.activated.connect(
        lambda reason: (cat.show(), cat.raise_(), cat.activateWindow())
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
    tray.show()
    cat.tray = tray
    if getattr(sys, "frozen", False):
        cat.uninstall_cb = _uninstall

    cat.show()

    if get_setting("first_launch", "1") == "1":
        save_setting("first_launch", "0")
        QTimer.singleShot(800, cat.toggle_bubble)
        tray.showMessage("Clocker 9000",
                         "Click the cat anytime to clock in or out!",
                         QSystemTrayIcon.MessageIcon.Information, 5000)

    sys.exit(app.exec())
