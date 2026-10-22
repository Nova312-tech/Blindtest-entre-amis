#!/usr/bin/env python3
"""
Blind test en direct, façon Kahoot : l'écran principal (PC / TV) joue les extraits, chaque joueur
répond sur son téléphone parmi 4 propositions, résultats et classement entre chaque question.

  python jeu.py                       ATELIER : page web pour fabriquer les blind tests et lancer les parties
  python jeu.py --console             ancien mode : choix du lot dans cette fenêtre
  python jeu.py --dossier extraits_20s
  python jeu.py --nouveaux-choix      régénère les 4 propositions de chaque question

Les téléphones se connectent par le Wi-Fi de la maison, ou par Internet (tunnel Cloudflare).
Aucune installation : Python seul suffit (plus ffmpeg pour fabriquer les extraits).
"""

import argparse
from collections import Counter
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import blindtest as bt
import donnees_quiz as dq

DOSSIER_PAGES = bt.BASE / "jeu"
POINTS_MAX, POINTS_MIN = 1000, 500          # bonne réponse : de 1000 (immédiate) à 500 (dernière seconde)
PSEUDO_MAX = 16
JOUEURS_MAX = 100
ESSAIS_CODE_MAX = 10                            # codes faux autorisés par minute et par appareil


def afficher(msg=""):
    bt.afficher(msg)          # console, et journal de la page web pendant une préparation


# ---------- Les 4 propositions ----------

CYRILLIQUE = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґђјљњћџ",
                      ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                       "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g",
                       "dj", "j", "lj", "nj", "c", "dz"]))
GREC = dict(zip("αβγδεζηθικλμνξοπρσςτυφχψωάέήίόύώϊϋΐΰ",
                ["a", "v", "g", "d", "e", "z", "i", "th", "i", "k", "l", "m", "n", "x", "o", "p", "r", "s", "s", "t",
                 "y", "f", "ch", "ps", "o", "a", "e", "i", "i", "o", "y", "o", "i", "y", "i", "y"]))


def latin(texte):
    """Cyrillique et grec écrits en lettres latines (« Группа крови » -> « Gruppa krovi ») : sinon, au milieu de
    leurres en cyrillique, la seule proposition lisible serait la bonne."""
    sortie = []
    for ch in texte or "":
        bas = ch.lower()
        t = CYRILLIQUE.get(bas, GREC.get(bas))
        if t is None:
            sortie.append(ch)
        else:
            sortie.append(t.capitalize() if ch != bas else t)
    return "".join(sortie)


def ecriture_latine(texte):
    """Vrai si toutes les lettres sont latines (pas de chinois, japonais, coréen, arabe, hébreu, thaï...)."""
    return all(not ch.isalpha() or unicodedata.name(ch, "").startswith("LATIN") for ch in texte or "")


def titre_affiche(c):
    """Artiste et titre corrects : ceux de la liste (« Kino - Gruppa Krovi ») plutôt que ceux de Deezer
    (« Кино - Группа крови »), sans « Remastered »..., en lettres latines."""
    if c.get("oeuvre"):
        artiste, titre = c["artiste"].strip(), (c.get("titre_court") or c["titre"]).strip()
    else:
        artiste, titre = bt.artiste_titre(c)
        titre = c.get("titre_court") if c.get("titre_court") and not c.get("reponse") else titre
    return latin(artiste).strip(), latin(titre).strip()


def meme(a, b):
    return bt.normaliser(a) == bt.normaliser(b)


FORMES = {
    # forme : consigne affichée (None = « titre et artiste », pas de consigne particulière)
    "grille": None,                                   # 2 artistes x 2 titres
    "meme_artiste": "Quel est le titre ?",            # 1 artiste, 4 titres
    "meme_titre": "Qui chante ?",                     # 4 artistes, 1 titre
    "differents": None,                               # 4 artistes, 4 titres
}
# blind test à œuvres (comédies musicales, films) : les propositions portent sur le titre et l'œuvre,
# pas sur l'interprète (sinon « Rod Janois » et « la troupe de 1789 » seraient deux réponses justes)
CONSIGNES_OEUVRE = {"grille": None, "meme_artiste": "Quel est le titre ?",
                    "meme_titre": "De quel film ou spectacle ?", "differents": None}

# titres que beaucoup d'œuvres partagent : « Introduction » proposée avec 4 films serait juste 4 fois
GENERIQUES = re.compile(r"^(intro|introduction|ouverture|overture|prologue|epilogue|finale?|entracte|entr acte|"
                        r"main title|main theme|theme|end credits|suite|medley|opening|bows|exit music)\b")
MOTS_VIDES = {"the", "le", "la", "les", "l", "de", "du", "des", "of", "a", "an", "and", "et"}


def mots(s):
    return set(bt.normaliser(s).split()) - MOTS_VIDES


def meme_oeuvre(a, b):
    """« Le Roi Soleil » = « Le Roi Soleil: De Versailles à Monaco », « Mozart Opera Rock » = « Mozart l'Opéra Rock »,
    « Frozen » = « Frozen 2 » (trop proches pour être proposées l'une contre l'autre)."""
    if not bt.normaliser(a) or not bt.normaliser(b):
        return False
    if bt.normaliser(a) == bt.normaliser(b):
        return True
    petit, grand = sorted((mots(a), mots(b)), key=len)
    return bool(petit) and petit <= grand


def symetrique(choix):
    """Vrai si aucune proposition ne se distingue par le nombre d'apparitions de son artiste ou de son titre
    (sinon, un joueur malin trouverait la bonne réponse sans écouter)."""
    artistes = [bt.normaliser(a) for a, _ in choix]
    titres = [bt.normaliser_titre(t) for _, t in choix]
    signatures = {(artistes.count(a), titres.count(t)) for a, t in zip(artistes, titres)}
    distinctes = len({(a, t) for a, t in zip(artistes, titres)}) == len(choix)
    return len(choix) == 4 and len(signatures) == 1 and distinctes


# --- Profil des artistes (homme, femme, groupe) : quatre voix d'hommes et une de femme, ça se devine ---

MB_API = "https://musicbrainz.org/ws/2/artist/"
MB_UA = bt.MB_UA
FICHIER_PROFILS = bt.DOSSIER_CACHE / "profils_artistes.json"


