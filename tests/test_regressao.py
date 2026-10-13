"""Teste de regressão — rodar antes de cada commit:
    venv/bin/python tests/test_regressao.py

Tudo simulado (sem controle, sem Bluetooth, sem Dolphin), numa pasta
temporária: nunca encosta em data/ nem na configuração real do Dolphin.
"""
import asyncio
import logging
import math
import os
import socket
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="easyjoycon-teste-"))
os.environ["EASYJOYCON_DATA_DIR"] = str(TMP / "data")
os.environ["EASYJOYCON_DOLPHIN_CONFIG"] = str(TMP / "dolphin")
os.environ["EASYJOYCON_IDIOMA"] = "pt"   # os testes conferem os avisos em português
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, dolphin, dsu, i18n, motion, players  # noqa: E402
from app import evdev as E  # noqa: E402
from app.controllers import Controller  # noqa: E402

logging.disable(logging.CRITICAL)  # sem log no terminal durante o teste
CFG = config.load()
RESULTS = []


def caso(fn):
    RESULTS.append(fn)
    return fn


def close(a, b, tol=1e-3):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


# --- controles de mentira ------------------------------------------------
class FakeCtrl:
    def __init__(self, kind, mac, cfg=CFG):
        self.kind, self.mac = kind, mac
        self.name = {"L": "Joy-Con (L)", "R": "Joy-Con (R)", "PRO": "Pro Controller"}[kind]
        self.buttons = set()
        self.sticks = {"main": (0.0, 0.0), "left": (0.0, 0.0), "right": (0.0, 0.0)}
        self.motion = motion.MotionProcessor(cfg)
        self.player = None
        self.led_pattern = None

    def pressed(self, name):
        return name in self.buttons

    def stick(self, which="main"):
        return self.sticks[which]

    def battery(self):
        return 4, False


def manager():
    for f in (TMP / "data").glob("jogadores*.json"):
        f.unlink()
    return players.PlayerManager(CFG)


# --- DSU -----------------------------------------------------------------
def client_packet(msg_type, payload):
    body = struct.pack("<I", msg_type) + payload
    head = struct.pack("<4sHHII", b"DSUC", 1001, len(body), 0, 1234)
    crc = zlib.crc32(head + body) & 0xFFFFFFFF
    return struct.pack("<4sHHII", b"DSUC", 1001, len(body), crc, 1234) + body


def check_server_packet(data, expected_size):
    assert len(data) == expected_size, (len(data), expected_size)
    magic, ver, length, crc, _ = struct.unpack_from("<4sHHII", data)
    assert magic == b"DSUS" and ver == 1001 and length == len(data) - 16
    zeroed = data[:8] + b"\0\0\0\0" + data[12:]
    assert zlib.crc32(zeroed) & 0xFFFFFFFF == crc, "CRC errado"
    return struct.unpack_from("<I", data, 16)[0]


