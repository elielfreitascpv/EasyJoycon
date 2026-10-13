"""Tela no terminal: quem está conectado, jogadores, e teclas de atalho."""
import asyncio
import os
import shutil
import sys
import termios
import time
import tty

from . import spawn
from .players import MODE_NAMES

BATTERY = {0: "?", 1: "crítica", 2: "baixa", 3: "média", 4: "alta", 5: "cheia"}
LEDS = {(1, 0, 0, 0): "●○○○", (1, 1, 0, 0): "●●○○", (1, 1, 1, 0): "●●●○",
        (1, 1, 1, 1): "●●●●", (1, 0, 0, 1): "●○○●"}
BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"
YELLOW, GREEN, RED = "\x1b[33m", "\x1b[32m", "\x1b[31m"

KEYS = ("P procurar controles · C calibrar (controles parados) · [ ] suavização · "
        "R recomeçar jogadores · Q sair")


def _ctrl_line(ctrl, now):
    level, charging = ctrl.battery()
    bat = BATTERY[level] + (" carregando" if charging else "")
    m = ctrl.motion
    if m.broken_driver_cal:
        mov = f"{RED}movimento zerado{RESET}"
    elif m.last_calibration == "calibrando":
        mov = f"{YELLOW}calibrando…{RESET}"
    elif not m.calibrated:
        mov = f"{YELLOW}pouse na mesa 2 s pra calibrar{RESET}"
    elif m.stalled(now):
        mov = f"{YELLOW}sem dados{RESET}"
    else:
        mov = f"{GREEN}movimento ok{RESET}"
    hic = ctrl.hiccups_per_min(now)
    net = f"  {YELLOW}engasgos {hic:.0f}/min{RESET}" if hic >= 6 else ""
    leds = LEDS.get(ctrl.led_pattern, "")
    return f"{ctrl.name:15} {leds:4}  bateria {bat:16} {mov}{net}"


def render(server, width):
    now = time.monotonic()
    out = []
    bt = server.bt
    if bt.adapter:
        left = bt.searching_until - now
        left = "até apertar A" if left == float("inf") else f"{left:.0f} s"
        state = (f"{YELLOW}procurando ({left}){RESET}"
                 if bt.searching else "não está procurando")
        out.append(f"{BOLD}EasyJoycon{RESET}  ·  Bluetooth {bt.adapter_name}: {state}")
    else:
        erro = bt.error or "sem adaptador"
        out.append(f"{BOLD}EasyJoycon{RESET}  ·  {RED}Bluetooth: {erro}{RESET}")
    dol = (f"{GREEN}recebendo{RESET}" if server.dolphin_connected
           else f"{DIM}ainda não conectou (abra o Dolphin){RESET}")
    out.append(f"Dolphin: {dol}")
    out.append("")

    for n in range(1, 5):
        p = server.players.players.get(n)
        if p is None:
            out.append(f"{DIM}Jogador {n}: —{RESET}")
            continue
        status = "" if p.complete else f"  {YELLOW}(falta controle — aperte um botão nele){RESET}"
        out.append(f"{BOLD}Jogador {n}{RESET} — {MODE_NAMES[p.mode]}{status}")
        for role in ("R", "L", "PRO"):
            c = p.members.get(role)
            if c:
                out.append("   " + _ctrl_line(c, now))
            elif role in p.macs:
                out.append(f"   {DIM}{role}: desconectado{RESET}")
    out.append("")

    waiting = list(server.players.waiting.values())
    if waiting:
        out.append(f"{BOLD}Esperando{RESET} — L e R juntos (um Joy-Con em cada mão), "
                   "SL+SR (deitado) ou R+ZR / L+ZL (em pé):")
        out.extend("   " + _ctrl_line(c, now) for c in waiting)
        out.append("")
    hid_users = sorted({n for names in server.hid_users.values() for n in names})
    if hid_users:
        out.append(f"{RED}{' e '.join(hid_users)} está usando o controle{RESET} — feche: "
                   "estraga a calibração e a reconexão.")
        out.append("")
    if server.no_permission:
        out.append(f"{RED}Sem permissão pra ler os botões de {len(server.no_permission)} "
                   f"controle(s).{RESET} Rode uma vez:  scripts/instalar-sistema.sh "
                   "(ou \"Preparar o sistema\" na janela)")
        out.append("")

    mv = server.cfg["movimento"]
    out.append(f"Movimento: suavização {BOLD}{server.smoothing}{RESET} (0–10)  ·  "
               f"zona morta {mv['zona_morta']}°/s  ·  config em data/config.toml")
    out.append("")
    out.append(f"{DIM}Últimas mensagens:{RESET}")
    for t, msg in server.players.events[-5:]:
        ago = time.strftime("%H:%M:%S", time.localtime(time.time() - (now - t)))
        out.append(f"  {DIM}{ago}{RESET} {msg}")
    out.append("")
    out.append(f"{DIM}{KEYS}{RESET}")
    return "\n".join(line if len(line) < width + 20 else line[: width + 20] for line in out)


class TerminalUI:
    def __init__(self, server):
        self.server = server
        self.tty = sys.stdin.isatty() and sys.stdout.isatty()
        self._old = None

    async def run(self):
        if not self.tty:
            return
        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        loop.add_reader(fd, self._on_key)
        sys.stdout.write("\x1b[?25l\x1b[2J")   # esconde o cursor, limpa
        try:
            while not self.server.stopping.is_set():
                width = shutil.get_terminal_size((100, 30)).columns
                sys.stdout.write("\x1b[H" + render(self.server, width).replace("\n", "\x1b[K\n")
                                 + "\x1b[K\x1b[J")
                sys.stdout.flush()
                await asyncio.sleep(0.25)
        finally:
            self.restore()

    def restore(self):
        if self._old is not None:
            fd = sys.stdin.fileno()
            asyncio.get_event_loop().remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, self._old)
            self._old = None
            sys.stdout.write("\x1b[?25h\n")
            sys.stdout.flush()

    def _on_key(self):
        try:
            keys = os.read(sys.stdin.fileno(), 32).decode(errors="ignore").lower()
        except OSError:
            return
        s = self.server
        for k in keys:
            if k == "q":
                s.stop()
            elif k == "p":
                spawn(s.toggle_search())
            elif k == "c":
                s.calibrate()
            elif k == "[":
                s.change_smoothing(-1)
            elif k == "]":
                s.change_smoothing(+1)
            elif k == "r":
                s.reset_players()
