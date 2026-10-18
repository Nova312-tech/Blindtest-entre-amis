#!/usr/bin/env python3
"""
Blind test : génère des extraits audio (01.mp3, 02.mp3...) à partir des extraits
gratuits de 30 s de Deezer, et la fiche réponses.

Double-clic sur le script : menu interactif.

En ligne de commande :
  python blindtest.py --option 1 --theme "années 80"
  python blindtest.py --option 2 --playlist https://www.deezer.com/fr/playlist/867825522
  python blindtest.py --option 3 --liste titres.txt
  python blindtest.py --duree 15 --nombre 40 --spotify https://open.spotify.com/playlist/...
  python blindtest.py --annuler              (annule le dernier lancement de l'historique)

Formats :
  option 1 : 50 extraits de 10 s  -> dossier extraits_10s
  option 2 : 35 extraits de 20 s  -> dossier extraits_20s
  option 3 : 25 extraits de 30 s  -> dossier extraits_30s
  --duree N : durée libre de 5 à 30 s -> dossier extraits_Ns

Le même moteur sert à la page web (python jeu.py) : voir reglages() et lancer().

Options utiles :
  --essai            génère les extraits dans essai_10s / essai_20s / essai_30s,
                     sans toucher au dossier du montage ni à l'historique
  --melanger         mode liste : mélange l'ordre des titres
  --debut 5          décalage de départ dans l'extrait Deezer (options 1 et 2)
  --max-artiste 2    nombre max de titres par artiste
  --top 50           ne garder que les X % de titres les plus populaires
  --blocage 2        nombre de lancements pendant lesquels un titre utilisé est bloqué
  --nombre N         forcer le nombre d'extraits (tests)

Prérequis : Python 3.8+ et ffmpeg / ffprobe (dans le PATH ou à côté du script).
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = "https://api.deezer.com"
UA = {"User-Agent": "Mozilla/5.0 (blindtest)"}

FORMATS = {
    1: {"nombre": 50, "duree": 10},
    2: {"nombre": 35, "duree": 20},
    3: {"nombre": 25, "duree": 30},
}
DUREE_PREVIEW = 30
DUREE_MIN, DUREE_MAX = 5, DUREE_PREVIEW
FONDU = 0.5
TOLERANCE = 0.5          # un extrait peut être jusqu'à 0,5 s plus court que prévu
MARGE = 1.10             # 10 % de candidats en plus pour remplacer les extraits défaillants
PALIERS_PLAYLISTS = [3, 5, 8, 12, 20, 30, 45]
POPULARITES = [50, 75, 100]
CACHE_DUREE = 6 * 3600
PARALLELE = 6

FICHIER_HISTORIQUE = BASE / "historique.json"
DOSSIER_CACHE = BASE / "cache"
DOSSIER_TRAVAIL = BASE / "_travail"
FICHIER_VERROU = BASE / "_generation.lock"


class Erreur(Exception):
    """Erreur à afficher telle quelle à l'utilisateur."""


class Annule(Erreur):
    """Fabrication arrêtée à la demande de l'utilisateur (bouton « Annuler » de la page web)."""


class Introuvable(Exception):
    pass


class FichierVerrouille(Erreur):
    pass


# La page web s'abonne aux messages (journal de progression) et peut demander l'arrêt de la fabrication.
ECOUTEURS = []
ANNULATION = threading.Event()


def afficher(msg=""):
    try:
        print(msg, flush=True)
    except (OSError, ValueError):     # pas de console (lancé sans fenêtre)
        pass
    for f in list(ECOUTEURS):
        f(str(msg))


def verifier_annulation():
    if ANNULATION.is_set():
        raise Annule("Arrêté à votre demande.")


class verrou_generation:
    """Une seule fabrication à la fois (deux lancements se marcheraient dessus dans _travail).
    Verrou système : libéré automatiquement si le programme est fermé brutalement."""

    def __enter__(self):
        self.f = open(FICHIER_VERROU, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise Erreur("Un autre blind test est déjà en cours de fabrication (autre fenêtre ou page web). "
                         "Attendez qu'il se termine, puis relancez.")
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()


def musique_continue(duree):
    """Devinette de 25 s ou moins : pendant la réponse, la musique continue jusqu'au bout de l'extrait
    Deezer (30 s). Au-delà, il ne reste pas assez de musique : la réponse se fait en silence."""
    return duree <= DUREE_PREVIEW - DUREE_REPONSE


def nombre_par_defaut(duree):
    """Nombre d'extraits du format le plus proche (10 s -> 50, 20 s -> 35, 30 s -> 25)."""
    return min(FORMATS.values(), key=lambda f: (abs(f["duree"] - duree), f["duree"]))["nombre"]


def debut_maxi(duree):
    """Décalage de départ maximal dans l'extrait Deezer (on garde 5 s de musique pour la réponse)."""
    return max(0, DUREE_PREVIEW - duree - (DUREE_REPONSE if musique_continue(duree) else 0))


def detecter_source(texte):
    """Champ unique de la page web : renvoie (type, valeur) avec type = spotify, playlist, lignes ou theme."""
    texte = (texte or "").strip()
    lignes = [l.strip() for l in texte.splitlines() if l.strip() and not l.strip().startswith("#")]
    if len(lignes) > 1 or (len(lignes) == 1 and " - " in texte and not re.match(r"(https?://|www\.)", texte.lower())):
        return "lignes", lignes
    if not texte:
        raise Erreur("Indiquez un thème, un lien de playlist (Deezer ou Spotify) ou une liste de titres.")
    bas = texte.lower()
    if "spotify" in bas:
        return "spotify", texte
    if "deezer" in bas or (texte.isdigit() and len(texte) >= 5):   # numéro de playlist Deezer
        return "playlist", texte
    if re.match(r"(https?://|www\.)", bas):
        raise Erreur("Lien non reconnu : seuls les liens de playlist Deezer ou Spotify sont acceptés.")
    return "theme", texte


def secondes(x):
    """3.5 -> '3,5' ; 5.0 -> '5'"""
    return f"{x:g}".replace(".", ",")


# ---------- Accès réseau (avec nouvelles tentatives et limite de débit) ----------

class Limiteur:
    """Deezer accepte environ 50 requêtes par 5 s : on reste en dessous."""

    def __init__(self, maxi=40, fenetre=5.0):
        self.maxi, self.fenetre = maxi, fenetre
        self.instants = deque()
        self.verrou = threading.Lock()

    def attendre(self):
        while True:
            with self.verrou:
                maintenant = time.monotonic()
                while self.instants and maintenant - self.instants[0] > self.fenetre:
                    self.instants.popleft()
                if len(self.instants) < self.maxi:
                    self.instants.append(maintenant)
                    return
                pause = self.fenetre - (maintenant - self.instants[0]) + 0.05
            time.sleep(pause)


LIMITEUR = Limiteur()
TENTATIVES = 6


def api(chemin):
    verifier_annulation()
    url = chemin if chemin.startswith("http") else API + chemin
    derniere = None
    for tentative in range(TENTATIVES):
        if tentative:
            time.sleep(min(2 ** tentative, 20))
        LIMITEUR.attendre()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            derniere = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504):
                continue
            raise Erreur(f"Deezer a refusé la requête ({derniere}).")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
            derniere = str(getattr(e, "reason", e))
            continue
        if isinstance(data, dict) and "error" in data:
            err = data["error"] if isinstance(data["error"], dict) else {}
            code = err.get("code")
            if code in (4, 700):          # limite de requêtes / service occupé
                derniere = err.get("message", "limite de requêtes")
                time.sleep(5)
                continue
            if code == 800:
                raise Introuvable(err.get("message", "introuvable"))
            raise Erreur(f"Erreur Deezer : {err.get('message', 'inconnue')}")
        return data
    raise Erreur("Deezer ne répond pas après plusieurs tentatives "
                 f"({derniere}). Vérifiez la connexion Internet puis relancez.")


def telecharger(url, cible):
    """Télécharge un extrait. Renvoie False si le lien est expiré / refusé."""
    derniere = None
    for tentative in range(4):
        if tentative:
            time.sleep(2 ** tentative)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r, open(cible, "wb") as f:
                shutil.copyfileobj(r, f)
            if Path(cible).stat().st_size < 10_000:
                derniere = "fichier reçu trop petit"
                continue
            return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                return False
            derniere = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            derniere = str(getattr(e, "reason", e))
    raise Erreur(f"téléchargement impossible ({derniere})")


def lien_expire(url, marge=120):
    m = re.search(r"exp=(\d+)", url or "")
    return bool(m) and int(m.group(1)) < time.time() + marge


# ---------- Normalisation / dédoublonnage ----------

MOTS_VERSION = re.compile(
    r"\b(remaster\w*|version|edit|radio|mix|mono|stereo|live|single|album|"
    r"extended|original|acoustic|acoustique|deluxe|bonus|demo|explicit|clean|"
    r"from|b\.?o\.?|soundtrack|feat|ft)\b", re.I)


