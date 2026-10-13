"""Configurações do usuário (data/config.toml).

O arquivo é criado na primeira vez a partir de MODELO, com explicação de cada
opção. Chave que falta ou valor inválido → usa o padrão e avisa no log.
"""
import logging
import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:   # Python 3.10 (Ubuntu 22.04, Mint 21)
    import tomli as tomllib

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("EASYJOYCON_DATA_DIR")
                or Path(__file__).resolve().parent.parent / "data")
CONFIG_FILE = DATA_DIR / "config.toml"

MODELO = """\
# Configurações do EasyJoycon. Depois de mudar, feche e abra o servidor de
# novo (a suavização dá pra mudar com o servidor aberto, teclas [ e ]).

[movimento]
# Suavização do giroscópio, de 0 (nenhuma, mais rápido) a 10 (bem suave,
# um pouco mais "atrasado"). Tira a tremedeira da mira sem atrasar movimento
# rápido (o filtro solta quando você mexe rápido).
suavizacao = 3

# Ignora rotação mais lenta que isso (graus por segundo). Evita a mira
# "escorregar" com o controle parado. 0 desliga.
zona_morta = 0.3

# Calibra o desvio do giroscópio sozinho sempre que o controle fica parado
# uns 2 segundos (em cima da mesa, por exemplo).
calibrar_parado = true

# Quando o adaptador Bluetooth engasga e o controle para de mandar dados,
# o servidor continua mandando o último movimento por até tantos
# milissegundos (disfarça o tranco) e depois para (evita a mira sair
# girando sozinha se o controle desconectar).
segurar_ms = 120

# Pegada livre (como no Switch): ao apertar o analógico pra recentralizar,
# o jeito que você está segurando vira "apontando pra TV". Dá pra segurar o
# Joy-Con em pé, deitado ou torto. false = só funciona apontando a ponta
# (os gatilhos) pra tela, como um Wii Remote de verdade.
pegada_livre = true

# Ler o movimento direto do Joy-Con (como o Switch e a Steam), com a
# calibração lida por nós: acaba o "movimento zerado" quando o driver lê a
# calibração errada e sabe exatamente quantos pacotes se perderam.
# false = movimento pelo driver do Linux, como antes.
direto = true

[vibracao]
# Força da vibração dos jogos, de 0 (desligada) a 100 (máximo do Joy-Con).
forca = 40

# Vibração grave (~150 Hz, como o SDL faz e parecida com o motor do Wii
# Remote), mandada direto pro controle. false = a do driver do Linux
# (160/320 Hz, mais "zumbido").
grave = true

# Com a vibração grave: imita o motorzinho do Wii Remote (embala e para
# devagar, com o zumbido do motor junto). false = a grave de antes, que liga
# e desliga seco num tom só.
motor = true

[bluetooth]
# Por quantos segundos procurar controles novos depois de apertar P.
# 0 = sem limite: procura até alguém apertar A (como no Switch) ou clicar
# em "Parar de procurar". Procurar atrapalha os controles já conectados
# (o adaptador divide o rádio), por isso não fica ligado o tempo todo.
procurar_segundos = 0

# Parar de procurar sozinho depois de tantos segundos sem achar controle
# novo (cada controle que aparece conta de novo). 0 = só para com A.
parar_sem_achar_segundos = 60

# Como no Switch quando ele dorme: com o Dolphin fechado, desconecta o
# controle que ninguém mexeu por tantos minutos (poupa a bateria). Ele volta
# apertando qualquer botão. 0 = nunca.
desligar_parado_minutos = 10

# Procurar sozinho ao abrir o servidor se nenhum controle estiver conectado.
procurar_ao_abrir = true

[jogadores]
# Com só um Joy-Con esquerdo e um direito esperando e nenhum jogador ainda,
# junta os dois sozinho depois de tantos segundos (0 = só com L+R).
juntar_sozinho_segundos = 3

# Lembrar qual controle era de qual jogador mesmo depois de fechar o
# servidor (reconecta direto no mesmo lugar).
lembrar = true

[dsu]
# Portas UDP do servidor DSU (o Dolphin conecta nelas). Só mude se outra
# coisa já usar essas portas — e rode "configurar-dolphin" de novo.
porta = 26760
porta_nunchuk = 26761

[dolphin]
# Com o Dolphin fechado, reconfigurar os Wii Remotes sozinho sempre que os
# jogadores mudarem (quantos são e se estão em par, deitados ou Pro).
configurar_sozinho = true

# Quantos Wii Remotes deixar ligados no Dolphin (1 a 4). Os jogadores entram,
# saem e mudam de jeito (par, em pé, deitado, Nunchuk) com o Dolphin aberto;
# Wii Remote sem controle fica com o ponteiro escondido. Só jogador além
# desse número pede fechar e abrir o Dolphin.
wiimotes = 4
"""

