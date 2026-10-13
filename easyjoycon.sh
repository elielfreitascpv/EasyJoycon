#!/usr/bin/env bash
# Abre o EasyJoycon (janela). Na primeira vez cria o ambiente Python.
#   ./easyjoycon.sh                     janela
#   ./easyjoycon.sh --terminal          tela no terminal
#   ./easyjoycon.sh configurar-dolphin  mapeia os Wii Remotes no Dolphin
#   ./easyjoycon.sh diagnostico         confere o sistema
set -u
umask 077
cd "$(dirname "$(readlink -f "$0")")" || exit 1
DIR="$(pwd)"

# (Re)cria o atalho do menu com o caminho desta pasta (fica fora do git)
criar_atalho() {
  local atalho="$DIR/EasyJoycon.desktop"
  grep -qF "Icon=$DIR/assets/easyjoycon.svg" "$atalho" 2>/dev/null && return
  cat > "$atalho" <<DESKTOP
[Desktop Entry]
Type=Application
Name=EasyJoycon
Comment=Joy-Cons no Dolphin com movimento
Exec="$DIR/easyjoycon.sh"
Path=$DIR
Icon=$DIR/assets/easyjoycon.svg
Terminal=false
Categories=Game;
DESKTOP
  chmod +x "$atalho"
  gio set "$atalho" metadata::trusted true 2>/dev/null
}
criar_atalho

# O venv enxerga os pacotes do sistema pra usar o GTK (python3-gi) da janela
if [ -x venv/bin/python ] && ! grep -q "include-system-site-packages = true" venv/pyvenv.cfg; then
  rm -rf venv
fi
if [ ! -x venv/bin/python ]; then
  echo "Preparando o EasyJoycon pela primeira vez…"
  python3 -m venv --system-site-packages venv && venv/bin/pip install -q -r requirements.txt || {
    echo "Não consegui instalar as dependências (precisa de internet nessa primeira vez)."
    exit 1
  }
fi

exec venv/bin/python run.py "$@"
