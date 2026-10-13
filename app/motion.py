"""Movimento: transforma as amostras do giroscópio/acelerômetro do Joy-Con no
que o Dolphin espera pelo DSU, com calibração do desvio, zona morta,
suavização e "segurar" durante os engasgos do Bluetooth.

Eixos do hid-nintendo (iguais no L, no R e no Pro):
  X: pra frente (lado dos gatilhos), Y: pra esquerda, Z: pra cima (face).
Segurando o Joy-Con em pé como um Wii Remote (gatilhos apontados pra TV),
"frente" do Joy-Con = frente do Wii Remote. O Dolphin usa o mesmo jeito:
parado com a face pra cima, aceleração (0, 0, +1 g) — WiimoteEmu.cpp,
GetTotalAcceleration.

Rótulos do Dolphin pro DSU (DualShockUDPClient.cpp):
  Accel Left = +x, Accel Up = −y, Accel Forward = +z
  Gyro Pitch Up = +pitch, Roll Right = +roll, Yaw Right = +yaw
"""
import math
from dataclasses import dataclass, field

ACCEL_RES = 4096   # unidades por g (JC_IMU_ACCEL_RES_PER_G)
# Borda da tela no desenho da mira: a câmera do Wii Remote vê 42° × 31,5°
# (Camera.h), então a barra de sensores sai da vista a ±21° pros lados e
# ±15,75° pra cima/baixo.
AIM_LIMIT = 20.0
AIM_LIMIT_Y = 15.0
# Como no Wii de verdade: o ponteiro do Dolphin some quando sai da tela e
# volta no lugar certo — sem prender (Total Yaw 360 = nunca prende). Prender
# no Dolphin arrasta o centro; prender aqui cortava o giro que os jogos
# com MotionPlus recebem (decidido jogando, em 2026-10-11).
POINTER_TOTAL_YAW = 360
# Correção da inclinação pela gravidade (como no GamepadMotionHelpers, do
# Jibb Smart): forte com o controle quieto, fraca chacoalhando, e nunca mais
# rápida que uma fração do próprio giro — senão o empurrão do braço num
# movimento rápido entorta a mira.
GRAVITY_WEIGHT = 0.02      # parado: 2% do erro a cada leitura (igual ao Dolphin)
GRAVITY_WEIGHT_SHAKY = 0.002
SHAKY_MIN_G, SHAKY_MAX_G = 0.01, 0.4   # "chacoalhado": desvio do acelerômetro da média recente
SHAKY_HALF_S = 0.25        # meia-vida da média e do pico de chacoalhado
GRAVITY_GYRO_FACTOR = 0.1  # corrige no máximo 10% do quanto girou nesta leitura…
GRAVITY_MIN_DEG = 0.05     # …ou isso (graus por leitura), o que for maior
GRAVITY_FADE_DPS = (30.0, 60.0)   # girando rápido: a correção some aos poucos entre esses
# Tempo: o Joy-Con lê o IMU a cada 5 ms e manda 3 leituras por pacote a cada
# ~15 ms. A hora que vale é a de chegada do pacote (controllers.read_imu).
SAMPLE_S = 0.005
PACKET_S = 0.015           # ritmo normal dos pacotes (aprendido por controle)
PER_PACKET = 3             # leituras por pacote (hid-nintendo: sempre 3)
LOST_TOL_S = 0.0225        # atrasou mais que 1,5 pacote = pacote perdido (igual ao driver)
SETTLE_PACKETS = 10        # pacotes no ritmo depois de um buraco: o buraco era perda mesmo
MAX_GAP_S = 0.5            # buraco maior que isso não dá pra adivinhar o giro
PAYBACK_S = 0.045          # o Dolphin alcança o ângulo de verdade em ~3 pacotes
MAX_DEBT = 45.0            # diferença maior que isso = algo estranho: recomeça a conta
MAX_FIX_DPS = 300.0
GYRO_RES = 14247   # unidades por grau/s (JC_IMU_GYRO_RES_PER_DPS)