PADRAO = {
    "movimento": {"suavizacao": 3, "zona_morta": 0.3, "calibrar_parado": True, "segurar_ms": 120,
                  "pegada_livre": True, "direto": True},
    "vibracao": {"forca": 40, "grave": True, "motor": True},
    "bluetooth": {"procurar_segundos": 0, "parar_sem_achar_segundos": 60,
                  "desligar_parado_minutos": 10,
                  "procurar_ao_abrir": True},
    "jogadores": {"juntar_sozinho_segundos": 3, "lembrar": True},
    "dsu": {"porta": 26760, "porta_nunchuk": 26761},
    "dolphin": {"configurar_sozinho": True, "wiimotes": 4},
}

_LIMITES = {
    ("movimento", "suavizacao"): (0, 10),
    ("movimento", "zona_morta"): (0, 20),
    ("movimento", "segurar_ms"): (0, 1000),
    ("vibracao", "forca"): (0, 100),
    ("bluetooth", "procurar_segundos"): (0, 3600),
    ("bluetooth", "parar_sem_achar_segundos"): (0, 3600),
    ("bluetooth", "desligar_parado_minutos"): (0, 240),
    ("jogadores", "juntar_sozinho_segundos"): (0, 60),
    ("dsu", "porta"): (1024, 65535),
    ("dsu", "porta_nunchuk"): (1024, 65535),
    ("dolphin", "wiimotes"): (1, 4),
}


def _limpar(lido):
    cfg = {sec: dict(vals) for sec, vals in PADRAO.items()}
    for sec, vals in PADRAO.items():
        for chave, padrao in vals.items():
            if chave not in lido.get(sec, {}):
                continue
            valor = lido[sec][chave]
            tipo_ok = isinstance(valor, bool) if isinstance(padrao, bool) else (
                isinstance(valor, int | float) and not isinstance(valor, bool))
            lim = _LIMITES.get((sec, chave))
            if not tipo_ok or (lim and not lim[0] <= valor <= lim[1]):
                log.warning("config: [%s] %s = %r inválido, usando %r", sec, chave, valor, padrao)
                continue
            cfg[sec][chave] = type(padrao)(valor) if not isinstance(padrao, bool) else valor
    return cfg


def load():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(MODELO, encoding="utf-8")
    try:
        lido = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.error("config.toml com erro (%s); usando os padrões", e)
        lido = {}
    return _limpar(lido)


def save_value(secao, chave, valor):
    """Troca um valor no config.toml mantendo os comentários. Se a chave (ou a
    seção) não existe no arquivo — config criado por uma versão antiga —,
    acrescenta (antes a mudança se perdia calada)."""
    linhas = CONFIG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    if linhas and not linhas[-1].endswith("\n"):
        linhas[-1] += "\n"
    atual, fim_secao = None, None
    for i, linha in enumerate(linhas):
        s = linha.strip()
        if s.startswith("[") and s.endswith("]"):
            atual = s[1:-1].strip()
            if atual == secao:
                fim_secao = i + 1
        elif atual == secao:
            if s.split("=")[0].strip() == chave:
                linhas[i] = f"{chave} = {valor}\n"
                break
            if s:
                fim_secao = i + 1
    else:
        if fim_secao is None:
            linhas += ["\n", f"[{secao}]\n", f"{chave} = {valor}\n"]
        else:
            linhas.insert(fim_secao, f"{chave} = {valor}\n")
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text("".join(linhas), encoding="utf-8")
    tmp.replace(CONFIG_FILE)
