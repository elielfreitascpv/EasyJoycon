"""O servidor: junta tudo num loop só (asyncio).

- a cada 1 s procura controles novos em /dev/input e cuida de consertos
  (calibração errada do driver, conexão pela metade, fim da busca);
- lê botões e movimento assim que chegam (add_reader nos /dev/input);
- manda o estado de cada jogador pros clientes DSU (Dolphin) na hora que
  chega pacote do controle, e num ritmo fixo de 100×/s.
"""
import asyncio
import errno
import math
import logging
import queue
import time

from . import colors, hidusers, rawimu
from . import controllers as C
from . import dsu, spawn
from .i18n import _
from .bluetooth import Bluetooth
from .vibration import VirtualRumble
from .motion import SIDEWAYS_L, SIDEWAYS_R, VERTICAL
from .players import (PAIR, PRO, SOLO, PadState, PlayerManager, build_pads, parked_pad,
                      signals)

log = logging.getLogger(__name__)

SEND_INTERVAL = 0.010   # ritmo fixo (engasgos/segurar); o normal sai na hora que chega
ACTIVE_STICK = 0.4   # analógico mais longe que isso do meio = alguém mexendo
ACTIVE_GYRO = 40.0   # girando mais rápido que isso (°/s) = alguém mexendo
SPI_DELAY_S = 4.0    # espera depois de conectar antes de ler a cor e a calibração
MUTE_S = 5.0   # conectado sem mandar nada por tanto tempo = travou, reconecta
HID_USERS_S = 5.0   # de quanto em quanto tempo ver se outro programa abriu o controle
SNAPSHOT_INTERVAL = 1 / 60   # foto do estado pra janela (o desenho segue o controle)
SLOTS = dsu.SLOTS
# vibrações: [(forte, fraca, ms ligado, ms desligado)]
JOIN_BUZZ = [(0.6, 0.6, 180, 0)]                       # virou jogador (como no Switch)
IDENTIFY_BUZZ = [(1.0, 0.7, 160, 120)] * 3              # botão "identificar"
CONFIRM_BUZZ = [(0.5, 0.5, 120, 0)]                      # A: terminou de procurar
# Botão na posição do A: no Joy-Con (L) em pé é a seta da direita, deitado a de baixo
CONFIRM_BUTTONS = {"R": ("A",), "PRO": ("A",), "L": ("RIGHT", "DOWN")}
MOTION_FRAME = {"L": SIDEWAYS_L, "R": SIDEWAYS_R}       # deitado (igual ao build_pads)
LOW_BATTERY = {2: "bateria baixa", 1: "bateria acabando — carregue logo"}