@dataclass
class MotionOut:
    """O que vai pro pacote DSU."""
    accel: tuple = (0.0, -1.0, 0.0)  # x, y, z em g (parado, face pra cima)
    gyro: tuple = (0.0, 0.0, 0.0)    # pitch, yaw, roll em graus/s
    timestamp_us: int = 0


# Referenciais: qual eixo do Joy-Con (índice, sinal) é frente, esquerda e cima.
# Em pé (Wii Remote/Nunchuk apontando pra TV, ou Pro Controller na mão):
VERTICAL = ((0, 1), (1, 1), (2, 1))
# Deitado, com a opção "Sideways Wii Remote" do Dolphin ligada: o Dolphin
# espera o movimento do ponto de vista de quem joga (frente = pra TV) e gira
# sozinho. Esquerdo deitado: gatilhos pra esquerda; direito: pra direita.
SIDEWAYS_L = ((1, -1), (0, 1), (2, 1))
SIDEWAYS_R = ((1, 1), (0, -1), (2, 1))


def grip_rotation(up):
    """Matriz que leva a direção "pra cima" medida (eixos do Joy-Con) até a
    face pra cima (0, 0, 1) pelo menor giro. Usada na pegada livre: a posição
    em que o controle está ao recentralizar vira "deitado, apontando pra TV".
    Segurando em pé (gatilhos pro teto, botões pra você), a frente vira as
    costas do Joy-Con, que apontam pra TV."""
    n = math.sqrt(sum(v * v for v in up))
    if n < 1e-6:
        return None
    ux, uy, uz = (v / n for v in up)
    c = uz                                  # cosseno do ângulo até (0, 0, 1)
    if c < -0.9999:                         # de cabeça pra baixo: meia volta na frente
        return ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    v = (uy, -ux, 0.0)                      # eixo do giro = up × (0, 0, 1)
    vx = ((0.0, -v[2], v[1]), (v[2], 0.0, -v[0]), (-v[1], v[0], 0.0))
    k = 1.0 / (1.0 + c)                     # Rodrigues: I + [v]× + [v]×² / (1 + c)
    return tuple(tuple((i == j) + vx[i][j] + k * sum(vx[i][n] * vx[n][j] for n in range(3))
                       for j in range(3)) for i in range(3))


def rotate(m, v):
    if m is None:
        return tuple(v)
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


def tilt_between(a, b):
    """Ângulo (graus) entre duas matrizes de pegada (None = sem giro)."""
    z = (0.0, 0.0, 1.0)
    # onde cada pegada manda o "cima" de verdade: compara pelo vetor que vira (0,0,1)
    ua = rotate(_transpose(a), z)
    ub = rotate(_transpose(b), z)
    d = max(-1.0, min(1.0, sum(x * y for x, y in zip(ua, ub, strict=True))))
    return math.degrees(math.acos(d))


def _transpose(m):
    if m is None:
        return None
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _matmul(a, b):
    """a × b (3×3), escrito por extenso: roda 400 vezes por segundo."""
    (a0, a1, a2), (a3, a4, a5), (a6, a7, a8) = a
    (b0, b1, b2), (b3, b4, b5), (b6, b7, b8) = b
    return ((a0 * b0 + a1 * b3 + a2 * b6, a0 * b1 + a1 * b4 + a2 * b7, a0 * b2 + a1 * b5 + a2 * b8),
            (a3 * b0 + a4 * b3 + a5 * b6, a3 * b1 + a4 * b4 + a5 * b7, a3 * b2 + a4 * b5 + a5 * b8),
            (a6 * b0 + a7 * b3 + a8 * b6, a6 * b1 + a7 * b4 + a8 * b7, a6 * b2 + a7 * b5 + a8 * b8))


