"""Janela do EasyJoycon (GTK 3), no estilo da tela de controles do Switch.

O servidor (asyncio) roda numa thread separada. A janela só lê
server.state (uma "foto" trocada inteira 60×/s) e a fila
server.notices; comandos vão pro servidor por server.call(...).
"""
import logging
import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

try:  # ícone perto do relógio: XApp no Cinnamon, Ayatana nos outros
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ImportError, ValueError):
    XApp = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3
except (ImportError, ValueError):
    AyatanaAppIndicator3 = None

from . import config, dolphin, sistema  # noqa: E402
from .motion import AIM_LIMIT  # noqa: E402
from .players import MODE_NAMES  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "io.github.easyjoycon"
AUTOSTART = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
             / "autostart" / "easyjoycon.desktop")
# O que abre o app: o próprio AppImage (o AppRun põe o caminho dele aqui) ou o
# easyjoycon.sh de quem baixou o código.
LAUNCHER = Path(os.environ.get("EASYJOYCON_LAUNCHER")
                or Path(__file__).resolve().parent.parent / "easyjoycon.sh")
ICON_FILE = Path(__file__).resolve().parent.parent / "assets" / "easyjoycon.svg"
# Ícone nos atalhos (.desktop): o AppImage instala o dele com esse nome; quem
# baixou o código usa o arquivo direto.
DESKTOP_ICON = os.environ.get("EASYJOYCON_ICON") or str(ICON_FILE)
NOTIFY_KINDS = {"conectou", "desconectou", "jogador", "aviso"}
BANNER_SECONDS = 4.0

# Cores (tema escuro do Switch + Joy-Cons neon)
BG = (0.17, 0.17, 0.18)
CARD = (0.23, 0.23, 0.25)
CARD_EMPTY = (0.20, 0.20, 0.21)
TEXT = (0.95, 0.95, 0.95)
TEXT_DIM = (0.60, 0.60, 0.63)
ACCENT = (0.0, 0.76, 0.89)
GREEN = (0.30, 0.85, 0.40)
YELLOW = (1.0, 0.78, 0.2)
RED = (1.0, 0.33, 0.30)
JOYCON_L = (0.0, 0.73, 0.89)
JOYCON_R = (1.0, 0.27, 0.22)
PRO_BODY = (0.15, 0.15, 0.16)
BUTTON = (0.12, 0.12, 0.13)
PRESSED = (1.0, 1.0, 1.0)

CSS = b"""
window, .ej-bg { background-color: #2b2b2e; color: #f2f2f2; }
.ej-title { font-size: 20px; font-weight: bold; }
.ej-pill { background-color: #3a3a3f; border-radius: 12px; padding: 4px 12px; }
.ej-banner { background-color: #00c2e3; color: #10181a; border-radius: 10px;
             padding: 10px 16px; font-weight: bold; }
.ej-banner.ej-bad { background-color: #ff554d; color: #ffffff; }
.ej-warn { background-color: #4a3d1a; border-radius: 10px; padding: 8px 12px; }
.ej-bar button { padding: 6px 14px; }
.ej-dim { color: #9a9aa0; }
"""


def _rgb(cr, c, a=1.0):
    cr.set_source_rgba(c[0], c[1], c[2], a)


