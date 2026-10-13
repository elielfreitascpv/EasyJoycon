"""Configura o Dolphin sozinho: liga o cliente DSU apontando pro EasyJoycon e
mapeia os Wii Remotes (WiimoteNew.ini), os controles de GameCube
(GCPadNew.ini) + perfis pra trocar pela tela.

Nomes conferidos no código do Dolphin:
- DSUClient.ini [Server] Enabled / Entries ("descrição:ip:porta;…")
- WiimoteNew.ini [WiimoteN]: "Grupo/Controle = expressão"; Source = 1
  (emulado); extensão em "Extension = Nunchuk" e grupos dela com prefixo
  "Nunchuk/".
- Pad DSU = "DSUClient/<slot>/<descrição>" (slot = número do jogador − 1).
- GCPadNew.ini [GCPadN]: Buttons/A…Start, Main Stick/, C-Stick/,
  Triggers/L, Triggers/L-Analog… (GCPad.cpp); porta ligada em Dolphin.ini
  [Core] SIDeviceN = 6 (SerialInterfaceDevice::GCController), 0 = nada.
"""
import json
import os
import shutil
import time
from pathlib import Path

from .config import DATA_DIR
from .motion import POINTER_TOTAL_YAW

DSU_NAME = "EasyJoycon"
RUMBLE_DEVICE = "evdev/0/EasyJoycon J{n} Vibracao"   # vibration.NAME, visto pelo Dolphin
DSU_NUNCHUK_NAME = "EasyJoycon Nunchuk"
FLATPAK_ID = "org.DolphinEmu.dolphin-emu"


def config_dir():
    """Pasta Config do Dolphin (Flatpak primeiro, depois instalação comum)."""
    env = os.environ.get("EASYJOYCON_DOLPHIN_CONFIG")
    if env:
        return Path(env)
    home = Path.home()
    if _flatpak_installed() or (home / ".var/app" / FLATPAK_ID).exists():
        return home / ".var/app" / FLATPAK_ID / "config/dolphin-emu"
    return home / ".config/dolphin-emu"


def _flatpak_installed():
    home = Path.home()
    return any((base / "app" / FLATPAK_ID).exists()
               for base in (home / ".local/share/flatpak", Path("/var/lib/flatpak")))


def launcher():
    """Comando que abre o Dolphin, ou None se ele não está instalado
    (Flatpak, o jeito oficial no Linux, ou pacote da distribuição)."""
    if _flatpak_installed() and shutil.which("flatpak"):
        return ["flatpak", "run", FLATPAK_ID]
    exe = shutil.which("dolphin-emu")
    return [exe] if exe else None


def installed():
    return os.environ.get("EASYJOYCON_DOLPHIN_CONFIG") is not None or launcher() is not None


INSTALL_HELP = (
    "Não achei o Dolphin instalado neste computador.\n\n"
    "Instale pela loja de aplicativos do sistema (procure \"Dolphin Emulator\") "
    "ou, no terminal:\n\n"
    "    flatpak install flathub org.DolphinEmu.dolphin-emu\n\n"
    "Depois é só clicar de novo em \"Abrir Dolphin\": o EasyJoycon configura os "
    "controles sozinho.")
DOWNLOAD_URL = "https://dolphin-emu.org/download/"


def dolphin_running():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm.startswith("dolphin-emu"):
            return True
    return False


# --- INI simples (o Dolphin usa "Chave = Valor" e [Seção]) ---------------
def read_ini(path):
    sections = {}
    current = sections.setdefault("", [])
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return sections
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = sections.setdefault(s[1:-1], [])
        else:
            current.append(line)
    return sections


def ini_get(lines, key):
    for line in lines:
        k, sep, v = line.partition("=")
        if sep and k.strip() == key:
            return v.strip()
    return None


def ini_set(lines, key, value):
    for i, line in enumerate(lines):
        k, sep, _ = line.partition("=")
        if sep and k.strip() == key:
            lines[i] = f"{key} = {value}"
            return
    lines.append(f"{key} = {value}")


