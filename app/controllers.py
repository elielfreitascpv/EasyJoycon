"""Controles físicos: acha os Joy-Cons/Pro Controller em /dev/input, lê
botões, analógicos e movimento, e mexe nos LEDs e na bateria pelo /sys.

Cada controle do hid-nintendo vira dois dispositivos com o mesmo "uniq"
(o MAC): "Joy-Con (R)" (botões/analógico) e "Joy-Con (R) (IMU)" (movimento).
"""
import errno
import logging
import os
import time
from pathlib import Path

from . import evdev as E
from .ff import Rumbler
from .hdrumble import DirectRumble
from .motion import ACCEL_RES, GYRO_RES, MotionProcessor
from .rawimu import KINDS as RAW_KINDS
from .rawimu import RawImu

log = logging.getLogger(__name__)

SYS_INPUT = Path("/sys/class/input")
SYS_LEDS = Path("/sys/class/leds")
SYS_POWER = Path("/sys/class/power_supply")

NINTENDO = "057e"
KINDS = {"2006": "L", "2007": "R", "2009": "PRO"}
KIND_NAMES = {"L": "Joy-Con (L)", "R": "Joy-Con (R)", "PRO": "Pro Controller"}

# Código do evdev → nome do botão, por tipo de controle (hid-nintendo.c:
# left/right_joycon_button_mappings e *_s_button_mappings pro SL/SR).
BUTTONS = {
    "L": {E.BTN_TL: "L", E.BTN_TL2: "ZL", E.BTN_SELECT: "-", E.BTN_THUMBL: "LS",
          E.BTN_DPAD_UP: "UP", E.BTN_DPAD_DOWN: "DOWN", E.BTN_DPAD_LEFT: "LEFT",
          E.BTN_DPAD_RIGHT: "RIGHT", E.BTN_Z: "CAP", E.BTN_TR: "SL", E.BTN_TR2: "SR"},
    "R": {E.BTN_EAST: "A", E.BTN_SOUTH: "B", E.BTN_NORTH: "X", E.BTN_WEST: "Y",
          E.BTN_TR: "R", E.BTN_TR2: "ZR", E.BTN_START: "+", E.BTN_THUMBR: "RS",
          E.BTN_MODE: "HOME", E.BTN_TL: "SL", E.BTN_TL2: "SR"},
    "PRO": {E.BTN_EAST: "A", E.BTN_SOUTH: "B", E.BTN_NORTH: "X", E.BTN_WEST: "Y",
            E.BTN_TL: "L", E.BTN_TR: "R", E.BTN_TL2: "ZL", E.BTN_TR2: "ZR",
            E.BTN_SELECT: "-", E.BTN_START: "+", E.BTN_THUMBL: "LS", E.BTN_THUMBR: "RS",
            E.BTN_MODE: "HOME", E.BTN_Z: "CAP"},
}
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

# Padrões dos 4 LEDs (como no Switch): jogador 1 = ●○○○ … jogador 4 = ●●●●
LED_PLAYER = {1: (1, 0, 0, 0), 2: (1, 1, 0, 0), 3: (1, 1, 1, 0), 4: (1, 1, 1, 1)}
LED_WAITING = (1, 0, 0, 1)

SAME_PACKET_US = 1500   # leituras que o kernel recebeu juntas = mesmo pacote
HICCUP_S = 0.05   # normal é ~15 ms entre pacotes; acima disso foi engasgo

BATTERY_LEVELS = {"critical": 1, "low": 2, "normal": 3, "high": 4, "full": 5}
BATTERY_CACHE_S = 2.0


