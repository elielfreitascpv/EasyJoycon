"""Tradução da janela e dos avisos: o texto fica em português no código e o
inglês está em EN, abaixo.

Idioma: o do sistema, como o gettext (LANGUAGE, depois LC_ALL, LC_MESSAGES e
LANG); começou com "pt" = português, qualquer outro = inglês.
EASYJOYCON_IDIOMA=pt|en força um (pra testar). O terminal (--terminal,
diagnostico, medir) e o log continuam em português.

Uso: _("Jogador {n}", n=2). Texto novo na janela → somar aqui; o teste
`traducao_completa` acusa o que faltar.
"""
import os


def detect(env=None):
    env = os.environ if env is None else env
    forced = env.get("EASYJOYCON_IDIOMA", "").strip().lower()
    if forced:
        return "pt" if forced.startswith("pt") else "en"
    # com o locale "C" o gettext ignora o LANGUAGE
    locale = next((env[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG") if env.get(v)), "")
    if locale and locale.split(".")[0] not in ("C", "POSIX"):
        first = env.get("LANGUAGE", "").split(":")[0] or locale
    else:
        first = locale
    return "pt" if first.lower().startswith("pt") else "en"


_current = {"lang": detect()}


def set_language(lang):
    _current["lang"] = lang


def language():
    return _current["lang"]


def _(text, **values):
    if _current["lang"] == "en":
        text = EN.get(text, text)
    return text.format(**values) if values else text


EN = {
    # --- janela: botões, réguas e dicas ---
    "Abrir com o computador": "Start with the computer",
    "Liga o EasyJoycon escondido perto do relógio quando você entra no computador":
        "Starts EasyJoycon hidden next to the clock when you log in",
    "Parear de novo": "Pair again",
    "Preparar o sistema": "Set up the system",
    "Ajusta o Bluetooth e as permissões dos controles (pede a senha de administrador uma "
    "vez)": "Sets up Bluetooth and the controller permissions (asks for the admin password "
            "once)",
    "Identificar": "Identify",
    "Vibra e acende os LEDs do controle desse jogador":
        "Rumbles and lights up the LEDs of this player's controller",
    "Em pé": "Upright",
    "Juntar": "Join",
    "Separar": "Split",
    "Cada Joy-Con vira um Wii Remote em pé": "Each Joy-Con becomes an upright Wii Remote",
    "Junta com o outro Joy-Con sozinho num Wii Remote + Nunchuk":
        "Joins with the other single Joy-Con into a Wii Remote + Nunchuk",
    "Tirar Nunchuk": "Unplug Nunchuk",
    "Pôr Nunchuk": "Plug in Nunchuk",
    "Tira ou põe o Nunchuk com o jogo rodando (quando o jogo pedir). Vale quando você voltar "
    "pro Dolphin — ele só lê os controles com a janela dele em foco. No controle, na hora: "
    "botão de captura do Joy-Con esquerdo":
        "Unplugs or plugs in the Nunchuk while the game is running (when the game asks). "
        "Takes effect when you go back to Dolphin — it only reads controllers while its "
        "window has focus. On the controller, instantly: the left Joy-Con's capture button",
    "Usar em pé": "Use upright",
    "Usar deitado": "Use sideways",
    "Procurar controles": "Search for controllers",
    "Parar de procurar": "Stop searching",
    "Calibrar": "Calibrate",
    "Deixe os controles parados em cima da mesa por 2 segundos":
        "Leave the controllers still on the table for 2 seconds",
    "Recomeçar jogadores": "Reset players",
    "Todo mundo volta a esperar o L+R / SL+SR": "Everyone goes back to waiting for L+R / SL+SR",
    "Esquecer controles": "Forget controllers",
    "Tira todos os Joy-Cons/Pro Controller do Bluetooth, como \"remover dispositivo\", pra "
    "parear do zero": "Removes all Joy-Cons/Pro Controllers from Bluetooth, like \"remove "
                      "device\", to pair from scratch",
    "Suavização": "Smoothing",
    "0 = nenhuma (mais rápido) · 10 = bem suave": "0 = none (fastest) · 10 = very smooth",
    "Vibração": "Rumble",
    "Força da vibração dos jogos · 0 = desligada": "Strength of game rumble · 0 = off",
    "Abrir Dolphin": "Open Dolphin",
    "Configurar Dolphin": "Set up Dolphin",
    "Mapeia os Wii Remotes conforme os jogadores de agora (com o Dolphin fechado)":
        "Maps the Wii Remotes to the current players (with Dolphin closed)",
    "Como usar": "How to use",
    "Abre o passo a passo no site do EasyJoycon: conectar, virar jogador, mirar e o que cada "
    "botão faz": "Opens the step-by-step guide on the EasyJoycon website: connecting, "
                 "becoming a player, aiming and what each button does",
    "Pra mirar: aponte pro meio da tela e aperte o analógico (no par, o do Joy-Con "
    "direito)": "To aim: point at the middle of the screen and press the stick (with a pair, "
                "the right Joy-Con's)",
    "Mostrar a janela": "Show window",
    "Sair": "Quit",

    # --- janela: cartões dos jogadores e faixa de baixo ---
    "Jogador {n}": "Player {n}",
    "Wii Remote + Nunchuk": "Wii Remote + Nunchuk",
    "deitado": "sideways",
    "Wii Remote em pé": "upright Wii Remote",
    "Pro Controller": "Pro Controller",
    "Wii Remote (Nunchuk tirado)": "Wii Remote (Nunchuk unplugged)",
    "movimento ok": "motion ok",
    "zerado, reconectando": "zeroed, reconnecting",
    "calibrando…": "calibrating…",
    "sem dados": "no data",
    "pouse na mesa 2 s": "set it down for 2 s",
    "instável ({n}/min)": "unstable ({n}/min)",
    "({role}) caiu — aperte um botão nele": "({role}) dropped — press a button on it",
    "Procurando controles — segure o botãozinho de sync na lateral do Joy-Con. Terminou? "
    "Aperte A.": "Searching for controllers — hold the small sync button on the side of the "
                 "Joy-Con. Done? Press A.",
    "Controle já pareado: aperte um botão nele.  Controle novo: \"Procurar controles\" e "
    "segure o sync.": "Already paired controller: press a button on it.  New controller: "
                      "\"Search for controllers\" and hold the sync button.",
    "L + R juntos (um Joy-Con em cada mão) = Wii Remote + Nunchuk   ·   SL + SR = deitado   "
    "·   R + ZR (ou L + ZL) = em pé": "L + R together (one Joy-Con in each hand) = Wii Remote "
                                     "+ Nunchuk   ·   SL + SR = sideways   ·   R + ZR (or L "
                                     "+ ZL) = upright",

    # --- janela: topo, faixas e avisos ---
    " · procurando": " · searching",
    "sem adaptador": "no adapter",
    "● Dolphin recebendo": "● Dolphin receiving",
    "○ Dolphin ainda não conectou": "○ Dolphin not connected yet",
    " · Dolphin recebendo": " · Dolphin receiving",
    "EasyJoycon — {n} jogador(es)": "EasyJoycon — {n} player(s)",
    " e ": " and ",
    "O sistema ainda não está preparado pro EasyJoycon ({what}). Clique em \"Preparar o "
    "sistema\": vai pedir a senha de administrador uma vez só.":
        "The system isn't set up for EasyJoycon yet ({what}). Click \"Set up the system\": "
        "it asks for the admin password just once.",
    "Sem permissão pra ler os botões de {n} controle(s): desligue e ligue o controle; se "
    "continuar, saia e entre de novo no computador.":
        "No permission to read the buttons of {n} controller(s): turn the controller off and "
        "on; if it keeps happening, log out and back in.",
    "{names}: pareado(s) sem guardar a chave — não reconectam sozinhos. Clique em \"Parear de "
    "novo\" e segure o botão de sync de cada um.":
        "{names}: paired without saving the key — they won't reconnect on their own. Click "
        "\"Pair again\" and hold the sync button on each one.",
    "{who} está usando o controle e manda comandos próprios pra ele: a calibração e a "
    "reconexão pelo botão podem falhar. Feche e, se o movimento ou o analógico ficarem "
    "estranhos, desligue e ligue o controle.":
        "{who} is using the controller and sending it its own commands: calibration and "
        "reconnecting with a button press may fail. Close it and, if motion or the stick act "
        "strange, turn the controller off and on.",
    "Sem o controle virtual (/dev/uinput): no Dolphin não funcionam a vibração, o Nunchuk nem "
    "o modo deitado. Veja o que falta com o diagnóstico (comando diagnostico).":
        "No virtual controller (/dev/uinput): rumble, the Nunchuk and sideways mode won't "
        "work in Dolphin. See what's missing with the \"diagnostico\" command.",
    "O EasyJoycon tem um jeito novo de configurar o Dolphin: feche e abra o Dolphin uma vez "
    "(depois, mudar os jogadores vale com ele aberto).":
        "EasyJoycon has a new way of setting up Dolphin: close and reopen Dolphin once "
        "(after that, player changes work with it open).",
    "Entrou jogador além dos Wii Remotes ligados no Dolphin: feche e abra o Dolphin pra ligar "
    "mais um (ou suba \"wiimotes\" no config).":
        "A player joined beyond the Wii Remotes enabled in Dolphin: close and reopen Dolphin "
        "to enable one more (or raise \"wiimotes\" in the config).",
    "O EasyJoycon continua ligado perto do relógio — os controles seguem funcionando. Pra "
    "fechar de vez: ícone → Sair.":
        "EasyJoycon keeps running next to the clock — the controllers keep working. To "
        "quit for good: icon → Quit.",
    "A porta do DSU já está em uso — o EasyJoycon já está aberto?":
        "The DSU port is already in use — is EasyJoycon already open?",
    "o servidor parou: {error}": "the server stopped: {error}",
    "Preparando o sistema: digite a sua senha na janelinha.":
        "Setting up the system: type your password in the dialog.",
    "Sistema pronto! Se um controle já estava ligado, desligue e ligue ele de novo.":
        "System ready! If a controller was already on, turn it off and on again.",
    "não sei por quê": "unknown reason",
    "Não deu pra preparar o sistema: {why}": "Couldn't set up the system: {why}",
    "Segure o botãozinho de sync na lateral de cada Joy-Con (as luzes vão correr) até ele "
    "conectar.": "Hold the small sync button on the side of each Joy-Con (the lights will "
                 "run) until it connects.",
    "Esquecer todos os controles?": "Forget all controllers?",
    "Os Joy-Cons e o Pro Controller saem do Bluetooth deste computador, como em \"remover "
    "dispositivo\", e os jogadores são apagados. Depois é só segurar o botãozinho de sync de "
    "um controle por vez pra parear de novo.\n\nOutros aparelhos (fone, teclado) não são "
    "mexidos.":
        "The Joy-Cons and the Pro Controller are removed from this computer's Bluetooth, "
        "like \"remove device\", and the players are cleared. Then just hold the small sync "
        "button on one controller at a time to pair again.\n\nOther devices (headphones, "
        "keyboard) are left alone.",
    "Cancelar": "Cancel",
    "Esquecer": "Forget",
    "Esquecendo… depois segure o sync de UM controle por vez (as luzes correm) até ele "
    "conectar.": "Forgetting… then hold the sync button on ONE controller at a time (the "
                 "lights run) until it connects.",
    "Não configurei o Dolphin: {error}.": "Didn't set up Dolphin: {error}.",
    "Dolphin pronto: {n} Wii Remote(s) e controle(s) de GameCube; trocar o jeito de jogar "
    "vale com ele aberto.": "Dolphin ready: {n} Wii Remote(s) and GameCube controller(s); "
                            "changing how you play works with it open.",
    "O Dolphin já está aberto.": "Dolphin is already open.",
    "Falta o Dolphin": "Dolphin is missing",
    "Abrir o site do Dolphin": "Open the Dolphin website",
    "Fechar": "Close",
    "Não achei o Dolphin instalado neste computador.\n\nInstale pela loja de aplicativos do "
    "sistema (procure \"Dolphin Emulator\") ou, no terminal:\n\n    flatpak install flathub "
    "org.DolphinEmu.dolphin-emu\n\nDepois é só clicar de novo em \"Abrir Dolphin\": o "
    "EasyJoycon configura os controles sozinho.":
        "Dolphin isn't installed on this computer.\n\nInstall it from your system's app "
        "store (search for \"Dolphin Emulator\") or, in the terminal:\n\n    flatpak install "
        "flathub org.DolphinEmu.dolphin-emu\n\nThen just click \"Open Dolphin\" again: "
        "EasyJoycon sets up the controllers on its own.",

    # --- o que falta preparar no sistema ---
    "o Bluetooth ainda não aceita Joy-Con": "Bluetooth doesn't accept Joy-Cons yet",
    "faltam as permissões dos controles": "the controller permissions are missing",
    "o joycond está ligado (ele esconde os botões)": "joycond is running (it hides the buttons)",

    # --- avisos dos jogadores ---
    "{name} voltou pro jogador {n}": "{name} is back as player {n}",
    "{name} → jogador {n} ({mode})": "{name} → player {n} ({mode})",
    "{name} conectado — esperando (L+R ou SL+SR)": "{name} connected — waiting (L+R or SL+SR)",
    "{name} do jogador {n} desconectou": "{name} of player {n} disconnected",
    "{name} desconectou": "{name} disconnected",
    "já tem 4 jogadores — aperte R pra recomeçar": "there are already 4 players — press R to "
                                                   "reset",
    "jogador {n} estava desconectado: lugar reaproveitado":
        "player {n} was disconnected: slot reused",
    "jogador {n}: {names} ({mode})": "player {n}: {names} ({mode})",
    "não tem Joy-Con {side} sozinho pra juntar com o jogador {n}":
        "there's no single {side} Joy-Con to join with player {n}",
    "direito": "right",
    "esquerdo": "left",
    "jogador {n} separado: cada Joy-Con é um Wii Remote em pé":
        "player {n} split: each Joy-Con is an upright Wii Remote",
    "jogador {n}: agora {mode}": "player {n}: now {mode}",
    "jogadores apagados — aperte L+R / SL+SR de novo": "players cleared — press L+R / SL+SR "
                                                       "again",

    # --- avisos do servidor ---
    "calibrando: deixe os controles parados por 2 segundos":
        "calibrating: keep the controllers still for 2 seconds",
    "sem permissão pros LEDs — clique em \"Preparar o sistema\" e depois saia e entre de novo "
    "no computador": "no permission for the LEDs — click \"Set up the system\", then log out "
                     "and back in",
    "{who} está usando o {ctrl}: feche pra não estragar a calibração e a reconexão":
        "{who} is using {ctrl}: close it so it doesn't break calibration and reconnecting",
    " do jogador {n}": " of player {n}",
    "bateria baixa": "battery low",
    "bateria acabando — carregue logo": "battery almost empty — charge it soon",
    "sem adaptador Bluetooth agora — não deu pra esquecer":
        "no Bluetooth adapter right now — couldn't forget",
    "esqueci {n} controle(s) — pareie um de cada vez": "forgot {n} controller(s) — pair them "
                                                       "one at a time",
    "o movimento veio zerado (calibração lida errada)":
        "motion came in as zero (calibration read wrong)",
    "parou de mandar dados faz {s} s": "stopped sending data {s} s ago",
    "ficou conectado pela metade": "got half connected",

    # --- avisos do Bluetooth ---
    "sem acesso ao Bluetooth ({error})": "no access to Bluetooth ({error})",
    "nenhum adaptador Bluetooth encontrado": "no Bluetooth adapter found",
    "adaptador Bluetooth voltou — procurando de novo": "Bluetooth adapter is back — searching "
                                                       "again",
    "o adaptador Bluetooth reiniciou sozinho — os controles caíram":
        "the Bluetooth adapter restarted on its own — the controllers dropped",
    "não consegui procurar: {error}": "couldn't search: {error}",
    "procurando… segure o botãozinho de sync na lateral do controle":
        "searching… hold the small sync button on the side of the controller",
    " (terminou? aperte A)": " (done? press A)",
    "parei de procurar": "stopped searching",
    "controle": "controller",
    "{name}: pareando de novo pra ele lembrar deste computador":
        "{name}: pairing again so it remembers this computer",
    "pareando {name}…": "pairing {name}…",
    "{name}: conectou pela metade — segure o sync de novo":
        "{name}: half connected — hold the sync button again",
    "{name}: o adaptador Bluetooth travou no meio da conexão — segure o sync de novo (se "
    "repetir, é o adaptador)": "{name}: the Bluetooth adapter froze mid-connection — hold "
                              "the sync button again (if it keeps happening, it's the "
                              "adapter)",
    "{name} pareado": "{name} paired",
    " (sem guardar a chave: vai precisar parear de novo da próxima vez)":
        " (without saving the key: it will need pairing again next time)",
    "demorou demais": "took too long",
    "{name}: não conectou ({why}) — segure o sync de novo":
        "{name}: didn't connect ({why}) — hold the sync button again",
    "{name}: {reason} — desconectei; aperte um botão nele pra voltar":
        "{name}: {reason} — disconnected it; press a button on it to bring it back",
}