def _rounded(cr, x, y, w, h, r):
    r = [min(v, w / 2, h / 2) for v in (r if isinstance(r, tuple) else (r,) * 4)]
    tl, tr, br, bl = r
    cr.new_sub_path()
    cr.arc(x + w - tr, y + tr, tr, -math.pi / 2, 0)
    cr.arc(x + w - br, y + h - br, br, 0, math.pi / 2)
    cr.arc(x + bl, y + h - bl, bl, math.pi / 2, math.pi)
    cr.arc(x + tl, y + tl, tl, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _circle(cr, x, y, r, color, alpha=1.0):
    cr.arc(x, y, r, 0, 2 * math.pi)
    _rgb(cr, color, alpha)
    cr.fill()


def _btn(cr, x, y, r, on):
    _circle(cr, x, y, r, PRESSED if on else BUTTON)


def _stick(cr, x, y, r, pos, on):
    _circle(cr, x, y, r, BUTTON)
    sx, sy = pos
    _circle(cr, x + sx * r * 0.45, y - sy * r * 0.45, r * 0.62,
            PRESSED if on else (0.30, 0.30, 0.32))


def _text(cr, s, x, y, size=13, color=TEXT, bold=False, center=True):
    cr.select_font_face("Sans", 0, 1 if bold else 0)
    cr.set_font_size(size)
    ext = cr.text_extents(s)
    _rgb(cr, color)
    cr.move_to(x - ext.width / 2 - ext.x_bearing if center else x, y)
    cr.show_text(s)


def draw_joycon(cr, x, y, w, h, c):
    """Joy-Con em pé. c = estado do controle (server._ctrl_state)."""
    left = c["kind"] == "L"
    b = c["buttons"]
    big, small = w * 0.5, w * 0.12
    # gatilhos (L/ZL ou R/ZR) aparecem acesos quando apertados
    shoulder = ("L", "ZL") if left else ("R", "ZR")
    for i, name in enumerate(shoulder):
        on = name in b
        _rounded(cr, x + (w * 0.15 if left else w * 0.05) + i * 2, y - 7 - i * 6, w * 0.8, 8,
                 (4, 4, 0, 0))
        _rgb(cr, PRESSED if on else (0.10, 0.10, 0.11))
        cr.fill()
    radii = (big, small, small, big) if left else (small, big, big, small)
    _rounded(cr, x, y, w, h, radii)
    _rgb(cr, body_color(c))
    cr.fill()
    # trilho com os LEDs de jogador e SL/SR
    rail_x = x + w - 4 if left else x + 1
    _rounded(cr, rail_x, y + 6, 3, h - 12, 1.5)
    _rgb(cr, (0.08, 0.08, 0.09))
    cr.fill()
    led = c.get("led") or (0, 0, 0, 0)
    for i, on in enumerate(led):
        _circle(cr, rail_x + 1.5, y + h * 0.38 + i * 7, 1.8, GREEN if on else (0.25, 0.25, 0.25))
    for name, fy in (("SL", 0.18), ("SR", 0.78)):
        if name in b:
            _rounded(cr, rail_x - 1, y + h * fy, 5, h * 0.1, 2)
            _rgb(cr, PRESSED)
            cr.fill()
    cx = x + w / 2 - (1.5 if left else -1.5)
    r = w * 0.11
    if left:
        _rounded(cr, cx + w * 0.18, y + h * 0.07, w * 0.18, w * 0.06, 1)
        _rgb(cr, PRESSED if "-" in b else BUTTON)
        cr.fill()
        _stick(cr, cx, y + h * 0.27, w * 0.27, c["stick"], "LS" in b)
        dy = y + h * 0.57
        for name, ox, oy in (("UP", 0, -1), ("DOWN", 0, 1), ("LEFT", -1, 0), ("RIGHT", 1, 0)):
            _btn(cr, cx + ox * w * 0.2, dy + oy * w * 0.2, r, name in b)
        _rounded(cr, cx - w * 0.07 + w * 0.12, y + h * 0.83, w * 0.14, w * 0.14, 2)
        _rgb(cr, PRESSED if "CAP" in b else BUTTON)
        cr.fill()
    else:
        _rounded(cr, cx - w * 0.36, y + h * 0.07, w * 0.18, w * 0.06, 1)
        _rgb(cr, PRESSED if "+" in b else BUTTON)
        cr.fill()
        _rounded(cr, cx - w * 0.30, y + h * 0.07 - w * 0.06, w * 0.06, w * 0.18, 1)
        cr.fill()
        fy = y + h * 0.27
        for name, ox, oy in (("X", 0, -1), ("B", 0, 1), ("Y", -1, 0), ("A", 1, 0)):
            _btn(cr, cx + ox * w * 0.2, fy + oy * w * 0.2, r, name in b)
        _stick(cr, cx, y + h * 0.58, w * 0.27, c["stick"], "RS" in b)
        _circle(cr, cx - w * 0.12, y + h * 0.84, w * 0.09, PRESSED if "HOME" in b else BUTTON)


def draw_pro(cr, x, y, w, h, c):
    b = c["buttons"]
    for name, sx in (("L", 0.08), ("ZL", 0.12), ("R", 0.62), ("ZR", 0.58)):
        _rounded(cr, x + w * sx, y - 6, w * 0.3, 8, (4, 4, 0, 0))
        _rgb(cr, PRESSED if name in b else (0.1, 0.1, 0.11))
        cr.fill()
    _rounded(cr, x, y, w, h * 0.62, w * 0.18)
    _rgb(cr, body_color(c))
    cr.fill()
    for gx in (0.18, 0.82):  # empunhaduras
        cr.arc(x + w * gx, y + h * 0.62, w * 0.17, 0, math.pi)
        cr.fill()
    _stick(cr, x + w * 0.25, y + h * 0.25, w * 0.09, c["stick"], "LS" in b)
    _stick(cr, x + w * 0.62, y + h * 0.48, w * 0.09, c["rstick"], "RS" in b)
    r = w * 0.035
    for name, ox, oy in (("UP", 0, -1), ("DOWN", 0, 1), ("LEFT", -1, 0), ("RIGHT", 1, 0)):
        _btn(cr, x + w * 0.38 + ox * w * 0.06, y + h * 0.48 + oy * w * 0.06, r, name in b)
    for name, ox, oy in (("X", 0, -1), ("B", 0, 1), ("Y", -1, 0), ("A", 1, 0)):
        _btn(cr, x + w * 0.75 + ox * w * 0.07, y + h * 0.25 + oy * w * 0.07, r * 1.2, name in b)
    led = c.get("led") or (0, 0, 0, 0)
    for i, on in enumerate(led):
        _circle(cr, x + w * 0.42 + i * 8, y + h * 0.56, 2, GREEN if on else (0.3, 0.3, 0.3))


def draw_controller(cr, cx, cy, c, scale=1.0, sideways=False, tilt=False):
    """Desenha centrado em (cx, cy). tilt: gira junto com o controle (pros lados)."""
    cr.save()
    cr.translate(cx, cy)
    cr.scale(scale, scale)
    if tilt and "aim" in c:
        cr.rotate(math.radians(max(-1.0, min(1.0, c["aim"][0])) * AIM_LIMIT))
    if c["kind"] == "PRO":
        draw_pro(cr, -70, -45, 140, 120, c)
    else:
        if sideways:
            cr.rotate(-math.pi / 2 if c["kind"] == "L" else math.pi / 2)
        draw_joycon(cr, -23, -65, 46, 130, c)
    cr.restore()


def draw_aim(cr, cx, y, w, h, c, extra=None):
    """A mira, como o ponteiro do Dolphin: uma telinha com o ponto. Apertar o
    analógico centraliza. Apontando pra fora (no Dolphin o ponteiro some,
    como no Wii) vira um anel amarelo na borda, na direção de onde aponta —
    prender cada eixo separado fazia o ponto "contornar" a tela em L.
    extra: outro controle do par (o Nunchuk), desenhado menor e por baixo —
    não aponta no jogo, mas mostra que está vivo."""
    x = cx - w / 2
    _rounded(cr, x, y, w, h, 6)
    _rgb(cr, (0.12, 0.12, 0.13))
    cr.fill()
    _rgb(cr, (0.30, 0.30, 0.32))
    cr.set_line_width(1)
    cr.move_to(cx, y + 4)
    cr.line_to(cx, y + h - 4)
    cr.move_to(x + 4, y + h / 2)
    cr.line_to(x + w - 4, y + h / 2)
    cr.stroke()
    if extra is not None:
        (ex, ey), out = aim_on_screen(extra["aim"])
        ex, ey = cx + ex * (w / 2 - 7), y + h / 2 - ey * (h / 2 - 7)
        if out:  # anel na borda, na cor dele (a borda amarela é só da mira de verdade)
            cr.arc(ex, ey, 3.5, 0, 2 * math.pi)
            _rgb(cr, _kind_color(extra), 0.55)
            cr.set_line_width(1.5)
            cr.stroke()
        else:
            _circle(cr, ex, ey, 3.5, _kind_color(extra), 0.55)
    (ax, ay), out = aim_on_screen(c["aim"])
    px, py = cx + ax * (w / 2 - 7), y + h / 2 - ay * (h / 2 - 7)
    if out:
        _rounded(cr, x, y, w, h, 6)
        _rgb(cr, YELLOW)
        cr.set_line_width(2)
        cr.stroke()
        cr.arc(px, py, 4.5, 0, 2 * math.pi)        # anel: fora da tela, nessa direção
        cr.set_line_width(2)
        cr.stroke()
    else:
        _circle(cr, px, py, 5, _kind_color(c))


def aim_on_screen(aim):
    """((x, y) dentro da telinha, fora?). Fora: o ponto da borda na linha do
    centro até onde aponta (encolhe os dois eixos juntos, sem escorregar)."""
    ax, ay = aim
    far = max(abs(ax), abs(ay))
    if far <= 1.0:
        return (ax, ay), False
    return (ax / far, ay / far), True


def _kind_color(c):
    return body_color(c) if c["kind"] in ("L", "R") else ACCENT


def body_color(c):
    """Cor de verdade do controle (lida da memória dele), ou a padrão."""
    color = c.get("color")
    if color:
        return tuple(v / 255 for v in color["body"])
    return JOYCON_L if c["kind"] == "L" else JOYCON_R if c["kind"] == "R" else PRO_BODY


def draw_battery(cr, x, y, level, charging):
    _rounded(cr, x, y, 22, 11, 2)
    _rgb(cr, TEXT_DIM)
    cr.set_line_width(1.2)
    cr.stroke()
    cr.rectangle(x + 22, y + 3, 2, 5)
    cr.fill()
    if level:
        color = RED if level <= 1 else YELLOW if level == 2 else GREEN
        cr.rectangle(x + 2, y + 2, 18 * level / 5, 7)
        _rgb(cr, color)
        cr.fill()
    if charging:  # raiozinho
        cr.move_to(x + 13, y + 1)
        cr.line_to(x + 8, y + 6.5)
        cr.line_to(x + 11.5, y + 6.5)
        cr.line_to(x + 9, y + 11)
        cr.line_to(x + 15, y + 4.5)
        cr.line_to(x + 11.5, y + 4.5)
        cr.close_path()
        _rgb(cr, TEXT)
        cr.fill()


def _left(seconds):
    """" (42 s)" com limite de tempo; nada quando procura até apertar A."""
    return "" if seconds == float("inf") else f" ({seconds:.0f} s)"


def ctrl_status(c):
    """Texto curto (cabe no cartão) + cor."""
    if c["motion"] == "zerado":
        return "zerado, reconectando", RED
    if c["motion"] == "calibrando":
        return "calibrando…", YELLOW
    if c["motion"] == "sem dados":
        return "sem dados", YELLOW
    if not c.get("calibrated", True):
        return "pouse na mesa 2 s", YELLOW
    if c["hiccups"] >= 30:
        return f"instável ({c['hiccups']:.0f}/min)", YELLOW
    return "movimento ok", GREEN


CARD_W, CARD_H = 215, 400   # tamanho pro qual o cartão foi desenhado
STRIP_H = 190


def _fit(cr, w, h, base_w, base_h):
    """Encolhe o desenho por igual quando falta espaço; devolve o tamanho
    "virtual" (sempre ≥ base) pra desenhar com as medidas de sempre."""
    s = min(1.0, w / base_w if base_w else 1.0, h / base_h)
    if s < 1.0:
        cr.scale(s, s)
    return w / s, h / s


class PlayerCard(Gtk.DrawingArea):
    def __init__(self, number, app):
        super().__init__()
        self.number, self.app = number, app
        self.drawn = None            # estado do último desenho (redesenha se mudar)
        # o desenho é feito pra 215×400 e encolhe junto (tela pequena, 1366×768)
        self.set_size_request(int(CARD_W * 0.7), int(CARD_H * 0.62))
        self.connect("draw", self.on_draw)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self.on_click)

    def _single(self):
        st = self.app.state
        p = st["players"].get(self.number) if st else None
        return p is not None and p["mode"] in ("sozinho", "empe")

    def on_click(self, _w, _ev):
        if self._single():  # Joy-Con sozinho: alterna deitado ↔ em pé
            self.app.server.call(self.app.server.toggle_single_mode, self.number)
        return True

    def on_draw(self, widget, cr):
        w, h = _fit(cr, self.get_allocated_width(), self.get_allocated_height(),
                    CARD_W, CARD_H)
        st = self.app.state
        p = st["players"].get(self.number) if st else None
        _rounded(cr, 4, 4, w - 8, h - 8, 16)
        _rgb(cr, CARD if p else CARD_EMPTY)
        cr.fill()
        if p and p["complete"]:
            _rounded(cr, 4, 4, w - 8, h - 8, 16)
            _rgb(cr, ACCENT, 0.9)
            cr.set_line_width(2)
            cr.stroke()
        _text(cr, f"Jogador {self.number}", w / 2, 34, 16, TEXT if p else TEXT_DIM, bold=True)
        for i in range(4):  # LEDs do número do jogador
            on = i < self.number
            _circle(cr, w / 2 - 21 + i * 14, 50, 4, GREEN if (p and on) else (0.35, 0.35, 0.37))
        if not p:
            _text(cr, "—", w / 2, h / 2 + 10, 30, (0.35, 0.35, 0.37))
            return
        label = MODE_NAMES[p["mode"]]
        if p["mode"] in ("par", "pro") and not p.get("nunchuk", True):
            label = "Wii Remote (Nunchuk tirado)"
        _text(cr, label, w / 2, 72, 12, TEXT_DIM)
        mid = 165 + max(0, h - 400) * 0.3
        members = p["members"]
        # a mira é do Wii Remote: no par, o direito (o esquerdo aparece menor)
        if p["mode"] == "par":
            pointer, extra = members.get("R"), members.get("L")
            if pointer is None:
                pointer, extra = extra, None
        else:
            pointer, extra = next(iter(members.values()), None), None
        if pointer is not None:
            draw_aim(cr, w / 2, mid + 82, 110, 58, pointer, extra)
        if p["mode"] == "par":
            for role, dx in (("L", -30), ("R", 30)):
                if role in members:
                    draw_controller(cr, w / 2 + dx, mid, members[role], tilt=True)
                else:
                    _rounded(cr, w / 2 + dx - 23, mid - 65, 46, 130, 14)
                    cr.set_dash([5, 4])
                    _rgb(cr, TEXT_DIM)
                    cr.set_line_width(1.5)
                    cr.stroke()
                    cr.set_dash([])
                    _text(cr, "?", w / 2 + dx, mid + 6, 22, TEXT_DIM)
        elif members:
            c = next(iter(members.values()))
            draw_controller(cr, w / 2, mid, c, sideways=p["mode"] == "sozinho", tilt=True)

        y = h - 70
        for c in members.values():
            draw_battery(cr, 22, y - 9, c["battery"], c["charging"])
            label = c["name"].replace("Joy-Con ", "")
            _text(cr, label, 60, y, 11, TEXT_DIM, center=False)
            msg, color = ctrl_status(c)
            _circle(cr, 98, y - 4, 3.5, color)
            _text(cr, msg, 106, y, 11, color, center=False)
            y += 20
        for role in p["missing"]:
            _text(cr, f"({role}) caiu — aperte um botão nele", w / 2, y, 11, YELLOW)
            y += 18


