import sys
import os
import math
import time
import sqlite3
from datetime import datetime, timedelta
 
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QListWidget, QComboBox
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import (QPainter, QColor, QBrush, QPen, QPainterPath,
                           QPolygonF, QFont)
 
# ============================================================
#  CONFIG / PALETTES
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
 
FUR_COLORS = {
    "white": "#f2f2f6", "gray": "#9aa0a6", "black": "#3a3a44",
    "brown": "#9c6b3f", "orange": "#e8943a",
}
CAT_NAMES = list(FUR_COLORS)
PINK = QColor("#f6c4cb")
 
SIZE_LEVELS = {"Small": 0.75, "Medium": 1.0, "Large": 1.35}
SIZE_ORDER = ["Small", "Medium", "Large"]
 
# bubble colors
BUB_BG     = QColor("#ffffff")
BUB_TEXT   = QColor("#2a2a32")
BUB_MUTED  = QColor("#8a8a96")
BUB_BTN    = QColor("#f0f0f4")
BUB_BORDER = QColor("#d8d8e0")
ACCENT     = QColor("#7aa2f7")
GREEN      = QColor("#4caf7d")
RED        = QColor("#e05c6e")
SHADOW     = QColor(0, 0, 0, 40)
 
FONT_NAME = "Segoe UI"
 
 
def shade(hexc, f):
    r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
    if f <= 1:
        r, g, b = int(r * f), int(g * f), int(b * f)
    else:
        t = f - 1
        r, g, b = int(r + (255 - r) * t), int(g + (255 - g) * t), int(b + (255 - b) * t)
    cl = lambda v: max(0, min(255, v))
    return QColor(cl(r), cl(g), cl(b))
 
 
def palette_for(hexc):
    r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
    bright = (r + g + b) / 3
    if bright < 90:
        outline, eye = shade(hexc, 1.8), QColor("#f2c84a")
    else:
        outline, eye = shade(hexc, 0.42), QColor("#2a2a30")
    return QColor(hexc), outline, eye
 
 
# ============================================================
#  DATABASE  (same schema/logic as the Tkinter version)
# ============================================================
conn = sqlite3.connect(os.path.join(SCRIPT_DIR, "clocker_history.db"))
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS sessions (stamp TEXT, minutes REAL)")
cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
conn.commit()
_cols = [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]
if "project" not in _cols:
    cur.execute("ALTER TABLE sessions ADD COLUMN project TEXT DEFAULT 'General'")
    conn.commit()
 
 
def get_setting(key, default):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default
 
 
def save_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
    conn.commit()
 
 
def get_projects():
    raw = get_setting("projects", "General")
    items = [p for p in raw.split("||") if p.strip()]
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
    cur.execute("SELECT COALESCE(SUM(minutes),0) FROM sessions WHERE stamp LIKE ?",
                (today + "%",))
    return cur.fetchone()[0] or 0.0
 
 
def log_session(minutes, project):
    if minutes < 0.1:  # ignore sessions under ~6 seconds
        return
    stamp = datetime.now().strftime("%Y-%m-%d  %H:%M")
    cur.execute("INSERT INTO sessions (stamp, minutes, project) VALUES (?,?,?)",
                (stamp, minutes, project))
    conn.commit()
 
 
def project_totals():
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    cur.execute("""
        SELECT COALESCE(project,'General') AS p,
               SUM(minutes) AS total,
               SUM(CASE WHEN substr(stamp,1,10) >= ? THEN minutes ELSE 0 END) AS week,
               SUM(CASE WHEN substr(stamp,1,10) = ? THEN minutes ELSE 0 END) AS today
        FROM sessions GROUP BY p ORDER BY total DESC
    """, (monday, today_str))
    return [(p, (t or 0) / 60, (w or 0) / 60, (d or 0) / 60)
            for p, t, w, d in cur.fetchall()]
 
 
