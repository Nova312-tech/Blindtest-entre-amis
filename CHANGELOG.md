# Nouveautés

## Version 2.1.0

- **Installation Windows autonome** : nouvel installateur avec FFmpeg, ffprobe, Cloudflare et les pages du jeu
  inclus. Une empreinte SHA-256 est publiée avec chaque fichier de release.
- **Spotify prêt à l’emploi** : le Client ID public est inclus dans l’application ; les données personnelles
  restent locales et les jetons ne sont jamais publiés dans le dépôt.

## Version 2.0.0

- **Connexion Spotify OAuth** : playlists privées, titres aimés et écoutes récentes disponibles depuis l’atelier,
  avec autorisation révoquable et stockage local des jetons sans Client secret.
- **Pouvoirs équilibrables** : après deux erreurs consécutives, un joueur peut choisir un adversaire et lui
  transmettre un effet pour la question suivante.
- **Effets disponibles** : révélation au ralenti, tomate, brouillard et rideau de cinéma.
- **Suivi personnel** : graphique d'évolution du score sur le téléphone.
- **Windows autonome** : scripts de compilation PyInstaller/Inno Setup, fichiers tiers intégrés et données
  utilisateur dans `%LOCALAPPDATA%\BlindTest`.
- **Fabrication dédiée au quiz** : les lots sont préparés pour le jeu sur téléphones.

## Version 1.1.0

- **Compatible Mac** : `Installer (Mac).command` (Homebrew, Python, ffmpeg, raccourci sur le bureau) et
  `Blind test (Mac).command`. Polices de la vidéo, corbeille, ouverture des dossiers, mise en veille,
  Cloudflare et Tailscale adaptés au Mac.

## Version 1.0.0

Première version publique.

- **Atelier** (page web) : fabriquer un blind test à partir d'un thème, d'une playlist Deezer ou Spotify,
  ou d'une liste de chansons ; aperçu des titres avant fabrication ; éditeur de réponses ; archives.
- **Thèmes fidèles** : un thème avec une langue (« chanson française ») ne prend que des chansons dans
  cette langue.
- **Jeu sur téléphones** façon Kahoot : QR code, 4 propositions, points selon la rapidité, séries, questions ×2,
  « le plus rapide », pochettes, classement animé, podium et statistiques.
- **Modes** : classique, lettre par lettre, trouver l'année, quelle langue, quel pays ; **équipes** (2 à 4).
- **Animateur** : le premier joueur inscrit pilote la partie depuis son téléphone.
- **Vidéo YouTube** toute prête (décompte, réponses, intro et fin), fond et décomptes créés automatiquement.
- **Jeu à distance** par Internet (Cloudflare ou Tailscale).
- **Reprise** d'une partie interrompue, journal des événements (`journal.txt`).
