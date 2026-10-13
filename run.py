#!/usr/bin/env python3
"""EasyJoycon — Joy-Cons no Dolphin com movimento, pelo protocolo DSU.

Uso:
  python run.py                      abre o servidor (janela)
  python run.py --terminal           abre o servidor com a tela no terminal
  python run.py configurar-dolphin   liga os Wii Remotes no Dolphin (com ele fechado)
  python run.py diagnostico          confere o sistema e diz o que falta
  python run.py testar               mostra ao vivo o que o Dolphin vai receber
  python run.py medir [segundos]     mede os engasgos do Bluetooth (compara adaptadores)
"""
import asyncio
import grp
import os
import signal
import subprocess
import sys
from pathlib import Path

from app import config, log


def _gui_available():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def cmd_gui(hidden=False):
    from app import gui
    from app.server import Server

    log.setup()
    return gui.run(Server(config.load()), hidden)


def cmd_server():
    from app.server import Server
    from app.ui import TerminalUI

    cfg = config.load()
    ui_on = sys.stdin.isatty() and sys.stdout.isatty()
    log.setup(console=not ui_on)

    async def main():
        server = Server(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, server.stop)
        ui = TerminalUI(server)
        try:
            await asyncio.gather(server.run(), ui.run())
        finally:
            ui.restore()

    try:
        asyncio.run(main())
    except OSError as e:
        if e.errno == 98:
            print("A porta do DSU já está em uso — o EasyJoycon já está aberto em outro terminal?")
            return 1
        raise
    print("EasyJoycon fechado.")
    return 0


def cmd_configure(args):
    from app import dolphin

    cfg = config.load()
    log.setup()
    layout = dolphin.layout_from_memory()   # jogadores lembrados (além dos Wii Remotes ligados)
    try:
        path, backup = dolphin.configure(layout, cfg["dsu"]["porta"], cfg["dsu"]["porta_nunchuk"],
                                         cfg["dolphin"]["wiimotes"])
    except RuntimeError as e:
        print(f"Não configurei: {e}.")
        if not dolphin.installed():
            print("\n" + dolphin.INSTALL_HELP.replace('clicar de novo em "Abrir Dolphin"',
                                                      "rodar este comando de novo"))
        return 1
    print(f"Dolphin configurado em {path}")
    if backup:
        print(f"Cópia da configuração anterior: {backup}")
    slots = dolphin.slots_for(layout, cfg["dolphin"]["wiimotes"])
    print(f"Wii Remotes ligados: {', '.join(map(str, slots))}. O jeito de jogar (par, em pé, "
          "deitado, Nunchuk) muda com o Dolphin aberto.")
    return 0


PREPARE = ('clique em "Preparar o sistema" na janela (ou rode '
           'scripts/instalar-sistema.sh)')


def _ok(cond, good, bad):
    print(("  ✔ " + good) if cond else ("  ✘ " + bad))
    return cond


def _running(name):
    return subprocess.run(["pgrep", "-x", name], capture_output=True,
                          check=False).returncode == 0


