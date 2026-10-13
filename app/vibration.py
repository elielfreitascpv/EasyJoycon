"""Vibração dos jogos pra todos os jogadores.

O DSU não tem caminho de volta (o Dolphin não manda vibração por ele). Então
o servidor cria, por jogador, um "controle virtual de vibração" no Linux
(uinput): "EasyJoycon J1 Vibracao" … "J4". O Wii Remote N do Dolphin manda
a vibração pro dispositivo virtual N, e o servidor repassa pro Joy-Con de
quem é o jogador N agora — mesmo se trocarem de controle no meio.

Como o Dolphin usa (evdev.cpp, Effect::UpdateEffect): liga = sobe o efeito
(EVIOCSFF) + "play"; muda a força = sobe de novo; desliga = apaga o efeito
(EVIOCRMFF). No uinput isso chega como pedidos UI_FF_UPLOAD / UI_FF_ERASE e
eventos EV_FF.

O Dolphin só lista dispositivo com ≥ 2 eixos ou ≥ 8 botões, por isso o
virtual tem eixos (ABS_X/Y/Z = "Axis 0/1/2" no Dolphin). Eles também servem
de sinal pro Dolphin com ele aberto (dolphin.mapping_universal): eixo X em
1 = Nunchuk conectado (o "Extension" do Wii Remote é calculado a cada
atualização a partir dele: Attachments.cpp + WiimoteEmu.cpp,
HandleExtensionSwap); eixo Y em 1 = Wii Remote deitado ("Options/Sideways
Wiimote"); eixo Z em 1 = controle completo (par ou Pro), que liga o
controle de GameCube (dolphin.mapping_gc).
"""
import fcntl
import logging
import os
import struct

from .evdev import ABS_X, ABS_Y, ABS_Z, EV_ABS, EVENT
from .ff import EV_FF, FF_EFFECT_SIZE, FF_RUMBLE, unpack_effect

log = logging.getLogger(__name__)

UINPUT = "/dev/uinput"
NAME = "EasyJoycon J{n} Vibracao"   # sem acento: vai em arquivo .ini do Dolphin
VENDOR, PRODUCT = 0x1209, 0xE501    # pid.codes (teste); longe da Nintendo (057e)


def _uioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | nr


_W, _R = 1, 2
UI_DEV_CREATE = _uioc(0, 1, 0)
UI_DEV_DESTROY = _uioc(0, 2, 0)
UI_DEV_SETUP = _uioc(_W, 3, 92)            # input_id (8) + nome (80) + ff_effects_max (4)
UI_ABS_SETUP = _uioc(_W, 4, 28)            # code u16 + 2 + input_absinfo (24)
UI_SET_EVBIT = _uioc(_W, 100, 4)
UI_SET_ABSBIT = _uioc(_W, 103, 4)
UI_SET_FFBIT = _uioc(_W, 107, 4)
FF_UPLOAD_SIZE = 8 + 2 * FF_EFFECT_SIZE    # request_id, retval, effect, old
UI_BEGIN_FF_UPLOAD = _uioc(_R | _W, 200, FF_UPLOAD_SIZE)
UI_END_FF_UPLOAD = _uioc(_W, 201, FF_UPLOAD_SIZE)
UI_BEGIN_FF_ERASE = _uioc(_R | _W, 202, 12)
UI_END_FF_ERASE = _uioc(_W, 203, 12)
EV_UINPUT, UI_FF_UPLOAD, UI_FF_ERASE = 0x0101, 1, 2
EV_SYN = 0
SIGNAL_AXES = (ABS_X, ABS_Y, ABS_Z)


