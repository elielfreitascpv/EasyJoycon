"""Cliente DSU de teste (faz o mesmo que o Dolphin): mostra ao vivo o que o
servidor está mandando de cada jogador. Comando: ./easyjoycon.sh testar
"""
import socket
import struct
import sys
import time
import zlib

from . import dsu

NAMES1 = {v: k for k, v in dsu.BITS1.items()}
NAMES2 = {v: k for k, v in dsu.BITS2.items()}


def _packet(msg_type, payload):
    body = struct.pack("<I", msg_type) + payload
    head = struct.pack("<4sHHII", b"DSUC", 1001, len(body), 0, 4242)
    crc = zlib.crc32(head + body) & 0xFFFFFFFF
    return struct.pack("<4sHHII", b"DSUC", 1001, len(body), crc, 4242) + body


def decode(data):
    """Pacote de dados do pad → dict legível (ou None)."""
    if len(data) != 100 or data[:4] != b"DSUS":
        return None
    if struct.unpack_from("<I", data, 16)[0] != dsu.MSG_DATA:
        return None
    b1, b2, ps, touch = data[36:40]
    pressed = [n for bit, n in NAMES1.items() if b1 & bit]
    pressed += [n for bit, n in NAMES2.items() if b2 & bit]
    pressed += ["ps"] * ps + ["touch"] * touch
    lx, ly, rx, ry = ((v - 128) / 127 for v in data[40:44])
    _, ax, ay, az, gp, gy, gr = struct.unpack_from("<Q6f", data, 68)
    return {"slot": data[20], "battery": data[31], "buttons": pressed,
            "lstick": (lx, ly), "rstick": (rx, ry),
            "accel": (ax, ay, az), "gyro": (gp, gy, gr)}


def run(ports, seconds=None):
    socks = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.05)
        socks.append((port, s))
    last = {}
    t0 = next_req = time.monotonic()
    tty = sys.stdout.isatty()
    while seconds is None or time.monotonic() - t0 < seconds:
        now = time.monotonic()
        if now >= next_req:
            for port, s in socks:
                s.sendto(_packet(dsu.MSG_DATA, struct.pack("<BB6s", 0, 0, b"\0" * 6)),
                         ("127.0.0.1", port))
            next_req = now + 1
        for port, s in socks:
            try:
                while True:
                    d = decode(s.recv(2048))
                    if d:
                        last[(port, d["slot"])] = d
            except (TimeoutError, BlockingIOError):
                pass
        if tty:
            lines = ["Ctrl+C pra sair.  Accel em g (esquerda, −cima, frente);"
                     " gyro em °/s (pitch up, yaw right, roll right)", ""]
            for (port, slot), d in sorted(last.items()):
                kind = "Nunchuk" if port != ports[0] else "Wii Remote"
                a = " ".join(f"{v:+5.2f}" for v in d["accel"])
                g = " ".join(f"{v:+7.1f}" for v in d["gyro"])
                lines.append(f"Jogador {slot + 1} {kind:10} accel {a}   gyro {g}")
                if kind == "Wii Remote":
                    ls = " ".join(f"{v:+.2f}" for v in d["lstick"])
                    rs = " ".join(f"{v:+.2f}" for v in d["rstick"])
                    lines.append(f"           botões: {' '.join(d['buttons']) or '—':40}"
                                 f" esq {ls}  dir {rs}")
            if not last:
                lines.append("nada chegando — o servidor (./easyjoycon.sh) está aberto?")
            sys.stdout.write("\x1b[H\x1b[J" + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(0.05)
    return last
