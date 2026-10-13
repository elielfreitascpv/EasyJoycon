"""O sistema está preparado pro EasyJoycon? (scripts/instalar-sistema.sh)

A preparação mexe em arquivos do sistema (Bluetooth, regras do udev), então
precisa da senha de administrador. Pela janela ela roda com o pkexec (a
janelinha de senha do sistema). O script é copiado antes pra uma pasta
temporária: dentro do AppImage os arquivos ficam num lugar que nem o
administrador consegue ler.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "instalar-sistema.sh"
INPUT_CONF = Path("/etc/bluetooth/input.conf")
RULES_FILE = Path("/etc/udev/rules.d/70-easyjoycon-uaccess.rules")
RULES_VERSION = 2   # = VERSAO no instalar-sistema.sh
MARKER = "# easyjoycon-sistema "


def _bonded_only_off(conf=INPUT_CONF):
    try:
        lines = conf.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.replace(" ", "") == "ClassicBondedOnly=false" for line in lines)


def _rules_version(rules=RULES_FILE):
    try:
        first = rules.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return 0
    if not first.startswith(MARKER):
        return 0
    try:
        return int(first[len(MARKER):])
    except ValueError:
        return 0


def _joycond_active():
    try:
        out = subprocess.run(["systemctl", "is-active", "joycond"], capture_output=True,
                             text=True, check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out == "active"


def missing(conf=INPUT_CONF, rules=RULES_FILE, joycond=None):
    """O que falta preparar, em palavras: [] = pronto."""
    out = []
    if not _bonded_only_off(conf):
        out.append("o Bluetooth ainda não aceita Joy-Con")
    if _rules_version(rules) < RULES_VERSION:
        out.append("faltam as permissões dos controles")
    if _joycond_active() if joycond is None else joycond:
        out.append("o joycond está ligado (ele esconde os botões)")
    return out


def can_ask_password():
    return shutil.which("pkexec") is not None


def prepare():
    """Roda a preparação com a janelinha de senha. Bloqueia (rodar fora da
    thread da janela). Devolve (deu certo?, texto do que aconteceu)."""
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        return False, "sem o pkexec — rode no terminal: " + str(SCRIPT)
    with tempfile.TemporaryDirectory(prefix="easyjoycon-") as tmp:
        script = Path(tmp) / "instalar-sistema.sh"
        shutil.copyfile(SCRIPT, script)    # numa pasta comum o administrador lê
        try:
            proc = subprocess.run([pkexec, "/bin/bash", str(script)], capture_output=True,
                                  text=True, check=False, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode in (126, 127) and not proc.stdout:
        return False, "cancelado (a senha não foi digitada)"
    return proc.returncode == 0, out
