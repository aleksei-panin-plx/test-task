#!/bin/sh
# Двойной щелчок в Finder устанавливает CardForge Light (macOS). Окно Терминала откроется само —
# в нём ничего печатать не нужно. Ключи Light не нужны.
cd "$(dirname "$0")/.." || exit 1
clear
echo "CardForge Light: установка. Это займёт 1–3 минуты; окно можно не трогать."
echo
sh installer/install.sh --gui
status=$?
echo
if [ "$status" = 0 ]; then
  echo "Готово. Это окно можно закрыть."
else
  echo "Установка не завершилась (код $status). Сохраните текст выше и отправьте команде CardForge."
fi
echo "Нажмите Enter, чтобы закрыть окно."
read -r _
