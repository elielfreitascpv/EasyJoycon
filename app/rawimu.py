"""Movimento lido direto do controle (hidraw), como o Switch, o SDL e a
Steam fazem, em vez do /dev/input do hid-nintendo.

Por quê: o driver lê a calibração do IMU uma vez ao conectar e, quando lê
errado ("inaccurate IMU divisors"), o movimento sai zerado até reconectar.
Ele também arredonda, só manda eixo que mudou ("fuzz") e não sabe quantos
pacotes se perderam. Aqui a calibração é lida por nós (e guardada em
data/calibracao-imu.json, como as cores) e o pacote diz o tempo dele.

Relatório de entrada 0x30 (o driver já deixa o controle nesse modo; o
hidraw recebe uma cópia de tudo, sem atrapalhar o driver):
  byte 1 = relógio do controle (+1 a cada 5 ms, dá a volta em 256),
  13–48 = 3 leituras de 12 bytes (acelerômetro xyz, giroscópio xyz; int16
  little-endian), da mais NOVA pra mais velha (ver controllers.read_imu).

Calibração na memória (SPI): 24 bytes = acelerômetro origem xyz, escala
xyz, giroscópio origem xyz, escala xyz. A do usuário (feita no Switch) fica
em 0x8028 se 0x8026 = B2 A1; senão vale a de fábrica, em 0x6020.

A conta é a mesma do hid-nintendo (joycon_parse_imu_report), sem arredondar:
  giro (°/s) = (cru − origem) × escala / (escala − origem) / 14,247
  aceleração (g) = cru × escala / (escala − origem) / 4096   (sem tirar a
  origem: o driver diz que tirando piorava)
No Joy-Con (R) os eixos Y e Z vêm invertidos (o sensor está virado).
Conferido em 2026-10-12 lendo os dois caminhos juntos: bate com o driver
(diferença menor que o "fuzz" dele).

Relógio do pacote (gravado em 2026-10-12): normal +3 por pacote. Com sinal
ruim o R passa a mandar a cada 30 ms com +6 (metade das leituras nunca
chega), ou +9 num buraco; às vezes +2 ou +5 (pacote saiu um pouco antes ou
depois). Então leituras perdidas = (passo − 3) × 5 ms — exato, sem
adivinhar pela hora de chegada. Só nos Joy-Cons: o Pro pelo Bluetooth manda
em outro ritmo (8–11 ms, diz o driver) e não foi medido.
"""
import json
import logging
import os
import struct

from .colors import hidraw_path, read_spi
from .config import DATA_DIR

log = logging.getLogger(__name__)

CAL_FILE = DATA_DIR / "calibracao-imu.json"
KINDS = ("L", "R")        # controles medidos (ver acima)
SPI_FACTORY = 0x6020
SPI_USER = 0x8026         # 2 bytes de "tem calibração do usuário" + os 24 dela
USER_MAGIC = b"\xb2\xa1"
CAL_LEN = 24
REPORT_IMU = 0x30
REPORT_LEN = 49
IMU_AT = 13
TICK_S = 0.005            # relógio do pacote: 5 ms por passo
NORMAL_STEP = 3           # passos por pacote (3 leituras)
WRAP_S = 1.0              # pacotes mais longe que isso: o relógio pode ter dado a volta
ACCEL_RES = 4096          # o mesmo do driver (unidades por g)
GYRO_RES = 14.247         # o mesmo do driver (unidades por °/s)
# Calibração com cara de certa: as de fábrica conferidas têm escala 16384
# no acelerômetro e 13371–15335 no giroscópio, origens de poucas centenas.
DIVISOR_RANGE = (8000, 24000)
MAX_OFFSET = 4000