def cmd_diagnose():
    from app import colors, controllers, dolphin, hidusers, sistema

    from app import adapter

    print("Bluetooth")
    found = adapter.adapters()
    _ok(bool(found), f"{len(found)} adaptador(es)", "nenhum adaptador Bluetooth")
    for a in found:
        _ok(a["good"], f"{a['hci']}: {a['chip']} {a['vid']}:{a['pid']} — {a['verdict']}",
            f"{a['hci']}: {a['chip']} {a['vid']}:{a['pid']} — {a['verdict']}")
        if a["blocked"]:
            print(f"  · {a['hci']} desligado (rfkill)")
        if a["drops"]:
            print(f"  ! {a['hci']} caiu do USB {a['drops']} vez(es) desde que o PC ligou")
    try:
        conf = Path("/etc/bluetooth/input.conf").read_text()
    except OSError:
        conf = ""
    _ok("\nClassicBondedOnly=false" in "\n" + conf.replace(" ", ""),
        "BlueZ aceita Joy-Con (ClassicBondedOnly=false)",
        f"falta ClassicBondedOnly=false — {PREPARE}")

    print("Permissões")
    groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    _ok("input" in groups, "usuário no grupo input", "usuário fora do grupo input")
    _ok(sistema._rules_version() >= sistema.RULES_VERSION, "regras do udev instaladas",
        f"faltam as regras do udev (ou são antigas) — {PREPARE}")
    _ok(os.access("/dev/uinput", os.R_OK | os.W_OK),
        "controle virtual (/dev/uinput) liberado: vibração, Nunchuk e deitado no Dolphin",
        "sem acesso ao /dev/uinput — sem vibração, Nunchuk e deitado no Dolphin "
        f"({PREPARE})")
    jc = subprocess.run(["systemctl", "is-active", "joycond"], capture_output=True, text=True,
                        check=False).stdout.strip()
    _ok(jc != "active", "joycond desligado (o EasyJoycon faz o papel dele)",
        f"joycond ligado — ele esconde os botões; {PREPARE}")

    print("Controles")
    found = controllers.scan()
    if not found:
        print("  - nenhum controle da Nintendo conectado agora")
    for mac, info in found.items():
        name = controllers.KIND_NAMES[info["kind"]]
        readable = info["main"] and os.access(info["main"], os.R_OK)
        _ok(info["main"] and info["imu"] and readable, f"{name} {mac}: botões e movimento ok",
            f"{name} {mac}: " + ("sem permissão nos botões" if info["main"] else "incompleto"))

    paths = {colors.hidraw_path(info["hid"]): mac for mac, info in found.items()}
    paths.pop(None, None)
    using = hidusers.users(list(paths))
    for path, mac in paths.items():
        names = " e ".join(sorted(using.get(path, ())))
        who = f"{controllers.KIND_NAMES[found[mac]['kind']]} {mac}"
        _ok(not names, f"{who}: nenhum outro programa usando o controle",
            f"{who}: {names} está usando o controle — feche (estraga a calibração e a "
            "reconexão)")
    if not found and _running("steam"):
        print("  ! a Steam está aberta: ao conectar, ela pega o Joy-Con e estraga a calibração")

    print("Dolphin")
    if not _ok(dolphin.installed(), "Dolphin instalado",
               "Dolphin não instalado — flatpak install flathub org.DolphinEmu.dolphin-emu"):
        return 0
    d = dolphin.config_dir()
    ini = dolphin.read_ini(d / "DSUClient.ini")
    entries = dolphin.ini_get(ini.get("Server", []), "Entries") or ""
    _ok(dolphin.DSU_NAME + ":" in entries, f"DSU configurado ({d})",
        "Dolphin ainda não configurado — clique em \"Abrir Dolphin\" na janela")
    return 0


def cmd_measure(args):
    """Mede os engasgos de cada controle e guarda em data/medicoes.txt pra
    comparar adaptadores."""
    from app import adapter

    seconds = float(args[0]) if args and args[0].replace(".", "").isdigit() else 30.0
    a = adapter.in_use(adapter.adapters())
    nome = f"{a['chip']} {a['vid']}:{a['pid']}" if a else "sem adaptador"
    print(f"Medindo {seconds:.0f} s com {nome}. Deixe os controles ligados (parados ou não).")
    shown = [-1]

    def progress(f):
        pct = int(f * 100) // 10 * 10
        if pct != shown[0]:
            shown[0] = pct
            print(f"\r  {pct:3d}%", end="", flush=True)

    result = adapter.measure(seconds, progress)
    print()
    if not result:
        print("Nenhum controle conectado pra medir.")
        return 1
    path = config.DATA_DIR / "medicoes.txt"
    adapter.save_measure(path, nome, result)
    print(path.read_text(encoding="utf-8").rstrip().split("\n\n")[-1])
    print(f"\n(guardado em {path}; normal: ~66 pacotes/s, mediana ~15 ms, 0–5 engasgos/min)")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "janela"
    if cmd in ("janela", "--segundo-plano"):
        if _gui_available():
            return cmd_gui(hidden=cmd == "--segundo-plano")
        print("Sem interface gráfica disponível (falta o python3-gi?) — abrindo no terminal.")
        return cmd_server()
    if cmd in ("--terminal", "terminal", "servidor"):
        return cmd_server()
    if cmd == "configurar-dolphin":
        return cmd_configure(argv[2:])
    if cmd == "diagnostico":
        return cmd_diagnose()
    if cmd == "medir":
        return cmd_measure(argv[2:])
    if cmd == "testar":
        from app import probe
        cfg = config.load()
        try:
            probe.run([cfg["dsu"]["porta"], cfg["dsu"]["porta_nunchuk"]])
        except KeyboardInterrupt:
            pass
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
