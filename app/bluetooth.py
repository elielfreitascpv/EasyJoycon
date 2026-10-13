"""Bluetooth pelo BlueZ (D-Bus do sistema): acha controles da Nintendo,
pareia, marca como confiável e conecta sozinho — o "procurar controles" da
tela do Switch.

Procurar divide o rádio com os controles já conectados (com adaptador fraco
dá tranco), então só procura por um tempo, quando pedido.
"""
import asyncio
import logging
import re
import time

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, method

from . import spawn
from .i18n import _

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
AGENT_PATH = "/easyjoycon/agent"
NINTENDO_NAME = re.compile(r"^(Joy-Con \((L|R)\)|Pro Controller)$")
HALF_CONNECTED_S = 10.0   # conectado no Bluetooth mas sem virar controle
INPUT_WAIT_S = 20.0       # depois de conectar, quanto esperar o Linux criar o controle
ZERO_IMU_RETRY_S = 60.0   # não reconectar o mesmo controle mais de 1× por minuto


def pick_adapter(found):
    """Com dois adaptadores (o do notebook + o USB), o ligado; o desligado
    pelo rfkill ("off-blocked") por último. Empate: o primeiro (hci0)."""
    def rank(path):
        props = found[path]
        state = props.get("PowerState", Variant("s", "")).value
        powered = props.get("Powered", Variant("b", False)).value
        return (state == "off-blocked", not powered)
    return min(found, key=rank)


def mac_from_path(path):
    return path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()


