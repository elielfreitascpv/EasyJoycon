"""Adaptador Bluetooth: quem é (original ou cópia) e como está se saindo
(quedas do USB, engasgos dos controles).
"""
import itertools
import os
import select
import statistics
import subprocess
import time
from pathlib import Path

from . import controllers
from . import evdev as E

SYS_BT = Path("/sys/class/bluetooth")

# vendor USB → (chip, bom pra controle?)
VENDORS = {
    "2357": ("TP-Link (chip Realtek)", True),
    "0bda": ("Realtek", True),
    "8087": ("Intel", True),
    "0a12": ("CSR8510", None),     # original raro; quase sempre cópia
    "33fa": ("Barrot", False),     # muitos UGREEN 5.3/5.4: suporte fraco no Linux
}


def _kernel_log():
    try:
        out = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=10, check=False)
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def adapters():
    """[{hci, vid, pid, product, chip, verdict, usb_port, drops}]"""
    log = _kernel_log()
    out = []
    for hci in sorted(SYS_BT.glob("hci[0-9]*")) if SYS_BT.exists() else []:
        if ":" in hci.name:
            continue
        info = {"hci": hci.name, "vid": "", "pid": "", "product": "", "usb_port": ""}
        try:
            usb = (hci / "device").resolve().parent
            info["vid"] = (usb / "idVendor").read_text().strip()
            info["pid"] = (usb / "idProduct").read_text().strip()
            prod = usb / "product"
            info["product"] = prod.read_text().strip() if prod.exists() else ""
            info["usb_port"] = usb.name
        except OSError:
            pass
        chip, good = VENDORS.get(info["vid"], ("desconhecido", None))
        if info["vid"] == "0a12":
            clone = "CSR clone detected" in log
            verdict = ("cópia de CSR8510 — instável com vários controles" if clone
                       else "CSR8510 (Bluetooth 4.0, fraco pra vários controles)")
            good = False
        elif good:
            verdict = "original, bom pra controles"
            if info["vid"] in ("2357", "0bda") and "RTL: fw version" not in log:
                verdict += " (mas o firmware Realtek não apareceu no log — confira)"
        elif good is False:
            verdict = "chip com suporte fraco no Linux"
        else:
            verdict = "não conheço esse adaptador"
        port = info["usb_port"]
        info.update(chip=chip, good=good, verdict=verdict,
                    drops=log.count(f"usb {port}: USB disconnect") if port else 0,
                    blocked=_blocked(hci),
                    links=len(list(SYS_BT.glob(f"{hci.name}:*"))))   # conexões abertas
        out.append(info)
    return out


def _blocked(hci):
    """Desligado pelo rfkill (ex.: o Bluetooth do notebook com o USB plugado)."""
    for soft in hci.glob("rfkill*/soft"):
        try:
            if soft.read_text().strip() == "1":
                return True
        except OSError:
            pass
    return False


def in_use(found):
    """O adaptador dos controles: o que tem conexões, senão o não bloqueado."""
    return min(found, key=lambda a: (-a["links"], a["blocked"])) if found else None


def measure(seconds=20.0, on_progress=None):
    """Mede a chegada do movimento de cada controle conectado.
    Devolve {nome: {pacotes_s, mediana_ms, p99_ms, pior_ms, engasgos_min}}."""
    devs = {}
    for mac, info in controllers.scan().items():
        if info["imu"]:
            try:
                d = E.InputDevice(info["imu"])
            except OSError:
                continue
            devs[d.fd] = (f"{controllers.KIND_NAMES[info['kind']]} {mac[-5:]}", d, [])
    if not devs:
        return {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select(list(devs), [], [], 0.2)
        now = time.monotonic()
        for fd in ready:
            _, d, times = devs[fd]
            try:
                if d.read_events():
                    times.append(now)
            except OSError:
                pass
        if on_progress:
            on_progress(1 - (end - now) / seconds)
    result = {}
    for name, d, times in devs.values():
        d.close()
        gaps = sorted((b - a) * 1000 for a, b in itertools.pairwise(times))
        if len(gaps) < 10:
            result[name] = None
            continue
        result[name] = {
            "pacotes_s": len(times) / seconds,
            "mediana_ms": statistics.median(gaps),
            "p99_ms": gaps[int(len(gaps) * 0.99)],
            "pior_ms": gaps[-1],
            "engasgos_min": sum(1 for g in gaps if g > 50) * 60 / seconds,
        }
    return result


def save_measure(path, adapter, result):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n{time.strftime('%Y-%m-%d %H:%M')}  {adapter}\n")
        for name, r in result.items():
            if r is None:
                f.write(f"  {name}: sem dados\n")
            else:
                f.write(f"  {name}: {r['pacotes_s']:.0f} pacotes/s, "
                        f"mediana {r['mediana_ms']:.0f} ms, 99% até {r['p99_ms']:.0f} ms, "
                        f"pior {r['pior_ms']:.0f} ms, "
                        f"{r['engasgos_min']:.0f} engasgos/min\n")
    os.chmod(path, 0o600)
