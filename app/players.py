"""Jogadores, do jeito do Switch:

- Controle novo fica "esperando".
- Joy-Con esquerdo segurando L (ou ZL) + direito segurando R (ou ZR) ao
  mesmo tempo → os dois viram um jogador (Wii Remote + Nunchuk).
- Um Joy-Con sozinho com SL + SR → jogador com o Joy-Con deitado.
- Um Joy-Con sozinho segurando os dois botões de cima (R + ZR ou L + ZL) →
  Wii Remote em pé, sem Nunchuk. Clicar no cartão alterna deitado/em pé.
- Pro Controller com L + R → jogador.
- Quem reconecta volta pro mesmo jogador sozinho. Fechou e abriu: o número
  segue a ordem de conexão (como no Switch); o modo e o parceiro do par
  ficam lembrados por MAC.

Cada jogador vira um "pad" DSU (botões + analógicos + movimento). O
movimento do Joy-Con esquerdo vai num segundo pad (o do Nunchuk).
"""
import json
import logging
import time
from dataclasses import dataclass, field

from .config import DATA_DIR
from .controllers import LED_PLAYER, LED_WAITING
from .motion import SIDEWAYS_L, SIDEWAYS_R, MotionOut
from .i18n import _

log = logging.getLogger(__name__)

MEMORY_FILE = DATA_DIR / "jogadores.json"
MAX_PLAYERS = 4

PAIR, SOLO, UPRIGHT, PRO = "par", "sozinho", "empe", "pro"
STICK_DPAD = 0.5   # deitado: analógico mais longe que isso = seta apertada
MODES = (PAIR, SOLO, UPRIGHT, PRO)
MODE_NAMES = {PAIR: "Wii Remote + Nunchuk", SOLO: "deitado", UPRIGHT: "Wii Remote em pé",
              PRO: "Pro Controller"}


@dataclass
class PadState:
    """Um pad DSU, já no formato "PlayStation" que o protocolo usa."""
    connected: bool = False
    buttons: dict = field(default_factory=dict)  # circle, cross, …, up, l3…
    lstick: tuple = (0.0, 0.0)
    rstick: tuple = (0.0, 0.0)
    motion: MotionOut = field(default_factory=MotionOut)
    battery: int = 0
    charging: bool = False


@dataclass
class Player:
    number: int
    mode: str
    macs: dict = field(default_factory=dict)   # papel ("L", "R", "PRO") → MAC
    members: dict = field(default_factory=dict)  # papel → Controller conectado
    nunchuk: bool = True     # par/Pro: Nunchuk "conectado" (o jogo pode pedir pra tirar)

    @property
    def online(self):
        return bool(self.members)

    @property
    def complete(self):
        return len(self.members) == len(self.macs)


def _holding_shoulder(ctrl):
    """Esquerdo segurando L/ZL, direito segurando R/ZR."""
    if ctrl.kind == "L":
        return ctrl.pressed("L") or ctrl.pressed("ZL")
    return ctrl.pressed("R") or ctrl.pressed("ZR")


def _both_shoulders(ctrl):
    """Os dois botões de cima do mesmo Joy-Con: L+ZL ou R+ZR."""
    if ctrl.kind == "L":
        return ctrl.pressed("L") and ctrl.pressed("ZL")
    return ctrl.pressed("R") and ctrl.pressed("ZR")


def _upright(ctrl):
    """Joy-Con sozinho em pé = Wii Remote: mesmos campos do lado direito do
    modo par (o mapeamento do Dolphin é o mesmo, sem Nunchuk). No esquerdo,
    as setas fazem o papel de A/B/X/Y."""
    p = ctrl.pressed
    if ctrl.kind == "R":
        return {"circle": p("A"), "cross": p("B"), "triangle": p("X"), "square": p("Y"),
                "r1": p("R"), "r2": p("ZR"), "options": p("+"), "ps": p("HOME"),
                "r3": p("RS"), "share": p("SL")}
    return {"circle": p("RIGHT"), "cross": p("DOWN"), "triangle": p("UP"), "square": p("LEFT"),
            "r1": p("L"), "r2": p("ZL"), "options": p("-"), "ps": p("CAP"),
            "r3": p("LS"), "share": p("SL")}