class VirtualRumble:
    """Dispositivo virtual de vibração de um jogador.
    on_rumble(número, forte 0–1, fraca 0–1, ms) — forte=fraca=0 = parar."""

    def __init__(self, number, on_rumble):
        self.number = number
        self.name = NAME.format(n=number)
        self.on_rumble = on_rumble
        self.effects = {}        # id → (forte, fraca, ms)
        self.playing = set()
        self.fd = None
        self.signals = (False, False, False)   # (Nunchuk, deitado, controle completo)

    def create(self):
        fd = os.open(UINPUT, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
            for axis in SIGNAL_AXES:
                fcntl.ioctl(fd, UI_SET_ABSBIT, axis)
                fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack("<HH6i", axis, 0, 0, -1, 1, 0, 0, 0))
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_FF)
            fcntl.ioctl(fd, UI_SET_FFBIT, FF_RUMBLE)
            name = self.name.encode()[:79]
            fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("<HHHH80sI", 0x06, VENDOR, PRODUCT, 1,
                                                       name, 8))
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return fd

    def set_signals(self, nunchuk, sideways, full=False):
        """Avisa o Dolphin (pelos eixos) se tem Nunchuk, se está deitado e
        se é controle completo (GameCube). Só escreve quando muda."""
        want = (bool(nunchuk), bool(sideways), bool(full))
        if self.fd is None or want == self.signals:
            return
        data = b"".join(EVENT.pack(0, 0, EV_ABS, axis, int(on))
                        for axis, on in zip(SIGNAL_AXES, want, strict=True))
        data += EVENT.pack(0, 0, EV_SYN, 0, 0)
        try:
            os.write(self.fd, data)
        except OSError as e:
            log.debug("%s: sinal não foi (%s)", self.name, e)
            return
        self.signals = want

    def destroy(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None

    def _upload(self, request_id):
        buf = bytearray(FF_UPLOAD_SIZE)
        struct.pack_into("<I", buf, 0, request_id)
        fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, buf)
        typ, eid, length, strong, weak = unpack_effect(buf[8:8 + FF_EFFECT_SIZE])
        ok = typ == FF_RUMBLE
        if ok:
            self.effects[eid] = (strong / 0xFFFF, weak / 0xFFFF, length)
            if eid in self.playing:               # mudou a força com ele tocando
                self.on_rumble(self.number, *self.effects[eid])
        struct.pack_into("<i", buf, 4, 0 if ok else -22)   # -EINVAL pra outro tipo
        fcntl.ioctl(self.fd, UI_END_FF_UPLOAD, buf)

    def _erase(self, request_id):
        buf = bytearray(struct.pack("<IiI", request_id, 0, 0))
        fcntl.ioctl(self.fd, UI_BEGIN_FF_ERASE, buf)
        eid = struct.unpack_from("<I", buf, 8)[0]
        self.effects.pop(eid, None)
        if eid in self.playing:
            self.playing.discard(eid)
            self._update()
        struct.pack_into("<i", buf, 4, 0)
        fcntl.ioctl(self.fd, UI_END_FF_ERASE, buf)

    def _update(self):
        if not self.playing:
            self.on_rumble(self.number, 0.0, 0.0, 0)
            return
        strong = max(self.effects.get(e, (0, 0, 0))[0] for e in self.playing)
        weak = max(self.effects.get(e, (0, 0, 0))[1] for e in self.playing)
        length = max(self.effects.get(e, (0, 0, 0))[2] for e in self.playing)
        self.on_rumble(self.number, strong, weak, length)

    def handle(self):
        """Chamado quando o uinput tem algo pra ler."""
        try:
            data = os.read(self.fd, EVENT.size * 32)
        except BlockingIOError:
            return
        for _, _, typ, code, value in EVENT.iter_unpack(data[: len(data) - len(data) % EVENT.size]):
            try:
                if typ == EV_UINPUT and code == UI_FF_UPLOAD:
                    self._upload(value)
                elif typ == EV_UINPUT and code == UI_FF_ERASE:
                    self._erase(value)
                elif typ == EV_FF and code in self.effects:
                    (self.playing.add if value else self.playing.discard)(code)
                    self._update()
            except OSError as e:
                log.debug("%s: %s", self.name, e)