def _axis_angle(axis, deg):
    """Matriz do giro de deg graus em volta de axis (qualquer tamanho)."""
    n = math.sqrt(sum(v * v for v in axis))
    if n < 1e-12 or abs(deg) < 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    x, y, z = (v / n for v in axis)
    ang = math.radians(deg)
    c, s, k = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    return ((c + x * x * k, x * y * k - z * s, x * z * k + y * s),
            (y * x * k + z * s, c + y * y * k, y * z * k - x * s),
            (z * x * k - y * s, z * y * k + x * s, c + z * z * k))


def _spin(d):
    """Matriz do giro d (graus em cada eixo, já vezes o tempo)."""
    return _axis_angle(d, math.sqrt(sum(v * v for v in d)))


def to_dsu(accel, gyro, frame=VERTICAL):
    """Eixos do Joy-Con (g, graus/s) → campos DSU (accel xyz, gyro pitch/yaw/roll)."""
    fwd, left, up = (s * accel[i] for i, s in frame)
    w_fwd, w_left, w_up = (s * gyro[i] for i, s in frame)
    # Levantar a frente = girar em torno da direita → pitch up = −w_left.
    # Esquerda subindo (lado direito desce) = roll right = +w_fwd.
    # Frente virando pra esquerda = yaw left → yaw right = −w_up.
    return (left, -up, fwd), (-w_left, -w_up, w_fwd)


class TieredSmoothing:
    """Suavização em camadas (a dos jogos com mira por giroscópio, de Jibb
    Smart): giro lento — a tremedeira da mão — passa por uma média; giro
    rápido passa direto, sem atraso nenhum; no meio, mistura. Olha a
    velocidade do vetor inteiro, então os três eixos são tratados igual e a
    diagonal sai reta (o filtro antigo, um por eixo, entortava o caminho e
    atrasava ~10 ms)."""

    def __init__(self, low, high, tau):
        self.low, self.high, self.tau = low, high, tau
        self.avg = [0.0, 0.0, 0.0]

    def __call__(self, v, dt):
        speed = math.sqrt(sum(x * x for x in v))
        direct = min(1.0, max(0.0, (speed - self.low) / (self.high - self.low)))
        k = min(1.0, dt / self.tau)
        self.avg = [a + k * (x * (1 - direct) - a) for a, x in zip(self.avg, v, strict=True)]
        return [x * direct + a for x, a in zip(v, self.avg, strict=True)]


class Smooth:
    """Média simples de vetor (acelerômetro)."""

    def __init__(self, tau):
        self.tau, self.x = tau, None

    def __call__(self, v, dt):
        if self.x is None:
            self.x = list(v)
        k = min(1.0, dt / self.tau)
        self.x = [a + k * (x - a) for a, x in zip(self.x, v, strict=True)]
        return self.x


def filter_params(level):
    """Suavização 0–10 → giro abaixo de "low" °/s é todo suavizado, acima de
    "high" passa direto; tau = janela da média (s). 0 = sem filtro."""
    if level <= 0:
        return None
    # medido em 2026-10-11: janela de 10 ms × nível atrasava a mira lenta
    # 20 ms no nível 3 (dava pra sentir). Com 5 ms × nível: 5 ms de atraso, e
    # a tremedeira de 10 Hz passa 79% (era 50%) — quem quiser mais liso sobe
    # a régua.
    return {"gyro": (0.75 * level, 2.5 * level, 0.005 * level), "accel": 0.004 * level}


@dataclass
class _Stillness:
    """Detecta controle parado pelo quanto a leitura *varia* (não pelo valor:
    o desvio pode ser grande e o acelerômetro de fábrica pode ler 1,09 g
    parado — os dois já impediram a calibração em 2026-10-07)."""
    since: float | None = None
    mean: list | None = None      # média móvel do giroscópio (°/s)
    var: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    amean: list | None = None     # média móvel do acelerômetro (g)
    avar: list = field(default_factory=lambda: [0.0, 0.0, 0.0])


