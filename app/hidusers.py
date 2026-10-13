"""Quem mais está com o controle aberto pelo hidraw.

A Steam (e outros, como o SDL de jogos) abre o /dev/hidrawN do Joy-Con e
manda comandos próprios: com ela aberta o L conectou com a calibração errada
("inaccurate IMU divisors", "Failed to read left stick cal") e os dois
pararam de reconectar pelo botão (visto em 2026-10-12). Aqui só
olhamos /proc/<pid>/fd de cada processo — o mesmo que
`ls -l /proc/*/fd | grep hidraw` —, sem mexer em nada. Processos de outro
usuário (root) não dá pra ver, e tudo bem: a Steam roda como o usuário.
"""
import os
from pathlib import Path

PROC = Path("/proc")
OUR_RUN = Path(__file__).resolve().parent.parent / "run.py"

# Nome do processo (/proc/<pid>/comm, até 15 letras) → nome que a gente mostra.
FRIENDLY = (("steam", "Steam"), ("dolphin-emu", "Dolphin"))


def friendly(comm):
    for prefix, name in FRIENDLY:
        if comm.startswith(prefix):
            return name
    return comm


def is_us(pid_dir):
    """É o EasyJoycon (outra cópia, ou a janela vista pelo `diagnostico`)?
    Ele mesmo abre o hidraw pra vibração grave e pra ler a cor."""
    try:
        args = (pid_dir / "cmdline").read_bytes().split(b"\0")
        cwd = os.readlink(pid_dir / "cwd")
    except OSError:
        return False
    for arg in args:
        if arg.endswith(b"run.py"):
            path = Path(cwd, os.fsdecode(arg))     # absoluto ignora o cwd
            if os.path.realpath(path) == str(OUR_RUN):
                return True
    return False


def is_easyjoycon(pid_dir):
    """Outra cópia do EasyJoycon (o AppImage e o código baixado, por exemplo):
    um run.py com a pasta app/ do lado."""
    try:
        args = (pid_dir / "cmdline").read_bytes().split(b"\0")
        cwd = os.readlink(pid_dir / "cwd")
    except OSError:
        return False
    return any(arg.endswith(b"run.py") and Path(cwd, os.fsdecode(arg)).with_name("app")
               .joinpath("hidusers.py").exists() for arg in args)


def users(paths, proc=PROC, me=None):
    """{caminho do hidraw: {nomes dos programas}} pros que estão abertos por
    outro processo além deste. Bloqueia (lê o /proc todo; rodar fora do laço
    asyncio)."""
    wanted = set(filter(None, paths))
    me = os.getpid() if me is None else me
    found = {}
    if not wanted:
        return found
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit() and int(p) != me]
    except OSError:
        return found
    for pid in pids:
        fd_dir = proc / pid / "fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:            # de outro usuário, ou já saiu
            continue
        hit = set()
        for fd in fds:
            try:
                target = os.readlink(fd_dir / fd)
            except OSError:
                continue
            if target in wanted:
                hit.add(target)
        if not hit or is_us(proc / pid):
            continue
        try:
            comm = (proc / pid / "comm").read_text().strip()
        except OSError:
            comm = f"processo {pid}"
        if is_easyjoycon(proc / pid):
            comm = "outro EasyJoycon aberto"
        for target in hit:
            found.setdefault(target, set()).add(friendly(comm))
    return found
