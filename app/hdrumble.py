"""Vibração direta pelo hidraw, com a frequência que a gente escolhe.

O hid-nintendo vibra sempre em 160/320 Hz e não deixa mudar. O SDL (que o
Dolphin usa pra ler Joy-Con direto) vibra a ~150 Hz nas duas faixas — uma
tremida grave, parecida com o motorzinho do Wii Remote. Aqui mandamos o
mesmo relatório que o SDL (SDL_hidapi_switch.c, licença zlib; tabela do
dekuNukem/Nintendo_Switch_Reverse_Engineering, rumble_data_table.md):

  relatório de saída 0x10: contador (0–15) + 4 bytes por lado (esquerdo,
  direito): frequência alta, amplitude alta, frequência baixa, amplitude baixa.

O Joy-Con para de vibrar sozinho se não receber nada, então enquanto vibra
o relatório é repetido a cada 50 ms (o hid-nintendo faz igual).

Dois estilos (config [vibracao] estilo):
- "grave": as duas faixas a ~150 Hz, liga e desliga seco (o do SDL).
- "motor" (2026-10-12): imita o motorzinho do Wii Remote (massa girando):
  faixa grave = o corpo da tremida, faixa aguda no dobro da frequência e
  mais fraca = o zumbido do motor; ele embala (ATTACK_S, sobe a força e a
  frequência) e para devagar (RELEASE_S) em vez de ligar/desligar seco.
"""
import asyncio
import logging
import math
import os

from .colors import hidraw_path

log = logging.getLogger(__name__)

GRAVE_HZ = 150         # estilo "grave": as duas faixas aqui (o SDL usa esse)
NEUTRAL = bytes([0x00, 0x01, 0x40, 0x40])
REFRESH_S = 0.05
RAMP_TICK_S = 0.015    # enquanto embala/para: um pacote por pacote do Joy-Con
# Estilo "motor"
ATTACK_S = 0.04        # 0 → força máxima
RELEASE_S = 0.06       # força máxima → parado
MOTOR_HZ = (110, 160)  # frequência da faixa grave: embalando → embalado
BUZZ = 0.5             # força da faixa aguda (2× a frequência) / da grave
STYLES = ("motor", "grave")
STOP_REPEAT = 3        # pacotes de "parar" (um pode se perder no Bluetooth)
PACKET_LEN = 49        # tamanho do relatório pelo Bluetooth

# Limites de amplitude (0–65535) → código, 101 degraus (tabela do SDL).
_THRESHOLDS = (
    0, 514, 775, 921, 1096, 1303, 1550, 1843, 2192, 2606, 3100, 3686, 4383, 5213, 6199,
    7372, 7698, 8039, 8395, 8767, 9155, 9560, 9984, 10426, 10887, 11369, 11873, 12398,
    12947, 13520, 14119, 14744, 15067, 15397, 15734, 16079, 16431, 16790, 17158, 17534,
    17918, 18310, 18711, 19121, 19540, 19967, 20405, 20851, 21308, 21775, 22251, 22739,
    23236, 23745, 24265, 24797, 25340, 25894, 26462, 27041, 27633, 28238, 28856, 29488,
    30134, 30794, 31468, 32157, 32861, 33581, 34316, 35068, 35836, 36620, 37422, 38242,
    39079, 39935, 40809, 41703, 42616, 43549, 44503, 45477, 46473, 47491, 48531, 49593,
    50679, 51789, 52923, 54082, 55266, 56476, 57713, 58977, 60268, 61588, 62936, 64315,
    65535)


def _step(amplitude):
    for i, limit in enumerate(_THRESHOLDS):
        if amplitude <= limit:
            return i
    return len(_THRESHOLDS) - 1


def _freq(hz):
    """Hz → (código da faixa alta, código da faixa baixa). Fórmula do
    rumble_data_table.md: round(log2(hz/10)·32); alta = (e − 0x60)·4,
    baixa = e − 0x40 (alta vale ~82–1253 Hz, baixa ~41–626 Hz)."""
    e = round(math.log2(hz / 10.0) * 32.0)
    return max(0, min(0x1FC, (e - 0x60) * 4)), max(0, min(0x7F, e - 0x40))