class MotionProcessor:
    """Um por Joy-Con. Recebe amostras cruas (já em g e graus/s) e devolve o
    movimento filtrado."""

    STILL_GYRO_STD = 1.0      # giroscópio variando menos que isso (°/s) = parado (no máximo)
    # Aprende o quão quieto ESTE controle fica (como o GamepadMotionHelpers):
    # depois de ver ele na mesa (~0,1–0,2 °/s), "parado" passa a exigir até 2×
    # isso — um giro lento e firme da mão não vira desvio. Sem ver, fica 1 °/s.
    NOISE_FACTOR = 2.0
    NOISE_MIN = 0.05          # °/s: abaixo disso é a resolução do sensor
    NOISE_CLIMB = 0.01        # o ruído aprendido sobe 1%/s (se o sensor esquentar, reaprende)
    STILL_ACCEL_STD = 0.015   # acelerômetro variando menos que isso (g) = parado
    MAX_BIAS_DPS = 20.0       # desvio de verdade nunca passa disso (senão está girando)
    STILL_SECONDS = 1.5       # parado por tanto tempo → calibra
    WINDOW_TAU = 0.3          # janela das médias móveis (s)
    BIAS_TAU = 1.5            # rapidez dos ajustes seguintes (s)
    ZERO_SECONDS = 1.5        # giroscópio zerado por tanto tempo = calibração do driver quebrada
    RECENTER_HOLD_S = 1.0     # pegada mudou: segura o Recenter do Dolphin por tanto tempo
    GRIP_CHANGE_DEG = 5.0     # menos que isso não conta como pegada nova

    def __init__(self, cfg):
        self.bias = [0.0, 0.0, 0.0]
        self.raw_accel = (0.0, 0.0, 1.0)
        self.raw_gyro = (0.0, 0.0, 0.0)
        self.last_sample_t = None
        self.last_ts_us = 0
        self._still = _Stillness()
        self._manual = None   # calibração pedida: [soma xyz, n, fim, movimento?]
        self.last_calibration = None
        self.calibrated = False   # já calibrou o desvio pelo menos uma vez
        self.gyro_noise = self.STILL_GYRO_STD   # ruído aprendido (°/s)
        self._still_n = 0
        self._zero_since = None
        self.broken_driver_cal = False
        self.grip = None              # pegada livre: giro aplicado no que sai (None = nenhum)
        self.orient = None            # orientação (eixos do controle → mundo, z = cima)
        self._smooth_a = None         # média recente do acelerômetro
        self._shaky = 0.0             # quanto está chacoalhando (g)
        # mira da janela, no "espaço do jogador" (como no Switch): girar em
        # volta da gravidade = lado; inclinar a frente = altura. Assim vale
        # com o controle tombado na mão (pelos eixos do controle uma
        # diagonal virava horizontal — gravação de 2026-10-11).
        self._orient0 = None          # orientação no Recenter (o "meio" da mira)
        self.recenter_until = 0.0     # até quando segurar o Recenter do Dolphin
        # Ângulo: quanto o controle girou de verdade (real) e quanto o Dolphin
        # já somou do que mandamos (sent). A diferença é devolvida aos poucos:
        # pacote perdido, atrasado ou embolado não tira a mira do lugar.
        self.real_int = [0.0, 0.0, 0.0]
        self.sent_int = [0.0, 0.0, 0.0]
        self._out_t = None
        self._out_rate = [0.0, 0.0, 0.0]
        self._prev_g = None           # última leitura (pra estimar o giro num buraco)
        self._pkt_us = None           # chegada do último pacote
        self._nread = 0               # leituras contadas (a cada 3, pacote novo)
        self._period = PACKET_S
        self._late = 0.0              # atraso acumulado dos pacotes (s)
        self._lost = 0.0              # parte dele que já contamos como perdida
        self._lost_vec = [0.0, 0.0, 0.0]   # giro que estimamos pra essa parte (graus)
        self._steady = 0
        self.apply_config(cfg)

    def apply_config(self, cfg):
        mv = cfg["movimento"]
        self.deadzone = mv["zona_morta"]
        self.auto_cal = mv["calibrar_parado"]
        self.hold_s = mv["segurar_ms"] / 1000
        self.free_grip = mv["pegada_livre"]
        if not self.free_grip:
            self.grip = None
        self.set_smoothing(mv["suavizacao"])

    def set_smoothing(self, level):
        self.smoothing = level
        p = filter_params(level)
        if p is None:
            self._gyro_f = self._accel_f = None
        else:
            self._gyro_f = TieredSmoothing(*p["gyro"])
            self._accel_f = Smooth(p["accel"])
        self._out_gyro = [0.0, 0.0, 0.0]
        self._out_accel = list(self.raw_accel)

    # --- entrada -------------------------------------------------------
    def add_samples(self, samples, now, gaps=None):
        """samples: [(timestamp_us, (ax, ay, az) g, (gx, gy, gz) dps)] na ordem.
        gaps: segundos de leituras perdidas antes de cada amostra, quando o
        próprio pacote diz (rawimu); sem isso, adivinha pela hora de chegada."""
        if not samples:
            return
        burst_g, burst_a = [], []
        for k, (ts, accel, gyro) in enumerate(samples):
            dt = SAMPLE_S
            gap = 0.0
            if gaps is not None:
                gap = gaps[k] if gaps[k] <= MAX_GAP_S else 0.0
            # pacote novo a cada 3 leituras (atrasados chegam quase juntos,
            # então não dá pra separar pela hora)
            elif self._nread % PER_PACKET == 0:
                if self._pkt_us is not None:
                    gap = self._packet_arrived((ts - self._pkt_us) / 1e6)
                self._pkt_us = ts
            self._nread += 1
            self.last_ts_us = ts
            self._check_zero(accel, gyro, now)
            self._calibrate(accel, gyro, dt, now)
            g = [gyro[i] - self.bias[i] for i in range(3)]
            # zona morta pela velocidade total (por eixo, um giro lento na
            # diagonal perdia o eixo mais fraco e entortava)
            if math.sqrt(g[0] * g[0] + g[1] * g[1] + g[2] * g[2]) < self.deadzone:
                g = [0.0, 0.0, 0.0]
            if self._gyro_f:
                g = self._gyro_f(g, dt)
                a = self._accel_f(accel, dt)
            else:
                a = list(accel)
            gg = rotate(self.grip, g)
            aa = rotate(self.grip, a)
            self._track_orientation(gg, aa, dt)
            for i in range(3):
                self.real_int[i] += gg[i] * dt
            if gap > 0 and self._prev_g is not None:
                # giro no buraco ≈ média entre antes e depois
                v = [(self._prev_g[i] + gg[i]) / 2 * gap for i in range(3)]
            elif gap < 0:
                # o "buraco" era só atraso: os pacotes chegaram embolados e
                # trazem o giro de verdade — desfaz a estimativa (a mesma)
                frac = min(1.0, -gap / (self._lost - gap))
                v = [-x * frac for x in self._lost_vec]
            else:
                v = None
            if v is not None:
                self._turn(v)
                for i in range(3):
                    self.real_int[i] += v[i]
                    if gaps is None:          # perda adivinhada: pode ter que desfazer
                        self._lost_vec[i] += v[i]
            self._prev_g = gg
            burst_g.append(gg)
            burst_a.append(aa)
            self.raw_accel, self.raw_gyro = accel, gyro
        # Vai a leitura mais nova (a média do pacote atrasava 5 ms). O giro
        # total continua certo: a conta de ângulo em output() devolve a
        # diferença entre o que o controle girou e o que o Dolphin somou.
        n = len(burst_g)
        self._out_gyro = list(burst_g[-1])
        self._out_accel = [sum(v[i] for v in burst_a) / n for i in range(3)]
        self.last_sample_t = now

    @property
    def up(self):
        """O "cima" (gravidade) visto do controle, já com a pegada."""
        o = self.orient
        return None if o is None else [o[2][0], o[2][1], o[2][2]]

    def _track_orientation(self, g, a, dt):
        """Orientação completa do controle, como o Dolphin guarda pro
        ponteiro (Dynamics.cpp, EmulateIMUCursor): o giro gira, e o
        acelerômetro acerta a inclinação aos poucos (pouco quando chacoalha:
        ele sente o empurrão do braço). A mira da janela
        sai daqui: somar "giro em volta da gravidade" errava a volta pro meio
        depois de diagonais (−8° numa gravação de 2026-10-11)."""
        n = math.sqrt(sum(v * v for v in a))
        if self.orient is None:
            if n < 1e-6:
                return
            u = [v / n for v in a]             # leva o "cima" medido pro z do mundo
            self.orient = _axis_angle((u[1], -u[0], 0.0),
                                      math.degrees(math.acos(max(-1.0, min(1.0, u[2])))))
            self._orient0 = self.orient       # até o 1º Recenter, o meio é onde começou
            return
        d = [v * dt for v in g]
        self._turn(d)
        if n < 1e-6:
            return
        # chacoalhado = pico (que vai sumindo) do desvio do acelerômetro da média
        keep = 0.5 ** (dt / SHAKY_HALF_S)
        sa = self._smooth_a or list(a)
        self._shaky = max(self._shaky * keep, math.sqrt(sum((a[i] - sa[i]) ** 2 for i in range(3))))
        self._smooth_a = [a[i] + (sa[i] - a[i]) * keep for i in range(3)]
        k = min(1.0, max(0.0, (self._shaky - SHAKY_MIN_G) / (SHAKY_MAX_G - SHAKY_MIN_G)))
        weight = GRAVITY_WEIGHT + (GRAVITY_WEIGHT_SHAKY - GRAVITY_WEIGHT) * k
        lo, hi = GRAVITY_FADE_DPS
        weight *= min(1.0, max(0.0, (hi - math.sqrt(sum(v * v for v in g))) / (hi - lo)))
        o = self.orient
        aw = [(o[i][0] * a[0] + o[i][1] * a[1] + o[i][2] * a[2]) / n for i in range(3)]  # no mundo
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, aw[2]))))
        turned = math.sqrt(sum(v * v for v in d))
        step = min(tilt * weight, max(turned * GRAVITY_GYRO_FACTOR, GRAVITY_MIN_DEG))
        self.orient = _matmul(_axis_angle((aw[1], -aw[0], 0.0), step), o)

    def _turn(self, d):
        """Gira a orientação por d (graus em cada eixo do controle)."""
        if self.orient is not None:
            self.orient = _matmul(self.orient, _spin(d))

    @staticmethod
    def _pointing(o, k, sign):
        """(lado, altura) em graus pra onde a frente aponta: k=0 frente X,
        k=1 frente ±Y."""
        f = [o[i][k] * sign for i in range(3)]
        return (math.degrees(math.atan2(f[1], f[0])),
                math.degrees(math.asin(max(-1.0, min(1.0, f[2])))))

    def _packet_arrived(self, delta):
        """Chegou pacote novo, delta segundos depois do anterior. Devolve
        quantos segundos de leituras se perderam desde o último (negativo =
        o que parecia perdido só tinha atrasado). O atraso acumulado sobe
        num buraco e desce quando pacotes atrasados chegam embolados; se fica
        lá em cima com os pacotes de volta no ritmo, foi perda mesmo."""
        if delta > MAX_GAP_S or delta < 0:      # sumiu demais: não dá pra adivinhar
            self._late = self._lost = 0.0
            self._lost_vec = [0.0, 0.0, 0.0]
            self._steady = 0
            return 0.0
        if abs(delta - self._period) < 0.004:   # aprende o ritmo de verdade
            self._period += 0.02 * (delta - self._period)
        self._late = max(0.0, self._late + delta - self._period)
        lost = self._late if self._late > LOST_TOL_S else 0.0
        change, self._lost = lost - self._lost, lost
        self._steady = self._steady + 1 if abs(delta - self._period) < 0.003 else 0
        if self._steady >= SETTLE_PACKETS:      # de volta ao ritmo: fecha a conta
            self._late = self._lost = 0.0
            self._lost_vec = [0.0, 0.0, 0.0]
        return change

    def _check_zero(self, accel, gyro, now):
        # O giroscópio de verdade sempre treme um pouco, mesmo parado. Os três
        # eixos exatamente zero = o driver leu a calibração errada ao conectar
        # ("inaccurate IMU divisors" no kernel). Às vezes só o giroscópio
        # zera e o acelerômetro continua funcionando (Joy-Con R, 2026-10-10).
        if any(gyro):
            self._zero_since = None
            self.broken_driver_cal = False
        elif self._zero_since is None:
            self._zero_since = now
        elif now - self._zero_since > self.ZERO_SECONDS:
            self.broken_driver_cal = True

    def _calibrate(self, accel, gyro, dt, now):
        if self._manual is not None:
            m = self._manual
            for i in range(3):
                m[0][i] += gyro[i]
            m[1] += 1
            if max(abs(gyro[i] - m[0][i] / m[1]) for i in range(3)) > 15:
                m[3] = True
            if now >= m[2]:
                if not m[3] and m[1] > 20:
                    self.bias = [s / m[1] for s in m[0]]
                    self.last_calibration = "ok"
                    self.calibrated = True
                else:
                    self.last_calibration = "mexeu"
                self._manual = None
            return
        if not self.auto_cal:
            return
        st = self._still
        if st.mean is None:
            st.mean, st.amean = list(gyro), list(accel)
            return
        k = min(1.0, dt / self.WINDOW_TAU)
        for i in range(3):
            d = gyro[i] - st.mean[i]
            st.mean[i] += k * d
            st.var[i] += k * (d * d - st.var[i])
            d = accel[i] - st.amean[i]
            st.amean[i] += k * d
            st.avar[i] += k * (d * d - st.avar[i])
        mag = math.sqrt(sum(v * v for v in st.amean))
        self._learn_noise(st, dt)
        limit = min(self.STILL_GYRO_STD, self.NOISE_FACTOR * self.gyro_noise)
        still = (max(st.var) < limit ** 2
                 and max(st.avar) < self.STILL_ACCEL_STD ** 2
                 and 0.7 < mag < 1.3
                 and max(abs(v) for v in st.mean) < self.MAX_BIAS_DPS)
        if not still:
            st.since = None
            return
        if st.since is None:
            st.since = now
        if now - st.since < self.STILL_SECONDS:
            return
        if not self.calibrated:          # primeira vez: na hora
            self.bias = list(st.mean)
            self.calibrated = True
        else:                            # depois: ajuste suave (temperatura etc.)
            k = min(1.0, dt / self.BIAS_TAU)
            self.bias = [self.bias[i] + k * (st.mean[i] - self.bias[i]) for i in range(3)]

    def _learn_noise(self, st, dt):
        """Menor tremida do giroscópio vista com o acelerômetro quieto."""
        self._still_n += 1
        g_std = math.sqrt(max(st.var))
        self.gyro_noise = min(self.STILL_GYRO_STD, self.gyro_noise * (1 + self.NOISE_CLIMB * dt))
        warm = self._still_n * dt > 3 * self.WINDOW_TAU      # média já assentou
        if warm and max(st.avar) < self.STILL_ACCEL_STD ** 2 and g_std < self.gyro_noise:
            self.gyro_noise = max(self.NOISE_MIN, g_std)

    def start_calibration(self, now, seconds=2.0):
        self._manual = [[0.0, 0.0, 0.0], 0, now + seconds, False]
        self.last_calibration = "calibrando"

    # --- mira da janela ------------------------------------------------
    def reset_aim(self):
        self._orient0 = self.orient
        self._sync()

    def _sync(self):
        """Esquece a diferença entre o ângulo real e o do Dolphin."""
        self.sent_int = list(self.real_int)

    def recenter(self, now):
        """Recentralizar (analógico apertado). Com a pegada livre, o jeito que
        o controle está agora vira "apontando pra TV", como no Switch. Se a
        pegada mudou, segura o Recenter do Dolphin um instante: ele corrige a
        inclinação pelo acelerômetro aos poucos (2% a cada 5 ms) e só depois
        guarda a altura do centro."""
        self.reset_aim()
        if not self.free_grip:
            return
        st = self._still
        up = st.amean if st.amean is not None else self.raw_accel
        new = grip_rotation(up)
        if new is None:
            return
        if tilt_between(self.grip, new) > self.GRIP_CHANGE_DEG:
            self.recenter_until = now + self.RECENTER_HOLD_S
        back = _transpose(self.grip)      # o que já está pronto pra sair passa pra pegada nova
        self._out_accel = list(rotate(new, rotate(back, self._out_accel)))
        self._out_gyro = list(rotate(new, rotate(back, self._out_gyro)))
        self.grip = new
        if self.orient is not None:   # a orientação passa pros eixos da pegada nova
            change = new if back is None else _matmul(new, back)
            self.orient = _matmul(self.orient, _transpose(change))
        self._orient0 = self.orient

    def recentering(self, now):
        return now < self.recenter_until

    def aim(self, frame=VERTICAL):
        """Mira da janela: (x pra direita, y pra cima); ±1 = borda da tela.
        Passa de 1 quando aponta pra fora (no Dolphin o ponteiro sumiu); o
        desenho decide como mostrar. Limitado a ±4 só pra não crescer à toa."""
        axis, sign = frame[0]                # frente: X (em pé) ou ±Y (deitado)
        if self.orient is None or self._orient0 is None:
            return (0.0, 0.0)
        az, el = self._pointing(self.orient, axis, sign)
        az0, el0 = self._pointing(self._orient0, axis, sign)
        yaw = (az - az0 + 180.0) % 360.0 - 180.0   # + = esquerda
        pitch = el - el0
        return (max(-4.0, min(4.0, -yaw / AIM_LIMIT)),
                max(-4.0, min(4.0, pitch / AIM_LIMIT_Y)))

    # --- saída ---------------------------------------------------------
    def stalled(self, now):
        return self.last_sample_t is None or now - self.last_sample_t > self.hold_s

    def output(self, now, frame=VERTICAL):
        """Movimento atual em campos DSU. Durante um engasgo curto repete o
        último; passou de segurar_ms, zera a rotação (mantém a gravidade)."""
        stalled = self.stalled(now)
        if self._out_t is None or not 0 <= now - self._out_t <= 0.5:
            self._sync()
        else:   # o Dolphin somou a última rotação mandada até agora
            el = now - self._out_t
            for i in range(3):
                self.sent_int[i] += self._out_rate[i] * el
        rate = [0.0, 0.0, 0.0] if stalled else list(self._out_gyro)
        if not stalled:
            debt = [self.real_int[i] - self.sent_int[i] for i in range(3)]
            if max(abs(d) for d in debt) > MAX_DEBT:   # algo estranho: recomeça a conta
                self._sync()
            else:
                for i in range(3):
                    rate[i] += max(-MAX_FIX_DPS, min(MAX_FIX_DPS, debt[i] / PAYBACK_S))
        self._out_t, self._out_rate = now, rate
        accel, gyro = to_dsu(self._out_accel, rate, frame)   # já com a pegada
        return MotionOut(accel, gyro, int(now * 1e6))
