"""Vibração ("force feedback") pelo evdev do Linux, sem biblioteca: monta o
struct ff_effect e fala com o kernel por ioctl.

struct ff_effect (x86_64, 48 bytes): type u16, id s16, direction u16,
trigger (button u16, interval u16), replay (length u16, delay u16), 2 bytes
de alinhamento e a união de 32 bytes (no rumble: strong u16, weak u16).
"""
import fcntl
import os
import struct

from .evdev import EVENT, _IOC_WRITE, _ioc

EV_FF = 0x15
FF_RUMBLE = 0x50
FF_EFFECT_SIZE = 48
_HEAD = struct.Struct("<HhHHHHH")      # 14 bytes antes do alinhamento
_RUMBLE = struct.Struct("<HH")
EVIOCSFF = _ioc(_IOC_WRITE, 0x80, FF_EFFECT_SIZE)
EVIOCRMFF = _ioc(_IOC_WRITE, 0x81, 4)
MAX = 0xFFFF


def pack_rumble(effect_id, strong, weak, length_ms):
    buf = bytearray(FF_EFFECT_SIZE)
    _HEAD.pack_into(buf, 0, FF_RUMBLE, effect_id, 0, 0, 0, min(length_ms, 0xFFFF), 0)
    _RUMBLE.pack_into(buf, 16, strong, weak)
    return buf


def unpack_effect(buf):
    """ff_effect cru → (tipo, id, duração ms, strong, weak)."""
    typ, eid, _, _, _, length, _ = _HEAD.unpack_from(buf, 0)
    strong, weak = _RUMBLE.unpack_from(buf, 16)
    return typ, eid, length, strong, weak


class Rumbler:
    """Faz um /dev/input/eventX vibrar (o do controle, não o do IMU)."""

    def __init__(self, path):
        self.path = path
        self.fd = None
        self.effect_id = -1

    def _open(self):
        if self.fd is None:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)

    def play(self, strong, weak, length_ms=0):
        """strong/weak de 0 a 1. length_ms 0 = até mandar parar."""
        self._open()
        buf = pack_rumble(self.effect_id, int(max(0, min(1, strong)) * MAX),
                          int(max(0, min(1, weak)) * MAX), length_ms)
        fcntl.ioctl(self.fd, EVIOCSFF, buf)
        self.effect_id = struct.unpack_from("<h", buf, 2)[0]
        os.write(self.fd, EVENT.pack(0, 0, EV_FF, self.effect_id, 1))

    def stop(self):
        if self.fd is not None and self.effect_id >= 0:
            os.write(self.fd, EVENT.pack(0, 0, EV_FF, self.effect_id, 0))

    def close(self):
        if self.fd is not None:
            try:
                if self.effect_id >= 0:
                    fcntl.ioctl(self.fd, EVIOCRMFF, self.effect_id)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
            self.effect_id = -1

