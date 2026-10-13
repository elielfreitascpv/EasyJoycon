"""Cor de verdade de cada controle: fica gravada na memória (SPI) do Joy-Con
/ Pro Controller, em 0x6050 (corpo RGB, botões RGB; no Pro também as
alças). O hid-nintendo não mostra isso, então pedimos direto pelo hidraw
(subcomando 0x10, "ler SPI") — uma vez só por controle: a resposta fica em
data/cores.json.

Pedido (relatório de saída 0x01): contador, 8 bytes de vibração neutra,
subcomando 0x10, endereço (4 bytes, little-endian) e tamanho.
Resposta (relatório de entrada 0x21): byte 13 = ACK, 14 = subcomando,
15–18 = endereço, 19 = tamanho, 20… = dados.
"""
import json
import logging
import os
import select
import time
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

COLORS_FILE = DATA_DIR / "cores.json"
SYS_HID = Path("/sys/bus/hid/devices")
SPI_COLORS = 0x6050
SPI_COLORS_LEN = 12
NEUTRAL_RUMBLE = bytes([0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40])


def load():
    try:
        data = json.loads(COLORS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {mac: v for mac, v in data.items() if _valid(v)}


def save(cache):
    tmp = COLORS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    tmp.replace(COLORS_FILE)


def _valid(v):
    return (isinstance(v, dict) and all(
        isinstance(v.get(k), list) and len(v[k]) == 3
        and all(isinstance(x, int) and 0 <= x <= 255 for x in v[k]) for k in ("body", "buttons")))


def hidraw_path(hid):
    """/dev/hidrawN do controle (hid = "0005:057E:2007.0012")."""
    try:
        names = os.listdir(SYS_HID / hid / "hidraw")
    except OSError:
        return None
    return f"/dev/{names[0]}" if names else None


def spi_read_request(counter, address, length):
    return (bytes([0x01, counter & 0x0F]) + NEUTRAL_RUMBLE
            + bytes([0x10]) + address.to_bytes(4, "little") + bytes([length]))


def parse_spi_reply(report, address, length):
    """Dados de uma resposta 0x21 ao "ler SPI" desse endereço, ou None."""
    if (len(report) < 20 + length or report[0] != 0x21 or report[14] != 0x10
            or int.from_bytes(report[15:19], "little") != address or report[19] != length):
        return None
    return bytes(report[20:20 + length])


def colors_from_spi(data):
    body, buttons = list(data[0:3]), list(data[3:6])
    if body == [0xFF, 0xFF, 0xFF] and buttons == [0xFF, 0xFF, 0xFF]:
        return None    # memória vazia (controle sem cor gravada)
    return {"body": body, "buttons": buttons}


def read_spi(hid, address, length, timeout=1.5, tries=2):
    """Lê `length` bytes da memória do controle. Bloqueia (rodar fora do laço
    asyncio). None se não deu (sem permissão, sem resposta, controle saiu)."""
    path = hidraw_path(hid)
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        log.debug("SPI: não abriu %s (%s)", path, e)
        return None
    try:
        for attempt in range(tries):
            os.write(fd, spi_read_request(attempt, address, length))
            end = time.monotonic() + timeout
            while (left := end - time.monotonic()) > 0:
                if not select.select([fd], [], [], left)[0]:
                    break
                try:
                    report = os.read(fd, 64)
                except BlockingIOError:
                    continue
                data = parse_spi_reply(report, address, length)
                if data is not None:
                    return data
    except OSError as e:
        log.debug("SPI: %s (%s)", path, e)
    finally:
        os.close(fd)
    return None


def read_colors(hid):
    """Pede as cores ao controle (bloqueia). None se não deu."""
    data = read_spi(hid, SPI_COLORS, SPI_COLORS_LEN)
    return None if data is None else colors_from_spi(data)
