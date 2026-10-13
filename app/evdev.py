"""Leitura dos dispositivos de entrada do Linux (/dev/input/eventX) sem
biblioteca externa: só os ioctl que o servidor usa e o formato dos eventos.
"""
import fcntl
import os
import struct
import time
from dataclasses import dataclass

# struct input_event (64 bits): timeval (2 × long), type, code (u16), value (s32)
EVENT = struct.Struct("llHHi")

EV_SYN, EV_KEY, EV_ABS, EV_MSC = 0x00, 0x01, 0x03, 0x04
SYN_REPORT = 0
MSC_TIMESTAMP = 0x05
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05

# Botões que o hid-nintendo usa (linux/input-event-codes.h)
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_Z = 0x135
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE = 0x13A, 0x13B, 0x13C
BTN_THUMBL, BTN_THUMBR = 0x13D, 0x13E
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223

_IOC_READ, _IOC_WRITE = 2, 1


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


ABSINFO = struct.Struct("6i")  # value, minimum, maximum, fuzz, flat, resolution


@dataclass
class AbsInfo:
    value: int
    minimum: int
    maximum: int
    fuzz: int
    flat: int
    resolution: int


def _ioctl_str(fd, nr):
    buf = bytearray(256)
    n = fcntl.ioctl(fd, _ioc(_IOC_READ, nr, len(buf)), buf)
    return bytes(buf[: max(n - 1, 0)]).decode(errors="replace")


def get_absinfo(fd, axis):
    buf = bytearray(ABSINFO.size)
    fcntl.ioctl(fd, _ioc(_IOC_READ, 0x40 + axis, ABSINFO.size), buf)
    return AbsInfo(*ABSINFO.unpack(buf))


def get_pressed_keys(fd):
    """Botões apertados agora (pra não perder um botão já segurado ao abrir)."""
    buf = bytearray(96)  # KEY_MAX = 0x2ff → 768 bits
    fcntl.ioctl(fd, _ioc(_IOC_READ, 0x18, len(buf)), buf)
    return {i for i in range(len(buf) * 8) if buf[i // 8] >> (i % 8) & 1}


class InputDevice:
    """Um /dev/input/eventX aberto sem bloquear."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            self.name = _ioctl_str(self.fd, 0x06)
            self.uniq = _ioctl_str(self.fd, 0x08)
        except OSError:
            os.close(self.fd)
            raise

    def absinfo(self, axis):
        return get_absinfo(self.fd, axis)

    def pressed_keys(self):
        return get_pressed_keys(self.fd)

    def read_events(self):
        """Lê tudo o que já chegou. Devolve [(type, code, value)].
        OSError(ENODEV) quando o controle desconectou."""
        try:
            data = os.read(self.fd, EVENT.size * 256)
        except BlockingIOError:
            return []
        return [e[2:] for e in EVENT.iter_unpack(data[: len(data) - len(data) % EVENT.size])]

    def read_events_timed(self):
        """Igual a read_events, com a hora em que o kernel recebeu cada
        evento: [(microssegundos, type, code, value)]."""
        try:
            data = os.read(self.fd, EVENT.size * 256)
        except BlockingIOError:
            return []
        return [(sec * 1_000_000 + usec, typ, code, value) for sec, usec, typ, code, value
                in EVENT.iter_unpack(data[: len(data) - len(data) % EVENT.size])]

    def use_monotonic_clock(self):
        """Hora dos eventos pelo relógio que não pula (EVIOCSCLOCKID)."""
        fcntl.ioctl(self.fd, _ioc(_IOC_WRITE, 0xA0, 4), struct.pack("i", time.CLOCK_MONOTONIC))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