def _rotate_solo(ctrl):
    """Joy-Con deitado (Wii Remote deitado, estilo NES): botões e analógico
    girados pra posição física — direito no sentido horário, esquerdo no
    anti-horário — e mandados nos MESMOS campos do par (mapeamento universal
    do Dolphin): A = circle, B = cross, 1 = triangle, 2 = square, − = share,
    direcional = setas, Recenter = r3. O Dolphin gira o resto ("deitado")."""
    sx, sy = ctrl.stick()
    p = ctrl.pressed
    if ctrl.kind == "R":
        top, bottom, left, right = p("Y"), p("A"), p("B"), p("X")
        dx, dy = sy, -sx
        plus, home, press = p("+"), p("HOME"), p("RS")
    else:
        top, bottom, left, right = p("RIGHT"), p("LEFT"), p("UP"), p("DOWN")
        dx, dy = -sy, sx
        plus, home, press = p("-"), p("CAP"), p("LS")
    return {"circle": top, "cross": left or p("SR"), "triangle": bottom, "square": right,
            "share": p("SL"), "options": plus, "ps": home, "r3": press,
            "up": dy > STICK_DPAD, "down": dy < -STICK_DPAD,
            "left": dx < -STICK_DPAD, "right": dx > STICK_DPAD}


def signals(player):
    """(Nunchuk conectado, deitado, controle completo) desse jogador, pro
    Dolphin (vibration). Completo = par ou Pro: só esses viram controle de
    GameCube (um Joy-Con sozinho não tem C-Stick nem botões suficientes)."""
    if player is None or not player.online:
        return False, False, False
    full = player.mode in (PAIR, PRO)
    return full and player.nunchuk, player.mode == SOLO, full


def parked_pad(now):
    """Wii Remote sem controle: parado apontando pro teto (gravidade ao longo
    da frente = DSU (0, 0, 1) em pé e deitado). O ponteiro some da tela,
    como o de um Wii Remote desligado — senão fica parado no meio."""
    pad = PadState(connected=True)
    pad.motion = MotionOut((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), int(now * 1e6))
    return pad


def build_pads(player, now):
    """(pad principal, pad do nunchuk) desse jogador."""
    main, nun = PadState(), PadState()
    if player is None:
        return main, nun
    if not player.online:
        # Jogador esperando o controle voltar: o Wii Remote dele continua no
        # Dolphin e, sem dados, o ponteiro ficava parado no meio da tela.
        # Mandamos ele "apontando pro teto" — o ponteiro some, como um Wii
        # Remote desligado. Quando o controle volta, segue normal.
        return parked_pad(now), nun
    m = player.members
    main.connected = True
    levels = [c.battery() for c in m.values()]
    known = [lv for lv in levels if lv[0]]
    if known:
        main.battery = min(lv[0] for lv in known)
        main.charging = any(lv[1] for lv in known)

    if player.mode == SOLO:
        ctrl = next(iter(m.values()))
        main.buttons = _rotate_solo(ctrl)
        _hold_recenter(main, ctrl, "r3", now)
        main.motion = ctrl.motion.output(now, SIDEWAYS_R if ctrl.kind == "R" else SIDEWAYS_L)
        return main, nun

    if player.mode == UPRIGHT:
        ctrl = next(iter(m.values()))
        main.buttons = _upright(ctrl)
        _hold_recenter(main, ctrl, "r3", now)
        main.rstick = ctrl.stick()
        main.motion = ctrl.motion.output(now)
        return main, nun

    if player.mode == PRO:
        c = m["PRO"]
        left = right = c
    else:
        left, right = m.get("L"), m.get("R")

    def lp(name):
        return bool(left and left.pressed(name))

    def rp(name):
        return bool(right and right.pressed(name))

    main.buttons = {
        "circle": rp("A"), "cross": rp("B"), "triangle": rp("X"), "square": rp("Y"),
        "r1": rp("R"), "r2": rp("ZR"), "l1": lp("L"), "l2": lp("ZL"),
        "up": lp("UP"), "down": lp("DOWN"), "left": lp("LEFT"), "right": lp("RIGHT"),
        "share": lp("-"), "options": rp("+"), "ps": rp("HOME"), "touch": lp("CAP"),
        "l3": lp("LS"), "r3": rp("RS"),
    }
    if player.mode == PRO:
        main.lstick, main.rstick = c.stick("left"), c.stick("right")
        main.motion = c.motion.output(now)
        _hold_recenter(main, c, "r3", now)
        # o Nunchuk do Pro se mexe junto (mapeamento universal: sempre lê o
        # movimento do Nunchuk do pad do Nunchuk)
        nun.connected = True
        nun.motion = main.motion
        nun.battery, nun.charging = main.battery, main.charging
        return main, nun
    if left:
        main.lstick = left.stick()
        nun.connected = True
        nun.motion = left.motion.output(now)
        nun.battery, nun.charging = main.battery, main.charging
    if right:
        main.rstick = right.stick()
        main.motion = right.motion.output(now)
        _hold_recenter(main, right, "r3", now)
    return main, nun