def sans_accents(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normaliser(s):
    s = sans_accents(s).lower()
    s = s.replace("&", " and ").replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def normaliser_titre(titre):
    t = re.sub(r"\s*[\(\[].*?[\)\]]", " ", titre)            # (Remastered 2011), [Radio Edit], (feat. X)
    parties = re.split(r"\s+[-–—]\s+", t)
    while len(parties) > 1 and MOTS_VERSION.search(parties[-1]):  # "Titre - Remastered 2011"
        parties.pop()
    t = " - ".join(parties)
    t = re.split(r"\s+(?:feat\.?|ft\.?)\s+", t, flags=re.I)[0]
    return normaliser(t) or normaliser(titre)


def cle_titre(artiste, titre):
    return f"{normaliser(artiste)}|{normaliser_titre(titre)}"


def piste_vers_dict(p):
    return {
        "id": p.get("id"),
        "artiste": p["artist"]["name"],
        "titre": p.get("title_short") or p["title"],
        "preview": p.get("preview"),
        "rank": p.get("rank") or 0,
    }


# ---------- Sources de morceaux ----------

def id_playlist(texte):
    """Accepte un numéro, un lien deezer.com/.../playlist/123 ou un lien court de partage."""
    texte = texte.strip().strip('"').strip()
    if texte.isdigit():
        return texte
    m = re.search(r"playlist/(\d+)", texte)
    if m:
        return m.group(1)
    if re.match(r"https?://", texte):
        try:
            req = urllib.request.Request(texte, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                final = r.geturl()
                corps = r.read(200_000).decode("utf-8", "replace")
        except Exception as e:
            raise Erreur(f"Impossible d'ouvrir le lien : {texte} ({e})")
        for t in (final, corps):
            m = re.search(r"playlist/(\d+)", t)
            if m:
                return m.group(1)
    raise Erreur(f"Impossible de trouver un identifiant de playlist dans : {texte}\n"
                 "Exemple attendu : https://www.deezer.com/fr/playlist/867825522")


def charger_playlist(pid, titre=None, cache=True):
    """Titres d'une playlist (avec cache de 6 h)."""
    DOSSIER_CACHE.mkdir(exist_ok=True)
    fichier = DOSSIER_CACHE / f"playlist_{pid}.json"
    if cache and fichier.exists():
        try:
            data = json.loads(fichier.read_text(encoding="utf-8"))
            if time.time() - data["date"] < CACHE_DUREE:
                return data["titre"], data["pistes"]
        except (ValueError, KeyError, OSError):
            pass
    if titre is None:
        try:
            titre = api(f"/playlist/{pid}").get("title", f"playlist {pid}")
        except Introuvable:
            raise Erreur(f"Playlist {pid} introuvable (supprimée ou privée ?).")
    pistes, index = [], 0
    while index < 2000:
        data = api(f"/playlist/{pid}/tracks?limit=100&index={index}")
        lot = data.get("data", [])
        pistes += [piste_vers_dict(p) for p in lot
                   if p.get("readable", True) and p.get("preview") and p.get("type", "track") == "track"]
        if not data.get("next") or not lot:
            break
        index += len(lot)
    try:
        tmp = fichier.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": time.time(), "titre": titre, "pistes": pistes},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, fichier)
    except OSError:
        pass
    return titre, pistes


def est_editoriale(pl):
    nom = normaliser(pl.get("user", {}).get("name", ""))
    return any(m in nom.split() for m in ("deezer", "editor", "editrice", "editeur"))


def decennies(texte):
    """Décennies citées : 'années 80', '80s', '1980', '2000' -> {'80', '00'}."""
    trouve = set()
    for m in re.finditer(r"\b(?:19|20)?(\d)0s?\b", normaliser(texte)):
        if len(m.group(0).rstrip("s")) in (2, 4):
            trouve.add(m.group(1) + "0")
    return trouve


def titre_colle_au_theme(titre, theme):
    """Le nom de la playlist contient tous les mots du thème (pluriels et '80s' tolérés)
    et ne cite pas d'autre décennie que celles du thème."""
    mots_titre = [m.rstrip("s") for m in normaliser(titre).split()]
    for mot in normaliser(theme).split():
        if len(mot) < 2 or mot in MOTS_VIDES:
            continue
        if not any(t.startswith(mot.rstrip("s")) for t in mots_titre):
            return False
    d_theme = decennies(theme)
    return not d_theme or decennies(titre) <= d_theme


MOTS_VIDES = {"de", "des", "du", "la", "le", "les", "et", "en", "the", "of", "and", "a"}


def paliers(nb):
    return sorted({n for n in PALIERS_PLAYLISTS if n < nb} | {min(nb, PALIERS_PLAYLISTS[-1])} - {0})


def dedoublonner(pistes):
    uniques = {}
    for p in pistes:
        k = cle_titre(p["artiste"], p["titre"])
        if k not in uniques or p["rank"] > uniques[k]["rank"]:
            uniques[k] = dict(p, cle=k)
    return list(uniques.values())


def candidats(pool, bloques, top, max_artiste):
    """Filtre de popularité, historique, tirage au sort, limite par artiste."""
    tri = sorted(pool, key=lambda p: p["rank"], reverse=True)
    garde = tri[:max(1, math.ceil(len(tri) * top / 100))] if tri else []
    dispo = [p for p in garde if p["cle"] not in bloques]
    random.shuffle(dispo)
    choix, compte = [], Counter()
    for p in dispo:
        a = normaliser(p["artiste"])
        if compte[a] < max_artiste:
            choix.append(p)
            compte[a] += 1
    return choix


# ---------- Rester dans le thème : langue / pays ----------
# « chanson française » : jamais de titres en anglais pour compléter, même si les playlists n'en ont pas assez.
# 1) les playlists dont le nom cite une autre langue (« Pop française ET anglaise ») sont écartées ;
# 2) chaque titre est vérifié : langue du titre, sinon pays de l'artiste (MusicBrainz), sinon il doit
#    apparaître dans au moins deux playlists du thème (ou une playlist éditoriale Deezer).

GROUPES_LANGUE = {
    "fr": {"prefixes": ("francai", "francopho", "french", "france"), "mots": ("fr", "vf")},
    "en": {"prefixes": ("anglais", "anglopho", "english", "britanni", "british", "americai", "american",
                        "irlandai", "irish", "australi"), "mots": ("uk", "us", "usa", "brit")},
    "es": {"prefixes": ("espagnol", "spanish", "hispan", "latino", "espana"), "mots": ()},
    "it": {"prefixes": ("italie", "italian", "italia"), "mots": ()},
    "de": {"prefixes": ("allemand", "german", "deutsch"), "mots": ()},
    "pt": {"prefixes": ("portugai", "portugues", "bresil", "brasil", "brazil", "bossa"), "mots": ()},
    "kr": {"prefixes": ("coreen", "korean"), "mots": ("kpop",), "paires": (("k", "pop"),)},
    "jp": {"prefixes": ("japonai", "japanese"), "mots": ("jpop",), "paires": (("j", "pop"),)},
    "ar": {"prefixes": ("arabe", "arabic"), "mots": ()},
}
NOMS_LANGUES = {"fr": "français", "en": "anglais", "es": "espagnol", "it": "italien", "de": "allemand",
                "pt": "portugais", "kr": "coréen", "jp": "japonais", "ar": "arabe"}
MOTS_GENERIQUES = {"chanson", "chansons", "musique", "musiques", "music", "hits", "hit", "tubes", "tube", "titres",
                   "songs", "song", "playlist", "best", "top", "meilleures", "meilleurs", "classiques",
                   "incontournables", "variete", "varietes", "essentials", "essentiels"}


def langues_citees(texte):
    """Langues ou pays cités : « pop anglaise » -> {'en'} ; « Rap FR & US » -> {'fr', 'en'}."""
    mots = normaliser(texte).split()
    trouve = set()
    for code, g in GROUPES_LANGUE.items():
        if any(m in g["mots"] or (len(m) >= 4 and m.startswith(g["prefixes"])) for m in mots):
            trouve.add(code)
        if any((a, b) in g.get("paires", ()) for a, b in zip(mots, mots[1:])):
            trouve.add(code)
    return trouve


# mots fréquents et propres à une langue (les mots partagés, comme « la » ou « me », sont laissés de côté)
MOTS_LANGUE = {
    "fr": set("""le les une des du et est je il elle nous vous ils elles mon mes ton tes ses moi toi lui pas ne plus
        qui quoi dans sur pour avec sans sous chez au aux ce cette ces mais ou quand tout tous toute rien jamais toujours
        encore amour coeur vie nuit jour monde homme femme fille garcon reve ciel soleil mer belle beau petit petite
        mieux bien chanson danse danser aimer aime aimes sais veux peux faut fait vais suis sommes etes sont ai avons
        ont etre avoir merci adieu bonjour ici enfants papa maman pourquoi comment notre votre leur demain hier vivre
        mourir partir revenir pense ensemble seul seule autre autres vers deja maison""".split()),
    "en": set("""the and is are was were be been you he she we they my your his our their him them this that these
        those what when where why how who not yes dont cant wont aint love heart life night day time world man woman
        girl boy baby dream sky sun beautiful little big good bad song want need know like feel make take give get go
        come say see look think tell call stay leave let just only never ever always still every everything nothing
        something more way home away back down up out over into from with without of on at by for to tonight tomorrow
        yesterday forever together alone again sweet look boys cry run high low live summer one""".split()),
    "es": set("""el los las y que por con para mi su como pero mas muy amor corazon vida noche dia tiempo mundo quiero
        puedo tengo eres soy estoy esta donde cuando porque sin bailar besame te quiero""".split()),
    "it": set("""il lo gli di che non per con sono sei amore cuore vita notte giorno tempo mondo voglio io lei noi voi
        quando perche senza sempre ancora piu bella ciao""".split()),
    "de": set("""der die das ein eine und ist ich du er wir ihr nicht mit fur auf im zu von liebe herz leben nacht tag
        zeit welt mein dein wenn warum ohne immer noch""".split()),
    "pt": set("""os um uma nao meu minha voce eu ele ela nos coracao vida noite dia tempo mundo quero sou estou
        quando porque sem sempre saudade""".split()),
}
_PARTAGES = {m for k, v in MOTS_LANGUE.items() for m in v
             if any(m in w for k2, w in MOTS_LANGUE.items() if k2 != k)}
for _code in MOTS_LANGUE:            # un mot présent dans deux listes ne prouve rien
    MOTS_LANGUE[_code] -= _PARTAGES
ACCENTS = {"fr": "èêëçùûôîïœ", "es": "ñáíóú¿¡", "it": "ìò", "pt": "ãõ", "de": "äöüß"}
ACCENTS_FAIBLES = {"fr": "éà"}       # aussi en espagnol, italien, portugais : un indice seulement
CONTRACTIONS = {"fr": re.compile(r"\b(?:j|l|d|qu|c|n|m|t|s)'\w", re.I),
                "en": re.compile(r"\w'(?:s|t|m|re|ll|ve|d)\b|\bi'm\b|\bain't\b", re.I)}
LANGUES_TITRE = {"fr", "en", "es", "it", "de", "pt"}      # langues reconnaissables au titre


def langue_titre(titre):
    """Langue d'un titre si elle est claire, sinon None (« Imagine », « Hallelujah », « Mamma Mia »)."""
    brut = re.sub(r"\s*[\(\[].*?[\)\]]", " ", titre or "").replace("’", "'").lower()
    brut = re.split(r"\s+(?:feat\.?|ft\.?)\s+|\s+-\s+", brut)[0]
    scores = Counter()
    for code, signes in ACCENTS.items():
        if any(ch in brut for ch in signes):
            scores[code] += 2
    for code, signes in ACCENTS_FAIBLES.items():
        if any(ch in brut for ch in signes):
            scores[code] += 1
    for code, motif in CONTRACTIONS.items():
        if motif.search(brut):
            scores[code] += 2
    for mot in normaliser(brut).split():
        for code, mots in MOTS_LANGUE.items():
            if mot in mots:
                scores[code] += 1
    if not scores:
        return None
    (premier, s1), *reste = scores.most_common()
    return premier if s1 >= 1 and (not reste or s1 > reste[0][1]) else None


# pays (code ISO de MusicBrainz) -> langues probables de ses artistes ; ailleurs : surtout l'anglais
LANGUES_PAYS = {"FR": {"fr"}, "BE": {"fr"}, "LU": {"fr"}, "MC": {"fr"}, "CH": {"fr", "de", "it"},
                "CA": {"fr", "en"}, "GB": {"en"}, "US": {"en"}, "IE": {"en"}, "AU": {"en"}, "NZ": {"en"},
                "ES": {"es"}, "IT": {"it"}, "SM": {"it"}, "DE": {"de"}, "AT": {"de"}, "PT": {"pt"}, "BR": {"pt"},
                "KR": {"kr"}, "JP": {"jp"}, "MA": {"ar", "fr"}, "DZ": {"ar", "fr"}, "TN": {"ar", "fr"},
                "SN": {"fr"}, "CI": {"fr"}, "CM": {"fr"}, "CD": {"fr"}, "HT": {"fr"}}
for _p in ("MX", "AR", "CO", "CL", "PE", "VE", "CU", "PR", "DO", "UY", "EC", "BO", "PA", "CR", "GT", "HN", "SV",
           "NI", "PY"):
    LANGUES_PAYS[_p] = {"es"}
for _p in ("EG", "LB", "SA", "AE", "IQ", "SY", "JO"):
    LANGUES_PAYS[_p] = {"ar"}

MB_API = "https://musicbrainz.org/ws/2/artist/"
# MusicBrainz demande que chaque application se présente (nom, version, contact) : mettez l'adresse de votre
# dépôt ou de votre site à la place de CONTACT si vous publiez le programme
APPLI, CONTACT = "BlindTestEntreAmis/1.0", "https://github.com"
MB_UA = {"User-Agent": f"{APPLI} ( {CONTACT} )", "Accept": "application/json"}
FICHIER_PAYS = DOSSIER_CACHE / "pays_artistes.json"
PAYS_MAX = 200           # vérifications MusicBrainz par fabrication (une par seconde ; mémorisées ensuite)


class PaysArtistes:
    """Pays d'un artiste d'après MusicBrainz, gardé en mémoire d'une fois sur l'autre (cache/pays_artistes.json)."""

    def __init__(self):
        try:
            self.connus = json.loads(FICHIER_PAYS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.connus = {}
        self.nouveaux, self.dernier = 0, 0.0

    def connu(self, nom):
        return normaliser(nom) in self.connus

    def __call__(self, nom):
        cle = normaliser(nom)
        if cle not in self.connus:
            pays = self._chercher(nom, cle)
            if pays is None:                       # MusicBrainz injoignable : on réessaiera une autre fois
                return None
            self.connus[cle] = pays
            self.nouveaux += 1
        return self.connus[cle] or None

    def _chercher(self, nom, cle):
        q = urllib.parse.quote('artist:"' + nom.replace('"', "") + '"')
        for _ in range(3):
            verifier_annulation()
            pause = 1.1 - (time.monotonic() - self.dernier)
            if pause > 0:
                time.sleep(pause)
            self.dernier = time.monotonic()
            try:
                req = urllib.request.Request(f"{MB_API}?query={q}&fmt=json&limit=6", headers=MB_UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    artistes = json.load(r).get("artists", [])
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    return None
                time.sleep(2)
            except (OSError, ValueError):
                time.sleep(2)
        else:
            return None
        exacts = [a for a in artistes if a.get("score", 0) >= 90
                  and (normaliser(a.get("name", "")) == cle
                       or any(normaliser(x.get("name", "")) == cle for x in a.get("aliases") or []))]
        pays = {a.get("country") or ((a.get("area") or {}).get("iso-3166-1-codes") or [None])[0]
                for a in exacts[:3]} - {None, "XW", "XE"}
        return pays.pop() if len(pays) == 1 else ""      # homonymes de pays différents : on ne tranche pas

    def enregistrer(self):
        if self.nouveaux:
            DOSSIER_CACHE.mkdir(exist_ok=True)
            tmp = FICHIER_PAYS.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.connus, ensure_ascii=False, indent=0), encoding="utf-8")
            os.replace(tmp, FICHIER_PAYS)
            self.nouveaux = 0


class FiltreTheme:
    """Garde seulement les titres dans la langue du thème. Les décisions sont gardées d'une étape à l'autre."""

    def __init__(self, langues):
        self.langues = langues
        self.par_titre = langues <= LANGUES_TITRE        # K-pop : titres souvent en anglais, seul le pays compte
        self.pays = PaysArtistes()
        self.decisions = {}                               # clé du titre -> (gardé ?, raison)
        self.verifies = 0
        self.echecs = 0                                   # MusicBrainz injoignable : on arrête de le solliciter

    def choisir(self, pool, choix, presence, fiables, objectif):
        """Parcourt les titres tirés au sort (dans l'ordre) et garde ceux du thème jusqu'à en avoir `objectif` :
        on ne vérifie sur MusicBrainz que les artistes vraiment nécessaires."""
        langue_artiste = {}                       # langue déjà claire d'après d'autres titres de l'artiste
        if self.par_titre:
            for x in pool:
                lt = langue_titre(x["titre"])
                if lt:
                    langue_artiste.setdefault(normaliser(x["artiste"]), set()).add(lt)
        gardes = []
        for x in choix:
            if len(gardes) >= objectif:
                break
            if x["cle"] not in self.decisions:
                self._decider(x, langue_artiste, presence, fiables)
            if self.decisions[x["cle"]][0]:
                gardes.append(x)
        self.pays.enregistrer()
        return gardes

    def _decider(self, x, langue_artiste, presence, fiables):
        k, artiste = x["cle"], x["artiste"]
        lt = langue_titre(x["titre"]) if self.par_titre else None
        if lt:
            self.decisions[k] = (lt in self.langues, f"titre en {NOMS_LANGUES.get(lt, lt)}")
            return
        vues = langue_artiste.get(normaliser(artiste), set())
        if len(vues) == 1 and not self.pays.connu(artiste):
            lg = next(iter(vues))
            self.decisions[k] = (lg in self.langues, f"artiste qui chante en {NOMS_LANGUES.get(lg, lg)}")
            return
        if not self.pays.connu(artiste) and self.verifies < PAYS_MAX and self.echecs < 3:
            if not self.verifies:
                afficher("  Vérification de la langue des artistes sur MusicBrainz (une par seconde, "
                         "mémorisée ensuite)...")
            self.verifies += 1
            if self.verifies % 25 == 0:
                afficher(f"    {self.verifies} artistes vérifiés")
            if self.pays(artiste) is None and not self.pays.connu(artiste):
                self.echecs += 1
                if self.echecs == 3:
                    afficher("  MusicBrainz ne répond pas : les titres sans langue claire ne sont gardés que "
                             "s'ils sont dans plusieurs playlists du thème.")
            else:
                self.echecs = 0
        pays = self.pays(artiste) if self.pays.connu(artiste) else None
        ls = LANGUES_PAYS.get(pays, {"en"}) if pays else set()
        if ls and (len(ls) == 1 or not ls & self.langues):
            self.decisions[k] = (bool(ls & self.langues), f"artiste du pays {pays}")
        else:                                     # pays bilingue (Canada, Suisse...) ou inconnu :                                     # rien de sûr : il faut que plusieurs playlists l'aient
            ok = presence.get(k, 0) >= 2 or k in fiables
            self.decisions[k] = (ok, "présent dans plusieurs playlists du thème" if ok
                                 else "langue incertaine, une seule playlist")

    def ecartes(self, pool):
        return [(p, self.decisions[p["cle"]][1]) for p in pool
                if p["cle"] in self.decisions and not self.decisions[p["cle"]][0]]


def variante_theme(theme):
    """« chanson française » <-> « chansons françaises » : d'autres playlists, plus confidentielles."""
    mots = theme.split()
    pluriel = any(m.lower().endswith(("s", "x")) and len(m) > 3 for m in mots)
    autres = [(m[:-1] if pluriel and m.lower().endswith("s") and len(m) > 3
               else m + "s" if not pluriel and len(m) > 3 and not m.isdigit() and normaliser(m) not in MOTS_VIDES
               else m) for m in mots]
    v = " ".join(autres)
    return v if v.lower() != theme.lower() else None


def colle_au_theme(titre, theme, langues, exiger_langue=True):
    """Avec une langue dans le thème, le nom de la playlist doit la citer (et aucune autre) ; les mots
    génériques (« chanson », « tubes »...) ne sont pas exigés : « Variété française » colle à « chanson française ».
    Playlist éditoriale Deezer : citer la langue n'est pas obligatoire (« Rap Hits » pour « rap français »)."""
    if not langues:
        return titre_colle_au_theme(titre, theme)
    citees = langues_citees(titre)
    if citees - langues or (exiger_langue and not citees & langues):
        return False
    mots_titre = [m.rstrip("s") for m in normaliser(titre).split()]
    for mot in normaliser(theme).split():
        if len(mot) < 2 or mot in MOTS_VIDES or mot in MOTS_GENERIQUES or langues_citees(mot):
            continue
        if not any(t.startswith(mot.rstrip("s")) for t in mots_titre):
            return False
    d_theme = decennies(theme)
    return not d_theme or decennies(titre) <= d_theme


def playlists_pour_theme(theme, langues=frozenset()):
    """Renvoie (éditoriales Deezer, autres playlists dont le nom colle au thème, autres, écartées).
    Recherche large (thème et sa variante, 200 résultats chacun) pour trouver aussi des playlists confidentielles."""
    vus, resultats = set(), []
    for q in filter(None, (theme, variante_theme(theme))):
        for index in (0, 100):
            for p in api(f"/search/playlist?limit=100&index={index}&q=" + urllib.parse.quote(q)).get("data", []):
                if p["id"] not in vus:
                    vus.add(p["id"])
                    resultats.append(p)
    resultats = [p for p in resultats if p.get("nb_tracks", 0) >= 15]
    ecartees = []
    if langues:                       # « Pop française et anglaise » pour « pop anglaise » : non
        ecartees = [p for p in resultats if langues_citees(p.get("title", "")) - langues]
        resultats = [p for p in resultats if p not in ecartees]
    editoriales = [p for p in resultats if est_editoriale(p)]
    if langues:                       # « Pop française » ne colle pas à « rap français »
        hors = [p for p in editoriales if not colle_au_theme(p.get("title", ""), theme, langues, False)]
        editoriales = [p for p in editoriales if p not in hors]
        ecartees += [dict(p, _raison="ne correspond pas au thème") for p in hors]
    autres = [p for p in resultats if not est_editoriale(p)]
    proches = [p for p in autres if colle_au_theme(p.get("title", ""), theme, langues)]
    return editoriales, proches, [p for p in autres if p not in proches], ecartees


def source_theme(theme, nombre, bloques, args, notes):
    afficher(f"Recherche de playlists pour « {theme} »...")
    langues = langues_citees(theme)
    exclues = {str(x) for x in (getattr(args, "exclure_playlists", None) or ())}
    editoriales, proches, autres, ecartees = playlists_pour_theme(theme, langues)
    retirees = [p for p in editoriales + proches + autres if str(p["id"]) in exclues]
    editoriales, proches, autres = ([p for p in liste if str(p["id"]) not in exclues]
                                    for liste in (editoriales, proches, autres))
    if not (editoriales or proches or autres):
        raise Erreur(f"Aucune playlist trouvée pour le thème « {theme} ».")
    if langues:
        afficher(f"  Thème en {', '.join(NOMS_LANGUES[l] for l in sorted(langues))} : les titres dans une autre "
                 "langue sont écartés, même s'il en manque.")
        if ecartees:
            afficher(f"  {len(ecartees)} playlists écartées (autre langue, ou autre style), ex. : "
                     + ", ".join(f"« {p['title']} »" for p in ecartees[:3]))
    besoin = math.ceil(nombre * MARGE)
    chargees = {}
    filtre = FiltreTheme(langues) if langues else None

    def charger(pls):
        a_faire = [pl for pl in pls if pl["id"] not in chargees]
        with ThreadPoolExecutor(PARALLELE) as ex:
            for pl, res in zip(a_faire, ex.map(
                    lambda pl: charger_playlist(pl["id"], pl["title"], not args.sans_cache), a_faire)):
                chargees[pl["id"]] = res[1]
                afficher(f"  Playlist : {pl['title']} ({pl['user']['name']}, {len(res[1])} titres)")

    # Ordre d'élargissement, pour rester dans le thème :
    #   1) playlists éditoriales Deezer, par paliers ;
    #   2) mêmes playlists, titres moins populaires (75 %, puis 100 %) ;
    #   3) ajout par paliers de playlists dont le nom correspond au thème (jusqu'à 45, même confidentielles) ;
    #   4) en dernier recours seulement (aucune des précédentes), les autres résultats.
    # Avec une langue dans le thème, chaque titre est vérifié à chaque étape : on n'élargit jamais à d'autres langues.
    tops = [args.top] + [t for t in POPULARITES if t > args.top]
    etapes = []
    base = editoriales[:PALIERS_PLAYLISTS[-1]]
    if base:
        etapes += [(editoriales[:n], args.top) for n in paliers(len(base))]
        etapes += [(base, t) for t in tops[1:]]
    ajout = proches if proches or base else autres
    if ajout is autres and autres:
        notes.append("Aucune playlist ne correspond clairement au thème : résultats de recherche "
                     "utilisés tels quels, vérifiez la fiche réponses.")
    for n in paliers(len(ajout)):
        etapes += [(base + ajout[:n], t) for t in tops]

    choix, utilisees, top, pool = [], [], args.top, []
    for utilisees, top in etapes:
        charger(utilisees)
        pool = dedoublonner([p for pl in utilisees for p in chargees[pl["id"]]])
        if filtre:
            presence, fiables = Counter(), set()
            for pl in utilisees:
                for k in {cle_titre(p["artiste"], p["titre"]) for p in chargees[pl["id"]]}:
                    presence[k] += 1
                    if est_editoriale(pl):
                        fiables.add(k)
        choix = candidats(pool, bloques, top, args.max_artiste)
        if filtre:
            choix = filtre.choisir(pool, choix, presence, fiables, besoin)
        if len(choix) >= besoin:
            break
        afficher(f"  Stock insuffisant : {len(choix)} titres disponibles pour {besoin} nécessaires "
                 f"({len(utilisees)} playlists, {top:g} % les plus populaires).")
    if top != args.top:
        notes.append(f"Filtre de popularité élargi à {top:g} % pour avoir assez de titres.")
    ajoutees = [pl["title"] for pl in utilisees if pl not in editoriales]
    if ajoutees and not editoriales:
        afficher("  Aucune playlist éditoriale Deezer pour ce thème : playlists d'utilisateurs utilisées.")
        notes.append("Aucune playlist éditoriale Deezer pour ce thème : playlists d'utilisateurs utilisées "
                     "(" + ", ".join(ajoutees) + "). Vérifiez la fiche réponses.")
    elif ajoutees:
        notes.append("Playlists non éditoriales ajoutées pour avoir assez de titres : " + ", ".join(ajoutees))
    hors = []
    if filtre:
        tous = dedoublonner([p for pl in utilisees for p in chargees.get(pl["id"], [])])
        hors = filtre.ecartes(tous)
        if hors:
            afficher(f"  {len(hors)} titres écartés car hors de la langue du thème.")
            notes.append(f"{len(hors)} titres écartés car hors de la langue du thème, ex. : "
                         + ", ".join(f"{p['artiste']} - {p['titre']} ({r})" for p, r in hors[:8]))
        if len(choix) < nombre:
            notes.append(f"Seulement {len(choix)} titres du thème trouvés : le blind test sera plus court plutôt "
                         "que de compléter avec d'autres langues.")
    # pour l'aperçu de la page web : playlists utilisées / écartées, titres écartés
    args._details = {
        "langues": [NOMS_LANGUES[l] for l in sorted(langues)],
        "playlists": [{"id": pl["id"], "titre": pl["title"], "auteur": pl.get("user", {}).get("name", ""),
                       "titres": len(chargees.get(pl["id"], [])), "editoriale": est_editoriale(pl)}
                      for pl in utilisees],
        "inutilisees": [{"id": pl["id"], "titre": pl["title"], "auteur": pl.get("user", {}).get("name", ""),
                         "editoriale": est_editoriale(pl)}
                        for pl in (editoriales + proches)[:60] if pl not in utilisees],
        "retirees": [{"id": pl["id"], "titre": pl["title"]} for pl in retirees],
        "ecartees": [{"id": pl["id"], "titre": pl["title"], "raison": pl.get("_raison", "cite une autre langue")}
                     for pl in ecartees[:30]],
        "hors_theme": [{"artiste": p["artiste"], "titre": p["titre"], "raison": r} for p, r in hors[:150]],
    }
    desc = f"thème « {theme} » ({len(utilisees)} playlists : " + ", ".join(pl["title"] for pl in utilisees) + ")"
    return choix, desc


def source_playlist(lien, nombre, bloques, args, notes):
    pid = id_playlist(lien)
    afficher(f"Lecture de la playlist {pid}...")
    titre, pistes = charger_playlist(pid, cache=not args.sans_cache)
    afficher(f"  Playlist : {titre} ({len(pistes)} titres)")
    if not pistes:
        raise Erreur("Cette playlist ne contient aucun titre écoutable.")
    pool = dedoublonner(pistes)
    besoin = math.ceil(nombre * MARGE)
    for top in [args.top] + [t for t in POPULARITES if t > args.top]:
        choix = candidats(pool, bloques, top, args.max_artiste)
        if len(choix) >= besoin:
            break
        afficher(f"  Stock insuffisant : {len(choix)} titres disponibles pour {besoin} nécessaires "
                 f"({top:g} % les plus populaires).")
    if top != args.top:
        notes.append(f"Filtre de popularité élargi à {top:g} % pour avoir assez de titres.")
    return choix, f"playlist « {titre} » ({pid})"


def lire_liste(chemin):
    brut = Path(chemin).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            texte = brut.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        texte = brut.decode("latin-1")
    return [l.strip() for l in texte.splitlines() if l.strip() and not l.strip().startswith("#")]


def chercher_titre(ligne):
    """Mode liste : meilleur résultat Deezer pour 'Artiste - Titre'."""
    if " - " in ligne:
        artiste, titre = [s.strip() for s in ligne.split(" - ", 1)]
        requetes = [f'artist:"{artiste}" track:"{titre}"', f"{artiste} {titre}"]
        voulu_a = normaliser(artiste)
    else:
        requetes, voulu_a, titre = [ligne], None, None
    secours = None
    for q in requetes:
        bons = []
        for p in api("/search?q=" + urllib.parse.quote(q)).get("data", []):
            if not p.get("preview") or not p.get("readable", True):
                continue
            d = piste_vers_dict(p)
            a = normaliser(d["artiste"])
            if voulu_a is None or voulu_a in a or a in voulu_a:
                bons.append(d)
            secours = secours or d
        if bons:
            if titre is None:
                return bons[0]
            # titre exact d'abord (évite remix / feat.), puis même titre hors version, puis popularité
            return max(bons, key=lambda d: (normaliser(d["titre"]) == normaliser(titre),
                                            normaliser_titre(d["titre"]) == normaliser_titre(titre),
                                            d["rank"]))
    return secours


def source_liste(chemin, nombre, bloques, args, notes):
    chemin = Path(str(chemin).strip().strip('"'))
    if not chemin.is_absolute():
        chemin = BASE / chemin
    if not chemin.exists():
        raise Erreur(f"Fichier liste introuvable : {chemin}")
    lignes = lire_liste(chemin)
    if not lignes:
        raise Erreur(f"Le fichier {chemin.name} ne contient aucun titre.")
    return source_lignes(lignes, f"liste « {chemin.name} »", nombre, bloques, args, notes)


def source_lignes(lignes, libelle, nombre, bloques, args, notes, titres_source=None):
    """Titres donnés un par un (fichier liste ou playlist Spotify), cherchés sur Deezer."""
    titres_source = titres_source or [None] * len(lignes)
    # « Artiste - Titre | indication | famille » : l'indication (ex. la langue) s'affiche sur la carte
    # réponse ; deux titres de la même famille (ex. langues romanes) ne se suivront jamais
    parts = [[x.strip() for x in l.split(" | ")] for l in lignes]
    indications = [p[1] if len(p) > 1 else None for p in parts]
    familles = [p[2] if len(p) > 2 else None for p in parts]
    lignes = [p[0] for p in parts]
    afficher(f"Recherche de {len(lignes)} titres sur Deezer...")
    with ThreadPoolExecutor(PARALLELE) as ex:
        resultats = list(ex.map(chercher_titre, lignes))
    choix, vues = [], set()
    for ligne, res, indication, famille, titre_source in zip(lignes, resultats, indications, familles,
                                                             titres_source):
        if not res:
            afficher(f"  INTROUVABLE : {ligne}")
            notes.append(f"Introuvable sur Deezer : {ligne}")
            continue
        k = cle_titre(res["artiste"], res["titre"])
        if k in vues:
            notes.append(f"En double dans la liste, ignoré : {ligne}")
            continue
        vues.add(k)
        if k in bloques and getattr(args, "sans_repetition", False):
            ecartes = getattr(args, "_ecartes", 0) + 1
            args._ecartes = ecartes
            continue
        if k in bloques:
            afficher(f"  Déjà utilisé récemment (gardé) : {ligne}")
            notes.append(f"Déjà utilisé lors d'un des derniers lancements (gardé) : {ligne}")
        trouve = f"{res['artiste']} - {res['titre']}"
        if " - " in ligne:
            voulu_a = normaliser(ligne.split(" - ", 1)[0])
            a = normaliser(res["artiste"])
            if not (voulu_a in a or a in voulu_a):
                # Deezer n'a pas l'artiste demandé : on a pris la version d'un autre (reprise ?), alors que
                # la fiche réponses annoncera l'artiste de votre liste
                afficher(f"  AUTRE ARTISTE : « {ligne} » -> trouvé « {trouve} »")
                notes.append(f"À vérifier, autre artiste (peut-être une reprise) : « {ligne} » -> trouvé « {trouve} »")
            elif normaliser_titre(ligne.split(" - ", 1)[1]) != normaliser_titre(res["titre"]):
                notes.append(f"À vérifier : « {ligne} » -> trouvé « {trouve} »")
        # la fiche réponses reprend votre ligne telle quelle (si elle est au format « Artiste - Titre »)
        choix.append(dict(res, cle=k, reponse=ligne if " - " in ligne else trouve, note=indication,
                          famille=famille, titre_source=titre_source))
    if getattr(args, "_ecartes", 0):
        afficher(f"  {args._ecartes} titres déjà utilisés lors des derniers lancements : écartés.")
        notes.append(f"{args._ecartes} titres déjà utilisés lors des {args.blocage} derniers lancements "
                     "ont été écartés (--sans-repetition).")
    # 5) mélange facultatif
    if args.melanger:
        random.shuffle(choix)
    if len(lignes) > nombre:
        quoi = "tirés au sort" if args.melanger else "premiers titres valides"
        notes.append(f"La liste contient {len(lignes)} lignes : {nombre} {quoi} sont utilisés, "
                     "les autres servent de remplaçants.")
    return choix, libelle


# ---------- Playlist Spotify (lecture de la page publique, sans clé) ----------
# Spotify ne donne plus d'extraits : on lit seulement la liste des titres, puis on les cherche sur Deezer.
# Méthode non officielle : si Spotify change sa page, cette partie devra être adaptée.

MOTS_SUFFIXE = re.compile(r"\b(from|remaster\w*|live|extrait|version|soundtrack|radio edit|edit|mono|"
                          r"stereo|original|motion picture|cast|reprise de|mix|remix|full)\b", re.I)


def nettoyer_titre_spotify(titre):
    """« El Tango De Roxanne - From "Moulin Rouge" Soundtrack » -> « El Tango De Roxanne »."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", lambda m: "" if MOTS_SUFFIXE.search(m.group(0)) else m.group(0), titre)
    parties = re.split(r"\s+[-–—]\s+", t)
    while len(parties) > 1 and MOTS_SUFFIXE.search(parties[-1]):
        parties.pop()
    return " - ".join(parties).strip() or titre


SPOTIFY_MAX = 500          # au-delà, la lecture serait longue (une page Spotify par titre manquant)


def page_spotify(url):
    """Page « embed » publique de Spotify : renvoie (données de la page, jeton anonyme de la page)."""
    for tentative in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                page = r.read().decode("utf-8", "replace")
            break
        except Exception as e:
            derniere = e
            time.sleep(2 ** tentative)
    else:
        raise Erreur(f"Spotify ne répond pas ({derniere}).")
    donnees = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    jeton = re.search(r'"accessToken":"([^"]+)"', page)
    try:
        entite = json.loads(donnees.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise Erreur("Impossible de lire cette page Spotify (playlist privée, ou page Spotify modifiée).")
    return entite, jeton.group(1) if jeton else None


def titre_spotify(uri):
    """Titre et artistes d'un morceau, lus sur sa page « embed » (même format que la liste de la playlist)."""
    verifier_annulation()
    entite, _ = page_spotify(f"https://open.spotify.com/embed/track/{uri.split(':')[-1]}")
    return {"uri": uri, "title": entite.get("name") or entite.get("title"),
            "subtitle": ", ".join(a.get("name", "") for a in entite.get("artists") or [])}


def suite_spotify(pid, jeton, pistes):
    """La page publique ne donne que les 100 premiers titres. La liste complète (identifiants seulement) vient du
    service de playlists de Spotify, avec le jeton anonyme de la page ; on lit ensuite la page de chaque titre manquant.
    Méthode non officielle : en cas d'échec, on garde les 100 premiers. Renvoie (pistes, message éventuel)."""
    if len(pistes) < 100 or not jeton:
        return pistes, None
    try:
        req = urllib.request.Request(f"https://spclient.wg.spotify.com/playlist/v2/playlist/{pid}",
                                     headers={**UA, "Authorization": f"Bearer {jeton}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            uris = [i["uri"] for i in json.load(r)["contents"]["items"] if i.get("uri", "").startswith("spotify:track:")]
    except Exception as e:
        return pistes, f"Seuls les 100 premiers titres de la playlist Spotify ont pu être lus ({e})."
    connus = {p.get("uri"): p for p in pistes}
    message = None
    if len(uris) > SPOTIFY_MAX:
        message = f"Playlist Spotify de {len(uris)} titres : seuls les {SPOTIFY_MAX} premiers sont utilisés."
        uris = uris[:SPOTIFY_MAX]
    manquants = [u for u in uris if u not in connus]
    if not manquants:
        return pistes, message
    afficher(f"  Playlist de plus de 100 titres : lecture des {len(manquants)} titres suivants...")
    lus = {}

    def un(uri):
        try:
            lus[uri] = titre_spotify(uri)
        except Annule:
            raise
        except Erreur:
            pass

    with ThreadPoolExecutor(PARALLELE) as ex:
        list(ex.map(un, manquants))
    if len(lus) < len(manquants):
        message = (message + " " if message else "") + \
            f"{len(manquants) - len(lus)} titres de la playlist Spotify n'ont pas pu être lus."
    return [connus.get(u) or lus[u] for u in uris if u in connus or u in lus], message


def lire_spotify(lien, notes=None):
    lien = lien.strip().strip('"')
    if "spotify.link" in lien or "spotify.app.link" in lien:
        try:
            with urllib.request.urlopen(urllib.request.Request(lien, headers=UA), timeout=20) as r:
                lien = r.geturl() + " " + r.read(200_000).decode("utf-8", "replace")
        except Exception as e:
            raise Erreur(f"Impossible d'ouvrir le lien Spotify : {e}")
    m = re.search(r"playlist[/:]([A-Za-z0-9]{10,})", lien)
    if not m:
        raise Erreur("Lien Spotify non reconnu. Exemple : https://open.spotify.com/playlist/1eulR6Pz29spTdeXqvI4cr")
    entite, jeton = page_spotify(f"https://open.spotify.com/embed/playlist/{m.group(1)}")
    if not isinstance(entite.get("trackList"), list):
        raise Erreur("Impossible de lire cette playlist Spotify (privée, ou page Spotify modifiée).")
    pistes, message = suite_spotify(m.group(1), jeton, entite["trackList"])
    if message:
        afficher("  " + message)
        if notes is not None:
            notes.append(message)
    return entite.get("name", "Spotify"), pistes


def source_spotify(lien, nombre, bloques, args, notes):
    afficher("Lecture de la playlist Spotify...")
    nom, pistes = lire_spotify(lien, notes)
    afficher(f"  Playlist Spotify : {nom} ({len(pistes)} titres)")
    lignes, titres_source = [], []
    for p in pistes:
        artiste = re.split(r",\s*", p.get("subtitle") or "")[0].strip()   # virgule + espace spéciale
        titre = nettoyer_titre_spotify(p.get("title") or "")
        if artiste and titre:
            lignes.append(f"{artiste} - {titre}")
            titres_source.append(p.get("title"))
    if not lignes:
        raise Erreur("Cette playlist Spotify est vide.")
    return source_lignes(lignes, f"playlist Spotify « {nom} »", nombre, bloques, args, notes, titres_source)


# ---------- Traitement audio ----------

def verifier_outils():
    manquants = []
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    for outil in ("ffmpeg", "ffprobe"):
        local = BASE / (outil + ".exe")
        chemin = str(local) if local.exists() else shutil.which(outil)
        if not chemin:          # installé à l'instant par winget : Windows ne le « voit » qu'après redémarrage
            trouve = [winget / "Links" / f"{outil}.exe"] + sorted(winget.glob(f"Packages/Gyan.FFmpeg*/**/bin/{outil}.exe"))
            chemin = next((str(f) for f in trouve if f.is_file()), None)
        if not chemin:
            manquants.append(outil)
        OUTILS[outil] = chemin
    if manquants:
        raise Erreur(f"Introuvable : {', '.join(manquants)}.\n"
                     "Installez ffmpeg (par exemple : winget install Gyan.FFmpeg), fermez puis "
                     "rouvrez la fenêtre, ou copiez ffmpeg.exe et ffprobe.exe à côté du script.")


OUTILS = {}


def duree_audio(fichier):
    r = subprocess.run([OUTILS["ffprobe"], "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(fichier)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def decouper(source, cible, debut, eff, duree):
    """Découpe `eff` secondes, puis complète par du silence jusqu'à `duree` exactement :
    tous les extraits d'un format ont la même durée que les clips du modèle CapCut."""
    fin_fondu = max(eff - FONDU, 0)
    filtre = (f"afade=t=in:st=0:d={FONDU},afade=t=out:st={fin_fondu:.3f}:d={FONDU},"
              f"loudnorm=I=-14:TP=-1:LRA=11,aresample=44100,apad=whole_dur={duree},atrim=0:{duree}")
    r = subprocess.run(
        [OUTILS["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{debut:.3f}", "-t", f"{eff:.3f}", "-i", str(source),
         "-map", "0:a:0", "-map_metadata", "-1", "-af", filtre,
         "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k", str(cible)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise Erreur("ffmpeg : " + (r.stderr.strip().splitlines() or ["erreur inconnue"])[-1])


def traiter(cand, cible, tmp, debut, duree):
    """Télécharge et découpe un extrait. Renvoie None si OK, sinon la raison du rejet."""
    brut = Path(tmp) / (cible.stem + "_brut.mp3")
    try:
        url = cand.get("preview")
        if cand.get("id") and (not url or lien_expire(url)):
            url = api(f"/track/{cand['id']}").get("preview")
        if not url or not telecharger(url, brut):
            if not cand.get("id"):
                return "lien d'extrait refusé"
            url = api(f"/track/{cand['id']}").get("preview")     # lien expiré : on le redemande
            if not url or not telecharger(url, brut):
                return "extrait indisponible"
        dispo = duree_audio(brut) - debut
        if dispo < duree - TOLERANCE:
            return f"extrait trop court ({max(dispo, 0):.1f} s disponibles)"
        eff = min(duree, dispo)
        decouper(brut, cible, debut, eff, duree)
        obtenu = duree_audio(cible)
        if obtenu < duree - TOLERANCE:
            return f"extrait trop court après découpe ({obtenu:.1f} s)"
        return None
    except Introuvable:
        return "titre retiré de Deezer"
    except Erreur as e:
        return str(e)
    except Exception as e:  # on ne veut jamais bloquer tout le lot pour un morceau
        return f"erreur inattendue ({e})"
    finally:
        try:
            brut.unlink()
        except OSError:
            pass


def produire(cands, nombre, dossier_lot, debut, duree, notes):
    """Traite les candidats par vagues parallèles jusqu'à obtenir `nombre` extraits."""
    ok, suivant = [], 0
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(PARALLELE) as ex:
        while len(ok) < nombre and suivant < len(cands):
            vague = list(range(suivant, min(len(cands), suivant + nombre - len(ok))))
            suivant = vague[-1] + 1
            futurs = {i: ex.submit(traiter, cands[i], dossier_lot / f"c{i:04d}.mp3", tmp, debut, duree)
                      for i in vague}
            for i in vague:
                raison = futurs[i].result()
                verifier_annulation()
                c = cands[i]
                if raison is None:
                    ok.append(i)
                    afficher(f"  [{len(ok):02d}/{nombre}] OK")
                else:
                    afficher(f"  Rejeté : {c['artiste']} - {c['titre']} ({raison})")
                    notes.append(f"Rejeté et remplacé : {c['artiste']} - {c['titre']} ({raison})")
    ok.sort()
    retenus = []
    for n, i in enumerate(ok, 1):
        os.replace(dossier_lot / f"c{i:04d}.mp3", dossier_lot / f"{n:02d}.mp3")
        retenus.append(cands[i])
    return retenus


# ---------- Remplacement sûr des anciens fichiers ----------

def est_fichier_lot(nom):
    # toute vidéo du dossier d'extraits appartient au lot : une ancienne vidéo ne doit jamais survivre
    # à côté d'extraits qui ne lui correspondent plus
    return (bool(re.fullmatch(r"\d+\.mp3", nom))
            or nom in ("reponses.txt", "problemes.txt", "lot.json", "quiz.json", "propositions_a_verifier.txt",
                       "atelier.json")
            or nom.lower().endswith(".mp4"))


CHAMPS_LOT = ("id", "artiste", "titre", "reponse", "note", "famille", "oeuvre", "interprete", "titre_court")


def ordonner_sans_voisins(items, cle, essais=500):
    """Ordre (aléatoire) où deux éléments consécutifs n'ont jamais la même clé, ou None si impossible."""
    for _ in range(essais):
        reste, res, prec = list(items), [], None
        random.shuffle(reste)
        while reste:
            compte = Counter(cle(x) for x in reste)
            possibles = [x for x in reste if cle(x) != prec]
            if not possibles:
                break
            limite = (len(reste) + 1) // 2        # une famille trop nombreuse doit passer en priorité
            urgents = [x for x in possibles if compte[cle(x)] >= limite]
            x = random.choice(urgents or possibles)
            reste.remove(x)
            res.append(x)
            prec = cle(x)
        if not reste:
            return res
    return None


def alterner_familles(retenus, dossier, notes):
    """Réordonne les extraits déjà découpés (01.mp3...) pour séparer les familles, puis renumérote."""
    cle = lambda c: normaliser(c.get("famille") or "")
    if all(cle(a) != cle(b) for a, b in zip(retenus, retenus[1:])):
        return retenus
    ordre = ordonner_sans_voisins(list(range(len(retenus))), lambda i: cle(retenus[i]))
    if ordre is None:
        notes.append("Impossible d'éviter deux titres de la même famille à la suite : une famille est trop "
                      "représentée dans la liste.")
        return retenus
    for i in range(len(retenus)):
        os.replace(dossier / f"{i + 1:02d}.mp3", dossier / f"ancien_{i + 1:02d}.mp3")
    for n, i in enumerate(ordre, 1):
        os.replace(dossier / f"ancien_{i + 1:02d}.mp3", dossier / f"{n:02d}.mp3")
    afficher("  Ordre ajusté : jamais deux titres de la même famille à la suite.")
    return [retenus[i] for i in ordre]


def ecrire_lot(retenus, dossier):
    """lot.json : les réponses complètes, pour pouvoir refaire la vidéo plus tard sans nouveau tirage."""
    donnees = [{k: c.get(k) for k in CHAMPS_LOT if c.get(k) is not None} for c in retenus]
    tmp = dossier / "lot.json.tmp"
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, dossier / "lot.json")


def nom_video(nom):
    """Nom de fichier sûr pour Windows : 'blindtest musicals' -> 'blindtest musicals.mp4'."""
    nom = re.sub(r'[<>:"/\\|?*]', "", (nom or "").strip()).strip(" .") or "blindtest"
    return nom + ".mp4"


DOSSIER_ARCHIVES = BASE / "archives"


def nom_archive(dossier, dest):
    """Nom du dossier d'archive d'un ancien lot : celui de sa vidéo, sinon le format et la date du lot."""
    video = next(dossier.glob("*.mp4"), None)
    if video:
        nom = video.stem
    else:
        try:
            m = re.search(r"Blind test du (\d+)/(\d+)/(\d+) (\d+):(\d+)",
                          (dossier / "reponses.txt").read_text(encoding="utf-8"))
        except OSError:
            m = None
        nom = f"{dest.name} du {m[1]}-{m[2]}-{m[3]} {m[4]}h{m[5]}" if m else f"{dest.name} (ancien)"
    nom, n = re.sub(r'[<>:"/\\|?*]', "", nom).strip(" .") or "ancien lot", 2
    cible = DOSSIER_ARCHIVES / nom
    while cible.exists():
        cible = DOSSIER_ARCHIVES / f"{nom} ({n})"
        n += 1
    return cible


def installer(dossier_lot, dest, archiver=False):
    """Remplace l'ancien lot par le nouveau, sans rien casser si un fichier est verrouillé.
    Avec `archiver`, l'ancien lot (extraits, réponses, vidéo, jeu) est rangé dans archives au lieu d'être effacé.
    Renvoie le dossier d'archive, ou None."""
    dest.mkdir(exist_ok=True)
    ancien = DOSSIER_TRAVAIL / f"ancien_{dest.name}"
    shutil.rmtree(ancien, ignore_errors=True)
    ancien.mkdir(parents=True)
    anciens = [f for f in dest.iterdir() if f.is_file() and est_fichier_lot(f.name)]
    nouveaux = sorted(dossier_lot.iterdir())
    deplaces, installes = [], []
    try:
        for f in anciens:
            os.replace(f, ancien / f.name)
            deplaces.append(f.name)
        for f in nouveaux:
            os.replace(f, dest / f.name)
            installes.append(f.name)
    except OSError as e:
        # retour à l'état initial
        for nom in installes:
            try:
                os.replace(dest / nom, dossier_lot / nom)
            except OSError:
                pass
        for nom in deplaces:
            try:
                os.replace(ancien / nom, dest / nom)
            except OSError:
                pass
        try:
            ancien.rmdir()          # seulement s'il est vide (tout a été remis en place)
        except OSError:
            pass
        nom = Path(getattr(e, "filename", "") or "").name or "un fichier"
        raise FichierVerrouille(
            f"Impossible de remplacer {nom} dans {dest.name} : le fichier est utilisé par un autre "
            "programme (logiciel de montage ouvert ?).\nLes anciens fichiers n'ont pas été modifiés. "
            "Fermez le projet de montage (ou le lecteur audio) puis réessayez.")
    if archiver and any(re.fullmatch(r"\d+\.mp3", f.name) for f in ancien.iterdir()):
        cible = nom_archive(ancien, dest)
        try:
            DOSSIER_ARCHIVES.mkdir(exist_ok=True)
            os.replace(ancien, cible)            # même disque : instantané, même pour une grosse vidéo
            return cible
        except OSError:
            afficher(f"Attention : l'ancien lot n'a pas pu être rangé dans archives (il reste dans {ancien}).")
            return None
    shutil.rmtree(ancien, ignore_errors=True)
    return None


# ---------- Œuvres : film, série, comédie musicale d'où vient le morceau ----------
# Pour ces thèmes, le champ « artiste » de Deezer est peu fiable (troupe, personnage, spectacle,
# chef d'orchestre...). L'œuvre se lit dans l'album, mais seulement quand l'album est bien une
# bande originale : sinon on garderait le nom de l'album d'un chanteur en le faisant passer pour un film.

GENRES_BO = {173, 174, 175, 176}          # Films/Jeux vidéo, Musiques de films, Comédies musicales, B.O.
THEMES_OEUVRE = re.compile(
    r"\b(films?|cinema|series?|musicals?|comedies? musicales?|broadway|west end|disney|pixar|"
    r"bandes? originales?|b o|ost|soundtracks?|dessins? animes?|animes?|jeux? videos?|generiques?|"
    r"opera rock|spectacles?)\b")
PREUVES_BO = re.compile(
    r"(soundtrack|motion picture|cast recording|original cast|broadway|west end|bande originale|"
    r"musique originale|musique du film|music from|music for the|original score|\bscore\b|musical|"
    r"com[ée]die musicale|spectacle|op[ée]ra (rock|urbain))", re.I)
MOTS_BO = re.compile(
    r"\b(cast|recording|soundtrack|motion picture|original|version|edition|deluxe|live|musical|musique|"
    r"music from|music for|bande originale|b\.?o\.?|ost|score|remaster\w*|int[ée]grale|anniversary|studio|"
    r"broadway|west end|company|from the|film|movie|s[ée]rie|series|highlights|selections|spectacle|"
    r"com[ée]die musicale|conte musical|op[ée]ra rock|standard|concept album|official)\b", re.I)
COMPILATIONS = re.compile(
    r"(^l'?int[ée]grale$|\b(best of|greatest|hits|compilation|mixtape|essentials?|the very best|anthology|"
    r"collection|tubes|plus grand(e)?s|meilleur(e)?s|giganten|number ones|top \d+)\b)", re.I)
COLLECTIFS = re.compile(r"\b(cast|ensemble|company|orchestra|orchestre|troupe|original|chorus|choeurs?|"
                        r"various|artistes divers|band|disney|records)\b", re.I)


def oeuvre_citee(texte):
    """« Granted (From "High School Musical: The Series") » -> « High School Musical: The Series »."""
    m = re.search(r"\b(?:from|extrait\s+d[eu]\s*(?:spectacle|film)?)\s+(?:the\s+)?(?:[\w'-]+\s+){0,3}?"
                  r"[\"“«]\s*([^\"”»]+?)\s*[\"”»]", texte or "", re.I)
    if not m:   # sans guillemets : « - from the series Arcane League of Legends »
        m = re.search(r"\bfrom the (?:series|tv series|film|movie|musical|game|video game)\s+([^()\[\]]+?)\s*$",
                      texte or "", re.I)
    return m.group(1).strip() if m else None


def nettoyer_oeuvre(album):
    """« Hamilton (Original Broadway Cast Recording) » -> « Hamilton »."""
    t, avant = album or "", None
    while t != avant:        # parenthèses imbriquées : on retire d'abord les plus intérieures
        avant = t
        t = re.sub(r"\s*[\(\[][^\(\)\[\]]*[\)\]]",
                   lambda m: "" if MOTS_BO.search(m.group(0))
                   or re.fullmatch(r"\s*[\(\[][\d\s&,/–-]*\d{4}[\d\s&,/–-]*[\)\]]", m.group(0))   # (1936 & 1939)
                   else m.group(0), t)
    t = re.sub(r"^(original\s+)?(motion picture\s+)?soundtrack\s+", "", t, flags=re.I)
    parties = re.split(r"\s+[-–—]\s+|:\s+", t)
    while len(parties) > 1 and MOTS_BO.search(parties[-1]):
        parties.pop()
    t = ": ".join(parties)
    t = re.sub(r"\s+(the\s+)?(movie\s+|film\s+|original\s+)?soundtrack$", "", t, flags=re.I)
    t = re.sub(r"[:,]?\s*\b(season|saison|vol\.?|volume)\s*\d+.*$", "", t, flags=re.I).strip(" -:,")
    # « Notre Dame de Paris 1998 » -> « Notre Dame de Paris » (année de la version) ; mais « Blade Runner 2049 »
    # reste intact : on ne retire l'année que derrière un nom d'au moins 3 mots
    m = re.fullmatch(r"(.+?)\s+(?:19|20)\d{2}", t)
    if m and len(m.group(1).split()) >= 3:
        t = m.group(1)
    return None if not t or COMPILATIONS.search(album or "") else t


def mots_oeuvre(nom):
    return set(normaliser(nom).split()) - {"the", "le", "la", "les", "l", "de", "du", "des", "of", "a", "an", "live"}


MOTS_SUITE = {"ii", "iii", "iv", "again", "returns", "return", "retour", "reloaded", "part", "chapter", "chapitre",
              "sequel", "encore", "revolutions", "resurrection", "forever", "2", "3", "4", "5"}


def sous_titre_de(court, nom):
    """Vrai si `nom` est `court` suivi d'un sous-titre (« Le Roi Soleil: De Versailles à Monaco »,
    « Les Misérables - Live ») ou de « Live » seul ; faux pour un autre titre (« Annie Get Your Gun »)."""
    n_court, n_nom = normaliser(court), normaliser(nom)
    if not n_nom.startswith(n_court + " "):
        return False
    if set(n_nom[len(n_court):].split()) <= {"live"}:
        return True
    return any(normaliser(nom[:m.start()]) == n_court for m in re.finditer(r"\s*(?::|\s[-–—]\s|\()", nom))


def unifier_oeuvres(retenus):
    """Un seul nom par œuvre dans une partie : « Le Roi Soleil: De Versailles à Monaco » et « Le Roi Soleil »
    -> « Le Roi Soleil » ; « Les Misérables Live! » -> « Les Misérables ». Une suite reste à part
    (« Frozen 2 » n'est pas « Frozen », « Annie Get Your Gun » n'est pas « Annie »)."""
    noms = sorted({c["oeuvre"] for c in retenus if c.get("oeuvre")}, key=lambda n: (len(mots_oeuvre(n)), len(n)))
    remplace = {}
    for i, nom in enumerate(noms):
        m = mots_oeuvre(nom)
        for court in noms[:i]:                           # seulement vers un nom plus court
            mc = mots_oeuvre(court)
            if not mc or not mc <= m or not sous_titre_de(court, nom):
                continue
            if (m - mc) & MOTS_SUITE or any(x.isdigit() and len(x) < 4 for x in m - mc):   # suite : autre œuvre
                continue
            remplace[nom] = remplace.get(court, court)
            break
    for c in retenus:
        if c.get("oeuvre") in remplace:
            c["oeuvre"] = remplace[c["oeuvre"]]


def est_collectif(nom, oeuvre=None):
    n = normaliser(nom)
    if COLLECTIFS.search(n):
        return True
    o = normaliser(oeuvre or "")
    return bool(o) and (n == o or (len(n) > 3 and (n in o or o in n)))


def album_de_troupe(id_album):
    """Album de spectacle ou de film : ses chansons sont créditées à au moins 3 chanteurs différents
    (sur l'album d'un artiste, tout est à son nom, même avec des invités)."""
    pistes = api(f"/album/{id_album}/tracks?limit=100").get("data", [])
    return len({normaliser(p.get("artist", {}).get("name", "")) for p in pistes}) >= 3


def titre_sans_oeuvre(titre, oeuvre):
    """« West Side Story: Act I: America » -> « America » ; retire aussi « (From "...") »."""
    t = re.sub(r"\s*[\(\[]\s*from\b[^\)\]]*[\)\]]", "", titre, flags=re.I).strip()
    parties = re.split(r"\s*:\s+", t)
    if len(parties) > 1:
        tete, o = normaliser(parties[0]), normaliser(oeuvre)
        if tete and (tete in o or o in tete):
            t = parties[-1]
    if len(t) > 40:     # « An Irish Party in Third Class (includes "John Ryan's Polka"...) »
        t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t).strip() or t
    return t or titre


def identifier_oeuvre(c, albums):
    c["oeuvre"], c["interprete"], c["titre_court"] = None, None, c["titre"]
    if not c.get("id"):
        return
    piste = api(f"/track/{c['id']}")
    id_album = piste.get("album", {}).get("id")
    if id_album not in albums:
        albums[id_album] = api(f"/album/{id_album}")
    album = albums[id_album]
    titre_album = album.get("title", "")
    genres = {g.get("id") for g in album.get("genres", {}).get("data", [])}
    artiste_album = album.get("artist", {}).get("name", "")
    divers = normaliser(artiste_album) in ("various artists", "artistes divers", "multi interpretes")

    oeuvre = (oeuvre_citee(piste.get("title", "")) or oeuvre_citee(titre_album)
              or oeuvre_citee(c.get("titre_source")))       # ex. titre Spotify « - From "Cinderella" »
    if not oeuvre:
        propre = nettoyer_oeuvre(titre_album)
        if propre and normaliser_titre(propre) == normaliser_titre(c["titre"]):
            pass           # single du même nom que la chanson : ce n'est pas l'œuvre
        elif propre is None or album.get("record_type") == "compile":
            # compilation ou « L'Intégrale » : l'artiste peut être le spectacle lui-même (Mozart Opera Rock)
            if genres & GENRES_BO and artiste_album and not divers:
                oeuvre = artiste_album
        elif (genres & GENRES_BO or PREUVES_BO.search(titre_album) or est_collectif(artiste_album)
              or divers or normaliser(artiste_album) == normaliser(propre)
              or album.get("record_type") == "album" and album_de_troupe(id_album)):
            oeuvre = propre
    if not oeuvre:
        return
    c["oeuvre"] = oeuvre
    c["titre_court"] = titre_sans_oeuvre(c["titre"], oeuvre)
    noms = [c["artiste"]] + [x.get("name", "") for x in piste.get("contributors", [])]
    c["interprete"] = next((n for n in noms if n and not est_collectif(n, oeuvre)
                            and normaliser(n) not in COMPOSITEURS), None)


def enrichir_oeuvres(retenus, notes):
    afficher("\nRecherche des œuvres (film, série, spectacle) sur Deezer...")
    albums = {}

    def un(c):
        try:
            identifier_oeuvre(c, albums)
        except (Erreur, Introuvable):
            c["oeuvre"], c["interprete"], c["titre_court"] = None, None, c["titre"]

    with ThreadPoolExecutor(PARALLELE) as ex:
        list(ex.map(un, retenus))
    unifier_oeuvres(retenus)
    trouves = sum(1 for c in retenus if c.get("oeuvre"))
    afficher(f"  Œuvre identifiée pour {trouves} titres sur {len(retenus)}.")
    for n, c in enumerate(retenus, 1):
        if not c.get("oeuvre"):
            notes.append(f"Œuvre non identifiée pour la réponse {n:02d} (affichée « {texte_reponse(c)} ») : "
                         "à vérifier.")


def texte_reponse(c):
    if c.get("oeuvre"):
        return f"{c['oeuvre']} — « {c['titre_court']} »" + (f" ({c['interprete']})" if c.get("interprete") else "")
    base = c.get("reponse") or f"{c['artiste']} - {c['titre']}"
    return base + (f" ({c['note']})" if c.get("note") else "")


def oeuvres_par_defaut(args, source):
    if args.oeuvre is not None:
        return args.oeuvre
    # le thème peut se lire dans la source ou dans le nom choisi pour la vidéo (« blindtest comedies musicales »)
    return not (args.liste or getattr(args, "lignes", None)) and bool(THEMES_OEUVRE.search(normaliser(f"{source} {getattr(args, 'nom', '')}")))


# ---------- Vidéo YouTube complète (extrait + décompte, puis carte réponse) ----------

VIDEO_L, VIDEO_H, VIDEO_FPS = 2560, 1440, 30   # 1440p : YouTube la diffuse avec un meilleur codec, même en 1080p
# Encodage final : qualité élevée, sans paliers de couleur dans les zones sombres (aq-mode=3)
ENCODAGE = ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-x264-params", "aq-mode=3",
            "-profile:v", "high", "-pix_fmt", "yuv420p"]
DUREE_REPONSE = 5
CYAN, VIOLET, BLANC = "0x46D2FF", "0xC7A2FF", "0xFFFFFF"
DOSSIER_POLICES = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
EXT_IMAGES = (".png", ".jpg", ".jpeg", ".webp")


def px(v):
    """Mise en page pensée pour du 1080p : s'adapte à la résolution choisie."""
    return int(round(v * VIDEO_H / 1080))


def pour_filtre(chemin):
    """Chemin utilisable à l'intérieur d'un filtre ffmpeg (C: -> C\\:)."""
    return "'" + str(chemin).replace("\\", "/").replace(":", "\\:").replace("'", "") + "'"


def police(nom):
    for n in (nom, "arialbd.ttf", "arial.ttf"):
        if (DOSSIER_POLICES / n).exists():
            return pour_filtre(DOSSIER_POLICES / n)
    raise Erreur("Aucune police trouvée dans " + str(DOSSIER_POLICES))


EXT_VIDEOS = (".mp4", ".mov", ".webm")


def cite_duree(fichier, duree):
    """'countdown 20.mp4' cite 20 ; 'countdown(2).mp4' ne cite ni 10, ni 20, ni 30."""
    return re.search(rf"(?<!\d){duree}(?!\d)", fichier.stem) is not None


def trouver_fichier(noms_preferes, extensions, exclure=(), garder=lambda f: True):
    """fond.png / decompte.mp4 en priorité, sinon le seul fichier du bon type dans le dossier."""
    for nom in noms_preferes:
        for ext in extensions:
            if (BASE / (nom + ext)).exists() and garder(BASE / (nom + ext)):
                return BASE / (nom + ext)
    trouves = [f for f in BASE.iterdir() if f.is_file() and f.suffix.lower() in extensions and garder(f)
               and not any(f.name.lower().startswith(e) for e in exclure)]
    if len(trouves) == 1:
        return trouves[0]
    mots = [sans_accents(n).lower() for n in noms_preferes]
    avec_mot = [f for f in trouves if any(m in sans_accents(f.name).lower() for m in mots)]
    if len(avec_mot) == 1:
        return avec_mot[0]
    return trouves or None     # plusieurs candidats : c'est ambigu, l'appelant le signale


def fichiers_video(duree):
    """Renvoie (fond, décompte, message d'erreur éventuel).
    Décompte : celui qui cite la durée du format (« countdown 20 ») s'il existe, sinon le décompte général."""
    fond = trouver_fichier(["fond", "background"], EXT_IMAGES)
    dedies = [f for f in BASE.iterdir() if f.is_file() and f.suffix.lower() in EXT_VIDEOS
              and not f.name.lower().startswith("blindtest") and cite_duree(f, duree)]
    if len(dedies) == 1:
        decompte = dedies[0]
    else:
        decompte = trouver_fichier(["decompte", "countdown"], EXT_VIDEOS, exclure=("blindtest",),
                                   garder=lambda f: not any(cite_duree(f, d)
                                                            for d in range(DUREE_MIN, DUREE_MAX + 1)))
    erreurs = []
    for quoi, f, nom in (("image de fond", fond, "fond.png"), ("vidéo de décompte", decompte, "decompte.mp4")):
        if f is None:
            erreurs.append(f"{quoi} introuvable dans {BASE} (nommez-la {nom})")
        elif isinstance(f, list):
            erreurs.append(f"plusieurs fichiers possibles pour l'{quoi} ({', '.join(x.name for x in f)}) : "
                           f"renommez le bon en {nom}")
    if erreurs:
        return None, None, " ; ".join(erreurs)
    return fond, decompte, None


def ffmpeg(*args):
    verifier_annulation()
    r = subprocess.run([OUTILS["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise Erreur("ffmpeg : " + (r.stderr.strip().splitlines() or ["erreur inconnue"])[-1])


def calque_texte(tmp, nom, contenu, fonte, taille, couleur, y):
    """Texte centré, lu depuis un fichier : aucun caractère du titre ne peut casser la commande."""
    f = Path(tmp) / f"{nom}.txt"
    f.write_text(contenu, encoding="utf-8")
    return (f"drawtext=fontfile={police(fonte)}:textfile={pour_filtre(f)}:expansion=none:"
            f"fontsize={taille}:fontcolor={couleur}:x=(w-text_w)/2:y={y}")


def couper_titre(titre):
    for largeur in (22, 28, 34, 42):
        lignes = textwrap.wrap(titre, largeur) or [titre]
        if len(lignes) <= 2:
            break
    lignes = lignes[:3]
    taille = max(px(52), min(px(112), int(px(1500) / (0.58 * max(len(l) for l in lignes)))))
    return lignes, taille


def fond_youtube(fond, sortie):
    """Image de fond -> format YouTube 16:9 (recadrée pour remplir l'écran)."""
    ffmpeg("-i", fond, "-vf",
           f"scale={VIDEO_L}:{VIDEO_H}:force_original_aspect_ratio=increase:flags=lanczos,"
           f"crop={VIDEO_L}:{VIDEO_H}:(iw-{VIDEO_L})/2:(ih-{VIDEO_H})*0.62,format=rgb24",
           "-frames:v", 1, sortie)


def carte_reponse(n, grand, moyen, petit, fond_png, sortie, tmp):
    """grand : titre (ou œuvre) ; moyen : artiste (ou « titre ») ; petit : interprète, facultatif."""
    lignes, taille = couper_titre(grand)
    hl = int(taille * 1.12)
    t_moy = max(px(34), min(px(58), int(px(1700) / (0.52 * max(len(moyen), 1)))))  # texte long : plus petit
    t_pet = max(px(28), min(px(40), int(px(1700) / (0.5 * max(len(petit or ""), 1)))))
    bloc = px(100) + len(lignes) * hl + px(30) + t_moy + (px(22) + t_pet if petit else 0)
    y0 = (VIDEO_H - bloc) // 2
    y_titre = y0 + px(100)
    y_moy = y_titre + len(lignes) * hl + px(30)
    calques = [calque_texte(tmp, "lab", f"RÉPONSE  {n:02d}", "bahnschrift.ttf", px(46), CYAN, y0)]
    calques += [calque_texte(tmp, f"t{i}", l, "seguibl.ttf", taille, BLANC, y_titre + i * hl)
                for i, l in enumerate(lignes)]
    calques.append(calque_texte(tmp, "moy", moyen, "seguisb.ttf", t_moy, VIOLET, y_moy))
    if petit:
        calques.append(calque_texte(tmp, "pet", petit, "segoeui.ttf", t_pet, "0xB9C3E6", y_moy + t_moy + px(22)))
    deco = f"drawbox=x=(iw-{px(220)})/2:y={y0 + px(66)}:w={px(220)}:h={px(4)}:color={CYAN}@0.9:t=fill"
    halo = ",".join(c.replace(f"fontcolor={BLANC}", f"fontcolor={CYAN}") for c in calques)
    vide = f"color=c=black@0:s={VIDEO_L}x{VIDEO_H},format=rgba"
    filtre = (
        f"[0]eq=brightness=-0.08[bg];"
        f"{vide},drawbox=x=0:y={y0 - px(70)}:w=iw:h={bloc + px(140)}:color=0x05061A@0.6:t=fill,"
        f"gblur=sigma={px(40)}[bande];"
        f"{vide},{halo},{deco},gblur=sigma={px(14)}[halo];"
        f"[bg][bande]overlay=format=auto[b1];[b1][halo]overlay=format=auto,{deco},{','.join(calques)}[v]")
    ffmpeg("-i", fond_png, "-filter_complex", filtre, "-map", "[v]", "-frames:v", 1, sortie)


DOSSIER_SLIDES = BASE / "slides"
SLIDES = {
    "intro": {"duree": 8, "elements": [
        ("BLIND TEST", "bahnschrift.ttf", 46, CYAN, 0),
        ("Bonjour et bienvenue", "seguibl.ttf", 104, BLANC, 40),
        ("sur ce blind test !", "seguibl.ttf", 104, BLANC, 6),
        ("RÈGLES", "bahnschrift.ttf", 46, CYAN, 90),
        ("1 point par bonne réponse", "seguisb.ttf", 64, BLANC, 26),
        ("le titre  ·  l'artiste  ·  l'œuvre", "seguisb.ttf", 58, VIOLET, 16),
    ]},
    "fin": {"duree": 8, "elements": [
        ("BLIND TEST", "bahnschrift.ttf", 46, CYAN, 0),
        ("Merci d'avoir joué !", "seguibl.ttf", 112, BLANC, 40),
        ("N'hésitez pas à vous abonner", "seguisb.ttf", 62, VIOLET, 60),
        ("et à suivre la chaîne pour plus de blind tests", "seguisb.ttf", 52, VIOLET, 14),
        ("BOUTON:S'ABONNER", "bahnschrift.ttf", 54, BLANC, 70),
    ]},
}


def slide_png(elements, fond_png, sortie, tmp):
    """Texte centré sur le fond, avec le même halo néon que les cartes réponses."""
    hauteurs = [px(taille) + px(ecart) for _, _, taille, _, ecart in elements]
    bouton_h = px(110)
    total = sum(hauteurs) + sum(bouton_h - px(t) for txt, _, t, _, _ in elements if txt.startswith("BOUTON:"))
    y = (VIDEO_H - total) // 2
    calques, boites = [], []
    for i, (texte, fonte, taille, couleur, ecart) in enumerate(elements):
        y += px(ecart)
        if texte.startswith("BOUTON:"):
            texte, larg = texte[len("BOUTON:"):], px(460)
            boites.append(f"drawbox=x=(iw-{larg})/2:y={y}:w={larg}:h={bouton_h}:color=0xE62B4E@0.95:t=fill")
            calques.append(calque_texte(tmp, f"e{i}", texte, fonte, px(taille), couleur,
                                        y + (bouton_h - px(taille)) // 2 - px(4)))
            y += bouton_h
        else:
            calques.append(calque_texte(tmp, f"e{i}", texte, fonte, px(taille), couleur, y))
            y += px(taille)
    y_deco = (VIDEO_H - total) // 2 + px(elements[0][2]) + px(16)
    deco = f"drawbox=x=(iw-{px(220)})/2:y={y_deco}:w={px(220)}:h={px(4)}:color={CYAN}@0.9:t=fill"
    halo = ",".join(c.replace(f"fontcolor={BLANC}", f"fontcolor={CYAN}") for c in calques)
    vide = f"color=c=black@0:s={VIDEO_L}x{VIDEO_H},format=rgba"
    haut = (VIDEO_H - total) // 2
    filtre = (
        f"[0]eq=brightness=-0.08[bg];"
        f"{vide},drawbox=x=0:y={haut - px(90)}:w=iw:h={total + px(180)}:color=0x05061A@0.6:t=fill,"
        f"gblur=sigma={px(40)}[bande];"
        f"{vide},{','.join(boites + [halo, deco])},gblur=sigma={px(14)}[halo];"
        f"[bg][bande]overlay=format=auto[b1];[b1][halo]overlay=format=auto,"
        f"{','.join(boites + [deco] + calques)}[v]")
    ffmpeg("-i", fond_png, "-filter_complex", filtre, "-map", "[v]", "-frames:v", 1, sortie)


def creer_slides():
    """slides/intro.png + intro.mp4 et slides/fin.png + fin.mp4 (fondu d'entrée et de sortie)."""
    verifier_outils()
    fond = trouver_fichier(["fond", "background"], EXT_IMAGES)
    if not isinstance(fond, Path):
        raise Erreur("Image de fond introuvable ou ambiguë : nommez-la fond.png.")
    DOSSIER_SLIDES.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fond_png = tmp / "fond.png"
        fond_youtube(fond, fond_png)
        for nom, s in SLIDES.items():
            png, mp4 = DOSSIER_SLIDES / f"{nom}.png", DOSSIER_SLIDES / f"{nom}.mp4"
            slide_png(s["elements"], fond_png, png, tmp)
            d = s["duree"]
            ffmpeg("-loop", 1, "-framerate", VIDEO_FPS, "-t", d, "-i", png,
                   "-f", "lavfi", "-t", d, "-i", "anullsrc=r=44100:cl=stereo",
                   "-vf", f"fade=t=in:st=0:d=0.6,fade=t=out:st={d - 0.6}:d=0.6,format=yuv420p",
                   "-map", "0:v", "-map", "1:a", "-t", d, *ENCODAGE, "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", mp4)
            afficher(f"Créé : {png}  et  {mp4.name} ({d} s)")


MARQUE_FOND_INCLUS = "blindtest:fond-inclus"


def fond_inclus(video):
    """Les décomptes créés par ce script portent une marque : ils contiennent déjà le fond."""
    r = subprocess.run([OUTILS["ffprobe"], "-v", "error", "-show_entries", "format_tags=comment",
                        "-of", "default=nw=1:nk=1", str(video)], capture_output=True, text=True)
    return r.stdout.strip() == MARQUE_FOND_INCLUS


def creer_decompte(duree, fond, sortie):
    """Décompte de `duree` secondes sur le fond : anneau néon qui se vide, chiffre qui pulse
    à chaque seconde, passage au rose pendant les 3 dernières secondes."""
    taille = px(700)
    c = taille // 2
    fin = f"gte(T,{duree - 3})"
    dist = f"hypot(X-{c},Y-{c})"
    angle = f"mod(atan2(X-{c},{c}-Y)+2*PI,2*PI)/(2*PI)"        # 0 en haut, sens horaire
    anneau = f"clip({px(10.5)}-abs({dist}-{px(270)}),0,1)"
    cercle = f"clip({px(2)}-abs({dist}-{px(318)}),0,1)"
    alpha = f"255*{anneau}*if(lte({angle},1-T/{duree}),1,0.22)+110*{cercle}"
    geq = f"geq=r='if({fin},255,70)':g='if({fin},110,210)':b='if({fin},215,255)':a='{alpha}'"
    pulse = "(1+0.14*max(0,1-5*(t-floor(t))))"

    def chiffre(couleur, bord, quand):
        return (f"drawtext=fontfile={police('segoeuil.ttf')}:text='%{{eif\\:ceil({duree}-t)\\:d}}':"
                f"fontsize='{px(250)}*{pulse}':x=(w-text_w)/2:y=(h-text_h)/2-{px(18)}:fontcolor={couleur}:"
                f"borderw={px(3)}:bordercolor={bord}@0.7:enable='{quand}'")

    filtre = (f"color=c=black@0:s={taille}x{taille}:r={VIDEO_FPS}:d={duree},format=rgba,{geq},"
              f"{chiffre('0xF4EAFF', CYAN, f'lt(t,{duree - 3})')},"
              f"{chiffre('0xFFE3F6', '0xFF6EC7', f'gte(t,{duree - 3})')},"
              f"split[a][b];[b]gblur=sigma={px(18)}[g];"
              f"[0:v]fps={VIDEO_FPS},format=rgba[bg];[bg][g]overlay=(W-w)/2:(H-h)/2[t1];"
              f"[t1][a]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    with tempfile.TemporaryDirectory() as tmp:
        fond_png = Path(tmp) / "fond.png"
        fond_youtube(fond, fond_png)
        ffmpeg("-loop", 1, "-framerate", VIDEO_FPS, "-t", duree, "-i", fond_png,
               "-f", "lavfi", "-t", duree, "-i", "anullsrc=r=44100:cl=stereo",
               "-filter_complex", filtre, "-map", "[v]", "-map", "1:a", "-t", duree, *ENCODAGE,
               "-c:a", "aac", "-b:a", "128k", "-metadata", f"comment={MARQUE_FOND_INCLUS}",
               "-movflags", "+faststart", sortie)


def creer_decomptes(durees=(20, 30)):
    verifier_outils()
    fond = trouver_fichier(["fond", "background"], EXT_IMAGES)
    if not isinstance(fond, Path):
        raise Erreur("Image de fond introuvable ou ambiguë : nommez-la fond.png.")
    for d in durees:
        sortie = BASE / f"countdown {d}.mp4"
        afficher(f"Création de « {sortie.name} » ({d} s, fond : {fond.name})...")
        provisoire = BASE / f"_countdown {d}.tmp.mp4"
        try:
            creer_decompte(d, fond, provisoire)
            os.replace(provisoire, sortie)
        except OSError:
            raise Erreur(f"Impossible de remplacer {sortie.name} : fichier ouvert dans un autre programme ?")
        finally:
            provisoire.unlink(missing_ok=True)
    afficher("Décomptes créés. Ils seront utilisés automatiquement pour les options 2 (20 s) et 3 (30 s).")


def gabarit_extrait(fond_png, decompte, duree, sortie):
    """Fond + décompte pendant l'extrait. Le noir du décompte devient transparent (fusion « écran »).
    Décompte plus long que l'extrait : accéléré ; plus court : il démarre pour finir avec l'extrait."""
    dc = duree_audio(decompte)
    if dc <= 0:
        raise Erreur(f"vidéo de décompte illisible : {decompte.name}")
    inclus = fond_inclus(decompte)
    if duree <= dc + 0.05:
        cd = f"setpts=PTS*{min(1, duree / dc):.6f},fps={VIDEO_FPS},scale={VIDEO_L}:{VIDEO_H}:flags=lanczos"
    else:
        attente = "start_mode=clone" if inclus else "color=black"
        cd = (f"fps={VIDEO_FPS},scale={VIDEO_L}:{VIDEO_H}:flags=lanczos,"
              f"tpad=start_duration={duree - dc:.3f}:{attente}")
    if inclus:   # décompte créé par ce script : il contient déjà le fond, on le prend tel quel
        filtre = f"[1:v]{cd},tpad=stop_mode=clone:stop_duration=1,trim=duration={duree},format=yuv420p[v]"
    else:
        filtre = (f"[1:v]{cd},format=gbrp,trim=duration={duree}[cd];[0:v]format=gbrp[bg];"
                  f"[bg][cd]blend=all_mode=screen,format=yuv420p[v]")
    ffmpeg("-loop", 1, "-framerate", VIDEO_FPS, "-t", duree, "-i", fond_png, "-i", decompte,
           "-filter_complex", filtre, "-map", "[v]", "-t", duree, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-qp", 0, sortie)   # intermédiaire sans perte


def duree_reponse(dossier, duree):
    """Durée de la carte réponse : 5 s, ou plus quand la musique continue (fichier plus long que la devinette)."""
    longueur = duree_audio(dossier / "01.mp3") if (dossier / "01.mp3").exists() else duree
    return max(DUREE_REPONSE, round(longueur - duree))


def segment_video(n, gabarit, carte, duree, sortie, tmp, reponse=DUREE_REPONSE):
    """« N° 07 » + décompte pendant l'extrait, puis la carte réponse (`reponse` s, apparition en fondu)."""
    numero = calque_texte(tmp, f"num{n}", f"N° {n:02d}", "bahnschrift.ttf", px(78), BLANC, px(60))
    numero += f":borderw={px(3)}:bordercolor={CYAN}@0.55"
    filtre = (f"[0:v]{numero},setsar=1[a];"
              f"[1:v]fps={VIDEO_FPS},format=yuv420p,trim=duration={reponse},"
              f"fade=t=in:st=0:d=0.4,setsar=1[b];[a][b]concat=n=2:v=1:a=0[v]")
    ffmpeg("-i", gabarit, "-loop", 1, "-framerate", VIDEO_FPS, "-t", reponse, "-i", carte,
           "-filter_complex", filtre, "-map", "[v]", "-an", "-r", VIDEO_FPS, *ENCODAGE, sortie)


def artiste_titre(c):
    rep = c.get("reponse")
    if rep and " - " in rep:
        a, t = rep.split(" - ", 1)
        return a.strip(), t.strip()
    return c["artiste"], c["titre"]


def segment_slide(png, duree, sortie):
    """Slide d'intro ou de fin : image fixe avec fondu, encodée comme les autres séquences."""
    ffmpeg("-loop", 1, "-framerate", VIDEO_FPS, "-t", duree, "-i", png,
           "-vf", f"scale={VIDEO_L}:{VIDEO_H}:flags=lanczos,setsar=1,fade=t=in:st=0:d=0.6,"
                  f"fade=t=out:st={duree - 0.6}:d=0.6,format=yuv420p",
           "-an", "-r", VIDEO_FPS, *ENCODAGE, sortie)


def preparer_slides(fond_png, tmp):
    """slides/intro.png et slides/fin.png : ceux du dossier s'ils existent (modifiables), sinon créés."""
    DOSSIER_SLIDES.mkdir(exist_ok=True)
    for nom, s in SLIDES.items():
        png = DOSSIER_SLIDES / f"{nom}.png"
        if not png.exists():
            sous = tmp / f"slide_{nom}"
            sous.mkdir()
            slide_png(s["elements"], fond_png, png, sous)
    return DOSSIER_SLIDES / "intro.png", DOSSIER_SLIDES / "fin.png"


def fichiers_video_par_defaut(duree):
    """Première vidéo sur un PC neuf : sans image de fond ni décompte, on en crée (fond.png, « countdown N.mp4 »).
    Ils sont ensuite réutilisés ; on peut les remplacer par les siens (même nom)."""
    fond = trouver_fichier(["fond", "background"], EXT_IMAGES)
    if fond is None:
        afficher("Pas d'image de fond : création d'un fond par défaut (fond.png, remplaçable par la vôtre)...")
        ffmpeg("-f", "lavfi", "-i", f"gradients=s={VIDEO_L}x{VIDEO_H}:c0=0x2a1f6e:c1=0x05061a:x0={VIDEO_L // 2}:y0=0:"
               f"x1={VIDEO_L // 2}:y1={VIDEO_H}:nb_colors=2", "-frames:v", 1, "-vf", "vignette=PI/4", BASE / "fond.png")
        fond = BASE / "fond.png"
    if not isinstance(fond, Path):
        return                                           # plusieurs images possibles : fichiers_video() le signale
    _, decompte, _ = fichiers_video(duree)
    if decompte is None:
        afficher(f"Pas de vidéo de décompte : création de « countdown {duree}.mp4 » (une seule fois, environ 1 min)...")
        provisoire = BASE / f"_countdown {duree}.tmp.mp4"
        try:
            creer_decompte(duree, fond, provisoire)
            os.replace(provisoire, BASE / f"countdown {duree}.mp4")
        finally:
            provisoire.unlink(missing_ok=True)


def decompte_sur_mesure(duree):
    """Durée libre (ex. 15 s) plus longue que le décompte général : on crée une fois « countdown 15.mp4 »
    (même style que ceux de 20 et 30 s), réutilisé ensuite. Sinon le chiffre ne démarrerait qu'en cours d'extrait."""
    fond, decompte, erreur = fichiers_video(duree)
    if erreur or cite_duree(decompte, duree) or duree <= duree_audio(decompte) + 0.5:
        return
    sortie = BASE / f"countdown {duree}.mp4"
    provisoire = BASE / f"_countdown {duree}.tmp.mp4"
    afficher(f"Création du décompte de {duree} s (une seule fois, environ 1 min)...")
    try:
        creer_decompte(duree, fond, provisoire)
        os.replace(provisoire, sortie)
    finally:
        provisoire.unlink(missing_ok=True)


def monter_video(retenus, dossier_lot, duree, notes, sortie=None, slides=True):
    fichiers_video_par_defaut(duree)
    decompte_sur_mesure(duree)
    fond, decompte, erreur = fichiers_video(duree)
    if erreur:
        afficher(f"\nVidéo non créée : {erreur}.")
        notes.append(f"Vidéo non créée : {erreur}.")
        return
    afficher(f"\nMontage de la vidéo YouTube (fond : {fond.name}, décompte : {decompte.name})...")
    nb = len(retenus)
    reponse = duree_reponse(dossier_lot, duree)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fond_png, gabarit = tmp / "fond.png", tmp / "gabarit.mp4"
        fond_youtube(fond, fond_png)
        gabarit_extrait(fond_png, decompte, duree, gabarit)

        def un(n):
            c = retenus[n - 1]
            sous = tmp / f"s{n}"
            sous.mkdir()
            if c.get("oeuvre"):
                carte_reponse(n, c["oeuvre"], f"« {c['titre_court']} »", c.get("interprete"),
                              fond_png, sous / "carte.png", sous)
            else:
                artiste, titre = artiste_titre(c)
                carte_reponse(n, titre, artiste, c.get("note"), fond_png, sous / "carte.png", sous)
            segment_video(n, gabarit, sous / "carte.png", duree, tmp / f"seg{n:03d}.mp4", sous, reponse)

        faits = 0
        with ThreadPoolExecutor(3) as ex:
            for _ in ex.map(un, range(1, nb + 1)):
                faits += 1
                if faits % 10 == 0 or faits == nb:
                    afficher(f"  {faits}/{nb} séquences prêtes")

        d_intro = d_fin = 0
        fichiers = [f"seg{n:03d}.mp4" for n in range(1, nb + 1)]
        if slides:
            intro, fin = preparer_slides(fond_png, tmp)
            d_intro, d_fin = SLIDES["intro"]["duree"], SLIDES["fin"]["duree"]
            segment_slide(intro, d_intro, tmp / "slide_intro.mp4")
            segment_slide(fin, d_fin, tmp / "slide_fin.mp4")
            fichiers = ["slide_intro.mp4"] + fichiers + ["slide_fin.mp4"]
            afficher("  Slides d'intro et de fin ajoutées")

        # image : assemblage sans réencodage ; son : extrait (avec sa suite pendant la réponse, ou du silence)
        # calé à l'image près
        liste = tmp / "liste.txt"
        liste.write_text("".join(f"file '{f}'\n" for f in fichiers), encoding="utf-8")
        ffmpeg("-f", "concat", "-safe", 0, "-i", liste, "-c", "copy", tmp / "image.mp4")
        entrees = []
        for n in range(1, nb + 1):
            entrees += ["-i", dossier_lot / f"{n:02d}.mp3"]
        pistes = "".join(f"[{i}:a]apad=whole_dur={duree + reponse},atrim=0:{duree + reponse}[a{i}];"
                         for i in range(nb))
        filtre = pistes + "".join(f"[a{i}]" for i in range(nb)) + f"concat=n={nb}:v=0:a=1"
        if slides:   # silence pendant l'intro et la fin
            filtre += f",adelay={d_intro * 1000}:all=1,apad=pad_dur={d_fin}"
        filtre += "[a]"
        ffmpeg(*entrees, "-filter_complex", filtre, "-map", "[a]", "-c:a", "aac", "-b:a", "256k",
               "-ar", "44100", tmp / "son.m4a")
        ffmpeg("-i", tmp / "image.mp4", "-i", tmp / "son.m4a", "-map", "0:v", "-map", "1:a",
               "-c", "copy", "-movflags", "+faststart", "-shortest", sortie or dossier_lot / "blindtest.mp4")
    total = nb * (duree + reponse) + d_intro + d_fin
    afficher(f"  Vidéo prête ({total // 60} min {total % 60:02d} s)")


# ---------- CapCut (lecture seule : le script ne modifie jamais les projets) ----------

CAPCUT_PROJETS = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"


def capcut_ouvert():
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=10)
        return "capcut.exe" in r.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def chemin_comparable(p):
    return os.path.normcase(os.path.normpath(str(p)))


def projets_capcut(dossier):
    """Projets CapCut qui utilisent des sons du dossier : {nom du projet: {noms de fichiers}}."""
    trouves = {}
    try:
        meta = json.loads((CAPCUT_PROJETS / "root_meta_info.json").read_text(encoding="utf-8"))
        brouillons = meta.get("all_draft_store", [])
    except (OSError, ValueError):
        return trouves
    cible = chemin_comparable(dossier)
    for b in brouillons:
        try:
            d = json.loads(Path(b["draft_json_file"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for a in d.get("materials", {}).get("audios", []):
            chemin = a.get("path") or ""
            if chemin and chemin_comparable(Path(chemin).parent) == cible:
                trouves.setdefault(b.get("draft_name", "?"), set()).add(Path(chemin).name)
    return trouves


def verifier_capcut_avant(interactif):
    if not capcut_ouvert():
        return
    afficher("\nCapCut est ouvert. Si le projet du blind test est ouvert, CapCut risque de garder "
             "les anciens sons en mémoire.")
    if interactif:
        input("Fermez le projet (ou CapCut), puis appuyez sur Entrée pour continuer... ")
    else:
        afficher("Pensez à fermer puis rouvrir le projet dans CapCut après ce lancement.")


def bilan_capcut(dest, nb_produits):
    projets = projets_capcut(dest)
    if not projets:
        afficher(f"\nAucun projet CapCut n'utilise encore le dossier {dest.name}. Pour créer le modèle : "
                 f"dans CapCut, Importer > choisissez les fichiers de {dest}.")
        return
    produits = {f"{n:02d}.mp3" for n in range(1, nb_produits + 1)}
    for nom, fichiers in sorted(projets.items()):
        afficher(f"\nCapCut : ouvrez le projet « {nom} » pour retrouver les nouveaux sons.")
        manquants = sorted(f for f in fichiers if re.fullmatch(r"\d+\.mp3", f) and f not in produits)
        if manquants:
            afficher(f"  Attention : ce projet utilise {', '.join(manquants)}, absents de ce lot "
                     "(CapCut les affichera comme médias manquants).")


# ---------- Historique ----------

def lire_historique():
    for f in (FICHIER_HISTORIQUE, FICHIER_HISTORIQUE.with_suffix(".json.bak")):
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data.get("lancements"), list):
                    if f != FICHIER_HISTORIQUE:
                        afficher("Attention : historique abîmé, la copie de sauvegarde est utilisée.")
                    return data
            except (ValueError, OSError, AttributeError):
                afficher(f"Attention : fichier {f.name} illisible.")
    return {"lancements": []}


def ecrire_historique(data):
    tmp = FICHIER_HISTORIQUE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if FICHIER_HISTORIQUE.exists():
        shutil.copy2(FICHIER_HISTORIQUE, FICHIER_HISTORIQUE.with_suffix(".json.bak"))
    os.replace(tmp, FICHIER_HISTORIQUE)


def titres_bloques(historique, blocage):
    derniers = historique["lancements"][-blocage:] if blocage > 0 else []
    return {t["cle"] for l in derniers for t in l["titres"]}


def lire_lot(dest):
    """Réponses d'un lot existant : lot.json s'il existe, sinon reponses.txt (anciens lots)."""
    fichier = dest / "reponses.txt"
    if not fichier.exists():
        raise Erreur(f"Pas de reponses.txt dans {dest} : lancez d'abord un blind test dans ce format.")
    texte = fichier.read_text(encoding="utf-8")
    source = next((l[len("Source : "):] for l in texte.splitlines() if l.startswith("Source : ")), "")
    if (dest / "lot.json").exists():
        return json.loads((dest / "lot.json").read_text(encoding="utf-8")), source, texte
    retenus = []
    for ligne in texte.splitlines():
        m = re.match(r"^(\d+)\.\s+(.+)$", ligne.strip())
        if m:
            t = m.group(2)
            a, ti = t.split(" - ", 1) if " - " in t else ("", t)
            retenus.append({"reponse": t, "artiste": a.strip(), "titre": ti.strip()})
    # identifiants Deezer retrouvés dans l'historique (même liste de titres, dans le même ordre)
    for lanc in reversed(lire_historique()["lancements"]):
        titres = lanc["titres"]
        if [f"{t['artiste']} - {t['titre']}" for t in titres] == [c["reponse"] for c in retenus]:
            for c, t in zip(retenus, titres):
                c["id"] = t.get("id")
            break
    return retenus, source, texte


# ---------- Retouches d'un lot existant (éditeur de réponses de la page web) ----------

COMPOSITEURS = {normaliser(n) for n in (
    "Andrew Lloyd Webber", "Stephen Sondheim", "Alan Menken", "Stephen Schwartz", "Claude-Michel Schönberg",
    "Richard Rodgers", "Oscar Hammerstein II", "Leonard Bernstein", "Hans Zimmer", "John Williams", "Howard Ashman",
    "Tim Rice", "Benj Pasek", "Justin Paul", "Pasek & Paul", "Kristen Anderson-Lopez", "Robert Lopez", "Frank Wildhorn",
    "Alain Boublil", "Luc Plamondon", "Gérard Presgurvic", "Cole Porter", "Irving Berlin", "George Gershwin",
    "Jerry Herman", "John Kander", "Fred Ebb", "Marvin Hamlisch", "Jule Styne", "Frank Loesser", "Harold Arlen",
    "Sherman Brothers", "Richard M. Sherman", "Michel Legrand", "Ennio Morricone", "Alexandre Desplat")}


def entete_reponses(dest):
    try:
        return (dest / "reponses.txt").read_text(encoding="utf-8").split("\n\n", 1)[0]
    except OSError:
        return f"Blind test du {datetime.now().strftime('%d/%m/%Y %H:%M')}"


def reecrire_reponses(dest, retenus, entete=None):
    """lot.json et reponses.txt après une retouche (l'en-tête du lot est conservé)."""
    entete = entete or entete_reponses(dest)
    ecrire_lot(retenus, dest)
    lignes = [f"{n:02d}. {texte_reponse(c)}" for n, c in enumerate(retenus, 1)]
    tmp = dest / "reponses.txt.tmp"
    tmp.write_text(entete + "\n\n" + "\n".join(lignes) + "\n", encoding="utf-8")
    os.replace(tmp, dest / "reponses.txt")


def debut_du_lot(dest):
    m = re.search(r"départ à ([\d,.]+) s", entete_reponses(dest))
    return float(m.group(1).replace(",", ".")) if m else 0.0


def remplacer_extrait(dest, n, cand, duree, oeuvre=False):
    """Remplace l'extrait n° n par un autre morceau Deezer (même départ, même longueur que le reste du lot)."""
    with verrou_generation():
        verifier_outils()
        retenus = json.loads((dest / "lot.json").read_text(encoding="utf-8"))
        if not 1 <= n <= len(retenus):
            raise Erreur(f"Pas de question n° {n} dans ce lot.")
        debut = debut_du_lot(dest)
        longueur = DUREE_PREVIEW - debut if musique_continue(duree) else duree
        DOSSIER_TRAVAIL.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=DOSSIER_TRAVAIL) as tmp:
            cible = Path(tmp) / f"{n:02d}.mp3"
            afficher(f"Téléchargement et découpe de « {cand['artiste']} - {cand['titre']} »...")
            raison = traiter(cand, cible, tmp, debut, longueur)
            if raison:
                raise Erreur(f"Extrait impossible pour ce morceau : {raison}. Choisissez-en un autre.")
            nouveau = {k: cand.get(k) for k in ("id", "artiste", "titre")}
            nouveau["reponse"] = f"{cand['artiste']} - {cand['titre']}"
            for k in ("note", "famille"):              # langue et famille d'une liste : elles restent
                if retenus[n - 1].get(k):
                    nouveau[k] = retenus[n - 1][k]
            if oeuvre:
                afficher("Recherche de l'œuvre (film, spectacle)...")
                try:
                    identifier_oeuvre(nouveau, {})
                except (Erreur, Introuvable):
                    pass
            try:
                os.replace(cible, dest / f"{n:02d}.mp3")
            except OSError:
                raise FichierVerrouille(f"{n:02d}.mp3 est ouvert dans un autre programme (lecteur, CapCut ?) : "
                                        "fermez-le puis réessayez.")
        try:
            DOSSIER_TRAVAIL.rmdir()
        except OSError:
            pass
        retenus[n - 1] = {k: v for k, v in nouveau.items() if v is not None}
        reecrire_reponses(dest, retenus)
        afficher(f"Question {n:02d} remplacée : {texte_reponse(retenus[n - 1])}")
        return retenus[n - 1]


def garder_essai(essai, dest):
    """Un essai réussi devient le blind test officiel : l'ancien est archivé et les titres entrent dans l'historique."""
    with verrou_generation():
        retenus = json.loads((essai / "lot.json").read_text(encoding="utf-8"))
        entete = "\n".join(l for l in entete_reponses(essai).splitlines() if not l.startswith("Mode essai"))
        source = next((l[len("Source : "):] for l in entete.splitlines() if l.startswith("Source : ")), "essai")
        provisoire = DOSSIER_TRAVAIL / f"nouveau_{dest.name}"
        shutil.rmtree(provisoire, ignore_errors=True)
        DOSSIER_TRAVAIL.mkdir(exist_ok=True)
        try:
            os.replace(essai, provisoire)
        except OSError:
            raise FichierVerrouille(f"Un fichier de {essai.name} est ouvert dans un autre programme (lecteur audio, "
                                    "CapCut ?) : fermez-le puis réessayez.")
        try:
            reecrire_reponses(provisoire, retenus, entete)
            archive = installer(provisoire, dest, archiver=True)
        except Exception:
            if provisoire.exists() and not essai.exists():
                os.replace(provisoire, essai)        # rien n'a bougé : l'essai reste un essai
            raise
        shutil.rmtree(provisoire, ignore_errors=True)
        try:
            DOSSIER_TRAVAIL.rmdir()
        except OSError:
            pass
        historique = lire_historique()
        historique["lancements"].append({
            "date": datetime.now().strftime("%d/%m/%Y %H:%M"), "option": None,
            "duree": int(dest.name[len("extraits_"):-1]), "source": source,
            "titres": [{"cle": cle_titre(c["artiste"], c["titre"]), "artiste": c["artiste"], "titre": c["titre"],
                        "id": c.get("id")} for c in retenus]})
        historique["lancements"] = historique["lancements"][-100:]
        ecrire_historique(historique)
        return archive


def video_seule(duree, nom=None, oeuvres=None, slides=True, dest=None):
    """Monte la vidéo à partir des extraits et des réponses déjà présents (sans tirage)."""
    with verrou_generation():
        return _video_seule(duree, nom, oeuvres, slides, dest)


def _video_seule(duree, nom, oeuvres, slides, dest):
    verifier_outils()
    dest = dest or BASE / f"extraits_{duree}s"
    retenus, source, texte = lire_lot(dest)
    manquants = [f"{n:02d}.mp3" for n in range(1, len(retenus) + 1) if not (dest / f"{n:02d}.mp3").exists()]
    if not retenus or manquants:
        raise Erreur(f"Extraits manquants dans {dest} : {', '.join(manquants) or 'aucune réponse lue'}.")
    afficher(f"{len(retenus)} extraits et réponses trouvés dans {dest.name}.")
    notes = []
    if oeuvres is None:   # lot déjà enrichi (œuvres trouvées ou corrigées à la main) : on les garde
        oeuvres = (any(c.get("oeuvre") for c in retenus)
                   or bool(THEMES_OEUVRE.search(normaliser(f"{source} {nom or ''}"))))
    if oeuvres and not any("oeuvre" in c for c in retenus):
        if not all(c.get("id") for c in retenus):
            raise Erreur("Impossible de retrouver les morceaux sur Deezer pour ce lot (lancement annulé ou "
                         "mode essai ?). Relancez sans --oeuvre, ou refaites un tirage.")
        enrichir_oeuvres(retenus, notes)
        # la fiche réponses doit correspondre à la vidéo : on la réécrit avec les œuvres
        entete = texte.split("\n\n", 1)[0]
        lignes = [f"{n:02d}. {texte_reponse(c)}" for n, c in enumerate(retenus, 1)]
        (dest / "reponses.txt.tmp").write_text(entete + "\n\n" + "\n".join(lignes) + "\n", encoding="utf-8")
        os.replace(dest / "reponses.txt.tmp", dest / "reponses.txt")
        ecrire_lot(retenus, dest)
    elif oeuvres is False:
        for c in retenus:
            c.pop("oeuvre", None)
    DOSSIER_TRAVAIL.mkdir(exist_ok=True)
    provisoire = DOSSIER_TRAVAIL / f"blindtest_{duree}s.mp4"
    try:
        monter_video(retenus, dest, duree, notes, sortie=provisoire, slides=slides)
        if not provisoire.exists():
            raise Erreur(notes[-1] if notes else "vidéo non créée")
        try:
            for ancienne in dest.glob("*.mp4"):     # une seule vidéo par dossier
                if ancienne.name != nom_video(nom):
                    ancienne.unlink()
            os.replace(provisoire, dest / nom_video(nom))
        except OSError:
            raise Erreur(f"Impossible de remplacer la vidéo dans {dest} : le fichier est ouvert dans un "
                         "autre programme (lecteur vidéo, CapCut ?). Fermez-le puis relancez.")
    finally:
        provisoire.unlink(missing_ok=True)
        try:
            DOSSIER_TRAVAIL.rmdir()
        except OSError:
            pass
    afficher(f"\nVidéo : {dest / nom_video(nom)}")
    return dest / nom_video(nom)


def annuler_dernier():
    h = lire_historique()
    if not h["lancements"]:
        afficher("L'historique est vide : rien à annuler.")
        return
    l = h["lancements"].pop()
    ecrire_historique(h)
    format_ = f"{l['duree']} s" if l.get("duree") else f"option {l.get('option')}"
    afficher(f"Lancement annulé : {l['date']}, {format_}, {l['source']} "
             f"({len(l['titres'])} titres libérés).")
    afficher("Remarque : les fichiers audio déjà générés ne sont pas modifiés.")


# ---------- Programme principal ----------

def reglages(**valeurs):
    """Réglages d'une fabrication, avec les mêmes valeurs par défaut que la ligne de commande.
    Ex. : lancer(reglages(duree=15, nombre=40, spotify="https://open.spotify.com/playlist/...", video=False))"""
    args = analyseur().parse_args([])
    for k, v in valeurs.items():
        if not hasattr(args, k) and k not in ("lignes", "selection", "exclure_playlists"):
            raise TypeError(f"réglage inconnu : {k}")
        setattr(args, k, v)
    return args


def lancer(args, interactif=False):
    """Fabrique un lot. Renvoie un résumé (dossier, nombre d'extraits, vidéo, problèmes signalés...)."""
    with verrou_generation():
        return _lancer(args, interactif)


def _lancer(args, interactif):
    try:
        return _fabriquer(args, interactif)
    except Annule:
        raise Annule("Fabrication annulée : les anciens fichiers et l'historique n'ont pas été modifiés.")


def parametres(args):
    """Durée, nombre, départ et longueur réelle des fichiers, vérifiés. Renvoie (duree, nombre, debut, longueur)."""
    duree = getattr(args, "duree", None) or FORMATS[args.option]["duree"]
    if not (isinstance(duree, int) and DUREE_MIN <= duree <= DUREE_MAX):
        raise Erreur(f"Durée impossible : {duree} s (de {DUREE_MIN} à {DUREE_MAX} s, extraits Deezer de 30 s).")
    nombre = args.nombre or nombre_par_defaut(duree)
    debut = args.debut if duree < DUREE_PREVIEW else 0.0
    suite = musique_continue(duree)
    if debut < 0 or debut > debut_maxi(duree):
        besoin = duree + (DUREE_REPONSE if suite else 0)   # au moins 5 s de musique pendant la réponse
        raise Erreur(f"Décalage impossible : {secondes(debut)} s + {besoin} s dépasse les {DUREE_PREVIEW} s "
                     f"de l'extrait Deezer (maximum {debut_maxi(duree)} s pour cette durée).")
    # durée réelle du fichier : la devinette, plus la suite de l'extrait jusqu'au bout pendant la réponse
    longueur = DUREE_PREVIEW - debut if suite else duree
    return duree, nombre, debut, longueur


def selectionner(args, nombre, bloques, notes):
    """Choisit les titres (sans rien télécharger). Renvoie (candidats dans l'ordre, description de la source)."""
    if args.theme:
        cands, source = source_theme(args.theme, nombre, bloques, args, notes)
    elif args.playlist:
        cands, source = source_playlist(args.playlist, nombre, bloques, args, notes)
    elif args.spotify:
        cands, source = source_spotify(args.spotify, nombre, bloques, args, notes)
    elif getattr(args, "lignes", None):
        cands, source = source_lignes(args.lignes, f"liste collée ({len(args.lignes)} titres)", nombre,
                                      bloques, args, notes)
    else:
        cands, source = source_liste(args.liste, nombre, bloques, args, notes)
    if not cands:
        raise Erreur("Aucun titre disponible avec ces critères.")
    if len(cands) < nombre:
        afficher(f"Attention : seulement {len(cands)} titres disponibles pour {nombre} extraits.")
    return cands, source


def entete_lancement(args, duree, nombre, debut):
    historique = lire_historique()
    bloques = titres_bloques(historique, args.blocage)
    afficher(f"\nFormat : {nombre} extraits de {duree} s"
             + (f" (départ à {secondes(debut)} s)" if debut else "")
             + (" — MODE ESSAI (historique non modifié)" if args.essai else ""))
    if bloques and not (args.liste or getattr(args, "lignes", None)):
        afficher(f"{len(bloques)} titres bloqués par l'historique ({args.blocage} derniers lancements).")
    return bloques


def apercu(args):
    """Aperçu de la page web : les titres qui seraient retenus (puis les remplaçants), sans rien télécharger."""
    duree, nombre, debut, longueur = parametres(args)
    bloques = entete_lancement(args, duree, nombre, debut)
    notes = []
    cands, source = selectionner(args, nombre, bloques, notes)
    return {"cands": cands, "source": source, "notes": notes, "details": getattr(args, "_details", None),
            "nombre": nombre, "duree": duree}


def _fabriquer(args, interactif):
    verifier_outils()
    duree, nombre, debut, longueur = parametres(args)
    selection = getattr(args, "selection", None)      # titres déjà choisis dans l'aperçu de la page web
    if selection:
        afficher(f"\nFormat : {nombre} extraits de {duree} s — sélection de l'aperçu ({len(selection['cands'])} titres)"
                 + (" — MODE ESSAI (historique non modifié)" if args.essai else ""))
        cands, source, notes = list(selection["cands"]), selection["source"], list(selection["notes"])
    else:
        bloques = entete_lancement(args, duree, nombre, debut)
        notes = []
        cands, source = selectionner(args, nombre, bloques, notes)

    afficher(f"\nTéléchargement et découpe ({PARALLELE} à la fois)...")
    dest = BASE / (f"essai_{duree}s" if args.essai else f"extraits_{duree}s")  # l'essai ne touche pas au montage
    dossier_lot = DOSSIER_TRAVAIL / f"nouveau_{dest.name}"
    shutil.rmtree(dossier_lot, ignore_errors=True)
    dossier_lot.mkdir(parents=True)
    try:
        retenus = produire(cands, nombre, dossier_lot, debut, longueur, notes)
        if not retenus:
            raise Erreur("Aucun extrait n'a pu être produit (voir les messages ci-dessus).")
        if len(retenus) < nombre:
            msg = (f"ATTENTION : seulement {len(retenus)} extraits produits sur {nombre} demandés "
                   "(pas assez de titres disponibles).")
            afficher("\n" + msg)
            notes.insert(0, msg)

        verifier_annulation()
        if any(c.get("famille") for c in retenus):
            retenus = alterner_familles(retenus, dossier_lot, notes)
        if oeuvres_par_defaut(args, source):
            enrichir_oeuvres(retenus, notes)
        ecrire_lot(retenus, dossier_lot)

        verifier_annulation()
        if args.video:
            try:
                monter_video(retenus, dossier_lot, duree, notes, sortie=dossier_lot / nom_video(args.nom),
                             slides=args.slides)
            except Annule:
                raise
            except Erreur as e:
                afficher(f"\nVidéo non créée : {e}")
                notes.append(f"Vidéo non créée : {e}")

        date = datetime.now().strftime("%d/%m/%Y %H:%M")
        entete = [f"Blind test du {date}",
                  f"Format : {len(retenus)} extraits de {duree} s"
                  + (f" (départ à {secondes(debut)} s)" if debut else ""),
                  f"Source : {source}"]
        if args.essai:
            entete.append("Mode essai : non enregistré dans l'historique")
        if (dossier_lot / nom_video(args.nom)).exists():
            entete.insert(1, f"Vidéo : {nom_video(args.nom)}")
        lignes = [f"{n:02d}. {texte_reponse(c)}" for n, c in enumerate(retenus, 1)]
        (dossier_lot / "reponses.txt").write_text("\n".join(entete + [""] + lignes) + "\n", encoding="utf-8")
        if notes:
            (dossier_lot / "problemes.txt").write_text(
                "\n".join([f"Blind test du {date} — {source}", ""] + notes) + "\n", encoding="utf-8")

        verifier_annulation()          # dernier moment où l'on peut encore arrêter sans rien toucher
        if not args.essai:
            verifier_capcut_avant(interactif)
        archive = None
        while True:
            try:
                archive = installer(dossier_lot, dest, archiver=not args.essai)
                if archive:
                    afficher(f"\nAncien blind test conservé dans : {archive}")
                break
            except FichierVerrouille as e:
                afficher("\n" + str(e))
                if not interactif:
                    raise Erreur("Nouveau lot abandonné. Relancez une fois le fichier libéré.")
                rep = input("Appuyez sur Entrée pour réessayer, ou tapez A puis Entrée pour abandonner : ")
                if rep.strip().lower().startswith("a"):
                    raise Erreur("Nouveau lot abandonné : les anciens fichiers sont intacts.")
    finally:
        shutil.rmtree(dossier_lot, ignore_errors=True)
        try:
            DOSSIER_TRAVAIL.rmdir()
        except OSError:
            pass

    if not args.essai:
        historique = lire_historique()   # relu juste avant l'écriture : un autre lancement a pu écrire entre-temps
        historique["lancements"].append({
            "date": date, "option": args.option, "duree": duree, "source": source,
            "titres": [{"cle": c["cle"], "artiste": c["artiste"], "titre": c["titre"], "id": c.get("id")}
                       for c in retenus]})
        historique["lancements"] = historique["lancements"][-100:]
        ecrire_historique(historique)

    afficher(f"\nTerminé : {len(retenus)} extraits dans {dest}")
    afficher(f"Réponses : {dest / 'reponses.txt'}")
    if (dest / nom_video(args.nom)).exists():
        afficher(f"Vidéo : {dest / nom_video(args.nom)}")
    if notes:
        afficher(f"Problèmes signalés : {dest / 'problemes.txt'}")
    if args.essai:
        afficher("Mode essai : l'historique n'a pas été modifié et le dossier du montage n'a pas été touché.")
    else:
        bilan_capcut(dest, len(retenus))
        afficher("Si CapCut affiche encore un ancien son ou une ancienne forme d'onde : fermez puis "
                 "rouvrez le projet.")
    video = dest / nom_video(args.nom)
    return {"dossier": dest, "duree": duree, "nombre": len(retenus), "demandes": nombre, "source": source,
            "video": video if video.exists() else None, "archive": archive, "notes": notes}


def demander(question, choix=None, defaut=None):
    while True:
        rep = input(question).strip()
        if not rep and defaut is not None:
            return defaut
        if choix is None or rep in choix:
            if rep:
                return rep
        afficher("  Réponse non valide.")


def question_oeuvre():
    rep = demander("Afficher le film / la série / le spectacle dans les réponses ? "
                   "(o/n, Entrée = automatique selon le thème) : ", {"o", "n", "O", "N"}, defaut="auto")
    return None if rep == "auto" else rep.lower() == "o"


def menu(args):
    afficher("=" * 50)
    afficher("   BLIND TEST — générateur d'extraits Deezer")
    afficher("=" * 50)
    nb = len(lire_historique()["lancements"])
    afficher(f"Historique : {nb} lancement(s) enregistré(s).\n")
    afficher("1) Nouveau blind test")
    afficher("2) Annuler le dernier lancement de l'historique")
    afficher("3) Refaire seulement la vidéo à partir des extraits existants")
    afficher("4) Quitter")
    action = demander("Votre choix : ", {"1", "2", "3", "4"})
    if action == "4":
        return
    if action == "3":
        lots = sorted(d for d in BASE.glob("extraits_*s") if (d / "reponses.txt").exists())
        if not lots:
            afficher("Aucun lot d'extraits : lancez d'abord un nouveau blind test.")
            return
        afficher("\nLot :")
        for n, d in enumerate(lots, 1):
            afficher(f"  {n}) {d.name}")
        d = lots[int(demander("Votre choix : ", {str(n) for n in range(1, len(lots) + 1)})) - 1]
        nom = demander("Nom de la vidéo [blindtest] : ", defaut="blindtest")
        video_seule(int(d.name[len("extraits_"):-1]), nom, question_oeuvre(), dest=d)
        return
    if action == "2":
        annuler_dernier()
        return
    afficher("\nFormat :")
    for n, f in FORMATS.items():
        afficher(f"  {n}) {f['nombre']} extraits de {f['duree']} s")
    afficher(f"  4) Autre durée ({DUREE_MIN} à {DUREE_MAX} s)")
    choix = demander("Votre choix : ", {"1", "2", "3", "4"})
    if choix == "4":
        args.duree = int(demander(f"Durée en secondes ({DUREE_MIN} à {DUREE_MAX}) : ",
                                  {str(d) for d in range(DUREE_MIN, DUREE_MAX + 1)}))
        nb = nombre_par_defaut(args.duree)
        args.nombre = int(demander(f"Nombre d'extraits [{nb}] : ", {str(n) for n in range(1, 201)}, defaut=str(nb)))
    else:
        args.option = int(choix)
        args.duree = FORMATS[args.option]["duree"]
    afficher("\nSource des morceaux :")
    afficher("  1) Thème libre (ex. années 80, rap français)")
    afficher("  2) Playlist Deezer précise (lien ou numéro)")
    afficher("  3) Ma liste de titres (fichier texte « Artiste - Titre »)")
    afficher("  4) Playlist Spotify (lien) : titres retrouvés sur Deezer")
    src = demander("Votre choix : ", {"1", "2", "3", "4"})
    if src == "1":
        args.theme = demander("Thème : ")
    elif src == "2":
        args.playlist = demander("Lien ou numéro de la playlist : ")
    elif src == "4":
        args.spotify = demander("Lien de la playlist Spotify : ")
    else:
        args.liste = demander("Fichier liste (vous pouvez le glisser dans la fenêtre) [liste.txt] : ",
                              defaut="liste.txt")
    if debut_maxi(args.duree) > 0:
        maxi = debut_maxi(args.duree)
        while True:
            rep = demander(f"Décalage de départ en secondes, de 0 à {maxi} [0] : ", defaut="0")
            try:
                args.debut = float(rep.replace(",", "."))
                if 0 <= args.debut <= maxi:
                    break
            except ValueError:
                pass
            afficher("  Réponse non valide.")
    if args.liste or args.spotify:
        args.melanger = demander("Mélanger l'ordre des titres de la liste ? (o/N) : ",
                                 defaut="n").lower().startswith("o")
    fond, decompte, erreur = fichiers_video(args.duree)
    erreur = erreur if erreur and "plusieurs fichiers" in erreur else None    # absents : créés au montage
    if erreur:
        afficher(f"\n(Vidéo YouTube impossible : {erreur}.)")
        args.video = False
    else:
        args.video = not demander("Monter aussi la vidéo YouTube complète ? (O/n) : ",
                                  defaut="o").lower().startswith("n")
        if args.video:
            args.nom = demander("Nom de la vidéo [blindtest] : ", defaut="blindtest")
    if not args.liste:
        args.oeuvre = question_oeuvre()
    args.essai = demander("Mode essai (dossier essai_..., sans historique) ? (o/N) : ",
                          defaut="n").lower().startswith("o")
    lancer(args, interactif=True)


def analyseur():
    p = argparse.ArgumentParser(description="Extraits de blind test via Deezer")
    p.add_argument("fichier", nargs="?", help="fichier liste 'Artiste - Titre' (équivaut à --liste)")
    p.add_argument("--option", type=int, choices=[1, 2, 3], help="1 = 50×10 s, 2 = 35×20 s, 3 = 25×30 s")
    p.add_argument("--duree", type=int, choices=range(DUREE_MIN, DUREE_MAX + 1), metavar="N",
                   help=f"durée libre de la devinette ({DUREE_MIN} à {DUREE_MAX} s), à la place de --option")
    p.add_argument("--theme", help="thème libre, ex. 'années 80'")
    p.add_argument("--playlist", help="lien ou numéro de playlist Deezer")
    p.add_argument("--spotify", help="lien de playlist Spotify (titres retrouvés sur Deezer)")
    p.add_argument("--liste", help="fichier texte, une ligne 'Artiste - Titre' par morceau")
    p.add_argument("--essai", action="store_true", help="écrire dans essai_XXs, sans toucher à l'historique")
    p.add_argument("--creer-slides", action="store_true",
                   help="créer les slides d'intro et de fin (dossier slides)")
    p.add_argument("--creer-decomptes", action="store_true",
                   help="créer 'countdown 20.mp4' et 'countdown 30.mp4' sur l'image de fond")
    p.add_argument("--video-seule", action="store_true",
                   help="monter la vidéo à partir des extraits existants (avec --option)")
    p.add_argument("--annuler", action="store_true", help="annuler le dernier lancement de l'historique")
    p.add_argument("--debut", type=float, default=0, help="décalage de départ en s (durées de moins de 30 s)")
    p.add_argument("--max-artiste", type=int, default=2, help="titres max par artiste (défaut 2)")
    p.add_argument("--top", type=float, default=50, help="garder les X %% plus populaires (défaut 50)")
    p.add_argument("--blocage", type=int, default=2, help="lancements pendant lesquels un titre est bloqué (défaut 2)")
    p.add_argument("--nombre", type=int, help="forcer le nombre d'extraits (tests)")
    p.add_argument("--sans-slides", dest="slides", action="store_false",
                   help="ne pas ajouter les slides d'intro et de fin à la vidéo")
    p.add_argument("--sans-video", dest="video", action="store_false",
                   help="ne pas monter la vidéo YouTube")
    p.add_argument("--nom", default="blindtest", help="nom de la vidéo (défaut : blindtest)")
    p.add_argument("--oeuvre", dest="oeuvre", action="store_true", default=None,
                   help="afficher le film / la série / le spectacle dans les réponses (automatique pour ces thèmes)")
    p.add_argument("--sans-oeuvre", dest="oeuvre", action="store_false",
                   help="ne pas chercher l'œuvre, afficher « Artiste - Titre »")
    p.add_argument("--melanger", action="store_true", help="mode liste : mélanger l'ordre des titres")
    p.add_argument("--sans-repetition", action="store_true",
                   help="liste / Spotify : écarter les titres utilisés lors des derniers lancements "
                        "(pour découper une grande playlist en plusieurs vidéos)")
    p.add_argument("--sans-cache", action="store_true", help="ignorer le cache des playlists")
    return p


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    p = analyseur()
    args = p.parse_args()
    if args.fichier and not args.liste:
        args.liste = args.fichier

    interactif = len(sys.argv) == 1 or (args.liste and args.option is None and args.duree is None
                                       and not (args.theme or args.playlist))
    code = 0
    try:
        if len(sys.argv) == 1:
            menu(args)
        elif args.creer_slides:
            creer_slides()
        elif args.creer_decomptes:
            creer_decomptes()
        elif args.video_seule:
            if args.option is None and args.duree is None:
                p.error("indiquez le format : --video-seule --option 1, 2 ou 3 (ou --duree N).")
            video_seule(args.duree or FORMATS[args.option]["duree"], args.nom, args.oeuvre, args.slides)
        elif args.annuler:
            annuler_dernier()
        else:
            if sum(bool(x) for x in (args.liste, args.theme, args.playlist, args.spotify)) != 1:
                p.error("indiquez une seule source : --theme, --playlist, --spotify ou --liste.")
            if args.option is None and args.duree is None:
                if not interactif:
                    p.error("indiquez le format : --option 1, 2 ou 3 (ou --duree N).")
                afficher("Format : 1) 50 × 10 s   2) 35 × 20 s   3) 25 × 30 s")
                args.option = int(demander("Votre choix : ", {"1", "2", "3"}))
            if args.nombre is not None and args.nombre < 1:
                p.error("--nombre doit être au moins 1.")
            lancer(args, interactif=interactif)
    except Erreur as e:
        afficher(f"\nERREUR : {e}")
        code = 1
    except KeyboardInterrupt:
        afficher("\nInterrompu : les anciens fichiers et l'historique n'ont pas été modifiés.")
        code = 1
    except Exception as e:
        afficher(f"\nERREUR inattendue : {type(e).__name__} : {e}")
        code = 1
    if interactif:
        try:
            input("\nAppuyez sur Entrée pour fermer cette fenêtre...")
        except EOFError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