def write_ini(path, sections):
    out = []
    for name, lines in sections.items():
        body = [ln for ln in lines if ln.strip()]
        if name:
            out.append(f"[{name}]")
        elif not body:
            continue
        out.extend(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- mapeamentos -----------------------------------------------------------
def _q(control, device=None):
    return f"`{device}:{control}`" if device else f"`{control}`"


def _any(*controls):
    return " | ".join(_q(c) for c in controls)


def _imu(prefix, device=None):
    keys = {}
    for d in ("Up", "Down", "Left", "Right", "Forward", "Backward"):
        keys[f"{prefix}IMUAccelerometer/{d}"] = _q(f"Accel {d}", device)
    return keys


def _gyro():
    m = {f"IMUGyroscope/{d}": _q(f"Gyro {d}") for d in
         ("Pitch Up", "Pitch Down", "Roll Left", "Roll Right", "Yaw Left", "Yaw Right")}
    # O EasyJoycon já calibra e já tem zona morta (motion.py). Os do Dolphin
    # (IMUGyroscope.cpp: 2 °/s e recalibrar após 3 s quase parado) jogavam fora
    # a mira lenta e podiam tomar giro devagar por desvio.
    m["IMUGyroscope/Dead Zone"] = "0"
    m["IMUGyroscope/Calibration Period"] = "0"
    # Onde o ponteiro prende pros lados (Dynamics.cpp: ± metade disso). O
    # padrão, 25°, prendia cedo e a referência deslocava; não muda a
    # sensibilidade (essa é o FOV), só deixa o ponto sair da tela e voltar.
    m["IMUIR/Total Yaw"] = f"{POINTER_TOTAL_YAW:g}"
    return m


def _rumble(slot):
    return {"Rumble/Motor": _q("Strong", RUMBLE_DEVICE.format(n=slot + 1))}


def mapping_pair(slot):
    """Joy-Con direito em pé = Wii Remote; esquerdo = Nunchuk."""
    nunchuk_dev = f"DSUClient/{slot}/{DSU_NUNCHUK_NAME}"
    m = {
        "Device": f"DSUClient/{slot}/{DSU_NAME}",
        "Buttons/A": _q("Circle"),                 # A
        "Buttons/B": _any("R2", "Cross"),          # ZR (gatilho) ou B
        "Buttons/1": _q("Triangle"),               # X
        "Buttons/2": _q("Square"),                 # Y
        "Buttons/-": _q("Share"),                  # −
        "Buttons/+": _q("Options"),                # +
        "Buttons/Home": _q("PS"),                  # HOME
        "D-Pad/Up": _any("Pad N", "Right Y+"),     # setas do esquerdo ou analógico direito
        "D-Pad/Down": _any("Pad S", "Right Y-"),
        "D-Pad/Left": _any("Pad W", "Right X-"),
        "D-Pad/Right": _any("Pad E", "Right X+"),
        "IMUIR/Enabled": "True",
        "IMUIR/Recenter": _q("R3"),                # apertar o analógico direito
        "Extension": "Nunchuk",
        "Nunchuk/Buttons/C": _q("L1"),             # L
        "Nunchuk/Buttons/Z": _q("L2"),             # ZL
        "Nunchuk/Stick/Up": _q("Left Y+"),
        "Nunchuk/Stick/Down": _q("Left Y-"),
        "Nunchuk/Stick/Left": _q("Left X-"),
        "Nunchuk/Stick/Right": _q("Left X+"),
    }
    m.update(_imu(""))
    m.update(_gyro())
    m.update(_imu("Nunchuk/", nunchuk_dev))
    m.update(_rumble(slot))
    return m


def mapping_universal(slot):
    """Um mapeamento só pra todos os jeitos de jogar (2026-10-11): o
    EasyJoycon traduz os botões de cada modo pros mesmos campos do DSU (os
    do par) e avisa pelo dispositivo virtual do jogador se tem Nunchuk e se
    está deitado — o Dolphin lê isso a cada atualização, então separar,
    juntar, deitar ou tirar o Nunchuk vale com o jogo rodando."""
    signals = RUMBLE_DEVICE.format(n=slot + 1)
    m = mapping_pair(slot)
    m["Extension"] = _q("Axis 0+", signals)                 # 1 = Nunchuk, 0 = nenhum
    m["Options/Sideways Wiimote"] = _q("Axis 1+", signals)  # 1 = deitado
    return m


def mapping_gc(slot):
    """Controle de GameCube, como o GameCube do Switch Online: analógicos =
    analógico e C-Stick, ZL/ZR = gatilhos L/R, R = Z, + = Start. Só vale com
    controle completo (par ou Pro): cada entrada passa pelo sinal "Axis 2+"
    do dispositivo virtual do jogador — com um Joy-Con sozinho fica mudo, e
    trocar o jeito de jogar não pede reabrir o Dolphin."""
    gate = _q("Axis 2+", RUMBLE_DEVICE.format(n=slot + 1))

    def on(*controls):
        expr = _any(*controls)
        return f"({expr}) & {gate}" if len(controls) > 1 else f"{expr} & {gate}"

    m = {"Device": f"DSUClient/{slot}/{DSU_NAME}",
         "Buttons/A": on("Circle"), "Buttons/B": on("Cross"),
         "Buttons/X": on("Triangle"), "Buttons/Y": on("Square"),
         "Buttons/Z": on("R1"), "Buttons/Start": on("Options"),
         "Triggers/L": on("L2"), "Triggers/R": on("R2"),
         "Triggers/L-Analog": on("L2"), "Triggers/R-Analog": on("R2"),
         "D-Pad/Up": on("Pad N"), "D-Pad/Down": on("Pad S"),
         "D-Pad/Left": on("Pad W"), "D-Pad/Right": on("Pad E")}
    for group, side in (("Main Stick", "Left"), ("C-Stick", "Right")):
        for d, axis in (("Up", "Y+"), ("Down", "Y-"), ("Left", "X-"), ("Right", "X+")):
            m[f"{group}/{d}"] = on(f"{side} {axis}")
    m.update(_rumble(slot))
    return m


SI_GC_CONTROLLER = 6


# Modos que o app conhece (todos usam o mapeamento universal no Dolphin).
MAPPINGS = {"par": mapping_universal, "sozinho": mapping_universal,
            "empe": mapping_universal, "pro": mapping_universal}
PROFILES = {"Qualquer jeito": mapping_universal}
APPLIED_FILE = DATA_DIR / "dolphin-aplicado.json"
# Subir quando mudar o mapeamento: o Dolphin é reconfigurado sozinho de novo.
# 2 = vibração dos jogos (2026-10-07)
# 3 = giroscópio sem zona morta/calibração do Dolphin, ponteiro até 50° (2026-10-10)
# 6 = mapeamento universal: mudar de modo não precisa reabrir o Dolphin
# 7 = SDL do Dolphin sem pegar os Joy-Cons direto (SDL_HINTS)
# 8 = controle de GameCube (GCPadNew.ini), só com par ou Pro
MAPPING_VERSION = 8
# O SDL do Dolphin abre o hidraw do Joy-Con/Pro e manda comandos próprios
# (2026-10-12: com 2 jogadores, o movimento dos dois parou no menu do Wii).
# Dolphin.ini [SDL_Hints] vira SDL_SetHint ao iniciar; desligado, o SDL lê
# pelo evdev, só olhando.
SDL_HINTS = {"SDL_JOYSTICK_HIDAPI_JOY_CONS": "0", "SDL_JOYSTICK_HIDAPI_SWITCH": "0"}
MAX_BACKUPS = 10


def describe(layout):
    """{1: "par", 3: "sozinho"} → "J1 Wii Remote + Nunchuk, J3 deitado"."""
    names = {"par": "Wii Remote + Nunchuk", "sozinho": "deitado", "empe": "em pé",
             "pro": "Pro Controller"}
    return ", ".join(f"J{n} {names[m]}" for n, m in sorted(layout.items())) or "nenhum"


def layout_from_memory():
    """Jogadores lembrados (data/jogadores.json) → {número: modo}."""
    try:
        mem = json.loads((DATA_DIR / "jogadores.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for v in mem.values():
        if isinstance(v, dict) and v.get("modo") in MAPPINGS and v.get("jogador") in (1, 2, 3, 4):
            out[v["jogador"]] = v["modo"]
    return out


def applied_layout():
    """Último layout aplicado — None se nunca aplicou ou se o mapeamento mudou."""
    try:
        data = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        if data.get("versao") != MAPPING_VERSION:
            return None
        return {int(k): v for k, v in data["layout"].items() if v in MAPPINGS}
    except (OSError, ValueError, AttributeError, KeyError):
        return None


def _backup(files):
    root = DATA_DIR / "backups"
    backup = root / time.strftime("dolphin-%Y%m%d-%H%M%S")
    for f in files:
        if f.exists():
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, backup / f.name)
    old = sorted(p for p in root.glob("dolphin-*") if p.is_dir()) if root.exists() else []
    for p in old[:-MAX_BACKUPS]:
        shutil.rmtree(p, ignore_errors=True)
    return backup if backup.exists() else None


def slots_for(layout, wiimotes):
    """Wii Remotes ligados no Dolphin: os primeiros `wiimotes` e os de
    qualquer jogador além disso. Com o mapeamento universal, só isso exige
    reabrir o Dolphin — o jeito de jogar muda com ele aberto."""
    return sorted(set(range(1, wiimotes + 1)) | set(layout))


def applied_slots():
    """Wii Remotes ligados no último configurar (None = precisa configurar)."""
    try:
        data = json.loads(APPLIED_FILE.read_text(encoding="utf-8"))
        if data.get("versao") != MAPPING_VERSION:
            return None
        return sorted(int(n) for n in data["slots"])
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None


def configure(layout, port, port_nunchuk, wiimotes=4):
    """Liga os Wii Remotes e as portas de GameCube de slots_for(layout,
    wiimotes) com o mapeamento universal (Wii Remote N = GameCube N =
    jogador N). Devolve (pasta do Dolphin, backup)."""
    if not installed():
        raise RuntimeError("o Dolphin não está instalado")
    if dolphin_running():
        raise RuntimeError("feche o Dolphin antes (ele regrava a configuração ao fechar)")
    slots = slots_for(layout, wiimotes)
    cfg = config_dir()
    backup = _backup([cfg / "DSUClient.ini", cfg / "WiimoteNew.ini", cfg / "GCPadNew.ini",
                      cfg / "Dolphin.ini"])

    main = read_ini(cfg / "Dolphin.ini")
    hints = main.setdefault("SDL_Hints", [])
    for k, v in SDL_HINTS.items():
        ini_set(hints, k, v)
    core = main.setdefault("Core", [])
    for n in range(1, 5):   # portas de GameCube = os mesmos jogadores dos Wii Remotes
        ini_set(core, f"SIDevice{n - 1}", str(SI_GC_CONTROLLER if n in slots else 0))
    write_ini(cfg / "Dolphin.ini", main)

    gc = read_ini(cfg / "GCPadNew.ini")
    for n in range(1, 5):
        gc[f"GCPad{n}"] = [f"{k} = {v}" for k, v in mapping_gc(n - 1).items()]
    write_ini(cfg / "GCPadNew.ini", gc)

    # Cliente DSU: mantém servidores de outros programas, troca os nossos
    dsu = read_ini(cfg / "DSUClient.ini")
    server = dsu.setdefault("Server", [])
    entries = [e for e in (ini_get(server, "Entries") or "").strip('"').split(";")
               if e and not e.startswith((DSU_NAME + ":", DSU_NUNCHUK_NAME + ":"))]
    entries += [f"{DSU_NAME}:127.0.0.1:{port}", f"{DSU_NUNCHUK_NAME}:127.0.0.1:{port_nunchuk}"]
    ini_set(server, "Enabled", "True")
    ini_set(server, "Entries", ";".join(entries) + ";")
    write_ini(cfg / "DSUClient.ini", dsu)

    wii = read_ini(cfg / "WiimoteNew.ini")
    for n in range(1, 5):
        lines = [f"{k} = {v}" for k, v in mapping_universal(n - 1).items()]
        lines.append(f"Source = {1 if n in slots else 0}")
        wii[f"Wiimote{n}"] = lines
    write_ini(cfg / "WiimoteNew.ini", wii)

    # Perfis pra trocar na tela do Dolphin (Controles → Configurar → Perfil)
    pdir = cfg / "Profiles" / "Wiimote"
    for old in pdir.glob("EasyJoycon J? - *.ini") if pdir.is_dir() else ():
        old.unlink()                       # perfis por modo de antes (todos nossos)
    for n in range(1, 5):
        for label, fn in PROFILES.items():
            lines = [f"{k} = {v}" for k, v in fn(n - 1).items()]
            write_ini(pdir / f"EasyJoycon J{n} - {label}.ini", {"Profile": lines})
        lines = [f"{k} = {v}" for k, v in mapping_gc(n - 1).items()]
        write_ini(cfg / "Profiles" / "GCPad" / f"EasyJoycon J{n}.ini", {"Profile": lines})

    APPLIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPLIED_FILE.write_text(json.dumps({"versao": MAPPING_VERSION, "slots": slots, "layout": {
        str(k): v for k, v in sorted(layout.items())}}), encoding="utf-8")
    return cfg, backup
