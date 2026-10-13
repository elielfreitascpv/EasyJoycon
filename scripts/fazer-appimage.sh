#!/usr/bin/env bash
# Monta o EasyJoycon-<versão>-x86_64.AppImage em dist/.
#   ./scripts/fazer-appimage.sh
# Precisa de internet (baixa o appimagetool e as dependências). O app dentro
# é só Python: usa o python3 e o GTK do sistema de quem abrir (packaging/AppRun).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
ROOT="$(pwd)"

TOOL_VERSION=1.9.0
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/easyjoycon-build"
TOOL="$CACHE/appimagetool-$TOOL_VERSION-x86_64.AppImage"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/__init__.py)"
OUT="$ROOT/dist/EasyJoycon-$VERSION-x86_64.AppImage"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
APPDIR="$BUILD/EasyJoycon.AppDir"
DEST="$APPDIR/usr/share/easyjoycon"

if [ ! -x "$TOOL" ]; then
  echo "Baixando o appimagetool $TOOL_VERSION…"
  mkdir -p "$CACHE"
  curl -fL -o "$TOOL.tmp" \
    "https://github.com/AppImage/appimagetool/releases/download/$TOOL_VERSION/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL.tmp"
  mv "$TOOL.tmp" "$TOOL"
fi

echo "Copiando o app…"
mkdir -p "$DEST/scripts" "$DEST/assets"
cp -r app run.py README.md README.pt-BR.md LICENSE "$DEST/"
cp scripts/instalar-sistema.sh scripts/desfazer-sistema.sh "$DEST/scripts/"
cp assets/easyjoycon.svg "$DEST/assets/"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +

echo "Dependências (em Python puro, servem pra qualquer Python 3.10+)…"
PIP=(python3 -m pip)
[ -x venv/bin/pip ] && PIP=(venv/bin/pip)
SKIP_CYTHON=1 "${PIP[@]}" install -q --no-binary dbus-fast --no-deps --target "$DEST/vendor" \
  "dbus-fast==5.0.26" "tomli==2.0.1"
if find "$DEST/vendor" -name "*.so" | grep -q .; then
  echo "Erro: veio parte compilada nas dependências (só funcionaria numa versão do Python)." >&2
  exit 1
fi
rm -rf "$DEST"/vendor/*.dist-info/RECORD "$DEST/vendor/bin"

cp packaging/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp assets/easyjoycon.svg "$APPDIR/easyjoycon.svg"
ln -s easyjoycon.svg "$APPDIR/.DirIcon"
cat > "$APPDIR/easyjoycon.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=EasyJoycon
Comment=Joy-Cons in Dolphin with motion controls
Comment[pt]=Joy-Cons no Dolphin com movimento
Exec=AppRun
Icon=easyjoycon
Terminal=false
Categories=Game;
DESKTOP

echo "Montando o AppImage…"
mkdir -p dist
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" "$OUT" >"$BUILD/tool.log" 2>&1 || { cat "$BUILD/tool.log"; exit 1; }
echo "Pronto: $OUT ($(du -h "$OUT" | cut -f1))"
