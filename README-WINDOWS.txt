Application Windows « Blind test »
==================================

L'installateur est destiné aux joueurs : il contient le programme, Python,
FFmpeg, ffprobe, cloudflared et le générateur de QR code. Les joueurs n'ont pas à les installer.
La création du quizz télécharge les extraits audio puis les prépare localement ;
aucun fichier musical ou archive du PC de développement n'est inclus.

Construction de l'installateur (pour le développeur)
----------------------------------------------------

1. Installer Python, FFmpeg (build partagé Windows de Gyan), cloudflared et
   Inno Setup 6 sur le PC de construction.
2. Dans PowerShell, à la racine du projet, lancer :

       python -m pip install -r requirements-build.txt
       .\build-windows.ps1

3. Récupérer build\windows\installer\Blind-test-v2-FR-Setup.exe.

Pour conserver un build existant en cours d'utilisation et générer une
nouvelle version à côté, choisir un autre répertoire :

       .\build-windows.ps1 -OutputRoot build\windows\score-chart

L'installateur sera alors créé dans
build\windows\score-chart\installer\Blind-test-v2-FR-Setup.exe.

Le script inclut le programme, les pages du jeu, FFmpeg et ses DLL, ffprobe,
cloudflared et leurs mentions de licence. Le paquet FFmpeg actuel est GPL v3 ;
voir THIRD_PARTY_NOTICES.txt et les fichiers installés dans « licenses ».

Au lancement, l'atelier s'ouvre dans le navigateur. Gardez cette application
ouverte pendant la préparation et la partie ; le bouton « Quitter » de l'atelier
l'arrête proprement. Si le lancement échoue, une fenêtre indique l'erreur et
le détail est conservé dans journal.txt.

Pendant une partie, deux réponses manquées de suite donnent un pouvoir de
rattrapage à utiliser sur un adversaire après la question. Les effets sont
visuels et durent une question : révélation au ralenti, tomate, brouillard,
rideau de cinéma. En mode « Trouver l'année », seuls les
effets visuels compatibles sont proposés.

Les lots audio, caches, journaux et historique de chaque utilisateur sont
stockés dans %LOCALAPPDATA%\BlindTest. Ils ne sont pas supprimés à la
désinstallation.

Tests ciblés (sur le PC de développement) :

    python -m unittest discover -s tests