class Agent(ServiceInterface):
    """Agente só pros pareamentos que este programa pede: controle da
    Nintendo não tem tela nem teclado, então aceita sem PIN — e só pra eles."""

    def __init__(self, bt):
        super().__init__("org.bluez.Agent1")
        self.bt = bt

    def _ok(self, device):
        if not NINTENDO_NAME.match(self.bt._all.get(device, {}).get("Name", "")):
            raise DBusError("org.bluez.Error.Rejected", "não é controle da Nintendo")

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        self._ok(device)
        return "0000"

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        self._ok(device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        pass

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        self._ok(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        self._ok(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        self._ok(device)

    @method()
    def Cancel(self):
        pass


class Bluetooth:
    def __init__(self, on_message=None, has_input=None):
        self.bus = None
        self.adapter = None          # caminho, ex.: /org/bluez/hci0
        self.adapter_name = ""
        self.powered = False
        self._all = {}               # caminho → propriedades de todo aparelho conhecido
        self.searching_until = 0.0
        self.search_deadline = 0.0   # fim pedido da busca (inf = até apertar A)
        self.idle_stop_s = 0.0       # para depois de tanto tempo sem achar controle (0 = não)
        self.error = None
        self._busy = set()           # caminhos com pareamento/conexão em andamento
        self._connected_since = {}
        self._last_fix = {}
        self._rebond_tried = set()   # caminhos já repareados nesta sessão
        self._pair_lock = None       # um pareamento por vez (asyncio.Lock)
        self._paused = False         # busca pausada durante um pareamento
        self._has_input = has_input or (lambda mac: True)
        self._resume_after_reset = 0.0
        self._say = on_message or (lambda msg: None)

    # --- início --------------------------------------------------------
    async def start(self):
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except (OSError, DBusError) as e:
            self.error = _("sem acesso ao Bluetooth ({error})", error=e)
            return False
        self.bus.export(AGENT_PATH, Agent(self))
        self.bus.add_message_handler(self._on_signal)
        await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "AddMatch", "s",
                         ["type='signal',sender='org.bluez'"])
        await self._load_objects()
        # O agente fica no bluetoothd, não no adaptador: registra mesmo sem
        # adaptador (antes, plugando o adaptador depois, parear pelo app não
        # tinha agente até reabrir o app).
        try:
            await self._call(BLUEZ, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                             "os", [AGENT_PATH, "NoInputNoOutput"])
        except DBusError as e:
            log.warning("não registrou o agente de pareamento: %s", e)
        if self.adapter is None:
            self.error = _("nenhum adaptador Bluetooth encontrado")
            return False
        return True

    async def _call(self, dest, path, iface, member, signature="", body=None, *, timeout=None):
        msg = Message(destination=dest, path=path, interface=iface, member=member,
                      signature=signature, body=body or [])
        coro = self.bus.call(msg)
        reply = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def _load_objects(self):
        (objs,) = await self._call(BLUEZ, "/", "org.freedesktop.DBus.ObjectManager",
                                   "GetManagedObjects")
        found = {p: i["org.bluez.Adapter1"] for p, i in objs.items() if "org.bluez.Adapter1" in i}
        if found and self.adapter is None:
            path = pick_adapter(found)
            self._set_adapter(path, found[path])
        for path, ifaces in objs.items():
            if "org.bluez.Device1" in ifaces:
                self._update_device(path, ifaces["org.bluez.Device1"])

    def _set_adapter(self, path, props):
        self.adapter = path
        self.adapter_name = path.rsplit("/", 1)[-1]
        self.error = None
        self.powered = bool(props.get("Powered", Variant("b", False)).value)

    @property
    def devices(self):
        """Só os controles da Nintendo: caminho → propriedades."""
        return {p: v for p, v in self._all.items() if NINTENDO_NAME.match(v.get("Name", ""))}

    def _update_device(self, path, changed):
        if self.adapter and not path.startswith(self.adapter + "/"):
            return
        props = self._all.setdefault(path, {})
        was_nintendo = NINTENDO_NAME.match(props.get("Name", ""))
        props.update({k: v.value for k, v in changed.items()})
        if not NINTENDO_NAME.match(props.get("Name", "")):
            return
        new = not was_nintendo
        if props.get("Connected"):
            self._connected_since.setdefault(path, time.monotonic())
        else:
            self._connected_since.pop(path, None)
        if self.searching and not props.get("Connected") and path not in self._busy:
            # Apareceu na busca = está em modo de pareamento (botão de sync)
            if new or "RSSI" in changed:
                spawn(self._pair_and_connect(path))

    def _on_signal(self, msg):
        if msg.message_type != MessageType.SIGNAL:
            return
        if msg.member == "InterfacesAdded":
            path, ifaces = msg.body
            if "org.bluez.Device1" in ifaces:
                self._update_device(path, ifaces["org.bluez.Device1"])
            elif "org.bluez.Adapter1" in ifaces and self.adapter is None:
                self._set_adapter(path, ifaces["org.bluez.Adapter1"])
                if self._resume_after_reset:
                    left = self._resume_after_reset
                    self._resume_after_reset = 0.0
                    self._say(_("adaptador Bluetooth voltou — procurando de novo"))
                    spawn(self.search(left))
        elif msg.member == "InterfacesRemoved":
            path, ifaces = msg.body
            if "org.bluez.Device1" in ifaces:
                self._all.pop(path, None)
                self._connected_since.pop(path, None)
            if path == self.adapter:
                # Adaptador sumiu (o clone de CSR às vezes "desconecta do USB")
                self._resume_after_reset = max(0.0, self.searching_until - time.monotonic())
                self.adapter, self.powered = None, False
                self.searching_until, self._paused = 0.0, False
                self._all.clear()
                self._connected_since.clear()
                self._say(_("o adaptador Bluetooth reiniciou sozinho — os controles caíram"),
                          "aviso")
        elif msg.member == "PropertiesChanged" and msg.path:
            iface, changed, _invalidated = msg.body
            if iface == "org.bluez.Device1":
                self._update_device(msg.path, changed)
            elif iface == "org.bluez.Adapter1" and msg.path == self.adapter:
                if "Powered" in changed:
                    self.powered = bool(changed["Powered"].value)
                if ("Discovering" in changed and not changed["Discovering"].value
                        and not self._paused):
                    self.searching_until = min(self.searching_until, time.monotonic())

    # --- procurar ------------------------------------------------------
    @property
    def searching(self):
        return time.monotonic() < self.searching_until

    async def search(self, seconds):
        if not self.adapter:
            return
        try:
            if not self.powered:
                await self._call(BLUEZ, self.adapter, "org.freedesktop.DBus.Properties", "Set",
                                 "ssv", ["org.bluez.Adapter1", "Powered", Variant("b", True)])
            await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "SetDiscoveryFilter",
                             "a{sv}", [{"Transport": Variant("s", "bredr")}])
            await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "StartDiscovery")
        except DBusError as e:
            if "InProgress" not in str(e.type):
                self._say(_("não consegui procurar: {error}", error=e.text), "aviso")
                return
        self.search_deadline = time.monotonic() + seconds
        self._extend_search()
        self._say(_("procurando… segure o botãozinho de sync na lateral do controle")
                  + (_(" (terminou? aperte A)") if seconds == float("inf") else ""))
        # controles já conhecidos que estão em modo de pareamento
        for path, props in list(self.devices.items()):
            if not props.get("Connected") and "RSSI" in props:
                spawn(self._pair_and_connect(path))

    def _extend_search(self):
        """Procurar atrapalha os controles já conectados (o rádio fica
        ocupado: o R chegou a ficar 4,7 s sem mandar movimento em 2026-10-10),
        então a busca para sozinha depois de idle_stop_s sem achar controle."""
        end = self.search_deadline
        if self.idle_stop_s:
            end = min(end, time.monotonic() + self.idle_stop_s)
        self.searching_until = end

    async def stop_search(self):
        if not self.adapter or not self.searching_until:
            return
        self.searching_until = 0.0
        try:
            await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "StopDiscovery")
        except DBusError:
            pass
        self._say(_("parei de procurar"))

    async def _pair_and_connect(self, path):
        """Um controle por vez, com a busca pausada: procurar e conectar ao
        mesmo tempo derrubou o adaptador (USB disconnect em 2026-10-07)."""
        if path in self._busy:
            return
        self._busy.add(path)
        if self._pair_lock is None:
            self._pair_lock = asyncio.Lock()
        try:
            async with self._pair_lock:
                if self.devices.get(path, {}).get("Connected"):
                    return
                await self._pause_search()
                try:
                    await self._pair_and_connect_locked(path)
                finally:
                    if self.searching:          # achou um: conta o tempo de novo
                        self._extend_search()
                    await self._resume_search()
        finally:
            self._busy.discard(path)

    async def _pause_search(self):
        if self.searching:
            self._paused = True
            try:
                await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "StopDiscovery")
            except DBusError:
                pass

    async def _resume_search(self):
        if not self._paused:
            return
        self._paused = False
        if self.searching and self.adapter:
            try:
                await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "StartDiscovery")
            except DBusError as e:
                if "InProgress" not in str(e.type):
                    log.warning("não retomou a busca: %s", e)

    async def _wait_input(self, mac):
        end = time.monotonic() + INPUT_WAIT_S
        while time.monotonic() < end:
            if self._has_input(mac):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _pair_and_connect_locked(self, path):
        name = self.devices.get(path, {}).get("Name", _("controle"))
        try:
            props = self.devices.get(path, {})
            if props.get("Paired") and not props.get("Bonded") and path not in self._rebond_tried:
                # Pareado sem guardar a chave: o controle não consegue reconectar
                # sozinho. Esquece e pareia de novo (a busca acha ele de novo).
                self._rebond_tried.add(path)
                self._say(_("{name}: pareando de novo pra ele lembrar deste computador", name=name))
                await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "RemoveDevice",
                                 "o", [path])
                return
            if not props.get("Paired"):
                self._say(_("pareando {name}…", name=name))
                try:
                    await self._call(BLUEZ, path, "org.bluez.Device1", "Pair", timeout=25)
                except (DBusError, TimeoutError) as e:
                    if "AlreadyExists" not in str(getattr(e, "type", "")):
                        log.info("Pair %s falhou (%s); tentando conectar direto", name, e)
            await self._call(BLUEZ, path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                             ["org.bluez.Device1", "Trusted", Variant("b", True)])
            # Depois do Pair() o Bluetooth já fica "Connected", mas só o
            # Connect() abre o canal do controle (HID). Sem ele o Joy-Con só
            # aparecia depois de desconectar e ele voltar sozinho (~25 s).
            if not self._has_input(mac_from_path(path)):
                try:
                    await self._call(BLUEZ, path, "org.bluez.Device1", "Connect", timeout=25)
                except DBusError as e:
                    if not any(k in str(e.type) for k in ("AlreadyConnected", "InProgress")):
                        raise
            if not await self._wait_input(mac_from_path(path)):
                # Conectou no Bluetooth mas o Linux não criou o controle. Sem
                # ServicesResolved o adaptador travou no meio da conversa
                # (o clone de CSR faz isso; visto no btmon em 2026-10-07).
                if self.devices.get(path, {}).get("ServicesResolved"):
                    self._say(_("{name}: conectou pela metade — segure o sync de novo", name=name),
                              "aviso")
                else:
                    self._say(_("{name}: o adaptador Bluetooth travou no meio da conexão — "
                                "segure o sync de novo (se repetir, é o adaptador)", name=name),
                              "aviso")
                await self._call(BLUEZ, path, "org.bluez.Device1", "Disconnect", timeout=10)
                return
            bonded = self.devices.get(path, {}).get("Bonded")
            self._say(_("{name} pareado", name=name) + ("" if bonded else _(
                      " (sem guardar a chave: vai precisar parear de novo da próxima vez)")),
                      "info" if bonded else "aviso")
        except (DBusError, TimeoutError) as e:
            why = getattr(e, "text", "") or _("demorou demais")
            self._say(_("{name}: não conectou ({why}) — segure o sync de novo", name=name,
                        why=why), "aviso")

    # --- consertos -----------------------------------------------------
    async def reconnect(self, mac, reason):
        """Desconecta um controle pra ele reconectar do zero (o driver lê a
        calibração de novo). O controle volta sozinho ao apertar um botão."""
        path = f"{self.adapter}/dev_{mac.replace(':', '_')}"
        now = time.monotonic()
        if now - self._last_fix.get(path, -1e9) < ZERO_IMU_RETRY_S:
            return False
        self._last_fix[path] = now
        name = self.devices.get(path, {}).get("Name", mac)
        self._say(_("{name}: {reason} — desconectei; aperte um botão nele pra voltar", name=name,
                    reason=reason), "aviso")
        try:
            await self._call(BLUEZ, path, "org.bluez.Device1", "Disconnect", timeout=10)
        except (DBusError, TimeoutError) as e:
            log.warning("Disconnect %s: %s", name, e)
        return True

    def half_connected(self, input_macs):
        """Controles "conectados" no Bluetooth faz tempo mas que não viraram
        controle no Linux (aconteceu antes do ClassicBondedOnly=false)."""
        now = time.monotonic()
        out = []
        for path, since in self._connected_since.items():
            if path in self._busy:      # pareando agora: quem decide é o pareamento
                continue
            mac = mac_from_path(path)
            if mac not in input_macs and now - since > HALF_CONNECTED_S:
                out.append(mac)
        return out

    def unbonded(self):
        """Controles pareados sem a chave guardada (não reconectam sozinhos)."""
        return [(mac_from_path(p), v.get("Name", "")) for p, v in self.devices.items()
                if v.get("Paired") and not v.get("Bonded")]

    async def forget_and_search(self, seconds):
        """Botão "parear de novo": esquece os controles sem chave e procura."""
        for path, props in list(self.devices.items()):
            if props.get("Paired") and not props.get("Bonded"):
                self._rebond_tried.add(path)
                try:
                    await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "RemoveDevice",
                                     "o", [path])
                except DBusError as e:
                    log.warning("RemoveDevice %s: %s", path, e)
        await self.search(seconds)

    async def forget_all(self):
        """Botão "esquecer controles": tira do Bluetooth todo controle da
        Nintendo, como "remover dispositivo" nas configurações. Outros
        aparelhos (fone etc.) ficam. Devolve quantos esqueceu, ou None sem
        adaptador."""
        if not self.adapter:
            return None
        if self.searching_until:
            self.searching_until, self._paused = 0.0, False
            try:
                await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "StopDiscovery")
            except DBusError:
                pass
        n = 0
        for path in list(self.devices):
            try:
                await self._call(BLUEZ, self.adapter, "org.bluez.Adapter1", "RemoveDevice",
                                 "o", [path], timeout=10)
                n += 1
            except (DBusError, TimeoutError) as e:
                log.warning("RemoveDevice %s: %s", path, e)
        self._rebond_tried.clear()
        self._last_fix.clear()
        return n

    def connected_macs(self):
        return {mac_from_path(p) for p, v in self.devices.items() if v.get("Connected")}
