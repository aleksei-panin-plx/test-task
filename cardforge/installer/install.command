#!/bin/sh
# Двойной щелчок в Finder устанавливает CardForge (macOS). Окно Терминала откроется само —
# в нём ничего печатать не нужно: ключ OpenRouter спросит отдельное окно.
cd "$(dirname "$0")/.." || exit 1
clear
echo "CardForge: установка. Это займёт 5–15 минут; окно можно не трогать."
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
