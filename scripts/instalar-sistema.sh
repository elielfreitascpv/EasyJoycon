#!/usr/bin/env bash
# Prepara o sistema pro EasyJoycon (uma vez só; pede a senha de administrador).
# Roda pelo terminal (sudo) ou pelo botão "Preparar o sistema" da janela
# (pkexec). O que faz, na ordem:
#   1. BlueZ aceita Joy-Con sem "bond" (ClassicBondedOnly=false) — sem isso o
#      Bluetooth conecta mas o controle não vira controle no Linux.
#   2. Permissões: quem está usando o computador (uaccess, na hora) e o grupo
#      input (LEDs; vale depois de sair e entrar) podem ler os botões dos
#      controles da Nintendo, conversar direto com eles (hidraw: vibração,
#      cor e movimento direto) e criar o controle virtual (/dev/uinput:
#      vibração dos jogos, Nunchuk e modo deitado no Dolphin).
#   3. Tira a economia de energia do USB dos adaptadores Bluetooth CSR8510
#      (os clones baratos caem do USB com ela).
#   4. Desliga o joycond (ele escondia os botões e juntava os Joy-Cons do
#      jeito dele; agora o EasyJoycon faz isso).
# Desfazer: scripts/desfazer-sistema.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Vou pedir a senha de administrador pra mexer em configurações do sistema."
  exec sudo "$0" "$@"
fi

# Quem pediu (pra pôr no grupo input): sudo deixa SUDO_USER, pkexec PKEXEC_UID
USUARIO="${SUDO_USER:-}"
if [ -z "$USUARIO" ] && [ -n "${PKEXEC_UID:-}" ]; then
  USUARIO="$(id -nu "$PKEXEC_UID" 2>/dev/null || true)"
fi

CONF=/etc/bluetooth/input.conf
RULES=/etc/udev/rules.d
VERSAO=2   # app/sistema.py RULES_VERSION: subir quando mudar as regras
reiniciar_bt=0

echo "1/4 Bluetooth: aceitar Joy-Con sem bond"
if grep -q '^ClassicBondedOnly=false' "$CONF" 2>/dev/null; then
  echo "    já estava"
else
  if grep -q '^ClassicBondedOnly=' "$CONF" 2>/dev/null; then
    sed -i 's/^ClassicBondedOnly=.*/ClassicBondedOnly=false/' "$CONF"
  elif grep -q '^\[General\]' "$CONF" 2>/dev/null; then
    sed -i 's/^\[General\]$/[General]\nClassicBondedOnly=false/' "$CONF"
  else
    printf '[General]\nClassicBondedOnly=false\n' >> "$CONF"
  fi
  reiniciar_bt=1
  echo "    feito"
fi

echo "2/4 Permissões dos controles"
# uaccess precisa vir antes do 73-seat-late.rules (que aplica o acesso)
cat > "$RULES/70-easyjoycon-uaccess.rules" <<UDEV
# easyjoycon-sistema $VERSAO
# EasyJoycon: quem está usando o computador acessa os controles da
# Nintendo (057e) e o /dev/uinput (controle virtual pro Dolphin).
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/vendor}=="057e", TAG+="uaccess"
KERNEL=="hidraw*", KERNELS=="*057E:*", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="057e", TAG+="uaccess"
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
UDEV
cat > "$RULES/99-easyjoycon.rules" <<UDEV
# easyjoycon-sistema $VERSAO
# EasyJoycon: controles da Nintendo (057e) também pro grupo input (sessão
# sem login gráfico) e LEDs de jogador graváveis pelo grupo input.
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/vendor}=="057e", MODE="0660", GROUP="input"
SUBSYSTEM=="leds", KERNEL=="000[35]:057E:*", RUN+="/bin/chgrp input /sys%p/brightness", RUN+="/bin/chmod 0664 /sys%p/brightness"
KERNEL=="hidraw*", KERNELS=="*057E:*", MODE="0660", GROUP="input"
KERNEL=="hidraw*", ATTRS{idVendor}=="057e", MODE="0660", GROUP="input"
UDEV
echo "uinput" > /etc/modules-load.d/easyjoycon.conf
modprobe uinput 2>/dev/null || true
udevadm control --reload
for sub in input hidraw leds misc; do
  udevadm trigger --subsystem-match="$sub" --action=add
done
if [ -n "$USUARIO" ] && [ "$USUARIO" != root ] && ! id -nG "$USUARIO" | grep -qw input; then
  usermod -aG input "$USUARIO"
  echo "    feito ($USUARIO entrou no grupo input: as luzes de jogador valem depois de sair e entrar)"
else
  echo "    feito"
fi

echo "3/4 Adaptador Bluetooth sem economia de energia no USB"
# Os clones de CSR8510 (0a12:0001) "caem do USB" quando o Linux tenta
# suspender o adaptador pra economizar energia.
echo "options btusb enable_autosuspend=0" > /etc/modprobe.d/easyjoycon-btusb.conf
cat > "$RULES/98-easyjoycon-bt-power.rules" <<'UDEV'
# EasyJoycon: adaptador Bluetooth sempre ligado (sem autosuspend do USB)
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0a12", ATTR{idProduct}=="0001", ATTR{power/control}="on"
UDEV
for dev in /sys/bus/usb/devices/*; do
  if [ "$(cat "$dev/idVendor" 2>/dev/null)" = "0a12" ] && [ "$(cat "$dev/idProduct" 2>/dev/null)" = "0001" ]; then
    echo on > "$dev/power/control"
  fi
done
echo N > /sys/module/btusb/parameters/enable_autosuspend 2>/dev/null || true
echo "    feito"

echo "4/4 Desligar o joycond"
if systemctl list-unit-files joycond.service >/dev/null 2>&1; then
  systemctl disable --now joycond.service >/dev/null 2>&1 || true
  echo "    feito (pra voltar: scripts/desfazer-sistema.sh)"
else
  echo "    não está instalado"
fi

if [ "$reiniciar_bt" -eq 1 ]; then
  echo "Reiniciando o Bluetooth (os controles desconectam; aperte um botão neles depois)"
  systemctl restart bluetooth
fi
echo "Pronto."
