"""Servidor DSU (protocolo "Cemuhook", UDP) — o jeito que o Dolphin recebe
controle com movimento de outro programa.

Formato conferido com o cliente do Dolphin
(Source/Core/InputCommon/ControllerInterface/DualShockUDPClient/
DualShockUDPProto.h, tudo little-endian e sem espaço entre os campos):
cabeçalho de 16 bytes ("DSUS", versão 1001, tamanho sem cabeçalho, CRC32 do
pacote com o campo do CRC zerado, id do servidor) + tipo da mensagem (u32).

O Dolphin chama cada pad de "DSUClient/<slot>/<descrição do servidor>".
"""
import asyncio
import logging
import random
import struct
import time
import zlib

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1001
MSG_VERSION, MSG_PORTS, MSG_DATA = 0x100000, 0x100001, 0x100002
SLOTS = 4
CLIENT_TIMEOUT = 5.0   # o Dolphin pede de novo a cada 1 s
GONE_ANNOUNCE = 3.0    # avisa "desconectado" por tanto tempo depois que o jogador sai

HEADER = struct.Struct("<4sHHII")          # magic, versão, tamanho, crc, id
PORT = struct.Struct("<BBBB6sB")           # slot, estado, modelo, conexão, mac, bateria
PAD_TAIL = struct.Struct("<BIBBBBBBBB12B6s6sQ6f")
# ↑ active, contador, botões1, botões2, PS, touch, LX, LY, RX, RY,
#   12 analógicos, touch1, touch2, timestamp µs, accel xyz, gyro pitch/yaw/roll

STATE_DISCONNECTED, STATE_CONNECTED = 0, 2
MODEL_FULL_GYRO, CONN_BLUETOOTH = 2, 2
BATTERY_CHARGING, BATTERY_CHARGED = 0xEE, 0xEF

# button_states1 / button_states2 (bits do DualShock 4)
BITS1 = {"share": 0x01, "l3": 0x02, "r3": 0x04, "options": 0x08,
         "up": 0x10, "right": 0x20, "down": 0x40, "left": 0x80}
BITS2 = {"l2": 0x01, "r2": 0x02, "l1": 0x04, "r1": 0x08,
         "triangle": 0x10, "circle": 0x20, "cross": 0x40, "square": 0x80}
ANALOG = ("left", "down", "right", "up", "square", "cross", "circle", "triangle",
          "r1", "l1", "r2", "l2")


def packet(server_id, msg_type, payload):
    body = struct.pack("<I", msg_type) + payload
    head = HEADER.pack(b"DSUS", PROTOCOL_VERSION, len(body), 0, server_id)
    crc = zlib.crc32(head + body) & 0xFFFFFFFF
    return HEADER.pack(b"DSUS", PROTOCOL_VERSION, len(body), crc, server_id) + body


def parse_request(data):
    """Pedido do cliente → (tipo, resto) ou None se inválido."""
    if len(data) < HEADER.size + 4:
        return None
    magic, version, length, crc, _ = HEADER.unpack_from(data)
    if magic != b"DSUC" or version > PROTOCOL_VERSION or HEADER.size + length > len(data):
        return None
    data = data[: HEADER.size + length]
    zeroed = data[:8] + b"\0\0\0\0" + data[12:]
    if zlib.crc32(zeroed) & 0xFFFFFFFF != crc:
        return None
    (msg_type,) = struct.unpack_from("<I", data, HEADER.size)
    return msg_type, data[HEADER.size + 4:]


def _byte_axis(v):
    return max(0, min(255, round(128 + v * 127)))


def _battery_byte(pad):
    if pad.charging:
        return BATTERY_CHARGED if pad.battery >= 5 else BATTERY_CHARGING
    return pad.battery


