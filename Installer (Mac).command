#!/bin/bash
# Installe ce qu'il faut au blind test sur un Mac, puis crée le raccourci « Blind test » sur le bureau.
cd "$(dirname "$0")" || exit 1
printf '\033]0;Blind test\007'
fin() { echo; read -r -p "Appuyez sur Entrée pour fermer cette fenêtre..." _; exit "$1"; }
echec() { echo; echo "L'installation a échoué. Solution manuelle : installez Homebrew (https://brew.sh), puis tapez dans le Terminal :"; echo "    brew install python ffmpeg"; fin 1; }
echo "=============================================================="
echo "   INSTALLATION DU BLIND TEST (Mac)"
echo "   Installe Python et ffmpeg si besoin, puis crée le raccourci « Blind test » sur le bureau. Comptez 5 à 15 minutes."
echo "=============================================================="
echo

# --- Homebrew ---
BREW=""
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && BREW="$b" && break; done
if [ -z "$BREW" ]; then
  echo "[..] Installation de Homebrew (l'installateur gratuit de programmes pour Mac)."
  echo "     Le mot de passe de votre Mac va être demandé : rien ne s'affiche pendant la frappe, c'est normal."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || echec
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && BREW="$b" && break; done
  [ -n "$BREW" ] || echec
fi
eval "$("$BREW" shellenv)"

# --- Python 3.9 or newer ---
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
  echo "[OK] Python est déjà installé."
else
  echo "[..] Installation de Python..."
  "$BREW" install python || echec
fi

# --- ffmpeg ---
if command -v ffmpeg >/dev/null 2>&1; then
  echo "[OK] ffmpeg est déjà installé."
else
  echo "[..] Installation de ffmpeg..."
  "$BREW" install ffmpeg || echec
fi

# --- desktop shortcut ---
xattr -dr com.apple.quarantine . 2>/dev/null
chmod +x "Blind test (Mac).command" "Installer (Mac).command"
ln -sf "$PWD/Blind test (Mac).command" "$HOME/Desktop/Blind test.command"
echo "[OK] Raccourci « Blind test » créé sur le bureau."
echo
echo "=============================================================="
echo "   TERMINÉ ! Double-cliquez sur « Blind test » sur le bureau."
echo "   Si le Mac demande d'accepter les connexions entrantes pour Python : cliquez sur « Autoriser »."
echo "=============================================================="
fin 0
