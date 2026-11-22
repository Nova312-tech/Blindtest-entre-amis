import json
from http.server import ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import blindtest as bt
import jeu


class JeuTests(unittest.TestCase):
    def test_liens_spotify_courts_sont_valides_strictement(self):
        self.assertTrue(bt.spotify_hote_autorise("https://spotify.link/abc"))
        self.assertTrue(bt.spotify_hote_autorise("https://open.spotify.com/playlist/1234567890"))
        self.assertFalse(bt.spotify_hote_autorise("http://127.0.0.1:8765/callback"))
        self.assertFalse(bt.spotify_hote_autorise("http://evil.test/?spotify.link"))
        self.assertFalse(bt.spotify_hote_autorise("https://open.spotify.com@evil.test/playlist/1234567890"))
        with self.assertRaises(bt.Erreur):
            bt.lire_spotify("http://evil.test/?spotify.link")

    def test_titre_est_toujours_affiche_avant_artiste(self):
        dossier = Path(jeu.__file__).parent / "jeu"
        joueur = (dossier / "joueur.html").read_text(encoding="utf-8")
        ecran = (dossier / "ecran.html").read_text(encoding="utf-8")
        self.assertIn("<span><span class=\"t\">${echapper(c.titre)}</span><span class=\"artiste\">${echapper(c.artiste)}</span></span>", joueur)
        self.assertIn('<span><span class="titre">${echapper(c.titre)}</span><span class="artiste">${echapper(c.artiste)}</span></span>', ecran)
        self.assertNotIn('e.forme === "meme_titre"', joueur)
        self.assertNotIn('const qui = forme === "meme_titre"', ecran)
        self.assertNotIn('c.artiste}</span></span><span class="artiste">${echapper(c.titre)', ecran)

    def test_spotify_configurable_sans_secret_et_sans_connexion(self):
        with tempfile.TemporaryDirectory() as temporaire:
            fichier = Path(temporaire) / "spotify.json"
            with patch.object(bt, "FICHIER_SPOTIFY", fichier):
                etat = bt.spotify_etat()
                self.assertFalse(etat["configure"])
                self.assertFalse(etat["connecte"])
                bt.spotify_configurer("AbCdEf123456")
                etat = bt.spotify_etat()
                self.assertTrue(etat["configure"])
                self.assertFalse(etat["connecte"])
                bt.spotify_deconnecter()
                self.assertTrue(fichier.exists())
                self.assertFalse(bt.spotify_etat()["connecte"])

    def test_vote_recu_est_confirme_et_score_calcule(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}]), encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                question = {
                    "audio": "01.mp3",
                    "bonne": 1,
                    "choix": [{"titre": "Mauvais", "artiste": "X"}, {"titre": "Bon", "artiste": "Y"}],
                }
                partie = jeu.Partie({"questions": [question]}, 10, dossier, "partie", "123456")
                joueur = jeu.joueur_neuf("jeton", "Camille", time.time())
                partie.joueurs[joueur["jeton"]] = joueur
                partie.phase = "question"
                partie.idx = 0
                partie.debut = time.monotonic() - 2

                self.assertTrue(partie.voter("jeton", 1))
                self.assertFalse(partie.voter("jeton", 1))
                self.assertEqual(partie.phase, "resultats")
                self.assertGreater(joueur["score"], 0)
                self.assertEqual(partie.etat("jeton")["moi"]["a_vote"], True)

    def test_deux_erreurs_de_suite_donnent_un_pouvoir_et_le_malus_touche_sa_cible(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [{"audio": f"{n:02d}.mp3", "bonne": 0,
                          "choix": [{"titre": "Bon", "artiste": "A"}, {"titre": "Faux", "artiste": "B"}]}
                         for n in range(1, 4)]
            (dossier / "lot.json").write_text(json.dumps([{"id": f"track-{n}"} for n in range(1, 4)]),
                                               encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                partie = jeu.Partie({"questions": questions}, 10, dossier, "partie", "123456")
                joueurs = [jeu.joueur_neuf(jeton, pseudo, time.time())
                           for jeton, pseudo in (("a", "Alice"), ("b", "Bob"))]
                partie.joueurs = {j["jeton"]: j for j in joueurs}

                for numero in (0, 1):
                    partie.idx = numero
                    partie.phase = "question"
                    partie.votes.clear()
                    partie.debut = time.monotonic()
                    self.assertTrue(partie.voter("a", 1))
                    self.assertTrue(partie.voter("b", 0))

                alice = partie.joueurs["a"]
                self.assertTrue(alice["pouvoir_charge"])
                self.assertEqual(alice["rates_consecutifs"], 0)
                self.assertTrue(partie.etat("a")["moi"]["pouvoir_gagne"])
                self.assertEqual(len(partie.etat("a")["moi"]["pouvoirs"]), 4)
                score_avant_pouvoir = alice["score"]

                self.assertFalse(partie.choisir_pouvoir("a", "inconnu", "b"))
                self.assertFalse(partie.choisir_pouvoir("a", "tomate", "a"))
                self.assertTrue(partie.choisir_pouvoir("a", "tomate", "b"))
                self.assertFalse(alice["pouvoir_charge"])
                self.assertEqual(alice["score"], score_avant_pouvoir)
                self.assertEqual(partie.etat("a")["moi"]["pouvoir_utilise"],
                                 {"nom": "Tomate à l’écran", "cible": "Bob"})
                self.assertFalse(partie.choisir_pouvoir("a", "brouillard", "b"))

                partie.lancer_question(2)
                partie.minuteur.cancel()
                self.assertEqual(partie.etat("b")["moi"]["malus"]["id"], "tomate")
                self.assertIsNone(partie.etat("a")["moi"]["malus"])

    def test_pouvoirs_en_mode_annee_et_cibles_dans_les_equipes(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [{"audio": "01.mp3", "type": "annee", "annee": 2000},
                         {"audio": "02.mp3", "type": "annee", "annee": 2001}]
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}, {"id": "track-2"}]),
                                               encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                partie = jeu.Partie({"questions": questions}, 10, dossier, "partie", "123456", equipes=2)
                joueurs = [jeu.joueur_neuf(jeton, pseudo, time.time())
                           for jeton, pseudo in (("a", "Alice"), ("b", "Bob"), ("c", "Chloé"))]
                partie.joueurs = {j["jeton"]: j for j in joueurs}
                partie.joueurs["a"]["equipe"] = 0
                partie.joueurs["b"]["equipe"] = 0
                partie.joueurs["c"]["equipe"] = 1
                partie._equilibrer()
                partie.joueurs["a"]["pouvoir_charge"] = True
                partie.phase = "resultats"
                partie.idx = 0
                partie.resultats = {"comptes": [], "rapide": None, "proches": [], "gains": {}}

                moi = partie.etat("a")["moi"]
                self.assertEqual([p["id"] for p in moi["pouvoirs"]],
                                 ["tomate", "brouillard", "ecran_cinema"])
                self.assertEqual([c["jeton"] for c in moi["cibles"]], ["c"])
                self.assertFalse(partie.choisir_pouvoir("a", "brouillard", "b"))
                self.assertTrue(partie.choisir_pouvoir("a", "brouillard", "c"))

    def test_pouvoir_et_cible_en_attente_sont_restaurees_apres_reprise(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [{"audio": "01.mp3", "bonne": 0, "choix": [{"titre": "Titre", "artiste": "Artiste"}]}]
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}]), encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                partie = jeu.Partie({"questions": questions}, 10, dossier, "partie", "123456")
                sauvegarde = {
                    "suite": 0,
                    "questions": questions,
                    "joueurs": [
                        {"jeton": "a", "pseudo": "Alice", "arrivee": 1, "pouvoir_charge": True,
                         "rates_consecutifs": 1},
                        {"jeton": "b", "pseudo": "Bob", "arrivee": 2,
                         "malus_suivant": {"id": "tomate", "nom": "Tomate à l’écran"}},
                    ],
                }
                partie.reprendre(sauvegarde)

                self.assertTrue(partie.joueurs["a"]["pouvoir_charge"])
                self.assertEqual(partie.joueurs["a"]["rates_consecutifs"], 1)
                self.assertEqual(partie.joueurs["b"]["malus_suivant"]["id"], "tomate")

    def test_route_http_pouvoir_transmet_la_cible_et_le_malus(self):
        partie = SimpleNamespace(choisir_pouvoir=lambda jeton, pouvoir, cible: (jeton, pouvoir, cible) ==
                                 ("alice", "brouillard", "bob"))
        serveur = ThreadingHTTPServer(("127.0.0.1", 0), jeu.fabriquer_gestionnaire(SimpleNamespace(partie=partie)))
        fil = threading.Thread(target=serveur.serve_forever)
        fil.start()
        try:
            demande = urllib.request.Request(
                f"http://127.0.0.1:{serveur.server_port}/pouvoir",
                data=json.dumps({"jeton": "alice", "pouvoir": "brouillard", "cible": "bob"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(demande) as reponse:
                resultat = json.load(reponse)
            self.assertTrue(resultat["ok"])
        finally:
            serveur.shutdown()
            fil.join()
            serveur.server_close()

    def test_evolution_score_est_personnelle_et_inclut_les_questions_sans_points(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [
                {"audio": "01.mp3", "bonne": 0, "choix": [
                    {"titre": "Bon", "artiste": "A"}, {"titre": "Faux", "artiste": "B"}]},
                {"audio": "02.mp3", "bonne": 1, "choix": [
                    {"titre": "Faux", "artiste": "A"}, {"titre": "Bon", "artiste": "B"}]},
            ]
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}, {"id": "track-2"}]), encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                partie = jeu.Partie({"questions": questions}, 10, dossier, "partie", "123456")
                joueurs = [jeu.joueur_neuf(jeton, pseudo, time.time())
                           for jeton, pseudo in (("a", "Alice"), ("b", "Bob"))]
                partie.joueurs = {j["jeton"]: j for j in joueurs}
                partie.idx = 0
                partie.phase = "question"
                partie.debut = time.monotonic()

                self.assertTrue(partie.voter("a", 0))
                self.assertTrue(partie.voter("b", 1))
                score_alice = partie.joueurs["a"]["score"]
                self.assertGreater(score_alice, 0)
                self.assertEqual(partie.stats[0]["scores"], {"a": score_alice, "b": 0})

                partie.idx = 1
                partie.votes.clear()
                partie.phase = "question"
                partie.debut = time.monotonic()
                self.assertTrue(partie.voter("a", 0))
                self.assertTrue(partie.voter("b", 1))
                partie.suivant()

                evolution_alice = partie.etat("a")["moi"]["evolution"]
                evolution_bob = partie.etat("b")["moi"]["evolution"]
                self.assertEqual([p["score"] for p in evolution_alice], [0, score_alice, score_alice])
                self.assertEqual([p["score"] for p in evolution_bob], [0, 0, joueurs[1]["score"]])
                self.assertEqual(evolution_alice[-1]["numero"], 2)

    def test_evolution_indisponible_si_reprise_sans_anciens_scores(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [{"audio": "01.mp3", "bonne": 0, "choix": [{"titre": "Bon"}]}]
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}]), encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", dossier / "partie.json"):
                partie = jeu.Partie({"questions": questions}, 10, dossier, "partie", "123456")
                joueur = jeu.joueur_neuf("jeton", "Camille", time.time())
                partie.joueurs["jeton"] = joueur
                partie.stats = [{"numero": 1, "reponse": "Bon", "taux": 0}]
                partie.phase = "fin"

                self.assertEqual(partie.etat("jeton")["moi"]["evolution"], [])

    def test_reprise_restaure_scores_et_progression(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            questions = [{"audio": "01.mp3", "bonne": 0, "choix": [{"titre": "Titre", "artiste": "Artiste"}]}]
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}]), encoding="utf-8")
            sauvegarde = {
                "dossier": str(dossier),
                "empreinte": ["track-1"],
                "questions": questions,
                "joueurs": [{"jeton": "jeton", "pseudo": "Camille", "arrivee": 1, "score": 750}],
                "suite": 0,
                "id": "partie",
                "code": "123456",
            }
            fichier = dossier / "partie.json"
            fichier.write_text(json.dumps(sauvegarde), encoding="utf-8")
            with patch.object(jeu, "FICHIER_PARTIE", fichier):
                reprise = jeu.lire_sauvegarde()
                self.assertIsNotNone(reprise)
                partie = jeu.Partie({"questions": questions}, 10, dossier, "autre", "000000")
                partie.reprendre(reprise)

            self.assertEqual(partie.idx, -1)
            self.assertEqual(partie.joueurs["jeton"]["score"], 750)
            self.assertEqual(partie.joueurs["jeton"]["pseudo"], "Camille")

    def test_quiz_deja_prepare_ne_refait_pas_les_recherches(self):
        with tempfile.TemporaryDirectory() as temporaire:
            dossier = Path(temporaire)
            empreinte = ["track-1"]
            quiz = {"version": jeu.VERSION_QUIZ, "empreinte": empreinte, "questions": [{"numero": 1}]}
            (dossier / "lot.json").write_text(json.dumps([{"id": "track-1"}]), encoding="utf-8")
            (dossier / "quiz.json").write_text(json.dumps(quiz), encoding="utf-8")
            with patch.object(jeu, "proposer", side_effect=AssertionError("le quiz devait être réutilisé")):
                resultat = jeu.preparer_quiz(dossier)

            self.assertEqual(resultat, quiz)

    def test_remplacement_extrait_met_a_jour_audio_et_lot(self):
        with tempfile.TemporaryDirectory() as temporaire:
            racine = Path(temporaire)
            dossier = racine / "extraits_10s"
            dossier.mkdir()
            lot = [{"id": "ancien", "artiste": "Ancien", "titre": "Titre", "reponse": "Ancien - Titre",
                    "famille": "Pop"}]
            (dossier / "lot.json").write_text(json.dumps(lot), encoding="utf-8")
            (dossier / "reponses.txt").write_text("Départ à 0 s\n\n01. Ancien - Titre\n", encoding="utf-8")
            travail = racine / "_travail"

            def traiter(candidat, cible, repertoire, debut, longueur):
                Path(cible).write_bytes(b"nouvel audio")
                return None

            with patch.object(bt, "DOSSIER_TRAVAIL", travail):
                with patch.object(bt, "FICHIER_VERROU", racine / "_generation.lock"):
                    with patch.object(bt, "verifier_outils"):
                        with patch.object(bt, "traiter", side_effect=traiter):
                            with patch.object(bt, "musique_continue", return_value=False):
                                resultat = bt.remplacer_extrait(
                                    dossier, 1, {"id": "nouveau", "artiste": "Nouvel", "titre": "Morceau"}, 10
                                )

            lot_apres = json.loads((dossier / "lot.json").read_text(encoding="utf-8"))
            self.assertEqual(resultat["id"], "nouveau")
            self.assertEqual(lot_apres[0]["famille"], "Pop")
            self.assertEqual((dossier / "01.mp3").read_bytes(), b"nouvel audio")

    def test_generateur_qr_est_sert_par_le_serveur_local(self):
        serveur = ThreadingHTTPServer(("127.0.0.1", 0), jeu.fabriquer_gestionnaire(SimpleNamespace()))
        fil = threading.Thread(target=serveur.serve_forever)
        fil.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{serveur.server_port}/qrcode.min.js") as reponse:
                contenu = reponse.read().decode("utf-8")
            self.assertIn("QRCode", contenu)
        finally:
            serveur.shutdown()
            fil.join()
            serveur.server_close()

    def test_test_connexion_atelier_interroge_deezer(self):
        app = SimpleNamespace(
            cle_atelier="cle-test",
            apercu=None,
            modifier_lot=None,
            remplacer=None,
            garder_essai=None,
            renommer=None,
            supprimer=None,
        )
        serveur = ThreadingHTTPServer(("127.0.0.1", 0), jeu.fabriquer_gestionnaire(app))
        fil = threading.Thread(target=serveur.serve_forever)
        fil.start()
        try:
            demande = urllib.request.Request(
                f"http://127.0.0.1:{serveur.server_port}/atelier/connexion?cle=cle-test",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(bt, "api", return_value={"data": []}) as api:
                with urllib.request.urlopen(demande) as reponse:
                    resultat = json.load(reponse)
            self.assertTrue(resultat["ok"])
            api.assert_called_once_with("/search?q=Daft%20Punk&limit=1")
        finally:
            serveur.shutdown()
            fil.join()
            serveur.server_close()


if __name__ == "__main__":
    unittest.main()