def encode_bands(low_hz, low_level, high_hz, high_level):
    """As duas faixas de um lado → 4 bytes. Amplitude pela tabela do SDL:
    alta = 2× degrau; baixa = 0x40 + degrau/2, com o bit 0x8000 nos ímpares."""
    if low_level <= 0 and high_level <= 0:
        return NEUTRAL
    hf, _ = _freq(high_hz)
    _, lf = _freq(low_hz)
    hi = _step(int(min(1.0, max(0.0, high_level)) * 65535))
    lo = _step(int(min(1.0, max(0.0, low_level)) * 65535))
    high_amp = 2 * hi
    low_amp = (0x40 + lo // 2) | (0x8000 if lo % 2 else 0)
    return bytes([hf & 0xFF,
                  high_amp | ((hf >> 8) & 0x01),
                  lf | ((low_amp >> 8) & 0x80),
                  low_amp & 0xFF])


def encode(level):
    """Estilo "grave": força 0–1 → as duas faixas a ~150 Hz."""
    return encode_bands(GRAVE_HZ, level, GRAVE_HZ, level)


def motor_side(force, spin):
    """Estilo "motor": força atual 0–1 e quanto já embalou (0–1) → 4 bytes."""
    low_hz = MOTOR_HZ[0] + (MOTOR_HZ[1] - MOTOR_HZ[0]) * max(0.0, min(1.0, spin))
    return encode_bands(low_hz, force, 2 * low_hz, force * BUZZ)


def packet(counter, level=None, side=None):
    side = encode(level) if side is None else side
    return (bytes([0x10, counter & 0x0F]) + side + side).ljust(PACKET_LEN, b"\0")


class DirectRumble:
    """Vibra um controle pelo hidraw. Tudo no laço asyncio do servidor."""

    def __init__(self, hid, style="motor"):
        self.hid = hid
        self.style = style if style in STYLES else "motor"
        self.fd = None
        self.counter = 0
        self.level = 0.0         # força pedida
        self.force = 0.0         # estilo motor: força de agora (embalando/parando)
        self.peak = 0.0          # estilo motor: força de quando estava embalado
        self._last_tick = None
        self._refresh = None
        self._stop_at = None

    def open(self):
        path = hidraw_path(self.hid)
        if path is None:
            return False
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            log.debug("vibração direta: não abriu %s (%s)", path, e)
            return False
        return True

    def _send(self, level=None, side=None):
        if self.fd is None:
            return
        try:
            os.write(self.fd, packet(self.counter, level, side))
        except BlockingIOError:
            pass                       # Bluetooth ocupado: o próximo refresh manda
        self.counter = (self.counter + 1) & 0x0F

    def play(self, level, length_ms=0):
        """level 0–1; length_ms 0 = até mandar parar."""
        loop = asyncio.get_running_loop()
        if self._stop_at is not None:
            self._stop_at.cancel()
            self._stop_at = None
        if level <= 0:
            self.release()
            return
        self.level = level
        if self.style == "grave":
            self._send(level)
            delay = REFRESH_S
        else:
            delay = self._motor_step(loop.time())
        self._schedule(delay)
        if length_ms:
            self._stop_at = loop.call_later(length_ms / 1000, self.release)

    def release(self):
        """Parar como o motor para: devagar (no "grave", na hora)."""
        if self._stop_at is not None:
            self._stop_at.cancel()
            self._stop_at = None
        if self.style == "grave" or self.force <= 0:
            self.stop()
            return
        self.level = 0.0
        self._schedule(self._motor_step(asyncio.get_running_loop().time()))

    def _schedule(self, delay):
        if self._refresh is not None:
            self._refresh.cancel()
        self._refresh = None
        if delay is not None:
            self._refresh = asyncio.get_running_loop().call_later(delay, self._tick)

    def _motor_step(self, now):
        """Anda a força em direção ao pedido, manda e diz quando voltar
        (None = parou de vez)."""
        # no máximo um passo de rampa por vez: parado/segurando há 50 ms não
        # pode virar um salto quando a força muda
        dt = 0.0 if self._last_tick is None else min(RAMP_TICK_S, now - self._last_tick)
        self._last_tick = now
        if self.force < self.level:
            self.force = min(self.level, self.force + dt / ATTACK_S if dt else self.level * 0.25)
            self.peak = self.level
        elif self.force > self.level:
            self.force = max(self.level, self.force - dt / RELEASE_S)
        if self.force <= 0:
            self.stop()
            return None
        spin = self.force / self.peak if self.peak else 1.0
        self._send(side=motor_side(self.force, spin))
        return REFRESH_S if self.force == self.level else RAMP_TICK_S

    def _tick(self):
        self._refresh = None
        try:
            self._tick_send()
        except OSError as e:          # o controle saiu: para quieto (o servidor fecha)
            log.debug("vibração direta: %s", e)
            self.level = self.force = self.peak = 0.0
            self._last_tick = None

    def _tick_send(self):
        if self.style == "grave":
            if self.level > 0:
                self._send(self.level)
                self._schedule(REFRESH_S)
            return
        self._schedule(self._motor_step(asyncio.get_running_loop().time()))

    def stop(self):
        """Para na hora."""
        self.level = self.force = self.peak = 0.0
        self._last_tick = None
        for h in (self._refresh, self._stop_at):
            if h is not None:
                h.cancel()
        self._refresh = self._stop_at = None
        for _ in range(STOP_REPEAT):
            self._send(0.0)

    def close(self):
        if self.fd is not None:
            try:
                self.stop()
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