class WaitingStrip(Gtk.DrawingArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.drawn = None
        self.set_size_request(-1, int(STRIP_H * 0.68))
        self.connect("draw", self.on_draw)

    def on_draw(self, widget, cr):
        w, h = _fit(cr, self.get_allocated_width(), self.get_allocated_height(),
                    0, STRIP_H)
        st = self.app.state
        waiting = st["waiting"] if st else []
        _rounded(cr, 4, 4, w - 8, h - 8, 16)
        _rgb(cr, CARD_EMPTY)
        cr.fill()
        if not waiting:
            if st and st["searching"]:
                msg = ("Procurando controles — segure o botãozinho de sync na lateral do "
                       "Joy-Con. Terminou? Aperte A.")
            else:
                msg = ("Controle já pareado: aperte um botão nele.  Controle novo: "
                       "\"Procurar controles\" e segure o sync.")
            _text(cr, msg, w / 2, h / 2 + 5, 13, TEXT_DIM)
            return
        _text(cr, "L + R juntos (um Joy-Con em cada mão) = Wii Remote + Nunchuk   ·   "
              "SL + SR = deitado   ·   R + ZR (ou L + ZL) = em pé", w / 2, 28, 13, TEXT, bold=True)
        pulse = 0.55 + 0.45 * math.sin(time.monotonic() * 4)
        n = len(waiting)
        gap = min(110, (w - 40) / max(n, 1))
        x0 = w / 2 - gap * (n - 1) / 2
        for i, c in enumerate(waiting):
            cx = x0 + i * gap
            cr.arc(cx, 115, 52, 0, 2 * math.pi)
            _rgb(cr, ACCENT, 0.18 * pulse)
            cr.fill()
            draw_controller(cr, cx, 112, c, scale=0.62 if c["kind"] != "PRO" else 0.5)
            _text(cr, c["name"].replace("Joy-Con ", "Joy-Con "), cx, h - 14, 11, TEXT_DIM)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="EasyJoycon")
        self.app = app
        self.set_default_size(1100, 680)  # cabe em 1366×768
        try:
            self.set_icon_from_file(str(ICON_FILE))
        except GLib.Error:      # sem o leitor de SVG (librsvg) ou sem o arquivo
            self.set_icon_name("input-gaming")
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(14)
        self.add(root)

        top = Gtk.Box(spacing=10)
        title = Gtk.Label(label="EasyJoycon")
        title.get_style_context().add_class("ej-title")
        top.pack_start(title, False, False, 0)
        self.bt_pill = Gtk.Label()
        self.dolphin_pill = Gtk.Label()
        for pill in (self.dolphin_pill, self.bt_pill):
            pill.get_style_context().add_class("ej-pill")
            top.pack_end(pill, False, False, 0)
        self.autostart = Gtk.CheckButton(label="Abrir com o computador")
        self.autostart.set_tooltip_text("Liga o EasyJoycon escondido perto do relógio quando "
                                        "você entra no computador")
        self.autostart.set_active(AUTOSTART.exists())
        if AUTOSTART.exists():
            set_autostart(True)     # o AppImage pode ter mudado de pasta
        self.autostart.connect("toggled", lambda b: set_autostart(b.get_active()))
        top.pack_end(self.autostart, False, False, 8)
        root.pack_start(top, False, False, 0)

        self.banner = Gtk.Label(xalign=0)
        self.banner.set_line_wrap(True)
        self.banner.get_style_context().add_class("ej-banner")
        self.banner_rev = Gtk.Revealer()
        self.banner_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.banner_rev.add(self.banner)
        root.pack_start(self.banner_rev, False, False, 0)
        self._banner_until = 0.0

        self.warn_box = Gtk.Box(spacing=10)
        self.warn_box.get_style_context().add_class("ej-warn")
        self.warn_label = Gtk.Label(xalign=0)
        self.warn_label.set_line_wrap(True)
        self.warn_box.pack_start(self.warn_label, True, True, 0)
        self.rebond_btn = Gtk.Button(label="Parear de novo")
        self.rebond_btn.connect("clicked", lambda *_: self.app.rebond())
        self.warn_box.pack_end(self.rebond_btn, False, False, 0)
        self.setup_btn = Gtk.Button(label="Preparar o sistema")
        self.setup_btn.set_tooltip_text("Ajusta o Bluetooth e as permissões dos controles "
                                        "(pede a senha de administrador uma vez)")
        self.setup_btn.connect("clicked", lambda *_: self.app.prepare_system())
        self.warn_box.pack_end(self.setup_btn, False, False, 0)
        self.warn_rev = Gtk.Revealer()
        self.warn_rev.add(self.warn_box)
        root.pack_start(self.warn_rev, False, False, 0)

        cards = Gtk.Box(spacing=10, homogeneous=True)
        self.cards = [PlayerCard(n, app) for n in range(1, 5)]
        self.card_buttons = []
        for c in self.cards:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.pack_start(c, True, True, 0)
            # 2×2: os 4 lado a lado deixavam a janela mais larga que a tela
            row = Gtk.Grid(row_spacing=4, column_spacing=4, column_homogeneous=True)
            ident = Gtk.Button(label="Identificar")
            ident.set_tooltip_text("Vibra e acende os LEDs do controle desse jogador")
            ident.connect("clicked", lambda _b, n=c.number: app.server.call(app.server.identify, n))
            mode = Gtk.Button(label="Em pé")
            mode.connect("clicked", lambda _b, n=c.number: app.server.call(
                app.server.toggle_single_mode, n))
            join = Gtk.Button(label="Juntar")
            join.connect("clicked", lambda _b, n=c.number: app.server.call(
                app.server.join_or_split, n))
            nun = Gtk.Button(label="Nunchuk")
            nun.set_tooltip_text("Tira ou põe o Nunchuk com o jogo rodando (quando o jogo "
                                 "pedir). Vale quando você voltar pro Dolphin — ele só lê os "
                                 "controles com a janela dele em foco. No controle, na hora: "
                                 "botão de captura do Joy-Con esquerdo")
            nun.connect("clicked", lambda _b, n=c.number: app.server.call(
                app.server.toggle_nunchuk, n))
            for i, btn in enumerate((ident, mode, join, nun)):
                btn.set_hexpand(True)
                row.attach(btn, i % 2, i // 2, 1, 1)
            col.pack_start(row, False, False, 0)
            cards.pack_start(col, True, True, 0)
            self.card_buttons.append((c.number, ident, mode, join, nun))
        root.pack_start(cards, True, True, 0)
        self.strip = WaitingStrip(app)
        self.add_tick_callback(self._on_frame)
        root.pack_start(self.strip, False, False, 0)

        bar = Gtk.Box(spacing=8)
        bar.get_style_context().add_class("ej-bar")
        self.search_btn = Gtk.Button(label="Procurar controles")
        self.search_btn.connect("clicked", lambda *_: self.app.toggle_search())
        bar.pack_start(self.search_btn, False, False, 0)
        calib = Gtk.Button(label="Calibrar")
        calib.set_tooltip_text("Deixe os controles parados em cima da mesa por 2 segundos")
        calib.connect("clicked", lambda *_: self.app.calibrate())
        bar.pack_start(calib, False, False, 0)
        reset = Gtk.Button(label="Recomeçar jogadores")
        reset.set_tooltip_text("Todo mundo volta a esperar o L+R / SL+SR")
        reset.connect("clicked", lambda *_: self.app.reset_players())
        bar.pack_start(reset, False, False, 0)
        forget = Gtk.Button(label="Esquecer controles")
        forget.set_tooltip_text("Tira todos os Joy-Cons/Pro Controller do Bluetooth, como "
                                "\"remover dispositivo\", pra parear do zero")
        forget.connect("clicked", lambda *_: self.app.forget_all())
        bar.pack_start(forget, False, False, 0)
        # as duas réguas empilhadas: lado a lado a janela ficava larga demais
        sliders = Gtk.Grid(column_spacing=8)
        self.smooth = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 10, 1)
        self.smooth.set_value(app.server.smoothing)
        self.smooth.set_tooltip_text("0 = nenhuma (mais rápido) · 10 = bem suave")
        self.smooth.connect("value-changed", self.on_smooth)
        self.rumble = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 10)
        self.rumble.set_value(app.server.rumble_force)
        self.rumble.set_tooltip_text("Força da vibração dos jogos · 0 = desligada")
        self.rumble.connect("value-changed", self.on_rumble)
        for row, (text, scale) in enumerate((("Suavização", self.smooth),
                                             ("Vibração", self.rumble))):
            lbl = Gtk.Label(label=text, xalign=1)
            lbl.get_style_context().add_class("ej-dim")
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_size_request(130, -1)
            sliders.attach(lbl, 0, row, 1, 1)
            sliders.attach(scale, 1, row, 1, 1)
        bar.pack_start(sliders, False, False, 6)
        play = Gtk.Button(label="Abrir Dolphin")
        play.connect("clicked", lambda *_: self.app.open_dolphin())
        bar.pack_end(play, False, False, 0)
        conf = Gtk.Button(label="Configurar Dolphin")
        conf.set_tooltip_text("Mapeia os Wii Remotes conforme os jogadores de agora "
                              "(com o Dolphin fechado)")
        conf.connect("clicked", lambda *_: self.app.configure_dolphin())
        bar.pack_end(conf, False, False, 0)
        root.pack_start(bar, False, False, 0)

        self.connect("key-press-event", self.on_key)

    def on_smooth(self, scale):
        level = round(scale.get_value())
        self.app.server.call(self.app.server.set_smoothing, level)

    def on_rumble(self, scale):
        force = round(scale.get_value() / 10) * 10
        self.app.server.call(self.app.server.set_rumble_force, force)

    def on_key(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval) or ""
        actions = {"p": self.app.toggle_search, "c": self.app.calibrate,
                   "r": self.app.reset_players}
        if k.lower() in actions:
            actions[k.lower()]()
            return True
        if k in ("bracketleft", "bracketright"):
            self.smooth.set_value(self.smooth.get_value() + (1 if k == "bracketright" else -1))
            return True
        return False

    def show_banner(self, msg, bad=False):
        ctx = self.banner.get_style_context()
        (ctx.add_class if bad else ctx.remove_class)("ej-bad")
        self.banner.set_text(msg)
        self.banner_rev.set_reveal_child(True)
        self._banner_until = time.monotonic() + BANNER_SECONDS

    def refresh(self, st):
        now = time.monotonic()
        if self._banner_until and now > self._banner_until:
            self.banner_rev.set_reveal_child(False)
            self._banner_until = 0.0
        if st["bt_ok"]:
            self.bt_pill.set_text(f"● Bluetooth {st['bt_name']}"
                                  + (" · procurando" + _left(st["search_left"])
                                     if st["searching"] else ""))
        else:
            self.bt_pill.set_text(f"✕ Bluetooth: {st['bt_error'] or 'sem adaptador'}")
        self.dolphin_pill.set_text("● Dolphin recebendo" if st["dolphin"]
                                   else "○ Dolphin ainda não conectou")
        self.search_btn.set_label("Parar de procurar" if st["searching"]
                                  else "Procurar controles")
        warns = []
        setup = self.app.setup_missing
        if setup:
            warns.append("O sistema ainda não está preparado pro EasyJoycon ("
                         + "; ".join(setup) + "). Clique em \"Preparar o sistema\": "
                         "vai pedir a senha de administrador uma vez só.")
        elif st["no_permission"]:
            warns.append(f"Sem permissão pra ler os botões de {st['no_permission']} controle(s): "
                         "desligue e ligue o controle; se continuar, saia e entre de novo "
                         "no computador.")
        if st["unbonded"]:
            nomes = ", ".join(sorted({n for _, n in st["unbonded"]}))
            warns.append(f"{nomes}: pareado(s) sem guardar a chave — não reconectam sozinhos. "
                         "Clique em \"Parear de novo\" e segure o botão de sync de cada um.")
        if st.get("hid_users"):
            quem = " e ".join(st["hid_users"])
            quem = quem[:1].upper() + quem[1:]
            warns.append(f"{quem} está usando o controle e manda comandos próprios pra ele: "
                         "a calibração e a reconexão pelo botão podem falhar. Feche e, se o "
                         "movimento ou o analógico ficarem estranhos, desligue e ligue o "
                         "controle.")
        if st.get("uinput_error") and not setup:     # o "Preparar o sistema" resolve
            warns.append("Sem o controle virtual (/dev/uinput): no Dolphin não funcionam a "
                         "vibração, o Nunchuk nem o modo deitado. Veja o que falta com "
                         "o diagnóstico (comando diagnostico).")
        if self.app.dolphin_pending:
            if self.app.dolphin_pending == "versao":
                warns.append("O EasyJoycon tem um jeito novo de configurar o Dolphin: feche e "
                             "abra o Dolphin uma vez (depois, mudar os jogadores vale com ele "
                             "aberto).")
            else:
                warns.append("Entrou jogador além dos Wii Remotes ligados no Dolphin: feche e "
                             "abra o Dolphin pra ligar mais um (ou suba \"wiimotes\" no "
                             "config).")
        self.rebond_btn.set_visible(bool(st["unbonded"]))
        self.setup_btn.set_visible(bool(setup))
        self.setup_btn.set_sensitive(not self.app.preparing)
        self.warn_label.set_text("\n".join(warns))
        self.warn_rev.set_reveal_child(bool(warns))
        singles = {n: next(iter(p["members"])) for n, p in st["players"].items()
                   if p["mode"] in ("sozinho", "empe") and len(p["members"]) == 1}
        for n, ident, mode, join, nun in self.card_buttons:
            role = singles.get(n)
            p = st["players"].get(n)
            pair = bool(p and p["mode"] == "par")
            join.set_visible(pair or (role in ("L", "R") and any(
                r in ("L", "R") and r != role for m, r in singles.items() if m != n)))
            join.set_label("Separar" if pair else "Juntar")
            has_nun = bool(p and p["mode"] in ("par", "pro"))
            nun.set_visible(has_nun)
            if has_nun:
                nun.set_label("Tirar Nunchuk" if p["nunchuk"] else "Pôr Nunchuk")
            join.set_tooltip_text("Cada Joy-Con vira um Wii Remote em pé" if pair else
                                  "Junta com o outro Joy-Con sozinho num Wii Remote + Nunchuk")
            ident.set_sensitive(bool(p and p["members"]))
            single = bool(p and p["mode"] in ("sozinho", "empe"))
            mode.set_visible(single)
            if single:
                mode.set_label("Usar em pé" if p["mode"] == "sozinho" else "Usar deitado")
        self.redraw_changed(st)

    def redraw_changed(self, st):
        """Redesenha só o cartão que mudou (o desenho segue o controle a ~60 qps
        sem gastar processador com quem está parado)."""
        for c in self.cards:
            p = st["players"].get(c.number)
            if p != c.drawn:
                c.drawn = p
                c.queue_draw()
        waiting = st["waiting"], st["searching"]
        if waiting != self.strip.drawn or st["waiting"]:   # esperando: luz pulsando
            self.strip.drawn = waiting
            self.strip.queue_draw()

    def _on_frame(self, _widget, _clock):
        st = self.app.server.state
        if st is not None and st is not self.app.state and self.get_visible():
            self.app.state = st
            self.redraw_changed(st)
        return GLib.SOURCE_CONTINUE