@caso
def dsu_conversa_como_o_dolphin():
    """Versão, lista de portas e dados do pad com os tamanhos do Dolphin
    (VersionResponse 24, PortInfo 32, PadDataResponse 100 bytes)."""
    pad = players.PadState(connected=True, battery=4)
    pad.buttons = {"circle": True, "r2": True, "up": True, "l3": True, "ps": True}
    pad.lstick, pad.rstick = (1.0, -1.0), (0.0, 0.5)
    pad.motion = motion.MotionOut((0.1, -1.0, 0.2), (10.0, -20.0, 30.0), 123456)
    pads = [pad, players.PadState(), players.PadState(), players.PadState()]

    async def run():
        server = await dsu.start("Teste", 0, 0, lambda s: pads[s])
        port = server.transport.get_extra_info("sockname")[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()

        async def ask(pkt, n=1):
            sock.sendto(pkt, ("127.0.0.1", port))
            out = []
            for _ in range(n):
                out.append(await asyncio.wait_for(loop.sock_recv(sock, 2048), 1))
            return out

        (v,) = await ask(client_packet(dsu.MSG_VERSION, b""))
        assert check_server_packet(v, 24) == dsu.MSG_VERSION
        assert struct.unpack_from("<H", v, 20)[0] == 1001
        # só o slot conectado responde (o Dolphin recria o controle se os vazios responderem)
        sock.sendto(client_packet(dsu.MSG_PORTS, struct.pack("<I4B", 4, 0, 1, 2, 3)),
                    ("127.0.0.1", port))
        await asyncio.sleep(0.1)
        ports = []
        while True:
            try:
                ports.append(sock.recv(2048))
            except BlockingIOError:
                break
        assert [(p[20], p[21]) for p in ports] == [(0, 2)], [(p[20], p[21]) for p in ports]
        assert check_server_packet(ports[0], 32) == dsu.MSG_PORTS
        # pacote com CRC errado é ignorado
        bad = bytearray(client_packet(dsu.MSG_VERSION, b""))
        bad[12] ^= 0xFF
        sock.sendto(bytes(bad), ("127.0.0.1", port))
        # registra no slot 0 e recebe dados
        sock.sendto(client_packet(dsu.MSG_DATA, struct.pack("<BB6s", 1, 0, b"\0" * 6)),
                    ("127.0.0.1", port))
        await asyncio.sleep(0.05)
        server.send_pads(time.monotonic())
        d = await asyncio.wait_for(loop.sock_recv(sock, 2048), 1)
        assert check_server_packet(d, 100) == dsu.MSG_DATA
        b1, b2, ps = d[36], d[37], d[38]
        assert b1 & 0x10 and b1 & 0x02, "D-pad cima / L3"
        assert b2 & 0x20 and b2 & 0x02, "círculo / R2"
        assert ps == 1
        lx, ly, rx, ry = d[40:44]
        assert (lx, ly, rx, ry) == (255, 1, 128, 192), (lx, ly, rx, ry)
        analog = d[44:56]
        assert analog[3] == 255 and analog[6] == 255 and analog[10] == 255  # up, circle, r2
        ts, *vals = struct.unpack_from("<Q6f", d, 68)
        assert ts == 123456
        assert close(vals, [0.1, -1.0, 0.2, 10.0, -20.0, 30.0], 1e-5)
        # jogador saiu: avisa "desconectado" (igual ao vazio do Dolphin) e depois para
        server.send_pads(time.monotonic())
        pads[0] = players.PadState()
        server.send_pads(time.monotonic())
        while True:
            try:
                sock.recv(2048)
            except BlockingIOError:
                break
        (gone,) = await ask(client_packet(dsu.MSG_PORTS, struct.pack("<I4B", 1, 0, 0, 0, 0)))
        assert gone[20:32] == b"\0" * 12, gone[20:32].hex()
        server._gone_until[0] = 0
        sock.sendto(client_packet(dsu.MSG_PORTS, struct.pack("<I4B", 1, 0, 0, 0, 0)),
                    ("127.0.0.1", port))
        await asyncio.sleep(0.1)
        try:
            sock.recv(2048)
            raise AssertionError("slot vazio não devia responder")
        except BlockingIOError:
            pass
        sock.close()
        server.transport.close()

    asyncio.run(run())


# --- movimento -----------------------------------------------------------
@caso
def movimento_parado_e_eixos():
    """Parado com a face pra cima: Dolphin lê +1 g pra cima. Levantar a
    ponta (pitch up) e girar pra direita (yaw right) dão sinal positivo."""
    accel, gyro = motion.to_dsu((0, 0, 1), (0, 0, 0))
    assert close(accel, (0, -1, 0))          # Accel Up = −y → +1
    # rotação de −10°/s em torno de Y (esquerda) = frente subindo
    _, gyro = motion.to_dsu((0, 0, 1), (0, -10, 0))
    assert close(gyro, (10, 0, 0)), gyro    # pitch up positivo
    _, gyro = motion.to_dsu((0, 0, 1), (0, 0, -10))
    assert close(gyro, (0, 10, 0)), gyro    # frente virando pra direita = yaw right
    _, gyro = motion.to_dsu((0, 0, 1), (10, 0, 0))
    assert close(gyro, (0, 0, 10)), gyro    # roll right
    # deitado: esquerdo aponta os gatilhos pra esquerda de quem joga
    accel, _ = motion.to_dsu((1, 0, 0), (0, 0, 0), motion.SIDEWAYS_L)
    assert close(accel, (1, 0, 0))           # "Accel Left"
    accel, _ = motion.to_dsu((1, 0, 0), (0, 0, 0), motion.SIDEWAYS_R)
    assert close(accel, (-1, 0, 0))


def feed(mp, n, gyro, accel=(0, 0, 1), start=0.0, dt=0.005):
    t = start
    for i in range(n):
        t = start + i * dt
        mp.add_samples([(int(t * 1e6) + 1, accel, gyro)], t)
    return t


@caso
def calibra_sozinho_parado():
    """Desvio de 1,2°/s com o controle parado some depois de alguns segundos."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    t = feed(mp, 2000, (1.2, -0.8, 0.5))
    assert close(mp.bias, (1.2, -0.8, 0.5), 0.05), mp.bias
    out = mp.output(t)
    assert close(out.gyro, (0, 0, 0), 0.01), out.gyro


@caso
def calibra_joycon_com_desvio_grande_e_fabrica_torta():
    """Caso real do Joy-Con (L) em 2026-10-07: parado lê 1,09 g e o desvio
    passava do limite antigo — nunca calibrava e a mira escorregava."""
    import random
    rnd = random.Random(1)
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    t = 0.0
    for i in range(800):                     # 4 s na mesa, com ruído de verdade
        t = i * 0.005
        g = (3.5 + rnd.gauss(0, 0.15), -0.8 + rnd.gauss(0, 0.15), -2.9 + rnd.gauss(0, 0.15))
        a = (0.07 + rnd.gauss(0, 0.004), -0.02 + rnd.gauss(0, 0.004), 1.09 + rnd.gauss(0, 0.004))
        mp.add_samples([(int(t * 1e6) + 1, a, g)], t)
    assert mp.calibrated and close(mp.bias, (3.5, -0.8, -2.9), 0.1), mp.bias
    assert close(mp.output(t).gyro, (0, 0, 0), 0.01)


@caso
def nao_calibra_na_mao():
    """Na mão o giroscópio varia vários °/s: não pode virar "desvio"."""
    import random
    rnd = random.Random(2)
    mp = motion.MotionProcessor(CFG)
    for i in range(1200):
        t = i * 0.005
        g = (rnd.gauss(1, 6), rnd.gauss(0, 6), rnd.gauss(-2, 8))
        a = (0.28 + rnd.gauss(0, 0.04), -0.2 + rnd.gauss(0, 0.07), 1.0 + rnd.gauss(0, 0.05))
        mp.add_samples([(int(t * 1e6) + 1, a, g)], t)
    assert not mp.calibrated and mp.bias == [0.0, 0.0, 0.0]


@caso
def nao_calibra_mexendo():
    mp = motion.MotionProcessor(CFG)
    t = feed(mp, 1000, (60.0, 0, 0))
    assert close(mp.bias, (0, 0, 0)), mp.bias
    assert not mp.stalled(t)


@caso
def giro_lento_da_mao_nao_vira_desvio():
    """Depois de ver o controle quieto na mesa, "parado" passa a exigir quase
    aquele silêncio: virar a mão devagar e firme (3 °/s, tremendo 0,4 °/s)
    não é confundido com parado (antes virava desvio e a mira escorregava)."""
    import random
    rnd = random.Random(1)
    mp = motion.MotionProcessor(CFG)
    t = 0.0
    for _ in range(800):                              # 4 s na mesa: ruído 0,06 °/s
        t += 0.005
        g = tuple(rnd.gauss(0, 0.06) for _ in range(3))
        mp.add_samples([(int(t * 1e6), (0, 0, 1), g)], t)
    assert mp.calibrated and mp.gyro_noise < 0.1, mp.gyro_noise
    bias0 = list(mp.bias)
    for _ in range(1600):                             # 8 s virando a mão devagar
        t += 0.005
        g = (rnd.gauss(0, 0.4), rnd.gauss(0, 0.4), 3.0 + rnd.gauss(0, 0.4))
        mp.add_samples([(int(t * 1e6), (0, 0, 1), g)], t)
    assert abs(mp.bias[2] - bias0[2]) < 0.05, mp.bias


@caso
def calibrar_na_tecla():
    mp = motion.MotionProcessor({**CFG, "movimento": {**CFG["movimento"], "calibrar_parado": False}})
    mp.start_calibration(0.0)
    feed(mp, 500, (2.0, 1.0, -1.0))
    assert mp.last_calibration == "ok" and close(mp.bias, (2, 1, -1), 0.01)
    mp.start_calibration(10.0)
    t = 10.0
    for i in range(500):
        t = 10.0 + i * 0.005
        mp.add_samples([(int(t * 1e6), (0, 0, 1), (100.0 * (i % 2), 0, 0))], t)
    assert mp.last_calibration == "mexeu" and close(mp.bias, (2, 1, -1), 0.01)


@caso
def engasgo_segura_e_depois_zera():
    """Sem dados: repete o último movimento até segurar_ms, depois zera a
    rotação (a mira não sai girando se o controle cair)."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False
    t = feed(mp, 50, (0, -50.0, 0))
    assert mp.output(t + 0.05).gyro[0] > 49        # 50 ms depois: segura
    assert mp.output(t + 0.5).gyro == (0, 0, 0)     # 500 ms depois: zera
    assert close(mp.output(t + 0.5).accel, (0, -1, 0))


@caso
def suavizacao_tira_tremedeira_sem_atrasar():
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(5)
    mp.auto_cal = False
    t = 0.0
    sinal = []
    for i in range(400):
        t = i * 0.005
        mp.add_samples([(int(t * 1e6) + 1, (0, 0, 1), (3.0 * (-1) ** i, 0, 0))], t)
        sinal.append(abs(mp._out_gyro[0]))
    assert max(sinal[100:]) < 1.0, "tremedeira de ±3°/s deveria cair bem"
    for i in range(400, 440):                      # movimento rápido de verdade
        t = i * 0.005
        mp.add_samples([(int(t * 1e6) + 1, (0, 0, 1), (300.0, 0, 0))], t)
    assert mp._out_gyro[0] > 280, mp._out_gyro      # em 200 ms já chegou


@caso
def pacote_manda_a_mais_nova_sem_perder_giro():
    """3 leituras num pacote: vai a mais nova (menos atraso) e o giro total
    que o Dolphin soma continua batendo (a conta de ângulo devolve o resto)."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False
    feed(mp, 30, (0, 0, 0))
    mp.output(0.0)
    mp.add_samples([(1_000_000, (0, 0, 1), (0, 0, 0)), (1_000_000, (0, 0, 1), (0, 0, 0)),
                    (1_000_000, (0, 0, 1), (0, -300.0, 0))], 0.0)
    assert mp.output(0.0).gyro[0] > 299                # a mais nova, na hora
    sent, t = 0.0, 0.0
    for k in range(40):                                # depois, parado
        if k % 3 == 0 and k:
            mp.add_samples([(1_000_000 + k * 5000, (0, 0, 1), (0, 0, 0))] * 3, t)
        sent += mp.output(t).gyro[0] * 0.005
        t += 0.005
    assert abs(sent - 300 * 0.005) < 0.05, sent        # 1,5° de verdade


@caso
def calibracao_quebrada_do_driver():
    """Tudo exatamente zero = driver leu a calibração errada (aconteceu com o
    Joy-Con R em 2026-10-07: 'inaccurate IMU divisors (5)')."""
    mp = motion.MotionProcessor(CFG)
    feed(mp, 500, (0, 0, 0), accel=(0, 0, 0))
    assert mp.broken_driver_cal
    feed(mp, 5, (0, 0, 0), accel=(0, 0, 1), start=3)
    assert mp.broken_driver_cal                      # só o giroscópio zerado também conta
    feed(mp, 5, (0.02, -0.01, 0), accel=(0, 0, 1), start=4)
    assert not mp.broken_driver_cal
    # 2026-10-10: Joy-Con R com "inaccurate IMU divisors (1)": acelerômetro
    # funcionando, giroscópio sempre zero → antes passava batido
    mp = motion.MotionProcessor(CFG)
    feed(mp, 500, (0, 0, 0), accel=(0.25, -0.26, 0.92))
    assert mp.broken_driver_cal


@caso
def pegada_livre_em_pe():
    """Como no Switch: segurando em pé (gatilhos pro teto, botões pra você),
    recentralizar faz o Dolphin ver um Wii Remote deitado apontando pra TV:
    virar pros lados = yaw, inclinar a ponta pra TV = mira desce."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False
    up = (1.0, 0.0, 0.0)                             # em pé: o "cima" é o eixo X
    feed(mp, 100, (0, 0, 0), accel=up)
    assert not close(mp.output(1.0).accel, (0, -1, 0))
    mp.recenter(1.0)
    assert close(mp.output(1.0).accel, (0, -1, 0), 1e-2), mp.output(1.0).accel
    assert mp.recentering(1.5) and not mp.recentering(2.1)   # segura o Recenter 1 s
    # virar pra direita = girar em volta do "cima" (−X)
    mp.add_samples([(10_000_000, up, (-50, 0, 0))], 2.0)
    pitch, yaw, roll = mp.output(2.0).gyro
    assert close((pitch, yaw, roll), (0, 50, 0), 1e-2), (pitch, yaw, roll)
    # ponta pra TV (pras costas, −Z) = giro em +Y → mira desce
    mp.add_samples([(10_005_000, up, (0, 40, 0))], 2.6)
    pitch, yaw, roll = mp.output(2.6).gyro          # (0,6 s depois: conta nova)
    assert close((pitch, yaw, roll), (-40, 0, 0), 1e-2), (pitch, yaw, roll)
    # recentralizar de novo na mesma pegada não segura o botão de novo
    mp.recenter(3.0)
    assert not mp.recentering(3.0)
    # o pad do Wii Remote fica com o Recenter apertado enquanto isso
    from types import SimpleNamespace
    r = FakeCtrl("R", "AA:00:00:00:00:02")
    r.motion = mp
    mp.recenter_until = 10.0
    for mode in (players.UPRIGHT, players.PAIR):
        p = SimpleNamespace(online=True, mode=mode, members={"R": r})
        assert players.build_pads(p, 5.0)[0].buttons["r3"], mode
        assert not players.build_pads(p, 11.0)[0].buttons["r3"], mode
    # desligada: nada muda
    cfg = {**CFG, "movimento": {**CFG["movimento"], "pegada_livre": False}}
    mp2 = motion.MotionProcessor(cfg)
    feed(mp2, 100, (0, 0, 0), accel=up)
    mp2.recenter(1.0)
    assert mp2.grip is None and not mp2.recentering(1.0)


def _vira(st, deg, rate=100.0):
    """Gira o controle deg graus pra direita (negativo = esquerda) em volta do
    "cima" e soma o que o Dolphin recebe de yaw pra direita."""
    w = [0.0, 0.0, 0.0]
    w[st["axis"]] = -rate if deg > 0 else rate     # direita = −giro em volta do "cima"
    steps = round(abs(deg) / rate / 0.005) if rate else round(deg / 0.005)  # rate 0: deg = segundos
    for _ in range(steps):
        st["ts"] += 5000
        st["t"] += 0.005
        st["mp"].add_samples([(st["ts"], st["up"], tuple(w))], st["t"])
        st["sent"] += st["mp"].output(st["t"]).gyro[1] * 0.005


@caso
def ponteiro_sai_da_tela_e_volta_como_no_wii():
    """Opção escolhida jogando (2026-10-11): como no Wii de verdade. O Dolphin recebe
    o giro inteiro (jogos com MotionPlus precisam dele) e o ponteiro volta
    no mesmo lugar; só o desenho da janela fica preso na borda. Vale também
    com o Joy-Con em pé."""
    for up, axis in (((0.0, 0.0, 1.0), 2), ((1.0, 0.0, 0.0), 0)):
        mp = motion.MotionProcessor(CFG)
        mp.set_smoothing(0)
        mp.auto_cal = False
        feed(mp, 50, (0, 0, 0), accel=up)
        mp.recenter(0.3)
        st = {"mp": mp, "up": up, "axis": axis, "sent": 0.0, "t": 1.0, "ts": 20_000_000}
        mp.output(st["t"])                            # o Dolphin já está recebendo (parado)
        _vira(st, 40)                                 # 40° pra direita: sai da tela (borda 20°)
        assert abs(st["sent"] - 40) < 0.5, st["sent"]   # giro inteiro, sem cortar
        assert mp.aim()[0] > 1.0                      # desenho: fora da tela
        _vira(st, -15)                                # volta 15°: ainda fora
        assert mp.aim()[0] > 1.0
        _vira(st, -25)                                # volta o resto: meio de novo
        _vira(st, 0.2, rate=0.0)
        assert abs(st["sent"]) < 0.5, (up, st["sent"])
        assert abs(mp.aim()[0]) < 0.03
    assert motion.POINTER_TOTAL_YAW == 360            # o Dolphin nunca prende (nem arrasta)


def _simula_bluetooth(lost=(), late=None):
    """Joy-Con girando (pitch, ida e volta) com pacotes de 3 leituras a cada
    15 ms; lost: pacotes que se perdem; late: (pacote, atraso s) — ele e os
    seguintes chegam embolados. O "Dolphin" soma a rotação a cada 5 ms.
    Devolve (ângulo de verdade, ângulo no Dolphin) no fim, parado."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False

    def rate(t):                                      # °/s: sobe e volta irregular, depois para
        if t >= 1.2:
            return 0.0
        return 120 * math.sin(2 * math.pi * t / 0.9) + 40 * math.sin(2 * math.pi * t / 0.37)
    arrivals = []
    for k in range(106):                              # 1,6 s de pacotes (parado continua mandando)
        if k in lost:
            continue
        t_arr = 0.015 * (k + 1)
        if late and late[0] <= k and t_arr < 0.015 * (late[0] + 1) + late[1]:
            t_arr = 0.015 * (late[0] + 1) + late[1] + 0.0005 * (k - late[0])
        if arrivals:                                  # Bluetooth entrega na ordem
            t_arr = max(t_arr, arrivals[-1][0] + 0.0005)
        reads = [(0.015 * k + 0.005 * j + 0.0025) for j in range(3)]
        arrivals.append((t_arr, reads))
    angle, ang = {}, 0.0                              # ângulo (graus) em cada leitura
    for j in range(318):
        ang += rate(0.005 * j + 0.0025) * 0.005
        angle[j] = ang
    true = -ang

    def accel(r):                                     # gravidade gira junto com o controle
        th = math.radians(angle[round((r - 0.0025) / 0.005)])
        return (-math.sin(th), 0.0, math.cos(th))
    feed(mp, 20, (0, 0, 0))
    t, sent, i = 0.0, 0.0, 0
    mp.output(t)
    while t < 1.6:
        while i < len(arrivals) and arrivals[i][0] <= t + 1e-9:
            t_arr, reads = arrivals[i]
            ts = int(10_000_000 + t_arr * 1e6)
            mp.add_samples([(ts, accel(r), (0, rate(r), 0)) for r in reads], t)
            i += 1
        sent += mp.output(t).gyro[0] * 0.005
        t += 0.005
    return true, sent


@caso
def mira_nao_anda_quando_o_bluetooth_engasga():
    """2026-10-10: o R perde ~7 pacotes seguidos de vez em quando (sinal
    fraco). O ângulo que chega no Dolphin tem que bater com o de verdade."""
    true, sent = _simula_bluetooth()
    assert abs(true - sent) < 0.3, (true, sent)                  # sem engasgo
    true, sent = _simula_bluetooth(lost=range(20, 27))           # 105 ms perdidos girando
    assert abs(true - sent) < 1.5, (true, sent)
    true, sent = _simula_bluetooth(late=(30, 0.06))              # 60 ms atrasado, sem perder
    assert abs(true - sent) < 0.3, (true, sent)


@caso
def desliga_controle_parado():
    """Como o Switch dormindo: Dolphin fechado e ninguém mexendo por 10 min
    → desconecta. Com o Dolphin aberto (ou analógico mexendo) não."""
    from types import SimpleNamespace

    from app import server as S
    pedidos = []

    async def reconnect(mac, reason):
        pedidos.append((mac, reason))
    c = FakeCtrl("R", "AA:00:00:00:00:02")
    c.complete, c.connected_at, c.last_active = True, 0.0, None
    fake = SimpleNamespace(cfg=CFG, dolphin_connected=False, controllers={c.mac: c},
                           bt=SimpleNamespace(searching=False, reconnect=reconnect))

    async def roda(now):
        S.Server._power_off_idle(fake, now)
        await asyncio.sleep(0)
    asyncio.run(roda(9 * 60))
    assert not pedidos                                # 9 min: ainda não
    fake.dolphin_connected = True
    asyncio.run(roda(11 * 60))
    assert not pedidos                                # jogando: nunca
    fake.dolphin_connected = False
    c.sticks["main"] = (0.8, 0.0)
    asyncio.run(roda(11 * 60))
    assert not pedidos and c.last_active == 11 * 60   # mexeu no analógico
    c.sticks["main"] = (0.0, 0.0)
    asyncio.run(roda(22 * 60))
    assert len(pedidos) == 1 and pedidos[0][0] == c.mac, pedidos
    asyncio.run(roda(22 * 60 + 1))
    assert len(pedidos) == 1                          # não pede de novo em seguida


@caso
def le_a_cor_do_joycon():
    """Cor gravada no Joy-Con (SPI 0x6050), pedida pelo hidraw. Bytes
    conferidos com Joy-Cons de verdade em 2026-10-11: L = 0A B9 E6
    (azul neon), R = FF 3C 28 (vermelho neon)."""
    from app import colors, gui
    req = colors.spi_read_request(17, 0x6050, 12)
    assert req == bytes([0x01, 0x01, 0, 1, 0x40, 0x40, 0, 1, 0x40, 0x40,
                         0x10, 0x50, 0x60, 0, 0, 12])
    reply = bytearray(49)
    reply[0], reply[13], reply[14] = 0x21, 0x90, 0x10
    reply[15:20] = bytes([0x50, 0x60, 0, 0, 12])
    reply[20:26] = bytes([0x0A, 0xB9, 0xE6, 0x00, 0x1E, 0x1E])
    data = colors.parse_spi_reply(bytes(reply), 0x6050, 12)
    assert colors.colors_from_spi(data) == {"body": [10, 185, 230], "buttons": [0, 30, 30]}
    reply[14] = 0x30                                   # resposta de outro pedido: ignora
    assert colors.parse_spi_reply(bytes(reply), 0x6050, 12) is None
    assert colors.parse_spi_reply(bytes(49), 0x6050, 12) is None   # relatório comum (0x30…)
    assert colors.colors_from_spi(bytes([0xFF] * 12)) is None      # memória vazia
    c = {"kind": "R", "color": {"body": [255, 60, 40], "buttons": [30, 10, 10]}}
    assert close(gui.body_color(c), (1.0, 60 / 255, 40 / 255))
    assert gui.body_color({"kind": "L"}) == gui.JOYCON_L        # sem cor lida: a padrão


def _rodrigues(w_deg, dt):
    """Matriz do giro w (°/s, eixos do controle) em dt segundos."""
    ang = math.radians(math.sqrt(sum(x * x for x in w_deg))) * dt
    if ang < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    n = math.sqrt(sum(x * x for x in w_deg))
    x, y, z = (v / n for v in w_deg)
    c, s_, C = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    return [[c + x * x * C, x * y * C - z * s_, x * z * C + y * s_],
            [y * x * C + z * s_, c + y * y * C, y * z * C - x * s_],
            [z * x * C - y * s_, z * y * C + x * s_, c + z * z * C]]


def _gira_de_verdade(mp, R, rate_fn, t_end):
    """Controle girando com velocidade rate_fn(t) (°/s, eixos do controle),
    pacotes de 3 leituras a cada 15 ms e acelerômetro coerente com a
    orientação R (corpo → mundo). Devolve R e (pitch, yaw) somados no Dolphin."""
    t, k, sp, sy = 0.0, 0, 0.0, 0.0
    while t < t_end:
        if t >= 0.015 * (k + 1) - 1e-9:
            reads = []
            for j in range(3):
                w = rate_fn(0.015 * k + 0.005 * j)
                d = _rodrigues(w, 0.005)
                R = [[sum(R[i][m] * d[m][c] for m in range(3)) for c in range(3)] for i in range(3)]
                up = (R[2][0], R[2][1], R[2][2])        # cima do mundo visto do corpo
                reads.append((int(1e7 + 0.015 * (k + 1) * 1e6), up, tuple(w)))
            mp.add_samples(reads, t)
            k += 1
        g = mp.output(t).gyro
        sp += g[0] * 0.005
        sy += g[1] * 0.005
        t += 0.005
    return R, (sp, sy)


@caso
def diagonal_rapida_sai_reta_e_sem_atraso():
    """2026-10-11: diagonal rápida fazia curva / virava horizontal na mira
    da janela, e o movimento estava mais atrasado. Giro rápido passa direto
    pelo filtro e o desenho mede no espaço do jogador."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(3)
    mp.auto_cal = False
    feed(mp, 30, (0, 0, 0))
    mp.output(0.0)
    mp.reset_aim()

    def rate(t):                                     # 0,1 s: 7,5° pra cima e 5° pra direita
        s = 150 * math.sin(math.pi * t / 0.1) ** 2 if t < 0.1 else 0.0
        return (0.0, -s, -s * 2 / 3)
    R0 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    _, (sp, sy) = _gira_de_verdade(mp, R0, rate, 0.4)
    assert abs(sp - 7.5) < 0.1 and abs(sy - 5.0) < 0.1, (sp, sy)   # Dolphin: exato
    x, y = mp.aim()
    up_deg, right_deg = y * motion.AIM_LIMIT_Y, x * motion.AIM_LIMIT
    assert abs(up_deg - 7.5) < 0.4 and abs(right_deg - 5.0) < 0.4, (up_deg, right_deg)


@caso
def diagonal_com_o_joycon_tombado_na_mao():
    """Gravação de 2026-10-11: segurando o Joy-Con tombado ~50° pro
    lado. Girando em volta do eixo da face (Z), a ponta anda na diagonal no
    espaço — pelos eixos do controle parecia só "pro lado" e o desenho
    mostrava horizontal. No espaço do jogador sai diagonal."""
    roll = math.radians(50)                          # tombado: cima = (0, −sen, cos)
    R0 = [[1.0, 0.0, 0.0], [0.0, math.cos(roll), -math.sin(roll)],
          [0.0, math.sin(roll), math.cos(roll)]]
    up = (R0[2][0], R0[2][1], R0[2][2])
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False
    feed(mp, 60, (0, 0, 0), accel=up)
    mp.output(0.0)
    mp.reset_aim()

    def rate(t):                                     # 10° em volta da face, em 0,2 s
        return (0.0, 0.0, -100.0) if t < 0.1 else (0.0, 0.0, 0.0)
    _gira_de_verdade(mp, R0, rate, 0.3)
    x, y = mp.aim()
    right_deg, up_deg = x * motion.AIM_LIMIT, y * motion.AIM_LIMIT_Y
    # no espaço: 10° × cos 50° pro lado e 10° × sen 50° na altura
    assert abs(right_deg - 10 * math.cos(roll)) < 0.5, (right_deg, up_deg)
    assert abs(abs(up_deg) - 10 * math.sin(roll)) < 0.5, (right_deg, up_deg)


@caso
def volta_pro_meio_por_outro_caminho():
    """2026-10-11: saindo da tela na diagonal e voltando pro meio, o ponto
    da janela às vezes voltava errado (−8° numa gravação). Somar "giro em
    volta da gravidade" depende do caminho; a orientação completa não: o
    controle de volta na mesma posição = ponto no meio."""
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    mp.auto_cal = False
    feed(mp, 30, (0, 0, 0))
    mp.output(0.0)
    mp.reset_aim()
    R0 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

    def ida(t):                                      # 40° pra direita, depois 25° pra cima
        if t < 0.2:
            return (0.0, 0.0, -200.0)
        if t < 0.325:
            return (0.0, -200.0, 0.0)
        return (0.0, 0.0, 0.0)
    R, _ = _gira_de_verdade(mp, R0, ida, 0.4)
    assert mp.aim()[0] > 1.0                         # fora da tela
    # volta por um caminho só (um giro em volta de um eixo inclinado)
    tr = R[0][0] + R[1][1] + R[2][2]
    ang = math.degrees(math.acos(max(-1.0, min(1.0, (tr - 1) / 2))))
    axis = (R[2][1] - R[1][2], R[0][2] - R[2][0], R[1][0] - R[0][1])
    n = math.sqrt(sum(v * v for v in axis))
    back = tuple(-v / n * ang / 0.2 for v in axis)  # desfaz em 0,2 s (eixos do controle)

    def volta(t):
        return back if t < 0.2 else (0.0, 0.0, 0.0)
    _gira_de_verdade(mp, R, volta, 0.3)
    x, y = mp.aim()
    assert abs(x) < 0.03 and abs(y) < 0.03, (x, y)  # no meio de novo


@caso
def jogador_desconectado_esconde_o_ponteiro():
    """2026-10-11: um 3º Joy-Con virou jogador 2 e caiu; o Wii Remote 2
    continuou no Dolphin com o ponteiro parado no meio da tela. Enquanto o
    jogador espera o controle voltar, o Wii Remote dele aponta pro teto
    (gravidade ao longo da frente → ponteiro fora da tela)."""
    from types import SimpleNamespace
    for mode, macs, frente in ((players.UPRIGHT, {"L": "x"}, (0.0, 0.0, 1.0)),
                               (players.PAIR, {"R": "x", "L": "y"}, (0.0, 0.0, 1.0)),
                               (players.SOLO, {"R": "x"}, None)):
        p = SimpleNamespace(online=False, mode=mode, macs=macs, members={})
        main, nun = players.build_pads(p, 1.0)
        assert main.connected and not nun.connected
        assert close(main.motion.gyro, (0, 0, 0))
        if frente:
            assert close(main.motion.accel, frente), main.motion.accel   # frente = cima
        else:                                          # deitado: o Dolphin gira sozinho
            assert abs(sum(v * v for v in main.motion.accel) - 1.0) < 1e-6
    assert not players.build_pads(None, 1.0)[0].connected
    # app reiniciado: sem jogador 2, mas o Dolphin ainda tem o Wii Remote 2
    from app import server as S
    fake = SimpleNamespace(players=SimpleNamespace(players={}), dolphin_slots={1, 2})
    main, _ = S.Server._pads(fake, 2, 1.0)
    assert main.connected and close(main.motion.accel, (0, 0, 1))
    assert not S.Server._pads(fake, 3, 1.0)[0].connected   # nem configurado: vazio


@caso
def mira_lenta_sem_atraso():
    """2026-10-11: a suavização atrasava 20–25 ms a mira lenta (3–6 °/s, a
    de escolher botão no Wii Menu) no nível 3. Agora ≤ 10 ms."""
    f = motion.TieredSmoothing(*motion.filter_params(3)["gyro"])
    out, inp = [], []
    for i in range(1000):
        v = 5 * math.sin(2 * math.pi * i * 0.005)
        out.append(f([v, 0, 0], 0.005)[0])
        inp.append(v)
    lag = min(range(40), key=lambda d: sum((out[i] - inp[i - d]) ** 2 for i in range(200, 1000)))
    assert lag * 5 <= 10, lag * 5


@caso
def vibracao_grave_direta():
    """Vibração direta pelo hidraw a ~150 Hz (como o SDL): pacote 0x10 com os
    dois lados iguais, repetido a cada 50 ms enquanto vibra e "parar" no fim."""
    from app import hdrumble as H
    pk = H.packet(3, 0.4)
    assert len(pk) == H.PACKET_LEN and pk[:10].hex() == "100374743d5d74743d5d"
    assert H.encode(0) == H.NEUTRAL and H.encode(1.0) == bytes([0x74, 0xC8, 0x3D, 0x72])
    r, w = os.pipe()
    os.set_blocking(r, False)
    d = H.DirectRumble("teste", "grave")
    d.fd = w

    async def roda():
        d.play(0.5, length_ms=120)
        await asyncio.sleep(0.2)
    asyncio.run(roda())
    data = os.read(r, 4096)
    pkts = [data[i:i + H.PACKET_LEN] for i in range(0, len(data), H.PACKET_LEN)]
    vib = [p for p in pkts if p[2:6] != H.NEUTRAL]
    stop = [p for p in pkts if p[2:6] == H.NEUTRAL]
    assert len(vib) >= 2, len(vib)                   # mandou e repetiu
    assert len(stop) == H.STOP_REPEAT and pkts[-1][2:6] == H.NEUTRAL   # parou sozinho
    assert [p[1] for p in pkts] == [i & 0xF for i in range(len(pkts))]  # contador
    os.close(r)
    os.close(w)


@caso
def vibracao_motor_embala_e_para_devagar():
    """Estilo "motor" (imita o Wii Remote): faixa grave + aguda no dobro da
    frequência e mais fraca; embala em ~40 ms, segura, e para em ~60 ms
    antes do "parar" — em vez de ligar e desligar seco."""
    from app import hdrumble as H
    # as duas faixas independentes: grave a 160 Hz forte, aguda a 320 Hz fraca
    side = H.motor_side(1.0, 1.0)
    assert side[2] & 0x7F == H._freq(160)[1] and side[0] | (side[1] & 1) << 8 == H._freq(320)[0]
    assert side[1] & 0xFE < 2 * 100 and side[3] == 0x72      # aguda mais fraca, grave no máximo
    assert H.motor_side(0.5, 0.0)[2] & 0x7F == H._freq(110)[1]  # embalando: mais grave
    r, w = os.pipe()
    os.set_blocking(r, False)
    d = H.DirectRumble("teste")
    d.fd = w

    async def roda():
        d.play(0.8, length_ms=200)
        await asyncio.sleep(0.4)
    asyncio.run(roda())
    data = os.read(r, 8192)
    pkts = [data[i:i + H.PACKET_LEN] for i in range(0, len(data), H.PACKET_LEN)]
    lows = [p[5] & 0x7F if p[2:6] != H.NEUTRAL else None for p in pkts]   # força da grave
    vib = [v for v in lows if v is not None]
    top = max(vib)
    up = vib[:vib.index(top) + 1]
    assert len(up) >= 3 and up == sorted(up), up                 # embalou aos poucos
    down = vib[len(vib) - vib[::-1].index(top) - 1:]
    assert len(down) >= 3 and down == sorted(down, reverse=True), down   # parou devagar
    assert lows[-H.STOP_REPEAT:] == [None] * H.STOP_REPEAT       # e mandou "parar"
    assert [p[1] for p in pkts] == [i & 0xF for i in range(len(pkts))]
    assert d.force == 0 and d._refresh is None
    os.close(r)
    os.close(w)


@caso
def inclinacao_aguenta_golpe_rapido():
    """Golpe de cotovelo (ponta a 30 cm): o acelerômetro sente o empurrão do
    braço. A inclinação calculada não pode entortar (antes: até 6,6° num
    golpe de 300 ms)."""
    def mul(A, B):
        return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    def mv(A, v):
        return [sum(A[i][k] * v[k] for k in range(3)) for i in range(3)]

    def tr(A):
        return [[A[j][i] for j in range(3)] for i in range(3)]
    for dur in (0.3, 0.15, 0.08):
        mp = motion.MotionProcessor(CFG)
        mp.set_smoothing(0)
        mp.auto_cal = False
        feed(mp, 60, (0, 0, 0))
        ax = (0.0, -math.radians(12), math.radians(-24))
        n = math.sqrt(sum(v * v for v in ax))
        ax = [v / n for v in ax]
        total = math.degrees(n)
        R = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        pp = vp = None
        worst, t, ts = 0.0, 0.0, 10_000_000
        while t < dur + 0.5:
            s = (math.sin(math.pi * t / dur) ** 2) * (2 / dur) if t < dur else 0.0
            om = [v * total * s for v in ax]                 # °/s no mundo
            R = mul(_rodrigues(om, 0.005), R)
            pos = mv(R, [0.3, 0.0, 0.0])
            vel = [(pos[i] - pp[i]) / 0.005 for i in range(3)] if pp else [0.0] * 3
            acc = [(vel[i] - vp[i]) / 0.005 for i in range(3)] if vp else [0.0] * 3
            pp, vp = pos, vel
            a = mv(tr(R), [acc[0] / 9.81, acc[1] / 9.81, acc[2] / 9.81 + 1])
            ts += 5000
            mp.add_samples([(ts, tuple(a), tuple(mv(tr(R), om)))], t)
            up_true, up = mv(tr(R), [0, 0, 1]), mp.up
            cosang = max(-1.0, min(1.0, sum(up[i] * up_true[i] for i in range(3))))
            worst = max(worst, math.degrees(math.acos(cosang)))
            t += 0.005
        assert worst < 1.0, (dur, worst)


@caso
def revisao_2026_10_11():
    """Achados da revisão: o DSU esquece o Dolphin fechado mesmo sem controle
    conectado; a zona morta não corta o eixo fraco de uma diagonal lenta."""
    from app import dsu
    srv = dsu.DsuServer("teste", 0, lambda s: players.PadState())
    srv.clients[("127.0.0.1", 1)] = {"slots": set(), "all": True, "t": time.monotonic()}
    assert srv.has_clients
    srv.clients[("127.0.0.1", 1)]["t"] -= dsu.CLIENT_TIMEOUT + 1   # Dolphin fechou
    assert not srv.has_clients                       # sem controle conectado também
    mp = motion.MotionProcessor(CFG)                 # zona morta 0,3 °/s
    mp.set_smoothing(0)
    mp.auto_cal = False
    feed(mp, 3, (0.25, 0.25, 0.0))                   # cada eixo < 0,3; juntos 0,35
    assert close(mp._out_gyro, (0.25, 0.25, 0.0)), mp._out_gyro
    feed(mp, 3, (0.2, 0.1, 0.0), start=1.0)          # juntos 0,22: parado
    assert mp._out_gyro == [0.0, 0.0, 0.0]


@caso
def forca_da_vibracao():
    """A força escolhida (0–100) multiplica o que o jogo pede."""
    from types import SimpleNamespace

    from app import server as S
    got = []
    r = SimpleNamespace(rumble=lambda *a: got.append(a))
    fake = SimpleNamespace(players=SimpleNamespace(players={1: SimpleNamespace(members={"R": r})}),
                           _rumbling={}, rumble_force=40)
    S.Server._game_rumble(fake, 1, 1.0, 0.5, 0)
    assert close(got[-1][:2], (0.4, 0.4)), got       # motor só, como o do Wii Remote
    fake.rumble_force = 0
    S.Server._game_rumble(fake, 1, 1.0, 0.5, 0)
    assert got[-1][:2] == (0.0, 0.0), got


# --- jogadores -----------------------------------------------------------
@caso
def lr_junta_um_jogador():
    pm = manager()
    left, right = FakeCtrl("L", "AA:00:00:00:00:01"), FakeCtrl("R", "AA:00:00:00:00:02")
    pm.controller_connected(left, 0)
    pm.controller_connected(right, 0)
    left.buttons.add("L")
    pm.buttons_changed(left, 1)
    assert not pm.players, "só um lado apertado não junta"
    right.buttons.add("ZR")
    pm.buttons_changed(right, 1.2)
    p = pm.players[1]
    assert p.mode == players.PAIR and p.members == {"R": right, "L": left}
    assert not pm.waiting


@caso
def sl_sr_deitado_e_botoes_girados():
    pm = manager()
    r = FakeCtrl("R", "AA:00:00:00:00:03")
    pm.controller_connected(r, 0)
    r.buttons |= {"SL", "SR"}
    pm.buttons_changed(r, 1)
    assert pm.players[1].mode == players.SOLO
    r.buttons = {"X", "SR"}
    r.sticks["main"] = (0.0, 1.0)            # analógico pra "cima" do Joy-Con em pé
    main, nun = players.build_pads(pm.players[1], 2)
    # direito deitado gira no sentido horário: X (em cima) fica à direita.
    # Mapeamento universal (mesmos campos do par): botão da direita = 2
    # (square), SR = B (cross); analógico vira as setas
    assert main.buttons["square"] and main.buttons["cross"] and not main.buttons["circle"]
    assert main.buttons["right"] and not main.buttons["up"]   # "cima" do Joy-Con = direita
    assert main.lstick == (0.0, 0.0) and not nun.connected
    assert players.signals(pm.players[1]) == (False, True, False)   # deitado, sem GameCube
    left = FakeCtrl("L", "AA:00:00:00:00:04")
    pm.controller_connected(left, 0)
    left.buttons |= {"SL", "SR"}
    pm.buttons_changed(left, 1)
    left.buttons = {"UP"}                     # esquerdo deitado: ↑ fica à esquerda = B
    left.sticks["main"] = (0.0, 1.0)
    main, _ = players.build_pads(pm.players[2], 2)
    assert main.buttons["cross"] and main.buttons["left"] and not main.buttons["square"]


@caso
def em_pe_e_alternar_no_cartao():
    """R+ZR num Joy-Con sozinho = Wii Remote em pé; clique alterna com deitado."""
    pm = manager()
    r = FakeCtrl("R", "AA:00:00:00:00:20")
    pm.controller_connected(r, 0)
    r.buttons = {"R"}
    pm.buttons_changed(r, 1)
    assert not pm.players, "só R (sem ZR) é o gesto de juntar, não de em pé"
    r.buttons = {"R", "ZR"}
    pm.buttons_changed(r, 1)
    assert pm.players[1].mode == players.UPRIGHT
    r.buttons, r.sticks["main"] = {"A", "ZR", "SL"}, (0.0, 1.0)
    main, nun = players.build_pads(pm.players[1], 2)
    assert main.buttons["circle"] and main.buttons["r2"] and main.buttons["share"]
    assert main.rstick == (0.0, 1.0) and not nun.connected   # analógico = direcional
    pm.toggle_single_mode(1)
    assert pm.players[1].mode == players.SOLO
    pm.toggle_single_mode(1)
    assert pm.players[1].mode == players.UPRIGHT
    assert players.PlayerManager(CFG).memory[r.mac]["modo"] == players.UPRIGHT
    left = FakeCtrl("L", "AA:00:00:00:00:21")
    pm.controller_connected(left, 0)
    left.buttons = {"L", "ZL"}
    pm.buttons_changed(left, 1)
    assert pm.players[2].mode == players.UPRIGHT
    left.buttons = {"RIGHT"}
    main, _ = players.build_pads(pm.players[2], 2)
    assert main.buttons["circle"]                           # seta da direita = A


@caso
def junta_jogadores_sozinhos_so_procurando():
    """Como na tela de controles do Switch: procurando, L + R junta dois
    Joy-Cons que já eram jogadores em pé; no jogo, não."""
    pm = manager()
    left, right = FakeCtrl("L", "AA:00:00:00:00:30"), FakeCtrl("R", "AA:00:00:00:00:31")
    for c, gesto in ((left, {"L", "ZL"}), (right, {"R", "ZR"})):
        pm.controller_connected(c, 0)
        c.buttons = gesto
        pm.buttons_changed(c, 1)
    assert [p.mode for p in pm.players.values()] == [players.UPRIGHT, players.UPRIGHT]
    left.buttons, right.buttons = {"L"}, {"R"}
    pm.buttons_changed(right, 2)                     # no jogo: continua separado
    assert len(pm.players) == 2
    pm.buttons_changed(right, 2, regroup=True)       # procurando: junta
    assert list(pm.players) == [1], pm.players
    p = pm.players[1]
    assert p.mode == players.PAIR and p.members == {"R": right, "L": left}
    assert left.player is p and right.player is p

    # botão "Juntar" no cartão: separa de novo (recomeçar) e junta pelo botão
    pm.reset([left, right])
    for c, gesto in ((right, {"R", "ZR"}), (left, {"L", "ZL"})):
        c.buttons = gesto
        pm.buttons_changed(c, 3)
    assert pm.players[1].members == {"R": right} and pm.players[2].members == {"L": left}
    pm.join_single(2)
    assert list(pm.players) == [1] and pm.players[1].mode == players.PAIR
    pm.join_single(1)                                # já é par: não faz nada
    assert pm.players[1].complete
    pm.split_pair(1)                                 # botão "Separar"
    assert pm.players[1].members == {"R": right} and pm.players[1].mode == players.UPRIGHT
    assert pm.players[2].members == {"L": left} and pm.players[2].mode == players.UPRIGHT
    assert left.player is pm.players[2] and pm.players[1].macs == {"R": right.mac}
    pm.join_single(1)                                # e junta de novo
    assert list(pm.players) == [1] and pm.players[1].mode == players.PAIR


@caso
def controle_mudo_e_reconectado():
    """Conectado mas sem mandar nada (o R travou ao juntar, 2026-10-10)."""
    from app import server as S
    c = FakeCtrl("R", "AA:00:00:00:00:02")
    c.complete, c.connected_at = True, 0.0
    assert not S.is_mute(c, 3.0)                     # acabou de conectar
    assert S.is_mute(c, 6.0)                         # 6 s sem nada
    c.motion.add_samples([(5000, (0, 0, 1), (0.1, 0, 0))], 6.0)
    assert not S.is_mute(c, 7.0)
    assert S.is_mute(c, 11.5)


@caso
def mira_da_janela_segue_o_giro():
    """A mira desenhada segue a orientação (como o ponteiro do Dolphin),
    passa da borda quando aponta pra fora e centraliza no analógico."""
    m = motion.MotionProcessor(CFG)
    m.set_smoothing(0)
    m.auto_cal = False
    # girando pra direita (yaw right = −giro em Z) 60°/s por 51 amostras de 5 ms
    m.add_samples([(5000 * (i + 1), (0, 0, 1), (0, 0, -60)) for i in range(51)], 0)
    x, y = m.aim()
    # (a 1ª leitura só diz a posição inicial: contam 50 × 5 ms = 15°)
    assert abs(x - 60 * 0.005 * 50 / motion.AIM_LIMIT) < 0.01 and abs(y) < 0.01, (x, y)
    m.add_samples([(5000 * (i + 52), (0, 0, 1), (0, 0, -60)) for i in range(200)], 1)
    assert m.aim()[0] > 1.0                          # passou da borda: fora da tela
    assert m.aim(motion.SIDEWAYS_R)[0] > 1.0        # deitado: virar pros lados é o mesmo eixo
    from app import gui
    # fora da tela: anel na borda na direção certa, sem escorregar pela borda (L)
    assert gui.aim_on_screen((2.0, 1.0)) == ((1.0, 0.5), True)
    assert gui.aim_on_screen((0.5, -0.2)) == ((0.5, -0.2), False)
    m.reset_aim()
    # levantar a ponta (giro em Y) é pitch em pé, mas roll deitado: mira não sobe
    m.add_samples([(5000 * (i + 260), (0, 0, 1), (0, -60, 0)) for i in range(50)], 2)
    assert m.aim()[1] > 0.4 and abs(m.aim(motion.SIDEWAYS_R)[1]) < 0.01, m.aim()
    m.reset_aim()
    assert m.aim() == (0.0, 0.0)


@caso
def busca_para_sozinha_sem_achar():
    from app import bluetooth as B
    bt = B.Bluetooth()
    bt.adapter, bt.powered, bt.idle_stop_s = "/org/bluez/hci0", True, 60

    async def fake_call(*a, **k):
        return []
    bt._call = fake_call
    asyncio.run(bt.search(float("inf")))
    left = bt.searching_until - time.monotonic()
    assert 59 < left <= 60, left                     # sem limite, mas para em 1 min
    bt.searching_until = time.monotonic() + 5        # faltando 5 s, achou um controle
    bt._extend_search()
    assert bt.searching_until - time.monotonic() > 59
    asyncio.run(bt.search(10))                       # pediu menos que o limite: vale o pedido
    assert bt.searching_until - time.monotonic() <= 10
    bt.idle_stop_s = 0
    asyncio.run(bt.search(float("inf")))
    assert bt.searching_until == float("inf")


@caso
def par_monta_os_dois_pads():
    pm = manager()
    left, right = FakeCtrl("L", "AA:00:00:00:00:05"), FakeCtrl("R", "AA:00:00:00:00:06")
    pm.controller_connected(left, 0)
    pm.controller_connected(right, 0)
    left.buttons, right.buttons = {"L"}, {"R"}
    pm.buttons_changed(right, 1)
    left.buttons, right.buttons = {"ZL", "DOWN", "-"}, {"A", "ZR", "HOME"}
    left.sticks["main"], right.sticks["main"] = (0.5, 0.5), (-1.0, 0.0)
    main, nun = players.build_pads(pm.players[1], 2)
    b = main.buttons
    assert b["circle"] and b["r2"] and b["ps"] and b["l2"] and b["down"] and b["share"]
    assert not b["cross"] and not b["l1"]
    assert main.lstick == (0.5, 0.5) and main.rstick == (-1.0, 0.0)
    assert nun.connected and main.connected and main.battery == 4


@caso
def reconecta_no_mesmo_jogador():
    pm = manager()
    a, b = FakeCtrl("L", "AA:00:00:00:00:07"), FakeCtrl("R", "AA:00:00:00:00:08")
    c, d = FakeCtrl("L", "AA:00:00:00:00:09"), FakeCtrl("R", "AA:00:00:00:00:0A")
    for x in (a, b, c, d):
        pm.controller_connected(x, 0)
    a.buttons, b.buttons = {"L"}, {"R"}
    pm.buttons_changed(b, 1)
    a.buttons, b.buttons = set(), set()
    c.buttons, d.buttons = {"L"}, {"R"}
    pm.buttons_changed(c, 1)
    assert set(pm.players) == {1, 2}
    pm.controller_gone(b)                    # cai o direito do jogador 1
    assert pm.players[1].online and not pm.players[1].complete
    b2 = FakeCtrl("R", b.mac)
    pm.controller_connected(b2, 5)
    assert pm.players[1].members["R"] is b2 and pm.players[1].complete

    # fechou e abriu o servidor: memória em data/jogadores.json
    # o número segue a ordem de chegada (como no Switch): d era o 2, chegou
    # primeiro, vira o 1; o parceiro lembrado (c) volta pro mesmo jogador
    pm2 = players.PlayerManager(CFG)
    d2 = FakeCtrl("R", d.mac)
    pm2.controller_connected(d2, 0)
    assert pm2.players[1].members == {"R": d2}
    c2 = FakeCtrl("L", c.mac)
    pm2.controller_connected(c2, 0)
    assert pm2.players[1].complete and pm2.players[1].mode == players.PAIR
    a2 = FakeCtrl("L", a.mac)
    pm2.controller_connected(a2, 0)
    assert pm2.players[2].members == {"L": a2} and pm2.players[2].macs["R"] == b.mac
    pm2.reset([c2, d2])
    assert not pm2.players and len(pm2.waiting) == 2


@caso
def junta_sozinho_quando_obvio():
    pm = manager()
    left, right = FakeCtrl("L", "AA:00:00:00:00:0B"), FakeCtrl("R", "AA:00:00:00:00:0C")
    pm.controller_connected(left, 10)
    pm.controller_connected(right, 11)
    pm.tick(12)
    assert not pm.players
    pm.tick(11 + CFG["jogadores"]["juntar_sozinho_segundos"])
    assert pm.players[1].mode == players.PAIR
    # com alguém já jogando não junta sozinho
    l2, r2 = FakeCtrl("L", "AA:00:00:00:00:0D"), FakeCtrl("R", "AA:00:00:00:00:0E")
    pm.controller_connected(l2, 20)
    pm.controller_connected(r2, 20)
    pm.tick(100)
    assert 2 not in pm.players


# --- leitura do /dev/input -----------------------------------------------
class PipeDevice(E.InputDevice):
    def __init__(self, fd):  # sem ioctl: só o formato dos eventos
        self.fd, self.name, self.uniq, self.path = fd, "teste", "", "pipe"


@caso
def le_eventos_do_imu():
    """Joy-Con manda 3 amostras por pacote; eixo que não mudou não vem de novo."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    ctrl = Controller("AA:00:00:00:00:10", "R", "0005:057E:2007.0001", CFG)
    ctrl.motion.set_smoothing(0)
    ctrl.imu = PipeDevice(r)
    ev = E.EVENT.pack
    # um pacote: 3 leituras chegando juntas, da mais NOVA pra mais velha
    # (giro X 30, 20, 10 °/s); o MSC_TIMESTAMP do driver é ignorado
    G = 14247
    data = b"".join([
        ev(5, 1000, E.EV_MSC, E.MSC_TIMESTAMP, 1000),
        ev(5, 1000, E.EV_ABS, E.ABS_Z, 4096), ev(5, 1000, E.EV_ABS, E.ABS_RX, G * 30),
        ev(5, 1000, E.EV_SYN, E.SYN_REPORT, 0),
        ev(5, 1010, E.EV_MSC, E.MSC_TIMESTAMP, 6000),
        ev(5, 1010, E.EV_ABS, E.ABS_RX, G * 20),
        ev(5, 1010, E.EV_SYN, E.SYN_REPORT, 0),
    ])
    os.write(w, data)
    ctrl.read_imu(1.0)
    assert ctrl.motion.last_sample_t is None           # pacote pela metade: espera
    os.write(w, b"".join([
        ev(5, 1020, E.EV_MSC, E.MSC_TIMESTAMP, 11000),
        ev(5, 1020, E.EV_ABS, E.ABS_RX, G * 10),
        ev(5, 1020, E.EV_SYN, E.SYN_REPORT, 0),
    ]))
    ctrl.read_imu(1.01)
    # desvirado: a última processada é a mais nova (30 °/s)
    assert close(ctrl.motion.raw_accel, (0, 0, 1)) and close(ctrl.motion.raw_gyro, (30, 0, 0))
    assert ctrl.motion.last_ts_us == 5_001_000
    os.close(w)
    os.close(r)


# --- movimento direto (rawimu) ---------------------------------------------
# Joy-Con (R) de verdade (gravação de 2026-10-12): calibração de fábrica e um
# pacote 0x30, com o que o hid-nintendo entregou pras 3 leituras (nova → velha).
CAL_R = [-205, -58, -383, 16384, 16384, 16384, 28, 4, 3, 15335, 15335, 15335]
PACOTE_R = bytes.fromhex(
    "30688e000000000000156876090d03a3fe79f0f9ff160009000e03a5fe7cf0f7ff130009000903a2fe7d"
    "f0f2ff13000700")
DRIVER_R = [(765, 353, 3884, -35064, -18004, -6001), (766, 351, 3884, -37067, -15003, -6001),
            (766, 351, 3884, -42076, -15003, -4000)]


@caso
def movimento_direto_bate_com_o_driver():
    from app import rawimu
    leituras = rawimu.Converter("R", CAL_R).samples(PACOTE_R)
    for (a, g), d in zip(leituras, reversed(DRIVER_R), strict=True):   # velha → nova
        assert close(a, [v / 4096 for v in d[:3]], 0.005), (a, d)     # fuzz do driver
        assert close(g, [v / 14247 for v in d[3:]], 0.002), (g, d)


@caso
def calibracao_do_usuario_ou_de_fabrica():
    from app import rawimu
    fabrica = struct.pack("<12h", *CAL_R)
    usuario = struct.pack("<12h", 10, 20, 30, 16000, 16000, 16000, 1, 2, 3, 13371, 13371, 13371)
    memoria = {}

    def le(hid, endereco, tamanho):
        return memoria.get(endereco)
    original = rawimu.read_spi
    rawimu.read_spi = le
    try:
        memoria.update({0x8026: b"\xff\xff" + b"\xff" * 24, 0x6020: fabrica})
        assert rawimu.read_calibration("x") == CAL_R                 # sem a do usuário
        memoria[0x8026] = b"\xb2\xa1" + usuario
        assert rawimu.read_calibration("x")[:3] == [10, 20, 30]      # a do usuário vale
        memoria[0x8026] = b"\xb2\xa1" + b"\x00" * 24                 # usuário esquisito
        assert rawimu.read_calibration("x") == CAL_R
        memoria[0x6020] = b"\xff" * 24                                # memória lida errada
        assert rawimu.read_calibration("x") is None
        del memoria[0x8026]                                           # não respondeu
        assert rawimu.read_calibration("x") is None
    finally:
        rawimu.read_spi = original
    rawimu.save({"AA:00:00:00:00:01": CAL_R, "AA:00:00:00:00:02": [0] * 12})
    assert rawimu.load() == {"AA:00:00:00:00:01": CAL_R}              # guardada, sem a ruim


@caso
def relogio_do_pacote_diz_o_que_se_perdeu():
    from app import rawimu
    r = rawimu.RawImu.__new__(rawimu.RawImu)
    r._timer = r._t = None
    t, perdas = 1.0, []
    for passo, depois in ((0, 0), (3, .015), (6, .030), (9, .015), (2, .010), (5, .020),
                          (3, .120), (3, 0)):
        t += depois
        timer = ((r._timer or 0) + passo) & 0xFF
        perdas.append(round(r.lost_before(timer, t), 4))
    assert perdas == [0, 0, .015, .03, 0, .01, 0, 0], perdas   # atrasado ≠ perdido
    # 1,5 s sem nada: o relógio deu a volta (256 × 5 ms = 1,28 s) — a hora decide
    timer = (r._timer + 300) & 0xFF
    assert round(r.lost_before(timer, t + 1.5), 3) == round((300 - 3) * 0.005, 3)


@caso
def meio_pacote_perdido_nao_tira_a_mira_do_lugar():
    # Sinal ruim: o R passa a mandar a cada 30 ms com relógio +6 (3 leituras
    # nunca chegam). Girando a 100 °/s por 1 s, o ângulo tem que dar 100°.
    mp = motion.MotionProcessor(CFG)
    mp.set_smoothing(0)
    t = 1.0
    for k in range(34):
        t += 0.030
        mp.add_samples([(int(t * 1e6), (0, 0, 1), (0, 100.0, 0))] * 3, t,
                       [0.015 if k else 0.0, 0.0, 0.0])
    andou = 0.005 * 3 * 34 + 0.015 * 33    # leituras que chegaram + as perdidas
    assert abs(mp.real_int[1] - 100 * andou) < 0.5, (mp.real_int, 100 * andou)


@caso
def controle_le_o_movimento_direto():
    from app import rawimu
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)   # um pacote por leitura
    a.setblocking(False)
    ctrl = Controller("AA:00:00:00:00:11", "R", "0005:057E:2007.0001", CFG)
    raw = rawimu.RawImu.__new__(rawimu.RawImu)
    raw.convert, raw.fd, raw._timer, raw._t = rawimu.Converter("R", CAL_R), a.fileno(), None, None
    ctrl.raw = raw
    resposta = bytes([0x21]) + bytes(48)                  # resposta de comando: ignorada
    b.send(PACOTE_R)
    b.send(resposta)
    b.send(PACOTE_R[:1] + bytes([(PACOTE_R[1] + 6) & 0xFF]) + PACOTE_R[2:])   # 1 pacote perdido
    ctrl.read_raw(1.0)
    assert ctrl.motion.last_sample_t == 1.0
    assert close(ctrl.motion.raw_gyro, [v / 14247 for v in DRIVER_R[0][3:]], 0.002)   # a mais nova
    assert ctrl.motion._nread == 6, ctrl.motion._nread    # 2 pacotes, sem a resposta
    real0 = ctrl.motion.real_int[0]
    assert real0 < -0.05 * 0.03 * 6, real0                # contou os 15 ms perdidos girando
    ctrl.raw = None
    a.close()
    b.close()


@caso
def calibracao_que_mudou_e_trocada_na_hora():
    # Recalibrou no Switch: a guardada liga o movimento direto na hora, e a
    # conferida depois de conectar substitui (sem reabrir nada).
    from app import rawimu
    from app import server as S
    nova = list(CAL_R)
    nova[9:12] = [13371, 13371, 13371]
    ctrl = Controller("AA:00:00:00:00:12", "R", "0005:057E:2007.0001", CFG)
    raw = rawimu.RawImu.__new__(rawimu.RawImu)
    raw.convert, raw.fd = rawimu.Converter("R", CAL_R), None
    ctrl.raw = raw

    class FakeServer:
        def __init__(self):
            self.controllers = {ctrl.mac: ctrl}
            self.colors = {ctrl.mac: {"body": [1, 2, 3], "buttons": [4, 5, 6]}}
            self.imu_cal = {ctrl.mac: list(CAL_R)}
        _save = staticmethod(S.Server._save)

    le, espera = rawimu.read_calibration, S.SPI_DELAY_S
    rawimu.read_calibration, S.SPI_DELAY_S = (lambda hid: nova), 0
    try:
        async def run():
            srv = FakeServer()
            srv._loop = asyncio.get_running_loop()
            await S.Server._fetch_spi(srv, ctrl)
            return srv
        srv = asyncio.run(run())
    finally:
        rawimu.read_calibration, S.SPI_DELAY_S = le, espera
    assert srv.imu_cal[ctrl.mac] == nova and rawimu.load()[ctrl.mac] == nova
    assert ctrl.raw.convert.gyro_k[0] == rawimu.Converter("R", nova).gyro_k[0]
    ctrl.raw = None


@caso
def vibracao_direta_para_quieta_se_o_controle_sai():
    from app.hdrumble import DirectRumble
    r, w = os.pipe()
    os.close(r)                        # escrever agora dá erro (como o controle que saiu)
    d = DirectRumble("x", "motor")
    d.fd = w
    d.level = d.force = d.peak = 1.0

    async def run():
        d._tick()                      # chamado pelo laço: não pode estourar
    asyncio.run(run())
    assert d.force == 0 and d._refresh is None
    os.close(w)


@caso
def sabe_o_que_falta_preparar_no_sistema():
    from app import sistema
    d = TMP / "sistema"
    d.mkdir(exist_ok=True)
    conf, regras = d / "input.conf", d / "70-easyjoycon-uaccess.rules"
    falta = sistema.missing(conf, regras, joycond=True)
    assert len(falta) == 3, falta                            # nada feito
    conf.write_text("[General]\n#ClassicBondedOnly=true\nClassicBondedOnly = false\n")
    regras.write_text("# easyjoycon-sistema 1\n")           # regras antigas: refazer
    assert sistema.missing(conf, regras, joycond=False) == ["faltam as permissões dos controles"]
    regras.write_text(f"# easyjoycon-sistema {sistema.RULES_VERSION}\nKERNEL==...\n")
    assert sistema.missing(conf, regras, joycond=False) == []
    # o script do botão bate com o que o app confere
    script = sistema.SCRIPT.read_text()
    assert f"VERSAO={sistema.RULES_VERSION}" in script and sistema.RULES_FILE.name in script


@caso
def sem_dolphin_instalado_avisa_em_vez_de_configurar():
    from app import dolphin
    env = os.environ.pop("EASYJOYCON_DOLPHIN_CONFIG")
    original = dolphin.launcher
    dolphin.launcher = lambda: None
    try:
        assert not dolphin.installed()
        try:
            dolphin.configure({}, 26760, 26761)
            raise AssertionError("configurou sem o Dolphin")
        except RuntimeError as e:
            assert "não está instalado" in str(e)
        assert "flatpak install flathub org.DolphinEmu.dolphin-emu" in dolphin.INSTALL_HELP
    finally:
        dolphin.launcher = original
        os.environ["EASYJOYCON_DOLPHIN_CONFIG"] = env
    assert dolphin.installed()


# --- Dolphin -------------------------------------------------------------
@caso
def configura_o_dolphin():
    cfgdir = TMP / "dolphin"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "DSUClient.ini").write_text(
        '[Server]\nEnabled = False\nEntries = Celular:192.0.2.10:26760;EasyJoycon:127.0.0.1:1;\n')
    (cfgdir / "WiimoteNew.ini").write_text(
        "[Wiimote1]\nDevice = XInput/0/Gamepad\nButtons/A = `Button A`\n[BalanceBoard]\nSource = 0\n")
    (cfgdir / "Dolphin.ini").write_text(
        "[General]\nISOPaths = 1\n[SDL_Hints]\nSDL_JOYSTICK_HIDAPI_COMBINE_JOY_CONS = 1\n"
        "SDL_JOYSTICK_HIDAPI_JOY_CONS = 1\n")
    real = dolphin.dolphin_running
    dolphin.dolphin_running = lambda: True
    try:
        dolphin.configure({1: "par"}, 26760, 26761)
        raise AssertionError("devia recusar com o Dolphin aberto")
    except RuntimeError:
        pass
    finally:
        dolphin.dolphin_running = real
    dolphin.dolphin_running = lambda: False
    try:
        path, backup = dolphin.configure({1: "par", 2: "par"}, 26760, 26761, wiimotes=2)
    finally:
        dolphin.dolphin_running = real
    assert backup and (backup / "WiimoteNew.ini").exists()
    dsu_ini = dolphin.read_ini(path / "DSUClient.ini")
    assert dolphin.ini_get(dsu_ini["Server"], "Enabled") == "True"
    entries = dolphin.ini_get(dsu_ini["Server"], "Entries")
    assert entries == ("Celular:192.0.2.10:26760;EasyJoycon:127.0.0.1:26760;"
                       "EasyJoycon Nunchuk:127.0.0.1:26761;"), entries
    wii = dolphin.read_ini(path / "WiimoteNew.ini")
    w2 = wii["Wiimote2"]
    assert dolphin.ini_get(w2, "Device") == "DSUClient/1/EasyJoycon"
    assert dolphin.ini_get(w2, "Source") == "1"
    assert dolphin.ini_get(wii["Wiimote3"], "Source") == "0"
    assert dolphin.ini_get(w2, "Nunchuk/IMUAccelerometer/Up") == \
        "`DSUClient/1/EasyJoycon Nunchuk:Accel Up`"
    assert dolphin.ini_get(w2, "IMUGyroscope/Pitch Up") == "`Gyro Pitch Up`"
    assert dolphin.ini_get(w2, "IMUGyroscope/Dead Zone") == "0"
    assert dolphin.ini_get(w2, "IMUGyroscope/Calibration Period") == "0"
    assert dolphin.ini_get(w2, "IMUIR/Total Yaw") == "360"
    assert dolphin.ini_get(w2, "Buttons/B") == "`R2` | `Cross`"
    assert dolphin.ini_get(w2, "Rumble/Motor") == "`evdev/0/EasyJoycon J2 Vibracao:Strong`"
    assert dolphin.ini_get(wii["Wiimote1"], "Buttons/A") == "`Circle`"
    assert "BalanceBoard" in wii, "seção que não é nossa fica"
    prof = path / "Profiles/Wiimote/EasyJoycon J3 - Qualquer jeito.ini"
    p = dolphin.read_ini(prof)["Profile"]
    assert dolphin.ini_get(p, "Device") == "DSUClient/2/EasyJoycon"
    assert dolphin.applied_slots() == [1, 2]
    # o SDL do Dolphin não pega o Joy-Con pelo hidraw (o resto do Dolphin.ini fica)
    main = dolphin.read_ini(path / "Dolphin.ini")
    assert dolphin.ini_get(main["SDL_Hints"], "SDL_JOYSTICK_HIDAPI_JOY_CONS") == "0"
    assert dolphin.ini_get(main["SDL_Hints"], "SDL_JOYSTICK_HIDAPI_SWITCH") == "0"
    assert dolphin.ini_get(main["SDL_Hints"], "SDL_JOYSTICK_HIDAPI_COMBINE_JOY_CONS") == "1"
    assert dolphin.ini_get(main["General"], "ISOPaths") == "1"
    assert (backup / "Dolphin.ini").exists()
    # GameCube: portas dos jogadores ligadas, mapeamento só com controle completo
    assert dolphin.ini_get(main["Core"], "SIDevice1") == "6"
    assert dolphin.ini_get(main["Core"], "SIDevice2") == "0"
    gc = dolphin.read_ini(path / "GCPadNew.ini")["GCPad2"]
    gate = "`evdev/0/EasyJoycon J2 Vibracao:Axis 2+`"
    assert dolphin.ini_get(gc, "Device") == "DSUClient/1/EasyJoycon"
    assert dolphin.ini_get(gc, "Buttons/A") == f"`Circle` & {gate}"
    assert dolphin.ini_get(gc, "Buttons/Z") == f"`R1` & {gate}"
    assert dolphin.ini_get(gc, "Triggers/L-Analog") == f"`L2` & {gate}"
    assert dolphin.ini_get(gc, "C-Stick/Left") == f"`Right X-` & {gate}"
    assert dolphin.ini_get(gc, "Main Stick/Up") == f"`Left Y+` & {gate}"
    assert dolphin.ini_get(gc, "Rumble/Motor") == "`evdev/0/EasyJoycon J2 Vibracao:Strong`"
    assert (path / "Profiles/GCPad/EasyJoycon J4.ini").exists()


@caso
def dolphin_um_mapeamento_pra_tudo():
    """2026-10-11: um mapeamento só pra todos os jeitos de jogar. Nunchuk e
    "deitado" vêm do dispositivo virtual do jogador, que o Dolphin lê a cada
    atualização — separar/juntar/deitar/tirar o Nunchuk vale com ele aberto.
    Só ligar MAIS Wii Remotes pede reabrir o Dolphin."""
    real = dolphin.dolphin_running
    dolphin.dolphin_running = lambda: False
    try:
        path, _ = dolphin.configure({1: "par", 2: "sozinho"}, 26760, 26761, wiimotes=2)
        wii = dolphin.read_ini(path / "WiimoteNew.ini")
        g = dolphin.ini_get
        for n in (1, 2):
            w = wii[f"Wiimote{n}"]
            assert g(w, "Extension") == f"`evdev/0/EasyJoycon J{n} Vibracao:Axis 0+`"
            assert g(w, "Options/Sideways Wiimote") == f"`evdev/0/EasyJoycon J{n} Vibracao:Axis 1+`"
            assert g(w, "Buttons/A") == "`Circle`" and g(w, "Source") == "1"
            assert g(w, "Nunchuk/Buttons/C") == "`L1`"
        assert g(wii["Wiimote3"], "Source") == "0"
        # mudar o jeito de jogar não muda nada; jogador além dos ligados, sim
        assert dolphin.slots_for({1: "sozinho"}, 2) == dolphin.slots_for({1: "par"}, 2) == [1, 2]
        assert dolphin.slots_for({3: "par"}, 2) == [1, 2, 3]
        path, _ = dolphin.configure({}, 26760, 26761, wiimotes=4)   # sem jogador: liga os 4
        wii = dolphin.read_ini(path / "WiimoteNew.ini")
        assert all(g(wii[f"Wiimote{n}"], "Source") == "1" for n in range(1, 5))
        assert dolphin.applied_slots() == [1, 2, 3, 4]
    finally:
        dolphin.dolphin_running = real
    backups = list((TMP / "data" / "backups").glob("dolphin-*"))
    assert len(backups) <= dolphin.MAX_BACKUPS


@caso
def sinais_do_nunchuk_e_deitado_chegam_no_dolphin():
    """O dispositivo virtual do jogador avisa pelos eixos: X = Nunchuk, Y =
    deitado (o Dolphin lê "Axis 0+" / "Axis 1+")."""
    from app import vibration
    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
        print("      (sem acesso ao /dev/uinput: pulei)")
        return
    v = vibration.VirtualRumble(8, lambda *a: None)
    v.name = "EasyJoycon Teste Sinais"
    v.create()
    try:
        node = None
        for _ in range(50):
            for e in os.listdir("/sys/class/input"):
                try:
                    if e.startswith("event") and open(f"/sys/class/input/{e}/device/name").read().strip() == v.name:
                        node = f"/dev/input/{e}"
                except OSError:
                    pass
            if node and os.access(node, os.R_OK):
                break
            time.sleep(0.05)
        assert node, "dispositivo virtual não apareceu"
        if not os.access(node, os.R_OK):   # fora do grupo input (ou sem relogar)
            print("      (sem permissão no /dev/input: pulei)")
            return
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            def axes():
                return tuple(E.get_absinfo(fd, a).value for a in (E.ABS_X, E.ABS_Y, E.ABS_Z))
            assert axes() == (0, 0, 0)
            v.set_signals(True, False, True)               # par: Nunchuk, completo
            time.sleep(0.05)
            assert axes() == (1, 0, 1)
            v.set_signals(False, True, False)              # separou e deitou
            time.sleep(0.05)
            assert axes() == (0, 1, 0)
        finally:
            os.close(fd)
    finally:
        v.destroy()
    # quem decide os sinais: o jogador
    from types import SimpleNamespace
    par = SimpleNamespace(online=True, mode=players.PAIR, nunchuk=True)
    assert players.signals(par) == (True, False, True)
    par.nunchuk = False                                # "Tirar Nunchuk": segue GameCube
    assert players.signals(par) == (False, False, True)
    assert players.signals(SimpleNamespace(online=True, mode=players.UPRIGHT,
                                           nunchuk=True)) == (False, False, False)
    assert players.signals(None) == (False, False, False)


# --- vibração dos jogos ---------------------------------------------------
@caso
def vibracao_do_jogo_chega_no_jogador():
    """Faz o papel do Dolphin num dispositivo virtual de verdade (uinput):
    sobe efeito + toca → vibra; muda a força → atualiza; apaga → para."""
    import fcntl

    from app import ff, vibration
    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
        print("      (sem acesso ao /dev/uinput: pulei)")
        return
    calls = []
    v = vibration.VirtualRumble(9, lambda *a: calls.append(a))
    v.name = "EasyJoycon Teste Vibracao"
    fd = v.create()
    try:
        node = None
        for _ in range(50):                   # o udev demora um pouquinho
            for e in os.listdir("/sys/class/input"):
                try:
                    if e.startswith("event") and open(f"/sys/class/input/{e}/device/name").read().strip() == v.name:
                        node = f"/dev/input/{e}"
                except OSError:
                    pass
            if node and os.access(node, os.W_OK):
                break
            time.sleep(0.05)
        assert node, "dispositivo virtual não apareceu"
        if not os.access(node, os.R_OK | os.W_OK):   # fora do grupo input (ou sem relogar)
            print("      (sem permissão no /dev/input: pulei)")
            return
        dfd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        import threading
        stop = threading.Event()

        def pump():                           # o servidor atende os pedidos do uinput
            while not stop.is_set():
                v.handle()
                time.sleep(0.002)
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        buf = ff.pack_rumble(-1, 0xFFFF, 0x4000, 500)
        fcntl.ioctl(dfd, ff.EVIOCSFF, buf)
        eid = struct.unpack_from("<h", buf, 2)[0]
        os.write(dfd, E.EVENT.pack(0, 0, ff.EV_FF, eid, 1))
        time.sleep(0.1)
        buf = ff.pack_rumble(eid, 0x8000, 0, 500)
        fcntl.ioctl(dfd, ff.EVIOCSFF, buf)
        time.sleep(0.1)
        fcntl.ioctl(dfd, ff.EVIOCRMFF, eid)
        time.sleep(0.1)
        stop.set()
        th.join()
        os.close(dfd)
    finally:
        v.destroy()
    assert calls[0][0] == 9 and close(calls[0][1:3], (1.0, 0x4000 / 0xFFFF)), calls
    assert any(close(c[1:3], (0x8000 / 0xFFFF, 0.0)) for c in calls), calls
    assert calls[-1][1:3] == (0.0, 0.0), calls
    del fd


@caso
def com_dois_adaptadores_usa_o_ligado():
    """Notebook com Bluetooth próprio + TP-Link, o do notebook desligado no
    rfkill: o app pegava o hci0 (o primeiro) mesmo desligado."""
    from dbus_fast import Variant

    from app import bluetooth

    def ad(powered, state=None):
        props = {"Powered": Variant("b", powered)}
        if state:
            props["PowerState"] = Variant("s", state)
        return props

    pick = bluetooth.pick_adapter
    assert pick({"/org/bluez/hci0": ad(False, "off-blocked"),
                 "/org/bluez/hci1": ad(True, "on")}) == "/org/bluez/hci1"
    assert pick({"/org/bluez/hci0": ad(False), "/org/bluez/hci1": ad(True)}) == "/org/bluez/hci1"
    # bloqueado perde até pro desligado comum (o app liga esse ao procurar)
    assert pick({"/org/bluez/hci0": ad(False, "off-blocked"),
                 "/org/bluez/hci1": ad(False, "off")}) == "/org/bluez/hci1"
    assert pick({"/org/bluez/hci0": ad(True), "/org/bluez/hci1": ad(True)}) == "/org/bluez/hci0"
    assert pick({"/org/bluez/hci0": ad(False)}) == "/org/bluez/hci0"


# --- janela ---------------------------------------------------------------
@caso
def janela_desenha_todos_os_jeitos():
    """Desenha os cartões e a faixa de espera fora da tela, com todos os
    modos, controle faltando, bateria carregando e Pro esperando."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("      (sem tela: pulei o desenho)")
        return
    from app import gui  # escolhe o GTK 3 antes de importar
    GLib, Gtk = gui.GLib, gui.Gtk

    def c(kind, mac, **kw):
        base = {"mac": mac, "kind": kind, "name": f"Joy-Con ({kind})" if kind != "PRO"
                else "Pro Controller", "battery": 4, "charging": False, "led": (1, 0, 0, 0),
                "motion": "ok", "hiccups": 0, "buttons": frozenset(), "stick": (0.3, -0.2),
                "rstick": (0, 0), "calibrated": True, "aim": (0.4, -0.3)}
        base.update(kw)
        return base

    st = {"players": {
        1: {"number": 1, "mode": "par", "complete": True, "missing": [],
            "members": {"R": c("R", "1", buttons=frozenset({"A", "ZR", "HOME"})),
                        "L": c("L", "2", hiccups=90, calibrated=False)}},
        2: {"number": 2, "mode": "sozinho", "complete": True, "missing": [],
            "members": {"L": c("L", "3", battery=1, charging=True, motion="calibrando")}},
        3: {"number": 3, "mode": "empe", "complete": False, "missing": [],
            "members": {"R": c("R", "4", motion="zerado", aim=(1.0, 0.2))}},   # na borda
        4: {"number": 4, "mode": "par", "complete": False, "missing": ["L"],
            "members": {"R": c("R", "5", motion="sem dados")}}},
        "waiting": [c("L", "6", led=(1, 0, 0, 1)), c("PRO", "7")],
        "searching": True, "search_left": 30}

    class App:
        state = st

    jogando = dict(st, waiting=[], searching=False)

    class Jogando:          # ninguém esperando: a faixa mostra como mirar
        state = jogando

    off = Gtk.OffscreenWindow()
    box = Gtk.Box(spacing=10)
    for n in range(1, 5):
        box.pack_start(gui.PlayerCard(n, App), True, True, 0)
    box.pack_start(gui.WaitingStrip(App), True, True, 0)
    box.pack_start(gui.WaitingStrip(Jogando), True, True, 0)
    off.add(box)
    off.set_default_size(1200, 420)
    off.show_all()
    erros = []
    old = sys.excepthook
    sys.excepthook = lambda *a: erros.append(a[1])   # exceção dentro do "draw" do GTK
    GLib.timeout_add(300, Gtk.main_quit)
    Gtk.main()
    sys.excepthook = old
    assert off.get_pixbuf() is not None
    assert not erros, erros


@caso
def janela_cabe_em_tela_1366x768():
    """O tamanho mínimo da janela cabe numa tela de notebook (1366×768 menos o
    painel do Cinnamon). Maior que a tela, o Cinnamon tira o botão de
    maximizar (2026-10-12: era 1060×773)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("      (sem tela: pulei)")
        return
    from unittest import mock
    from app import gui
    Gtk = gui.Gtk

    class App(Gtk.Application):
        pass

    a = App(application_id=f"teste.easyjoycon.tamanho{os.getpid()}")
    a.register(None)
    a.server = mock.MagicMock(smoothing=3, rumble_force=50)
    a.state = None
    try:
        for lang in ("pt", "en"):            # os textos têm tamanhos diferentes
            i18n.set_language(lang)
            w = gui.MainWindow(a)
            w.show_all()
            minimo, _ = w.get_preferred_size()
            w.destroy()
            assert minimo.width <= 1366 and minimo.height <= 690, (lang, minimo.width,
                                                                   minimo.height)
    finally:
        i18n.set_language("pt")


# --- tradução --------------------------------------------------------------
@caso
def traducao_completa():
    """Todo texto passado pro _() (e os que vão por variável: modos, bateria,
    instalar o Dolphin) tem a versão em inglês, com os mesmos {campos}."""
    import ast
    import string
    from app import server
    textos = set(players.MODE_NAMES.values()) | set(server.LOW_BATTERY.values())
    textos.add(dolphin.INSTALL_HELP)
    for f in (Path(__file__).resolve().parent.parent / "app").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                textos.add(node.args[0].value)
    assert len(textos) > 100, len(textos)

    def campos(t):
        return {nome for _, nome, _, _ in string.Formatter().parse(t) if nome}
    faltam = sorted(t for t in textos if t not in i18n.EN)
    assert not faltam, faltam
    errados = [t for t in textos if campos(t) != campos(i18n.EN[t])]
    assert not errados, errados
    sobrando = sorted(set(i18n.EN) - textos)
    assert not sobrando, sobrando
    try:
        i18n.set_language("en")
        assert i18n._("Jogador {n}", n=2) == "Player 2"
        assert i18n._("texto sem tradução") == "texto sem tradução"
    finally:
        i18n.set_language("pt")
    assert i18n._("Jogador {n}", n=2) == "Jogador 2"


@caso
def como_usar_aponta_pras_paginas_do_tutorial():
    """O botão "Como usar" abre docs/como-usar.md ou docs/how-to-use.md no
    GitHub: os dois existem e um aponta pro outro."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("      (sem tela: pulei)")
        return
    from app import gui
    raiz = Path(__file__).resolve().parent.parent
    assert set(gui.TUTORIAL) == {"pt", "en"}
    for lang, url in gui.TUTORIAL.items():
        assert url.startswith("https://github.com/thel4stbolacha/EasyJoycon/blob/main/"), url
        pagina = raiz / url.split("/blob/main/", 1)[1]
        assert pagina.exists(), pagina
        outra = "how-to-use.md" if lang == "pt" else "como-usar.md"
        assert f"({outra})" in pagina.read_text(encoding="utf-8"), (pagina, outra)


@caso
def idioma_segue_o_sistema():
    d = i18n.detect
    assert d({"LANG": "pt_BR.UTF-8"}) == "pt"
    assert d({"LANG": "en_US.UTF-8"}) == "en"
    assert d({"LANG": "es_ES.UTF-8"}) == "en"              # sem tradução: inglês
    assert d({"LANG": "en_US.UTF-8", "LANGUAGE": "pt_BR:pt:en"}) == "pt"
    assert d({"LANG": "pt_BR.UTF-8", "LANGUAGE": "en_US:en"}) == "en"
    assert d({"LANG": "C.UTF-8", "LANGUAGE": "pt_BR"}) == "en"   # locale C ignora LANGUAGE
    assert d({"LC_ALL": "pt_PT.UTF-8", "LANG": "en_US.UTF-8"}) == "pt"
    assert d({}) == "en"
    assert d({"LANG": "pt_BR.UTF-8", "EASYJOYCON_IDIOMA": "en"}) == "en"


# --- configuração --------------------------------------------------------
@caso
def config_invalida_usa_padrao_e_salvar_mantem_comentarios():
    original = config.CONFIG_FILE.read_text()
    try:
        config.CONFIG_FILE.write_text(original.replace("suavizacao = 3", "suavizacao = 99")
                                      .replace("lembrar = true", 'lembrar = "sim"'))
        cfg = config.load()
        assert cfg["movimento"]["suavizacao"] == 3 and cfg["jogadores"]["lembrar"] is True
        config.save_value("movimento", "suavizacao", 7)
        text = config.CONFIG_FILE.read_text()
        assert "suavizacao = 7" in text and "# Suavização do giroscópio" in text
        assert config.load()["movimento"]["suavizacao"] == 7
        # config de uma versão antiga, sem a chave (ou sem a seção): acrescenta
        sem = "\n".join(ln for ln in original.splitlines() if not ln.startswith("forca ="))
        config.CONFIG_FILE.write_text(sem)
        config.save_value("vibracao", "forca", 70)
        assert config.load()["vibracao"]["forca"] == 70
        assert config.load()["bluetooth"] == config._limpar({})["bluetooth"]   # resto intacto
        config.CONFIG_FILE.write_text("[movimento]\nsuavizacao = 2\n")
        config.save_value("vibracao", "grave", "false")
        assert config.load()["vibracao"]["grave"] is False
        assert config.load()["movimento"]["suavizacao"] == 2
    finally:
        config.CONFIG_FILE.write_text(original)


@caso
def esquecer_tira_so_os_controles():
    from app.bluetooth import Bluetooth
    bt = Bluetooth()
    bt.adapter = "/org/bluez/hci0"
    bt.searching_until = time.monotonic() + 30
    bt._all = {
        "/org/bluez/hci0/dev_AA_00_00_00_00_40": {"Name": "Joy-Con (L)", "Paired": True},
        "/org/bluez/hci0/dev_AA_00_00_00_00_41": {"Name": "Joy-Con (R)"},
        "/org/bluez/hci0/dev_AA_00_00_00_00_42": {"Name": "Fone Bluetooth", "Paired": True},
    }
    chamadas = []

    async def fake_call(dest, path, iface, member, signature="", body=None, *, timeout=None):
        chamadas.append((member, (body or [None])[0]))
        return []
    bt._call = fake_call
    n = asyncio.run(bt.forget_all())
    removidos = {b for m, b in chamadas if m == "RemoveDevice"}
    assert n == 2, n
    assert removidos == {"/org/bluez/hci0/dev_AA_00_00_00_00_40",
                         "/org/bluez/hci0/dev_AA_00_00_00_00_41"}, removidos
    assert ("StopDiscovery", None) in chamadas and not bt.searching
    bt.adapter = None                               # adaptador caiu do USB
    assert asyncio.run(bt.forget_all()) is None


@caso
def avisa_quando_o_adaptador_trava():
    from app import bluetooth as B
    espera = B.INPUT_WAIT_S
    B.INPUT_WAIT_S = 0.01
    try:
        for resolvido, esperado in ((False, "adaptador Bluetooth travou"),
                                    (True, "conectou pela metade")):
            ditos = []
            bt = B.Bluetooth(on_message=lambda m, *_, d=ditos: d.append(m),
                             has_input=lambda mac: False)
            bt.adapter = "/org/bluez/hci0"
            path = "/org/bluez/hci0/dev_AA_00_00_00_00_40"
            bt._all = {path: {"Name": "Joy-Con (L)", "ServicesResolved": resolvido}}

            async def fake_call(*a, **k):
                return []
            bt._call = fake_call
            asyncio.run(bt._pair_and_connect_locked(path))
            assert any(esperado in m for m in ditos), ditos
    finally:
        B.INPUT_WAIT_S = espera


@caso
def conecta_o_controle_depois_de_parear():
    """Depois do Pair() o BlueZ já diz Connected, mas sem Connect() o canal
    do controle não abre (visto no TP-Link em 2026-10-10: ~25 s e aviso falso)."""
    from app import bluetooth as B
    ditos, chamadas, conectado = [], [], set()
    path = "/org/bluez/hci0/dev_AA_00_00_00_00_40"
    bt = B.Bluetooth(on_message=lambda m, *_: ditos.append(m),
                     has_input=lambda mac: mac in conectado)
    bt.adapter = "/org/bluez/hci0"
    bt._all = {path: {"Name": "Joy-Con (L)"}}

    async def fake_call(dest, p, iface, member, *a, **k):
        chamadas.append(member)
        if member == "Pair":
            bt._all[path].update(Paired=True, Bonded=True, Connected=True)
        if member == "Connect":
            conectado.add("AA:00:00:00:00:40")
        return []
    bt._call = fake_call
    asyncio.run(bt._pair_and_connect_locked(path))
    assert "Connect" in chamadas and "Disconnect" not in chamadas, chamadas
    assert ditos[-1] == "Joy-Con (L) pareado", ditos


@caso
def nao_derruba_quem_esta_pareando():
    from app.bluetooth import Bluetooth
    bt = Bluetooth()
    bt.adapter = "/org/bluez/hci0"
    path = "/org/bluez/hci0/dev_AA_00_00_00_00_40"
    bt._connected_since[path] = time.monotonic() - 60   # "conectado" faz 1 min, sem controle
    bt._busy.add(path)
    assert bt.half_connected(set()) == []               # pareando: deixa quieto
    bt._busy.discard(path)
    assert bt.half_connected(set()) == ["AA:00:00:00:00:40"]


@caso
def apertar_a_termina_a_busca():
    from app import server as S

    class FakeBt:
        searching, paradas = True, 0

        async def stop_search(self):
            self.paradas += 1
            self.searching = False

    class FakeServer:
        bt = FakeBt()

        async def buzz(self, ctrl, pattern):
            pass

    async def run():
        srv = FakeServer()
        for kind, botao, termina in (("L", "LEFT", False), ("R", "B", False),
                                     ("L", "RIGHT", True), ("R", "A", True),
                                     ("L", "DOWN", True), ("PRO", "A", True)):
            srv.bt.searching, srv.bt.paradas = True, 0
            c = FakeCtrl(kind, "AA:00:00:00:00:09")
            c.buttons.add(botao)
            S.Server._confirm_search(srv, c)
            await asyncio.sleep(0)
            assert (srv.bt.paradas == 1) == termina, (kind, botao)
        srv.bt.searching, srv.bt.paradas = False, 0       # sem procurar: A é só A
        S.Server._confirm_search(srv, c)
        await asyncio.sleep(0)
        assert srv.bt.paradas == 0
    asyncio.run(run())
    assert S.Server._search_seconds(type("X", (), {"cfg": CFG})()) == float("inf")


@caso
def avisa_quando_outro_programa_usa_o_controle():
    """Steam com o hidraw do Joy-Con aberto estraga a calibração e a
    reconexão (2026-10-12): achar quem abriu pelo /proc e avisar uma vez.
    Não acusar o próprio EasyJoycon (ele abre o hidraw pra vibrar)."""
    from app import colors, hidusers
    from app import server as S
    proc = TMP / "proc"
    raiz = hidusers.OUR_RUN.parent

    def processo(pid, comm, alvos, cmdline=b"x", cwd="/"):
        d = proc / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(cmdline)
        (d / "cwd").symlink_to(cwd)
        for n, alvo in enumerate(alvos):
            (d / "fd" / str(n)).symlink_to(alvo)

    processo(100, "steamwebhelper", ["/dev/hidraw3", "/dev/null"])
    processo(101, "steam", ["/dev/hidraw3"])
    processo(102, "python3", ["/dev/hidraw4"], b"venv/bin/python\0run.py\0", str(raiz))
    processo(103, "python3", ["/dev/hidraw4"])               # outro python qualquer
    processo(104, "eu", ["/dev/hidraw4"])                    # este processo
    (proc / "105").mkdir()                                   # de outro usuário: sem fd
    achou = hidusers.users(["/dev/hidraw3", "/dev/hidraw4", None], proc=proc, me=104)
    assert achou == {"/dev/hidraw3": {"Steam"}, "/dev/hidraw4": {"python3"}}, achou
    assert hidusers.users([], proc=proc) == {}

    ditos = []

    class FakeServer:
        def __init__(self):
            self.hid_users = {}
            self.controllers = {"AA:00:00:00:00:01": FakeCtrl("L", "AA:00:00:00:00:01")}
            self.controllers["AA:00:00:00:00:01"].hid = "0005:057E:2006.0001"
            self.players = type("P", (), {"_say": lambda self, m, k="info": ditos.append(m)})()

    caminho, usuarios = colors.hidraw_path, hidusers.users
    colors.hidraw_path = lambda hid: "/dev/hidraw3"
    hidusers.users = lambda paths: usuarios(paths, proc=proc, me=104)
    try:
        async def run():
            srv = FakeServer()
            srv._loop = asyncio.get_running_loop()
            for _ in range(2):                                 # avisa uma vez só
                await S.Server._check_hid_users(srv)
            assert srv.hid_users == {"AA:00:00:00:00:01": ["Steam"]}, srv.hid_users
            assert len(ditos) == 1 and "Steam está usando o Joy-Con (L)" in ditos[0], ditos
            colors.hidraw_path = lambda hid: "/dev/hidraw9"     # Steam fechou
            await S.Server._check_hid_users(srv)
            assert srv.hid_users == {} and len(ditos) == 1
        asyncio.run(run())
    finally:
        colors.hidraw_path, hidusers.users = caminho, usuarios


def main():
    falhas = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"ok    {fn.__name__}")
        except Exception as e:  # noqa: BLE001 — relatar e seguir pros outros casos
            falhas += 1
            print(f"FALHA {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(RESULTS) - falhas}/{len(RESULTS)} passaram")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