def scan():
    """Controles Nintendo presentes agora: {mac: {"kind", "hid", "main", "imu"}}
    com o caminho /dev/input/eventX de cada parte."""
    found = {}
    try:
        entries = sorted(os.listdir(SYS_INPUT))
    except OSError:
        return found
    for ev in entries:
        if not ev.startswith("event"):
            continue
        dev = SYS_INPUT / ev / "device"
        try:
            if (dev / "id/vendor").read_text().strip() != NINTENDO:
                continue
            kind = KINDS.get((dev / "id/product").read_text().strip())
            name = (dev / "name").read_text().strip()
            mac = (dev / "uniq").read_text().strip().upper()
            hid = os.path.basename(os.path.realpath(dev / "device"))
        except OSError:
            continue
        if not kind or not mac or "Combined" in name or "Virtual" in name:
            continue
        info = found.setdefault(mac, {"kind": kind, "hid": hid, "main": None, "imu": None})
        info["imu" if name.endswith("(IMU)") else "main"] = f"/dev/input/{ev}"
    return found


def _norm_stick(value, info):
    half = (info.maximum - info.minimum) / 2 or 1
    center = (info.maximum + info.minimum) / 2
    return max(-1.0, min(1.0, (value - center) / half))


class Controller:
    """Um Joy-Con ou Pro Controller conectado."""

    def __init__(self, mac, kind, hid, cfg):
        self.mac, self.kind, self.hid, self.cfg = mac, kind, hid, cfg
        self.name = KIND_NAMES[kind]
        self.main = self.imu = None
        self.main_path = self.imu_path = None
        self.no_permission = False
        self.buttons = set()        # nomes ("A", "ZR", "UP"…)
        self.axes = {}              # código ABS → valor cru (botões/analógico)
        self._absinfo = {}
        self._imu_abs = [0, 0, 0, 0, 0, 0]  # accel xyz, gyro xyz (crus)
        self._imu_pending = []
        self._imu_scale = (ACCEL_RES, GYRO_RES)
        self.raw = None             # movimento direto do hidraw (rawimu); None = pelo driver
        self.motion = MotionProcessor(cfg)
        self.player = None          # Player ao qual pertence (players.py cuida)
        self.led_pattern = None
        self.connected_at = None
        self.last_active = None       # último botão/analógico/giro de verdade (desligar parado)
        self.rumbler = None         # vibração (abre o dispositivo pra escrita só quando usar)
        self.direct_rumble = None   # vibração direta pelo hidraw (hdrumble)
        self.battery_warned = 9     # menor nível de bateria já avisado
        self.hiccups = []           # instantes em que os dados atrasaram (> 50 ms)
        self._last_imu_arrival = None
        self._battery = None         # (instante, valor) da última leitura

    # --- abrir/fechar --------------------------------------------------
    def open_parts(self, main_path, imu_path):
        """Abre o que ainda não está aberto. Devolve os dispositivos novos."""
        new = []
        if main_path and self.main is None:
            try:
                self.main = E.InputDevice(main_path)
                self.main_path = main_path
                self.rumbler = Rumbler(main_path)
                if self.cfg["vibracao"]["grave"]:
                    direct = DirectRumble(self.hid, "motor" if self.cfg["vibracao"]["motor"]
                                          else "grave")
                    self.direct_rumble = direct if direct.open() else None
                self.no_permission = False
                for code in (E.ABS_X, E.ABS_Y, E.ABS_RX, E.ABS_RY, ABS_HAT0X, ABS_HAT0Y):
                    try:
                        info = self.main.absinfo(code)
                    except OSError:
                        continue
                    self._absinfo[code] = info
                    self.axes[code] = info.value
                names = BUTTONS[self.kind]
                self.buttons = {names[c] for c in self.main.pressed_keys() if c in names}
                new.append(self.main)
            except PermissionError:
                self.no_permission = True
            except OSError as e:
                log.warning("%s: não abriu %s (%s)", self.name, main_path, e)
        if imu_path and self.imu is None:
            try:
                self.imu = E.InputDevice(imu_path)
                self.imu_path = imu_path
                self._imu_scale = (self.imu.absinfo(E.ABS_X).resolution or ACCEL_RES,
                                   self.imu.absinfo(E.ABS_RX).resolution or GYRO_RES)
                self._imu_abs = [self.imu.absinfo(a).value for a in range(6)]
                try:
                    self.imu.use_monotonic_clock()
                except OSError as e:   # sem isso a hora vem do relógio de parede: serve igual
                    log.debug("%s: relógio monotônico: %s", self.name, e)
                new.append(self.imu)
            except OSError as e:
                log.warning("%s: não abriu %s (%s)", self.name, imu_path, e)
        return new

    def start_raw(self, cal):
        """Passa a ler o movimento direto do controle (rawimu). Devolve o
        leitor novo, ou None (controle não medido, sem permissão…): aí
        continua pelo driver."""
        if self.raw is not None or self.kind not in RAW_KINDS:
            return None
        try:
            self.raw = RawImu(self.hid, self.kind, cal)
        except OSError as e:
            log.info("%s: movimento direto não abriu (%s) — fica pelo driver", self.name, e)
            return None
        self._imu_pending.clear()
        return self.raw

    def close(self):
        for d in (self.main, self.imu, self.raw):
            if d:
                d.close()
        self.main = self.imu = self.raw = None
        if self.rumbler:
            self.rumbler.close()
            self.rumbler = None
        if self.direct_rumble:
            self.direct_rumble.close()
            self.direct_rumble = None

    def rumble(self, strong, weak, length_ms=0):
        """Vibra (0 a 1). Falha calado: vibração é enfeite, não pode derrubar nada.
        Com a vibração direta (hdrumble) o "forte" e o "fraco" viram uma
        força só, como o motor do Wii Remote."""
        if self.direct_rumble is not None:
            try:
                self.direct_rumble.play(max(strong, weak), length_ms)
                return True
            except OSError as e:
                log.debug("%s: vibração direta falhou (%s) — volto pra do driver", self.name, e)
                self.direct_rumble.close()
                self.direct_rumble = None
        if self.rumbler is None:
            return False
        try:
            if strong <= 0 and weak <= 0:
                self.rumbler.stop()
            else:
                self.rumbler.play(strong, weak, length_ms)
            return True
        except OSError as e:
            log.debug("%s: vibração falhou (%s)", self.name, e)
            return False

    @property
    def complete(self):
        return self.main is not None and self.imu is not None

    # --- leitura -------------------------------------------------------
    def read_main(self):
        """Lê botões/analógico. Devolve True se algum botão mudou.
        OSError(ENODEV) se o controle saiu."""
        names = BUTTONS[self.kind]
        changed = False
        for typ, code, value in self.main.read_events():
            if typ == E.EV_KEY and code in names:
                (self.buttons.add if value else self.buttons.discard)(names[code])
                changed = True
            elif typ == E.EV_ABS:
                self.axes[code] = value
        return changed

    def _imu_arrived(self, now):
        if self._last_imu_arrival is not None and now - self._last_imu_arrival > HICCUP_S:
            self.hiccups.append(now)
            if len(self.hiccups) > 200:
                del self.hiccups[:100]
        self._last_imu_arrival = now

    def read_raw(self, now):
        """Lê o movimento direto do controle (rawimu). O pacote diz quanto se
        perdeu antes dele."""
        packets = self.raw.read(now)
        if not packets:
            return
        self._imu_arrived(now)
        samples, gaps = [], []
        ts = int(now * 1e6)
        for lost, reads in packets:
            for k, (accel, gyro) in enumerate(reads):
                samples.append((ts, accel, gyro))
                gaps.append(lost if k == 0 else 0.0)
        self.motion.add_samples(samples, now, gaps)

    def read_imu(self, now):
        """Lê o movimento e manda as amostras completas pro processador."""
        if self.raw is not None:
            self.imu.read_events()     # o movimento vem do hidraw: só esvazia a fila
            return
        self._imu_arrived(now)
        ax_res, gy_res = self._imu_scale
        samples = []
        # A hora de cada amostra é a de chegada no kernel (as 3 de um pacote
        # chegam juntas). O MSC_TIMESTAMP do hid-nintendo anda 2× mais rápido
        # que o relógio (30 ms por pacote de 15) e não pula quando pacotes
        # se perdem — medido em 2026-10-10 —, então não serve pra tempo.
        pend = self._imu_pending
        for t_us, typ, code, value in self.imu.read_events_timed():
            if typ == E.EV_ABS and code <= E.ABS_RZ:
                self._imu_abs[code] = value
            elif typ == E.EV_SYN and code == E.SYN_REPORT:
                a = self._imu_abs
                pend.append((t_us,
                             (a[0] / ax_res, a[1] / ax_res, a[2] / ax_res),
                             (a[3] / gy_res, a[4] / gy_res, a[5] / gy_res)))
        # O Joy-Con põe as 3 leituras do pacote da mais NOVA pra mais velha e
        # o hid-nintendo repassa nessa ordem (o SDL também trata a 1ª como a
        # mais nova). Conferido em 3 gravações de 2026-10-11: desvirando, o
        # sinal fica 20–65× mais liso — e a "mais nova" mandada pro Dolphin
        # era na verdade 10 ms mais velha.
        i = 0
        while i < len(pend):
            group = [pend[i]]
            while (len(group) < 3 and i + len(group) < len(pend)
                   and abs(pend[i + len(group)][0] - pend[i][0]) < SAME_PACKET_US):
                group.append(pend[i + len(group)])
            if len(group) < 3 and i + len(group) == len(pend):
                break                     # pacote chegando pela metade: espera o resto
            samples.extend(reversed(group))
            i += len(group)
        del pend[:i]
        self.motion.add_samples(samples, now)

    def hiccups_per_min(self, now, window=20.0):
        recent = [t for t in self.hiccups if now - t <= window]
        return len(recent) * 60 / window

    def stick(self, which="main"):
        """Analógico normalizado: (x, y) de −1 a 1, y positivo = pra cima."""
        if self.kind == "R" or which == "right":
            cx, cy = E.ABS_RX, E.ABS_RY
        else:
            cx, cy = E.ABS_X, E.ABS_Y
        if cx not in self._absinfo:
            return 0.0, 0.0
        x = _norm_stick(self.axes.get(cx, 0), self._absinfo[cx])
        y = -_norm_stick(self.axes.get(cy, 0), self._absinfo[cy])
        return x, y

    def pressed(self, name):
        if name in self.buttons:
            return True
        if self.kind == "PRO" and name in ("UP", "DOWN", "LEFT", "RIGHT"):
            hx, hy = self.axes.get(ABS_HAT0X, 0), self.axes.get(ABS_HAT0Y, 0)
            return {"UP": hy < 0, "DOWN": hy > 0, "LEFT": hx < 0, "RIGHT": hx > 0}[name]
        return False

    # --- /sys: bateria e LEDs -----------------------------------------
    def battery(self):
        """(nível 0–5, carregando?) — 0 = não sei. Guardado por BATTERY_CACHE_S:
        cada pacote pro Dolphin leva a bateria (centenas por segundo)."""
        now = time.monotonic()
        if self._battery and now - self._battery[0] < BATTERY_CACHE_S:
            return self._battery[1]
        base = SYS_POWER / f"nintendo_switch_controller_battery_{self.hid}"
        try:
            level = (base / "capacity_level").read_text().strip().lower()
            status = (base / "status").read_text().strip().lower()
            value = BATTERY_LEVELS.get(level, 0), status == "charging"
        except OSError:
            value = 0, False
        self._battery = (now, value)
        return value

    def set_leds(self, pattern):
        """Acende os LEDs de jogador. Lento (o driver espera a resposta do
        controle): chamar fora do loop principal. Devolve False sem permissão."""
        for i, on in enumerate(pattern, start=1):
            path = SYS_LEDS / f"{self.hid}:green:player-{i}" / "brightness"
            try:
                path.write_text("1" if on else "0")
            except PermissionError:
                return False
            except OSError as e:
                if e.errno != errno.ENODEV:
                    log.debug("%s: LED %d: %s", self.name, i, e)
                return True
        self.led_pattern = pattern
        return True
