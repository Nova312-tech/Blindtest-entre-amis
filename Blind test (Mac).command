#!/bin/bash
# Lance l'atelier du blind test. Laissez cette fenêtre ouverte pendant la partie.
# (also works from the desktop shortcut: we go back to the real folder of this script)
cible="$0"
[ -L "$cible" ] && cible="$(readlink "$cible")"
cd "$(dirname "$cible")" || exit 1
printf '\033]0;Blind test\007'
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PY=""
for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python est introuvable. Lancez d'abord « Installer (Mac).command » dans ce dossier."
  read -r -p "Appuyez sur Entrée pour fermer cette fenêtre..." _
  exit 1
fi
"$PY" jeu.py || read -r -p "Appuyez sur Entrée pour fermer cette fenêtre..." _