class DsuServer(asyncio.DatagramProtocol):
    """Um servidor DSU com 4 slots. get_pad(slot) → PadState."""

    def __init__(self, description, index, get_pad):
        self.description = description
        self.server_id = random.getrandbits(32)
        self.get_pad = get_pad
        self.macs = [bytes([0x02, 0x4A, 0x43, 0x00, index, s]) for s in range(SLOTS)]
        self.clients = {}       # endereço → {"slots": set, "all": bool, "t": instante}
        self.counters = [0] * SLOTS
        self.transport = None
        self._seen = set()      # (endereço, tipo) já logados
        self._was_connected = [False] * SLOTS
        self._gone_until = [0.0] * SLOTS

    def connection_made(self, transport):
        self.transport = transport

    def _port_info(self, slot):
        pad = self.get_pad(slot)
        if not pad.connected:
            # igual ao "vazio" do Dolphin ({} com só o pad_id), pra ele não ver mudança
            return PORT.pack(slot, STATE_DISCONNECTED, 0, 0, b"\0" * 6, 0)
        return PORT.pack(slot, STATE_CONNECTED, MODEL_FULL_GYRO, CONN_BLUETOOTH,
                         self.macs[slot], _battery_byte(pad))

    def track(self, now):
        """Anota quem saiu, pra avisar o Dolphin por alguns segundos."""
        for slot in range(SLOTS):
            connected = self.get_pad(slot).connected
            if self._was_connected[slot] and not connected:
                self._gone_until[slot] = now + GONE_ANNOUNCE
            self._was_connected[slot] = connected

    def _answers_ports(self, slot, now):
        """O Dolphin espera resposta só dos controles conectados ("We will receive
        a message for every connected device"). Responder também os slots vazios
        fazia ele recriar o controle a cada 2 s e nada chegava no jogo
        (DualShockUDPClient.cpp, HotplugThreadFunc)."""
        return self.get_pad(slot).connected or now < self._gone_until[slot]

    def datagram_received(self, data, addr):
        req = parse_request(data)
        if req is None:
            if ("inválido", addr) not in self._seen:
                self._seen.add(("inválido", addr))
                log.warning("DSU %s: pacote inválido de %s (%d bytes): %s", self.description,
                            addr, len(data), data[:24].hex())
            return
        msg_type, rest = req
        if (addr, msg_type) not in self._seen:
            self._seen.add((addr, msg_type))
            log.info("DSU %s: cliente %s:%d pediu %s", self.description, addr[0], addr[1],
                     {MSG_VERSION: "versão", MSG_PORTS: "lista de portas",
                      MSG_DATA: "dados"}.get(msg_type, hex(msg_type)))
        if msg_type == MSG_VERSION:
            self.transport.sendto(packet(self.server_id, MSG_VERSION,
                                         struct.pack("<HH", PROTOCOL_VERSION, 0)), addr)
        elif msg_type == MSG_PORTS and len(rest) >= 4:
            (count,) = struct.unpack_from("<I", rest)
            now = time.monotonic()
            for slot in rest[4: 4 + min(count, SLOTS)]:
                if slot < SLOTS and self._answers_ports(slot, now):
                    self.transport.sendto(
                        packet(self.server_id, MSG_PORTS, self._port_info(slot) + b"\0"), addr)
        elif msg_type == MSG_DATA and len(rest) >= 8:
            flags, slot, mac = rest[0], rest[1], rest[2:8]
            c = self.clients.setdefault(addr, {"slots": set(), "all": False, "t": 0})
            c["t"] = time.monotonic()
            if flags == 0:
                c["all"] = True
            if flags & 1 and slot < SLOTS:
                c["slots"].add(slot)
            if flags & 2 and mac in self.macs:
                c["slots"].add(self.macs.index(mac))

    def pad_packet(self, slot, pad):
        self.counters[slot] = (self.counters[slot] + 1) & 0xFFFFFFFF
        b = pad.buttons
        b1 = sum(bit for name, bit in BITS1.items() if b.get(name))
        b2 = sum(bit for name, bit in BITS2.items() if b.get(name))
        m = pad.motion
        tail = PAD_TAIL.pack(
            1, self.counters[slot], b1, b2, int(bool(b.get("ps"))), int(bool(b.get("touch"))),
            _byte_axis(pad.lstick[0]), _byte_axis(pad.lstick[1]),
            _byte_axis(pad.rstick[0]), _byte_axis(pad.rstick[1]),
            *(255 if b.get(n) else 0 for n in ANALOG),
            b"\0" * 6, b"\0" * 6, m.timestamp_us & 0xFFFFFFFFFFFFFFFF,
            *m.accel, *m.gyro)
        return packet(self.server_id, MSG_DATA, self._port_info(slot) + tail)

    def send_pads(self, now):
        """Manda o estado de todos os slots pra quem pediu (ritmo fixo)."""
        self.track(now)
        for slot in range(SLOTS):
            self.send_slot(slot, now)

    def send_slot(self, slot, now):
        """Manda um slot já (chamado também na hora que chega pacote do controle)."""
        if not self.clients or self.transport is None:
            return
        pad = self.get_pad(slot)
        if not pad.connected:
            return
        data = None
        for addr, c in list(self.clients.items()):
            if now - c["t"] > CLIENT_TIMEOUT:
                del self.clients[addr]
                continue
            if c["all"] or slot in c["slots"]:
                data = data or self.pad_packet(slot, pad)
                self.transport.sendto(data, addr)

    @property
    def has_clients(self):
        """Algum cliente pediu dados nos últimos CLIENT_TIMEOUT s. (Antes só
        os esquecia ao mandar dados: sem nenhum controle conectado, o Dolphin
        fechado continuava "recebendo" pra sempre.)"""
        now = time.monotonic()
        return any(now - c["t"] <= CLIENT_TIMEOUT for c in self.clients.values())


async def start(description, index, port, get_pad):
    loop = asyncio.get_running_loop()
    server = DsuServer(description, index, get_pad)
    await loop.create_datagram_endpoint(lambda: server, local_addr=("127.0.0.1", port))
    log.info("DSU \"%s\" em 127.0.0.1:%d", description, port)
    return server