def set_autostart(on):
    """Cria/apaga ~/.config/autostart/easyjoycon.desktop."""
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(
            "[Desktop Entry]\nType=Application\nName=EasyJoycon\n"
            "Comment=Joy-Cons no Dolphin com movimento\n"
            f'Exec="{LAUNCHER}" --segundo-plano\nIcon={DESKTOP_ICON}\nTerminal=false\n'
            "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    else:
        AUTOSTART.unlink(missing_ok=True)


class EasyJoyconApp(Gtk.Application):
    def __init__(self, server, hidden=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.server = server
        self._start_hidden = hidden
        self.tray = None
        self._told_tray = False
        self._tray_tip = None
        self.window = None
        self.state = None
        self._thread = None
        self._bus = None
        self.server_error = None
        self.cfg = config.load()
        self.dolphin_open = False
        self.dolphin_pending = None   # jogadores mudaram com o Dolphin aberto
        self._last_check = 0.0
        self.setup_missing = sistema.missing()   # o que falta preparar no sistema
        self.preparing = False

    # --- ciclo de vida ------------------------------------------------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        style = Gtk.CssProvider()
        style.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        self.window = MainWindow(self)
        self.window.connect("delete-event", self._on_close)
        self.tray = self._make_tray()
        if self.tray is not None:
            self.hold()   # continua rodando com a janela escondida
        GLib.timeout_add(50, self._tick)

    def do_activate(self):
        if self._start_hidden and self.tray is not None:
            self._start_hidden = False      # aberto pelo login: fica só no relógio
            return
        self.show_window()

    def show_window(self, *_):
        first = not self.window.get_visible()
        self.window.show_all()
        if first:
            self.window.warn_rev.set_reveal_child(False)
            if self.state:
                self.window.refresh(self.state)
        self.window.present()

    def _on_close(self, *_):
        if self.tray is None:
            self.quit()
            return False
        self.window.hide()
        if not self._told_tray:
            self._told_tray = True
            self.notify("info-tray", "O EasyJoycon continua ligado perto do relógio — "
                        "os controles seguem funcionando. Pra fechar de vez: ícone → Sair.")
        return True

    def _tray_menu(self):
        menu = Gtk.Menu()
        for label, cb in (("Mostrar a janela", self.show_window),
                          ("Procurar controles", lambda *_: self.toggle_search()),
                          ("Sair", lambda *_: self.quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        return menu

    def _make_tray(self):
        if XApp is not None and hasattr(XApp, "StatusIcon"):
            icon = XApp.StatusIcon()
            icon.set_icon_name("input-gaming")
            icon.set_tooltip_text("EasyJoycon")
            icon.set_secondary_menu(self._tray_menu())
            icon.connect("activate", lambda *_: self.show_window())
            return icon
        if AyatanaAppIndicator3 is not None:
            ind = AyatanaAppIndicator3.Indicator.new(
                "easyjoycon", "input-gaming",
                AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            ind.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
            ind.set_menu(self._tray_menu())
            return ind
        return None

    def do_shutdown(self):
        self.server.call(self.server.stop)
        if self._thread:
            self._thread.join(timeout=3)
        Gtk.Application.do_shutdown(self)

    def _run_server(self):
        import asyncio
        try:
            asyncio.run(self.server.run())
        except OSError as e:
            self.server_error = ("A porta do DSU já está em uso — o EasyJoycon já está aberto?"
                                 if e.errno == 98 else str(e))
            log.error("servidor parou: %s", e)
        except Exception as e:  # mostrar na janela em vez de sumir calado
            self.server_error = f"o servidor parou: {e}"
            log.exception("servidor parou")

    def _tick(self):
        if self.server_error and self.window:
            self.window.show_banner(self.server_error, bad=True)
            self.server_error = None
        st = self.server.state
        if st is not None:
            self.state = st
            if self.window and self.window.get_visible():
                self.window.refresh(st)
            n = sum(1 for p in st["players"].values() if p["members"])
            tip = f"EasyJoycon — {n} jogador(es)"
            if st["dolphin"]:
                tip += " · Dolphin recebendo"
            is_xapp = XApp is not None and isinstance(self.tray, XApp.StatusIcon)
            if tip != self._tray_tip and is_xapp:
                self._tray_tip = tip
                self.tray.set_tooltip_text(tip)
        while True:
            try:
                kind, msg = self.server.notices.get_nowait()
            except queue.Empty:
                break
            self.notify(kind, msg)
        now = time.monotonic()
        if st is not None and now - self._last_check > 2.0:
            self._last_check = now
            self._follow_players(st["layout"])
        return True

    def _needs_dolphin(self, layout):
        """O Dolphin precisa ser reconfigurado? Só se mudou QUAIS Wii Remotes
        estão ligados (o jeito de jogar muda com ele aberto)."""
        return dolphin.slots_for(layout, self._wiimotes()) != dolphin.applied_slots()

    def _wiimotes(self):
        return self.cfg["dolphin"]["wiimotes"]

    def _follow_players(self, layout):
        """Mantém o Dolphin configurado pros jogadores de agora."""
        self.dolphin_open = dolphin.dolphin_running()
        if not self._needs_dolphin(layout):
            self.dolphin_pending = None
            return
        if self.dolphin_open:
            self.dolphin_pending = "versao" if dolphin.applied_slots() is None else "mais"
        elif self.cfg["dolphin"]["configurar_sozinho"]:
            self.apply_layout(layout, quiet=True)

    # --- avisos -------------------------------------------------------
    def notify(self, kind, msg):
        bad = kind in ("desconectou", "aviso")
        if kind == "info-tray":
            kind = "aviso"
            bad = False
        if self.window and self.window.get_visible():
            self.window.show_banner(msg[:1].upper() + msg[1:], bad=bad)
        if kind not in NOTIFY_KINDS:
            return
        try:
            if self._bus is None:
                self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            hints = {"transient": GLib.Variant("b", not bad),
                     "urgency": GLib.Variant("y", 1)}
            self._bus.call(
                "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", "Notify",
                GLib.Variant("(susssasa{sv}i)", ("EasyJoycon", 0, DESKTOP_ICON, "EasyJoycon",
                                                 msg[:1].upper() + msg[1:], [], hints,
                                                 6000 if bad else 3000)),
                None, Gio.DBusCallFlags.NONE, -1, None, None, None)
        except GLib.Error as e:
            log.debug("notificação: %s", e)

    # --- botões -------------------------------------------------------
    def toggle_search(self):
        self.server.call(self.server.toggle_search)

    def calibrate(self):
        self.server.call(self.server.calibrate)

    def reset_players(self):
        self.server.call(self.server.reset_players)

    def prepare_system(self):
        """Roda o instalar-sistema.sh com a janelinha de senha, numa thread."""
        if self.preparing:
            return
        self.preparing = True
        self.window.show_banner("Preparando o sistema: digite a sua senha na janelinha.")

        def work():
            ok, out = sistema.prepare()
            GLib.idle_add(done, ok, out)

        def done(ok, out):
            self.preparing = False
            self.setup_missing = sistema.missing()
            log.info("preparar o sistema: %s\n%s", "ok" if ok else "falhou", out)
            if ok and not self.setup_missing:
                self.window.show_banner("Sistema pronto! Se um controle já estava ligado, "
                                        "desligue e ligue ele de novo.")
            else:
                last = out.strip().splitlines()[-1] if out.strip() else "não sei por quê"
                self.window.show_banner(f"Não deu pra preparar o sistema: {last}", bad=True)
            return False

        threading.Thread(target=work, daemon=True).start()

    def rebond(self):
        self.window.show_banner("Segure o botãozinho de sync na lateral de cada Joy-Con "
                                "(as luzes vão correr) até ele conectar.")
        self.server.call(self.server.forget_and_search)

    def forget_all(self):
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE,
                                text="Esquecer todos os controles?")
        dlg.format_secondary_text(
            "Os Joy-Cons e o Pro Controller saem do Bluetooth deste computador, como em "
            "\"remover dispositivo\", e os jogadores são apagados. Depois é só segurar o "
            "botãozinho de sync de um controle por vez pra parear de novo.\n\n"
            "Outros aparelhos (fone, teclado) não são mexidos.")
        dlg.add_buttons("Cancelar", Gtk.ResponseType.CANCEL,
                        "Esquecer", Gtk.ResponseType.OK)
        dlg.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class(
            "destructive-action")
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if ok:
            self.window.show_banner("Esquecendo… depois segure o sync de UM controle por vez "
                                    "(as luzes correm) até ele conectar.")
            self.server.call(self.server.forget_all)

    def apply_layout(self, layout, quiet=False):
        """Configura os Wii Remotes conforme os jogadores. True se deu certo."""
        try:
            path, _ = dolphin.configure(layout, self.cfg["dsu"]["porta"],
                                        self.cfg["dsu"]["porta_nunchuk"], self._wiimotes())
        except (RuntimeError, OSError) as e:
            if not quiet:
                self.window.show_banner(f"Não configurei o Dolphin: {e}.", bad=True)
            return False
        self.dolphin_pending = None
        slots = dolphin.slots_for(layout, self._wiimotes())
        log.info("Dolphin configurado (Wii Remotes e GameCube %s) em %s", slots, path)
        self.window.show_banner(f"Dolphin pronto: {len(slots)} Wii Remote(s) e controle(s) de "
                                "GameCube; trocar o jeito de jogar vale com ele aberto.")
        return True

    def configure_dolphin(self):
        if not dolphin.installed():
            self.dolphin_missing()
            return
        self.apply_layout((self.state or {}).get("layout") or {})

    def open_dolphin(self):
        cmd = dolphin.launcher()
        if cmd is None:
            self.dolphin_missing()
            return
        if dolphin.dolphin_running():
            self.window.show_banner("O Dolphin já está aberto.")
            return
        layout = (self.state or {}).get("layout") or {}
        if self._needs_dolphin(layout) and not self.apply_layout(layout):
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def dolphin_missing(self):
        """Dolphin não instalado: explica como instalar (com link pro site)."""
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.NONE, text="Falta o Dolphin")
        dlg.format_secondary_text(dolphin.INSTALL_HELP)
        dlg.add_buttons("Abrir o site do Dolphin", 1, "Fechar", Gtk.ResponseType.CLOSE)
        if dlg.run() == 1:
            Gtk.show_uri_on_window(self.window, dolphin.DOWNLOAD_URL, Gdk.CURRENT_TIME)
        dlg.destroy()


def run(server, hidden=False):
    app = EasyJoyconApp(server, hidden)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, lambda: (app.quit(), False)[1])   # Ctrl+C
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, lambda: (app.quit(), False)[1])  # SIGTERM
    return app.run([])