class Profils:
    """Profil d'un artiste d'après MusicBrainz (base musicale ouverte), gardé en mémoire d'une fois sur l'autre.
    MusicBrainz n'accepte qu'une demande par seconde : la première préparation est un peu plus longue."""

    def __init__(self):
        self.verrou, self.reseau, self.dernier = threading.Lock(), threading.Lock(), 0.0
        try:
            self.connus = json.loads(FICHIER_PROFILS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.connus = {}
        self.nouveaux = 0

    def __call__(self, nom):
        cle = bt.normaliser(nom)
        if not cle:
            return "inconnu"
        if bt.COLLECTIFS.search(cle):
            return "groupe"
        with self.verrou:
            if cle in self.connus:
                return self.connus[cle]
        profil = self._chercher(nom, cle)
        if profil is None:                     # MusicBrainz injoignable : on réessaiera la prochaine fois
            return "inconnu"
        with self.verrou:
            self.connus[cle] = profil
            self.nouveaux += 1
        return profil

    def _chercher(self, nom, cle):
        q = urllib.parse.quote('artist:"' + nom.replace('"', "") + '"')
        artistes = None
        for _ in range(3):
            with self.reseau:                  # une demande par seconde, tous fils confondus
                pause = 1.1 - (time.monotonic() - self.dernier)
                if pause > 0:
                    time.sleep(pause)
                self.dernier = time.monotonic()
            try:
                req = urllib.request.Request(f"{MB_API}?query={q}&fmt=json&limit=8", headers=MB_UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    artistes = json.load(r).get("artists", [])
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    return None
                time.sleep(2)
            except (OSError, ValueError):
                time.sleep(2)
        if artistes is None:
            return None
        exacts = [a for a in artistes if a.get("score", 0) >= 90
                  and (bt.normaliser(a.get("name", "")) == cle
                       or any(bt.normaliser(x.get("name", "")) == cle for x in a.get("aliases") or []))]
        profils = {self._profil(a) for a in exacts[:3]}
        # homonymes de profils différents (« Six » : groupe ou chanteur ?) : on ne tranche pas
        return profils.pop() if len(profils) == 1 else "inconnu"

    @staticmethod
    def _profil(a):
        if a.get("type") in ("Group", "Orchestra", "Choir"):
            return "groupe"
        return {"male": "homme", "female": "femme"}.get((a.get("gender") or "").lower(), "inconnu")

    def enregistrer(self):
        if not self.nouveaux:
            return
        bt.DOSSIER_CACHE.mkdir(exist_ok=True)
        tmp = FICHIER_PROFILS.with_suffix(".tmp")
        with self.verrou:
            tmp.write_text(json.dumps(self.connus, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(FICHIER_PROFILS)


class Ressources:
    """Ce qu'on demande à Deezer, partagé entre les questions (une seule demande par artiste, album...)."""

    def __init__(self):
        self.pistes, self.albums, self.tops, self.proches, self.chante = {}, {}, {}, {}, {}
        self.profil = Profils()
        self.usage = Counter()                           # combien de fois chaque leurre a déjà servi
        self.verrou_leurres = threading.Lock()
        self._oeuvres = None

    def oeuvres_externes(self, pool):
        with self.verrou_leurres:
            if self._oeuvres is None:
                self._oeuvres = OeuvresExternes(pool)
            return self._oeuvres

    @staticmethod
    def _memo(cache, cle, calcul, defaut):
        if cle not in cache:
            try:
                cache[cle] = calcul()
            except Exception:
                cache[cle] = defaut
        return cache[cle]

    def piste(self, id_piste):
        return self._memo(self.pistes, id_piste, lambda: bt.api(f"/track/{id_piste}"), {})

    def album(self, id_album):
        return self._memo(self.albums, id_album,
                          lambda: bt.api(f"/album/{id_album}/tracks?limit=100").get("data", []), [])

    def top(self, id_artiste):
        return self._memo(self.tops, id_artiste, lambda: [
            x.get("title_short") or x["title"]
            for x in bt.api(f"/artist/{id_artiste}/top?limit=25").get("data", [])], [])

    def voisins(self, id_artiste):
        return self._memo(self.proches, id_artiste, lambda: [
            (x["id"], x["name"]) for x in bt.api(f"/artist/{id_artiste}/related?limit=20").get("data", [])], [])

    def a_chante(self, nom, titre):
        """Vrai si cet artiste a vraiment enregistré ce titre (reprise, autre version) : la proposition
        « Jo Stafford — Over the Rainbow » serait alors juste elle aussi."""
        cle = (bt.normaliser(nom), bt.normaliser_titre(titre))

        def chercher():
            q = urllib.parse.quote(f'artist:"{nom.replace(chr(34), "")}" track:"{titre.replace(chr(34), "")}"')
            return any(bt.normaliser_titre(p.get("title_short") or p.get("title", "")) == cle[1]
                       and bt.normaliser(p.get("artist", {}).get("name", "")) == cle[0]
                       for p in bt.api(f"/search?q={q}&limit=15").get("data", []))
        return self._memo(self.chante, cle, chercher, False)


VARIANTE = re.compile(r"\b(remix|rmx|mix|live|version|edit|acoustic|acoustique|instrumental|karaoke|sped up|slowed)\b",
                      re.I)


def propre(titre):
    """Titre affiché sur les cartes : sans « (feat. X) », « (From "Film") », « - Remastered 2011 »."""
    t = re.sub(r"\s*[\(\[]\s*(feat\.?|ft\.?|with|from|remaster)[^\)\]]*[\)\]]", "", titre, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+[^-–—]*\b(remaster\w*|version)\b.*$", "", t, flags=re.I)
    return t.strip() or titre


def originaux(titres):
    """Écarte remix, live, versions : un leurre « Tell Me Why (Dorado Remix) » se repère tout de suite."""
    return [t for t in titres if t and not VARIANTE.search(t)]


def distincts(titres, exclus):
    """Titres sans doublon (« Remastered », « Reprise » et accents ignorés), hors titres exclus."""
    vus, res = {bt.normaliser_titre(x) for x in exclus}, []
    for t in titres:
        k = bt.normaliser_titre(t or "")
        if k and k not in vus:
            vus.add(k)
            res.append(t)
    return res


def chansons_oeuvre(x, res):
    """Chansons de l'album de l'œuvre de x, seulement si c'est bien l'album de cette œuvre (pas une compilation
    où « Circle of Life » passerait pour une chanson de Cendrillon)."""
    oeuvre = x["oeuvre"]
    infos = res.piste(x["id"]).get("album") or {}
    album_propre = bt.nettoyer_oeuvre(infos.get("title", ""))
    if not (album_propre and meme_oeuvre(album_propre, oeuvre)):
        return []
    titres = []
    for p in res.album(infos.get("id")):
        cite = bt.oeuvre_citee(p.get("title", ""))
        if cite and not meme_oeuvre(cite, oeuvre):
            continue
        titres.append(bt.titre_sans_oeuvre(p.get("title_short") or p.get("title", ""), oeuvre))
    return [t for t in originaux(titres) if len(t) <= 45]


LEURRE_MAX = 2                                           # un même leurre : 2 fois maximum par partie


class OeuvresExternes:
    """Œuvres connues qui ne sont PAS dans le blind test (donnees_quiz.OEUVRES), pour servir de leurres :
    sinon, dans un blind test où chaque œuvre ne passe qu'une fois, la seule œuvre jamais sortie est la bonne."""

    def __init__(self, pool):
        du_lot = [c["oeuvre"] for c in pool if c.get("oeuvre")]
        fr = en = 0
        for o in set(du_lot):
            for nom, nom_fr, _, _ in dq.OEUVRES:
                if nom_fr and meme_oeuvre(o, nom_fr) and not meme_oeuvre(o, nom):
                    fr += 1
                elif meme_oeuvre(o, nom):
                    en += 1
        self.francais = fr > en                           # leurres écrits comme les œuvres du blind test
        # catégorie dominante du blind test : pour une œuvre inconnue du catalogue (pas de Peau d'âne parmi Broadway)
        cats = Counter(c for c in (self.categorie(o) for o in set(du_lot)) if c)
        self.cat_lot = cats.most_common(1)[0][0] if cats else None
        self.catalogue = [(nom_fr if self.francais and nom_fr else nom, cat, chansons)
                          for nom, nom_fr, cat, chansons in dq.OEUVRES
                          if not any(meme_oeuvre(o, nom) or (nom_fr and meme_oeuvre(o, nom_fr)) for o in du_lot)]

    @staticmethod
    def categorie(oeuvre):
        return next((cat for nom, nom_fr, cat, _ in dq.OEUVRES
                     if meme_oeuvre(oeuvre, nom) or (nom_fr and meme_oeuvre(oeuvre, nom_fr))), None)

    def choisir(self, oeuvre, n, res):
        """n œuvres de la même catégorie d'abord, les moins utilisées jusqu'ici dans la partie."""
        cat = self.categorie(oeuvre) or self.cat_lot
        ordre = ([cat] + dq.CATEGORIES_PROCHES.get(cat, [])) if cat else []
        with res.verrou_leurres:
            tri = sorted(self.catalogue, key=lambda x: (res.usage[x[0]] >= LEURRE_MAX,
                                                        ordre.index(x[1]) if x[1] in ordre else len(ordre),
                                                        res.usage[x[0]], random.random()))
            for x in tri[:3]:                             # compté tout de suite : les questions se préparent en parallèle
                res.usage[x[0]] += 1
            return tri[:n]

    @staticmethod
    def compter(noms, res):
        with res.verrou_leurres:
            for n in noms:
                res.usage[n] += 1


def reponses_a_venir(pool, rang):
    """Réponses des questions suivantes : jamais en leurre (ce serait leur donner la réponse d'avance)."""
    return {bt.normaliser_titre(x.get("titre_court") or x["titre"]) for x in pool[rang + 1:]}


def choix_oeuvres(c, pool, res, rang):
    """Formes « titre + œuvre ». Renvoie {forme: fabrique}."""
    oeuvre, titre = c["oeuvre"], (c.get("titre_court") or c["titre"]).strip()
    a_venir = reponses_a_venir(pool, rang)

    def hors_spoil(titres):
        return [t for t in titres if t and bt.normaliser_titre(t) not in a_venir]

    # autres chansons de la même œuvre : son album, et les titres de la partie déjà joués
    titres_oeuvre = distincts(hors_spoil(chansons_oeuvre(c, res) + [
        x.get("titre_court") for x in pool[:rang] if x.get("oeuvre") and meme_oeuvre(x["oeuvre"], oeuvre)]), [titre])
    random.shuffle(titres_oeuvre)
    # Leurres : surtout des œuvres HORS du blind test (même catégorie), et au plus UNE œuvre déjà sortie dans la
    # partie. Ainsi, il y a toujours au moins 2 propositions jamais sorties : « la seule nouvelle » ne marche plus.
    externes = res.oeuvres_externes(pool)
    autres = {}                                          # œuvre -> quelques-unes de ses chansons
    for nom, _, chansons in externes.choisir(oeuvre, 4, res):
        autres[nom] = [t for t in chansons if bt.normaliser_titre(t) != bt.normaliser_titre(titre)]
    deja = {}                                            # œuvres déjà sorties avant cette question
    for x in pool[:rang]:
        o = x.get("oeuvre")
        if not o or meme_oeuvre(o, oeuvre):
            continue
        cle = next((k for k in deja if meme_oeuvre(k, o)), o)
        deja.setdefault(cle, []).extend(hors_spoil(chansons_oeuvre(x, res) + [(x.get("titre_court") or x["titre"]).strip()]))
    noms = list(autres)
    random.shuffle(noms)
    pieges = [o for o in deja if res.usage[o] < LEURRE_MAX]
    if pieges and random.random() < 0.5:                 # une ancienne réponse, de temps en temps, comme piège
        ancienne = min(pieges, key=lambda o: (res.usage[o], random.random()))
        autres[ancienne] = deja[ancienne]
        noms.insert(random.randrange(min(3, len(noms)) + 1), ancienne)
    generique = bool(GENERIQUES.match(bt.normaliser_titre(titre)))

    def grille():
        if generique or not titres_oeuvre or not noms:
            return None
        o2, t2 = noms[0], titres_oeuvre[0]
        return [(oeuvre, titre), (oeuvre, t2), (o2, titre), (o2, t2)]

    def meme_artiste():
        return [(oeuvre, titre)] + [(oeuvre, t) for t in titres_oeuvre[:3]] if len(titres_oeuvre) >= 3 else None

    def meme_titre():
        return None if generique or len(noms) < 3 else [(oeuvre, titre)] + [(o, titre) for o in noms[:3]]

    def differents():
        choix, pris = [(oeuvre, titre)], [titre]
        for o in noms:
            t = next(iter(distincts(autres[o], pris)), None)
            if t and len(choix) < 4:
                choix.append((o, t))
                pris.append(t)
        return choix if len(choix) == 4 else None

    return {"grille": grille, "meme_artiste": meme_artiste, "meme_titre": meme_titre, "differents": differents}


def choix_artistes(c, pool, res, oeuvres_lot, suivi, rang):
    """Formes « titre + artiste », sans fausse proposition qui serait juste elle aussi, ni artiste qui
    détonne (voix de femme parmi trois voix d'hommes, troupe parmi des solistes)."""
    artiste, titre = titre_affiche(c)
    piste = res.piste(c["id"])
    aid = piste.get("artist", {}).get("id")
    album = res.album(piste.get("album", {}).get("id")) if piste.get("album") else []
    # ceux qui chantent sur le même album ou le même titre (membres du cast, invités) : jamais en leurre
    membres = {bt.normaliser(n) for n in [artiste] + [x.get("name", "") for x in piste.get("contributors", [])]
               + [p.get("artist", {}).get("name", "") for p in album]}
    collectif = bool(bt.COLLECTIFS.search(bt.normaliser(artiste)))
    profil_bonne = res.profil(artiste)

    def valable(nom, titres=()):
        n = bt.normaliser(nom)
        if not n or n in membres:
            return False
        if bool(bt.COLLECTIFS.search(n)) != collectif:           # troupe contre soliste
            return False
        if any(meme_oeuvre(nom, o) for o in oeuvres_lot):         # « Le Roi Soleil » n'est pas un chanteur
            return False
        if any(res.a_chante(nom, t) for t in titres):             # sa version existe : réponse juste aussi
            return False
        if profil_bonne != "inconnu" and res.profil(nom) != profil_bonne:
            return False
        return True

    a_venir = reponses_a_venir(pool, rang)

    def hors_spoil(titres):
        return [t for t in titres if t and bt.normaliser_titre(t) not in a_venir]

    autres_titres = distincts(hors_spoil(originaux(res.top(aid)[:15])), [titre]) if aid else []
    random.shuffle(autres_titres)
    voisins = [(i, n) for i, n in (res.voisins(aid) if aid else []) if not meme(n, artiste)]
    random.shuffle(voisins)
    lot_autres = [(i, x) for i, x in enumerate(pool) if x is not c and not meme(titre_affiche(x)[0], artiste)]
    random.shuffle(lot_autres)
    candidats = [n for _, n in voisins] or [titre_affiche(x)[0] for _, x in lot_autres]

    def chanson_de(i, x):
        """Titre déjà joué, sinon une autre chanson de cet artiste qui n'est pas dans la partie."""
        if i < rang:
            return titre_affiche(x)[1]
        id_x = res.piste(x["id"]).get("artist", {}).get("id")
        return next(iter(hors_spoil(originaux(res.top(id_x)[:10])) if id_x else []), None)

    def noms_valables(nb, titres=()):
        pris = []
        for n in candidats:
            if len(pris) == nb:
                break
            if not any(meme(n, x) for x in pris) and valable(n, titres):
                pris.append(n)
        return pris if len(pris) == nb else None

    def multi(choix):                                    # plusieurs artistes : profil inconnu = à vérifier
        if choix and profil_bonne == "inconnu":
            suivi["doute"] = True
        return choix

    def grille():
        if not autres_titres:
            return None
        t2 = autres_titres[0]
        b = noms_valables(1, (titre, t2))
        return multi([(artiste, titre), (artiste, t2), (b[0], titre), (b[0], t2)]) if b else None

    def meme_artiste():
        return [(artiste, titre)] + [(artiste, t) for t in autres_titres[:3]] if len(autres_titres) >= 3 else None

    def meme_titre():
        noms = None if GENERIQUES.match(bt.normaliser_titre(titre)) else noms_valables(3, (titre,))
        return multi([(artiste, titre)] + [(n, titre) for n in noms]) if noms else None

    def differents():
        choix, pris = [(artiste, titre)], [titre]
        for id_v, nom in voisins:                        # artistes proches avec une de leurs chansons
            if len(choix) == 4:
                break
            chansons = distincts(hors_spoil(originaux(res.top(id_v)[:10])), pris)
            if chansons and not any(meme(nom, a) for a, _ in choix) and valable(nom):
                t = random.choice(chansons[:6])
                choix.append((nom, t))
                pris.append(t)
        for i, x in lot_autres:                          # complément : autres artistes de la partie
            if len(choix) == 4:
                break
            a = titre_affiche(x)[0]
            if any(meme(a, y) for y, _ in choix) or not valable(a):
                continue
            t = chanson_de(i, x)
            if t and distincts([t], pris):
                choix.append((a, t))
                pris.append(t)
        return multi(choix) if len(choix) == 4 else None

    return {"grille": grille, "meme_artiste": meme_artiste, "meme_titre": meme_titre, "differents": differents}


def proposer(c, pool, res, oeuvres_lot, preferee=None, rang=0):
    """4 propositions (dont la bonne) selon la forme préférée, ou une autre si elle est impossible.
    Renvoie un dict : forme, mode (« oeuvre » ou « artiste »), choix, bonne, doute."""
    bt.verifier_annulation()
    ordre = list(FORMES)
    random.shuffle(ordre)                                # les autres formes en repli, dans le désordre
    if preferee in ordre:
        ordre.remove(preferee)
        ordre.insert(0, preferee)
    essais = []
    if c.get("oeuvre"):
        essais.append(("oeuvre", (c["oeuvre"], (c.get("titre_court") or c["titre"]).strip()),
                       lambda s: choix_oeuvres(c, pool, res, rang)))
    essais.append(("artiste", titre_affiche(c), lambda s: choix_artistes(c, pool, res, oeuvres_lot, s, rang)))
    for mode, bonne, fabrique in essais:
        suivi = {"doute": False}
        fabriques = fabrique(suivi)
        for forme in ordre:
            suivi["doute"] = False
            choix = fabriques[forme]()
            if choix and symetrique(choix):
                choix = [(latin(a), latin(propre(t))) for a, t in choix]
                # une proposition en chinois, arabe... au milieu des autres en lettres latines se repère : forme écartée
                if not all(ecriture_latine(a) and ecriture_latine(t) for a, t in choix):
                    continue
                if symetrique(choix):
                    return {"forme": forme, "mode": mode, "choix": choix, "bonne": (latin(bonne[0]), latin(propre(bonne[1]))),
                            "doute": suivi["doute"]}
    return {"forme": None, "mode": "artiste", "choix": [titre_affiche(c)], "bonne": titre_affiche(c),
            "doute": True}


VERSION_QUIZ = 4        # 3 : œuvres, membres du cast, profils ; 4 : leurres hors du blind test (anti-déduction)


def faire_question(n, c, r):
    """Question du quiz (et éventuelle remarque « à vérifier ») à partir du résultat de proposer()."""
    choix = r["choix"]
    random.shuffle(choix)
    remarque = None
    if r["forme"] is None:
        afficher(f"  Attention : impossible de préparer 4 propositions équilibrées pour la question {n}.")
        remarque = f"{n:02d}. {bt.texte_reponse(c)} : pas de propositions équilibrées possibles"
    elif r["doute"]:
        remarque = (f"{n:02d}. {bt.texte_reponse(c)} : profil de l'artiste inconnu (homme, femme, groupe ?), "
                    "propositions à vérifier")
    consignes = CONSIGNES_OEUVRE if r["mode"] == "oeuvre" else FORMES
    question = {"numero": n, "audio": f"{n:02d}.mp3", "forme": r["forme"], "mode": r["mode"],
                "consigne": consignes.get(r["forme"]),
                "choix": [{"artiste": a, "titre": t} for a, t in choix],
                "bonne": choix.index(r["bonne"]),
                "info": bt.texte_reponse(c) if c.get("oeuvre") else None}
    return question, remarque


def ecrire_quiz(dossier, quiz, a_verifier):
    fichier = dossier / "quiz.json"
    tmp = fichier.with_suffix(".tmp")
    tmp.write_text(json.dumps(quiz, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fichier)
    verif = dossier / "propositions_a_verifier.txt"
    if a_verifier:
        verif.write_text("Questions du jeu à vérifier avant de jouer\n\n" + "\n".join(sorted(a_verifier)) + "\n",
                         encoding="utf-8")
    else:
        verif.unlink(missing_ok=True)


def preparer_quiz(dossier, regenerer=False):
    lot_json = dossier / "lot.json"
    if not lot_json.exists():
        raise bt.Erreur(f"Pas de lot.json dans {dossier.name} : fabriquez d'abord un blind test avec blindtest.py.")
    lot = json.loads(lot_json.read_text(encoding="utf-8"))
    empreinte = [c.get("id") for c in lot]
    fichier = dossier / "quiz.json"
    if fichier.exists() and not regenerer:
        quiz = json.loads(fichier.read_text(encoding="utf-8"))
        # propositions d'une version plus ancienne (moins fiables) : refaites automatiquement
        if quiz.get("empreinte") == empreinte and quiz.get("version") == VERSION_QUIZ:
            afficher(f"Propositions déjà prêtes ({len(quiz['questions'])} questions).")
            return quiz
    if not all(empreinte):
        raise bt.Erreur("Ce lot ne contient pas les identifiants Deezer nécessaires pour préparer les choix.")
    afficher(f"Préparation des 4 propositions pour {len(lot)} questions (Deezer et MusicBrainz, 1 à 3 min)...")
    res = Ressources()
    oeuvres_lot = {c["oeuvre"] for c in lot if c.get("oeuvre")}
    # formes distribuées comme un paquet de cartes mélangé : aléatoire, mais équilibré sur la partie
    paquet = []
    while len(paquet) < len(lot):
        tour = list(FORMES)
        random.shuffle(tour)
        paquet += tour
    with ThreadPoolExecutor(bt.PARALLELE) as ex:
        resultats = list(ex.map(lambda r: proposer(lot[r], lot, res, oeuvres_lot, paquet[r], r), range(len(lot))))
    res.profil.enregistrer()
    questions, formes, a_verifier = [], [], []
    for n, (c, r) in enumerate(zip(lot, resultats), 1):
        question, remarque = faire_question(n, c, r)
        questions.append(question)
        formes.append(r["forme"])
        if remarque:
            a_verifier.append(remarque)
    quiz = {"version": VERSION_QUIZ, "empreinte": empreinte, "questions": questions}
    ecrire_quiz(dossier, quiz, a_verifier)
    noms = {"grille": "grille 2x2", "meme_artiste": "1 artiste", "meme_titre": "4 artistes",
            "differents": "4 différents"}
    repartition = ", ".join(f"{noms[f]} : {formes.count(f)}" for f in FORMES if formes.count(f))
    nb_oeuvres = sum(1 for q in questions if q["mode"] == "oeuvre")
    afficher(f"Propositions prêtes ({repartition}" + (f" ; {nb_oeuvres} sur l'œuvre" if nb_oeuvres else "")
             + f"), enregistrées dans {dossier.name}/quiz.json.")
    if a_verifier:
        afficher(f"  {len(a_verifier)} question(s) à vérifier : voir propositions_a_verifier.txt")
    return quiz


def actualiser_quiz(dossier, numeros):
    """Après une retouche dans l'éditeur : refait seulement les questions modifiées (même forme qu'avant).
    Sans quiz utilisable, prépare tout."""
    lot = json.loads((dossier / "lot.json").read_text(encoding="utf-8"))
    try:
        quiz = json.loads((dossier / "quiz.json").read_text(encoding="utf-8"))
        utilisable = quiz.get("version") == VERSION_QUIZ and len(quiz.get("questions", [])) == len(lot)
    except (OSError, ValueError):
        utilisable = False
    if not utilisable:
        return preparer_quiz(dossier, True) if all(c.get("id") for c in lot) else None
    numeros = sorted({n for n in numeros if 1 <= n <= len(lot)})
    afficher(f"Mise à jour du quiz : question(s) {', '.join(f'{n:02d}' for n in numeros)}...")
    res = Ressources()
    oeuvres_lot = {c["oeuvre"] for c in lot if c.get("oeuvre")}
    try:
        verif = (dossier / "propositions_a_verifier.txt").read_text(encoding="utf-8").splitlines()
        a_verifier = [l for l in verif if re.match(r"\d+\.", l) and int(l.split(".")[0]) not in numeros]
    except OSError:
        a_verifier = []
    for n in numeros:
        ancienne = quiz["questions"][n - 1]
        r = proposer(lot[n - 1], lot, res, oeuvres_lot, ancienne.get("forme"), n - 1)
        question, remarque = faire_question(n, lot[n - 1], r)
        quiz["questions"][n - 1] = question
        if remarque:
            a_verifier.append(remarque)
    res.profil.enregistrer()
    quiz["empreinte"] = [c.get("id") for c in lot]
    ecrire_quiz(dossier, quiz, a_verifier)
    afficher("Quiz à jour.")
    return quiz


# ---------- Modes de jeu : données en plus (pochette, année, langue, pays) ----------

MODES = {
    "classique": ("Classique", "4 propositions, les points baissent avec le temps"),
    "progressif": ("Lettre par lettre", "les 4 propositions s'écrivent lettre par lettre pendant l'extrait"),
    "annee": ("Trouver l'année", "chacun propose une année de sortie ; plus on est proche, plus on marque"),
    "langue": ("Quelle langue ?", "dans quelle langue la chanson est-elle chantée ?"),
    "pays": ("Quel pays ?", "de quel pays vient l'artiste ?"),
}
CONSIGNES_MODE = {"annee": "En quelle année est sorti ce titre ?",
                  "langue": "Dans quelle langue est chantée cette chanson ?",
                  "pays": "De quel pays vient l'artiste ?"}
NOMS_LANGUE_JEU = {"fr": "Français", "en": "Anglais", "es": "Espagnol", "it": "Italien", "de": "Allemand",
                   "pt": "Portugais", "kr": "Coréen", "jp": "Japonais", "ar": "Arabe"}
NOMS_PAYS = {
    "FR": "France", "BE": "Belgique", "CH": "Suisse", "LU": "Luxembourg", "MC": "Monaco", "CA": "Canada",
    "US": "États-Unis", "GB": "Royaume-Uni", "IE": "Irlande", "AU": "Australie", "NZ": "Nouvelle-Zélande",
    "DE": "Allemagne", "AT": "Autriche", "IT": "Italie", "ES": "Espagne", "PT": "Portugal", "NL": "Pays-Bas",
    "SE": "Suède", "NO": "Norvège", "DK": "Danemark", "FI": "Finlande", "IS": "Islande", "PL": "Pologne",
    "CZ": "Tchéquie", "HU": "Hongrie", "GR": "Grèce", "TR": "Turquie", "RU": "Russie", "UA": "Ukraine",
    "RO": "Roumanie", "HR": "Croatie", "RS": "Serbie", "BR": "Brésil", "AR": "Argentine", "MX": "Mexique",
    "CO": "Colombie", "CL": "Chili", "PE": "Pérou", "VE": "Venezuela", "CU": "Cuba", "PR": "Porto Rico",
    "DO": "République dominicaine", "JM": "Jamaïque", "TT": "Trinité-et-Tobago", "BB": "Barbade",
    "JP": "Japon", "KR": "Corée du Sud", "CN": "Chine", "IN": "Inde", "IL": "Israël", "LB": "Liban",
    "EG": "Égypte", "MA": "Maroc", "DZ": "Algérie", "TN": "Tunisie", "SN": "Sénégal", "CI": "Côte d'Ivoire",
    "CM": "Cameroun", "CD": "RD Congo", "ML": "Mali", "NG": "Nigeria", "GH": "Ghana", "ZA": "Afrique du Sud",
    "HT": "Haïti", "CV": "Cap-Vert", "BG": "Bulgarie", "SK": "Slovaquie", "SI": "Slovénie", "EE": "Estonie",
    "LV": "Lettonie", "LT": "Lituanie", "BY": "Biélorussie", "AM": "Arménie", "AL": "Albanie", "KZ": "Kazakhstan",
}
CODES_LANGUE = {v.lower(): k for k, v in NOMS_LANGUE_JEU.items()}   # « Français » -> fr (évite les doublons)
NOMS_PAYS.update({
    "MT": "Malte", "BA": "Bosnie-Herzégovine", "MK": "Macédoine du Nord", "MD": "Moldavie", "SY": "Syrie",
    "JO": "Jordanie", "IQ": "Irak", "IR": "Iran", "SA": "Arabie saoudite", "AE": "Émirats arabes unis",
    "GE": "Géorgie", "AZ": "Azerbaïdjan", "KE": "Kenya", "TZ": "Tanzanie", "ET": "Éthiopie", "MG": "Madagascar",
    "AO": "Angola", "GN": "Guinée", "UY": "Uruguay", "TW": "Taïwan", "HK": "Hong Kong", "PK": "Pakistan",
    "VN": "Viêt Nam", "TH": "Thaïlande", "PH": "Philippines", "ID": "Indonésie", "MY": "Malaisie", "MN": "Mongolie",
})
PAYS_COURANTS = ["FR", "US", "GB", "CA", "BE", "DE", "IT", "ES", "SE", "AU", "IE", "NL", "BR", "JP", "KR"]
LANGUES_COURANTES = ["en", "fr", "es", "it", "de", "pt"]
FICHIER_ANNEES = bt.DOSSIER_CACHE / "annees.json"


def drapeau(code):
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in code) if len(code or "") == 2 and code.isalpha() else ""


def nom_valeur(mode, v):
    if mode == "pays":
        return f"{drapeau(v)} {NOMS_PAYS.get(v, v)}".strip()
    return NOMS_LANGUE_JEU.get(v, v)


class Annees:
    """Année de première sortie d'un titre d'après MusicBrainz (bien plus juste que la date d'une compilation
    ou d'un remaster sur Deezer), gardée en mémoire d'une fois sur l'autre (cache/annees.json)."""

    def __init__(self):
        try:
            self.connues = json.loads(FICHIER_ANNEES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.connues = {}
        self.dernier, self.nouvelles, self.echecs = 0.0, 0, 0

    def __call__(self, artiste, titre):
        cle = f"{bt.normaliser(artiste)}|{bt.normaliser_titre(titre)}"
        if cle in self.connues:
            return self.connues[cle] or None
        if self.echecs >= 3:                              # MusicBrainz injoignable : on n'insiste pas
            return None
        annee = self._chercher(artiste, titre)
        if annee is None:
            self.echecs += 1
            return None
        self.echecs = 0
        self.connues[cle] = annee
        self.nouvelles += 1
        return annee or None

    def _chercher(self, artiste, titre):
        propre = re.sub(r"\s*[\(\[].*?[\)\]]", "", titre).split(" - ")[0].strip() or titre
        q = urllib.parse.quote(f'recording:"{propre.replace(chr(34), "")}" AND artist:"{artiste.replace(chr(34), "")}"')
        for _ in range(3):
            bt.verifier_annulation()
            pause = 1.1 - (time.monotonic() - self.dernier)
            if pause > 0:
                time.sleep(pause)
            self.dernier = time.monotonic()
            try:
                req = urllib.request.Request(f"https://musicbrainz.org/ws/2/recording/?query={q}&fmt=json&limit=25",
                                             headers=MB_UA)
                with urllib.request.urlopen(req, timeout=15) as r:
                    recs = json.load(r).get("recordings", [])
                break
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503):
                    return None
                time.sleep(2)
            except (OSError, ValueError):
                time.sleep(2)
        else:
            return None
        voulu_t, voulu_a = bt.normaliser_titre(propre), bt.normaliser(artiste)
        annees = []
        for r in recs:
            credit = " ".join(bt.normaliser(a.get("name", "")) for a in r.get("artist-credit") or [] if isinstance(a, dict))
            date = r.get("first-release-date") or ""
            if (r.get("score", 0) >= 80 and bt.normaliser_titre(r.get("title", "")) == voulu_t
                    and voulu_a and voulu_a in credit and re.match(r"\d{4}", date)):
                annees.append(int(date[:4]))
        return min(annees) if annees else 0

    def enregistrer(self):
        if self.nouvelles:
            bt.DOSSIER_CACHE.mkdir(exist_ok=True)
            tmp = FICHIER_ANNEES.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.connues, ensure_ascii=False, indent=0), encoding="utf-8")
            os.replace(tmp, FICHIER_ANNEES)
            self.nouvelles = 0


def artiste_et_titre(c):
    """Artiste qui chante et titre affiché (œuvre : l'interprète)."""
    artiste, titre = bt.artiste_titre(c)
    if c.get("oeuvre"):
        artiste, titre = c.get("interprete") or c.get("artiste") or artiste, c.get("titre_court") or titre
    return artiste, titre


def enrichir_quiz(dossier, quiz, besoins):
    """Ajoute aux questions ce dont la partie a besoin : la pochette (toujours), et selon le mode l'année de sortie,
    le pays de l'artiste ou la langue. Gardé dans quiz.json : la deuxième fois, c'est immédiat."""
    lot = json.loads((dossier / "lot.json").read_text(encoding="utf-8"))
    qs = quiz["questions"]
    change = False
    sans = [i for i, q in enumerate(qs) if "pochette" not in q]
    if sans:
        afficher(f"Pochettes des albums ({len(sans)})...")

        def piste(i):
            try:
                return bt.api(f"/track/{lot[i]['id']}") if lot[i].get("id") else {}
            except (bt.Erreur, bt.Introuvable):
                return {}

        with ThreadPoolExecutor(bt.PARALLELE) as ex:
            for i, p in zip(sans, ex.map(piste, sans)):
                album = p.get("album") or {}
                qs[i]["pochette"] = album.get("cover_big") or album.get("cover_medium")
                date = p.get("release_date") or ""
                # date Deezer : seulement en secours, et jamais celle d'une compilation
                qs[i]["annee_deezer"] = int(date[:4]) if re.match(r"\d{4}", date) and album.get("type") != "compile" \
                    and "compil" not in bt.normaliser(album.get("title", "")) else None
        change = True
    if "annee" in besoins and any("annee" not in q for q in qs):
        annees = Annees()
        a_faire = [i for i, q in enumerate(qs) if "annee" not in q]
        afficher(f"Années de sortie ({len(a_faire)} titres, MusicBrainz : environ {len(a_faire)} s la première fois)...")
        for i in a_faire:
            artiste, titre = artiste_et_titre(lot[i])
            qs[i]["annee"] = annees(artiste, titre) or qs[i].get("annee_deezer")
        annees.enregistrer()
        change = True
    if besoins & {"pays", "langue"} and any("pays" not in q for q in qs):
        pays = bt.PaysArtistes()
        a_faire = [i for i, q in enumerate(qs) if "pays" not in q]
        afficher(f"Pays des artistes ({len(a_faire)} titres, MusicBrainz)...")
        echecs = 0
        for i in a_faire:
            artiste = artiste_et_titre(lot[i])[0]
            code = pays(artiste) if echecs < 3 or pays.connu(artiste) else None
            if code is None and not pays.connu(artiste):
                echecs += 1
            qs[i]["pays"] = code or None
        pays.enregistrer()
        change = True
    if "langue" in besoins and any("langue" not in q for q in qs):
        for i, q in enumerate(qs):
            if "langue" in q:
                continue
            # liste « Artiste - Titre | Langue » : la langue écrite dans la liste fait foi
            note = (lot[i].get("note") or "").strip()
            if note and not any(ch.isdigit() for ch in note):
                q["langue"] = CODES_LANGUE.get(note.lower(), note)
                continue
            lt = bt.langue_titre(artiste_et_titre(lot[i])[1])
            if not lt and q.get("pays"):                  # titre sans indice : la langue du pays, s'il n'en a qu'une
                ls = bt.LANGUES_PAYS.get(q["pays"])
                lt = next(iter(ls)) if ls and len(ls) == 1 else None
            q["langue"] = lt
        change = True
    if change:
        tmp = (dossier / "quiz.json").with_suffix(".tmp")
        tmp.write_text(json.dumps(quiz, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(dossier / "quiz.json")
    return quiz


def leurres_mode(mode, v, passees, toutes, usage):
    """3 leurres pour « Quelle langue ? » / « Quel pays ? » : au plus une réponse déjà sortie (piège de mémoire),
    le reste HORS du blind test, de la même famille de langues / région d'abord, chacun 2 fois maximum."""
    if mode == "langue":
        groupe, proches, catalogue = dq.LANGUES_MONDE.get, dq.FAMILLES_PROCHES, list(dq.LANGUES_MONDE)
    else:
        groupe, proches, catalogue = dq.REGIONS_PAYS.get, dq.REGIONS_PROCHES, list(dq.REGIONS_PAYS)
    g = groupe(v)
    ordre = ([g] + proches.get(g, [])) if g else []
    choix = []
    pieges = [x for x in dict.fromkeys(passees) if usage[x] < LEURRE_MAX]
    if pieges and random.random() < 0.5:                # une ancienne réponse comme piège, parmi les moins utilisées
        choix.append(min(pieges, key=lambda x: (usage[x], random.random())))
    candidats = [x for x in catalogue if x not in toutes and x != v]
    candidats.sort(key=lambda x: (usage[x] >= LEURRE_MAX, ordre.index(groupe(x)) if groupe(x) in ordre else len(ordre),
                                  usage[x], random.random()))
    for x in candidats:
        if len(choix) == 3:
            break
        if nom_valeur(mode, x) not in {nom_valeur(mode, y) for y in choix + [v]}:
            choix.append(x)
    for x in choix:
        usage[x] += 1
    return choix


def questions_du_mode(quiz, dossier, mode):
    """Questions de la partie selon le mode. Les titres sans donnée sûre (année, pays...) sont laissés de côté."""
    lot = json.loads((dossier / "lot.json").read_text(encoding="utf-8"))
    qs = quiz["questions"]
    res = []
    valeur = (lambda v: NOMS_LANGUE_JEU.get(v, v)) if mode == "langue" else (lambda v: v)
    toutes = {valeur(q[mode]) for q in qs if q.get(mode)} if mode in ("langue", "pays") else set()
    passees, usage = [], Counter()
    for q, c in zip(qs, lot):
        chanson = bt.texte_reponse(c)
        if mode in ("classique", "progressif"):
            x = dict(q, chanson=chanson)
            if mode == "progressif":
                x["lettres"] = 0.75                         # texte complet aux 3/4 de l'extrait (fraction de la durée)
        elif mode == "annee":
            if not q.get("annee"):
                continue
            x = {"type": "annee", "annee": q["annee"], "consigne": CONSIGNES_MODE["annee"]}
        else:
            if not q.get(mode):
                continue
            v = valeur(q[mode])
            options = [v] + leurres_mode(mode, v, [p for p in passees if p != v], toutes, usage)
            passees.append(v)
            random.shuffle(options)
            x = {"type": "choix", "forme": "mode", "consigne": CONSIGNES_MODE[mode], "bonne": options.index(v),
                 "choix": [{"titre": nom_valeur(mode, o), "artiste": ""} for o in options]}
        x.update(numero=q["numero"], audio=q["audio"], pochette=q.get("pochette"), chanson=chanson,
                 info=q.get("info") if mode in ("classique", "progressif") else chanson)
        res.append(x)
    return res


# ---------- La partie ----------

# Sauvegarde de la partie en cours (scores, joueurs, question atteinte) à chaque changement : si la fenêtre se
# ferme ou si le PC plante, l'atelier propose de la reprendre, avec le même code (les téléphones se reconnectent).
FICHIER_PARTIE = bt.BASE / "_partie_en_cours.json"
EQUIPES = [{"nom": "Rouges", "couleur": "#e5486b"}, {"nom": "Bleus", "couleur": "#3f8cff"},
           {"nom": "Jaunes", "couleur": "#f2b632"}, {"nom": "Verts", "couleur": "#2fbf71"}]
SERIE_BONUS, SERIE_BONUS_MAX = 100, 500
JOURNAL_DETAILLE = True                                   # une ligne par question dans journal.txt                  # +100 par bonne réponse de suite (à partir de 2), 500 max
CHAMPS_JOUEUR = ("jeton", "pseudo", "score", "arrivee", "equipe", "serie", "bonnes", "serie_max", "plus_rapide",
                 "repondues")


def lire_sauvegarde():
    """Partie interrompue qu'on peut reprendre (même lot), sinon None."""
    try:
        data = json.loads(FICHIER_PARTIE.read_text(encoding="utf-8"))
        dossier = Path(data["dossier"])
        lot = json.loads((dossier / "lot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if [c.get("id") for c in lot] != data.get("empreinte") or not data.get("questions"):
        return None
    if not data.get("joueurs") or data.get("suite", 0) >= len(data["questions"]):
        return None
    return data


def points_annee(ecart):
    """Mode année : 1000 pile, puis de moins en moins (rien au-delà de 10 ans d'écart)."""
    return {0: 1000, 1: 750, 2: 500, 3: 300, 4: 200, 5: 120}.get(ecart, 50 if ecart <= 10 else 0)


def joueur_neuf(jeton, pseudo, arrivee):
    return {"jeton": jeton, "pseudo": pseudo, "score": 0, "arrivee": arrivee, "dernier": None, "equipe": None,
            "serie": 0, "bonnes": 0, "serie_max": 0, "plus_rapide": None, "repondues": 0, "rang_avant": None}


class Partie:
    def __init__(self, quiz, duree, dossier, ident, code, precedente=None, mode="classique", equipes=0):
        self.questions = [dict(q) for q in quiz["questions"]]
        self.mode = mode if mode in MODES else "classique"
        self.nb_equipes = equipes if equipes in (2, 3, 4) else 0
        self.duree = duree
        self.dossier = dossier
        self.id = ident                                  # change à chaque lancement du serveur
        self.code = code                                 # code de partie à 6 chiffres (façon Kahoot)
        self.echecs = {}                                 # appareil -> heures des codes faux récents
        self.verrou = threading.Condition()
        self.version = 0
        self.joueurs = {}                                # jeton -> joueur
        self.stats = []                                  # une ligne par question jouée (fin de partie)
        if precedente:
            # nouvelle partie dans la même soirée : les joueurs inscrits restent, leurs scores repartent à 0
            with precedente.verrou:
                if precedente.minuteur:
                    precedente.minuteur.cancel()
                self.version = precedente.version + 1
                for t, j in precedente.joueurs.items():
                    n = joueur_neuf(t, j["pseudo"], j["arrivee"])
                    if self.nb_equipes and j.get("equipe") is not None and j["equipe"] < self.nb_equipes:
                        n["equipe"] = j["equipe"]
                    self.joueurs[t] = n
                # l'ancienne partie est déclarée « remplacée » par installer_partie, APRÈS la mise en place de
                # celle-ci : sinon les écrans réveillés liraient encore l'ancienne et rateraient la salle d'attente
        self.manche = secrets.token_hex(4)               # identifie cette partie (une nouvelle question 1 ≠ l'ancienne)
        self._equilibrer()
        # questions à points doubles : environ une sur dix, jamais la première, annoncées à l'écran
        if len(self.questions) >= 6 and not any(q.get("double") for q in self.questions):
            for i in random.sample(range(1, len(self.questions)), max(1, len(self.questions) // 10)):
                self.questions[i]["double"] = True
        self.phase = "attente"                           # attente, question, resultats, fin
        self.idx = -1
        self.debut = 0.0
        self.votes = {}                                  # jeton -> (choix, secondes)
        self.resultats = None
        self.minuteur = None
        self.pause_depuis = None                         # question en pause (invités qui arrivent, bruit...)
        try:
            self.empreinte = [c.get("id") for c in json.loads((dossier / "lot.json").read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            self.empreinte = None

    def reprendre(self, data):
        """Remet les joueurs, leurs scores et leurs statistiques d'une partie interrompue ; elle repart de la salle
        d'attente, et « Reprendre » lance la question qui suit la dernière terminée (une question coupée est rejouée)."""
        self.joueurs = {}
        for j in data["joueurs"]:
            n = joueur_neuf(j["jeton"], j["pseudo"], j["arrivee"])
            n.update({k: j[k] for k in CHAMPS_JOUEUR if k in j})
            self.joueurs[j["jeton"]] = n
        self.stats = data.get("stats") or []
        self.idx = data["suite"] - 1
        self._equilibrer()

    # --- utilitaires (à appeler verrou pris) ---
    def _changer(self):
        self.version += 1
        self.verrou.notify_all()
        self._sauver()

    def _sauver(self):
        if self.phase == "remplacee" or not self.empreinte:
            return
        try:
            if self.phase == "fin":
                FICHIER_PARTIE.unlink(missing_ok=True)
                return
            if not self.joueurs:
                return
            # prochaine question à jouer : celle en cours si elle a été coupée, sinon la suivante
            suite = self.idx + 1 if self.phase in ("resultats", "attente") else max(self.idx, 0)
            data = {"dossier": str(self.dossier), "empreinte": self.empreinte, "id": self.id, "code": self.code,
                    "suite": suite, "total": len(self.questions), "date": time.strftime("%d/%m/%Y %H:%M"),
                    "mode": self.mode, "equipes": self.nb_equipes, "questions": self.questions,
                    "stats": self.stats[:suite],
                    "joueurs": [{k: j.get(k) for k in CHAMPS_JOUEUR} for j in self.joueurs.values()]}
            tmp = FICHIER_PARTIE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, FICHIER_PARTIE)
        except OSError:
            pass                                         # la partie continue, même sans sauvegarde

    def _classement(self):
        return sorted(self.joueurs.values(), key=lambda j: (-j["score"], j["arrivee"]))

    def _equilibrer(self):
        """Mode équipes : chaque joueur sans équipe rejoint la moins nombreuse."""
        if not self.nb_equipes:
            for j in self.joueurs.values():
                j["equipe"] = None
            return
        for j in sorted(self.joueurs.values(), key=lambda x: x["arrivee"]):
            if j.get("equipe") is None:
                tailles = [sum(1 for x in self.joueurs.values() if x.get("equipe") == e) for e in range(self.nb_equipes)]
                j["equipe"] = tailles.index(min(tailles))

    def _equipes(self):
        res = []
        for e in range(self.nb_equipes):
            membres = [j for j in self.joueurs.values() if j.get("equipe") == e]
            # score d'équipe : la moyenne de ses joueurs (juste même si les équipes n'ont pas la même taille)
            res.append(dict(EQUIPES[e], indice=e, membres=len(membres),
                            score=round(sum(j["score"] for j in membres) / len(membres)) if membres else 0,
                            gain=round(sum((self.resultats or {}).get("gains", {}).get(j["jeton"], 0) for j in membres)
                                       / len(membres)) if membres and self.phase == "resultats" else None))
        return sorted(res, key=lambda x: -x["score"])

    # --- actions des joueurs ---
    def rejoindre(self, pseudo, jeton=None, code=None, appareil=""):
        """Renvoie (joueur, None) ou (None, message d'erreur)."""
        with self.verrou:
            if jeton in self.joueurs:
                return self.joueurs[jeton], None
            maintenant = time.monotonic()
            recents = [t for t in self.echecs.get(appareil, []) if maintenant - t < 60]
            if len(recents) >= ESSAIS_CODE_MAX:
                return None, "Trop d'essais : attends une minute."
            if str(code or "").strip() != self.code:
                self.echecs[appareil] = recents + [maintenant]
                return None, "Code de partie incorrect."
            if len(self.joueurs) >= JOUEURS_MAX:
                return None, "La partie est complète."
            pseudo = "".join(ch for ch in str(pseudo or "") if ch.isprintable())
            pseudo = " ".join(pseudo.split())[:PSEUDO_MAX] or "Joueur"
            base, n = pseudo, 2
            while any(j["pseudo"].lower() == pseudo.lower() for j in self.joueurs.values()):
                pseudo = f"{base[:PSEUDO_MAX - 3]} {n}"
                n += 1
            jeton = secrets.token_urlsafe(12)
            self.joueurs[jeton] = joueur_neuf(jeton, pseudo, time.time())
            self._equilibrer()
            self._changer()
            return self.joueurs[jeton], None

    def choisir_equipe(self, jeton, equipe):
        with self.verrou:
            if (self.phase != "attente" or jeton not in self.joueurs or not isinstance(equipe, int)
                    or not 0 <= equipe < self.nb_equipes):
                return False
            self.joueurs[jeton]["equipe"] = equipe
            self._changer()
            return True

    def voter(self, jeton, choix):
        with self.verrou:
            if (self.phase != "question" or self.pause_depuis is not None or jeton not in self.joueurs
                    or jeton in self.votes or isinstance(choix, bool) or not isinstance(choix, int)):
                return False
            q = self.questions[self.idx]
            ecoule = time.monotonic() - self.debut
            if q.get("type") == "annee":
                if not 1900 <= choix <= 2100:
                    return False
            else:
                if not 0 <= choix < len(q["choix"]):
                    return False
            self.votes[jeton] = (choix, ecoule)
            if len(self.votes) >= len(self.joueurs):    # tout le monde a répondu
                self._reveler()
            else:
                self._changer()
            return True

    # --- actions de l'écran principal ---
    def animateur(self):
        """Jeton de l'animateur : le premier joueur arrivé encore présent. Il pilote la partie depuis son téléphone."""
        return min(self.joueurs.values(), key=lambda j: j["arrivee"])["jeton"] if self.joueurs else None

    def action_animateur(self, jeton, action):
        with self.verrou:
            if not jeton or jeton != self.animateur():
                return False
        if action == "suivant":
            self.suivant()
        elif action == "reveler":
            self.reveler()
        elif action == "pause":
            self.basculer_pause()
        else:
            return False
        return True

    def retirer(self, jeton):
        """Salle d'attente : retire un joueur (parti, ou inscrit deux fois). Son téléphone revient à l'inscription."""
        with self.verrou:
            if self.phase != "attente" or self.joueurs.pop(jeton, None) is None:
                return False
            self._changer()
            return True

    def lancer_question(self, idx):
        if JOURNAL_DETAILLE:
            print(f"Partie : question {idx + 1} / {len(self.questions)} ({len(self.joueurs)} joueurs)", file=sys.stderr,
                  flush=True)
        with self.verrou:
            self.idx, self.phase = idx, "question"
            self.votes, self.resultats = {}, None
            self.debut = time.monotonic()
            self.pause_depuis = None
            if self.minuteur:
                self.minuteur.cancel()
            self.minuteur = threading.Timer(self.duree + 0.3, self.fin_du_temps, args=(idx,))
            self.minuteur.daemon = True
            self.minuteur.start()
            self._changer()

    def fin_du_temps(self, idx):
        with self.verrou:
            if self.phase == "question" and self.idx == idx:
                self._reveler()

    def reveler(self):
        with self.verrou:
            if self.phase == "question":
                self._reveler()

    def basculer_pause(self):
        """Met la question en pause (chrono arrêté, votes bloqués), ou la reprend là où elle en était."""
        with self.verrou:
            if self.phase != "question":
                return
            if self.pause_depuis is None:
                self.pause_depuis = time.monotonic()
                if self.minuteur:
                    self.minuteur.cancel()
            else:
                ecoule = self.pause_depuis - self.debut
                self.debut = time.monotonic() - ecoule      # les points restent calculés sur le temps joué
                self.pause_depuis = None
                self.minuteur = threading.Timer(max(0.0, self.duree - ecoule) + 0.3, self.fin_du_temps,
                                                args=(self.idx,))
                self.minuteur.daemon = True
                self.minuteur.start()
            self._changer()

    def _reveler(self):
        q = self.questions[self.idx]
        annee = q.get("type") == "annee"
        avant = {j["jeton"]: r for r, j in enumerate(self._classement(), 1)}
        comptes = [0] * len(q.get("choix") or [])
        gains, rapide, proches, nb_bons = {}, None, [], 0
        facteur = 2 if q.get("double") else 1
        for jeton, j in self.joueurs.items():
            j["rang_avant"] = avant[jeton]
            vote = self.votes.get(jeton)
            gain, bon, d = 0, False, {"choix": None}
            if vote is not None:
                choix, t = vote
                j["repondues"] += 1
                vitesse = max(0.0, 1 - t / self.duree)
                if annee:
                    ecart = abs(choix - q["annee"])
                    gain = round(points_annee(ecart) * (0.75 + 0.25 * vitesse))
                    bon = ecart <= 2
                    proches.append((ecart, t, j["pseudo"], choix))
                    d["ecart"] = ecart
                else:
                    comptes[choix] += 1
                    bon = choix == q["bonne"]
                    gain = round(POINTS_MIN + (POINTS_MAX - POINTS_MIN) * vitesse) if bon else 0
                d.update(choix=choix, temps=round(t, 1))
                if bon and (rapide is None or t < rapide["temps"]):
                    rapide = {"jeton": jeton, "pseudo": j["pseudo"], "temps": round(t, 1)}
            gain *= facteur
            bonus = 0
            if bon:
                nb_bons += 1
                j["serie"] += 1
                j["bonnes"] += 1
                j["serie_max"] = max(j["serie_max"], j["serie"])
                j["plus_rapide"] = vote[1] if j["plus_rapide"] is None else min(j["plus_rapide"], vote[1])
                bonus = min(SERIE_BONUS * (j["serie"] - 1), SERIE_BONUS_MAX)
            else:
                j["serie"] = 0
            d.update(bon=bon, gain=gain + bonus, bonus=bonus, serie=j["serie"], double=facteur == 2)
            j["dernier"] = d
            j["score"] += gain + bonus
            gains[jeton] = gain + bonus
        if rapide:
            self.joueurs[rapide["jeton"]]["dernier"]["plus_rapide"] = True
            rapide = {k: v for k, v in rapide.items() if k != "jeton"}     # le jeton reste secret
        proches.sort()
        bonne = (str(q["annee"]) if annee else
                 " — ".join(x for x in (q["choix"][q["bonne"]]["titre"], q["choix"][q["bonne"]]["artiste"]) if x))
        self.stats[self.idx:] = [{"numero": self.idx + 1, "reponse": bonne, "chanson": q.get("chanson"),
                                  "taux": round(nb_bons / len(self.joueurs), 2) if self.joueurs else 0,
                                  "rapide": rapide}]
        self.resultats = {"comptes": comptes, "gains": gains, "rapide": rapide,
                          "proches": [{"pseudo": p, "annee": a, "ecart": e} for e, _, p, a in proches[:5]]}
        self.phase = "resultats"
        self.pause_depuis = None
        if self.minuteur:
            self.minuteur.cancel()
        self._changer()

    def suivant(self):
        with self.verrou:
            phase, idx = self.phase, self.idx
        if phase == "attente":
            if self.joueurs:
                self.lancer_question(idx + 1)
        elif phase == "question":
            self.reveler()
        elif phase == "resultats":
            if idx + 1 < len(self.questions):
                self.lancer_question(idx + 1)
            else:
                with self.verrou:
                    self.phase = "fin"
                    self._changer()

    def _stats_fin(self):
        s = [x for x in self.stats if x]
        if not s or not self.joueurs:
            return None
        rapides = [x["rapide"] for x in s if x.get("rapide")]
        serie = max(self.joueurs.values(), key=lambda j: j["serie_max"])
        return {"plus_ratee": min(s, key=lambda x: x["taux"]), "plus_trouvee": max(s, key=lambda x: x["taux"]),
                "plus_rapide": min(rapides, key=lambda x: x["temps"]) if rapides else None,
                "meilleure_serie": {"pseudo": serie["pseudo"], "serie": serie["serie_max"]}
                if serie["serie_max"] >= 2 else None,
                "questions": len(s)}

    # --- ce que voit chacun ---
    def etat(self, jeton=None, hote=False):
        with self.verrou:
            if not hote and jeton not in self.joueurs:
                # visiteur non inscrit (ou jeton d'une ancienne partie) : il ne voit rien de la partie
                return {"partie": self.id, "version": self.version, "phase": self.phase, "moi": None}
            classement = self._classement()
            gains = (self.resultats or {}).get("gains", {})
            e = {"partie": self.id, "manche": self.manche, "version": self.version, "phase": self.phase,
                 "numero": self.idx + 1,
                 "total": len(self.questions), "duree": self.duree, "mode": self.mode,
                 "nom_mode": MODES[self.mode][0], "nb_joueurs": len(self.joueurs), "nb_votes": len(self.votes),
                 "classement": [{"pseudo": j["pseudo"], "score": j["score"], "gain": gains.get(j["jeton"]),
                                 "equipe": j.get("equipe"), "serie": j["serie"],
                                 # places gagnées (+) ou perdues (-) à cette question
                                 "evolution": (j["rang_avant"] - r) if self.phase == "resultats" and j.get("rang_avant")
                                 else 0,
                                 **({"jeton": j["jeton"]} if hote else {})}
                                for r, j in enumerate(classement, 1)]}
            if self.nb_equipes:
                e["equipes"] = self._equipes()
            if self.phase in ("question", "resultats"):
                q = self.questions[self.idx]
                e["type"] = q.get("type") or "choix"
                e["choix"] = q.get("choix")
                e["consigne"] = q.get("consigne")            # « Quel est le titre ? », « Qui chante ? »
                e["forme"] = q.get("forme")
                e["double"] = bool(q.get("double"))
                # lettre par lettre : secondes au bout desquelles le texte des propositions est complet
                e["lettres"] = round(q["lettres"] * self.duree, 2) if q.get("lettres") else None
                maintenant = self.pause_depuis if self.pause_depuis is not None else time.monotonic()
                e["reste_ms"] = max(0, int((self.duree - (maintenant - self.debut)) * 1000)) \
                    if self.phase == "question" else 0
                e["pause"] = self.pause_depuis is not None
                if hote:
                    e["audio"] = q["audio"]
            if self.phase == "resultats":
                q = self.questions[self.idx]
                e["bonne"] = q.get("bonne")
                e["annee"] = q.get("annee")
                e["info"] = q.get("info")
                e["chanson"] = q.get("chanson")
                e["pochette"] = q.get("pochette")
                e["comptes"] = self.resultats["comptes"]
                e["rapide"] = self.resultats["rapide"]
                e["proches"] = self.resultats["proches"]
            if self.phase == "fin":
                e["stats"] = self._stats_fin()
            if jeton in self.joueurs:
                j = self.joueurs[jeton]
                e["moi"] = {"pseudo": j["pseudo"], "score": j["score"],
                            "rang": classement.index(j) + 1,
                            "a_vote": jeton in self.votes,
                            "equipe": j.get("equipe"), "serie": j["serie"],
                            "animateur": jeton == self.animateur(),
                            "dernier": j["dernier"] if self.phase in ("resultats", "fin") else None,
                            "bilan": {"bonnes": j["bonnes"], "total": len([x for x in self.stats if x]),
                                      "serie_max": j["serie_max"],
                                      "plus_rapide": round(j["plus_rapide"], 1) if j["plus_rapide"] is not None
                                      else None} if self.phase == "fin" else None}
            animateur = self.animateur()
            e["animateur"] = self.joueurs[animateur]["pseudo"] if animateur else None
            if hote:
                e["code"] = self.code
            return e

    def attendre(self, version, delai=15):
        with self.verrou:
            self.verrou.wait_for(lambda: self.version != version, timeout=delai)
            return self.version


# ---------- Fabrication en arrière-plan (page « atelier ») ----------

class Fabrication:
    """Une tâche longue à la fois (fabriquer un lot, préparer une partie...), suivie par la page atelier :
    journal des messages, état final, annulation."""

    JOURNAL_MAX = 3000

    def __init__(self):
        self.verrou = threading.Condition()
        self.version = 0
        self.tache = None

    def _changer(self):
        self.version += 1
        self.verrou.notify_all()

    def occupee(self):
        return bool(self.tache and self.tache["etat"] == "en_cours")

    def lancer(self, nom, fonction):
        with self.verrou:
            if self.occupee():
                return False
            bt.ANNULATION.clear()
            self.tache = {"nom": nom, "etat": "en_cours", "journal": [], "debut": time.time(), "fin": None,
                          "resultat": None, "erreur": None}
            self._changer()
        threading.Thread(target=self._executer, args=(fonction,), daemon=True).start()
        return True

    def annuler(self):
        if self.occupee():
            bt.ANNULATION.set()
            bt.afficher("Annulation demandée : arrêt au prochain point sûr...")

    def _ecouter(self, ligne):
        with self.verrou:
            if self.occupee():
                j = self.tache["journal"]
                j.append(ligne)
                del j[:-self.JOURNAL_MAX]
                self._changer()

    def _executer(self, fonction):
        bt.ECOUTEURS.append(self._ecouter)
        resultat, etat, erreur = None, "termine", None
        try:
            resultat = fonction()
        except bt.Annule as e:
            etat, erreur = "annule", str(e)
        except bt.Erreur as e:
            etat, erreur = "erreur", str(e)
        except Exception as e:                            # ne jamais laisser la page sans réponse
            traceback.print_exc()
            etat, erreur = "erreur", f"Erreur inattendue : {type(e).__name__} : {e}"
        finally:
            bt.ECOUTEURS.remove(self._ecouter)
            bt.ANNULATION.clear()
        with self.verrou:
            self.tache.update(etat=etat, erreur=erreur, resultat=resultat, fin=time.time())
            self._changer()

    def attendre(self, version, delai):
        with self.verrou:
            self.verrou.wait_for(lambda: self.version != version, timeout=delai)

    def etat(self):
        with self.verrou:
            # les 400 dernières lignes suffisent à la page (elle est rafraîchie à chaque nouvelle ligne)
            return dict(self.tache, journal=self.tache["journal"][-400:]) if self.tache else None


def chemin_relatif(d):
    try:
        return Path(d).relative_to(bt.BASE).as_posix()
    except ValueError:                                   # lot hors du dossier (python jeu.py --dossier ailleurs)
        return str(d)


def lire_texte(f):
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        return ""


def lire_meta(d):
    """atelier.json : ce que l'atelier retient d'un lot (nom choisi, vidéo à refaire, quiz à mettre à jour)."""
    try:
        return json.loads((d / "atelier.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ecrire_meta(d, **valeurs):
    meta = lire_meta(d)
    meta.update(valeurs)
    meta = {k: v for k, v in meta.items() if v}
    if meta:
        (d / "atelier.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    else:
        (d / "atelier.json").unlink(missing_ok=True)


def infos_lot(d):
    lot = json.loads((d / "lot.json").read_text(encoding="utf-8"))
    meta = lire_meta(d)
    reponses = lire_texte(d / "reponses.txt")
    source = next((l[len("Source : "):] for l in reponses.splitlines() if l.startswith("Source : ")), "")
    date = re.search(r"Blind test du (\S+ \S+)", reponses)
    m = re.match(r"(?:thème|playlist Spotify|playlist|liste) « (.+?) »", source)
    nom = meta.get("nom") or (m.group(1) if m else (source.split(" (")[0] or d.name))
    quiz_pret = False
    try:
        quiz = json.loads((d / "quiz.json").read_text(encoding="utf-8"))
        quiz_pret = quiz.get("version") == VERSION_QUIZ and quiz.get("empreinte") == [c.get("id") for c in lot]
    except (OSError, ValueError):
        pass
    a_verifier = lire_texte(d / "propositions_a_verifier.txt")
    video = next(d.glob("*.mp4"), None)
    categorie = "archive" if d.parent.name == "archives" else ("essai" if d.name.startswith("essai_") else "actuel")
    return {"id": chemin_relatif(d), "dossier": d.name, "categorie": categorie, "nom": nom, "source": source,
            "date": date.group(1) if date else "", "nombre": len(lot), "duree": duree_lot(d),
            "video": video.name if video else None, "quiz": quiz_pret and not meta.get("quiz_a_actualiser"),
            "video_perimee": bool(video and meta.get("video_perimee")),
            "a_verifier": len(re.findall(r"^\d+\.", a_verifier, re.M)),
            "problemes": (d / "problemes.txt").exists(), "modifie": (d / "lot.json").stat().st_mtime}


def lister_lots():
    dossiers = (sorted(bt.BASE.glob("extraits_*s")) + sorted(bt.BASE.glob("essai_*s"))
                + sorted((bt.BASE / "archives").glob("*"), key=lambda d: d.stat().st_mtime, reverse=True))
    lots = []
    for d in dossiers:
        if (d / "lot.json").exists():
            try:
                lots.append(infos_lot(d))
            except (OSError, ValueError, KeyError):
                continue
    return lots


def trouver_lot(ident):
    """Dossier d'un lot à partir de son identifiant (chemin relatif), seulement parmi les lots connus."""
    for d in (bt.BASE.glob("extraits_*s"), bt.BASE.glob("essai_*s"), (bt.BASE / "archives").glob("*")):
        for x in d:
            if chemin_relatif(x) == ident and (x / "lot.json").exists():
                return x
    raise bt.Erreur("Ce blind test n'existe plus (déplacé ou supprimé ?).")


FICHIERS_LISIBLES = ("reponses.txt", "problemes.txt", "propositions_a_verifier.txt")


def corbeille(chemin):
    """Envoie un dossier à la corbeille (récupérable), au lieu de l'effacer."""
    if os.name != "nt":                                  # Mac : la Corbeille du Finder est ~/.Trash
        poubelle = Path.home() / ".Trash"
        cible, n = poubelle / Path(chemin).name, 2
        while cible.exists():
            cible, n = poubelle / f"{Path(chemin).name} {n}", n + 1
        try:
            poubelle.mkdir(exist_ok=True)
            shutil.move(str(chemin), str(cible))
        except OSError:
            raise bt.Erreur(f"Impossible de mettre {Path(chemin).name} à la corbeille (fichier ouvert ailleurs ?).")
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    op = SHFILEOPSTRUCTW(None, 3, str(Path(chemin).resolve()) + "\0", None, 0x40 | 0x10 | 0x04 | 0x400, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) or op.fAnyOperationsAborted:
        raise bt.Erreur(f"Impossible de mettre {Path(chemin).name} à la corbeille (fichier ouvert ailleurs ?).")
SANS_PARTIE = -2                                         # « version » renvoyée tant qu'aucune partie n'est lancée


# ---------- Application : serveur web, partie en cours, accès Internet ----------

class Application:
    def __init__(self, port, fond):
        self.port = port
        self.fond = fond
        self.id = secrets.token_hex(4)                   # change à chaque lancement du serveur
        self.code = f"{secrets.randbelow(900000) + 100000}"
        self.cle_hote = secrets.token_urlsafe(8)
        self.cle_atelier = secrets.token_urlsafe(12)
        self.partie = None
        self.changement = threading.Condition()          # attentes des pages tant qu'aucune partie n'existe
        self.url_locale = f"http://{ip_locale()}:{port}/"
        self.url_joueur = self.url_locale
        self.tunnel = None
        self.funnel = False                              # adresse publique fixe Tailscale ouverte ?
        self._url_tunnel = None
        self.alerte = None
        self.fabrication = Fabrication()
        self.arret = threading.Event()                   # bouton « Quitter » de l'atelier
        self.apercus = {}                                # aperçus de sélection (avant fabrication)
        self.reprise = lire_sauvegarde()                 # partie interrompue la dernière fois ?
        if self.reprise:
            self.id, self.code = self.reprise["id"], self.reprise["code"]

    @property
    def url_ecran(self):
        return f"http://localhost:{self.port}/ecran?cle={self.cle_hote}"

    @property
    def url_atelier(self):
        return f"http://localhost:{self.port}/atelier?cle={self.cle_atelier}"

    def installer_partie(self, quiz, duree, dossier, reprise=None, mode="classique", equipes=0):
        if reprise and (reprise.get("mode") == "progressif"):
            for q in reprise["questions"]:              # partie sauvegardée avant le mode « lettre par lettre »
                q.setdefault("lettres", 0.75)
        print(f"Partie : {'reprise' if reprise else 'nouvelle'}, {dossier.name}, mode {reprise.get('mode') if reprise else mode}",
              file=sys.stderr, flush=True)
        if reprise:                                      # même identifiant et même code : les téléphones se reconnectent
            self.id, self.code = reprise["id"], reprise["code"]
            partie = Partie({"questions": reprise["questions"]}, duree, dossier, self.id, self.code,
                            mode=reprise.get("mode", "classique"), equipes=reprise.get("equipes", 0))
            partie.reprendre(reprise)
        else:
            partie = Partie(quiz, duree, dossier, self.id, self.code, self.partie, mode, equipes)
        ancienne, self.partie = self.partie, partie
        if ancienne:                                     # réveille ceux qui attendaient l'ancienne partie
            with ancienne.verrou:
                partie.version = max(partie.version, ancienne.version + 1)
                ancienne.phase = "remplacee"
                ancienne._changer()
        if not reprise:
            with self.partie.verrou:                     # la sauvegarde suit la nouvelle partie tout de suite
                if self.partie.joueurs:
                    self.partie._sauver()
                else:
                    FICHIER_PARTIE.unlink(missing_ok=True)
        self.reprise = None
        with self.changement:
            self.changement.notify_all()
        self.fabrication_changee()

    def fabrication_changee(self):
        with self.fabrication.verrou:
            self.fabrication._changer()

    # --- accès Internet : adresse fixe Tailscale si possible, sinon tunnel Cloudflare temporaire ---
    def internet_actif(self):
        return bool(self.funnel or (self.tunnel and self.tunnel.poll() is None))

    def ouvrir_internet(self):
        if self.internet_actif():
            self.url_joueur = self._url_tunnel
            return
        if adresse_tailscale():
            self.url_joueur = ouvrir_funnel(self.port)
            self.funnel = True
        else:
            self.tunnel, self.url_joueur = lancer_tunnel(self.port)
        self.alerte = None
        self._url_tunnel = self.url_joueur

    def wifi_seulement(self):
        self.url_joueur = self.url_locale
        if self.funnel:                                  # plus besoin de l'adresse publique : on la ferme
            fermer_funnel()
            self.funnel = False

    def occupe(self):
        """Partie en cours ou fabrication : le PC ne doit pas se mettre en veille (réglé sur 15 min ici)."""
        p = self.partie
        return self.fabrication.occupee() or bool(p and p.joueurs and p.phase not in ("fin", "remplacee"))

    def surveiller_tunnel(self):
        tours = 0
        while True:
            time.sleep(2)
            tours += 1
            if os.name == "nt" and tours % 15 == 0 and self.occupe():
                # remet à zéro le compte à rebours de mise en veille (et d'extinction de l'écran) de Windows
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x00000001 | 0x00000002)
            elif sys.platform == "darwin" and tours % 15 == 0 and self.occupe():
                # le Mac ne se met pas en veille et garde l'écran allumé pendant 40 s (renouvelé toutes les 30 s)
                try:
                    subprocess.Popen(["caffeinate", "-d", "-i", "-t", "40"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            if self.tunnel and self.tunnel.poll() is not None:
                self.tunnel = None
                if self.url_joueur != self.url_locale:
                    self.url_joueur = self.url_locale
                    self.alerte = ("L'accès Internet s'est coupé : les téléphones en 4G ne peuvent plus jouer. "
                                   "Relancez la partie en choisissant « Internet » (il faudra rescanner le QR code).")
                    afficher("\nATTENTION : " + self.alerte)
                    self.fabrication_changee()

    def fermer(self):
        if self.funnel:
            fermer_funnel()
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        (bt.BASE / "_tunnel.pid").unlink(missing_ok=True)
        (bt.BASE / FICHIER_ATELIER).unlink(missing_ok=True)

    # --- tâches lancées depuis l'atelier ---
    def verifier_libre(self, dossier, quoi="modifier ce blind test", attente_ok=False):
        """Refuse de toucher au lot d'une partie en cours. Salle d'attente (aucune question posée) : une simple
        correction reste possible, la partie la prend en compte tout de suite."""
        p = self.partie
        if p and p.phase not in ("fin", "remplacee") and Path(p.dossier) == Path(dossier):
            if attente_ok and p.phase == "attente" and p.idx < 0:
                return
            raise bt.Erreur(f"Une partie est en cours avec ce blind test : terminez-la avant de {quoi}.")

    def rafraichir_partie(self, d):
        """Salle d'attente sur ce lot : la partie reprend les questions corrigées."""
        p = self.partie
        if p and Path(p.dossier) == Path(d) and p.phase == "attente" and p.idx < 0:
            try:
                quiz = json.loads((Path(d) / "quiz.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            with p.verrou:
                p.questions = quiz["questions"]
                p._changer()

    def lancer_tache(self, nom, fonction):
        if not self.fabrication.lancer(nom, fonction):
            raise bt.Erreur("Une fabrication est déjà en cours : attendez la fin, ou annulez-la.")
        return {"tache": True}

    def reglages_depuis(self, r):
        """Réglages d'une fabrication depuis le formulaire de l'atelier, vérifiés."""
        try:
            duree = int(r.get("duree"))
            nombre = int(r.get("nombre") or bt.nombre_par_defaut(duree))
            debut = float(r.get("debut") or 0)
        except (TypeError, ValueError):
            raise bt.Erreur("Durée, nombre d'extraits ou décalage non valide.")
        if not bt.DUREE_MIN <= duree <= bt.DUREE_MAX:
            raise bt.Erreur(f"La durée doit être comprise entre {bt.DUREE_MIN} et {bt.DUREE_MAX} secondes.")
        if not 1 <= nombre <= 200:
            raise bt.Erreur("Le nombre d'extraits doit être compris entre 1 et 200.")
        if not 0 <= debut <= bt.debut_maxi(duree):
            raise bt.Erreur(f"Le décalage de départ doit être compris entre 0 et {bt.debut_maxi(duree)} s.")
        type_, valeur = bt.detecter_source(r.get("source"))
        oeuvre = {"oui": True, "non": False}.get(r.get("oeuvre"))
        nom = (str(r.get("nom") or "").strip() or "blindtest")[:80]
        args = bt.reglages(duree=duree, nombre=nombre, debut=debut, video=r.get("usage") != "quiz", nom=nom,
                           oeuvre=oeuvre, melanger=bool(r.get("melanger")), essai=bool(r.get("essai")),
                           exclure_playlists=[str(x) for x in r.get("exclure_playlists") or []])
        setattr(args, type_, valeur)
        libelle = {"theme": "thème", "playlist": "playlist Deezer", "spotify": "playlist Spotify",
                   "lignes": "liste de titres"}[type_]
        return args, duree, nombre, libelle

    def apercu(self, r):
        args, duree, nombre, libelle = self.reglages_depuis(r)

        def tache():
            res = bt.apercu(args)
            ident = secrets.token_hex(4)
            self.apercus[ident] = {"args": args, "res": res, "libelle": libelle}
            for ancien in list(self.apercus)[:-3]:            # on garde les 3 derniers
                del self.apercus[ancien]
            afficher(f"\nAperçu prêt : {min(len(res['cands']), nombre)} titres retenus"
                     + (f", {len(res['cands']) - nombre} remplaçants" if len(res["cands"]) > nombre else "") + ".")
            return {"apercu": ident, "nombre": nombre, "trouves": len(res["cands"])}

        return self.lancer_tache(f"Aperçu : {nombre} extraits de {duree} s ({libelle})", tache)

    def detail_apercu(self, ident):
        ap = self.apercus.get(ident)
        if not ap:
            raise bt.Erreur("Cet aperçu n'existe plus : refaites-le.")
        res = ap["res"]
        return {"id": ident, "source": res["source"], "nombre": res["nombre"], "duree": res["duree"],
                "notes": res["notes"], "details": res["details"], "libelle": ap["libelle"],
                "titres": [{"cle": c["cle"], "artiste": c["artiste"], "titre": c["titre"], "id": c.get("id"),
                            "note": c.get("note")} for c in res["cands"]]}

    def fabriquer(self, r):
        usage = r.get("usage")
        if usage not in ("quiz", "video", "les_deux"):
            raise bt.Erreur("Choisissez si ce blind test est pour le quiz, pour la vidéo, ou les deux.")
        try:
            duree = int(r.get("duree"))
            nombre = int(r.get("nombre") or bt.nombre_par_defaut(duree))
            debut = float(r.get("debut") or 0)
        except (TypeError, ValueError):
            raise bt.Erreur("Durée, nombre d'extraits ou décalage non valide.")
        if not bt.DUREE_MIN <= duree <= bt.DUREE_MAX:
            raise bt.Erreur(f"La durée doit être comprise entre {bt.DUREE_MIN} et {bt.DUREE_MAX} secondes.")
        if not 1 <= nombre <= 200:
            raise bt.Erreur("Le nombre d'extraits doit être compris entre 1 et 200.")
        if not 0 <= debut <= bt.debut_maxi(duree):
            raise bt.Erreur(f"Le décalage de départ doit être compris entre 0 et {bt.debut_maxi(duree)} s.")
        ap = self.apercus.get(str(r.get("apercu") or "")) if r.get("apercu") else None
        if r.get("apercu") and not ap:
            raise bt.Erreur("Cet aperçu n'existe plus : refaites-le.")
        if ap:                                            # durée, nombre et source : ceux de l'aperçu
            duree, nombre = ap["res"]["duree"], ap["res"]["nombre"]
            r = dict(r, source=None)
        type_, valeur = bt.detecter_source(r.get("source")) if not ap else (None, None)
        cible = bt.BASE / f"{'essai' if r.get('essai') else 'extraits'}_{duree}s"
        p = self.partie
        if p and p.phase not in ("fin", "remplacee") and Path(p.dossier) == cible:
            raise bt.Erreur(f"Une partie est en cours avec les extraits de {duree} s ({cible.name}) : la fabrication "
                            "les remplacerait en plein jeu. Terminez la partie, ou choisissez une autre durée "
                            "ou le mode essai.")
        oeuvre = {"oui": True, "non": False}.get(r.get("oeuvre"))
        nom = (str(r.get("nom") or "").strip() or "blindtest")[:80]
        if ap:
            args = ap["args"]
            retirer = set(r.get("retirer") or [])
            cands = [c for c in ap["res"]["cands"] if c["cle"] not in retirer]
            if not cands:
                raise bt.Erreur("Tous les titres ont été retirés de la sélection.")
            notes = list(ap["res"]["notes"])
            if retirer:
                notes.append(f"{len(retirer)} titres retirés à la main dans l'aperçu.")
            for k, v in (("video", usage != "quiz"), ("nom", nom), ("oeuvre", oeuvre), ("essai", bool(r.get("essai")))):
                setattr(args, k, v)
            args.selection = {"cands": cands, "source": ap["res"]["source"], "notes": notes}
            libelle = ap["libelle"]
        else:
            args = bt.reglages(duree=duree, nombre=nombre, debut=debut, video=usage != "quiz", nom=nom,
                               oeuvre=oeuvre, melanger=bool(r.get("melanger")), essai=bool(r.get("essai")))
            setattr(args, {"theme": "theme", "playlist": "playlist", "spotify": "spotify",
                           "lignes": "lignes"}[type_], valeur)
            libelle = {"theme": "thème", "playlist": "playlist Deezer", "spotify": "playlist Spotify",
                       "lignes": "liste de titres"}[type_]

        def tache():
            res = bt.lancer(args)
            if usage != "video":
                afficher("")
                try:
                    preparer_quiz(res["dossier"])
                except bt.Annule:
                    raise bt.Annule("Le blind test est bien fabriqué, mais le quiz n'est pas préparé : il le sera "
                                    "au moment de jouer (1 à 3 min).")
            a_verifier = infos_lot(res["dossier"])["a_verifier"] if usage != "video" else 0
            return {"lot": chemin_relatif(res["dossier"]), "nombre": res["nombre"], "demandes": res["demandes"],
                    "video": res["video"].name if res["video"] else None, "notes": len(res["notes"]),
                    "archive": res["archive"].name if res["archive"] else None, "quiz": usage != "video",
                    "a_verifier": a_verifier}

        return self.lancer_tache(f"Fabrication : {nombre} extraits de {duree} s ({libelle})", tache)

    # --- éditeur de réponses ---
    @staticmethod
    def champs_affiches(c):
        """Ce que l'éditeur montre d'une réponse (et ce qu'il faut comparer pour savoir si elle a changé)."""
        artiste, titre = bt.artiste_titre(c)
        if c.get("oeuvre"):
            artiste, titre = c.get("artiste") or artiste, c.get("titre_court") or titre
        return {"artiste": artiste, "titre": titre, "oeuvre": c.get("oeuvre") or "",
                "interprete": c.get("interprete") or "", "note": c.get("note") or ""}

    def lot_detail(self, ident):
        d = trouver_lot(ident)
        lot = json.loads((d / "lot.json").read_text(encoding="utf-8"))
        alertes, corrigees = {}, set(lire_meta(d).get("corrigees") or [])
        for f, motif in (("problemes.txt", r"(?:réponse|Réponse) (\d{2})"), ("propositions_a_verifier.txt", r"^(\d{2})\.")):
            for ligne in lire_texte(d / f).splitlines():
                m = re.search(motif, ligne)
                if m and not (f == "problemes.txt" and int(m.group(1)) in corrigees):
                    alertes.setdefault(int(m.group(1)), []).append(ligne.split(" : ", 1)[-1] if f.startswith("prop")
                                                                   else ligne)
        questions = []
        for n, c in enumerate(lot, 1):
            questions.append(dict(self.champs_affiches(c), n=n, texte=bt.texte_reponse(c), alertes=alertes.get(n, [])))
        return dict(infos_lot(d), questions=questions, mode_oeuvre=any(c.get("oeuvre") for c in lot),
                    en_jeu=bool(self.partie and self.partie.phase not in ("fin", "remplacee")
                                and not (self.partie.phase == "attente" and self.partie.idx < 0)
                                and Path(self.partie.dossier) == d))

    @staticmethod
    def quiz_en_attente(d):
        """Questions dont le quiz est à refaire : {numéro: heure de la retouche}."""
        a = lire_meta(d).get("quiz_a_actualiser") or {}
        return {str(n): 0 for n in a} if isinstance(a, list) else a

    def apres_retouche(self, d, numeros):
        """Retouche enregistrée : quiz à mettre à jour (tout de suite si possible), vidéo à refaire, alertes
        de la fabrication levées pour ces questions."""
        meta = lire_meta(d)
        attente = self.quiz_en_attente(d)
        quand = time.time()
        attente.update({str(n): quand for n in numeros})
        ecrire_meta(d, video_perimee=bool(next(d.glob("*.mp4"), None)) or meta.get("video_perimee"),
                    quiz_a_actualiser=attente if (d / "quiz.json").exists() else None,
                    corrigees=sorted(set(meta.get("corrigees") or []) | set(numeros)))
        self.fabrication_changee()

    def actualiser_quiz_lot(self, d):
        """Met le quiz à jour. Une retouche enregistrée PENDANT la mise à jour reste en attente et est
        traitée au tour suivant (sinon sa question garderait l'ancienne réponse)."""
        fait = False
        for _ in range(5):
            attente = self.quiz_en_attente(d)
            if not attente:
                break
            actualiser_quiz(d, [int(n) for n in attente])
            fait = True
            reste = {n: t for n, t in self.quiz_en_attente(d).items() if attente.get(n) != t}
            ecrire_meta(d, quiz_a_actualiser=reste or None)
        if fait:
            self.rafraichir_partie(d)

    def modifier_lot(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        self.verifier_libre(d, attente_ok=True)
        lot = json.loads((d / "lot.json").read_text(encoding="utf-8"))
        changes = []
        for m in r.get("modifs") or []:
            try:
                n = int(m.get("n"))
            except (TypeError, ValueError):
                continue
            if not 1 <= n <= len(lot):
                continue
            c = dict(lot[n - 1])
            val = {k: " ".join(str(m.get(k) or "").split())[:150] for k in ("artiste", "titre", "oeuvre", "interprete",
                                                                             "note")}
            if val == self.champs_affiches(c):
                continue                                  # rien de changé sur cette ligne
            if not val["artiste"] or not val["titre"]:
                raise bt.Erreur(f"Question {n:02d} : l'artiste et le titre sont obligatoires.")
            c["artiste"], c["titre_court"] = val["artiste"], val["titre"]
            c["reponse"] = f"{val['artiste']} - {val['titre']}"
            for k in ("oeuvre", "interprete", "note"):
                if val[k]:
                    c[k] = val[k]
                else:
                    c.pop(k, None)
            if not c.get("oeuvre"):
                c.pop("interprete", None)
            if c != lot[n - 1]:
                lot[n - 1] = c
                changes.append(n)
        if changes:
            bt.reecrire_reponses(d, lot)
            self.apres_retouche(d, changes)
            if (d / "quiz.json").exists() and not self.fabrication.occupee():
                self.lancer_tache(f"Mise à jour du quiz ({len(changes)} question(s))", lambda: self.actualiser_quiz_lot(d))
        return {"modifiees": changes}

    def remplacer(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        self.verifier_libre(d, attente_ok=True)
        n, ident = int(r.get("n") or 0), int(r.get("id") or 0)
        lot = json.loads((d / "lot.json").read_text(encoding="utf-8"))
        oeuvre = any(c.get("oeuvre") for c in lot)

        def tache():
            piste = bt.api(f"/track/{ident}")
            if not piste.get("preview"):
                raise bt.Erreur("Ce morceau n'a pas d'extrait sur Deezer : choisissez-en un autre.")
            cand = bt.piste_vers_dict(piste)
            nouveau = bt.remplacer_extrait(d, n, cand, duree_lot(d), oeuvre)
            self.apres_retouche(d, [n])
            if (d / "quiz.json").exists():
                self.actualiser_quiz_lot(d)
            return {"lot": chemin_relatif(d), "remplace": n, "texte": bt.texte_reponse(nouveau)}

        return self.lancer_tache(f"Remplacement de la question {n:02d}", tache)

    def refaire_video(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        video = next(d.glob("*.mp4"), None)
        nom = video.stem if video else (lire_meta(d).get("nom") or "blindtest")

        def tache():
            sortie = bt.video_seule(duree_lot(d), nom, None, True, dest=d)
            ecrire_meta(d, video_perimee=None)
            return {"lot": chemin_relatif(d), "video": sortie.name}

        return self.lancer_tache(f"Montage de la vidéo ({infos_lot(d)['nom']})", tache)

    def garder_essai(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        if not d.name.startswith("essai_"):
            raise bt.Erreur("Seul un essai peut devenir le blind test officiel.")
        dest = bt.BASE / ("extraits_" + d.name[len("essai_"):])
        self.verifier_libre(d, "le garder")
        self.verifier_libre(dest, "remplacer le blind test officiel de cette durée")

        def tache():
            afficher(f"{d.name} devient {dest.name}...")
            archive = bt.garder_essai(d, dest)
            if archive:
                afficher(f"Ancien blind test rangé dans les archives : {archive.name}")
            afficher("Fait : ses titres entrent dans l'historique (pas de répétition aux prochains lancements).")
            return {"lot": chemin_relatif(dest), "archive": archive.name if archive else None}

        return self.lancer_tache(f"Garder l'essai ({infos_lot(d)['nom']})", tache)

    def renommer(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        ecrire_meta(d, nom=" ".join(str(r.get("nom") or "").split())[:80] or None)
        self.fabrication_changee()
        return {}

    def supprimer(self, r):
        d = trouver_lot(str(r.get("lot") or ""))
        if not (d.parent.name == "archives" or d.name.startswith("essai_")):
            raise bt.Erreur("Seuls les essais et les archives se suppriment (le blind test actuel est remplacé "
                            "automatiquement par le suivant).")
        self.verifier_libre(d, "le supprimer")
        if FICHIER_PARTIE.exists() and self.reprise and Path(self.reprise["dossier"]) == d:
            self.oublier_reprise()
        corbeille(d)
        self.fabrication_changee()
        return {}

    @staticmethod
    def preparer_partie(dossier, mode, nouveaux=False):
        """Quiz prêt + données du mode (pochettes, années, pays, langues) -> questions de la partie."""
        quiz = preparer_quiz(dossier, nouveaux)
        enrichir_quiz(dossier, quiz, {"pochette"} | ({mode} if mode in ("annee", "langue", "pays") else set()))
        questions = questions_du_mode(quiz, dossier, mode)
        ecartees = len(quiz["questions"]) - len(questions)
        if len(questions) < 3:
            raise bt.Erreur(f"Mode « {MODES[mode][0]} » : seulement {len(questions)} titre(s) avec une donnée sûre "
                            "dans ce blind test. Choisissez un autre mode.")
        if ecartees:
            afficher(f"Mode « {MODES[mode][0]} » : {ecartees} titre(s) sans donnée sûre laissé(s) de côté.")
        if mode in ("langue", "pays"):
            valeurs = Counter(quiz["questions"][q["numero"] - 1].get(mode) for q in questions)
            v, n = valeurs.most_common(1)[0]
            if n / len(questions) > 0.8:
                afficher(f"Attention : {n} réponses sur {len(questions)} sont « {nom_valeur(mode, v)} » : "
                         "ce mode sera facile avec ce blind test.")
        return {"questions": questions}

    def rejouer(self):
        """Podium : même blind test, même mode, mêmes joueurs, scores remis à zéro."""
        p = self.partie
        if not p or p.phase != "fin":
            return False

        def relancer():
            try:
                quiz = self.preparer_partie(p.dossier, p.mode)
                self.installer_partie(quiz, p.duree, p.dossier, mode=p.mode, equipes=p.nb_equipes)
            except bt.Erreur as e:
                afficher(f"Impossible de rejouer : {e}")

        threading.Thread(target=relancer, daemon=True).start()
        return True

    def jouer(self, r):
        reprise = self.reprise if r.get("reprise") else None
        if r.get("reprise") and not reprise:
            raise bt.Erreur("Cette partie ne peut plus être reprise.")
        dossier = Path(reprise["dossier"]) if reprise else trouver_lot(str(r.get("lot") or ""))
        internet = bool(r.get("internet"))
        nouveaux = bool(r.get("nouveaux_choix")) and not reprise
        mode = r.get("mode") if r.get("mode") in MODES else "classique"
        try:
            equipes = int(r.get("equipes") or 0)
        except (TypeError, ValueError):
            equipes = 0
        equipes = equipes if equipes in (2, 3, 4) else 0

        def tache():
            afficher(f"Lot : {dossier.name}")
            if not nouveaux:
                self.actualiser_quiz_lot(dossier)
            quiz = {"questions": reprise["questions"]} if reprise else self.preparer_partie(dossier, mode, nouveaux)
            if internet:
                self.ouvrir_internet()
            else:
                self.wifi_seulement()
            self.installer_partie(quiz, duree_lot(dossier), dossier, reprise, mode, equipes)
            if reprise:
                afficher(f"\nPartie reprise : {len(reprise['joueurs'])} joueurs, scores conservés. Elle repart "
                         f"à la question {reprise['suite'] + 1} / {len(quiz['questions'])}.")
            afficher(f"\nPartie prête : {len(quiz['questions'])} questions de {duree_lot(dossier)} s, mode "
                     f"« {MODES[self.partie.mode][0]} »" + (f", {self.partie.nb_equipes} équipes" if self.partie.nb_equipes
                                                            else "") + ".")
            afficher(f"Joueurs ({'Internet' if internet else 'même Wi-Fi'}) : {self.url_joueur}   "
                     f"code {self.code}")
            return {"lot": chemin_relatif(dossier), "url_ecran": self.url_ecran}

        if self.fabrication.lancer(f"Préparation de la partie ({infos_lot(dossier)['nom']})", tache):
            return {"tache": True}
        # pendant une fabrication : possible seulement si tout est déjà prêt (quiz, pochettes, données du mode)
        immediat = (not nouveaux and infos_lot(dossier)["quiz"]
                    and (not internet or (self.internet_actif() and self._url_tunnel)))
        if immediat and not reprise:
            quiz_fichier = json.loads((dossier / "quiz.json").read_text(encoding="utf-8"))
            cles = {"pochette"} | ({mode} if mode in ("annee", "langue", "pays") else set())
            immediat = all(k in q for q in quiz_fichier["questions"] for k in cles)
        if not immediat:
            raise bt.Erreur("Une fabrication est en cours : ce blind test demande une préparation (quiz, pochettes, "
                            "données du mode ou accès Internet). Attendez la fin de la fabrication, ou annulez-la.")
        quiz = {"questions": reprise["questions"]} if reprise else self.preparer_partie(dossier, mode)
        if internet:
            self.ouvrir_internet()
        else:
            self.wifi_seulement()
        self.installer_partie(quiz, duree_lot(dossier), dossier, reprise, mode, equipes)
        return {"tache": False, "url_ecran": self.url_ecran}

    def oublier_reprise(self):
        self.reprise = None
        FICHIER_PARTIE.unlink(missing_ok=True)
        self.fabrication_changee()

    def resume_reprise(self):
        d = self.reprise
        if not d:
            return None
        dossier = Path(d["dossier"])
        try:
            nom = infos_lot(dossier)["nom"]
            lot = chemin_relatif(dossier)
        except (OSError, ValueError, KeyError):
            nom, lot = dossier.name, None
        tete = sorted(d["joueurs"], key=lambda j: (-j["score"], j["arrivee"]))[:3]
        return {"lot": lot, "nom": nom, "date": d.get("date"), "suite": d["suite"], "total": d.get("total"),
                "mode": MODES.get(d.get("mode"), MODES["classique"])[0], "equipes": d.get("equipes") or 0,
                "joueurs": len(d["joueurs"]), "tete": [{"pseudo": j["pseudo"], "score": j["score"]} for j in tete]}

    def resume_partie(self):
        p = self.partie
        if not p:
            return None
        with p.verrou:
            return {"lot": chemin_relatif(p.dossier), "phase": p.phase, "numero": p.idx + 1,
                    "total": len(p.questions), "joueurs": len(p.joueurs), "mode": MODES[p.mode][0],
                    "equipes": p.nb_equipes}

    def etat_atelier(self):
        return {"version": self.fabrication.version, "tache": self.fabrication.etat(), "lots": lister_lots(),
                "partie": self.resume_partie(), "internet": self.internet_actif() and self.url_joueur != self.url_locale,
                "url_joueur": self.url_joueur, "url_ecran": self.url_ecran, "code": self.code, "alerte": self.alerte,
                "reprise": self.resume_reprise(),
                "formats": [{"duree": f["duree"], "nombre": f["nombre"]} for f in bt.FORMATS.values()],
                "duree_min": bt.DUREE_MIN, "duree_max": bt.DUREE_MAX, "preview": bt.DUREE_PREVIEW,
                "duree_reponse": bt.DUREE_REPONSE, "adresse_fixe": adresse_tailscale(),
                "modes": [{"id": k, "nom": v[0], "aide": v[1]} for k, v in MODES.items()],
                "cloudflared": bool(adresse_tailscale() or trouver_cloudflared())}


# ---------- Serveur web ----------

def fabriquer_gestionnaire(app):
    class Gestionnaire(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, obj, code=200):
            corps = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def _fichier(self, chemin, type_=None):
            if not chemin or not Path(chemin).is_file():
                return self._json({"erreur": "introuvable"}, 404)
            donnees = Path(chemin).read_bytes()
            # envoi par morceaux (« Range ») : Safari (Mac, iPad, iPhone) ne lit pas un son sans cela
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            debut, fin = 0, len(donnees) - 1
            if m and donnees and (m.group(1) or m.group(2)):
                if m.group(1):
                    debut = int(m.group(1))
                    fin = min(int(m.group(2)), fin) if m.group(2) else fin
                else:                                        # « bytes=-500 » : les 500 derniers octets
                    debut = max(0, len(donnees) - int(m.group(2)))
                if debut > fin:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(donnees)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {debut}-{fin}/{len(donnees)}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", type_ or mimetypes.guess_type(str(chemin))[0]
                             or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(fin - debut + 1))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(donnees[debut:fin + 1])

        def _lire_corps(self, maxi=10_000):
            n = min(int(self.headers.get("Content-Length") or 0), maxi)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def _est_hote(self, q):
            return q.get("cle", [""])[0] == app.cle_hote

        def _est_atelier(self, q):
            # l'atelier fabrique des fichiers sur ce PC : réservé à ce PC, jamais via le Wi-Fi ni le tunnel
            # Internet (cloudflared se connecte en local, mais ajoute toujours ses en-têtes Cf-...)
            local = self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
            relaye = any(h in self.headers for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For",
                                                     "Tailscale-Funnel-Request", "Tailscale-User-Login"))
            return local and not relaye and secrets.compare_digest(q.get("cle", [""])[0], app.cle_atelier)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/joueur"):
                return self._fichier(DOSSIER_PAGES / "joueur.html", "text/html; charset=utf-8")
            if u.path == "/ecran":
                if not self._est_hote(q):
                    return self._json({"erreur": "réservé à l'écran principal"}, 403)
                return self._fichier(DOSSIER_PAGES / "ecran.html", "text/html; charset=utf-8")
            if u.path == "/fond":
                return self._fichier(app.fond)
            if u.path == "/favicon.ico":                      # pas d'icône : réponse vide, sans erreur
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/infos":
                return self._json({"url_joueur": app.url_joueur, "partie": app.id})
            if u.path.startswith("/audio/") and self._est_hote(q) and app.partie:
                nom = Path(u.path).name
                if not nom.endswith(".mp3") or "/" in nom or "\\" in nom:
                    return self._json({"erreur": "introuvable"}, 404)
                return self._fichier(app.partie.dossier / nom, "audio/mpeg")
            if u.path == "/etat":
                # « attente longue » : on ne répond que lorsque la partie change (ou au bout de 25 s).
                # Contrairement à un flux continu, ça traverse sans souci le tunnel Cloudflare.
                try:
                    version = int(q.get("version", ["-1"])[0])
                except ValueError:
                    version = -1
                hote = self._est_hote(q)
                partie = app.partie
                if partie is None:
                    if version == SANS_PARTIE:                # déjà prévenu : on attend qu'une partie soit lancée
                        with app.changement:
                            app.changement.wait(25)
                    partie = app.partie
                else:
                    partie.attendre(version, 25)
                    partie = app.partie                       # remplacée pendant l'attente ?
                if partie is None:
                    return self._json({"partie": app.id, "version": SANS_PARTIE, "phase": "aucune", "moi": None})
                e = partie.etat(q.get("jeton", [None])[0], hote)
                if hote:
                    e["url_joueur"] = app.url_joueur
                return self._json(e)
            if u.path == "/atelier":
                if not self._est_atelier(q):
                    return self._json({"erreur": "réservé à ce PC : relancez « Blind test » pour ouvrir l'atelier"},
                                      403)
                return self._fichier(DOSSIER_PAGES / "atelier.html", "text/html; charset=utf-8")
            if u.path == "/atelier/etat" and self._est_atelier(q):
                try:
                    version = int(q.get("version", ["-1"])[0])
                except ValueError:
                    version = -1
                app.fabrication.attendre(version, 20)
                return self._json(app.etat_atelier())
            if u.path == "/atelier/apercu" and self._est_atelier(q):
                try:
                    return self._json(app.detail_apercu(q.get("id", [""])[0]))
                except bt.Erreur as e:
                    return self._json({"erreur": str(e)}, 404)
            if u.path == "/atelier/lot" and self._est_atelier(q):
                try:
                    return self._json(app.lot_detail(q.get("lot", [""])[0]))
                except (bt.Erreur, OSError, ValueError) as e:
                    return self._json({"erreur": str(e)}, 404)
            if u.path == "/atelier/audio" and self._est_atelier(q):
                try:
                    d = trouver_lot(q.get("lot", [""])[0])
                    n = int(q.get("n", ["0"])[0])
                except (bt.Erreur, ValueError):
                    return self._json({"erreur": "introuvable"}, 404)
                return self._fichier(d / f"{n:02d}.mp3", "audio/mpeg")
            if u.path == "/atelier/ecoute" and self._est_atelier(q):
                # extrait Deezer à écouter dans l'aperçu : les liens en cache expirent, on en demande un neuf
                try:
                    lien = bt.api(f"/track/{int(q.get('id', ['0'])[0])}").get("preview")
                except (bt.Erreur, bt.Introuvable, ValueError):
                    lien = None
                if not lien:
                    return self._json({"erreur": "extrait indisponible"}, 404)
                self.send_response(302)
                self.send_header("Location", lien)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/atelier/chercher" and self._est_atelier(q):
                texte = q.get("q", [""])[0].strip()
                if not texte:
                    return self._json({"resultats": []})
                try:
                    data = bt.api("/search?limit=15&q=" + urllib.parse.quote(texte)).get("data", [])
                except bt.Erreur as e:
                    return self._json({"erreur": str(e)}, 400)
                return self._json({"resultats": [
                    {"id": x["id"], "artiste": x["artist"]["name"], "titre": x.get("title"),
                     "album": (x.get("album") or {}).get("title"), "preview": x.get("preview")}
                    for x in data if x.get("preview") and x.get("readable", True)]})
            if u.path == "/atelier/fichier" and self._est_atelier(q):
                nom = q.get("nom", [""])[0]
                try:
                    dossier = trouver_lot(q.get("lot", [""])[0])
                except bt.Erreur as e:
                    return self._json({"erreur": str(e)}, 404)
                if nom not in FICHIERS_LISIBLES:
                    return self._json({"erreur": "introuvable"}, 404)
                return self._fichier(dossier / nom, "text/plain; charset=utf-8")
            return self._json({"erreur": "introuvable"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path.startswith("/atelier/"):
                if not self._est_atelier(q):
                    return self._json({"erreur": "action refusée"}, 403)
                corps = self._lire_corps(200_000)             # une liste de titres collée peut être longue
                try:
                    actions = {"/atelier/apercu": app.apercu, "/atelier/lot/modifier": app.modifier_lot,
                               "/atelier/lot/remplacer": app.remplacer, "/atelier/lot/video": app.refaire_video,
                               "/atelier/lot/garder": app.garder_essai, "/atelier/lot/renommer": app.renommer,
                               "/atelier/lot/supprimer": app.supprimer}
                    if u.path in actions:
                        return self._json(dict(actions[u.path](corps) or {}, ok=True))
                    if u.path == "/atelier/fabriquer":
                        app.fabriquer(corps)
                    elif u.path == "/atelier/jouer":
                        return self._json(dict(app.jouer(corps), ok=True))
                    elif u.path == "/atelier/oublier":     # ne pas reprendre la partie interrompue
                        app.oublier_reprise()
                    elif u.path == "/atelier/annuler":
                        app.fabrication.annuler()
                    elif u.path == "/atelier/ouvrir":       # le dossier du lot (Explorateur, Finder)
                        ouvrir_dossier(trouver_lot(str(corps.get("lot") or "")))
                    elif u.path == "/atelier/quitter":
                        self._json({"ok": True})
                        threading.Timer(0.5, app.arret.set).start()
                        return
                    else:
                        return self._json({"erreur": "introuvable"}, 404)
                except bt.Erreur as e:
                    return self._json({"erreur": str(e)}, 400)
                except OSError as e:
                    return self._json({"erreur": f"Impossible : {e}"}, 400)
                return self._json({"ok": True})
            corps = self._lire_corps()
            partie = app.partie
            if u.path == "/rejoindre":
                if not partie:
                    return self._json({"erreur": "Pas encore de partie : attends que l'hôte la lance."}, 400)
                # derrière le tunnel Cloudflare, la vraie adresse du téléphone est dans cet en-tête
                appareil = (self.headers.get("Cf-Connecting-Ip")
                            or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                            or self.client_address[0])
                j, erreur = partie.rejoindre(corps.get("pseudo"), corps.get("jeton"), corps.get("code"), appareil)
                if erreur:
                    return self._json({"erreur": erreur}, 400)
                return self._json({"jeton": j["jeton"], "pseudo": j["pseudo"], "partie": app.id})
            if u.path == "/animateur":                    # boutons de l'animateur, sur son téléphone
                action = corps.get("action")
                if action == "rejouer":
                    ok = bool(partie) and partie.animateur() == corps.get("jeton") and app.rejouer()
                else:
                    ok = bool(partie) and partie.action_animateur(corps.get("jeton"), action)
                return self._json({"ok": bool(ok)})
            if u.path == "/equipe":
                ok = bool(partie) and partie.choisir_equipe(corps.get("jeton"), corps.get("equipe"))
                return self._json({"ok": ok})
            if u.path == "/voter":
                ok = bool(partie) and partie.voter(corps.get("jeton"), corps.get("choix"))
                return self._json({"ok": ok})
            if partie and self._est_hote(q):
                if u.path == "/hote/suivant":
                    partie.suivant()
                    return self._json({"ok": True})
                if u.path == "/hote/reveler":
                    partie.reveler()
                    return self._json({"ok": True})
                if u.path == "/hote/retirer":
                    return self._json({"ok": partie.retirer(corps.get("jeton"))})
                if u.path == "/hote/pause":
                    partie.basculer_pause()
                    return self._json({"ok": True})
                if u.path == "/hote/rejouer":
                    return self._json({"ok": app.rejouer()})
            return self._json({"erreur": "action refusée"}, 403)

    return Gestionnaire


def ouvrir_dossier(chemin):
    if os.name == "nt":
        os.startfile(chemin)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(chemin)])


def trouver_cloudflared():
    candidats = []
    if os.name == "nt":
        candidats = [Path(os.environ.get("ProgramFiles(x86)", "")) / "cloudflared" / "cloudflared.exe",
                     Path(os.environ.get("ProgramFiles", "")) / "cloudflared" / "cloudflared.exe"]
        candidats += list((Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages")
                          .glob("Cloudflare.cloudflared*/**/cloudflared*.exe"))
    return bt.chercher_programme("cloudflared", candidats)


# ---------- Adresse publique fixe (Tailscale Funnel) ----------
# Gratuit, même adresse à chaque soirée (https://<pc>.<réseau>.ts.net) : le QR code ne change plus.

def trouver_tailscale():
    return bt.chercher_programme("tailscale", [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe",
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")])        # application Mac


def tailscale(*args, delai=30):
    exe = trouver_tailscale()
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=delai, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


_ADRESSE_TS = {}


def adresse_tailscale():
    """Adresse https fixe de ce PC si Tailscale est installé et connecté, sinon None (vérifiée toutes les 30 s)."""
    if time.monotonic() - _ADRESSE_TS.get("quand", -99) < 30:
        return _ADRESSE_TS.get("url")
    url = None
    if trouver_tailscale():
        try:
            etat = json.loads(tailscale("status", "--json", delai=10).stdout)
            nom = (etat.get("Self") or {}).get("DNSName", "").rstrip(".")
            if etat.get("BackendState") == "Running" and nom:
                url = f"https://{nom}/"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    _ADRESSE_TS.update(quand=time.monotonic(), url=url)
    return url


def ouvrir_funnel(port):
    url = adresse_tailscale()
    afficher(f"Ouverture de l'adresse Internet fixe {url} (Tailscale)...")
    try:
        r = tailscale("funnel", "--bg", str(port), delai=30)
    except subprocess.TimeoutExpired:
        raise bt.Erreur("Tailscale ne répond pas. L'option « Funnel » est-elle activée sur votre compte ? "
                        "Lancez « tailscale funnel 8000 » dans une fenêtre de commande pour voir le lien d'activation.")
    if r.returncode != 0:
        lien = re.search(r"https://login\.tailscale\.com/\S+", r.stdout + r.stderr)
        raise bt.Erreur("Tailscale refuse d'ouvrir l'adresse publique"
                        + (f" : activez « Funnel » ici, puis relancez : {lien.group(0)}" if lien
                           else f" ({(r.stderr or r.stdout).strip()[:200]})."))
    debut = time.monotonic()
    while time.monotonic() - debut < 60:              # la toute première fois, le certificat https peut tarder
        try:
            with urllib.request.urlopen(url + "infos", timeout=8):
                return url
        except Exception:
            time.sleep(3)
    afficher("Attention : l'adresse Internet ne répond pas encore. Si un téléphone affiche « introuvable », "
             "attendez une minute puis rescannez.")
    return url


def fermer_funnel():
    try:
        tailscale("funnel", "reset", delai=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def lancer_tunnel(port):
    """Tunnel Cloudflare gratuit (sans compte) : donne une adresse https publique vers ce PC.
    Renvoie (processus, adresse). Le processus doit rester en vie pendant toute la partie."""
    exe = trouver_cloudflared()
    if not exe:
        raise bt.Erreur("cloudflared est introuvable. Installez-le dans le Terminal avec : brew install cloudflared" if os.name != "nt" else
                        "cloudflared est introuvable. Installez-le avec : winget install Cloudflare.cloudflared")
    afficher("Ouverture de l'accès Internet (tunnel Cloudflare)...")
    # fenêtre fermée d'un coup la fois précédente : le tunnel a pu rester ouvert, on le ferme
    fichier_pid = bt.BASE / "_tunnel.pid"
    if fichier_pid.exists():
        ancien = fichier_pid.read_text().strip()
        if ancien.isdigit():
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", ancien, "/F", "/FI", "IMAGENAME eq cloudflared*"],
                               capture_output=True)
            else:                                        # Mac : seulement si ce numéro est bien un cloudflared
                r = subprocess.run(["ps", "-p", ancien, "-o", "comm="], capture_output=True, text=True)
                if "cloudflared" in r.stdout:
                    try:
                        os.kill(int(ancien), signal.SIGTERM)
                    except OSError:
                        pass
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    fichier_pid.write_text(str(proc.pid))
    trouve = {}
    pret = threading.Event()

    def lire():                                          # cloudflared écrit son adresse dans son journal
        for ligne in proc.stderr:                        # (et on continue de vider le journal ensuite)
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", ligne)
            if m and not pret.is_set():
                trouve["url"] = m.group(0)
                pret.set()

    threading.Thread(target=lire, daemon=True).start()
    if not pret.wait(45):
        proc.terminate()
        raise bt.Erreur("Le tunnel Cloudflare ne répond pas (connexion Internet ?). "
                        "Vous pouvez jouer en Wi-Fi local en attendant.")
    url = trouve["url"]
    # l'adresse met 20 à 30 s à être connue sur Internet : on attend qu'elle réponde vraiment avant
    # d'afficher le QR code (un téléphone qui la scanne trop tôt garderait « site introuvable » en mémoire)
    afficher("Adresse obtenue, attente de sa mise en service (jusqu'à 90 s)...")
    debut = time.monotonic()
    while time.monotonic() - debut < 90:
        try:
            with urllib.request.urlopen(url + "/infos", timeout=5):
                break
        except Exception:
            time.sleep(3)
    else:
        afficher("Attention : l'adresse Internet ne répond pas encore. Si un téléphone affiche "
                 "« introuvable », attendez une minute puis rescannez.")
    return proc, url + "/"


def ip_locale():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 80))                # aucun paquet n'est envoyé
            return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def duree_lot(dossier):
    """Durée de la question : celle de l'extrait (reponses.txt), sinon le nom du dossier (extraits_20s)."""
    try:
        m = re.search(r"extraits de (\d+) s", (dossier / "reponses.txt").read_text(encoding="utf-8"))
    except OSError:
        m = None
    m = m or re.search(r"(\d+)s$", dossier.name)
    return int(m.group(1)) if m else 20


def choisir_dossier(args):
    if args.dossier:
        d = Path(args.dossier)
        return d if d.is_absolute() else bt.BASE / d
    lots = [d for d in sorted(bt.BASE.glob("extraits_*s")) + sorted((bt.BASE / "archives").glob("*"))
            if (d / "lot.json").exists()]
    if not lots:
        raise bt.Erreur("Aucun lot d'extraits trouvé : fabriquez d'abord un blind test avec blindtest.py.")
    if len(lots) == 1:
        return lots[0]
    afficher("Quel lot d'extraits jouer ?")
    for i, d in enumerate(lots, 1):
        video = next(d.glob("*.mp4"), None)
        nb = len(json.loads((d / "lot.json").read_text(encoding="utf-8")))
        afficher(f"  {i}) {d.name} : {nb} extraits de {duree_lot(d)} s" + (f" ({video.stem})" if video else ""))
    rep = bt.demander("Votre choix : ", {str(i) for i in range(1, len(lots) + 1)})
    return lots[int(rep) - 1]


FICHIER_ATELIER = "_atelier.url"
FICHIER_JOURNAL = bt.BASE / "journal.txt"            # tout ce qui s'affiche, erreurs comprises, avec l'heure
JOURNAL_MAX = 2_000_000


class Double:
    """Écrit à la fois dans la fenêtre et dans journal.txt (horodaté) : même fenêtre fermée, on sait ce qui s'est passé."""

    def __init__(self, console, fichier):
        self.console, self.fichier, self.debut_ligne = console, fichier, True
        self.verrou = threading.Lock()

    def write(self, texte):
        try:
            if self.console:
                self.console.write(texte)
        except (OSError, ValueError, UnicodeError):
            pass
        with self.verrou:
            try:
                for morceau in texte.splitlines(keepends=True):
                    if self.debut_ligne:
                        self.fichier.write(time.strftime("%d/%m %H:%M:%S  "))
                    self.fichier.write(morceau)
                    self.debut_ligne = morceau.endswith("\n")
                self.fichier.flush()
            except (OSError, ValueError):
                pass
        return len(texte)

    def flush(self):
        try:
            if self.console:
                self.console.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, nom):                          # encoding, isatty... : ceux de la fenêtre
        return getattr(self.console, nom)


def ouvrir_journal():
    try:
        if FICHIER_JOURNAL.exists() and FICHIER_JOURNAL.stat().st_size > JOURNAL_MAX:
            os.replace(FICHIER_JOURNAL, FICHIER_JOURNAL.with_name("journal.ancien.txt"))
        f = open(FICHIER_JOURNAL, "a", encoding="utf-8", errors="replace")
    except OSError:
        return
    sys.stdout, sys.stderr = Double(sys.stdout, f), Double(sys.stderr, f)
    print("\n" + "-" * 30 + f" démarrage (Python {sys.version.split()[0]}, pid {os.getpid()}) " + "-" * 30, flush=True)


def surveiller_fermeture(app):
    """Fenêtre noire fermée avec la croix : on le note dans le journal et on ferme proprement l'accès Internet."""
    if os.name != "nt":
        def fermeture(signum, frame):
            print("\nArrêt : " + "fenêtre fermée" + ".", flush=True)
            try:
                app.fermer()
            finally:
                os._exit(0)
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, fermeture)
        return None
    import ctypes

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def gestionnaire(evenement):
        if evenement in (2, 5, 6):                       # fermeture de la fenêtre, fin de session, arrêt du PC
            raison = {2: "fenêtre fermée", 5: "fin de session Windows", 6: "arrêt du PC"}[evenement]
            print(f"\nArrêt : {raison}.", flush=True)
            try:
                app.fermer()
            except Exception:
                pass
        return False
    ctypes.windll.kernel32.SetConsoleCtrlHandler(gestionnaire, True)
    return gestionnaire                                  # à garder en vie tant que le programme tourne


def atelier_deja_ouvert():
    """Double-clic alors que le programme tourne déjà : on rouvre simplement sa page."""
    try:
        url = (bt.BASE / FICHIER_ATELIER).read_text(encoding="utf-8").strip()
        with urllib.request.urlopen(f"http://localhost:{urlparse(url).port}/infos", timeout=2):
            return url
    except (OSError, ValueError, TypeError):
        return None


class SansBruit:
    """Un téléphone qui ferme la page (écran verrouillé, onglet fermé) coupe la connexion en pleine réponse :
    sans gravité, mais cela remplissait journal.txt d'erreurs et pouvait cacher une vraie panne."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class ServeurIPv4(SansBruit, ThreadingHTTPServer):
    pass


class ServeurDouble(SansBruit, ThreadingHTTPServer):
    """Écoute en IPv6 ET en IPv4 : « localhost » se résout d'abord en ::1 sous Windows ; un serveur IPv4 seul
    faisait attendre 2 s chaque demande faite par ce nom."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def ouvrir_serveur(premier):
    for port in range(premier, premier + 10):
        try:
            return ServeurDouble(("::", port), None), port
        except OSError:
            try:                                         # IPv6 désactivé sur ce PC : IPv4 seul
                return ServeurIPv4(("0.0.0.0", port), None), port
            except OSError:
                continue
    raise bt.Erreur("Aucun port libre pour démarrer le serveur.")


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ouvrir_journal()
    # erreur dans un fil d'arrière-plan (fin du temps d'une question, tunnel...) : notée au lieu de passer inaperçue
    threading.excepthook = lambda a: print(f"\nERREUR dans un fil d'arrière-plan ({a.thread.name if a.thread else '?'}) :\n"
                                           + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)),
                                           file=sys.stderr, flush=True)
    p = argparse.ArgumentParser(description="Blind test en direct avec les téléphones")
    p.add_argument("--dossier", help="jouer directement ce lot d'extraits (ex. extraits_20s), sans l'atelier")
    p.add_argument("--console", action="store_true", help="ancien mode : choix du lot dans cette fenêtre")
    p.add_argument("--nouveaux-choix", action="store_true", help="régénérer les 4 propositions")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--sans-navigateur", action="store_true", help="ne pas ouvrir le navigateur")
    p.add_argument("--internet", action="store_true",
                   help="les téléphones se connectent par Internet (4G), via un tunnel Cloudflare")
    args = p.parse_args()
    console = bool(args.dossier or args.console)
    interactif = args.console and not args.dossier
    if not console:
        url = atelier_deja_ouvert()
        if url:
            afficher(f"Le blind test est déjà ouvert : {url}")
            if not args.sans_navigateur:
                webbrowser.open(url)
            return
    app = None
    try:
        fond = bt.trouver_fichier(["fond", "background"], bt.EXT_IMAGES)
        serveur, port = ouvrir_serveur(args.port)
        app = Application(port, fond if isinstance(fond, Path) else None)
        serveur.RequestHandlerClass = fabriquer_gestionnaire(app)
        serveur.daemon_threads = True
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        threading.Thread(target=app.surveiller_tunnel, daemon=True).start()
        app.garde_fermeture = surveiller_fermeture(app)   # doit rester référencé
        if console:
            dossier = choisir_dossier(args)
            if interactif:
                afficher("\nComment les téléphones se connectent-ils ?")
                afficher("  1) Par le Wi-Fi de la maison")
                afficher("  2) Par Internet (4G ou autre Wi-Fi)")
                args.internet = bt.demander("Votre choix [1] : ", {"1", "2"}, defaut="1") == "2"
            quiz = app.preparer_partie(dossier, "classique", args.nouveaux_choix)
            if args.internet:
                app.ouvrir_internet()
            app.installer_partie(quiz, duree_lot(dossier), dossier)
            afficher("\n" + "=" * 64)
            afficher(f"  Blind test prêt : {len(quiz['questions'])} questions de {duree_lot(dossier)} s "
                     f"({dossier.name})")
            afficher(f"  Joueurs ({'Internet' if args.internet else 'même Wi-Fi'}) : {app.url_joueur}")
            afficher(f"  Code de la partie : {app.code}   (inclus dans le QR code affiché à l'écran)")
            afficher(f"  Écran principal : {app.url_ecran}")
            afficher("  Arrêter la partie : fermez cette fenêtre ou Ctrl+C")
            afficher("=" * 64)
            if not args.internet:
                afficher("Si le Mac demande d'accepter les connexions entrantes pour Python : cliquez sur « Autoriser »." if os.name != "nt" else
                         "Si Windows demande l'autorisation du pare-feu : cochez « Réseaux privés » et autorisez.")
            if not args.sans_navigateur:
                webbrowser.open(app.url_ecran)
        else:
            (bt.BASE / FICHIER_ATELIER).write_text(app.url_atelier, encoding="utf-8")
            afficher("=" * 64)
            afficher("  BLIND TEST : la page s'ouvre dans votre navigateur.")
            afficher(f"  Si elle ne s'ouvre pas, copiez cette adresse : {app.url_atelier}")
            afficher("  Laissez cette fenêtre ouverte (vous pouvez la réduire).")
            afficher("  Pour tout arrêter : bouton « Quitter » de la page, ou fermez cette fenêtre.")
            afficher("=" * 64)
            afficher("Si le Mac demande d'accepter les connexions entrantes pour Python : cliquez sur « Autoriser »." if os.name != "nt" else
                     "Si Windows demande l'autorisation du pare-feu : cochez « Réseaux privés » et autorisez.")
            if not args.sans_navigateur:
                webbrowser.open(app.url_atelier)
        while not app.arret.wait(1):
            pass
        afficher("\nBlind test arrêté (bouton « Quitter » de l'atelier).")
    except bt.Erreur as e:
        afficher(f"\nERREUR : {e}")
        if interactif or not console:
            input("\nAppuyez sur Entrée pour fermer...")
        sys.exit(1)
    except KeyboardInterrupt:
        afficher("\nBlind test arrêté (Ctrl+C).")
    except Exception:
        traceback.print_exc()
        afficher(f"\nERREUR inattendue : le blind test s'est arrêté. Le détail est dans {FICHIER_JOURNAL}.")
        try:
            input("\nAppuyez sur Entrée pour fermer...")
        except (EOFError, OSError):
            pass
        sys.exit(1)
    finally:
        if app:
            app.fermer()


if __name__ == "__main__":
    main()