def _hold_recenter(pad, ctrl, button, now):
    """Pegada nova: continua "apertando" o Recenter do Dolphin um instante
    (motion.recenter explica por quê)."""
    if ctrl.motion.recentering(now):
        pad.buttons[button] = True


class PlayerManager:
    def __init__(self, cfg, led_setter=None):
        self.cfg = cfg
        self.players = {}            # número → Player
        self.waiting = {}            # mac → Controller sem jogador
        self._waiting_since = {}
        self.memory = self._load_memory() if cfg["jogadores"]["lembrar"] else {}
        self._led_setter = led_setter or (lambda ctrl, pattern: None)
        self.events = []             # mensagens pra tela
        self.on_event = None         # on_event(tipo, mensagem): notificações
        self.on_assigned = None      # on_assigned(controle): virou jogador (vibra)

    # --- memória -------------------------------------------------------
    def _load_memory(self):
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        mem = {}
        for mac, v in data.items():
            if (isinstance(v, dict) and v.get("modo") in MODES
                    and isinstance(v.get("jogador"), int) and 1 <= v["jogador"] <= MAX_PLAYERS):
                mem[mac] = v
        return mem

    def _save_memory(self):
        if not self.cfg["jogadores"]["lembrar"]:
            return
        mem = {}
        for p in self.players.values():
            for mac in p.macs.values():
                mem[mac] = {"jogador": p.number, "modo": p.mode}
        self.memory = mem
        tmp = MEMORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(mem, indent=1), encoding="utf-8")
        tmp.replace(MEMORY_FILE)

    def _say(self, msg, kind="info"):
        """kind: info, conectou, desconectou, jogador, aviso."""
        log.info(msg)
        self.events.append((time.monotonic(), msg))
        del self.events[:-6]
        if self.on_event:
            self.on_event(kind, msg)

    # --- controles entrando e saindo ----------------------------------
    def controller_connected(self, ctrl, now):
        for p in self.players.values():           # já era de um jogador desta sessão
            for role, mac in p.macs.items():
                if mac == ctrl.mac:
                    self._attach(p, role, ctrl)
                    self._say(_("{name} voltou pro jogador {n}", name=ctrl.name, n=p.number),
                              "conectou")
                    return
        mem = self.memory.get(ctrl.mac)
        n = mem and self._free_number()
        if n:
            # número pela ordem de chegada; o parceiro lembrado do par fica
            # reservado (quando chegar, cai no laço lá de cima)
            mode, role = mem["modo"], ctrl.kind
            p = self.players[n] = Player(n, mode)
            p.macs[role] = ctrl.mac
            for mac, v in self.memory.items():
                if mac != ctrl.mac and v["jogador"] == mem["jogador"] and v["modo"] == mode == PAIR:
                    p.macs.setdefault("L" if role == "R" else "R", mac)
            self._attach(p, role, ctrl)
            self._say(_("{name} → jogador {n} ({mode})", name=ctrl.name, n=n,
                         mode=_(MODE_NAMES[mode])), "conectou")
            return
        self.waiting[ctrl.mac] = ctrl
        self._waiting_since[ctrl.mac] = now
        self._led_setter(ctrl, LED_WAITING)
        self._say(_("{name} conectado — esperando (L+R ou SL+SR)", name=ctrl.name), "conectou")

    def controller_gone(self, ctrl):
        was_waiting = self.waiting.pop(ctrl.mac, None) is not None
        self._waiting_since.pop(ctrl.mac, None)
        p = ctrl.player
        if p:
            for role, c in list(p.members.items()):
                if c is ctrl:
                    del p.members[role]
            ctrl.player = None
            self._say(_("{name} do jogador {n} desconectou", name=ctrl.name, n=p.number),
                      "desconectou")
        elif was_waiting:
            self._say(_("{name} desconectou", name=ctrl.name), "desconectou")

    def _attach(self, player, role, ctrl):
        player.members[role] = ctrl
        ctrl.player = player
        self.waiting.pop(ctrl.mac, None)
        self._waiting_since.pop(ctrl.mac, None)
        self._led_setter(ctrl, LED_PLAYER[player.number])
        if self.on_assigned:
            self.on_assigned(ctrl)

    def _free_number(self):
        for n in range(1, MAX_PLAYERS + 1):
            if n not in self.players:
                return n
        for n in range(1, MAX_PLAYERS + 1):   # todos reservados: pega um vazio
            if not self.players[n].online:
                return n
        return None

    def _new_player(self, mode, ctrls):
        n = self._free_number()
        if n is None:
            self._say(_("já tem 4 jogadores — aperte R pra recomeçar"))
            return
        old = self.players.get(n)
        if old:
            self._say(_("jogador {n} estava desconectado: lugar reaproveitado", n=n))
        p = self.players[n] = Player(n, mode)
        for c in ctrls:
            p.macs[c.kind] = c.mac
            self._attach(p, c.kind, c)
        self._save_memory()
        nomes = " + ".join(c.name for c in ctrls)
        self._say(_("jogador {n}: {names} ({mode})", n=n, names=nomes, mode=_(MODE_NAMES[mode])),
                  "jogador")

    # --- gestos -------------------------------------------------------
    def _single(self, ctrl):
        """Joy-Con que é, sozinho, um jogador deitado ou em pé."""
        p = ctrl.player
        return (p is not None and p.mode in (SOLO, UPRIGHT)
                and list(p.macs.values()) == [ctrl.mac])

    def buttons_changed(self, ctrl, now, regroup=False):
        """regroup: procurando controles (a "tela de controles" do Switch) —
        L + R também junta Joy-Cons que já são jogadores sozinhos. Fora dela
        não, pra dois jogadores apertando L e R no jogo não virarem um só."""
        if ctrl.mac not in self.waiting:
            if regroup and self._single(ctrl):
                self._try_pair(ctrl, regroup)
            return
        p = ctrl.pressed
        if ctrl.kind == "PRO":
            if (p("L") or p("ZL")) and (p("R") or p("ZR")):
                self._new_player(PRO, [ctrl])
            return
        if p("SL") and p("SR"):
            self._new_player(SOLO, [ctrl])
            return
        if _both_shoulders(ctrl):
            self._new_player(UPRIGHT, [ctrl])
            return
        self._try_pair(ctrl, regroup)

    def _try_pair(self, ctrl, regroup):
        if not _holding_shoulder(ctrl):
            return
        others = list(self.waiting.values())
        if regroup:
            others += [c for p in self.players.values() for c in p.members.values()
                       if self._single(c)]
        for other in others:
            if (other is not ctrl and other.kind in ("L", "R") and other.kind != ctrl.kind
                    and _holding_shoulder(other)):
                self._pair_up(ctrl, other)
                return

    def _pair_up(self, a, b):
        left, right = (a, b) if a.kind == "L" else (b, a)
        for c in (left, right):                   # deixam de ser jogadores sozinhos
            if c.player is not None:
                del self.players[c.player.number]
                c.player = None
        self._new_player(PAIR, [right, left])

    def join_single(self, number):
        """Botão "Juntar" no cartão: o Joy-Con sozinho desse jogador vira par
        com o outro Joy-Con sozinho (o de número menor, se tiver mais de um)."""
        p = self.players.get(number)
        me = next(iter(p.members.values()), None) if p else None
        if me is None or me.kind not in ("L", "R") or not self._single(me):
            return
        for n in sorted(self.players):
            for c in self.players[n].members.values():
                if c.kind in ("L", "R") and c.kind != me.kind and self._single(c):
                    self._pair_up(me, c)
                    return
        other = _("direito") if me.kind == "L" else _("esquerdo")
        self._say(_("não tem Joy-Con {side} sozinho pra juntar com o jogador {n}", side=other,
                    n=number), "aviso")

    def split_pair(self, number):
        """Botão "Separar" no cartão do par: o direito fica com o número do
        jogador e o esquerdo vira o próximo jogador livre, os dois em pé."""
        p = self.players.get(number)
        if p is None or p.mode != PAIR:
            return
        left = p.members.get("L")
        right_mac = p.macs.get("R")
        p.mode = UPRIGHT
        p.macs = {"R": right_mac} if right_mac else {}
        p.members.pop("L", None)
        if not p.macs:                    # só tinha o esquerdo: ele fica com o número
            p.macs = {"L": left.mac} if left else {}
            if left:
                p.members["L"] = left
            left = None
        if left is not None:
            left.player = None
            n = self._free_number()
            if n is None:
                self.waiting[left.mac] = left
                self._waiting_since[left.mac] = time.monotonic()
                self._led_setter(left, LED_WAITING)
            else:
                lp = self.players[n] = Player(n, UPRIGHT)
                lp.macs["L"] = left.mac
                self._attach(lp, "L", left)
        self._save_memory()
        self._say(_("jogador {n} separado: cada Joy-Con é um Wii Remote em pé", n=number),
                  "jogador")

    def toggle_nunchuk(self, number):
        """Tira/põe o Nunchuk do Wii Remote do jogador (par ou Pro) — o jogo
        vê como desconectar o fio, com ele rodando."""
        p = self.players.get(number)
        if p is None or p.mode not in (PAIR, PRO):
            return
        p.nunchuk = not p.nunchuk
        self._say(f"jogador {number}: Nunchuk {'conectado' if p.nunchuk else 'desconectado'}",
                  "jogador")

    def toggle_single_mode(self, number):
        """Clique no cartão: Joy-Con sozinho alterna deitado ↔ em pé."""
        p = self.players.get(number)
        if p is None or p.mode not in (SOLO, UPRIGHT):
            return
        p.mode = UPRIGHT if p.mode == SOLO else SOLO
        self._save_memory()
        self._say(_("jogador {n}: agora {mode}", n=number, mode=_(MODE_NAMES[p.mode])), "jogador")

    def tick(self, now):
        """Junta sozinho um par L+R quando é óbvio (ninguém jogando ainda)."""
        wait_s = self.cfg["jogadores"]["juntar_sozinho_segundos"]
        if not wait_s or any(p.online for p in self.players.values()):
            return
        ls = [c for c in self.waiting.values() if c.kind == "L"]
        rs = [c for c in self.waiting.values() if c.kind == "R"]
        pros = [c for c in self.waiting.values() if c.kind == "PRO"]
        if len(ls) == 1 and len(rs) == 1 and not pros:
            since = max(self._waiting_since[ls[0].mac], self._waiting_since[rs[0].mac])
            if now - since >= wait_s:
                self._new_player(PAIR, [rs[0], ls[0]])
        elif len(pros) == 1 and not ls and not rs:
            if now - self._waiting_since[pros[0].mac] >= wait_s:
                self._new_player(PRO, [pros[0]])

    def reset(self, connected):
        """Todo mundo volta a esperar (tecla R)."""
        self.players.clear()
        now = time.monotonic()
        for c in connected:
            c.player = None
            self.waiting[c.mac] = c
            self._waiting_since[c.mac] = now
            self._led_setter(c, LED_WAITING)
        self.memory = {}
        if self.cfg["jogadores"]["lembrar"]:
            self._save_memory()
        self._say(_("jogadores apagados — aperte L+R / SL+SR de novo"))
