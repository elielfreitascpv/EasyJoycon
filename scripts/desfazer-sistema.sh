#!/usr/bin/env bash
# Desfaz o instalar-sistema.sh: tira as regras do udev e o carregamento do
# uinput, volta a economia de energia do adaptador e religa o joycond.
# (Deixa o ClassicBondedOnly=false: sem ele os Joy-Cons não funcionam nem
# com o joycond. E não tira ninguém do grupo input.)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

rm -f /etc/udev/rules.d/99-easyjoycon.rules /etc/udev/rules.d/70-easyjoycon-uaccess.rules \
  /etc/udev/rules.d/98-easyjoycon-bt-power.rules
rm -f /etc/modprobe.d/easyjoycon-btusb.conf /etc/modules-load.d/easyjoycon.conf
udevadm control --reload
for sub in input hidraw leds misc; do
  udevadm trigger --subsystem-match="$sub" --action=add
done
if systemctl list-unit-files joycond.service >/dev/null 2>&1; then
  systemctl enable --now joycond.service
fi
echo "Pronto: regras removidas e joycond ligado de novo."