def is_mute(ctrl, now):
    """Conectado inteiro, mas sem mandar movimento faz MUTE_S segundos."""
    last = ctrl.motion.last_sample_t
    if last is None:
        last = now if ctrl.connected_at is None else ctrl.connected_at
    return ctrl.complete and now - last > MUTE_S


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.controllers = {}         # mac → Controller (aberto)
        self.players = PlayerManager(cfg, led_setter=self._set_leds)
        self.bt = Bluetooth(on_message=self.players._say,
                            has_input=lambda mac: mac in self.controllers)
        self.bt.idle_stop_s = cfg["bluetooth"]["parar_sem_achar_segundos"]
        self.pads_main = [PadState() for _ in range(SLOTS)]
        self.pads_nunchuk = [PadState() for _ in range(SLOTS)]
        self.dsu_main = self.dsu_nunchuk = None
        self.no_permission = set()    # MACs cujos botões o usuário não pode ler
        self.colors = colors.load()   # MAC → cor de verdade (lida uma vez do controle)
        self.imu_cal = rawimu.load()  # MAC → calibração do IMU (idem; movimento direto)
        self.led_permission = True
        self.smoothing = cfg["movimento"]["suavizacao"]
        self.rumble_force = cfg["vibracao"]["forca"]
        self.dolphin_slots = set()     # Wii Remotes ligados no Dolphin (último configurar)
        self.stopping = asyncio.Event()
        self._loop = None
        self._started = time.monotonic()
        self._auto_search_done = False
        # Pra janela (outra thread): avisos numa fila e o estado numa "foto"
        # trocada inteira 60×/s — a janela nunca mexe nos objetos vivos.
        self.notices = queue.Queue()
        self.players.on_event = lambda kind, msg: self.notices.put((kind, msg))
        self.players.on_assigned = lambda ctrl: spawn(self.buzz(ctrl, JOIN_BUZZ))
        self.state = None
        self.rumble_devices = []      # VirtualRumble por jogador (vibração dos jogos)
        self.rumble_error = None
        self._rumbling = {}           # jogador → controle vibrando agora
        self.hid_users = {}           # mac → programas com o controle aberto (Steam…)
        self._hid_users_at = 0.0

    # --- início/fim ----------------------------------------------------
    async def run(self):
        self._loop = asyncio.get_running_loop()
        d = self.cfg["dsu"]
        self.dsu_main = await dsu.start("EasyJoycon", 0, d["porta"], lambda s: self.pads_main[s])
        self.dsu_nunchuk = await dsu.start("EasyJoycon Nunchuk", 1, d["porta_nunchuk"],
                                           lambda s: self.pads_nunchuk[s])
        if not await self.bt.start():
            self.players._say(f"Bluetooth: {self.bt.error}")
        self._create_rumble_devices()
        tasks = [asyncio.create_task(self._hotplug_loop()),
                 asyncio.create_task(self._send_loop()),
                 asyncio.create_task(self._snapshot_loop())]
        try:
            await self.stopping.wait()
        finally:
            for t in tasks:
                t.cancel()
            await self.bt.stop_search()
            for c in list(self.controllers.values()):
                self._remove(c)
            for v in self.rumble_devices:
                if v.fd is not None:
                    self._loop.remove_reader(v.fd)
                v.destroy()

    def stop(self):
        self.stopping.set()

    # --- chamadas vindas de outra thread (janela) ----------------------
    def call(self, fn, *args):
        """Roda fn(*args) — ou a corrotina que ela devolve — no loop do servidor."""
        def run():
            result = fn(*args)
            if asyncio.iscoroutine(result):
                spawn(result)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(run)

    # --- vibração dos jogos -------------------------------------------
    def _create_rumble_devices(self):
        for n in range(1, SLOTS + 1):
            v = VirtualRumble(n, self._game_rumble)
            try:
                fd = v.create()
            except OSError as e:
                self.rumble_error = str(e)
                log.warning("sem vibração dos jogos (uinput: %s)", e)
                return
            self._loop.add_reader(fd, v.handle)
            self.rumble_devices.append(v)
        log.info("vibração dos jogos: %d dispositivos virtuais", len(self.rumble_devices))

    def _game_rumble(self, number, strong, weak, length_ms):
        """O jogo mandou o Wii Remote N vibrar → Joy-Con de quem é o jogador N.
        No par, vibra o direito (é ele o Wii Remote)."""
        p = self.players.players.get(number)
        target = None
        if p is not None:
            m = p.members
            target = m.get("R") or m.get("PRO") or m.get("L")
        old = self._rumbling.get(number)
        if old is not None and old is not target:
            old.rumble(0, 0)
        if target is None:
            self._rumbling.pop(number, None)
            return
        # O Wii Remote tem um motor só (liga/desliga). O driver manda o
        # "forte" pro motor de um lado e o "fraco" pro do outro: a mesma força
        # nos dois faz o L, o R e o Pro vibrarem igual.
        level = max(strong, weak) * self.rumble_force / 100
        target.rumble(level, level, length_ms)
        self._rumbling[number] = target if (strong or weak) else None

    # --- controles entrando e saindo ----------------------------------
    def _scan(self):
        now = time.monotonic()
        found = C.scan()
        for mac, info in found.items():
            ctrl = self.controllers.get(mac)
            if ctrl is None:
                ctrl = C.Controller(mac, info["kind"], info["hid"], self.cfg)
                self.controllers[mac] = ctrl
                ctrl.motion.set_smoothing(self.smoothing)
                ctrl.connected_at = now
            was_complete = ctrl.complete
            for dev in ctrl.open_parts(info["main"], info["imu"]):
                self._loop.add_reader(dev.fd, self._on_readable, ctrl, dev)
            if ctrl.no_permission:
                self.no_permission.add(mac)
            else:
                self.no_permission.discard(mac)
            if ctrl.complete and not was_complete:
                self.players.controller_connected(ctrl, now)
                if ctrl.mac in self.imu_cal:
                    self._start_raw(ctrl)
                if self._wants_spi(ctrl):
                    spawn(self._fetch_spi(ctrl))
        for mac in set(self.controllers) - set(found):
            self._remove(self.controllers[mac])

    def _start_raw(self, ctrl):
        if not self.cfg["movimento"]["direto"]:
            return
        raw = ctrl.start_raw(self.imu_cal[ctrl.mac])
        if raw is not None:
            self._loop.add_reader(raw.fd, self._on_readable, ctrl, raw)
            log.info("%s: movimento direto do controle", ctrl.name)

    def _remove(self, ctrl):
        for dev in (ctrl.main, ctrl.imu, ctrl.raw):
            if dev is not None and dev.fd is not None:
                self._loop.remove_reader(dev.fd)
        was_complete = ctrl.complete
        ctrl.close()
        self.controllers.pop(ctrl.mac, None)
        self.no_permission.discard(ctrl.mac)
        if was_complete:
            self.players.controller_gone(ctrl)

    def _on_readable(self, ctrl, dev):
        now = time.monotonic()
        try:
            if dev is ctrl.imu or dev is ctrl.raw:
                (ctrl.read_raw if dev is ctrl.raw else ctrl.read_imu)(now)
                if max(abs(v) for v in ctrl.motion._out_gyro) > ACTIVE_GYRO:
                    ctrl.last_active = now
            elif ctrl.read_main():
                ctrl.last_active = now
                self._nunchuk_gesture(ctrl)
                if ctrl.pressed("RS") or ctrl.pressed("LS"):   # = Recenter do Dolphin
                    if self._is_pointer(ctrl):
                        ctrl.motion.recenter(now)
                    else:
                        ctrl.motion.reset_aim()
                # procurando = "tela de controles" do Switch: dá pra juntar
                # dois Joy-Cons que já são jogadores (L + R)
                self.players.buttons_changed(ctrl, now, regroup=self.bt.searching)
                self._confirm_search(ctrl)
            if ctrl.player is not None:
                self._send_player(ctrl.player.number, now)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                self._remove(ctrl)
            else:
                log.warning("%s: erro lendo (%s)", ctrl.name, e)

    def _nunchuk_gesture(self, ctrl):
        """Botão de captura (o quadradinho do Joy-Con esquerdo / do Pro):
        tira ou põe o Nunchuk, como o jogo às vezes pede."""
        down = ctrl.pressed("CAP")
        was, ctrl.cap_down = getattr(ctrl, "cap_down", False), down
        p = ctrl.player
        if (down and not was and p is not None and p.mode in (PAIR, PRO)
                and ctrl.kind in ("L", "PRO")):
            self.players.toggle_nunchuk(p.number)

    def toggle_nunchuk(self, number):
        self.players.toggle_nunchuk(number)

    @staticmethod
    def _is_pointer(ctrl):
        """O analógico desse controle é o Recenter do Dolphin? No par só o do
        direito (o esquerdo é o Nunchuk, que não aponta)."""
        p = ctrl.player
        return p is not None and not (p.mode == PAIR and ctrl.kind == "L")

    def _confirm_search(self, ctrl):
        """Como no Switch: procurando, apertar A num controle conectado termina."""
        if not self.bt.searching:
            return
        if any(ctrl.pressed(b) for b in CONFIRM_BUTTONS.get(ctrl.kind, ())):
            spawn(self.bt.stop_search())
            spawn(self.buzz(ctrl, CONFIRM_BUZZ))

    def _search_seconds(self):
        s = self.cfg["bluetooth"]["procurar_segundos"]
        return s if s > 0 else math.inf

    def _set_leds(self, ctrl, pattern):
        if not self.led_permission or ctrl.led_pattern == pattern:
            return

        def done(fut):
            if fut.result() is False and self.led_permission:
                self.led_permission = False
                self.players._say(_("sem permissão pros LEDs — clique em \"Preparar o "
                                    "sistema\" e depois saia e entre de novo no computador"))
        self._loop.run_in_executor(None, ctrl.set_leds, pattern).add_done_callback(done)

    # --- laços ---------------------------------------------------------
    async def _hotplug_loop(self):
        while True:
            try:
                self._scan()
                await self._maintenance()
            except Exception:  # um erro aqui não pode derrubar o servidor
                log.exception("erro na manutenção")
            await asyncio.sleep(1.0)

    async def _maintenance(self):
        now = time.monotonic()
        self.players.tick(now)
        self._refresh_dolphin_slots()
        self._check_batteries()
        bt = self.bt
        if bt.searching_until and not bt.searching:
            await bt.stop_search()
        if not bt.adapter:
            return
        # Procurar sozinho ao abrir, se ninguém conectado
        if (not self._auto_search_done and now - self._started > 3
                and self.cfg["bluetooth"]["procurar_ao_abrir"]):
            self._auto_search_done = True
            if not bt.connected_macs():
                await bt.search(self._search_seconds())
        # Movimento sempre zero = o driver leu a calibração errada ao conectar
        for ctrl in list(self.controllers.values()):
            if ctrl.motion.broken_driver_cal and now - (ctrl.connected_at or now) > 3:
                await bt.reconnect(ctrl.mac, _("o movimento veio zerado (calibração lida errada)"))
        self._power_off_idle(now)
        if now - self._hid_users_at >= HID_USERS_S:
            self._hid_users_at = now
            await self._check_hid_users()
        # Conectado, mas parou de mandar dados (o R travou assim ao juntar os
        # dois em 2026-10-10: "timeout waiting for input report" sem parar).
        # Parado na mesa ele continua mandando (~200 leituras/s), então
        # silêncio longo = travou.
        for ctrl in list(self.controllers.values()):
            if is_mute(ctrl, now):
                await bt.reconnect(ctrl.mac,
                                   _("parou de mandar dados faz {s} s", s=f"{MUTE_S:.0f}"))
        # Conectado no Bluetooth, mas o Linux não criou o controle
        for mac in bt.half_connected(set(self.controllers)):
            await bt.reconnect(mac, _("ficou conectado pela metade"))

    def _wants_spi(self, ctrl):
        return ctrl.mac not in self.colors or ctrl.kind in rawimu.KINDS

    async def _fetch_spi(self, ctrl):
        """Lê da memória do controle a calibração do IMU (fica em
        data/calibracao-imu.json; liga o movimento direto) e, uma vez só, a
        cor (fica em data/cores.json). A calibração é conferida a cada
        conexão: a guardada já vale desde o começo, mas pode ter mudado
        (recalibrado no Switch). Espera a conexão assentar: logo ao conectar
        o driver está lendo a calibração dele pelo mesmo caminho. Um pedido
        por vez."""
        await asyncio.sleep(SPI_DELAY_S)
        if self.controllers.get(ctrl.mac) is not ctrl:
            return
        if ctrl.kind in rawimu.KINDS:
            cal = await self._loop.run_in_executor(None, rawimu.read_calibration, ctrl.hid)
            if (cal is not None and cal != self.imu_cal.get(ctrl.mac)
                    and self.controllers.get(ctrl.mac) is ctrl):
                self.imu_cal[ctrl.mac] = cal
                self._save(rawimu.save, self.imu_cal, "a calibração do movimento")
                if ctrl.raw is not None:
                    ctrl.raw.convert = rawimu.Converter(ctrl.kind, cal)
                    log.info("%s: calibração do movimento mudou", ctrl.name)
                else:
                    self._start_raw(ctrl)
        if ctrl.mac not in self.colors and self.controllers.get(ctrl.mac) is ctrl:
            got = await self._loop.run_in_executor(None, colors.read_colors, ctrl.hid)
            if got is not None:
                self.colors[ctrl.mac] = got
                self._save(colors.save, self.colors, "as cores")

    @staticmethod
    def _save(fn, data, what):
        try:
            fn(data)
        except OSError as e:
            log.warning("não salvou %s: %s", what, e)

    async def _check_hid_users(self):
        """Outro programa (a Steam, quase sempre) com o controle aberto manda
        comandos próprios e estraga a calibração e a reconexão: avisa uma
        vez quando aparece; a janela mostra enquanto durar."""
        paths = {colors.hidraw_path(c.hid): c.mac for c in self.controllers.values()}
        paths.pop(None, None)
        found = await self._loop.run_in_executor(None, hidusers.users, list(paths))
        now_users = {paths[p]: sorted(names) for p, names in found.items() if p in paths}
        for mac, names in now_users.items():
            if names != self.hid_users.get(mac):
                ctrl = self.controllers.get(mac)
                who = ctrl.name if ctrl else mac
                self.players._say(_("{who} está usando o {ctrl}: feche pra não estragar a "
                                    "calibração e a reconexão", who=_(" e ").join(names),
                                    ctrl=who), "aviso")
        self.hid_users = now_users

    def _power_off_idle(self, now):
        """Como o Switch quando dorme: Dolphin fechado e ninguém mexendo no
        controle faz desligar_parado_minutos → desconecta (poupa bateria)."""
        minutes = self.cfg["bluetooth"]["desligar_parado_minutos"]
        if not minutes or self.dolphin_connected or self.bt.searching:
            return
        for ctrl in list(self.controllers.values()):
            if not ctrl.complete:
                continue
            stick = ctrl.stick("left" if ctrl.kind == "PRO" else "main")
            if any(abs(v) > ACTIVE_STICK for v in stick):
                ctrl.last_active = now
            last = ctrl.last_active if ctrl.last_active is not None else ctrl.connected_at
            if last is not None and now - last > minutes * 60:
                ctrl.last_active = now                 # não pedir de novo a cada segundo
                spawn(self.bt.reconnect(
                    ctrl.mac, f"ninguém mexeu faz {minutes} min, pra poupar a bateria"))

    def _pads(self, n, now):
        """Pads DSU do jogador n. Wii Remote que o Dolphin tem configurado
        mas sem jogador (ex.: o app reiniciou e o controle não voltou): manda
        apontando pro teto, pra não ficar um ponteiro parado na tela."""
        p = self.players.players.get(n)
        if p is None and n in self.dolphin_slots:
            return parked_pad(now), PadState()
        return build_pads(p, now)

    def _send_signals(self):
        """Nunchuk conectado e deitado, por jogador, pro Dolphin (pelo
        dispositivo virtual — mapeamento universal). Só escreve quando muda."""
        for v in self.rumble_devices:
            v.set_signals(*signals(self.players.players.get(v.number)))

    def _refresh_dolphin_slots(self):
        from . import dolphin
        self.dolphin_slots = set(dolphin.applied_slots() or [])

    def _send_player(self, n, now):
        """Monta os pads do jogador n e manda já (sem esperar o próximo ciclo)."""
        if self.dsu_main is None or not 1 <= n <= SLOTS:
            return
        main, nun = self._pads(n, now)
        self.pads_main[n - 1], self.pads_nunchuk[n - 1] = main, nun
        self.dsu_main.send_slot(n - 1, now)
        self.dsu_nunchuk.send_slot(n - 1, now)

    async def _send_loop(self):
        next_t = time.monotonic()
        while True:
            now = time.monotonic()
            for n in range(1, SLOTS + 1):
                main, nun = self._pads(n, now)
                self.pads_main[n - 1], self.pads_nunchuk[n - 1] = main, nun
            self._send_signals()
            self.dsu_main.send_pads(now)
            self.dsu_nunchuk.send_pads(now)
            next_t += SEND_INTERVAL
            delay = next_t - time.monotonic()
            if delay < -0.1:          # atrasou muito (computador ocupado): recomeça
                next_t = time.monotonic()
                delay = 0
            await asyncio.sleep(max(delay, 0))

    # --- comandos da tela ---------------------------------------------
    async def toggle_search(self):
        if self.bt.searching:
            await self.bt.stop_search()
        else:
            await self.bt.search(self._search_seconds())

    def calibrate(self):
        now = time.monotonic()
        for c in self.controllers.values():
            c.motion.start_calibration(now)
        self.players._say(_("calibrando: deixe os controles parados por 2 segundos"))

    def change_smoothing(self, delta):
        self.set_smoothing(self.smoothing + delta)

    def set_smoothing(self, level):
        from . import config
        level = max(0, min(10, int(level)))
        if level == self.smoothing:
            return
        self.smoothing = level
        for c in self.controllers.values():
            c.motion.set_smoothing(level)
        try:
            config.save_value("movimento", "suavizacao", level)
        except OSError as e:
            log.warning("não salvou a suavização: %s", e)

    def set_rumble_force(self, force):
        from . import config
        force = max(0, min(100, int(force)))
        if force == self.rumble_force:
            return
        self.rumble_force = force
        try:
            config.save_value("vibracao", "forca", force)
        except OSError as e:
            log.warning("não salvou a força da vibração: %s", e)

    def toggle_single_mode(self, number):
        self.players.toggle_single_mode(number)

    def join_single(self, number):
        self.players.join_single(number)

    def split_pair(self, number):
        self.players.split_pair(number)

    def join_or_split(self, number):
        """Botão do cartão: par → separa; Joy-Con sozinho → junta."""
        p = self.players.players.get(number)
        if p is not None and p.mode == PAIR:
            self.split_pair(number)
        else:
            self.join_single(number)

    def reset_players(self):
        self.players.reset([c for c in self.controllers.values() if c.complete])

    async def buzz(self, ctrl, pattern):
        for strong, weak, on_ms, off_ms in pattern:
            if not ctrl.rumble(strong, weak, on_ms):
                return
            await asyncio.sleep((on_ms + off_ms) / 1000)

    async def identify(self, number):
        """Vibra e acende todos os LEDs do(s) controle(s) do jogador por 1 s."""
        p = self.players.players.get(number)
        if p is None:
            return
        for c in p.members.values():
            spawn(self.buzz(c, IDENTIFY_BUZZ))
            self._set_leds(c, (1, 1, 1, 1))
        await asyncio.sleep(1.2)
        for c in p.members.values():
            self._set_leds(c, C.LED_PLAYER[number])

    def _check_batteries(self):
        for c in self.controllers.values():
            level, charging = c.battery()
            if charging and level >= 3:
                c.battery_warned = 9          # carregou: pode avisar de novo depois
            elif level in LOW_BATTERY and level < c.battery_warned and not charging:
                c.battery_warned = level
                quem = _(" do jogador {n}", n=c.player.number) if c.player else ""
                self.players._say(f"{c.name}{quem}: {_(LOW_BATTERY[level])}", "aviso")

    async def forget_and_search(self):
        await self.bt.forget_and_search(self._search_seconds())

    async def forget_all(self):
        """Esquece todos os controles, apaga os jogadores e já procura de novo."""
        n = await self.bt.forget_all()
        if n is None:
            self.players._say(_("sem adaptador Bluetooth agora — não deu pra esquecer"), "aviso")
            return
        self.reset_players()
        self.players._say(_("esqueci {n} controle(s) — pareie um de cada vez", n=n))
        await self.bt.search(self._search_seconds())

    # --- foto do estado pra janela ------------------------------------
    async def _snapshot_loop(self):
        while True:
            try:
                self.state = self.snapshot()
            except Exception:  # a foto não pode derrubar o servidor
                log.exception("erro montando o estado")
            await asyncio.sleep(SNAPSHOT_INTERVAL)

    def _ctrl_state(self, c, now):
        m = c.motion
        if m.broken_driver_cal:
            motion = "zerado"
        elif m.last_calibration == "calibrando":
            motion = "calibrando"
        elif m.stalled(now):
            motion = "sem dados"
        else:
            motion = "ok"
        level, charging = c.battery()
        frame = MOTION_FRAME.get(c.kind, VERTICAL) if (
            c.player is not None and c.player.mode == SOLO) else VERTICAL
        ax, ay = c.motion.aim(frame)
        return {"mac": c.mac, "kind": c.kind, "name": c.name, "battery": level,
                "aim": (round(ax, 2), round(ay, 2)),   # 0,2°: ruído parado não redesenha
                "charging": charging, "led": c.led_pattern, "motion": motion,
                "color": self.colors.get(c.mac),
                "calibrated": m.calibrated,
                "hiccups": c.hiccups_per_min(now), "buttons": frozenset(c.buttons),
                "stick": c.stick("left" if c.kind == "PRO" else "main"),
                "rstick": c.stick("right") if c.kind == "PRO" else (0.0, 0.0)}

    def snapshot(self):
        now = time.monotonic()
        players = {}
        for n, p in self.players.players.items():
            players[n] = {"number": n, "mode": p.mode, "complete": p.complete,
                          "nunchuk": p.nunchuk,
                          "members": {r: self._ctrl_state(c, now) for r, c in p.members.items()},
                          "missing": [r for r in p.macs if r not in p.members]}
        bt = self.bt
        return {
            "bt_ok": bt.adapter is not None, "bt_error": bt.error, "bt_name": bt.adapter_name,
            "searching": bt.searching, "search_left": max(0.0, bt.searching_until - now),
            "dolphin": self.dolphin_connected, "players": players,
            "waiting": [self._ctrl_state(c, now) for c in self.players.waiting.values()],
            "no_permission": len(self.no_permission), "led_permission": self.led_permission,
            "smoothing": self.smoothing, "unbonded": bt.unbonded(),
            "uinput_error": self.rumble_error,
            "hid_users": sorted({n for names in self.hid_users.values() for n in names}),
            "layout": {n: p.mode for n, p in self.players.players.items()},
        }

    @property
    def dolphin_connected(self):
        return self.dsu_main is not None and self.dsu_main.has_clients