# ============================================================
#  THE CAT OVERLAY
# ============================================================
class CatOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
 
        # state
        self.start_time = None            # None = clocked out
        self.frame = 0
        self._drag = None
        self._moved = False
        self._press = None
 
        # settings
        self.cat_name = get_setting("cat_name", "gray")
        if self.cat_name not in CAT_NAMES:
            self.cat_name = "gray"
        self.size_name = get_setting("cat_size", "Medium")
        if self.size_name not in SIZE_LEVELS:
            self.size_name = "Medium"
 
        self.projects = get_projects()
        cp = get_setting("current_project", self.projects[0])
        self.current_project = cp if cp in self.projects else self.projects[0]
 
        self._apply_size()
 
        # restore position
        px = int(get_setting("pos_x", -1))
        py = int(get_setting("pos_y", -1))
        if px < 0 or py < 0:
            screen = QApplication.primaryScreen().geometry()
            px = screen.width() - self.width() - 30
            py = screen.height() - self.height() - 60
        self.move(px, py)
 
        self.bubble = Bubble(self)
 
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
 
    # ---------- sizing ----------
    def _apply_size(self):
        scale = SIZE_LEVELS[self.size_name]
        self.cat_px = int(150 * scale)
        self.resize(self.cat_px, self.cat_px)
        self.scale = scale
 
    # ---------- clock ----------
    @property
    def working(self):
        return self.start_time is not None
 
    def toggle_clock(self):
        if self.start_time is None:
            self.start_time = time.time()
        else:
            minutes = (time.time() - self.start_time) / 60
            log_session(minutes, self.current_project)
            self.start_time = None
        self.update()
 
    def switch_project(self, name):
        if self.start_time is not None and name != self.current_project:
            minutes = (time.time() - self.start_time) / 60
            log_session(minutes, self.current_project)
            self.start_time = time.time()
        self.current_project = name
        save_setting("current_project", name)
 
    # ---------- animation ----------
    def _tick(self):
        self.frame += 1
        self.update()
 
    # ---------- painting ----------
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
            sw = math.sin(self.frame * 0.15) * 10
            tail = QPainterPath()
            tail.moveTo(96, 118)
            tail.cubicTo(120, 116, 130 + sw, 96, 124 + sw, 78)
            p.setPen(QPen(fur, 9, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(tail)
            p.setPen(pen)
            body = QPainterPath()
            body.addEllipse(40, 70, 70, 72)
            p.setBrush(fur)
            p.drawPath(body)
            p.drawEllipse(44, 34, 50, 50)
            for tri in [[(50, 44), (54, 14), (70, 40)], [(68, 40), (84, 14), (88, 44)]]:
                p.setBrush(fur)
                p.drawPolygon(QPolygonF([QPointF(*pt) for pt in tri]))
            p.setPen(Qt.NoPen)
            p.setBrush(PINK)
            for tri in [[(56, 40), (58, 24), (66, 39)], [(72, 39), (80, 24), (82, 40)]]:
                p.drawPolygon(QPolygonF([QPointF(*pt) for pt in tri]))
            p.setPen(pen)
            p.setBrush(fur)
            p.drawEllipse(58, 132, 13, 13)
            p.drawEllipse(70, 132, 13, 13)
            blink = (self.frame % 45) < 3
            p.setBrush(eye)
            p.setPen(Qt.NoPen)
            if not blink:
                p.drawEllipse(57, 54, 7, 9)
                p.drawEllipse(74, 54, 7, 9)
            else:
                p.setPen(QPen(eye, 2))
                p.drawLine(57, 59, 64, 59)
                p.drawLine(74, 59, 81, 59)
            p.setPen(Qt.NoPen)
            p.setBrush(eye)
            p.drawPolygon(QPolygonF([QPointF(66, 66), QPointF(72, 66), QPointF(69, 70)]))
            p.setPen(QPen(outline, 2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            m = QPainterPath(); m.moveTo(69, 71); m.quadTo(65, 75, 61, 72); p.drawPath(m)
            m2 = QPainterPath(); m2.moveTo(69, 71); m2.quadTo(73, 75, 77, 72); p.drawPath(m2)
        else:
            p.setBrush(fur)
            p.drawEllipse(38, 82, 68, 46)
            p.drawEllipse(46, 76, 40, 40)
            for tri in [[(52, 84), (54, 62), (68, 82)], [(66, 82), (80, 62), (82, 84)]]:
                p.drawPolygon(QPolygonF([QPointF(*pt) for pt in tri]))
            p.setPen(Qt.NoPen)
            p.setBrush(PINK)
            for tri in [[(56, 81), (58, 69), (65, 80)], [(69, 80), (76, 69), (78, 81)]]:
                p.drawPolygon(QPolygonF([QPointF(*pt) for pt in tri]))
            p.setPen(QPen(eye, 2, Qt.SolidLine, Qt.RoundCap))
            e1 = QPainterPath(); e1.moveTo(56, 94); e1.quadTo(60, 97, 64, 94); p.drawPath(e1)
            e2 = QPainterPath(); e2.moveTo(68, 94); e2.quadTo(72, 97, 76, 94); p.drawPath(e2)
            p.setPen(Qt.NoPen)
            p.setBrush(eye)
            p.drawPolygon(QPolygonF([QPointF(63, 100), QPointF(69, 100), QPointF(66, 104)]))
            p.setPen(QPen(outline, 2, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            mm = QPainterPath(); mm.moveTo(66, 105); mm.quadTo(62, 108, 58, 106); p.drawPath(mm)
            mm2 = QPainterPath(); mm2.moveTo(66, 105); mm2.quadTo(70, 108, 74, 106); p.drawPath(mm2)
            bob = int(math.sin(self.frame * 0.1) * 2)
            p.setPen(QColor("#cfd3ff"))
            p.setFont(QFont(FONT_NAME, 11, QFont.Bold))
            p.drawText(104, 60 + bob, "z Z")
 
    # ---------- interaction ----------
    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.pos()
        self._press = e.globalPosition().toPoint()
        self._moved = False
 
    def mouseMoveEvent(self, e):
        if self._drag is not None:
            if (e.globalPosition().toPoint() - self._press).manhattanLength() > 4:
                self._moved = True
                if self.bubble.isVisible():
                    self.bubble.hide()
            self.move(e.globalPosition().toPoint() - self._drag)
 
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
 
    # cycle helpers used by the bubble
    def cycle_cat(self):
        i = (CAT_NAMES.index(self.cat_name) + 1) % len(CAT_NAMES)
        self.cat_name = CAT_NAMES[i]
        save_setting("cat_name", self.cat_name)
        self.update()
 
    def cycle_size(self):
        i = (SIZE_ORDER.index(self.size_name) + 1) % len(SIZE_ORDER)
        self.size_name = SIZE_ORDER[i]
        save_setting("cat_size", self.size_name)
        self._apply_size()
        self.update()
 
 
# ============================================================
#  THE SPEECH BUBBLE  (frameless translucent, pointer at bottom)
# ============================================================
class Bubble(QWidget):
    def __init__(self, cat):
        super().__init__()
        self.cat = cat
        self.setWindowFlags(Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.view = "main"
        self._buttons = []
        self.bw = 230
        self.point_h = 12
        self.point_up = True
        self.body_h = 200
 
        # real input widgets (hidden until their view needs them)
        field_css = (
            "QLineEdit{background:#f0f0f4;border:1px solid #d8d8e0;"
            "border-radius:8px;padding:4px 8px;color:#2a2a32;"
            "selection-background-color:#7aa2f7;}"
        )
        self.goal_edit = QLineEdit(self)
        self.goal_edit.setStyleSheet(field_css)
        self.goal_edit.setAlignment(Qt.AlignCenter)
        self.goal_edit.setFont(QFont(FONT_NAME, 14, QFont.Bold))
        self.goal_edit.editingFinished.connect(self._goal_typed)
        self.goal_edit.hide()
 
        self.proj_edit = QLineEdit(self)
        self.proj_edit.setStyleSheet(field_css)
        self.proj_edit.setPlaceholderText("New project\u2026")
        self.proj_edit.setFont(QFont(FONT_NAME, 10))
        self.proj_edit.returnPressed.connect(self._add_project)
        self.proj_edit.hide()
 
        self.proj_list = QListWidget(self)
        self.proj_list.setStyleSheet(
            "QListWidget{background:#f0f0f4;border:none;border-radius:8px;"
            "color:#2a2a32;padding:2px;}"
            "QListWidget::item{padding:4px 8px;border-radius:6px;}"
            "QListWidget::item:selected{background:#7aa2f7;color:white;}"
        )
        self.proj_list.setFont(QFont(FONT_NAME, 10))
        self.proj_list.hide()
 
        # project dropdown for the main view
        self.proj_combo = QComboBox(self)
        self.proj_combo.setStyleSheet(
            "QComboBox{background:#f0f0f4;border:1px solid #d8d8e0;"
            "border-radius:8px;padding:4px 10px;color:#2a2a32;}"
            "QComboBox::drop-down{border:none;width:22px;}"
            "QComboBox QAbstractItemView{background:#ffffff;color:#2a2a32;"
            "selection-background-color:#7aa2f7;selection-color:white;"
            "border:1px solid #d8d8e0;outline:none;}"
        )
        self.proj_combo.setFont(QFont(FONT_NAME, 10))
        self.proj_combo.activated.connect(self._combo_picked)
        self.proj_combo.hide()
 
        self.hist_list = QListWidget(self)
        self.hist_list.setStyleSheet(
            "QListWidget{background:#f0f0f4;border:none;border-radius:8px;"
            "color:#2a2a32;padding:2px;font-family:Consolas;}"
            "QListWidget::item{padding:2px 6px;}"
        )
        self.hist_list.setFont(QFont("Consolas", 8))
        self.hist_list.hide()
 
    # ---------- show positioned above the cat ----------
    def show_near_cat(self):
        self.view = "main"
        self._reposition()
        self.show()
        self.raise_()
        self.activateWindow()
        self.update()
 
    def _hide_inputs(self):
        self.goal_edit.hide()
        self.proj_edit.hide()
        self.proj_list.hide()
        self.proj_combo.hide()
        self.hist_list.hide()
 
    # ---------- compute height for current view ----------
    def _layout(self):
        if self.view == "main":
            self.body_h = 226
        elif self.view == "goal":
            self.body_h = 160
        elif self.view == "settings":
            self.body_h = 160
        elif self.view == "history":
            trows = max(len(project_totals()), 1)
            self.body_h = 70 + trows * 22 + 30 + 150     # totals + sessions list
        elif self.view == "projects":
            rows = max(len(self.cat.projects), 1)
            list_h = min(rows, 6) * 28 + 6
            self.body_h = 54 + list_h + 12 + 30 + 40 + 30 + 16
        else:
            self.body_h = 200
        self.resize(self.bw, self.body_h + self.point_h)
 
    def _reposition(self):
        self._layout()
        cg = self.cat.frameGeometry()
        x = cg.center().x() - self.width() // 2
        y = cg.top() - self.height() - 4
        screen = QApplication.primaryScreen().geometry()
        x = max(4, min(x, screen.width() - self.width() - 4))
        if y < 4:
            y = cg.bottom() + 4
            self.point_up = False
        else:
            self.point_up = True
        self.move(x, y)
 
    # ---------- painting ----------
    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            self._buttons = []
            body_top = 0 if self.point_up else self.point_h
            p.setPen(Qt.NoPen)
            p.setBrush(SHADOW)
            p.drawRoundedRect(QRectF(8, body_top + 5, self.bw - 12, self.body_h - 4), 16, 16)
            p.setBrush(BUB_BG)
            p.setPen(QPen(BUB_BORDER, 1))
            p.drawRoundedRect(QRectF(6, body_top + 2, self.bw - 12, self.body_h - 4), 16, 16)
            cx = self.bw / 2
            p.setPen(Qt.NoPen)
            p.setBrush(BUB_BG)
            if self.point_up:
                tri = [QPointF(cx - 9, body_top + self.body_h - 3),
                       QPointF(cx + 9, body_top + self.body_h - 3),
                       QPointF(cx, self.body_h + self.point_h)]
            else:
                tri = [QPointF(cx - 9, self.point_h + 2),
                       QPointF(cx + 9, self.point_h + 2),
                       QPointF(cx, 0)]
            p.drawPolygon(QPolygonF(tri))
 
            if self.view == "main":
                self._paint_main(p, body_top)
            elif self.view == "goal":
                self._paint_goal(p, body_top)
            elif self.view == "settings":
                self._paint_settings(p, body_top)
            elif self.view == "history":
                self._paint_history(p, body_top)
            elif self.view == "projects":
                self._paint_projects(p, body_top)
        finally:
            p.end()
 
    def _btn(self, p, rect, text, cb, bg=BUB_BTN, fg=BUB_TEXT, bold=False):
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect, 8, 8)
        p.setPen(fg)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold if bold else QFont.Normal))
        p.drawText(rect, Qt.AlignCenter, text)
        self._buttons.append((rect, cb, "btn"))
 
    def _link(self, p, x, y, text, cb, color=BUB_MUTED, anchor=Qt.AlignLeft):
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
        p.setFont(QFont(FONT_NAME, 17, QFont.Bold))
        tstr = datetime.now().strftime("%I:%M %p").lstrip("0")
        p.drawText(QRectF(0, top + 12, self.bw, 30), Qt.AlignCenter, tstr)
        # draw a small gear shape (emoji doesn't render reliably)
        self._draw_gear(p, self.bw - 26, top + 24, 8)
        self._buttons.append((QRectF(self.bw - 40, top + 10, 30, 30),
                              lambda: self._go("settings"), "link"))
        working = self.cat.working
        r = QRectF(cx - 70, top + 46, 140, 34)
        self._btn(p, r, "Clock Out" if working else "Clock In",
                  self._do_clock, bg=(RED if working else GREEN),
                  fg=QColor("white"), bold=True)
        # project dropdown (real combobox) + Manage link below it
        if not self.proj_combo.view().isVisible():
            self.proj_combo.blockSignals(True)
            self.proj_combo.clear()
            self.proj_combo.addItems(self.cat.projects)
            idx = self.cat.projects.index(self.cat.current_project) \
                if self.cat.current_project in self.cat.projects else 0
            self.proj_combo.setCurrentIndex(idx)
            self.proj_combo.blockSignals(False)
        self.proj_combo.setGeometry(int(cx - 80), int(top + 86), 160, 28)
        self.proj_combo.show()
        self._link(p, 0, top + 116, "Manage projects", lambda: self._go("projects"),
                   color=ACCENT, anchor=Qt.AlignHCenter)
        done = today_minutes() / 60.0
        goal = get_goal_hours()
        frac = max(0.0, min(1.0, done / goal)) if goal > 0 else 0
        gy = top + 140
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 9))
        gr = QRectF(0, gy, self.bw, 16)
        p.drawText(gr, Qt.AlignCenter, f"{done:.1f} / {goal:.1f} hrs today")
        self._buttons.append((gr, lambda: self._go("goal"), "link"))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e6e6ec"))
        p.drawRoundedRect(QRectF(24, gy + 20, self.bw - 48, 6), 3, 3)
        if frac > 0:
            p.setBrush(GREEN if frac >= 1 else ACCENT)
            p.drawRoundedRect(QRectF(24, gy + 20, (self.bw - 48) * frac, 6), 3, 3)
        tr = QRectF(cx - 80, top + 186, 160, 28)
        self._btn(p, tr, "History & Totals", lambda: self._go("history"))
 
    def _draw_gear(self, p, cx, cy, r):
        # simple gear: a ring with little teeth + center hole
        p.setPen(Qt.NoPen)
        p.setBrush(BUB_MUTED)
        for i in range(8):
            a = i * math.pi / 4
            x = cx + math.cos(a) * (r + 2)
            y = cy + math.sin(a) * (r + 2)
            p.drawRect(QRectF(x - 1.6, y - 1.6, 3.2, 3.2))
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        p.setBrush(BUB_BG)
        p.drawEllipse(QRectF(cx - r / 2.2, cy - r / 2.2, r / 1.1, r / 1.1))
 
    # ---------- GOAL (typed input + steppers, capped 24) ----------
    def _paint_goal(self, p, top):
        cx = self.bw / 2
        self._link(p, 14, top + 12, "\u2039 Back", lambda: self._go("main"))
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top + 34, self.bw, 20), Qt.AlignCenter, "Daily goal (hours)")
        # typed field in the middle, with +/- on the sides
        minus = QRectF(cx - 80, top + 62, 30, 30)
        plus = QRectF(cx + 50, top + 62, 30, 30)
        self._btn(p, minus, "\u2212", lambda: self._bump_goal(-0.5), bold=True)
        self._btn(p, plus, "+", lambda: self._bump_goal(0.5), bold=True)
        # place the QLineEdit between them
        self.goal_edit.setGeometry(int(cx - 42), int(top + 62), 84, 30)
        if not self.goal_edit.hasFocus():
            self.goal_edit.setText(f"{get_goal_hours():g}")
        self.goal_edit.show()
        done = QRectF(cx - 50, top + 110, 100, 30)
        self._btn(p, done, "Done", lambda: self._go("main"),
                  bg=GREEN, fg=QColor("white"), bold=True)
 
    # ---------- SETTINGS (cat color + size cycling) ----------
    def _paint_settings(self, p, top):
        cx = self.bw / 2
        self._link(p, 14, top + 12, "\u2039 Back", lambda: self._go("main"))
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top + 30, self.bw, 20), Qt.AlignCenter, "Settings")
        c = QRectF(cx - 90, top + 60, 180, 30)
        self._btn(p, c, f"Color:  {self.cat.cat_name}", self._cycle_color)
        s = QRectF(cx - 90, top + 100, 180, 30)
        self._btn(p, s, f"Size:  {self.cat.size_name}", self._cycle_size)
 
    # ---------- HISTORY (totals + session log) ----------
    def _paint_history(self, p, top):
        self._link(p, 14, top + 12, "\u2039 Back", lambda: self._go("main"))
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top + 30, self.bw, 20), Qt.AlignCenter, "History & Totals")
        p.setFont(QFont(FONT_NAME, 8, QFont.Bold))
        p.setPen(BUB_MUTED)
        p.drawText(QRectF(16, top + 52, 70, 14), Qt.AlignLeft, "Project")
        p.drawText(QRectF(self.bw - 156, top + 52, 40, 14), Qt.AlignRight, "Total")
        p.drawText(QRectF(self.bw - 112, top + 52, 40, 14), Qt.AlignRight, "Week")
        p.drawText(QRectF(self.bw - 68, top + 52, 40, 14), Qt.AlignRight, "Today")
        rows = project_totals()
        y = top + 68
        for proj, total, week, tod in rows:
            name = proj if len(proj) <= 11 else proj[:10] + "\u2026"
            p.setPen(BUB_TEXT)
            p.setFont(QFont(FONT_NAME, 9, QFont.Bold))
            p.drawText(QRectF(16, y, 90, 18), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setFont(QFont(FONT_NAME, 9))
            p.setPen(ACCENT)
            p.drawText(QRectF(self.bw - 156, y, 40, 18), Qt.AlignRight | Qt.AlignVCenter, f"{total:.1f}")
            p.setPen(BUB_TEXT)
            p.drawText(QRectF(self.bw - 112, y, 40, 18), Qt.AlignRight | Qt.AlignVCenter, f"{week:.1f}")
            p.drawText(QRectF(self.bw - 68, y, 40, 18), Qt.AlignRight | Qt.AlignVCenter, f"{tod:.1f}")
            y += 22
        # sessions list
        y += 6
        p.setPen(BUB_MUTED)
        p.setFont(QFont(FONT_NAME, 8, QFont.Bold))
        p.drawText(QRectF(16, y, 100, 14), Qt.AlignLeft, "Sessions")
        y += 18
        self.hist_list.setGeometry(16, int(y), self.bw - 32,
                                   max(40, int(top + self.body_h - y - 12)))
        self.hist_list.show()
 
    # ---------- PROJECTS (manage: add / remove only) ----------
    def _paint_projects(self, p, top):
        self._link(p, 14, top + 12, "\u2039 Back", lambda: self._go("main"))
        p.setPen(BUB_TEXT)
        p.setFont(QFont(FONT_NAME, 10, QFont.Bold))
        p.drawText(QRectF(0, top + 30, self.bw, 20), Qt.AlignCenter, "Manage Projects")
        rows = max(len(self.cat.projects), 1)
        list_h = min(rows, 6) * 28 + 6
        self.proj_list.setGeometry(20, int(top + 54), self.bw - 40, list_h)
        self.proj_list.show()
        fy = top + 54 + list_h + 12
        self.proj_edit.setGeometry(20, int(fy), self.bw - 90, 30)
        self.proj_edit.show()
        addr = QRectF(self.bw - 62, fy, 42, 30)
        self._btn(p, addr, "Add", self._add_project, bg=GREEN, fg=QColor("white"))
        rmr = QRectF(20, fy + 40, self.bw - 40, 30)
        self._btn(p, rmr, "Remove selected", self._remove_selected,
                  bg=RED, fg=QColor("white"))
 
    def _fill_proj_list(self):
        self.proj_list.clear()
        for pname in self.cat.projects:
            self.proj_list.addItem(pname)
 
    # ---------- actions ----------
    def _go(self, view):
        self._hide_inputs()
        self.view = view
        if view == "projects":
            self._fill_proj_list()
        elif view == "history":
            self._fill_hist_list()
        self._reposition()
        self.update()
 
    def _fill_hist_list(self):
        self.hist_list.clear()
        cur.execute("SELECT stamp, minutes, COALESCE(project,'General') "
                    "FROM sessions ORDER BY rowid DESC")
        for stamp, minutes, proj in cur.fetchall():
            # convert 24h stamp to AM/PM for display
            try:
                dt = datetime.strptime(stamp.strip(), "%Y-%m-%d  %H:%M")
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
            g = float(self.goal_edit.text())
            g = max(0.5, min(24.0, g))
            save_setting("daily_goal", g)
            self.goal_edit.setText(f"{g:g}")
        except ValueError:
            self.goal_edit.setText(f"{get_goal_hours():g}")
        self.update()
 
    def _cycle_color(self):
        self.cat.cycle_cat()
        self.update()
 
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
            self._reposition()
            self.update()
 
    def _combo_picked(self, idx):
        if 0 <= idx < len(self.cat.projects):
            self.cat.switch_project(self.cat.projects[idx])
        self.update()
 
    def _remove_selected(self):
        it = self.proj_list.currentItem()
        if it and len(self.cat.projects) > 1:
            name = it.text()
            self.cat.projects.remove(name)
            save_projects(self.cat.projects)
            if self.cat.current_project == name:
                self.cat.current_project = self.cat.projects[0]
                save_setting("current_project", self.cat.current_project)
            self._fill_proj_list()
            self._reposition()
            self.update()
 
    # ---------- click routing ----------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        for rect, cb, kind in self._buttons:
            if rect.contains(pos):
                cb()
                return
 
    def focusOutEvent(self, e):
        # don't close if focus went to one of our own widgets
        for w in (self.goal_edit, self.proj_edit, self.proj_list,
                  self.proj_combo, self.hist_list):
            if w.hasFocus():
                return
        # don't close if the combo dropdown popup is open
        if self.proj_combo.view().isVisible():
            return
        self.hide()
 
 
# ============================================================
#  MAIN
# ============================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    cat = CatOverlay()
    cat.show()
    sys.exit(app.exec())