def load():
    try:
        data = json.loads(CAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {mac: v for mac, v in data.items() if valid(v)}


def save(cache):
    tmp = CAL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    tmp.replace(CAL_FILE)


def valid(cal):
    """cal = lista de 12 números (accel origem, escala; giro origem, escala)."""
    if not (isinstance(cal, list) and len(cal) == 12 and all(isinstance(x, int) for x in cal)):
        return False
    for base in (0, 6):
        for i in range(3):
            off, scale = cal[base + i], cal[base + 3 + i]
            if abs(off) > MAX_OFFSET or not DIVISOR_RANGE[0] <= scale - off <= DIVISOR_RANGE[1]:
                return False
    return True


def parse_calibration(data):
    return list(struct.unpack("<12h", data))


def read_calibration(hid):
    """Calibração do IMU que vale (a do usuário, se houver; senão a de
    fábrica). Bloqueia (rodar fora do laço asyncio). None se não deu ou se
    veio esquisita."""
    user = read_spi(hid, SPI_USER, 2 + CAL_LEN)
    if user is None:
        return None
    if user[:2] == USER_MAGIC:
        cal = parse_calibration(user[2:])
        if valid(cal):
            return cal
        log.info("calibração do usuário esquisita (%s): uso a de fábrica", cal)
    data = read_spi(hid, SPI_FACTORY, CAL_LEN)
    if data is None:
        return None
    cal = parse_calibration(data)
    if not valid(cal):
        log.warning("calibração de fábrica esquisita: %s", cal)
        return None
    return cal


class Converter:
    """Números crus do pacote → (g, °/s) nos eixos do hid-nintendo."""

    def __init__(self, kind, cal):
        ao, asc, go, gsc = cal[0:3], cal[3:6], cal[6:9], cal[9:12]
        sign = (1, -1, -1) if kind == "R" else (1, 1, 1)
        self.accel_k = [sign[i] * asc[i] / (asc[i] - ao[i]) / ACCEL_RES for i in range(3)]
        self.gyro_k = [sign[i] * gsc[i] / (gsc[i] - go[i]) / GYRO_RES for i in range(3)]
        self.gyro_off = list(go)

    def samples(self, report):
        """As 3 leituras do pacote, da mais velha pra mais nova:
        [((ax, ay, az) g, (gx, gy, gz) °/s)]."""
        ak, gk, go = self.accel_k, self.gyro_k, self.gyro_off
        out = []
        for i in (2, 1, 0):
            v = struct.unpack_from("<6h", report, IMU_AT + 12 * i)
            out.append(((v[0] * ak[0], v[1] * ak[1], v[2] * ak[2]),
                        ((v[3] - go[0]) * gk[0], (v[4] - go[1]) * gk[1],
                         (v[5] - go[2]) * gk[2])))
        return out


class RawImu:
    """Lê os pacotes de movimento do hidraw do controle (só leitura: os
    comandos continuam com o driver)."""

    def __init__(self, hid, kind, cal):
        path = hidraw_path(hid)
        if path is None:
            raise FileNotFoundError(f"hidraw de {hid}")
        self.convert = Converter(kind, cal)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        self._timer = None
        self._t = None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, now):
        """Pacotes que chegaram: [(segundos perdidos antes dele, leituras)].
        OSError se o controle saiu."""
        packets = []
        while True:
            try:
                report = os.read(self.fd, 64)
            except BlockingIOError:
                break
            if len(report) < REPORT_LEN or report[0] != REPORT_IMU:
                continue          # resposta de comando (0x21) etc.
            packets.append((self.lost_before(report[1], now), self.convert.samples(report)))
        return packets

    def lost_before(self, timer, now):
        """Quanto tempo de leituras se perdeu antes deste pacote, pelo
        relógio dele."""
        last, last_t = self._timer, self._t
        self._timer, self._t = timer, now
        if last is None:
            return 0.0
        step = (timer - last) & 0xFF
        if now - last_t > WRAP_S:      # o relógio pode ter dado a volta: a hora decide quantas
            step += 256 * round(((now - last_t) / TICK_S - step) / 256)
        return max(0, step - NORMAL_STEP) * TICK_S
