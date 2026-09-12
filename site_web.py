# -*- coding: utf-8 -*-
"""
Site web d'administration : portail unique pour Valerius (missions) et
Osiris (blâmes/discipline). Un même compte donne accès aux deux zones.
Tourne dans le MÊME processus Flask que le "keep_alive" du bot (bot.py) :
même serveur Render, un seul déploiement.

Ce module ne fait AUCUNE hypothèse sur les données du bot : toutes les
fonctions dont il a besoin (lecture/écriture des missions, des profils,
des backups...) lui sont injectées via configurer_site(app, bot, deps)
pour éviter tout import circulaire avec bot.py.

================= SYSTEME DE RÔLES =================
Quatre rôles, du plus faible au plus fort :
  greyjoy < malgache < instructeur < proprietaire

- N'importe qui avec le lien peut créer un compte via /inscription, en
  indiquant s'il fait déjà partie du pays (rôle "malgache") ou s'il va
  être recruté (rôle "greyjoy", accès très restreint en attendant). Dans
  les deux cas l'inscrit doit choisir le serveur Discord auquel il
  appartient. Ce choix est DÉFINITIF de son côté : lui seul ne peut plus
  le changer ensuite.
- "greyjoy" : compte en attente de recrutement. Accès volontairement
  minimal : uniquement la cloche 🔔, les paramètres ⚙️, l'IA 🧠 et la carte
  🗺️. Un instructeur peut ensuite le faire passer "malgache" une fois
  recruté (voir /admin/comptes).
- "malgache" : accès de base (/mon-profil, historique personnel)
  + accès en lecture au catalogue de missions de son serveur
  (/mon-catalogue).
- "instructeur" et plus : accède à /admin/serveurs (scope limité à
  son serveur assigné, sauf proprietaire : tous), peut gérer le
  catalogue de missions (ajout/suppression) et les comptes du site
  (créer/modifier/supprimer), mais seulement pour son propre
  serveur, et seulement des comptes d'un rôle strictement inférieur
  au sien (impossible de créer/modifier un compte proprietaire si
  on n'est pas soi-même proprietaire).
- "proprietaire" (Propriétaire) : seul rang avec un accès total et
  global, sur tous les serveurs, y compris les sauvegardes
  complètes (/admin/backup). C'est aussi le seul rang habilité à
  attribuer le rôle "proprietaire" à un autre compte, ou à changer
  le serveur assigné à n'importe quel compte.
  Le compte historique MAVIE7620 est toujours proprietaire.
"""
import os
import json
import secrets
import functools
import asyncio
import queue
import threading
from datetime import datetime, timedelta

import discord
import requests
from urllib.parse import urlencode
from flask import request, redirect, url_for, session, send_file, abort, render_template_string, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ================= BOUTIQUE — IMAGES DE PRODUITS =================
# Les images uploadées par les instructeurs sont stockées telles quelles sur
# le disque (dans ce dossier, à côté des fichiers JSON du bot) et servies via
# la route /boutique-images/<nom_fichier> ci-dessous. Un produit peut aussi
# utiliser une simple URL externe (image_url) au lieu d'un fichier uploadé,
# ce qui évite d'avoir à héberger l'image soi-même.
DOSSIER_IMAGES_BOUTIQUE = "boutique_images"
EXTENSIONS_IMAGE_AUTORISEES = {"png", "jpg", "jpeg", "webp", "gif"}
TAILLE_MAX_IMAGE_OCTETS = 5 * 1024 * 1024  # 5 Mo


def _extension_image_autorisee(nom_fichier):
    return "." in nom_fichier and nom_fichier.rsplit(".", 1)[1].lower() in EXTENSIONS_IMAGE_AUTORISEES


def _enregistrer_image_produit(fichier):
    """Sauvegarde un fichier uploadé (champ <input type=file>) sur le disque
    et renvoie son nom (à stocker dans le produit), ou None si aucun fichier
    valide n'a été fourni (champ vide, extension non reconnue, ou trop
    volumineux) — sans jamais lever d'exception. Générique : utilisé par la
    boutique ET par la roue (voir DOSSIER_IMAGES_BOUTIQUE, réutilisé tel
    quel pour les deux — même dossier, même route de service
    /boutique-images/<nom_fichier>, donc même synchronisation Google Drive
    automatique côté bot.py sans rien à changer là-bas)."""
    if not fichier or not fichier.filename:
        return None
    if not _extension_image_autorisee(fichier.filename):
        return None
    try:
        fichier.seek(0, os.SEEK_END)
        taille = fichier.tell()
        fichier.seek(0)
        if taille > TAILLE_MAX_IMAGE_OCTETS:
            return None
        os.makedirs(DOSSIER_IMAGES_BOUTIQUE, exist_ok=True)
        extension = fichier.filename.rsplit(".", 1)[1].lower()
        nom_unique = f"{secrets.token_hex(8)}.{extension}"
        fichier.save(os.path.join(DOSSIER_IMAGES_BOUTIQUE, nom_unique))
        return nom_unique
    except Exception:
        return None

# ================= WIDGET GÉNÉRIQUE : ZONE DE DÉPÔT D'IMAGE (DRAG & DROP) =================
# Réutilisé partout où une image peut être uploadée (boutique, roue...).
# Purement front-end : au drop (ou au clic classique, qui ouvre le
# sélecteur de fichiers), le fichier choisi est assigné au <input
# type="file"> caché à l'intérieur de la zone, donc le formulaire englobant
# (qui doit être enctype="multipart/form-data") le soumet exactement comme
# un input file classique — zéro changement supplémentaire côté serveur
# pour en bénéficier, au-delà de lire request.files comme d'habitude.
# Plusieurs zones peuvent coexister sur une même page (ex: une par part de
# la roue) : le script se branche sur TOUTES les ".dropzone" trouvées au
# chargement, sans avoir besoin d'un id unique par zone.
STYLE_DROPZONE = """
<style>
  .dropzone {
    display:flex; align-items:center; justify-content:center; gap:8px;
    border:2px dashed var(--border); border-radius:10px; padding:12px 16px; min-width:200px;
    text-align:center; cursor:pointer; transition:border-color .15s ease, background .15s ease;
    font-size:12.5px; color:var(--muted, #9aa3b2);
  }
  .dropzone:hover, .dropzone.dropzone-survol { border-color:var(--accent, #5865f2); background:rgba(88,101,242,0.08); }
  .dropzone-icone { font-size:18px; }
  .dropzone-texte strong { color:var(--text); }
  .dropzone input[type="file"] { display:none; }
</style>
"""

SCRIPT_DROPZONE = """
<script>
(function() {
  function initDropzone(zone) {
    if (zone.dataset.dropzoneInit) return;
    zone.dataset.dropzoneInit = "1";
    var input = zone.querySelector('input[type="file"]');
    var texte = zone.querySelector('.dropzone-texte');
    if (!input || !texte) return;
    var texteDefaut = texte.innerHTML;
    function afficherFichier(fichier) {
      texte.innerHTML = fichier ? ("<strong>" + fichier.name + "</strong>") : texteDefaut;
    }
    zone.addEventListener('click', function() { input.click(); });
    input.addEventListener('change', function() { afficherFichier(input.files[0]); });
    ['dragenter', 'dragover'].forEach(function(evt) {
      zone.addEventListener(evt, function(e) {
        e.preventDefault(); e.stopPropagation();
        zone.classList.add('dropzone-survol');
      });
    });
    ['dragleave', 'drop'].forEach(function(evt) {
      zone.addEventListener(evt, function(e) {
        e.preventDefault(); e.stopPropagation();
        zone.classList.remove('dropzone-survol');
      });
    });
    zone.addEventListener('drop', function(e) {
      var fichiers = e.dataTransfer.files;
      if (fichiers && fichiers.length) {
        input.files = fichiers;
        afficherFichier(fichiers[0]);
      }
    });
  }
  document.querySelectorAll('.dropzone').forEach(initDropzone);
})();
</script>
"""

# ================= TEMPS RÉEL — CLOCHE 🔔 (Server-Sent Events) =================
# Registre en mémoire des connexions "live" ouvertes par les navigateurs sur
# /api/notifications/flux, indexées par (guild_id, joueur_id). Quand une
# notification est ajoutée côté bot (bot.py -> ajouter_notification), celui-ci
# appelle notifier_maj_notifications(guild_id, joueur_id) ci-dessous, qui
# réveille instantanément toutes les connexions concernées : la cloche se
# met à jour sans attendre le prochain sondage. Si le flux SSE est fermé ou
# indisponible (proxy, navigateur trop vieux...), le script côté page
# retombe automatiquement sur un sondage classique toutes les 15 secondes,
# donc la cloche reste fonctionnelle dans tous les cas.
_VERROU_ABONNES_NOTIFICATIONS = threading.Lock()
_ABONNES_NOTIFICATIONS = {}  # {(guild_id:int, joueur_id:str): [queue.Queue, ...]}


def notifier_maj_notifications(guild_id, joueur_id):
    """À appeler après toute création/modification de notification pour
    `joueur_id` sur `guild_id` : réveille en temps réel les onglets ouverts
    de ce joueur sur le site (cloche 🔔)."""
    cle = (int(guild_id), str(joueur_id))
    with _VERROU_ABONNES_NOTIFICATIONS:
        files_abonnees = list(_ABONNES_NOTIFICATIONS.get(cle, []))
    for f in files_abonnees:
        try:
            f.put_nowait(True)
        except Exception:
            pass

COMPTES_FILE = "valerius_comptes.json"
SECRET_KEY_FILE = "valerius_secret.key"
TENTATIVES_FILE = "valerius_tentatives_connexion.json"
COMPTE_PROPRIETAIRE_LOGIN = "MAVIE7620"

# ================= LIAISON DE COMPTE DISCORD (OAuth2) =================
# Permet à quiconque de relier SON VRAI compte Discord (bouton "Lier mon
# compte Discord") au lieu de recopier son ID à la main : plus fiable, et
# ça prouve que c'est bien le sien (Discord ne renvoie l'ID qu'après que
# la personne se soit connectée et ait autorisé l'accès).
# Ces deux identifiants viennent d'une application créée sur
# https://discord.com/developers/applications (onglet OAuth2), à définir
# comme variables d'environnement DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET.
# L'URL de redirection (calculée automatiquement, voir _url_redirection_discord)
# doit être ajoutée telle quelle dans l'onglet OAuth2 > Redirects de cette
# application Discord, sinon Discord refusera la liaison.
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_API_BASE = "https://discord.com/api"

ROLES_ORDRE = ["greyjoy", "malgache", "instructeur", "proprietaire"]
ROLE_LABELS = {
    "greyjoy": "Grey Joy",
    "malgache": "Malgache",
    "instructeur": "Instructeur",
    "proprietaire": "Propriétaire",
}
# ================= RÔLE "GREY JOY" (compte en attente de recrutement) =================
# Choisi à l'inscription par toute personne qui indique ne pas encore faire
# partie du pays ("je vais être recruté"). Volontairement placé EN DESSOUS
# de "malgache" dans la hiérarchie : niveau_role("greyjoy") = 0, donc tout
# ce qui est protégé par @role_required("malgache") (ou plus) lui reste
# fermé (abort 403) sans rien avoir à coder de spécifique route par route.
# Ses SEULS accès valides sont ceux volontairement laissés en
# @login_required simple (jamais remontés à role_required) : la cloche 🔔
# (/notifications), les paramètres ⚙️ (/parametres), l'IA 🧠 (/ia) et la
# carte 🗺️ (/carte). Voir aussi _actions_rapides_nav_html() (icônes de nav)
# et page_accueil_pays() (portail "/") qui masquent tout le reste pour ce
# rôle côté affichage.
ROLES_SANS_ACCES_ZONES = {"greyjoy"}

# ================= SÉCURITÉ : ANTI BRUTE-FORCE & HISTORIQUE =================
MAX_TENTATIVES_CONNEXION = 6
FENETRE_TENTATIVES = timedelta(minutes=10)
DUREE_BLOCAGE_CONNEXION = timedelta(minutes=15)
MAX_HISTORIQUE_CONNEXIONS = 20
# Nombre max d'échecs tolérés sur UN SEUL identifiant, tous IP confondues.
# En plus du blocage par IP (facilement contournable en falsifiant l'en-tête
# X-Forwarded-For), ce blocage par COMPTE protège même si l'attaquant change
# d'IP apparente à chaque tentative : au bout de MAX_TENTATIVES_COMPTE échecs
# sur "MAVIE7620" par exemple, ce compte précis est bloqué temporairement,
# quelle que soit l'IP (ou la fausse IP) utilisée.
MAX_TENTATIVES_COMPTE = 10
DUREE_BLOCAGE_COMPTE = timedelta(minutes=15)

# ================= SÉCURITÉ : MOTS DE PASSE =================
LONGUEUR_MIN_MOT_DE_PASSE = 10
# Renouvellement obligatoire tous les 6 mois (≈ 182 jours). Un compte sans
# date de changement enregistrée (créé avant l'ajout de cette règle) est
# considéré comme expiré : il devra en définir un nouveau à la prochaine
# connexion, comme s'il s'agissait d'un mot de passe temporaire.
DUREE_VALIDITE_MOT_DE_PASSE = timedelta(days=182)


def _definir_mot_de_passe(compte, mot_de_passe_clair):
    """Fixe le hash du mot de passe ET la date de changement (utilisée pour
    calculer l'expiration à 6 mois) : à utiliser PARTOUT où un mot de passe
    est créé ou modifié, plutôt que d'assigner "password_hash" à la main,
    pour ne jamais oublier de rafraîchir la date."""
    compte["password_hash"] = generate_password_hash(mot_de_passe_clair)
    compte["password_changed_at"] = datetime.now().isoformat()


def _mot_de_passe_expire(compte):
    """Vrai si le mot de passe de ce compte a plus de 6 mois, ou si aucune
    date de changement n'est enregistrée (compte créé avant cette règle)."""
    date_str = compte.get("password_changed_at")
    if not date_str:
        return True
    try:
        date_changement = datetime.fromisoformat(date_str)
    except Exception:
        return True
    return datetime.now() - date_changement > DUREE_VALIDITE_MOT_DE_PASSE

# ================= CONFIRMATION PAR MP DISCORD (actions sensibles) =================
# Token -> {"code", "login_acteur", "expire", "description", "donnees_action"}.
# Volontairement en mémoire (non persisté) : une confirmation en attente
# n'a pas besoin de survivre à un redémarrage, elle expire en quelques
# minutes de toute façon.
CONFIRMATIONS_EN_ATTENTE = {}
# Verrou protégeant CONFIRMATIONS_EN_ATTENTE : le site tourne en threaded=True
# (plusieurs requêtes traitées en parallèle par des threads différents), et
# ce dictionnaire est modifié depuis plusieurs endroits (création, nettoyage
# des entrées expirées, validation). Sans verrou, deux actions sensibles
# déclenchées à quelques millisecondes d'écart peuvent provoquer une
# "RuntimeError: dictionary changed size during iteration" -> page 500.
_VERROU_CONFIRMATIONS = threading.Lock()
DUREE_VALIDITE_CONFIRMATION = timedelta(minutes=5)


def niveau_role(role):
    try:
        return ROLES_ORDRE.index(role)
    except (ValueError, TypeError):
        return 0


# ================= APERÇU D'UN AUTRE GRADE (staff uniquement) =================
# Permet à un instructeur/propriétaire de voir le site (portail "/", icônes
# de navigation, badge de rôle...) comme le voit un autre grade, pour
# vérifier/tester l'affichage — SANS jamais toucher à ses droits réels :
# les routes protégées par @role_required restent contrôlées par le VRAI
# rôle du compte (lu en base), jamais par cet aperçu. Impossible donc de se
# bloquer soi-même l'accès à l'administration en prévisualisant "malgache"
# ou "greyjoy". L'aperçu est stocké en session (donc personnel, temporaire,
# jamais persisté sur disque) sous la clé "apercu_role".
def _etat_apercu(connecte, role_reel):
    """Renvoie (role_a_afficher, role_apercu_actif_ou_None).
    role_a_afficher = role_reel, sauf si un aperçu est actif, auquel cas
    c'est le rôle prévisualisé qui est renvoyé (pour l'affichage
    uniquement)."""
    if not connecte or niveau_role(role_reel) < niveau_role("instructeur"):
        return role_reel, None
    apercu = session.get("apercu_role")
    if not apercu or apercu not in ROLES_ORDRE:
        return role_reel, None
    return apercu, apercu


def _bandeau_apercu_html(apercu_label):
    if not apercu_label:
        return ""
    return (
        '<div class="bandeau-apercu">🔍 Aperçu activé : tu vois le site comme '
        f'le grade « {ROLE_LABELS.get(apercu_label, apercu_label)} ». '
        'Tes droits réels ne changent pas. '
        '<a href="/parametres/apercu/arreter">Quitter l\'aperçu</a></div>'
    )


def guild_autorise(compte, guild_id):
    """True si ce compte a le droit de voir/gérer les données de ce serveur.
    Seul le rang Propriétaire a un accès global à tous les serveurs — un
    Instructeur, lui, reste limité à son unique serveur assigné."""
    if not compte:
        return False
    if compte.get("role") == "proprietaire":
        return True
    return str(compte.get("guild_id")) == str(guild_id)


# ================= STATISTIQUES DES MISSIONS =================

def formater_duree_secondes(secondes):
    """Formate une durée en secondes en texte lisible (ex: '2j 5h 12min')."""
    if secondes is None or secondes < 0:
        return "—"
    secondes = int(secondes)
    jours, reste = divmod(secondes, 86400)
    heures, reste = divmod(reste, 3600)
    minutes = reste // 60
    if jours:
        return f"{jours}j {heures}h {minutes}min"
    if heures:
        return f"{heures}h {minutes}min"
    return f"{minutes}min"


def calculer_stats_missions(guild_id, deps):
    """Agrège, pour un serveur donné, le taux de réussite global, la mission
    la plus/la moins populaire (nombre de fois attribuée, tous statuts
    confondus) et le temps moyen de complétion des missions réussies."""
    profils = deps["charger_profils"](guild_id)

    total_reussies = 0
    total_echouees = 0
    popularite = {}  # texte -> {"count": int, "categorie": str}
    durees_succes = []

    for profil in profils.values():
        total_reussies += profil.get("total_reussies", 0)
        total_echouees += profil.get("total_echouees", 0)
        for entree in profil.get("historique", []):
            texte = entree.get("texte", "?")
            info = popularite.setdefault(texte, {"count": 0, "categorie": entree.get("categorie", "inconnu")})
            info["count"] += 1
            if entree.get("statut") == "Succès" and entree.get("duree_secondes") is not None:
                durees_succes.append(entree["duree_secondes"])

    total_missions = total_reussies + total_echouees
    taux_reussite = round((total_reussies / total_missions) * 100, 1) if total_missions else None

    mission_plus_populaire = None
    mission_moins_populaire = None
    if popularite:
        mission_plus_populaire = max(popularite.items(), key=lambda kv: kv[1]["count"])
        mission_moins_populaire = min(popularite.items(), key=lambda kv: kv[1]["count"])

    temps_moyen_secondes = (sum(durees_succes) / len(durees_succes)) if durees_succes else None

    return {
        "total_reussies": total_reussies,
        "total_echouees": total_echouees,
        "total_missions": total_missions,
        "taux_reussite": taux_reussite,
        "mission_plus_populaire": mission_plus_populaire,
        "mission_moins_populaire": mission_moins_populaire,
        "nb_missions_distinctes": len(popularite),
        "temps_moyen_secondes": temps_moyen_secondes,
        "temps_moyen_texte": formater_duree_secondes(temps_moyen_secondes),
        "nb_completions_chronometrees": len(durees_succes),
    }


# ================= GESTION DES COMPTES =================

ANCIENS_ROLES_VERS_NOUVEAUX = {
    "user": "malgache",
    "recrue": "malgache",
    "membre": "malgache",
    "admin": "instructeur",
    "super_admin": "instructeur",
}


# Effets visuels du site, activables/désactivables un par un depuis
# /parametres (voir _effets_compte ci-dessous). Toutes les valeurs par
# défaut sont à True pour ne rien changer au rendu pour les comptes déjà
# existants (créés avant l'ajout de ce réglage) tant qu'ils ne touchent
# pas eux-mêmes à ces cases.
EFFETS_PAR_DEFAUT = {
    "fond_3d": True,             # SCRIPT_FOND_3D : décor 3D animé en arrière-plan
    "tilt_3d": True,             # SCRIPT_TILT3D + SCRIPT_BLASONS : inclinaison/lueur des cartes au survol
    "transition_cartes": True,   # SCRIPT_TRANSITION_CARTES : animation en cliquant sur une carte
    "compteurs": True,           # SCRIPT_COMPTEURS : chiffres des stats qui montent progressivement
}


def _effets_compte(compte):
    """Renvoie les préférences d'effets visuels d'un compte (dict complet,
    clés manquantes remplies avec EFFETS_PAR_DEFAUT). `compte` peut être
    None (visiteur non connecté) : on retombe alors sur les valeurs par
    défaut, tout simplement."""
    effets = dict(EFFETS_PAR_DEFAUT)
    if compte:
        for cle, valeur in (compte.get("effets") or {}).items():
            if cle in EFFETS_PAR_DEFAUT:
                effets[cle] = bool(valeur)
    return effets


def _migrer_comptes(comptes):
    """Migration douce des anciens systèmes de rôles (5 puis 6 rôles) vers
    la nouvelle hiérarchie à 3 rôles, sans casser les comptes existants."""
    modifie = False
    for login, c in comptes.items():
        role_actuel = c.get("role")
        if role_actuel in ANCIENS_ROLES_VERS_NOUVEAUX:
            c["role"] = ANCIENS_ROLES_VERS_NOUVEAUX[role_actuel]
            modifie = True
        if login == COMPTE_PROPRIETAIRE_LOGIN and c.get("role") != "proprietaire":
            c["role"] = "proprietaire"
            modifie = True
        if c.get("role") not in ROLES_ORDRE:
            c["role"] = "malgache"
            modifie = True
        c.setdefault("guild_id", None)
        c.setdefault("discord_id", None)
        c.setdefault("effets", dict(EFFETS_PAR_DEFAUT))
    if modifie:
        sauvegarder_comptes(comptes)
    return comptes


def charger_comptes():
    if not os.path.exists(COMPTES_FILE):
        return {}
    try:
        with open(COMPTES_FILE, "r", encoding="utf-8") as f:
            comptes = json.load(f)
    except Exception:
        return {}
    return _migrer_comptes(comptes)


def sauvegarder_comptes(comptes):
    with open(COMPTES_FILE, "w", encoding="utf-8") as f:
        json.dump(comptes, f, indent=4, ensure_ascii=False)


# ================= JOURNAL DES MISES À JOUR DU SITE =================
# Fichier séparé (préfixe "valerius_" pour être automatiquement inclus dans
# les sauvegardes/synchronisation Drive, voir _lister_fichiers_texte_a_sauvegarder
# dans bot.py — aucune modification de bot.py n'est nécessaire). Chaque
# entrée : {"id", "titre", "texte", "date_iso" (comparaison), "date" (affichage)}.
MAJ_FILE = "valerius_maj.json"


def charger_maj():
    if not os.path.exists(MAJ_FILE):
        return []
    try:
        with open(MAJ_FILE, "r", encoding="utf-8") as f:
            maj_liste = json.load(f)
    except Exception:
        return []
    return maj_liste if isinstance(maj_liste, list) else []


def sauvegarder_maj(maj_liste):
    with open(MAJ_FILE, "w", encoding="utf-8") as f:
        json.dump(maj_liste, f, indent=4, ensure_ascii=False)


def _compter_maj_non_lues(compte):
    """Nombre de mises à jour publiées depuis la dernière visite de ce
    compte sur /mises-a-jour (compte["maj_vues_le"]). Si le compte n'a
    jamais visité la page, toutes les entrées comptent comme non lues."""
    if not compte:
        return 0
    maj_liste = charger_maj()
    if not maj_liste:
        return 0
    vues_le = compte.get("maj_vues_le")
    if not vues_le:
        return len(maj_liste)
    try:
        seuil = datetime.fromisoformat(vues_le)
    except Exception:
        return len(maj_liste)
    total = 0
    for m in maj_liste:
        try:
            if datetime.fromisoformat(m.get("date_iso", "")) > seuil:
                total += 1
        except Exception:
            continue
    return total


def _generer_mot_de_passe():
    return secrets.token_urlsafe(9)


def _obtenir_ip_visiteur():
    """Adresse IP du visiteur. Render (comme la plupart des hébergeurs)
    place le site derrière un proxy : la vraie IP arrive dans l'en-tête
    X-Forwarded-For (la première de la liste), pas dans remote_addr."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "inconnue"


def _charger_tentatives():
    if not os.path.exists(TENTATIVES_FILE):
        return {}
    try:
        with open(TENTATIVES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _sauvegarder_tentatives(tentatives):
    # Écriture atomique : on écrit d'abord dans un fichier temporaire puis on
    # le renomme (os.replace, atomique sur un même disque). Ça évite qu'une
    # lecture concurrente (ex: la synchro Google Drive périodique, qui tourne
    # dans un thread séparé) ne tombe sur un fichier vidé/tronqué en plein
    # milieu de l'écriture — ce qui provoquait des erreurs JSON silencieuses
    # et une réinitialisation intempestive des compteurs de tentatives.
    chemin_tmp = TENTATIVES_FILE + ".tmp"
    with open(chemin_tmp, "w", encoding="utf-8") as f:
        json.dump(tentatives, f, ensure_ascii=False)
    os.replace(chemin_tmp, TENTATIVES_FILE)


def _ip_bloquee(ip):
    """Renvoie le nombre de secondes restantes si cette IP est actuellement
    bloquée pour trop de tentatives de connexion échouées, sinon None."""
    info = _charger_tentatives().get(ip)
    if not info or not info.get("bloque_jusqu"):
        return None
    bloque_jusqu = datetime.fromisoformat(info["bloque_jusqu"])
    if datetime.now() >= bloque_jusqu:
        return None
    return int((bloque_jusqu - datetime.now()).total_seconds())


def _enregistrer_echec_connexion(ip):
    """Incrémente le compteur d'échecs pour cette IP (fenêtre glissante de
    FENETRE_TENTATIVES) et déclenche un blocage temporaire au-delà de
    MAX_TENTATIVES_CONNEXION. Renvoie l'état à jour pour cette IP."""
    tentatives = _charger_tentatives()
    info = tentatives.get(ip, {"echecs": 0, "premiere_tentative": None, "bloque_jusqu": None})
    maintenant = datetime.now()
    premiere = datetime.fromisoformat(info["premiere_tentative"]) if info.get("premiere_tentative") else None
    if not premiere or maintenant - premiere > FENETRE_TENTATIVES:
        info = {"echecs": 1, "premiere_tentative": maintenant.isoformat(), "bloque_jusqu": None}
    else:
        info["echecs"] = info.get("echecs", 0) + 1
    if info["echecs"] >= MAX_TENTATIVES_CONNEXION:
        info["bloque_jusqu"] = (maintenant + DUREE_BLOCAGE_CONNEXION).isoformat()
    tentatives[ip] = info
    _sauvegarder_tentatives(tentatives)
    return info


def _reinitialiser_tentatives(ip):
    tentatives = _charger_tentatives()
    if ip in tentatives:
        del tentatives[ip]
        _sauvegarder_tentatives(tentatives)


# ---- Blocage par COMPTE (en plus du blocage par IP ci-dessus) ----
# L'en-tête X-Forwarded-For utilisé pour le blocage par IP peut, sur
# certaines configurations de proxy, être falsifié par le visiteur lui-même
# (voir la doc de _obtenir_ip_visiteur). Pour ne pas dépendre uniquement de
# cette IP, on bloque AUSSI l'identifiant ciblé lui-même après trop
# d'échecs, quelle que soit l'IP (ou la fausse IP) utilisée pour les
# tentatives : même en changeant d'IP à chaque essai, un attaquant ne peut
# pas dépasser MAX_TENTATIVES_COMPTE essais sur un même compte.
def _cle_compte(login):
    return f"compte:{(login or '').strip().upper()}"


def _compte_bloque(login):
    """Renvoie le nombre de secondes restantes si CE COMPTE est actuellement
    bloqué pour trop de tentatives de connexion échouées, sinon None."""
    info = _charger_tentatives().get(_cle_compte(login))
    if not info or not info.get("bloque_jusqu"):
        return None
    bloque_jusqu = datetime.fromisoformat(info["bloque_jusqu"])
    if datetime.now() >= bloque_jusqu:
        return None
    return int((bloque_jusqu - datetime.now()).total_seconds())


def _enregistrer_echec_connexion_compte(login):
    """Équivalent de _enregistrer_echec_connexion mais par identifiant de
    compte plutôt que par IP."""
    tentatives = _charger_tentatives()
    cle = _cle_compte(login)
    info = tentatives.get(cle, {"echecs": 0, "premiere_tentative": None, "bloque_jusqu": None})
    maintenant = datetime.now()
    premiere = datetime.fromisoformat(info["premiere_tentative"]) if info.get("premiere_tentative") else None
    if not premiere or maintenant - premiere > FENETRE_TENTATIVES:
        info = {"echecs": 1, "premiere_tentative": maintenant.isoformat(), "bloque_jusqu": None}
    else:
        info["echecs"] = info.get("echecs", 0) + 1
    if info["echecs"] >= MAX_TENTATIVES_COMPTE:
        info["bloque_jusqu"] = (maintenant + DUREE_BLOCAGE_COMPTE).isoformat()
    tentatives[cle] = info
    _sauvegarder_tentatives(tentatives)
    return info


def _reinitialiser_tentatives_compte(login):
    tentatives = _charger_tentatives()
    cle = _cle_compte(login)
    if cle in tentatives:
        del tentatives[cle]
        _sauvegarder_tentatives(tentatives)


def _enregistrer_connexion_reussie(compte, ip):
    """Ajoute une entrée à l'historique de connexions du compte (affiché
    dans « Mon profil »), la plus récente en premier, limité aux
    MAX_HISTORIQUE_CONNEXIONS dernières entrées."""
    historique = compte.setdefault("historique_connexions", [])
    historique.insert(0, {"date": datetime.now().strftime("%d/%m/%Y à %H:%M:%S"), "ip": ip})
    del historique[MAX_HISTORIQUE_CONNEXIONS:]


async def initialiser_compte_proprietaire(envoyer_log_proprietaire, bot):
    """À appeler une fois au démarrage (dans on_ready) : crée le compte
    propriétaire MAVIE7620 s'il n'existe pas encore, avec un mot de passe
    aléatoire envoyé en MP — jamais écrit en clair dans le code source."""
    comptes = charger_comptes()
    if COMPTE_PROPRIETAIRE_LOGIN in comptes:
        return

    mot_de_passe = _generer_mot_de_passe()
    comptes[COMPTE_PROPRIETAIRE_LOGIN] = {
        "role": "proprietaire",
        "discord_id": None,
        "guild_id": None,
        "must_change_password": True
    }
    _definir_mot_de_passe(comptes[COMPTE_PROPRIETAIRE_LOGIN], mot_de_passe)
    sauvegarder_comptes(comptes)

    try:
        await envoyer_log_proprietaire(
            bot,
            f"🌐 **Compte du site web créé !**\nIdentifiant : `{COMPTE_PROPRIETAIRE_LOGIN}`\n"
            f"Mot de passe temporaire : `{mot_de_passe}`\n"
            f"⚠️ Il te sera demandé de le changer dès la première connexion."
        )
    except Exception:
        print(f"[SITE WEB] Compte {COMPTE_PROPRIETAIRE_LOGIN} créé — mot de passe temporaire : {mot_de_passe}")


def _obtenir_secret_key():
    """Clé de session Flask, générée une fois puis persistée (et incluse
    dans les backups) pour ne pas déconnecter tout le monde à chaque
    redémarrage sur Render."""
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            cle = f.read().strip()
            if cle:
                return cle
    cle = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(cle)
    return cle


# ================= RENDU HTML (sans dossier templates/) =================

STYLE = """
<style>
  :root {
    color-scheme: dark;
    --bg: #05070a;
    --bg-soft: #0a0e13;
    --panel: #12171e;
    --panel-2: #1a212a;
    --border: #242c37;
    --text: #f3f5f7;
    --muted: #8b98a6;
    --gold: #e3b559;
    --gold-2: #f6d488;
    --red: #e5484d;
    --green: #34c98f;
    --green-2: #5fe0ac;
    --azur: #7ec8e3;
    --shadow: 0 12px 34px -14px rgba(0,0,0,0.75);
    --radius: 14px;
    --font-title: "Cinzel", "Segoe UI", serif;
    --font-body: "Inter", "Segoe UI", -apple-system, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin:0; font-family:var(--font-body);
    background: var(--bg);
    color:var(--text); min-height:100vh; line-height:1.55;
    animation: fade-in .4s ease;
  }
  @keyframes fade-in { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }

  /* ---- Fond ambiant du royaume : lueurs douces derrière la grille de
     carrés qui suit la souris (celle-ci reste inchangée, juste redécorée
     avec la nouvelle palette plus bas). Purement décoratif, jamais au-dessus
     du contenu. ---- */
  #fond-royaume {
    position: fixed; inset: 0; z-index: -1; overflow: hidden; pointer-events: none;
    background: var(--bg);
  }
  #fond-royaume::before, #fond-royaume::after {
    content:""; position:absolute; border-radius:50%; filter: blur(90px); opacity:.55;
    animation: royaume-derive 26s ease-in-out infinite alternate;
  }
  #fond-royaume::before {
    width:60vw; height:60vw; max-width:820px; max-height:820px;
    top:-18%; left:-12%;
    background: radial-gradient(circle, rgba(227,181,89,0.22), transparent 70%);
  }
  #fond-royaume::after {
    width:55vw; height:55vw; max-width:760px; max-height:760px;
    bottom:-20%; right:-10%;
    background: radial-gradient(circle, rgba(52,201,143,0.16), transparent 70%);
    animation-delay: -9s;
  }
  @keyframes royaume-derive {
    0% { transform: translate(0,0) scale(1); }
    100% { transform: translate(3%, 4%) scale(1.08); }
  }

  nav { display:flex;align-items:center;gap:12px;min-height:68px;padding:10px 24px;background:rgba(7,9,12,.86);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-bottom:1px solid rgba(232,189,85,.12);position:sticky;top:0;z-index:10;box-shadow:0 8px 30px -16px rgba(0,0,0,.9); }
  nav a { color:var(--muted);text-decoration:none;font-size:14px;font-weight:600;padding:9px 11px;border-radius:10px;transition:all .18s ease; }
  nav a:hover { color:#fff;background:rgba(255,255,255,.05); }
  nav .retour-pays { display:flex;align-items:center;gap:9px;padding:8px 10px 8px 0; }
  nav .retour-pays span { display:flex;flex-direction:column;line-height:1.05; }
  nav .retour-pays strong { color:#fff;font-family:var(--font-title);font-size:14px;letter-spacing:1px; }
  nav .retour-pays small { color:var(--muted);font-size:9px;letter-spacing:1.8px;text-transform:uppercase;margin-top:4px; }
  nav .brand { margin-right:auto;color:var(--muted);font-size:12px;letter-spacing:1.5px;text-transform:uppercase; }
  nav .separateur-nav { display:none; }
  nav .nav-actions { display:flex;align-items:center;gap:5px; }
  nav .nav-icon { position:relative;width:40px;height:40px;display:grid;place-items:center;padding:0;border:1px solid transparent;background:transparent;font-size:18px; }
  nav .nav-icon:hover { border-color:rgba(232,189,85,.18);background:rgba(232,189,85,.06); }
  nav .nav-icon-rond { border-radius:50% !important; width:36px; height:36px; }
  nav .nav-account { display:flex;align-items:center;margin-left:5px;padding:7px 11px;border:1px solid var(--border);background:rgba(255,255,255,.025); }
  nav .nav-user { display:flex;flex-direction:column;line-height:1.05; }
  nav .nav-user-name { color:#fff;font-size:13px; }
  nav .nav-role { color:var(--muted);font-size:10px;margin-top:3px; }
  nav .nav-logout { border-left:1px solid var(--border);margin-left:3px;padding-left:14px; }
  @media(max-width:760px){nav{padding:9px 12px}nav .brand{display:none}nav .nav-user{display:none}}
  .serveurs-grid{display:grid;gap:12px;margin-top:24px}
  .serveur-choix{position:relative;overflow:hidden;transform-style:preserve-3d;will-change:transform;display:flex;align-items:center;gap:16px;padding:18px 20px;border:1px solid var(--border);border-radius:14px;background:linear-gradient(135deg,rgba(255,255,255,.035),rgba(255,255,255,.015));text-decoration:none;color:var(--text);transition:transform .18s ease-out,border-color .18s ease,background .18s ease,box-shadow .18s ease;transform:rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg))}
  .serveur-choix:hover{transform:translateY(-2px) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg));border-color:rgba(232,189,85,.42);background:rgba(232,189,85,.055);box-shadow:0 14px 35px -20px rgba(232,189,85,.35)}
  .serveur-choix-icone{width:46px;height:46px;display:grid;place-items:center;border-radius:12px;background:rgba(232,189,85,.09);font-size:22px}
  .serveur-choix-contenu{flex:1;display:flex;flex-direction:column;gap:5px}.serveur-choix-contenu strong{font-size:16px;color:#fff}.serveur-choix-contenu span{font-size:12px;color:var(--muted)}
  .serveur-choix-fleche{font-size:25px;color:var(--gold-2);transition:transform .18s}.serveur-choix:hover .serveur-choix-fleche{transform:translateX(4px)}
  .serveur-header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:28px}.serveur-header h1{margin-top:5px}
  .outil-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}.outil-card{position:relative;overflow:hidden;transform-style:preserve-3d;will-change:transform;display:flex;flex-direction:column;gap:7px;min-height:145px;padding:22px;border:1px solid var(--border);border-radius:15px;background:linear-gradient(180deg,var(--panel),var(--panel-2));text-decoration:none;color:var(--text);transition:transform .18s ease-out,border-color .18s ease,box-shadow .18s ease;transform:rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg))}.outil-card:hover{transform:translateY(-3px) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg));border-color:rgba(232,189,85,.42);box-shadow:0 18px 40px -25px rgba(232,189,85,.45)}.outil-card b{font-size:27px}.outil-card strong{color:#fff;font-size:16px}.outil-card span{color:var(--muted);font-size:12px}
  @media(max-width:700px){.serveur-header{flex-direction:column}}
  main { max-width:1020px; margin:36px auto; padding:0 22px 70px; perspective:1400px; }
  h1 { font-family:var(--font-title); font-size:28px; margin:0 0 6px; font-weight:800; letter-spacing:.2px; }
  h2 { font-family:var(--font-title); font-size:18px; color:#e6e8ee; margin-top:34px; margin-bottom:10px; font-weight:700; letter-spacing:.2px; }
  .card {
    position:relative; overflow:hidden; transform-style:preserve-3d; will-change:transform;
    background:linear-gradient(180deg, var(--panel), var(--panel) 60%, var(--panel-2));
    border:1px solid var(--border); border-radius:var(--radius); padding:22px 24px; margin:16px 0;
    box-shadow: var(--shadow); transition: border-color .2s ease, transform .15s ease-out, box-shadow .2s ease;
    transform: translateY(0) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg));
  }
  .card::before {
    content:""; position:absolute; inset:0 0 auto 0; height:2px; z-index:2;
    background:linear-gradient(90deg, transparent, rgba(227,181,89,0.55), rgba(52,201,143,0.4), transparent);
  }
  /* ---- Lueur 3D qui suit le curseur (--mx/--my posées par SCRIPT_TILT3D,
     même principe que .zone-lueur-curseur sur l'accueil). ---- */
  .card::after, .outil-card::after, .stat-card::after, .serveur-choix::after {
    content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none; z-index:2; opacity:0;
    background:radial-gradient(260px circle at var(--mx,50%) var(--my,50%), rgba(232,189,85,0.16), transparent 62%);
    transition:opacity .25s ease;
  }
  .card:hover::after, .outil-card:hover::after, .stat-card:hover::after, .serveur-choix:hover::after { opacity:1; }
  .card:hover { border-color:#33404d; transform:translateY(-2px) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg)); box-shadow: 0 18px 40px -14px rgba(0,0,0,0.85); }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--border); font-size:14px; vertical-align:middle; }
  th { color:var(--muted); font-weight:700; font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:rgba(255,255,255,0.025); }
  input, select, button {
    font-family:inherit; font-size:14px; padding:11px 15px; border-radius:9px;
    border:1px solid var(--border); background:var(--bg-soft); color:var(--text);
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  input:focus, select:focus {
    outline:none; border-color: var(--gold); box-shadow:0 0 0 3px rgba(232,189,85,0.15);
  }
  button {
    background:linear-gradient(135deg, var(--gold-2), var(--gold));
    color:#1b1406; border:none; font-weight:700; cursor:pointer;
    padding:12px 20px; letter-spacing:.2px; border-radius:9px;
    box-shadow: 0 6px 16px -6px rgba(232,189,85,0.5);
    transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
  }
  button:hover { transform:translateY(-1px); filter:brightness(1.05); box-shadow:0 10px 20px -6px rgba(232,189,85,0.6); }
  button:active { transform:translateY(0); }
  button.danger {
    background:linear-gradient(135deg, #f0797d, var(--red)); color:#fff;
    box-shadow: 0 6px 16px -6px rgba(229,72,77,0.5);
  }
  button.danger:hover { filter:brightness(1.1); box-shadow:0 10px 20px -6px rgba(229,72,77,0.6); }
  button.secondary {
    background:var(--panel-2); color:var(--text); border:1px solid var(--border);
    box-shadow:none;
  }
  button.secondary:hover { background:#242c37; box-shadow:none; }
  .flash { padding:13px 16px; border-radius:10px; margin-bottom:16px; font-size:14px; font-weight:500; border:1px solid transparent; }
  .flash.erreur { background:rgba(229,72,77,0.14); color:#ffa3a6; border-color:rgba(229,72,77,0.4); }
  .flash.ok { background:rgba(52,201,143,0.12); color:#8bf0c0; border-color:rgba(52,201,143,0.32); }
  .badge {
    display:inline-flex; align-items:center; padding:4px 12px; border-radius:20px;
    font-size:12px; font-weight:700; white-space:nowrap; letter-spacing:.2px;
  }
  .badge.greyjoy { background:rgba(148,163,184,0.14); color:#94a3b8; }
  .badge.malgache { background:rgba(126,200,227,0.14); color:#7ec8e3; }
  .badge.instructeur { background:rgba(52,201,143,0.14); color:#5fe0ac; }
  .badge.proprietaire {
    background:linear-gradient(135deg, rgba(255,209,102,0.2), rgba(255,209,102,0.08));
    color:#ffd166; border:1px solid rgba(255,209,102,0.5);
  }

  /* ---- Bandeau d'aperçu d'un autre grade (staff uniquement) ---- */
  .bandeau-apercu {
    background:rgba(255,209,102,0.12); color:#ffd166; border-bottom:1px solid rgba(255,209,102,0.4);
    padding:10px 22px; font-size:13.5px; font-weight:600; text-align:center;
  }
  .bandeau-apercu a { color:#ffd166; text-decoration:underline; margin-left:8px; }

  /* ---- Bandeau "MAINTENANCE EN COURS" (visible du Propriétaire uniquement,
     le site étant bloqué pour tous les autres pendant la maintenance) ---- */
  .bandeau-maintenance {
    background:rgba(255,90,90,0.16); color:#ff8a8a; border-bottom:1px solid rgba(255,90,90,0.5);
    padding:14px 22px; font-size:16px; font-weight:800; text-align:center; letter-spacing:0.5px;
  }
  .bandeau-maintenance small { display:block; font-weight:500; font-size:12.5px; color:#ffb3b3; margin-top:3px; letter-spacing:normal; }

  /* ---- Icônes d'accès rapide (cloche 🔔 + paramètres ⚙️) : toujours
     affichées côte à côte, sur TOUTE page une fois connecté (jamais
     avant login) — voir _actions_rapides_nav_html(). ---- */
  .nav-actions { display:inline-flex; align-items:center; gap:2px; margin-right:2px; }
  .nav-icon {
    position:relative; display:inline-flex; align-items:center; justify-content:center;
    font-size:17px; padding:7px 11px !important; text-decoration:none; border-radius:10px;
    transition:transform .15s ease, background .15s ease;
  }
  .nav-icon:hover { transform:scale(1.1); background:var(--panel-2); }
  .nav-icon-rond { border-radius:50% !important; width:36px; height:36px; padding:0 !important; }
  .nav-admin { font-size:15px; opacity:.68; }
  .nav-admin:hover { opacity:1; }
  .nav-bell.a-des-notifs { animation: cloche-secoue 1.8s ease-in-out infinite; }
  .notif-badge {
    position:absolute; top:-2px; right:2px; min-width:16px; height:16px; padding:0 4px;
    border-radius:999px; background:var(--red); color:#fff; font-size:10px; font-weight:800;
    line-height:16px; text-align:center; box-shadow:0 0 0 2px var(--panel, #111);
  }
  /* Anneau qui pulse doucement autour du badge tant qu'il y a des
     notifications non lues, pour attirer l'œil sans être criard. */
  .notif-badge::after {
    content:""; position:absolute; inset:-4px; border-radius:999px;
    border:2px solid var(--red); opacity:.65; pointer-events:none;
    animation:notif-onde 1.8s ease-out infinite;
  }
  @keyframes notif-onde {
    0%   { transform:scale(.6); opacity:.7; }
    70%  { transform:scale(1.9); opacity:0; }
    100% { transform:scale(1.9); opacity:0; }
  }
  @keyframes cloche-secoue {
    0%, 85%, 100% { transform:rotate(0); }
    87% { transform:rotate(-12deg); } 89% { transform:rotate(10deg); }
    91% { transform:rotate(-8deg); } 93% { transform:rotate(6deg); } 95% { transform:rotate(0); }
  }
  .notif-item { display:flex; gap:12px; align-items:flex-start; padding:14px 16px; border-radius:10px; }
  .notif-item.non-lue { background:rgba(227,181,89,0.08); border:1px solid rgba(227,181,89,0.25); }
  .notif-item.lue { background:var(--bg-soft); border:1px solid var(--border); }
  .notif-icone { font-size:20px; line-height:1; }
  .notif-texte { font-size:14px; white-space:pre-wrap; }
  form.inline { display:inline; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .muted { color:var(--muted); font-size:13px; }
  a.btnlink, button.btnlink {
    position:relative; overflow:hidden;
    display:inline-flex; align-items:center; gap:6px; padding:11px 18px; border-radius:9px;
    background:var(--panel-2); border:1px solid var(--border); color:var(--text);
    text-decoration:none; font-size:14px; font-weight:600; cursor:pointer;
    transition: all .15s ease; font-family:inherit;
  }
  /* Reflet doré qui balaie le bouton au survol, comme la lumière sur une
     lame polie — discret, un seul passage, ne se répète pas en boucle. */
  a.btnlink::before, button.btnlink::before {
    content:""; position:absolute; top:0; left:-60%; width:35%; height:100%;
    background:linear-gradient(115deg, transparent, rgba(232,189,85,.35), transparent);
    transform:skewX(-18deg); transition:left .55s ease; pointer-events:none;
  }
  a.btnlink:hover::before, button.btnlink:hover::before { left:130%; }
  a.btnlink:hover, button.btnlink:hover {
    background:#242c37; border-color:rgba(232,189,85,.4); transform:translateY(-1px);
    box-shadow:0 4px 16px -6px rgba(232,189,85,.3);
  }

  .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:14px; margin:16px 0; }
  .stat-card {
    position:relative; overflow:hidden; transform-style:preserve-3d; will-change:transform;
    background:linear-gradient(180deg, var(--panel), var(--panel-2));
    border:1px solid var(--border); border-radius:12px; padding:18px 20px; box-shadow:var(--shadow);
    transition: transform .15s ease-out, border-color .15s ease;
    transform: rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg));
  }
  .stat-card:hover { transform:translateY(-2px) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg)); border-color:rgba(227,181,89,0.45); }
  .stat-card .valeur { font-family:var(--font-title); font-size:28px; font-weight:800; color:var(--gold-2); line-height:1.2; text-shadow:0 0 18px rgba(232,189,85,0.2); }
  .stat-card .label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-top:4px; }

  /* ---- Anneau de progression (taux de réussite) : un cercle SVG dont le
     tracé se remplit de 0 à --valeur (0-100), animé en JS. ---- */
  .anneau-progression { position:relative; width:120px; height:120px; flex-shrink:0; }
  .anneau-progression svg { width:100%; height:100%; transform:rotate(-90deg); }
  .anneau-progression circle { fill:none; stroke-width:10; }
  .anneau-fond { stroke:var(--panel-2); }
  .anneau-avant {
    stroke:var(--gold); stroke-linecap:round;
    stroke-dasharray:326.7; /* 2*pi*52 */
    stroke-dashoffset:calc(326.7 - (326.7 * var(--valeur, 0) / 100));
    filter:drop-shadow(0 0 8px rgba(232,189,85,0.5));
    transition:stroke-dashoffset .05s linear;
  }
  .anneau-texte {
    position:absolute; inset:0; display:flex; align-items:baseline; justify-content:center; gap:2px;
    font-family:var(--font-title); font-weight:800; font-size:15px; color:var(--text);
  }
  .anneau-chiffre { font-size:26px; color:var(--gold-2); }

  /* ---- Barre duo (réussies vs échouées) ---- */
  .barre-duo { display:flex; width:100%; height:14px; border-radius:8px; overflow:hidden; background:var(--panel-2); border:1px solid var(--border); }
  .barre-duo-segment { flex-grow:var(--part, 0); flex-basis:0; min-width:0; transition:flex-grow .8s cubic-bezier(.2,.8,.2,1); }
  .barre-duo-segment.succes { background:linear-gradient(90deg, var(--green), var(--green-2)); }
  .barre-duo-segment.echec { background:linear-gradient(90deg, #c94a4e, var(--red)); }
  .pastille { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; vertical-align:middle; }
  .pastille.succes { background:var(--green-2); }
  .pastille.echec { background:var(--red); }

  /* ---- Barres de comparaison (popularité des missions) ---- */
  .barre-comparaison-ligne + .barre-comparaison-ligne { margin-top:18px; }
  .barre-comparaison-entete { display:flex; justify-content:space-between; align-items:baseline; gap:10px; margin-bottom:7px; font-size:13.5px; flex-wrap:wrap; }
  .barre-comparaison-piste { width:100%; height:11px; border-radius:6px; background:var(--panel-2); border:1px solid var(--border); overflow:hidden; }
  .barre-comparaison-remplissage { height:100%; width:calc(var(--valeur, 0) * 1%); border-radius:6px; }
  .barre-comparaison-remplissage.plus { background:linear-gradient(90deg, var(--gold), var(--gold-2)); }
  .barre-comparaison-remplissage.moins { background:linear-gradient(90deg, var(--azur), #a8dcef); }

  /* ---- Frise interactive des rangs (/demande-rang) : chemin doré avec un
     nœud cliquable par rang, le rang actuel du joueur pulse doucement. ---- */
  .frise-rangs {
    display:flex; align-items:flex-start; gap:0; overflow-x:auto; overflow-y:hidden;
    padding:30px 10px 14px; position:relative; margin-top:10px;
    scrollbar-width:thin;
  }
  .frise-rangs::before {
    content:""; position:absolute; top:46px; left:36px; right:36px; height:2px;
    background:linear-gradient(90deg, rgba(232,189,85,.08), rgba(232,189,85,.5) 15%, rgba(232,189,85,.5) 85%, rgba(232,189,85,.08));
    z-index:0;
  }
  .frise-item {
    position:relative; z-index:1; display:flex; flex-direction:column; align-items:center;
    flex:0 0 88px; text-align:center; text-decoration:none; padding-top:2px;
  }
  .frise-noeud {
    display:flex; align-items:center; justify-content:center; width:52px; height:52px; border-radius:50%;
    background:linear-gradient(160deg, var(--panel), var(--panel-2)); border:2px solid var(--border);
    transition:transform .18s ease, border-color .18s ease, box-shadow .18s ease;
  }
  .frise-icone { font-size:21px; }
  .frise-item:hover .frise-noeud { transform:translateY(-4px) scale(1.08); border-color:rgba(232,189,85,.55); box-shadow:0 10px 22px -10px rgba(232,189,85,.55); }
  .frise-label { margin-top:9px; font-size:10.5px; color:var(--muted); line-height:1.3; max-width:84px; }
  .frise-ici {
    margin-top:3px; font-size:8.5px; letter-spacing:.6px; text-transform:uppercase; color:var(--gold-2);
    font-weight:800; background:rgba(232,189,85,.12); border-radius:5px; padding:1px 6px;
  }
  .frise-item.actuel .frise-noeud {
    border-color:var(--gold); background:linear-gradient(160deg, rgba(232,189,85,.28), rgba(232,189,85,.06));
    animation:frise-pulse 2.6s ease-in-out infinite;
  }
  .frise-item.actuel .frise-label { color:var(--gold-2); font-weight:700; }
  .frise-item.selectionne .frise-noeud { border-color:var(--azur); box-shadow:0 0 0 4px rgba(126,200,227,.16); }
  @keyframes frise-pulse {
    0%, 100% { box-shadow:0 0 0 3px rgba(232,189,85,.14), 0 0 16px rgba(232,189,85,.35); }
    50% { box-shadow:0 0 0 6px rgba(232,189,85,.08), 0 0 24px rgba(232,189,85,.55); }
  }
  @media (prefers-reduced-motion: reduce) { .frise-item.actuel .frise-noeud { animation:none; } }
  .frise-separateur { flex:0 0 1px; align-self:stretch; margin:44px 16px 0; background:linear-gradient(180deg, transparent, rgba(232,189,85,.4), transparent); }

  /* ---- Fiche personnage (/mon-profil) : blason du rang + identité +
     anneau de réussite, en tête de page façon feuille de personnage RPG. ---- */
  .fiche-personnage {
    display:flex; align-items:center; gap:22px; flex-wrap:wrap;
    background:linear-gradient(135deg, rgba(232,189,85,0.07), rgba(255,255,255,0.015));
    border:1px solid var(--border); border-radius:var(--radius); padding:22px 26px; margin:18px 0 22px;
    box-shadow:var(--shadow);
  }
  .fiche-blason {
    width:64px; height:74px; flex-shrink:0; display:flex; align-items:center; justify-content:center;
    font-size:30px; clip-path:polygon(50% 0%, 100% 16%, 100% 60%, 50% 100%, 0% 60%, 0% 16%);
    background:linear-gradient(160deg, rgba(232,189,85,0.26), rgba(232,189,85,0.05) 60%);
    border:1px solid rgba(232,189,85,0.4); filter:drop-shadow(0 6px 14px rgba(232,189,85,0.28));
  }
  .fiche-identite { flex:1; min-width:140px; }
  .fiche-nom { font-family:var(--font-title); font-size:19px; font-weight:800; color:#fff; }
  .fiche-rang { margin-top:4px; font-size:13px; color:var(--gold-2); font-weight:600; letter-spacing:.3px; }
  .anneau-petit { width:78px; height:78px; }
  .anneau-petit .anneau-chiffre { font-size:18px; }
  .anneau-petit .anneau-texte { font-size:11px; }
  @media (max-width:560px) { .fiche-personnage { justify-content:center; text-align:center; } }

  .log-entry {
    display:flex; gap:14px; padding:12px 16px; border-bottom:1px solid var(--border);
    font-size:13.5px; align-items:flex-start;
  }
  .log-entry:last-child { border-bottom:none; }
  .log-entry:hover { background:rgba(255,255,255,0.025); }
  .log-entry .date { color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; min-width:150px; }
  .log-entry .texte { color:#dcdde8; word-break:break-word; }

  .pill { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:20px; font-size:12px; font-weight:700; }
  .pill.on { background:rgba(52,201,143,0.14); color:#8bf0c0; }
  .pill.off { background:rgba(229,72,77,0.16); color:#ffa3a6; }
  .pill.attente { background:rgba(232,189,85,0.16); color:var(--gold-2); }

  /* ---- Missions en cours : chrono & indicateur temps réel ---- */
  .live-indicateur { display:inline-flex; align-items:center; font-size:12.5px; color:var(--muted); font-weight:600; }
  .live-dot {
    display:inline-block; width:8px; height:8px; border-radius:50%;
    background:var(--green); margin-right:7px; flex-shrink:0;
    animation: live-pulse 1.8s ease-in-out infinite;
  }
  @keyframes live-pulse {
    0% { box-shadow:0 0 0 0 rgba(52,201,143,0.55); }
    70% { box-shadow:0 0 0 8px rgba(52,201,143,0); }
    100% { box-shadow:0 0 0 0 rgba(52,201,143,0); }
  }
  .chrono { font-variant-numeric:tabular-nums; font-weight:800; font-size:14px; letter-spacing:.2px; }
  .chrono.ok { color:var(--green); }
  .chrono.warn { color:var(--gold-2); }
  .chrono.retard { color:#ffa3a6; animation:chrono-pulse 1.3s ease-in-out infinite; }
  @keyframes chrono-pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
  .mission-carte { transition: background-color .4s ease; }
  .mission-carte.nouvelle { animation: mission-apparait .6s ease; }
  @keyframes mission-apparait {
    from { opacity:0; transform:translateY(-6px); background-color:rgba(232,189,85,0.1); }
    to { opacity:1; transform:none; }
  }

  /* ---- Grille de carrés qui suit la souris : intensité continue (--i de 0 à 1)
     calculée à chaque frame en JS, au lieu de classes discrètes qui "sautaient"
     d'une case à l'autre. Le halo suit maintenant la souris au pixel près. ---- */
  #grille-curseur {
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    display: grid; opacity: .95;
  }
  .grille-case {
    --i: 0;
    border: 1px solid rgba(227,181,89, calc(0.035 + var(--i) * 0.445));
    background-color: rgba(227,181,89, calc(0.006 + var(--i) * 0.049));
    box-shadow:
      0 0 calc(var(--i) * 16px) rgba(227,181,89, calc(var(--i) * 0.28)),
      inset 0 0 calc(var(--i) * 10px) rgba(227,181,89, calc(var(--i) * 0.035));
    transition: border-color .06s linear, background-color .06s linear, box-shadow .06s linear;
    will-change: border-color, box-shadow, background-color;
  }
  nav, main { position: relative; z-index: 1; }

  /* ---- Lecteur de musique d'ambiance flottant (voir _widget_musique_html
     et SCRIPT_MUSIQUE) : un simple bouton rond en bas à droite, présent sur
     toutes les pages du site, connecté ou non. ---- */
  #musique-bouton {
    position: fixed; right: 18px; bottom: 18px; z-index: 40;
    width: 46px; height: 46px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: linear-gradient(160deg, var(--panel), var(--panel-2));
    border: 1px solid var(--border); box-shadow: 0 10px 26px -10px rgba(0,0,0,.7);
    font-size: 19px; cursor: pointer; user-select: none;
    transition: transform .15s ease, box-shadow .15s ease;
  }
  #musique-bouton:hover { transform: scale(1.08); }
  #musique-bouton.en-lecture { animation: musique-pulse 2.2s ease-in-out infinite; }
  @keyframes musique-pulse {
    0%, 100% { box-shadow: 0 10px 26px -10px rgba(232,189,85,.35); }
    50% { box-shadow: 0 10px 30px -6px rgba(232,189,85,.7); }
  }

</style>
<script>
(function () {
  var TAILLE_CASE = 46;
  var RAYON = 2.2; // rayon de base de l'effet, en nombre de cases
  var conteneur, cases = [], colonnes = 0, lignes = 0;
  var sourisX = -1000, sourisY = -1000;
  var frame = null, depart = null;
  var casesActives = []; // indices modifiés à la frame précédente, pour les réinitialiser proprement

  function construireGrille() {
    var largeur = window.innerWidth, hauteur = window.innerHeight;
    colonnes = Math.ceil(largeur / TAILLE_CASE);
    lignes = Math.ceil(hauteur / TAILLE_CASE);
    conteneur.style.gridTemplateColumns = "repeat(" + colonnes + ", 1fr)";
    conteneur.style.gridTemplateRows = "repeat(" + lignes + ", 1fr)";
    conteneur.innerHTML = "";
    cases = new Array(colonnes * lignes);
    var fragment = document.createDocumentFragment();
    for (var i = 0; i < colonnes * lignes; i++) {
      var c = document.createElement("div");
      c.className = "grille-case";
      cases[i] = c;
      fragment.appendChild(c);
    }
    conteneur.appendChild(fragment);
    casesActives = [];
  }

  // Bruit pseudo-aléatoire déterministe : chaque case garde toujours la même
  // "personnalité" (son propre rythme de scintillement), sans vrai hasard.
  function bruit(n) {
    var x = Math.sin(n * 12.9898) * 43758.5453;
    return x - Math.floor(x);
  }

  function animer(temps) {
    if (depart === null) depart = temps;
    var tempsSec = (temps - depart) / 1000;
    var nouvellesActives = [];

    if (colonnes && lignes && sourisX > -999) {
      var caseLargeur = window.innerWidth / colonnes;
      var caseHauteur = window.innerHeight / lignes;
      var tailleCase = Math.max(caseLargeur, caseHauteur);
      var rayonBase = RAYON * tailleCase + tailleCase * 0.5;

      var col = Math.floor(sourisX / caseLargeur);
      var lig = Math.floor(sourisY / caseHauteur);
      var etendue = Math.ceil(RAYON) + 2;

      // On ne calcule que les cases potentiellement dans le rayon : léger et rapide.
      for (var dy = -etendue; dy <= etendue; dy++) {
        var ll = lig + dy;
        if (ll < 0 || ll >= lignes) continue;
        for (var dx = -etendue; dx <= etendue; dx++) {
          var cc = col + dx;
          if (cc < 0 || cc >= colonnes) continue;
          var idx = ll * colonnes + cc;

          var centreX = (cc + 0.5) * caseLargeur;
          var centreY = (ll + 0.5) * caseHauteur;
          var vx = centreX - sourisX, vy = centreY - sourisY;
          var distance = Math.sqrt(vx * vx + vy * vy);

          // Le rayon se déforme légèrement selon l'angle et le temps : le halo
          // n'est plus un cercle parfait mais une forme organique qui ondule.
          var angle = Math.atan2(vy, vx);
          var ondulation = 1
            + 0.14 * Math.sin(angle * 3 + tempsSec * 0.7)
            + 0.08 * Math.sin(angle * 5 - tempsSec * 1.1);
          var rayonPx = rayonBase * ondulation;
          if (distance >= rayonPx) continue;

          var t = 1 - distance / rayonPx;
          var intensite = t * t * (3 - 2 * t); // smoothstep : chute douce, sans arête visible
          // Léger scintillement propre à chaque case : sensation de "respiration".
          intensite *= 0.88 + 0.12 * Math.sin(tempsSec * 1.6 + bruit(idx) * 6.283);

          cases[idx].style.setProperty("--i", Math.max(0, intensite).toFixed(3));
          nouvellesActives.push(idx);
        }
      }
    }

    // Réinitialise uniquement les cases qui ne sont plus dans le rayon d'effet.
    for (var k = 0; k < casesActives.length; k++) {
      var idxPrec = casesActives[k];
      if (nouvellesActives.indexOf(idxPrec) === -1) {
        cases[idxPrec].style.removeProperty("--i");
      }
    }
    casesActives = nouvellesActives;

    frame = requestAnimationFrame(animer);
  }

  function surMouvement(e) {
    // Position réelle utilisée directement, sans lissage de trajectoire : le
    // halo colle au curseur en temps réel, sans décalage perceptible.
    sourisX = e.clientX;
    sourisY = e.clientY;
  }

  function init() {
    conteneur = document.getElementById("grille-curseur");
    if (!conteneur) return;
    construireGrille();
    document.addEventListener("mousemove", surMouvement, { passive: true });
    var redim;
    window.addEventListener("resize", function () {
      clearTimeout(redim);
      redim = setTimeout(construireGrille, 180);
    });
    if (!frame) frame = requestAnimationFrame(animer);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
</script>
"""


SCRIPT_MAJ = """<script>
(function() {
  var icone = document.getElementById("nav-maj-icon");
  var badge = document.getElementById("nav-maj-badge");
  if (!icone || !badge) return;

  function afficher(count) {
    if (count > 0) {
      badge.textContent = count > 99 ? "99+" : count;
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }
  }

  function actualiser() {
    fetch("/api/maj/non-lues").then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (d) afficher(d.count);
    }).catch(function() {});
  }

  actualiser();
  setInterval(actualiser, 60000);
})();
</script>
"""


def _cloche_nav_html():
    """Icône cloche 🔔 de navigation, réutilisée sur TOUTES les pages
    affichées à un compte connecté (page_html ET le portail d'accueil
    page_accueil_pays) : voir SCRIPT_CLOCHE pour la logique qui l'anime."""
    return (
        '<a href="/notifications" class="nav-icon nav-bell" id="nav-notif-bell" title="Notifications">🔔'
        '<span class="notif-badge" id="nav-notif-badge" style="display:none">0</span></a>'
    )


def _parametres_nav_html():
    """Icône Paramètres ⚙️, réutilisée exactement comme la cloche : sur
    TOUTES les pages une fois connecté, jamais avant login."""
    return '<a href="/parametres" class="nav-icon" id="nav-settings-icon" title="Paramètres">⚙️</a>'


def _maj_nav_html():
    """Icône Mises à jour 🆕, réutilisée exactement comme la cloche : sur
    TOUTES les pages une fois connecté. Le badge (même style que celui des
    notifications) affiche le nombre de mises à jour publiées depuis la
    dernière visite de /mises-a-jour par ce compte — voir SCRIPT_MAJ."""
    return (
        '<a href="/mises-a-jour" class="nav-icon" id="nav-maj-icon" title="Mises à jour du site">🆕'
        '<span class="notif-badge" id="nav-maj-badge" style="display:none">0</span></a>'
    )


def _ia_nav_html():
    """Icône Intelligence Royale (IA) 🧠, réutilisée exactement comme la
    cloche et les paramètres : sur TOUTES les pages une fois connecté.
    /ia est accessible à TOUT compte connecté quel que soit son rôle,
    donc pas de restriction ici non plus (voir route admin_ia)."""
    return '<a href="/ia" class="nav-icon" id="nav-ia-icon" title="Intelligence Royale (IA)">🧠</a>'


def _boutique_nav_html():
    """Icône Boutique 🛒, réutilisée exactement comme la cloche/paramètres/IA :
    sur TOUTES les pages une fois connecté, quel que soit le rôle. Pointe
    toujours vers /boutique — c'est cette route qui redirige ensuite le
    staff vers la gestion des produits de son serveur (voir route
    boutique())."""
    return '<a href="/boutique" class="nav-icon nav-icon-rond" id="nav-boutique-icon" title="Boutique">🛒</a>'


def _roue_nav_html():
    """Icône Roue 🎡, réutilisée exactement comme la boutique : sur TOUTES
    les pages une fois connecté, quel que soit le rôle. Pointe toujours
    vers /roue — la gestion des parts (ajout/suppression/%) reste réservée
    au staff dans Administration → /admin/roue/<guild_id>."""
    return '<a href="/roue" class="nav-icon nav-icon-rond" id="nav-roue-icon" title="Roue">🎡</a>'


def _actions_rapides_nav_html(role=None):
    """Cloche 🔔 + Paramètres ⚙️ + IA 🧠 + Carte 🗺️ + Boutique 🛒 + Roue 🎡 +
    accès discret à l'administration pour le staff. L'administration reste
    protégée côté serveur par @role_required.

    Cas particulier "greyjoy" (compte en attente de recrutement) : seules
    la cloche, les paramètres, l'IA et la carte sont affichées, tout le
    reste (commandes NG, boutique, roue, admin) est masqué — ce rôle n'y a
    de toute façon pas accès côté serveur (voir role_required("malgache")
    sur ces routes)."""
    carte = '<a href="/carte" class="nav-icon nav-icon-rond" title="Carte en direct">🗺️</a>'
    if role == "greyjoy":
        return f'<span class="nav-actions">{_cloche_nav_html()}{_maj_nav_html()}{_parametres_nav_html()}{_ia_nav_html()}{carte}</span>'
    admin = ''
    if niveau_role(role) >= niveau_role("instructeur"):
        admin = '<a href="/admin" class="nav-icon nav-admin" id="nav-admin-icon" title="Administration">🛡️</a>'
    commandes_ng = '<a href="/commandes-ng" class="nav-icon nav-icon-rond" title="Commandes NationsGlory">🎮</a>'
    boutique = _boutique_nav_html()
    roue = _roue_nav_html()
    return f'<span class="nav-actions">{_cloche_nav_html()}{_maj_nav_html()}{_parametres_nav_html()}{_ia_nav_html()}{carte}{commandes_ng}{boutique}{roue}{admin}</span>'


# ================= PROTECTION ANTI-COPIE (DISSUASION UNIQUEMENT) =================
# ⚠️ IMPORTANT : ceci ne bloque PAS réellement la copie de contenu — n'importe
# qui peut désactiver JavaScript, ouvrir les outils développeur via le menu du
# navigateur, ou simplement faire une capture d'écran. C'est un frein pour le
# grand public (clic droit, sélection de texte, raccourcis d'inspection les
# plus connus), pas une protection technique. La vraie protection du contenu
# passe par le droit d'auteur, pas par du JavaScript côté client.
STYLE_PROTECTION = """
<style>
body { -webkit-user-select: none; -moz-user-select: none; -ms-user-select: none; user-select: none; }
input, textarea, code, pre, .copiable, .copiable * {
  -webkit-user-select: text; -moz-user-select: text; -ms-user-select: text; user-select: text;
}
img { -webkit-user-drag: none; user-drag: none; }
.pied-copyright {
  text-align: center; color: var(--muted); font-size: 11px; letter-spacing: .3px;
  padding: 18px 12px 24px; opacity: .7;
}
#menu-clic-droit {
  position: fixed; z-index: 9999; display: none; min-width: 190px;
  background: linear-gradient(180deg, var(--panel, #12171e), var(--panel-2, #1a212a));
  border: 1px solid var(--border, #242c37); border-radius: 12px;
  box-shadow: 0 18px 40px -12px rgba(0,0,0,.55); padding: 6px; overflow: hidden;
}
#menu-clic-droit a, #menu-clic-droit .mcd-item {
  display: flex; align-items: center; gap: 9px; padding: 9px 12px; border-radius: 8px;
  color: var(--text, #e8ecf1); font-size: 13.5px; font-weight: 600; text-decoration: none;
  cursor: pointer; user-select: none; white-space: nowrap;
}
#menu-clic-droit a:hover, #menu-clic-droit .mcd-item:hover { background: rgba(255,255,255,.06); }
#menu-clic-droit .mcd-separateur { height: 1px; margin: 5px 4px; background: var(--border, #242c37); }
</style>
"""


def _script_protection(admin_url=None):
    """Script anti-copie (dissuasion, voir avertissement ci-dessus) + menu
    contextuel personnalisé sur clic droit.

    - Pour le grand public (admin_url=None) : le clic droit reste
      simplement neutralisé, comme avant (aucun menu, juste
      `preventDefault`).
    - Pour le staff (admin_url défini, ex: "/admin") : le clic droit ouvre
      un petit menu personnalisé, positionné sous le curseur, avec un
      raccourci direct vers l'administration — pratique pour y accéder
      depuis n'importe quelle page sans revenir chercher l'icône 🛡️ dans
      la barre de navigation. Le clic droit sur un champ de saisie
      (input/textarea) garde le menu natif du navigateur, pour ne pas
      gêner la frappe (copier/coller...)."""
    menu_html = ""
    ouverture_js = "e.preventDefault();"
    if admin_url:
        menu_html = f"""
<div id="menu-clic-droit">
  <a href="{admin_url}">🛡️ Administration</a>
  <div class="mcd-separateur"></div>
  <div class="mcd-item" id="mcd-fermer">✖️ Fermer</div>
</div>"""
        ouverture_js = """
    var champSaisie = e.target && e.target.closest && e.target.closest('input, textarea, [contenteditable="true"]');
    if (champSaisie) return; // laisse le menu natif du navigateur sur les champs de saisie
    e.preventDefault();
    var menu = document.getElementById('menu-clic-droit');
    if (!menu) return;
    menu.style.display = 'block';
    var largeur = menu.offsetWidth, hauteur = menu.offsetHeight;
    var x = Math.min(e.clientX, window.innerWidth - largeur - 8);
    var y = Math.min(e.clientY, window.innerHeight - hauteur - 8);
    menu.style.left = Math.max(4, x) + 'px';
    menu.style.top = Math.max(4, y) + 'px';
"""
    return f"""{menu_html}
<script>
(function() {{
  document.addEventListener('contextmenu', function(e) {{
    {ouverture_js}
  }});
  document.addEventListener('click', function(e) {{
    var menu = document.getElementById('menu-clic-droit');
    if (menu && (!e.target.closest || e.target.closest('#menu-clic-droit') === null)) menu.style.display = 'none';
  }});
  document.addEventListener('keydown', function(e) {{
    if ((e.key || '').toUpperCase() === 'ESCAPE') {{
      var menu = document.getElementById('menu-clic-droit');
      if (menu) menu.style.display = 'none';
    }}
  }});
  var boutonFermer = document.getElementById('mcd-fermer');
  if (boutonFermer) boutonFermer.addEventListener('click', function() {{
    document.getElementById('menu-clic-droit').style.display = 'none';
  }});
  document.addEventListener('dragstart', function(e) {{
    if (e.target && e.target.tagName === 'IMG') e.preventDefault();
  }});
  document.addEventListener('keydown', function(e) {{
    var touche = (e.key || '').toUpperCase();
    if (touche === 'F12') {{ e.preventDefault(); return; }}
    if (e.ctrlKey && e.shiftKey && (touche === 'I' || touche === 'J' || touche === 'C')) {{ e.preventDefault(); return; }}
    if (e.ctrlKey && touche === 'U') {{ e.preventDefault(); return; }}
    if (e.ctrlKey && touche === 'S') {{ e.preventDefault(); return; }}
  }});
}})();
</script>
"""


def _pied_copyright_html():
    return f'<div class="pied-copyright">© {datetime.now().year} Madagascar Mocha Nation Glory — Tous droits réservés. Contenu protégé, toute reproduction sans autorisation est interdite.</div>'


# ================= MUSIQUE D'AMBIANCE (présente sur tout le site) =================
# Piste d'ambiance libre (SoundHelix, mise à disposition gratuitement pour ce
# genre d'usage), en boucle, avec un simple bouton flottant pour couper/remettre
# le son. Comme le site est un site multi-pages classique (pas une SPA), la
# musique ne peut pas jouer en continu d'une page à l'autre sans interruption :
# on mémorise donc dans le navigateur (localStorage) si l'utilisateur l'a
# activée + le volume + la position de lecture, pour reprendre au même endroit
# et relancer automatiquement la lecture à chaque nouvelle page. Les
# navigateurs bloquent parfois l'autoplay AVEC son tant qu'aucun clic n'a eu
# lieu sur la page en cours : dans ce cas, l'icône reste simplement sur 🔈 et
# un clic suffit pour relancer.
MUSIQUE_URL = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"


def _widget_musique_html():
    return (
        f'<audio id="musique-fond" loop preload="none" src="{MUSIQUE_URL}"></audio>'
        '<div id="musique-bouton" title="Musique d\'ambiance">🔈</div>'
    )


SCRIPT_MUSIQUE = """<script>
(function() {
  var audio = document.getElementById("musique-fond");
  var bouton = document.getElementById("musique-bouton");
  if (!audio || !bouton) return;

  var CLE_ACTIVE = "madagascar_musique_active";
  var CLE_VOLUME = "madagascar_musique_volume";
  var CLE_TEMPS = "madagascar_musique_temps";

  var volume = parseFloat(localStorage.getItem(CLE_VOLUME));
  audio.volume = isNaN(volume) ? 0.35 : volume;

  function majIcone() {
    bouton.textContent = audio.paused ? "🔈" : "🔊";
    bouton.classList.toggle("en-lecture", !audio.paused);
  }

  audio.addEventListener("loadedmetadata", function() {
    var t = parseFloat(localStorage.getItem(CLE_TEMPS));
    if (!isNaN(t) && t > 0 && t < audio.duration) {
      try { audio.currentTime = t; } catch (e) {}
    }
  });

  var dernierEnregistrement = 0;
  audio.addEventListener("timeupdate", function() {
    var maintenant = Date.now();
    if (maintenant - dernierEnregistrement > 4000) {
      dernierEnregistrement = maintenant;
      try { localStorage.setItem(CLE_TEMPS, audio.currentTime); } catch (e) {}
    }
  });

  bouton.addEventListener("click", function() {
    if (audio.paused) {
      audio.play().then(function() {
        try { localStorage.setItem(CLE_ACTIVE, "1"); } catch (e) {}
        majIcone();
      }).catch(function() {});
    } else {
      audio.pause();
      try { localStorage.setItem(CLE_ACTIVE, "0"); } catch (e) {}
      majIcone();
    }
  });

  if (localStorage.getItem(CLE_ACTIVE) === "1") {
    audio.play().then(majIcone).catch(function() { majIcone(); });
  } else {
    majIcone();
  }
})();
</script>
"""


# Script commun (sondage + temps réel SSE) qui fait vivre la cloche 🔔,
# injecté sur TOUTE page affichée à un compte connecté qui contient
# `_cloche_nav_html()`, pas seulement les pages passant par page_html.
SCRIPT_CLOCHE = """<script>
(function() {
  var cloche = document.getElementById("nav-notif-bell");
  var badge = document.getElementById("nav-notif-badge");
  if (!cloche || !badge) return;

  function afficher(count) {
    if (count > 0) {
      badge.textContent = count > 99 ? "99+" : count;
      badge.style.display = "inline-block";
      cloche.classList.add("a-des-notifs");
    } else {
      badge.style.display = "none";
      cloche.classList.remove("a-des-notifs");
    }
  }

  function actualiser() {
    fetch("/api/notifications/non-lues").then(function(r) { return r.ok ? r.json() : null; }).then(function(d) {
      if (d) afficher(d.count);
    }).catch(function() {});
  }

  // Temps réel via Server-Sent Events : la cloche se met à jour dès
  // qu'une notification arrive, sans attendre de sondage. Si la connexion
  // n'est pas disponible (vieux navigateur, proxy qui bloque le flux...),
  // on retombe automatiquement sur un sondage toutes les 15 secondes, donc
  // la cloche reste fonctionnelle dans tous les cas.
  var sondagePeriodique = null;
  function demarrerSondage() {
    if (sondagePeriodique) return;
    actualiser();
    sondagePeriodique = setInterval(actualiser, 15000);
  }

  if (typeof EventSource !== "undefined") {
    var flux = new EventSource("/api/notifications/flux");
    flux.onmessage = function(e) {
      var count = parseInt(e.data, 10);
      if (!isNaN(count)) afficher(count);
    };
    flux.onerror = function() {
      // Le flux temps réel a coupé (veille, réseau, proxy...) : le
      // navigateur retentera de lui-même, mais on active aussi le sondage
      // en secours pendant ce temps pour ne jamais laisser la cloche figée.
      demarrerSondage();
    };
    flux.onopen = function() {
      if (sondagePeriodique) {
        clearInterval(sondagePeriodique);
        sondagePeriodique = null;
      }
    };
  } else {
    demarrerSondage();
  }
})();
</script>"""


# Anime en "compteur qui défile" (0 -> valeur finale) tout chiffre trouvé
# dans une .stat-card .valeur, sur N'IMPORTE QUELLE page qui utilise cette
# classe (statistiques Valerius, tableau de bord admin...). Ne touche pas
# aux valeurs non numériques (ex: "En ligne depuis 3j 2h") : le suffixe
# après le nombre est simplement conservé tel quel.
SCRIPT_COMPTEURS = """<script>
(function() {
  function animerCompteur(el) {
    var texte = el.textContent.trim();
    var m = texte.match(/^(\\d[\\d\\s]*)(.*)$/);
    if (!m) return;
    var cible = parseInt(m[1].replace(/\\s/g, ""), 10);
    if (isNaN(cible)) return;
    var suffixe = m[2] || "";
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var duree = 900, depart = null;
    function etape(horodatage) {
      if (!depart) depart = horodatage;
      var avancement = Math.min((horodatage - depart) / duree, 1);
      var valeur = Math.round(cible * (1 - Math.pow(1 - avancement, 3))); // easing "ease-out"
      el.textContent = valeur + suffixe;
      if (avancement < 1) requestAnimationFrame(etape);
      else el.textContent = cible + suffixe;
    }
    requestAnimationFrame(etape);
  }
  document.querySelectorAll(".stat-card .valeur").forEach(animerCompteur);
})();
</script>"""


# ================= EFFETS 3D GLOBAUX (toutes les pages) =================
# Ces deux scripts sont injectés dans page_html() ET page_accueil_pays(),
# les deux "coquilles" par lesquelles passe absolument chaque page du site
# (y compris toute l'administration) : un seul endroit à maintenir pour
# que le rendu 3D s'applique partout, sans toucher chaque route une à une.

# ---- 1) Inclinaison 3D + lueur au survol, sur toutes les cartes du site
# (mêmes variables CSS --rx/--ry/--mx/--my que .zone-carte sur l'accueil,
# voir SCRIPT_BLASONS plus haut, juste généralisées à .card/.outil-card/
# .stat-card/.serveur-choix). Sur les gros panneaux (tableaux d'admin...),
# on garde uniquement la lueur qui suit le curseur, sans l'inclinaison,
# pour éviter un rendu bizarre sur de grandes surfaces.
SCRIPT_TILT3D = """
<script>
(function(){
  if (window.matchMedia && !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  document.querySelectorAll('.card, .outil-card, .stat-card, .serveur-choix').forEach(function(el){
    var grosPanneau = el.getBoundingClientRect().height > 420;
    el.addEventListener('mousemove', function(e){
      var r = el.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width;
      var y = (e.clientY - r.top) / r.height;
      el.style.setProperty('--mx', (x * 100).toFixed(1) + '%');
      el.style.setProperty('--my', (y * 100).toFixed(1) + '%');
      if (!grosPanneau) {
        el.style.setProperty('--rx', ((x - 0.5) * 8).toFixed(2) + 'deg');
        el.style.setProperty('--ry', (-(y - 0.5) * 8).toFixed(2) + 'deg');
      }
    });
    el.addEventListener('mouseleave', function(){
      el.style.setProperty('--rx', '0deg');
      el.style.setProperty('--ry', '0deg');
    });
  });
})();
</script>"""

# ---- 1bis) Transition "on entre dans la carte" : quand on clique sur une
# carte-lien (zone du portail, outil d'admin, choix de serveur...), au lieu
# de naviguer instantanément, on agrandit la carte cliquée jusqu'à couvrir
# tout l'écran (comme si on y pénétrait) avant de charger la page suivante.
# - Ne s'applique qu'aux <a> (les cartes non-cliquables, ex: verrouillées,
#   ne sont jamais des <a> donc ne sont jamais concernées).
# - Laisse le comportement normal du navigateur si on ouvre dans un nouvel
#   onglet (ctrl/cmd/molette-clic), si le lien est externe, ou si la
#   personne a demandé moins d'animations (accessibilité).
SCRIPT_TRANSITION_CARTES = """
<style>
.transition-entree-overlay{
  position:fixed;inset:0;z-index:9998;pointer-events:none;
  background:#0a0a0c;opacity:0;transition:opacity .55s ease;
}
.transition-entree-overlay.actif{ opacity:1; }
.transition-entree-clone{
  position:fixed;z-index:9999;overflow:hidden;pointer-events:none;
  margin:0;box-sizing:border-box;transform:none !important;perspective:none !important;
  transform-style:flat !important;
  transition:top .55s cubic-bezier(.65,0,.35,1),left .55s cubic-bezier(.65,0,.35,1),
             width .55s cubic-bezier(.65,0,.35,1),height .55s cubic-bezier(.65,0,.35,1),
             border-radius .55s cubic-bezier(.65,0,.35,1);
}
/* Onde de choc dorée qui part du point exact du clic, comme un sceau
   royal qui s'ouvre. Un cercle qui grandit depuis 0 jusqu'à recouvrir
   tout l'écran, puis s'efface. */
.transition-entree-onde{
  position:fixed;z-index:10000;border-radius:50%;pointer-events:none;
  transform:translate(-50%,-50%);
  border:1px solid rgba(232,189,85,.85);
  box-shadow:0 0 40px 6px rgba(232,189,85,.35), inset 0 0 30px rgba(232,189,85,.25);
  width:0;height:0;opacity:1;
  transition:width .6s cubic-bezier(.2,.85,.3,1),height .6s cubic-bezier(.2,.85,.3,1),
             opacity .6s ease .15s;
}
.transition-entree-onde.actif{ opacity:0; }
/* Le blason (icône) et le nom de la zone s'envolent depuis la carte
   cliquée jusqu'au centre de l'écran, en grossissant, comme si on
   traversait le sceau pour entrer dans la zone. */
.transition-entree-embleme{
  position:fixed;z-index:10001;pointer-events:none;
  display:flex;flex-direction:column;align-items:center;gap:16px;
  transform:translate(-50%,-50%);
  transition:top .6s cubic-bezier(.22,.8,.25,1),left .6s cubic-bezier(.22,.8,.25,1);
}
.transition-entree-embleme .embleme-icone{
  display:flex;align-items:center;justify-content:center;
  filter:drop-shadow(0 0 22px rgba(232,189,85,.6));
  transition:font-size .6s cubic-bezier(.22,.8,.25,1);
  animation:embleme-pivote 1.15s linear infinite;
}
@keyframes embleme-pivote{ from{transform:rotateY(0deg);} to{transform:rotateY(360deg);} }
.transition-entree-embleme .embleme-texte{
  font-family:'Cinzel',serif;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;
  color:#e8bd55;font-size:13px;white-space:nowrap;opacity:0;
  transition:opacity .4s ease .3s,transform .4s ease .3s;
  transform:translateY(6px);
}
.transition-entree-embleme.actif .embleme-texte{ opacity:1; transform:translateY(0); }

/* ---- Arrivée : à l'ouverture de la page suivante, on rejoue le sceau à
   l'envers (un voile sombre + une lueur dorée qui se referment vers rien),
   pour donner l'impression de ressortir du portail plutôt que de charger
   une page comme les autres. Ne se joue QUE si on vient de traverser une
   carte (voir sessionStorage plus bas) — jamais sur un chargement normal. */
.transition-arrivee-overlay{
  position:fixed;inset:0;z-index:10002;pointer-events:none;
  background:#0a0a0c;opacity:1;transition:opacity .6s ease .05s;
}
.transition-arrivee-overlay.sortie{ opacity:0; }
.transition-arrivee-lueur{
  position:fixed;top:50%;left:50%;z-index:10003;border-radius:50%;pointer-events:none;
  transform:translate(-50%,-50%) scale(1);
  background:radial-gradient(circle, rgba(232,189,85,.4) 0%, rgba(232,189,85,0) 62%);
  width:150vmax;height:150vmax;opacity:1;
  transition:opacity .7s ease, transform .7s cubic-bezier(.3,.1,.25,1);
}
.transition-arrivee-lueur.sortie{ opacity:0; transform:translate(-50%,-50%) scale(.25); }
</style>
<script>
(function(){
  // ---- Arrivée sur une nouvelle page après avoir traversé une carte ----
  // On vérifie ceci tout de suite, avant même le reste du module, pour que
  // le voile apparaisse dès que possible (moins de flash de la page nue).
  try {
    if (sessionStorage.getItem('portailEntree') === '1') {
      sessionStorage.removeItem('portailEntree');
      var overlayArrivee = document.createElement('div');
      overlayArrivee.className = 'transition-arrivee-overlay';
      var lueurArrivee = document.createElement('div');
      lueurArrivee.className = 'transition-arrivee-lueur';
      document.body.appendChild(overlayArrivee);
      document.body.appendChild(lueurArrivee);
      requestAnimationFrame(function(){
        requestAnimationFrame(function(){
          overlayArrivee.classList.add('sortie');
          lueurArrivee.classList.add('sortie');
        });
      });
      setTimeout(function(){
        overlayArrivee.remove();
        lueurArrivee.remove();
      }, 750);
    }
  } catch (err) { /* sessionStorage indisponible (mode privé strict...) : tant pis, pas d'arrivée animée */ }
})();
(function(){
  if (window.__transitionCartesInit) return;
  window.__transitionCartesInit = true;

  var reduitMouvement = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var enCours = false;
  var SELECTEUR_CARTES = 'a.zone-carte, a.outil-card, a.serveur-choix, a.card, a.stat-card';

  // Quand on revient en arrière (ou en avant), le navigateur peut restaurer
  // la page telle qu'elle était juste avant de la quitter (bfcache) :
  // la carte cliquée était alors "visibility:hidden" et les calques de
  // transition encore présents. Sans ce nettoyage, la carte resterait
  // invisible et la page resterait assombrie après un retour arrière.
  function nettoyerTransition(){
    enCours = false;
    document.querySelectorAll(
      '.transition-entree-overlay, .transition-entree-clone, .transition-entree-onde, .transition-entree-embleme, .transition-arrivee-overlay, .transition-arrivee-lueur'
    ).forEach(function(el){ el.remove(); });
    document.querySelectorAll(SELECTEUR_CARTES).forEach(function(el){
      if (el.style.visibility === 'hidden') el.style.visibility = '';
    });
  }
  nettoyerTransition(); // au cas où le script tournerait sur une page déjà restaurée

  // pageshow se déclenche à chaque affichage de page, y compris via
  // bfcache (event.persisted === true) : c'est le cas typique du bouton
  // précédent/suivant du navigateur.
  window.addEventListener('pageshow', nettoyerTransition);
  // Filet de sécurité supplémentaire : certains navigateurs déclenchent
  // plutôt/aussi popstate lors d'une navigation historique.
  window.addEventListener('popstate', nettoyerTransition);

  document.addEventListener('click', function(e){
    var lien = e.target.closest(SELECTEUR_CARTES);
    if (!lien || !lien.href) return;
    if (lien.classList.contains('verrouillee')) return;
    if (lien.target === '_blank' || lien.hasAttribute('download')) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    var cible;
    try { cible = new URL(lien.href, location.href); } catch (err) { return; }
    if (cible.origin !== location.origin) return;
    if (cible.href === location.href) return; // déjà sur la page, pas de transition
    if (enCours) { e.preventDefault(); return; }

    if (reduitMouvement) return; // navigation normale, instantanée

    e.preventDefault();
    enCours = true;

    var rect = lien.getBoundingClientRect();
    var style = getComputedStyle(lien);

    // 1) Onde de choc dorée qui part exactement du point cliqué.
    var diagonale = Math.hypot(window.innerWidth, window.innerHeight) * 2.05;
    var onde = document.createElement('div');
    onde.className = 'transition-entree-onde';
    onde.style.left = e.clientX + 'px';
    onde.style.top = e.clientY + 'px';
    document.body.appendChild(onde);

    // 2) Rectangle vide qui reprend la couleur/le fond/les coins de la
    // carte et grandit jusqu'à l'écran entier. On ne clone PAS le contenu
    // (texte, icône animée...) : ça faisait étirer le texte et gardait la
    // rotation 3D du survol figée sur le clone agrandi.
    var overlay = document.createElement('div');
    overlay.className = 'transition-entree-overlay';
    document.body.appendChild(overlay);

    var clone = document.createElement('div');
    clone.className = 'transition-entree-clone';
    clone.style.top = rect.top + 'px';
    clone.style.left = rect.left + 'px';
    clone.style.width = rect.width + 'px';
    clone.style.height = rect.height + 'px';
    clone.style.borderRadius = style.borderRadius;
    clone.style.background = style.backgroundImage && style.backgroundImage !== 'none'
      ? style.backgroundImage : style.backgroundColor;
    clone.style.border = style.border;
    clone.style.boxShadow = style.boxShadow;
    document.body.appendChild(clone);

    // 3) Le blason (icône) et le nom de la carte s'envolent vers le
    // centre de l'écran en grossissant, comme un sceau qu'on traverse.
    var embleme = null;
    var iconeEl = lien.querySelector('.zone-icone, .serveur-choix-icone, b');
    var titreEl = lien.querySelector('.zone-nom, strong');
    if (iconeEl || titreEl) {
      var iconeRect = (iconeEl || lien).getBoundingClientRect();
      var tailleDepart = iconeEl ? parseFloat(getComputedStyle(iconeEl).fontSize) || 28 : 28;
      embleme = document.createElement('div');
      embleme.className = 'transition-entree-embleme';
      embleme.style.left = (iconeRect.left + iconeRect.width / 2) + 'px';
      embleme.style.top = (iconeRect.top + iconeRect.height / 2) + 'px';

      if (iconeEl) {
        var spanIcone = document.createElement('div');
        spanIcone.className = 'embleme-icone';
        spanIcone.textContent = iconeEl.textContent.trim();
        spanIcone.style.fontSize = tailleDepart + 'px';
        embleme.appendChild(spanIcone);
      }
      if (titreEl) {
        var spanTexte = document.createElement('div');
        spanTexte.className = 'embleme-texte';
        spanTexte.textContent = 'Entrée dans ' + titreEl.textContent.trim() + '…';
        embleme.appendChild(spanTexte);
      }
      document.body.appendChild(embleme);
    }

    // On masque l'originale immédiatement : comme le clone occupe
    // exactement le même rectangle au départ, la bascule est invisible.
    lien.style.visibility = 'hidden';

    requestAnimationFrame(function(){
      requestAnimationFrame(function(){
        overlay.classList.add('actif');
        onde.classList.add('actif');
        onde.style.width = diagonale + 'px';
        onde.style.height = diagonale + 'px';
        clone.style.top = '0px';
        clone.style.left = '0px';
        clone.style.width = '100vw';
        clone.style.height = '100vh';
        clone.style.borderRadius = '0px';
        if (embleme) {
          embleme.classList.add('actif');
          embleme.style.left = '50%';
          embleme.style.top = '50%';
          var spanIcone = embleme.querySelector('.embleme-icone');
          if (spanIcone) spanIcone.style.fontSize = '76px';
        }
      });
    });

    setTimeout(function(){
      try { sessionStorage.setItem('portailEntree', '1'); } catch (err) { /* tant pis */ }
      location.href = lien.href;
    }, 560);
  }, true);
})();
</script>"""

# ---- 2) Décor 3D ambiant en fond (à l'intérieur de #fond-royaume, qui
# existe déjà sur toutes les pages) : un blason filaire doré qui tourne
# lentement, entouré d'une poussière d'étoiles aux couleurs du royaume.
# Purement décoratif : chargement paresseux, jamais bloquant si Three.js
# ne se charge pas (pas de réseau, adblock...), désactivé sur mobile et
# si la personne a demandé moins d'animations (accessibilité).
SCRIPT_FOND_3D = """
<script>
(function(){
  if (window.__fond3DInit) return;
  window.__fond3DInit = true;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) return;
  var conteneur = document.getElementById('fond-royaume');
  if (!conteneur) return;

  function demarrer(){
    try {
      if (typeof THREE === 'undefined') return;
      var largeur = window.innerWidth, hauteur = window.innerHeight;
      var scene = new THREE.Scene();
      var camera = new THREE.PerspectiveCamera(55, largeur / hauteur, 1, 2000);
      camera.position.z = 420;
      var rendu = new THREE.WebGLRenderer({ alpha: true, antialias: true });
      rendu.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
      rendu.setSize(largeur, hauteur);
      rendu.domElement.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;opacity:.5;';
      conteneur.appendChild(rendu.domElement);

      var geoBlason = new THREE.IcosahedronGeometry(160, 1);
      var matBlason = new THREE.MeshBasicMaterial({ color: 0xe3b559, wireframe: true, transparent: true, opacity: 0.18 });
      var blason = new THREE.Mesh(geoBlason, matBlason);
      scene.add(blason);

      var nbPoints = 220;
      var positions = new Float32Array(nbPoints * 3);
      var couleurs = new Float32Array(nbPoints * 3);
      var palette = [[0.89,0.71,0.35],[0.20,0.79,0.56],[0.49,0.78,0.89]];
      for (var i = 0; i < nbPoints; i++) {
        positions[i*3] = (Math.random() - 0.5) * 1400;
        positions[i*3+1] = (Math.random() - 0.5) * 900;
        positions[i*3+2] = (Math.random() - 0.5) * 800;
        var c = palette[i % palette.length];
        couleurs[i*3] = c[0]; couleurs[i*3+1] = c[1]; couleurs[i*3+2] = c[2];
      }
      var geoPoints = new THREE.BufferGeometry();
      geoPoints.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geoPoints.setAttribute('color', new THREE.BufferAttribute(couleurs, 3));
      var matPoints = new THREE.PointsMaterial({ size: 3.4, vertexColors: true, transparent: true, opacity: 0.55 });
      var etoiles = new THREE.Points(geoPoints, matPoints);
      scene.add(etoiles);

      var sourisX = 0, sourisY = 0;
      window.addEventListener('mousemove', function(e){
        sourisX = (e.clientX / window.innerWidth - 0.5);
        sourisY = (e.clientY / window.innerHeight - 0.5);
      }, { passive: true });
      window.addEventListener('resize', function(){
        largeur = window.innerWidth; hauteur = window.innerHeight;
        camera.aspect = largeur / hauteur; camera.updateProjectionMatrix();
        rendu.setSize(largeur, hauteur);
      });

      function animer(){
        requestAnimationFrame(animer);
        blason.rotation.y += 0.0013;
        blason.rotation.x += 0.0006;
        etoiles.rotation.y += 0.00025;
        camera.position.x += (sourisX * 60 - camera.position.x) * 0.02;
        camera.position.y += (-sourisY * 40 - camera.position.y) * 0.02;
        camera.lookAt(scene.position);
        rendu.render(scene, camera);
      }
      animer();
    } catch (e) { /* décor optionnel : ne doit jamais casser le site */ }
  }

  var script = document.createElement('script');
  script.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';
  script.onload = demarrer;
  script.onerror = function(){};
  document.head.appendChild(script);
})();
</script>"""


# Rempli par configurer_site() avec deps["charger_maintenance"], pour que
# page_html() (fonction module-level, sans accès direct à `deps`) puisse
# savoir si la maintenance est active et afficher le bandeau au Propriétaire.
_ETAT_SITE = {"maintenance_active": lambda: False}


def _page_maintenance_site():
    """Page minimaliste affichée à la place de TOUT le site (sauf connexion
    et mot de passe oublié) quand la maintenance est active, pour n'importe
    quel visiteur qui n'est pas connecté en tant que Propriétaire."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maintenance — Madagascar</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🛠️%3C/text%3E%3C/svg%3E">
{STYLE}
</head>
<body>
<div id="fond-royaume"></div>
<main style="max-width:560px;margin:100px auto;text-align:center;">
  <div class="card">
    <h1 style="margin-top:0;">🛠️ Site en maintenance</h1>
    <p class="muted">Le site est temporairement indisponible le temps d'une intervention de l'administration. Merci de réessayer un peu plus tard.</p>
  </div>
</main>
</body>
</html>"""


def page_html(titre, corps, connecte=None, role=None):
    role_affiche, apercu_label = _etat_apercu(connecte, role)
    effets = _effets_compte(charger_comptes().get(connecte) if connecte else None)
    nav_liens = ""
    admin_url_clic_droit = None
    if connecte:
        niveau = niveau_role(role_affiche)
        if niveau >= niveau_role("instructeur"):
            admin_url_clic_droit = "/admin"
        # Les zones ne sont volontairement PAS accessibles depuis la barre du haut.
        # Pour passer de Valerius à Osiris ou Sirius, retour obligatoire au portail /.
        cloche = _actions_rapides_nav_html(role_affiche)
        badge_icone = "👑 " if role_affiche == "proprietaire" else ""
        role_label = ROLE_LABELS.get(role_affiche, role_affiche) if role_affiche else ""
        nav_liens = (cloche
            + f'<div class="nav-account"><span class="nav-user"><span class="nav-user-name">{connecte}</span><span class="nav-role">{badge_icone}{role_label}</span></span></div>'
            + '<a class="nav-logout" href="/deconnexion">Déconnexion</a>')
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titre} — Madagascar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E⚖️%3C/text%3E%3C/svg%3E">
{STYLE}
{STYLE_PROTECTION}
</head>
<body>
<div id="fond-royaume"></div>
<div id="grille-curseur"></div>
{_widget_musique_html()}
<nav><a href="/" class="retour-pays" title="Retour au menu principal"><img class="drapeau-nav" src="/drapeau.png" alt="Drapeau"><span><strong>MADAGASCAR</strong><small>Nation Glory</small></span></a><span class="brand">Menu principal · sélection de zone</span>{nav_liens}</nav>
{_bandeau_apercu_html(apercu_label)}
{'<div class="bandeau-maintenance">🛠️ MAINTENANCE EN COURS<small>Rien ne sera enregistré côté joueurs/instructeurs — le site restaure automatiquement l\'état d\'avant maintenance à la désactivation.</small></div>' if role == "proprietaire" and _ETAT_SITE["maintenance_active"]() else ""}
<main>
{corps}
</main>
{_pied_copyright_html()}
{SCRIPT_MUSIQUE}
{SCRIPT_CLOCHE if connecte else ''}
{SCRIPT_MAJ if connecte else ''}
{SCRIPT_COMPTEURS if effets['compteurs'] else ''}
{SCRIPT_TILT3D if effets['tilt_3d'] else ''}
{SCRIPT_TRANSITION_CARTES if effets['transition_cartes'] else ''}
{SCRIPT_FOND_3D if effets['fond_3d'] else ''}
{_script_protection(admin_url_clic_droit)}
</body>
</html>"""


# ================= PORTAIL PAYS (page d'accueil "Madagascar") =================
# Le site n'est plus directement "Valerius" : Valerius devient une zone parmi
# d'autres, accessible depuis le portail du pays. D'autres zones pourront
# être ajoutées simplement à la liste ZONES_PAYS ci-dessous.

ZONES_PAYS = [
    {
        "id": "valerius",
        "nom": "Valerius",
        "icone": "⚖️",
        "description": "Système d'attribution de missions et de gestion administrative.",
        "url": "/valerius",
        "disponible": True,
    },
    {
        "id": "osiris",
        "nom": "Osiris",
        "icone": "🏺",
        "description": "Système disciplinaire : blâmes, avertissements et procès royaux.",
        "url": "/osiris",
        "disponible": True,
    },
    {
        "id": "sirius",
        "nom": "Sirius",
        "icone": "🎖️",
        "description": "Système des rangs du royaume : demandes de promotion et suivi des conditions.",
        "url": "/rangs",
        "disponible": True,
    },
]

STYLE_PAYS = """
<style>
  .pays-body {
    min-height:100vh; display:flex; flex-direction:column; align-items:center;
    justify-content:center; padding:60px 22px; position:relative; z-index:1;
    text-align:center;
  }
  .pays-drapeau {
    display:block; width:96px; height:52px; object-fit:contain; border-radius:8px;
    margin:0 auto 26px; box-shadow:0 8px 24px -8px rgba(0,0,0,0.7); border:1px solid rgba(255,255,255,0.12);
    background:#000; padding:4px; image-rendering:pixelated; image-rendering:crisp-edges;
  }
  nav .drapeau-nav {
    display:inline-block; width:24px; height:13px; object-fit:contain; border-radius:3px;
    border:1px solid rgba(255,255,255,0.15); vertical-align:middle; margin-right:6px;
    background:#000; image-rendering:pixelated; image-rendering:crisp-edges;
  }
  .pays-eyebrow {
    font-size:12px; letter-spacing:3px; text-transform:uppercase; color:var(--muted);
    font-weight:700; margin-bottom:10px;
  }
  .pays-titre {
    font-family:var(--font-title); font-weight:800; letter-spacing:2px;
    font-size:clamp(40px, 8vw, 74px); margin:0 0 14px; line-height:1;
    background:linear-gradient(135deg, var(--gold-2), var(--gold) 55%, #ff9da1);
    -webkit-background-clip:text; background-clip:text; color:transparent;
    text-shadow:0 0 60px rgba(232,189,85,0.25);
    animation: pays-apparait .6s ease;
  }
  @keyframes pays-apparait { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:none; } }
  .pays-sous-titre { color:var(--muted); font-size:15px; max-width:480px; margin:0 auto 48px; }

  .zones-grid {
    display:grid; grid-template-columns:repeat(auto-fit, minmax(230px, 1fr));
    gap:22px; width:100%; max-width:900px;
  }

  .zone-carte {
    position:relative; border-radius:18px; padding:2px; text-decoration:none; color:inherit;
    background:linear-gradient(140deg, rgba(232,189,85,0.55), rgba(229,9,20,0.35), rgba(232,189,85,0.15));
    box-shadow:0 20px 45px -18px rgba(0,0,0,0.85);
    transition: box-shadow .25s ease;
    display:block; overflow:hidden;
    perspective:900px;
  }
  .zone-carte:hover { box-shadow:0 28px 60px -16px rgba(232,189,85,0.32); }
  /* ---- Blason interactif : la carte s'incline en 3D vers la souris via
     les variables --rx/--ry (posées en JS, voir SCRIPT_BLASONS plus bas),
     tandis qu'une lueur dorée (--mx/--my) suit le curseur. Sur écran
     tactile ces variables restent à 0 : le JS ne s'attache pas. ---- */
  .zone-carte .zone-interieur {
    position:relative; border-radius:16px; padding:34px 26px 30px;
    background:linear-gradient(180deg, var(--panel), var(--panel-2) 120%);
    height:100%; overflow:hidden;
    transform-style:preserve-3d;
    transform:translateY(0) scale(1) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg));
    transition:transform .15s ease-out;
    will-change:transform;
  }
  .zone-carte:hover .zone-interieur { transform:translateY(-6px) scale(1.015) rotateX(var(--ry,0deg)) rotateY(var(--rx,0deg)); }
  .zone-carte .zone-interieur::after {
    content:""; position:absolute; inset:-40% -40% auto auto; width:180px; height:180px;
    background:radial-gradient(circle, rgba(232,189,85,0.22), transparent 70%);
    transition: opacity .25s ease; opacity:.5; pointer-events:none;
  }
  .zone-carte:hover .zone-interieur::after { opacity:1; }
  .zone-carte .zone-lueur-curseur {
    position:absolute; inset:0; border-radius:16px; pointer-events:none; opacity:0;
    background:radial-gradient(260px circle at var(--mx,50%) var(--my,50%), rgba(232,189,85,0.22), transparent 62%);
    transition:opacity .25s ease; z-index:1;
  }
  .zone-carte:hover .zone-lueur-curseur { opacity:1; }
  .zone-icone {
    position:relative; font-size:32px; width:70px; height:80px; display:flex; align-items:center; justify-content:center;
    margin:0 auto 18px;
    background:linear-gradient(160deg, rgba(232,189,85,0.24), rgba(232,189,85,0.04) 60%);
    border:1px solid rgba(232,189,85,0.4);
    clip-path:polygon(50% 0%, 100% 16%, 100% 60%, 50% 100%, 0% 60%, 0% 16%);
    filter:drop-shadow(0 6px 16px rgba(232,189,85,0.3));
    transform:translateZ(28px);
    overflow:hidden;
    perspective:240px;
  }
  /* Le blason tourne en continu sur lui-même (médaille qui pivote) ; la
     forme héraldique (clip-path) reste fixe sur .zone-icone, seul le
     contenu (l'emoji) pivote à l'intérieur, pour ne pas déformer l'écu. */
  .zone-icone-rotor {
    width:100%; height:100%; display:flex; align-items:center; justify-content:center;
    transform-style:preserve-3d;
    animation:blason-pivote 7s linear infinite;
  }
  .zone-carte:hover .zone-icone-rotor { animation-duration:1.4s; }
  @keyframes blason-pivote {
    from { transform:rotateY(0deg); }
    to { transform:rotateY(360deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .zone-icone-rotor { animation:none; }
  }
  /* Reflet qui balaie l'écu au survol (effet "blason poli") */
  .zone-icone::before {
    content:""; position:absolute; top:-60%; left:-30%; width:40%; height:220%;
    background:linear-gradient(120deg, transparent, rgba(255,255,255,0.55), transparent);
    transform:rotate(20deg) translateX(-160%);
    transition:transform .7s ease; z-index:2;
  }
  .zone-carte:hover .zone-icone::before { transform:rotate(20deg) translateX(260%); }
  .zone-nom {
    position:relative; z-index:2; transform:translateZ(14px);
    font-family:var(--font-title); font-weight:700; font-size:21px; letter-spacing:.5px;
    margin-bottom:8px; color:var(--text);
  }
  .zone-desc { position:relative; z-index:2; transform:translateZ(8px); font-size:13px; color:var(--muted); line-height:1.5; min-height:38px; }
  .zone-cta {
    position:relative; z-index:2; transform:translateZ(8px);
    margin-top:18px; display:inline-flex; align-items:center; gap:6px; font-size:12.5px;
    font-weight:700; letter-spacing:.5px; text-transform:uppercase; color:var(--gold-2);
  }
  .zone-cta svg { transition: transform .2s ease; }
  .zone-carte:hover .zone-cta svg { transform:translateX(4px); }

  .zone-carte.verrouillee {
    background:linear-gradient(140deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02));
    cursor:not-allowed; box-shadow:0 12px 30px -18px rgba(0,0,0,0.8);
  }
  .zone-carte.verrouillee:hover { transform:none; box-shadow:0 12px 30px -18px rgba(0,0,0,0.8); }
  .zone-carte.verrouillee .zone-interieur::after { display:none; }
  .zone-carte.verrouillee .zone-icone {
    background:var(--panel-2); border-color:var(--border); filter:none; opacity:.6;
  }
  .zone-carte.verrouillee .zone-nom, .zone-carte.verrouillee .zone-desc { opacity:.45; }
  .zone-carte.verrouillee .zone-cta { color:var(--muted); }

  .pays-pied { margin-top:56px; font-size:12px; color:var(--muted); letter-spacing:.3px; }
  a.retour-pays {
    color:var(--muted) !important; font-size:13px !important; display:flex; align-items:center; gap:6px;
  }
</style>
"""


SCRIPT_BLASONS = """
<script>
(function(){
  // Pas d'effet 3D sur écran tactile (pas de "survol" fiable) : on laisse
  // simplement les cartes telles quelles, --rx/--ry restent à 0.
  if (window.matchMedia && !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  document.querySelectorAll('.zone-carte:not(.verrouillee)').forEach(function(carte){
    var interieur = carte.querySelector('.zone-interieur');
    if (!interieur) return;
    carte.addEventListener('mousemove', function(e){
      var r = carte.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width;
      var y = (e.clientY - r.top) / r.height;
      var inclinaisonX = (x - 0.5) * 16;   // rotateY : gauche/droite
      var inclinaisonY = -(y - 0.5) * 16;  // rotateX : haut/bas
      interieur.style.setProperty('--rx', inclinaisonX.toFixed(2) + 'deg');
      interieur.style.setProperty('--ry', inclinaisonY.toFixed(2) + 'deg');
      interieur.style.setProperty('--mx', (x * 100).toFixed(1) + '%');
      interieur.style.setProperty('--my', (y * 100).toFixed(1) + '%');
    });
    carte.addEventListener('mouseleave', function(){
      interieur.style.setProperty('--rx', '0deg');
      interieur.style.setProperty('--ry', '0deg');
    });
  });
})();
</script>
"""


def _carte_zone_html(zone):
    if zone["disponible"]:
        return f"""
        <a class="zone-carte" href="{zone['url']}">
          <div class="zone-interieur">
            <div class="zone-lueur-curseur"></div>
            <div class="zone-icone"><div class="zone-icone-rotor">{zone['icone']}</div></div>
            <div class="zone-nom">{zone['nom']}</div>
            <div class="zone-desc">{zone['description']}</div>
            <div class="zone-cta">Accéder à la zone
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
            </div>
          </div>
        </a>"""
    return f"""
        <div class="zone-carte verrouillee">
          <div class="zone-interieur">
            <div class="zone-icone"><div class="zone-icone-rotor">{zone['icone']}</div></div>
            <div class="zone-nom">{zone['nom']}</div>
            <div class="zone-desc">{zone['description']}</div>
            <div class="zone-cta">Bientôt disponible</div>
          </div>
        </div>"""


def page_accueil_pays(connecte=None, role=None):
    """Portail du pays : réservé aux comptes connectés (voir @login_required
    sur la route "/"). Affiche les zones auxquelles le compte peut accéder.
    Une 4e carte "Administration" apparaît uniquement pour les comptes
    instructeur et plus : elle regroupe absolument tous les outils de
    gestion du site (comptes, sauvegardes, sécurité, logs...), qui ne sont
    plus dispersés ailleurs (ni dans Valerius, ni dans Osiris, ni dans la
    nav des autres pages)."""
    # L'administration n'est plus une grosse carte du portail : elle est
    # accessible discrètement depuis l'icône 🛡️ de la barre supérieure pour le staff.
    role_affiche, apercu_label = _etat_apercu(connecte, role)
    effets = _effets_compte(charger_comptes().get(connecte) if connecte else None)
    en_attente_recrutement = role_affiche in ROLES_SANS_ACCES_ZONES
    zones = [] if en_attente_recrutement else list(ZONES_PAYS)
    cartes = "".join(_carte_zone_html(z) for z in zones)
    nav_html = ""
    admin_url_clic_droit = "/admin" if connecte and niveau_role(role_affiche) >= niveau_role("instructeur") else None
    if connecte:
        badge_icone = "👑 " if role_affiche == "proprietaire" else ""
        badge = f'<span class="badge {role_affiche}">{badge_icone}{ROLE_LABELS.get(role_affiche, role_affiche)}</span>' if role_affiche else ""
        nav_html = (
            '<nav><span class="brand">🇲🇬 MADAGASCAR</span>'
            f'{_actions_rapides_nav_html(role_affiche)}<span class="muted">{connecte}</span>{badge}'
            '<a href="/deconnexion">Déconnexion</a></nav>'
        )
    sous_titre = "Choisis une zone pour y accéder. D'autres zones ouvriront prochainement."
    message_recrutement = ""
    if en_attente_recrutement:
        sous_titre = "Ton compte est en attente de recrutement."
        message_recrutement = (
            '<div class="card" style="max-width:520px;margin:0 auto 28px;text-align:left;">'
            '<strong>⏳ En attente de recrutement</strong>'
            '<p class="muted" style="margin-top:8px;">'
            "Tu n'as pas encore accès aux zones du pays (Valerius, Osiris, Sirius...). "
            "En attendant ton recrutement, tu peux utiliser la cloche 🔔, les paramètres ⚙️, "
            "l'IA 🧠 et la carte en direct 🗺️ ci-dessus. Un instructeur mettra ton compte à "
            "jour une fois ton recrutement effectif."
            "</p></div>"
        )
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Madagascar — Portail des zones</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="/drapeau.png">
{STYLE}
{STYLE_PAYS}
{STYLE_PROTECTION}
</head>
<body>
<div id="fond-royaume"></div>
<div id="grille-curseur"></div>
{_widget_musique_html()}
{nav_html}
{_bandeau_apercu_html(apercu_label)}
{'<div class="bandeau-maintenance">🛠️ MAINTENANCE EN COURS<small>Rien ne sera enregistré côté joueurs/instructeurs — le site restaure automatiquement l\'état d\'avant maintenance à la désactivation.</small></div>' if role == "proprietaire" and _ETAT_SITE["maintenance_active"]() else ""}
<div class="pays-body">
  <img class="pays-drapeau" src="/drapeau.png" alt="Drapeau de Madagascar">
  <div class="pays-eyebrow">Portail officiel</div>
  <h1 class="pays-titre">MADAGASCAR</h1>
  <p class="pays-sous-titre">{sous_titre}</p>
  {message_recrutement}
  <div class="zones-grid">
    {cartes}
  </div>
  <div class="pays-pied">🇲🇬 Madagascar — connecté en tant que {connecte or ''}</div>
</div>
{_pied_copyright_html()}
{SCRIPT_MUSIQUE}
{SCRIPT_BLASONS if effets['tilt_3d'] else ''}
{SCRIPT_CLOCHE if connecte else ''}
{SCRIPT_MAJ if connecte else ''}
{SCRIPT_TILT3D if effets['tilt_3d'] else ''}
{SCRIPT_TRANSITION_CARTES if effets['transition_cartes'] else ''}
{SCRIPT_FOND_3D if effets['fond_3d'] else ''}
{_script_protection(admin_url_clic_droit)}
</body>
</html>"""


# ================= ROUTES =================

def configurer_site(app, bot, deps):
    """Enregistre toutes les routes du site sur l'app Flask déjà utilisée
    par le keep_alive du bot. `deps` est un dict de fonctions/objets du
    bot dont le site a besoin (voir bot.py pour la liste exacte)."""
    app.secret_key = _obtenir_secret_key()
    _ETAT_SITE["maintenance_active"] = deps["charger_maintenance"]

    # Endpoints joignables même quand la maintenance est active, pour que
    # le Propriétaire puisse toujours se connecter (et que tout le monde
    # puisse voir la page de maintenance plutôt qu'une erreur brute).
    ENDPOINTS_AUTORISES_MAINTENANCE = {
        "connexion", "deconnexion", "mot_de_passe_oublie",
        "drapeau_image", "image_produit_boutique",
    }

    @app.before_request
    def _verrou_maintenance_site():
        """Bloque TOUT le site (pas seulement les commandes Discord) pour
        tout le monde SAUF le Propriétaire pendant la maintenance : plutôt
        que de simplement refuser, affiche une page de maintenance claire.
        Le Propriétaire, lui, continue de voir le site normalement (avec le
        bandeau d'avertissement ajouté par page_html/page_accueil_pays)."""
        if not deps["charger_maintenance"]():
            return None
        if request.endpoint in ENDPOINTS_AUTORISES_MAINTENANCE:
            return None
        login = session.get("login")
        compte = charger_comptes().get(login) if login else None
        if compte and compte.get("role") == "proprietaire":
            return None
        return _page_maintenance_site(), 503

    def connecte():
        return session.get("login")

    def annoncer_maintenance(actif):
        """Envoie un message dans le salon d'annonce dédié quand le mode
        maintenance est activé ou désactivé depuis le site."""
        channel_id = deps.get("salon_annonce_maintenance_id")
        if not channel_id:
            return
        channel = bot.get_channel(channel_id)
        if not channel:
            return
        texte = (
            "🛠️ **VALERIUS PASSE EN MAINTENANCE**\n"
            "Le bot est temporairement indisponible le temps des réglages, merci de votre patience !"
            if actif else
            "✅ **FIN DE LA MAINTENANCE**\n"
            "Valerius est de nouveau pleinement opérationnel."
        )
        try:
            future = asyncio.run_coroutine_threadsafe(channel.send(texte), bot.loop)
            future.result(timeout=10)
        except Exception:
            pass

    def compte_connecte():
        login = connecte()
        if not login:
            return None
        return charger_comptes().get(login)

    def login_required(f):
        @functools.wraps(f)
        def wrapper(*a, **kw):
            if not connecte():
                return redirect(url_for("connexion"))
            compte = compte_connecte()
            if not compte:
                session.clear()
                return redirect(url_for("connexion"))
            if (compte.get("must_change_password") or _mot_de_passe_expire(compte)) and request.endpoint != "changer_mot_de_passe":
                return redirect(url_for("changer_mot_de_passe"))
            return f(*a, **kw)
        return wrapper

    def role_required(min_role):
        """Exige d'être connecté ET d'avoir au moins ce rôle dans la
        hiérarchie malgache < instructeur < proprietaire."""
        def decorateur(f):
            @functools.wraps(f)
            def wrapper(*a, **kw):
                if not connecte():
                    return redirect(url_for("connexion"))
                compte = compte_connecte()
                if not compte:
                    session.clear()
                    return redirect(url_for("connexion"))
                if (compte.get("must_change_password") or _mot_de_passe_expire(compte)) and request.endpoint != "changer_mot_de_passe":
                    return redirect(url_for("changer_mot_de_passe"))
                if niveau_role(compte.get("role")) < niveau_role(min_role):
                    abort(403)
                return f(*a, **kw)
            return wrapper
        return decorateur

    # ---------- Liaison de compte Discord (OAuth2) ----------

    def _definir_flash_discord(type_, texte):
        """Mémorise un message (ok/erreur) en session, le temps d'un aller-
        retour OAuth2 vers Discord, pour l'afficher une fois de retour sur
        le site (inscription ou paramètres)."""
        session["discord_lien_message"] = {"type": type_, "texte": texte}

    def _lire_flash_discord():
        """Lit puis efface le message éventuel laissé par _definir_flash_discord."""
        return session.pop("discord_lien_message", None)

    def _url_redirection_discord():
        """URL de callback OAuth2, calculée depuis la requête en cours (donc
        valable sur n'importe quel domaine : Render, domaine perso...). Doit
        être renseignée à l'identique dans Discord Developer Portal, onglet
        OAuth2 > Redirects.
        _scheme="https" est forcé explicitement : Render (comme la plupart
        des hébergeurs) reçoit le trafic en HTTPS mais le transmet ensuite
        à l'app en HTTP en interne, donc sans ce forçage Flask générait une
        URL en http:// qui ne correspondait plus à celle enregistrée chez
        Discord (erreur "redirect_uri OAuth2 non valide")."""
        return url_for("discord_callback", _external=True, _scheme="https")

    @app.route("/discord/lier")
    def discord_lier():
        """Démarre la liaison d'un compte Discord (bouton "Lier mon compte
        Discord", sur /inscription comme sur /parametres).
        `retour` indique où revenir une fois la liaison terminée :
        - "inscription" : le compte n'existe pas encore, l'ID Discord obtenu
          est simplement mémorisé en session le temps de finir le formulaire.
        - "parametres" : le compte existe déjà et est connecté, l'ID Discord
          est enregistré directement dessus."""
        retour = request.args.get("retour", "parametres")
        if retour not in ("inscription", "parametres"):
            retour = "parametres"
        if retour == "parametres" and not connecte():
            return redirect(url_for("connexion"))
        if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
            _definir_flash_discord("erreur", "La liaison de compte Discord n'est pas configurée sur ce site pour le moment.")
            return redirect(url_for(retour if retour == "inscription" else "parametres"))
        etat = secrets.token_urlsafe(24)
        session["discord_oauth_state"] = etat
        session["discord_oauth_retour"] = retour
        parametres_url = {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": _url_redirection_discord(),
            "response_type": "code",
            "scope": "identify",
            "state": etat,
            "prompt": "consent",
        }
        return redirect(f"https://discord.com/oauth2/authorize?{urlencode(parametres_url)}")

    @app.route("/discord/callback")
    def discord_callback():
        """Point de retour après autorisation (ou refus) sur Discord."""
        retour = session.pop("discord_oauth_retour", "parametres")
        etat_attendu = session.pop("discord_oauth_state", None)
        cible = "inscription" if retour == "inscription" else "parametres"

        if request.args.get("error"):
            _definir_flash_discord("erreur", "Liaison annulée.")
            return redirect(url_for(cible))

        code = request.args.get("code")
        etat_recu = request.args.get("state")
        if not code or not etat_recu or not etat_attendu or etat_recu != etat_attendu:
            _definir_flash_discord("erreur", "La liaison a échoué (lien expiré ou invalide) : réessaie.")
            return redirect(url_for(cible))

        try:
            reponse_jeton = requests.post(
                f"{DISCORD_API_BASE}/oauth2/token",
                data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _url_redirection_discord(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            reponse_jeton.raise_for_status()
            jeton_acces = reponse_jeton.json()["access_token"]

            reponse_utilisateur = requests.get(
                f"{DISCORD_API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {jeton_acces}"},
                timeout=10,
            )
            reponse_utilisateur.raise_for_status()
            utilisateur_discord = reponse_utilisateur.json()
        except Exception:
            _definir_flash_discord("erreur", "Impossible de contacter Discord pour la liaison. Réessaie plus tard.")
            return redirect(url_for(cible))

        discord_id = str(utilisateur_discord.get("id") or "").strip()
        nom_discord = utilisateur_discord.get("username") or "Compte Discord"
        if not discord_id:
            _definir_flash_discord("erreur", "Discord n'a pas renvoyé d'identifiant valide.")
            return redirect(url_for(cible))

        comptes = charger_comptes()
        deja_pris = next(
            (login2 for login2, c in comptes.items()
             if c.get("discord_id") and str(c.get("discord_id")) == discord_id and login2 != connecte()),
            None,
        )
        if deja_pris:
            _definir_flash_discord("erreur", f"Ce compte Discord est déjà relié à un autre compte du site ({deja_pris}). Contacte un instructeur si c'est une erreur.")
            return redirect(url_for(cible))

        if retour == "inscription":
            session["discord_oauth_id"] = discord_id
            session["discord_oauth_nom"] = nom_discord
            _definir_flash_discord("ok", f"Compte Discord relié : {nom_discord}. Termine la création de ton compte ci-dessous.")
            return redirect(url_for("inscription"))

        # retour == "parametres" : le compte doit déjà exister et être connecté
        if not connecte():
            return redirect(url_for("connexion"))
        compte = comptes.get(connecte())
        if not compte:
            session.clear()
            return redirect(url_for("connexion"))
        compte["discord_id"] = discord_id
        sauvegarder_comptes(comptes)
        _definir_flash_discord("ok", f"Ton compte Discord ({nom_discord}) a bien été relié.")
        return redirect(url_for("parametres"))

    @app.route("/discord/delier", methods=["POST"])
    @login_required
    def discord_delier():
        """Retire la liaison Discord du compte connecté (en libre-service,
        comme la liaison). Un instructeur/propriétaire peut toujours le
        faire pour n'importe quel compte depuis « Comptes »."""
        comptes = charger_comptes()
        compte = comptes.get(connecte())
        if compte:
            compte["discord_id"] = None
            sauvegarder_comptes(comptes)
            _definir_flash_discord("ok", "Ton compte Discord a été délié.")
        return redirect(url_for("parametres"))

    # ---------- Authentification ----------

    @app.route("/drapeau.png")
    def drapeau_image():
        """Sert l'image du drapeau depuis la racine du dépôt (à côté de
        bot.py), sans exposer le reste des fichiers du dépôt. Route
        publique : utilisée aussi sur les pages de connexion/inscription,
        avant que le visiteur ait un compte."""
        chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drapeau.png")
        if not os.path.exists(chemin):
            abort(404)
        return send_file(chemin, mimetype="image/png")

    @app.route("/boutique-images/<nom_fichier>")
    def image_produit_boutique(nom_fichier):
        """Sert une image de produit uploadée. Route publique (comme
        /drapeau.png) : les images de la boutique ne sont pas sensibles et
        doivent s'afficher même sur les pages consultées sans être connecté
        n'existant pas ici, mais ça simplifie aussi le chargement <img> côté
        navigateur (pas de cookie de session à transmettre)."""
        nom_surete = secure_filename(nom_fichier)
        chemin = os.path.join(DOSSIER_IMAGES_BOUTIQUE, nom_surete)
        if not nom_surete or not os.path.exists(chemin):
            abort(404)
        return send_file(chemin)

    @app.route("/")
    @login_required
    def racine():
        """Portail du pays : point d'entrée du site. Réservé aux comptes
        connectés — @login_required renvoie vers /connexion sinon (et vers
        le changement de mot de passe si celui-ci est encore temporaire).
        Présente les zones auxquelles le compte a accès (Valerius, puis
        d'autres à venir)."""
        compte = compte_connecte()
        return page_accueil_pays(connecte(), compte.get("role") if compte else None)

    @app.route("/valerius")
    @role_required("malgache")
    def valerius_entree():
        """Entrée dans la zone Valerius : redirige le compte connecté vers
        la bonne page selon son rôle. Protégée par @login_required comme
        toute zone du portail — jamais accessible sans compte."""
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs"))
        return redirect(url_for("mon_profil"))

    @app.route("/osiris")
    @role_required("malgache")
    def osiris_entree():
        """Entrée dans la zone Osiris (système disciplinaire) : sa propre
        section, séparée de Valerius. Un instructeur/proprietaire arrive sur
        la liste des serveurs côté Osiris (uniquement les blâmes) ; un
        compte de base voit son propre casier."""
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs_osiris"))
        return redirect(url_for("mon_casier"))

    @app.route("/rangs")
    @role_required("malgache")
    def rangs_entree():
        """Entrée dans la zone Sirius (système des rangs) : un instructeur/
        propriétaire arrive sur les demandes à traiter, un compte de base
        sur la page de demande de rang."""
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs_rangs"))
        return redirect(url_for("demande_rang"))

    # ================= PROXY DYNMAP MOCHA (carte en direct intégrée) =================
    # NationsGlory bloque l'affichage de son Dynmap dans une iframe
    # (X-Frame-Options anti-clickjacking), donc on ne peut pas l'embarquer
    # tel quel. Un Dynmap est en réalité juste : 1) un fichier de config
    # JSON (mondes/cartes disponibles) et 2) des tuiles PNG servies à une
    # URL prévisible. Rien de tout ça n'est bloqué en iframe — seule la
    # PAGE HTML complète l'est. On fait donc transiter config + tuiles par
    # notre propre serveur (le navigateur ne voit alors que notre domaine),
    # puis on affiche une vraie carte Leaflet qui pointe dessus.
    #
    # ⚠️ Le format exact des noms de tuiles ci-dessous suit la convention
    # standard du plugin Dynmap, mais n'a pas pu être vérifié en direct
    # contre ce Dynmap précis (pas d'accès réseau à mocha.nationsglory.fr
    # depuis l'environnement qui a écrit ce code). Si les tuiles
    # n'apparaissent pas une fois déployé, ouvrir les outils de
    # développeur du navigateur sur https://mocha.nationsglory.fr/, onglet
    # Réseau, filtrer "tiles" ou ".png", et comparer l'URL réelle d'une
    # tuile avec _nom_tuile_dynmap() ci-dessous pour ajuster.
    DYNMAP_MOCHA_BASE = "https://mocha.nationsglory.fr"
    # mocha.nationsglory.fr rejette (403) les requêtes dont le User-Agent
    # trahit un script (ex: "python-requests/2.x" par défaut) : on se fait
    # donc passer pour un navigateur classique, avec un Referer cohérent.
    _EN_TETES_DYNMAP = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": DYNMAP_MOCHA_BASE + "/",
        "Accept": "*/*",
    }
    # Session réutilisée entre toutes les requêtes de tuiles : garde la
    # connexion HTTPS ouverte avec mocha.nationsglory.fr au lieu de refaire
    # une poignée de main TLS à chaque tuile — gain de vitesse notable vu
    # le nombre de tuiles chargées à chaque déplacement sur la carte.
    _SESSION_DYNMAP = requests.Session()
    _SESSION_DYNMAP.headers.update(_EN_TETES_DYNMAP)

    # Jeton personnel NationsGlory (voir https://nationsglory.readme.io) —
    # même clé que celle utilisée par bot.py pour l'outil IA
    # "joueurs_en_ligne_nationsglory", configurée une seule fois sur Render.
    NATIONSGLORY_API_KEY = os.environ.get("NATIONSGLORY_API_KEY")

    @app.route("/carte/joueurs")
    @login_required
    def carte_mocha_joueurs():
        """Nombre de joueurs en ligne sur Mocha (API officielle
        NationsGlory, pas le Dynmap) — affiché au-dessus de la carte."""
        if not NATIONSGLORY_API_KEY:
            return jsonify({"erreur": "Clé API NationsGlory non configurée."}), 503
        try:
            r = requests.get(
                "https://publicapi.nationsglory.fr/playercount",
                headers={"Authorization": f"Bearer {NATIONSGLORY_API_KEY}"},
                timeout=8,
            )
            r.raise_for_status()
            infos = r.json().get("mocha") or {}
            return jsonify({"joueurs": infos.get("players"), "max": infos.get("maxplayers")})
        except Exception as e:
            return jsonify({"erreur": str(e)}), 502

    @app.route("/carte/config")
    @login_required
    def carte_mocha_config():
        """Relaie le fichier de configuration du Dynmap MODE STANDALONE
        (liste des mondes/cartes, taille des tuiles...) : évite un appel
        direct du navigateur vers mocha.nationsglory.fr qui se heurterait
        au CORS. Chemin confirmé en inspectant le trafic réseau réel du
        Dynmap (mode standalone = fichiers statiques, pas l'API live
        classique /up/configuration)."""
        try:
            r = _SESSION_DYNMAP.get(f"{DYNMAP_MOCHA_BASE}/standalone/dynmap_world.json", timeout=8)
            r.raise_for_status()
            return Response(r.content, mimetype="application/json")
        except Exception as e:
            return jsonify({"erreur": str(e)}), 502

    @app.route("/carte/tuile/<path:chemin>")
    @login_required
    def carte_mocha_tuile(chemin):
        """Relaie une tuile PNG du Dynmap (même raison : contourner le
        CORS/hotlink, et rester sur notre propre domaine pour l'iframe/le
        <img> — les tuiles elles-mêmes ne sont pas concernées par le blocage
        anti-iframe, qui ne vise que la page HTML complète du Dynmap."""
        try:
            r = _SESSION_DYNMAP.get(f"{DYNMAP_MOCHA_BASE}/tiles/{chemin}", timeout=8)
            if r.status_code != 200:
                abort(404)
            reponse = Response(r.content, mimetype="image/png")
            # Les tuiles ne changent pas d'une seconde à l'autre (le terrain
            # évolue lentement) : on laisse le navigateur les garder en
            # cache 10 minutes pour ne pas re-télécharger celles déjà vues
            # en revenant sur une zone de la carte.
            reponse.headers["Cache-Control"] = "public, max-age=600"
            return reponse
        except requests.RequestException:
            abort(502)

    @app.route("/carte")
    @login_required
    def carte_mocha():
        """Carte en direct du serveur NationsGlory Mocha, intégrée
        directement dans la page via Leaflet + proxy (voir ci-dessus),
        avec repli automatique sur un simple lien si les tuiles ne
        chargent pas (Dynmap indisponible, format de tuile différent...).

        Aucune "vraie" API de carte n'existe côté NationsGlory (l'API
        publique publicapi.nationsglory.fr ne renvoie qu'un point de
        coordonnées par pays, pas les tracés de claims) — cette carte
        vient du Dynmap public de NationsGlory, pas de notre bot."""
        url_carte_directe = "https://mocha.nationsglory.fr/?worldname=world&mapname=flat&zoom=4"
        corps = f"""
        <h1>🗺️ Carte en direct — Mocha</h1>
        <p class="muted">Dynmap officiel de NationsGlory, mis à jour en temps réel.</p>
        <div class="card" id="carte-mocha-joueurs" style="display:flex; align-items:center; gap:10px;">
          <span class="muted">Chargement des joueurs en ligne…</span>
        </div>
        <div class="card" id="carte-mocha-conteneur" style="padding:0; overflow:hidden; position:relative;">
          <div id="carte-mocha" style="width:100%; height:78vh; background:#0b0d12;"></div>
        </div>
        <div class="card row" id="carte-mocha-repli" style="justify-content:space-between; display:none;">
          <div><strong>Carte du serveur Mocha</strong><div class="muted">L'intégration directe n'a pas pu charger les tuiles — voici la carte officielle dans un nouvel onglet.</div></div>
          <a class="btnlink" href="{url_carte_directe}" target="_blank" rel="noopener noreferrer">Ouvrir la carte ↗</a>
        </div>
        <script>
        fetch('/carte/joueurs').then(function(r) {{ return r.json(); }}).then(function(d) {{
          var conteneur = document.getElementById('carte-mocha-joueurs');
          if (d.erreur || d.joueurs === undefined) {{
            conteneur.innerHTML = '<span class="muted">Joueurs en ligne indisponibles pour le moment.</span>';
            return;
          }}
          conteneur.innerHTML = '<strong>' + d.joueurs + ' / ' + d.max + '</strong><span class="muted">joueurs en ligne sur Mocha</span>';
        }}).catch(function() {{
          document.getElementById('carte-mocha-joueurs').innerHTML = '<span class="muted">Joueurs en ligne indisponibles pour le moment.</span>';
        }});
        </script>

        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
        <style>
          /* Contrôle de zoom en petits ronds (au lieu des carrés empilés par défaut) */
          #carte-mocha .leaflet-control-zoom {{ border: none; }}
          #carte-mocha .leaflet-control-zoom a {{
            border-radius: 50% !important;
            margin-bottom: 6px;
            width: 30px; height: 30px; line-height: 30px;
          }}
        </style>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
        <script>
        (function() {{
          const CONTENEUR = document.getElementById('carte-mocha-conteneur');
          const REPLI = document.getElementById('carte-mocha-repli');
          function basculerVersRepli() {{
            CONTENEUR.style.display = 'none';
            REPLI.style.display = 'flex';
          }}

          // Monde/carte confirmés en inspectant le trafic réseau réel du
          // Dynmap (https://mocha.nationsglory.fr/tiles/world/flat/...).
          const MONDE = 'world';
          const PREFIXE_CARTE = 'flat';
          // Niveau de dézoom max confirmé observé (préfixes jusqu'à
          // "zzzzzz" = 6 lettres) ; sert de zoom Leaflet "le plus zoomé".
          const DEZOOM_MAX = 7;

          // Convention réelle observée : à chaque niveau de dézoom d,
          // seules les tuiles dont l'index est multiple de 2^d existent,
          // nommées "<lettres z répétées d fois>_<x>_<y>.png" (sans
          // préfixe du tout quand d=0), regroupées par dossiers de 32x32
          // sur cet index (pas de préfixe "z" dans le nom du dossier).
          // Cela correspond exactement au schéma de tuiles XYZ standard
          // (Leaflet) une fois qu'on multiplie l'index Leaflet par 2^d.
          //
          // INVERSER_Y : Leaflet compte ses lignes de tuiles du haut vers
          // le bas, mais rien ne garantit que Dynmap indexe ses tuiles
          // dans le même sens (nord/sud) — c'est la cause la plus
          // fréquente d'une carte qui charge mais dont les morceaux sont
          // mal placés. Si après ce correctif c'est toujours décalé,
          // remettre à false ici (au lieu de true) pour tester l'autre sens.
          const INVERSER_Y = true;

          function nomTuileDynmap(leafletX, leafletY, dezoom) {{
            const echelle = Math.pow(2, dezoom);
            const x = leafletX * echelle;
            const y = (INVERSER_Y ? -leafletY - 1 : leafletY) * echelle;
            const gx = Math.floor(x / 32), gy = Math.floor(y / 32);
            const prefixe = dezoom > 0 ? 'z'.repeat(dezoom) + '_' : '';
            return `${{gx}}_${{gy}}/${{prefixe}}${{x}}_${{y}}.png`;
          }}

          const map = L.map('carte-mocha', {{
            crs: L.CRS.Simple,
            minZoom: 0,
            maxZoom: DEZOOM_MAX,
            zoomControl: false,
            attributionControl: false,
          }});
          L.control.zoom({{ position: 'bottomleft' }}).addTo(map);

          let echecsTuiles = 0;
          let tuilesChargees = 0;

          const couche = L.tileLayer('', {{ tileSize: 128 }});
          couche.getTileUrl = function(coords) {{
            const dezoom = Math.max(0, DEZOOM_MAX - coords.z);
            const nom = nomTuileDynmap(coords.x, coords.y, dezoom);
            return `/carte/tuile/${{MONDE}}/${{PREFIXE_CARTE}}/${{nom}}`;
          }};
          couche.on('tileerror', function() {{
            echecsTuiles++;
            // Les tuiles vides (zones non explorées) donnent normalement
            // des 404 ponctuels — on ne bascule sur le lien de secours
            // que si RIEN ne charge du tout après un nombre significatif
            // d'essais (signe d'un vrai problème, pas juste une zone vide).
            if (echecsTuiles > 20 && tuilesChargees === 0) basculerVersRepli();
          }});
          couche.on('tileload', function() {{ tuilesChargees++; }});
          couche.addTo(map);

          map.setView([0, 0], 2);

          setTimeout(function() {{
            if (tuilesChargees === 0) basculerVersRepli();
          }}, 6000);
        }})();
        </script>
        """
        compte = compte_connecte()
        return page_html("Carte", corps, connecte(), compte.get("role") if compte else None)

    # ================= COMMANDES NATIONSGLORY (page d'aide) =================
    @app.route("/commandes-ng")
    @role_required("malgache")
    def commandes_ng():
        """Page d'aide publique (côté site) qui explique ce que font les
        commandes slash NationsGlory de Valerius, pour que les joueurs
        n'aient pas besoin d'ouvrir Discord pour savoir à quoi elles
        servent. Purement informatif : ne fait aucun appel à l'API
        NationsGlory (contrairement aux commandes elles-mêmes, gérées dans
        bot.py)."""
        corps = """
        <h1>🎮 Commandes NationsGlory</h1>
        <p class="muted">Ces commandes s'utilisent directement dans Discord (tape le nom, Discord te propose les champs à remplir). Elles interrogent les données officielles NationsGlory en direct — ni le site ni Valerius ne les stockent.</p>

        <div class="outil-grid">
          <div class="outil-card" style="cursor:default;">
            <b>🧑‍💼</b>
            <strong>/profil_ng</strong>
            <span>Affiche le profil NationsGlory d'un joueur : statut en ligne, pays, rang, power, temps de jeu, dernière connexion et compétences (mineur, bûcheron, fermier, bâtisseur, chasseur, ingénieur).</span>
          </div>
          <div class="outil-card" style="cursor:default;">
            <b>🏰</b>
            <strong>/pays_ng</strong>
            <span>Affiche les informations d'un pays NationsGlory : chef, date de création, nombre de membres, power, MMR/niveau, alliés, ennemis, et son drapeau.</span>
          </div>
        </div>

        <h2>Détails</h2>
        <div class="card">
          <h3>/profil_ng</h3>
          <p><strong>Paramètres :</strong></p>
          <ul>
            <li><code>pseudo</code> — pseudo exact du joueur NationsGlory (obligatoire)</li>
            <li><code>serveur</code> — serveur NationsGlory à consulter (optionnel, <code>mocha</code> par défaut)</li>
          </ul>
          <p class="muted">Si le joueur n'a pas de données sur le serveur demandé, la commande l'indique au lieu d'afficher des informations incorrectes.</p>
        </div>
        <div class="card">
          <h3>/pays_ng</h3>
          <p><strong>Paramètres :</strong></p>
          <ul>
            <li><code>pays</code> — nom exact du pays NationsGlory (obligatoire)</li>
            <li><code>serveur</code> — serveur NationsGlory à consulter (optionnel, <code>mocha</code> par défaut)</li>
          </ul>
        </div>
        """
        compte = compte_connecte()
        return page_html("Commandes NationsGlory", corps, connecte(), compte.get("role") if compte else None)

    @app.route("/admin")

    @role_required("instructeur")
    def admin_hub():
        """Zone "Administration" : regroupe absolument TOUS les outils de
        gestion du site qui ne sont pas propres à Valerius ou Osiris
        (comptes, sauvegardes, sécurité, logs, message, recherche joueur).
        Rien de ceci n'apparaît plus ailleurs dans la navigation."""
        compte = compte_connecte()
        super_admin = niveau_role(compte.get("role")) >= niveau_role("proprietaire")
        corps = render_template_string("""
        <h1>🛡️ Administration</h1>
        <p class="muted">Tous les outils de gestion du site, indépendants des zones Valerius et Osiris.</p>

        <div class="card row" style="justify-content:space-between;">
          <div><strong>👤 Comptes</strong><div class="muted">Créer, modifier ou supprimer les comptes du site.</div></div>
          <a class="btnlink" href="/admin/comptes">Ouvrir</a>
        </div>
        {% if super_admin %}
        <div class="card row" style="justify-content:space-between;">
          <div><strong>📊 Tableau de bord</strong><div class="muted">Vue d'ensemble globale du bot et du site.</div></div>
          <a class="btnlink" href="/admin/dashboard">Ouvrir</a>
        </div>
        <div class="card row" style="justify-content:space-between;">
          <div><strong>📜 Logs</strong><div class="muted">Historique des actions globales du bot.</div></div>
          <a class="btnlink" href="/admin/logs">Ouvrir</a>
        </div>
        <div class="card row" style="justify-content:space-between;">
          <div><strong>🔒 Sécurité</strong><div class="muted">Verrouillage des serveurs, code d'activation, maintenance.</div></div>
          <a class="btnlink" href="/admin/securite">Ouvrir</a>
        </div>
        <div class="card row" style="justify-content:space-between;">
          <div><strong>✉️ Message</strong><div class="muted">Envoyer un message dans n'importe quel salon Discord.</div></div>
          <a class="btnlink" href="/admin/message">Ouvrir</a>
        </div>
        <div class="card row" style="justify-content:space-between;">
          <div><strong>🔎 Rechercher un joueur</strong><div class="muted">Retrouver un joueur sur l'ensemble des serveurs.</div></div>
          <a class="btnlink" href="/admin/recherche-joueur">Ouvrir</a>
        </div>
        <div class="card row" style="justify-content:space-between;">
          <div><strong>💾 Sauvegardes</strong><div class="muted">Exporter/restaurer toutes les données du bot (tous serveurs).</div></div>
          <a class="btnlink" href="/admin/backup">Ouvrir</a>
        </div>
        {% endif %}
        """, super_admin=super_admin)
        return page_html("Administration", corps, connecte(), compte.get("role"))

    @app.route("/inscription", methods=["GET", "POST"])
    def inscription():
        erreur = None
        if request.method == "POST":
            login = request.form.get("login", "").strip()
            mdp = request.form.get("mot_de_passe", "")
            confirmation = request.form.get("confirmation", "")
            # L'ID Discord vient soit de la liaison OAuth (mémorisée en
            # session, prioritaire et non modifiable par le formulaire),
            # soit du champ manuel si la personne n'a pas utilisé le bouton.
            discord_id = session.get("discord_oauth_id") or request.form.get("discord_id", "").strip() or None
            guild_id = request.form.get("guild_id", "").strip()
            statut = request.form.get("statut", "").strip()
            comptes = charger_comptes()
            guilds_valides = {str(g.id) for g in bot.guilds}
            if not login:
                erreur = "Identifiant requis."
            elif login in comptes:
                erreur = "Cet identifiant est déjà pris."
            elif len(mdp) < LONGUEUR_MIN_MOT_DE_PASSE:
                erreur = f"Le mot de passe doit faire au moins {LONGUEUR_MIN_MOT_DE_PASSE} caractères."
            elif mdp != confirmation:
                erreur = "La confirmation ne correspond pas."
            elif statut not in ("membre", "recrutement"):
                erreur = "Merci d'indiquer si tu fais déjà partie du pays ou si tu vas être recruté."
            elif guild_id not in guilds_valides:
                erreur = "Merci de sélectionner un serveur Discord valide."
            else:
                # "membre" (déjà dans le pays) -> rôle malgache normal.
                # "recrutement" (pas encore recruté) -> rôle greyjoy, accès
                # restreint à la cloche/paramètres/IA/carte en attendant.
                role_choisi = "malgache" if statut == "membre" else "greyjoy"
                comptes[login] = {
                    "role": role_choisi,
                    "discord_id": discord_id,
                    "guild_id": guild_id,
                    "must_change_password": False
                }
                _definir_mot_de_passe(comptes[login], mdp)
                sauvegarder_comptes(comptes)
                session.clear()
                session["login"] = login
                session.permanent = True
                return redirect(url_for("racine"))
        guilds = list(bot.guilds)
        message_discord = _lire_flash_discord()
        # NOTE : session.clear() ci-dessus (en cas de succès) efface déjà
        # discord_oauth_id/nom, donc rien de plus à nettoyer ici.
        discord_oauth_id = session.get("discord_oauth_id")
        discord_oauth_nom = session.get("discord_oauth_nom")
        statut_soumis = request.form.get("statut", "") if request.method == "POST" else ""
        corps = render_template_string("""
        <div class="card" style="max-width:440px;margin:60px auto;">
          <h1>Créer un compte</h1>
          <p class="muted">Choisis ta situation ci-dessous : le rôle de ton compte en dépend. Un instructeur pourra ensuite te faire progresser.</p>
          {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
          {% if message_discord %}<div class="flash {{ message_discord.type }}">{{ message_discord.texte }}</div>{% endif %}
          {% if not guilds %}
          <div class="flash erreur">Le bot n'est connecté à aucun serveur pour l'instant. Réessaie plus tard.</div>
          {% else %}
          <form method="post">
            <p><input name="login" placeholder="Identifiant" required style="width:100%"></p>
            <p><input name="mot_de_passe" type="password" placeholder="Mot de passe (10 caractères min.)" required style="width:100%"></p>
            <p><input name="confirmation" type="password" placeholder="Confirmer le mot de passe" required style="width:100%"></p>

            <p style="margin-top:14px;margin-bottom:6px;font-weight:600;">Ma situation</p>
            <label class="row" style="align-items:center;gap:8px;padding:10px 12px;border:1px solid var(--border);border-radius:10px;margin-bottom:8px;cursor:pointer;">
              <input type="radio" name="statut" value="membre" {{ "checked" if statut_soumis == "membre" else "" }} required>
              <span>🇲🇬 Je fais déjà partie du pays</span>
            </label>
            <label class="row" style="align-items:center;gap:8px;padding:10px 12px;border:1px solid var(--border);border-radius:10px;">
              <input type="radio" name="statut" value="recrutement" {{ "checked" if statut_soumis == "recrutement" else "" }} required>
              <span>🏴 Grey Joy</span>
            </label>
            <p class="muted" style="margin-top:6px;">
              Rôle <strong>Grey Joy</strong> : accès restreint (cloche, paramètres, IA et carte uniquement).
            </p>

            <p style="margin-top:14px;margin-bottom:6px;font-weight:600;">Compte Discord</p>
            {% if discord_oauth_id %}
            <p class="row" style="justify-content:space-between;align-items:center;">
              <span class="pill on">🔗 Relié : {{ discord_oauth_nom }}</span>
              <a class="btnlink" href="/discord/lier?retour=inscription">Changer</a>
            </p>
            <input type="hidden" name="discord_id" value="{{ discord_oauth_id }}">
            {% else %}
            <p><a class="btnlink" href="/discord/lier?retour=inscription" style="display:block;text-align:center;">🔗 Lier mon compte Discord (recommandé)</a></p>
            <p class="muted" style="margin:6px 0 0;">Ou entre ton ID manuellement :</p>
            <p><input name="discord_id" placeholder="Ton ID Discord (optionnel)" style="width:100%"></p>
            {% endif %}

            <p style="margin-top:14px;">
              <select name="guild_id" required style="width:100%">
                <option value="" disabled selected>Choisis ton serveur Discord</option>
                {% for g in guilds %}
                <option value="{{ g.id }}">{{ g.name }}</option>
                {% endfor %}
              </select>
            </p>
            <p class="muted">⚠️ Ce choix est définitif de ton côté : seul un administrateur pourra le modifier ensuite.</p>
            <button type="submit" style="width:100%">Créer mon compte</button>
          </form>
          {% endif %}
          <p class="muted" style="text-align:center;margin-top:14px;"><a href="/connexion">J'ai déjà un compte</a></p>
        </div>
        """, erreur=erreur, guilds=guilds, message_discord=message_discord,
             discord_oauth_id=discord_oauth_id, discord_oauth_nom=discord_oauth_nom, statut_soumis=statut_soumis)
        return page_html("Créer un compte", corps)

    @app.route("/connexion", methods=["GET", "POST"])
    def connexion():
        erreur = None
        if request.method == "POST":
            ip = _obtenir_ip_visiteur()
            login = request.form.get("login", "").strip()
            secondes_restantes = _ip_bloquee(ip)
            secondes_restantes_compte = _compte_bloque(login)
            if secondes_restantes:
                minutes_restantes = max(1, -(-secondes_restantes // 60))
                erreur = (f"Trop de tentatives échouées depuis cette adresse. "
                          f"Réessaie dans environ {minutes_restantes} minute(s).")
            elif secondes_restantes_compte:
                # Blocage indépendant de l'IP : protège même si l'IP présentée
                # est falsifiée (voir _obtenir_ip_visiteur).
                minutes_restantes = max(1, -(-secondes_restantes_compte // 60))
                erreur = (f"Trop de tentatives échouées sur ce compte. "
                          f"Réessaie dans environ {minutes_restantes} minute(s).")
            else:
                mdp = request.form.get("mot_de_passe", "")
                comptes = charger_comptes()
                compte = comptes.get(login)
                if compte and check_password_hash(compte["password_hash"], mdp):
                    if deps["charger_maintenance"]() and compte.get("role") != "proprietaire":
                        erreur = "🛠️ Le site est en maintenance. Seul le Propriétaire peut se connecter pour l'instant."
                    else:
                        _reinitialiser_tentatives(ip)
                        _reinitialiser_tentatives_compte(login)
                        _enregistrer_connexion_reussie(compte, ip)
                        comptes[login] = compte
                        sauvegarder_comptes(comptes)
                        session.clear()
                        session["login"] = login
                        session.permanent = True
                        if compte.get("must_change_password") or _mot_de_passe_expire(compte):
                            return redirect(url_for("changer_mot_de_passe"))
                        return redirect(url_for("racine"))
                else:
                    info = _enregistrer_echec_connexion(ip)
                    info_compte = _enregistrer_echec_connexion_compte(login)
                    deps["sauvegarder_log_disque"](f"⚠️ Tentative de connexion échouée pour « {login} » depuis {ip}.")
                    if info["echecs"] >= MAX_TENTATIVES_CONNEXION:
                        erreur = (f"Trop de tentatives échouées. Cette adresse est bloquée "
                                  f"pendant {int(DUREE_BLOCAGE_CONNEXION.total_seconds() // 60)} minutes.")
                    elif info_compte["echecs"] >= MAX_TENTATIVES_COMPTE:
                        erreur = (f"Trop de tentatives échouées sur ce compte. Il est bloqué "
                                  f"pendant {int(DUREE_BLOCAGE_COMPTE.total_seconds() // 60)} minutes.")
                    else:
                        erreur = "Identifiant ou mot de passe incorrect."
        corps = render_template_string("""
        <div class="card" style="max-width:360px;margin:60px auto;">
          <h1>Connexion</h1>
          {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
          <form method="post">
            <p><input name="login" placeholder="Identifiant" required style="width:100%"></p>
            <p><input name="mot_de_passe" type="password" placeholder="Mot de passe" required style="width:100%"></p>
            <button type="submit" style="width:100%">Se connecter</button>
          </form>
          <p class="muted" style="text-align:center;margin-top:14px;"><a href="/inscription">Créer un compte</a></p>
          <p class="muted" style="text-align:center;margin-top:6px;"><a href="/mot-de-passe-oublie">Mot de passe oublié ?</a></p>
        </div>
        """, erreur=erreur)
        return page_html("Connexion", corps)

    @app.route("/mot-de-passe-oublie", methods=["GET", "POST"])
    def mot_de_passe_oublie():
        """Réinitialisation en libre-service. Comme le site n'a pas de
        système d'e-mail, le nouveau mot de passe temporaire est envoyé
        en message privé Discord au compte relié (discord_id), sur le
        même principe que la création du compte propriétaire.
        Le message affiché est volontairement générique dans tous les
        cas (compte inexistant, non relié à Discord, DM impossible...)
        pour ne jamais révéler si un identifiant existe sur le site."""
        message = None
        erreur = None
        if request.method == "POST":
            login = request.form.get("login", "").strip()
            comptes = charger_comptes()
            compte = comptes.get(login)
            if compte and compte.get("discord_id"):
                try:
                    discord_id = int(compte["discord_id"])
                    mot_de_passe_genere = _generer_mot_de_passe()

                    async def _envoyer_dm():
                        utilisateur = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
                        await utilisateur.send(
                            "🔑 **Réinitialisation de mot de passe — Site Valerius**\n"
                            f"Identifiant : `{login}`\n"
                            f"Nouveau mot de passe temporaire : `{mot_de_passe_genere}`\n"
                            "⚠️ Il te sera demandé de le changer dès ta prochaine connexion.\n"
                            "Si tu n'es pas à l'origine de cette demande, préviens un instructeur."
                        )

                    future = asyncio.run_coroutine_threadsafe(_envoyer_dm(), bot.loop)
                    future.result(timeout=10)

                    # Le mot de passe n'est mis à jour qu'une fois le DM
                    # confirmé envoyé, pour ne jamais bloquer l'accès à
                    # un compte si l'envoi Discord échoue silencieusement.
                    _definir_mot_de_passe(comptes[login], mot_de_passe_genere)
                    comptes[login]["must_change_password"] = True
                    sauvegarder_comptes(comptes)
                    deps["sauvegarder_log_disque"](f"🔑 Mot de passe réinitialisé via « mot de passe oublié » pour « {login} ».")
                except Exception:
                    pass
            message = ("Si un compte existe avec cet identifiant et qu'il est relié à un compte Discord, "
                       "un nouveau mot de passe temporaire vient de lui être envoyé en message privé sur Discord.")
        corps = render_template_string("""
        <div class="card" style="max-width:400px;margin:60px auto;">
          <h1>Mot de passe oublié</h1>
          <p class="muted">Indique ton identifiant : si ton compte est relié à ton Discord, tu recevras un nouveau mot de passe temporaire par message privé.</p>
          {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
          {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
          {% if not message %}
          <form method="post">
            <p><input name="login" placeholder="Identifiant" required style="width:100%"></p>
            <button type="submit" style="width:100%">Envoyer un nouveau mot de passe</button>
          </form>
          {% endif %}
          <p class="muted" style="text-align:center;margin-top:14px;">
            Ton compte n'est relié à aucun Discord, ou tu n'as rien reçu ? Contacte un instructeur pour qu'il réinitialise ton mot de passe manuellement.<br>
            <a href="/connexion">← Retour à la connexion</a>
          </p>
        </div>
        """, message=message, erreur=erreur)
        return page_html("Mot de passe oublié", corps)

    @app.route("/deconnexion")
    def deconnexion():
        session.clear()
        return redirect(url_for("connexion"))

    @app.route("/changer-mot-de-passe", methods=["GET", "POST"])
    def changer_mot_de_passe():
        if not connecte():
            return redirect(url_for("connexion"))
        erreur = None
        ok = None
        if request.method == "POST":
            actuel = request.form.get("mot_de_passe_actuel", "")
            nouveau = request.form.get("nouveau_mot_de_passe", "")
            confirmation = request.form.get("confirmation", "")
            comptes = charger_comptes()
            compte = comptes.get(connecte())
            if not compte or not check_password_hash(compte["password_hash"], actuel):
                erreur = "Mot de passe actuel incorrect."
            elif len(nouveau) < LONGUEUR_MIN_MOT_DE_PASSE:
                erreur = f"Le nouveau mot de passe doit faire au moins {LONGUEUR_MIN_MOT_DE_PASSE} caractères."
            elif nouveau != confirmation:
                erreur = "La confirmation ne correspond pas."
            else:
                _definir_mot_de_passe(compte, nouveau)
                compte["must_change_password"] = False
                sauvegarder_comptes(comptes)
                ok = "Mot de passe modifié avec succès."
        compte = charger_comptes().get(connecte(), {})
        corps = render_template_string("""
        <div class="card" style="max-width:400px;margin:60px auto;">
          <h1>Changer le mot de passe</h1>
          {% if force %}<div class="flash erreur">Tu dois changer ton mot de passe avant de continuer.</div>{% endif %}
          {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
          {% if ok %}<div class="flash ok">{{ ok }} <a href="/">Continuer</a></div>{% endif %}
          {% if not ok %}
          <form method="post">
            <p><input name="mot_de_passe_actuel" type="password" placeholder="Mot de passe actuel" required style="width:100%"></p>
            <p><input name="nouveau_mot_de_passe" type="password" placeholder="Nouveau mot de passe" required style="width:100%"></p>
            <p><input name="confirmation" type="password" placeholder="Confirmer le nouveau mot de passe" required style="width:100%"></p>
            <button type="submit" style="width:100%">Valider</button>
          </form>
          {% endif %}
        </div>
        """, erreur=erreur, ok=ok, force=compte.get("must_change_password", False))
        return page_html("Changer le mot de passe", corps, connecte(), compte.get("role"))

    # ---------- Paramètres (⚙️ accessible partout, comme la cloche) ----------

    @app.route("/parametres")
    @login_required
    def parametres():
        """Espace Paramètres du compte, ouvert via l'icône ⚙️ présente à
        côté de la cloche 🔔 sur toutes les pages une fois connecté. Regroupe
        les informations du compte et les réglages qui le concernent
        directement, quel que soit son rôle (malgache, instructeur ou
        proprietaire)."""
        compte = compte_connecte()
        g = discord.utils.get(bot.guilds, id=int(compte["guild_id"])) if compte.get("guild_id") else None
        message_discord = _lire_flash_discord()
        effets = _effets_compte(compte)
        apercu_role = session.get("apercu_role")
        if apercu_role not in ROLES_ORDRE:
            apercu_role = None
        corps = render_template_string("""
        <h1>⚙️ Paramètres</h1>

        {% if message_discord %}<div class="flash {{ message_discord.type }}">{{ message_discord.texte }}</div>{% endif %}

        <h2>Mon compte</h2>
        <div class="card">
          <div class="row" style="justify-content:space-between;">
            <div>
              <div style="font-size:17px;font-weight:700;">{{ login }}</div>
              <div class="muted" style="margin-top:4px;">
                Rôle : <strong>{{ role_label }}</strong>
                {% if guild_nom %} · Serveur : <strong>{{ guild_nom }}</strong>{% endif %}
                {% if role == "proprietaire" %} · Accès à tous les serveurs{% endif %}
              </div>
            </div>
            {% if role == "proprietaire" %}<span class="badge proprietaire">👑 Propriétaire</span>
            {% else %}<span class="badge {{ role }}">{{ role_label }}</span>{% endif %}
          </div>
          <div class="row" style="justify-content:space-between;margin-top:16px;align-items:center;">
            <span class="pill {{ 'on' if discord_id else 'off' }}">{{ "🔗 ID Discord relié" if discord_id else "⛔ Aucun ID Discord relié" }}</span>
            {% if discord_id %}
            <form class="inline" method="post" action="/discord/delier" onsubmit="return confirm('Délier ce compte Discord ?');">
              <button type="submit" class="btnlink">Délier</button>
            </form>
            {% else %}
            <a class="btnlink" href="/discord/lier?retour=parametres">🔗 Lier mon compte Discord</a>
            {% endif %}
          </div>
          {% if not discord_id %}
          <p class="muted" style="margin-top:10px;">
            Sans ID Discord relié, certaines fonctionnalités (demandes de rang, réception de MP de confirmation...)
            restent indisponibles. Clique sur « Lier mon compte Discord » ci-dessus, ou demande à un Instructeur de le faire depuis « Comptes ».
          </p>
          {% endif %}
        </div>

        <h2>Sécurité</h2>
        <div class="card row" style="justify-content:space-between;">
          <div>
            <div style="font-weight:600;">Mot de passe</div>
            <div class="muted" style="margin-top:2px;">Change-le régulièrement pour garder ton compte sécurisé.</div>
          </div>
          <a class="btnlink" href="/changer-mot-de-passe">Changer le mot de passe</a>
        </div>

        <h2>Notifications</h2>
        <div class="card row" style="justify-content:space-between;">
          <div>
            <div style="font-weight:600;">Cloche 🔔</div>
            <div class="muted" style="margin-top:2px;">Retrouve toutes tes notifications (missions, rangs...) au même endroit.</div>
          </div>
          <a class="btnlink" href="/notifications">Voir mes notifications</a>
        </div>

        <h2>Effets visuels</h2>
        <div class="card">
          <p class="muted" style="margin-top:-4px;margin-bottom:14px;">
            Désactive un effet si le site rame sur ton appareil, ou juste par préférence. Ça n'affecte que toi.
          </p>
          <form method="post" action="/parametres/effets">
            <div class="row" style="justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);align-items:center;">
              <div>
                <div style="font-weight:600;">🌌 Fond animé 3D</div>
                <div class="muted" style="margin-top:2px;">Décor en mouvement (étoiles, blason) derrière les pages — l'effet le plus lourd.</div>
              </div>
              <input type="checkbox" name="fond_3d" {{ "checked" if effets.fond_3d else "" }}>
            </div>
            <div class="row" style="justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);align-items:center;">
              <div>
                <div style="font-weight:600;">🎴 Inclinaison 3D des cartes</div>
                <div class="muted" style="margin-top:2px;">Les cartes s'inclinent et brillent au survol de la souris.</div>
              </div>
              <input type="checkbox" name="tilt_3d" {{ "checked" if effets.tilt_3d else "" }}>
            </div>
            <div class="row" style="justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);align-items:center;">
              <div>
                <div style="font-weight:600;">🚪 Transition en cliquant sur une carte</div>
                <div class="muted" style="margin-top:2px;">Animation d'entrée quand tu cliques sur une carte pour changer de zone.</div>
              </div>
              <input type="checkbox" name="transition_cartes" {{ "checked" if effets.transition_cartes else "" }}>
            </div>
            <div class="row" style="justify-content:space-between;padding:10px 0;align-items:center;">
              <div>
                <div style="font-weight:600;">🔢 Compteurs animés</div>
                <div class="muted" style="margin-top:2px;">Les chiffres des statistiques montent progressivement à l'affichage.</div>
              </div>
              <input type="checkbox" name="compteurs" {{ "checked" if effets.compteurs else "" }}>
            </div>
            <div style="margin-top:16px;"><button type="submit" class="btnlink">💾 Enregistrer</button></div>
          </form>
        </div>

        {% if role in ("instructeur", "proprietaire") %}
        <h2>Administration</h2>
        <div class="card row" style="justify-content:space-between;">
          <div>
            <div style="font-weight:600;">Comptes &amp; serveurs</div>
            <div class="muted" style="margin-top:2px;">Gérer les comptes du site, les serveurs, les catalogues...</div>
          </div>
          <a class="btnlink" href="/admin">Ouvrir l'administration</a>
        </div>

        <h2>Aperçu d'un autre grade</h2>
        <div class="card">
          <p class="muted" style="margin-top:-4px;margin-bottom:14px;">
            Prévisualise le portail et les icônes de navigation tels qu'un autre grade les voit
            (utile pour vérifier ce qu'un Malgache ou un Grey Joy peut voir). Ça ne change en
            rien tes droits réels : tu gardes ton accès complet à l'administration pendant l'aperçu.
          </p>
          {% if apercu_role %}
          <p class="row" style="justify-content:space-between;align-items:center;">
            <span class="pill on">🔍 Aperçu actif : {{ role_labels[apercu_role] }}</span>
            <form method="post" action="/parametres/apercu/arreter"><button type="submit" class="btnlink">Arrêter l'aperçu</button></form>
          </p>
          {% else %}
          <form method="post" action="/parametres/apercu" class="row" style="gap:10px;align-items:center;">
            <select name="role_apercu">
              {% for r in roles_apercu %}<option value="{{ r }}">{{ role_labels[r] }}</option>{% endfor %}
            </select>
            <button type="submit" class="btnlink">Prévisualiser</button>
          </form>
          {% endif %}
        </div>
        {% endif %}
        """, login=connecte(), role=compte.get("role"), role_label=ROLE_LABELS.get(compte.get("role"), compte.get("role")),
             discord_id=compte.get("discord_id"), guild_nom=(g.name if g else None), message_discord=message_discord,
             effets=effets, apercu_role=apercu_role, roles_apercu=ROLES_ORDRE, role_labels=ROLE_LABELS)
        return page_html("Paramètres", corps, connecte(), compte.get("role"))

    @app.route("/parametres/effets", methods=["POST"])
    @login_required
    def parametres_effets():
        """Enregistre les préférences d'effets visuels (une case à cocher
        par effet) pour le compte connecté uniquement — jamais d'impact
        sur les autres comptes."""
        login = connecte()
        comptes = charger_comptes()
        compte = comptes.get(login)
        if compte is None:
            return redirect(url_for("parametres"))
        compte["effets"] = {cle: (request.form.get(cle) == "on") for cle in EFFETS_PAR_DEFAUT}
        sauvegarder_comptes(comptes)
        return redirect(url_for("parametres"))

    @app.route("/parametres/apercu", methods=["POST"])
    @role_required("instructeur")
    def parametres_apercu_definir():
        """Active, pour le compte staff connecté uniquement (stocké en
        session, jamais persisté), un aperçu visuel d'un autre grade sur le
        portail et les icônes de navigation. N'affecte jamais les droits
        réels (voir _etat_apercu / role_required)."""
        role_choisi = request.form.get("role_apercu", "")
        if role_choisi in ROLES_ORDRE:
            session["apercu_role"] = role_choisi
        return redirect(url_for("parametres"))

    @app.route("/parametres/apercu/arreter", methods=["GET", "POST"])
    @role_required("instructeur")
    def parametres_apercu_arreter():
        """Coupe l'aperçu en cours. Accessible en GET aussi, pour le lien
        'Quitter l'aperçu' du bandeau affiché sur toutes les pages."""
        session.pop("apercu_role", None)
        retour = request.referrer
        if retour and retour.startswith(request.host_url):
            return redirect(retour)
        return redirect(url_for("parametres"))

    # ---------- Serveurs (instructeur et plus) ----------

    @app.route("/admin/serveurs")
    @role_required("instructeur")
    def admin_serveurs():
        compte = compte_connecte()
        if compte.get("role") == "proprietaire":
            guilds = list(bot.guilds)
        else:
            guilds = [g for g in bot.guilds if str(g.id) == str(compte.get("guild_id"))]
        corps = render_template_string("""
        <h1>⚖️ Valerius — Serveurs</h1>
        <p class="muted">Sélectionne un serveur pour gérer ses missions, ses profils ou ses missions en cours.</p>
        {% if not guilds %}<div class="card">Aucun serveur accessible pour ton compte. {% if not super_admin %}Ton compte n'est assigné à aucun serveur, ou celui-ci n'est plus accessible au bot.{% endif %}</div>{% endif %}
        <div class="serveurs-grid">
        {% for g in guilds %}<a class="serveur-choix" href="/admin/serveur/{{ g.id }}"><div class="serveur-choix-icone">⚖️</div><div class="serveur-choix-contenu"><strong>{{ g.name }}</strong><span>{{ g.member_count }} membres</span></div><div class="serveur-choix-fleche">→</div></a>{% endfor %}
        </div>
        """, guilds=guilds, super_admin=(compte.get("role") == "proprietaire"),
             peut_editer_catalogue=(niveau_role(compte.get("role")) >= niveau_role("instructeur")))
        return page_html("Valerius — Serveurs", corps, connecte(), compte.get("role"))

    # ---------- Serveurs — zone Osiris (instructeur et plus) ----------
    # Section totalement séparée de Valerius : uniquement le système
    # disciplinaire (blâmes/avertissements/procès), aucun lien vers les
    # missions, profils ou statistiques de Valerius.

    @app.route("/admin/serveurs-osiris")
    @role_required("instructeur")
    def admin_serveurs_osiris():
        compte = compte_connecte()
        if compte.get("role") == "proprietaire":
            guilds = list(bot.guilds)
        else:
            guilds = [g for g in bot.guilds if str(g.id) == str(compte.get("guild_id"))]
        corps = render_template_string("""
        <h1>🏺 Osiris — Serveurs</h1>
        <p class="muted">Sélectionne un serveur pour gérer ses blâmes, avertissements et procès.</p>
        {% if not guilds %}<div class="card">Aucun serveur accessible pour ton compte.</div>{% endif %}
        <div class="serveurs-grid">
        {% for g in guilds %}<a class="serveur-choix" href="/admin/serveur-osiris/{{ g.id }}"><div class="serveur-choix-icone">🏺</div><div class="serveur-choix-contenu"><strong>{{ g.name }}</strong><span>{{ g.member_count }} membres</span></div><div class="serveur-choix-fleche">→</div></a>{% endfor %}
        </div>
        """, guilds=guilds, super_admin=(compte.get("role") == "proprietaire"))
        return page_html("Osiris — Serveurs", corps, connecte(), compte.get("role"))

    # ---------- Serveurs — zone Sirius / Rangs (instructeur et plus) ----------

    @app.route("/admin/serveurs-rangs")
    @role_required("instructeur")
    def admin_serveurs_rangs():
        compte = compte_connecte()
        if compte.get("role") == "proprietaire":
            guilds = list(bot.guilds)
        else:
            guilds = [g for g in bot.guilds if str(g.id) == str(compte.get("guild_id"))]
        peut_editer_catalogue = compte.get("role") == "proprietaire"
        corps = render_template_string("""
        <h1>🎖️ Sirius — Serveurs</h1>
        <p class="muted">Sélectionne un serveur pour traiter les demandes de rang{{ " ou modifier le catalogue des rangs" if peut_editer_catalogue else "" }}.</p>
        {% if not guilds %}<div class="card">Aucun serveur accessible pour ton compte.</div>{% endif %}
        <div class="serveurs-grid">
        {% for g in guilds %}<a class="serveur-choix" href="/admin/serveur-rangs/{{ g.id }}"><div class="serveur-choix-icone">🎖️</div><div class="serveur-choix-contenu"><strong>{{ g.name }}</strong><span>{{ g.member_count }} membres</span></div><div class="serveur-choix-fleche">→</div></a>{% endfor %}
        </div>
        """, guilds=guilds, peut_editer_catalogue=peut_editer_catalogue)
        return page_html("Sirius — Serveurs", corps, connecte(), compte.get("role"))

    # ---------- Tableau de bord du serveur sélectionné ----------
    @app.route("/admin/serveur/<int:guild_id>")
    @role_required("instructeur")
    def admin_serveur(guild_id):
        compte=compte_connecte()
        if not guild_autorise(compte,guild_id): abort(403)
        g=discord.utils.get(bot.guilds,id=guild_id)
        corps=render_template_string("""<div class="serveur-header"><div><div class="muted">SERVEUR SÉLECTIONNÉ · VALERIUS</div><h1>⚖️ {{ g.name }}</h1><p class="muted">Tu es maintenant dans ce serveur. Les autres serveurs ne sont plus affichés.</p></div><a class="btnlink" href="/admin/serveurs">← Changer de serveur</a></div><div class="outil-grid">{% if proprietaire %}<a class="outil-card" href="/admin/missions/{{ g.id }}"><b>📚</b><strong>Catalogue</strong><span>Gérer les missions</span></a>{% endif %}<a class="outil-card" href="/admin/attribuer-mission/{{ g.id }}"><b>🎯</b><strong>Attribuer une mission</strong><span>Choisir précisément pour qui</span></a><a class="outil-card" href="/admin/missions-actives/{{ g.id }}"><b>⏱️</b><strong>Missions en cours</strong><span>Suivre les missions</span></a><a class="outil-card" href="/admin/profils/{{ g.id }}"><b>👥</b><strong>Profils</strong><span>Gérer les profils</span></a><a class="outil-card" href="/admin/statistiques/{{ g.id }}"><b>📊</b><strong>Statistiques</strong><span>Voir les performances</span></a><a class="outil-card" href="/admin/boutique/{{ g.id }}"><b>🛒</b><strong>Boutique</strong><span>Gérer les produits</span></a><a class="outil-card" href="/admin/roue/{{ g.id }}"><b>🎡</b><strong>Roue</strong><span>Gérer les parts de la roue</span></a></div>""",g=g,proprietaire=compte.get("role")=="proprietaire")
        return page_html(f"Valerius — {g.name}",corps,connecte(),compte.get("role"))

    @app.route("/admin/serveur-osiris/<int:guild_id>")
    @role_required("instructeur")
    def admin_serveur_osiris(guild_id):
        compte=compte_connecte()
        if not guild_autorise(compte,guild_id): abort(403)
        g=discord.utils.get(bot.guilds,id=guild_id)
        corps=render_template_string("""<div class="serveur-header"><div><div class="muted">SERVEUR SÉLECTIONNÉ · OSIRIS</div><h1>🏺 {{ g.name }}</h1><p class="muted">Tu es maintenant dans ce serveur.</p></div><a class="btnlink" href="/admin/serveurs-osiris">← Changer de serveur</a></div><div class="outil-grid"><a class="outil-card" href="/admin/blames/{{ g.id }}"><b>⚖️</b><strong>Blâmes</strong><span>Avertissements et discipline</span></a></div>""",g=g)
        return page_html(f"Osiris — {g.name}",corps,connecte(),compte.get("role"))

    @app.route("/admin/serveur-rangs/<int:guild_id>")
    @role_required("instructeur")
    def admin_serveur_rangs(guild_id):
        compte=compte_connecte()
        if not guild_autorise(compte,guild_id): abort(403)
        g=discord.utils.get(bot.guilds,id=guild_id)
        corps=render_template_string("""<div class="serveur-header"><div><div class="muted">SERVEUR SÉLECTIONNÉ · SIRIUS</div><h1>🎖️ {{ g.name }}</h1><p class="muted">Tu es maintenant dans ce serveur.</p></div><a class="btnlink" href="/admin/serveurs-rangs">← Changer de serveur</a></div><div class="outil-grid"><a class="outil-card" href="/admin/demandes-rang/{{ g.id }}"><b>📥</b><strong>Demandes</strong><span>Traiter les demandes de rang</span></a>{% if proprietaire %}<a class="outil-card" href="/admin/rangs/{{ g.id }}"><b>📜</b><strong>Catalogue des rangs</strong><span>Modifier les rangs disponibles</span></a>{% endif %}</div>""",g=g,proprietaire=compte.get("role")=="proprietaire")
        return page_html(f"Sirius — {g.name}",corps,connecte(),compte.get("role"))

    # ---------- Statistiques des missions (instructeur et plus, scope serveur) ----------

    @app.route("/admin/statistiques/<int:guild_id>")
    @role_required("instructeur")
    def admin_statistiques(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        g = discord.utils.get(bot.guilds, id=guild_id)
        stats = calculer_stats_missions(guild_id, deps)

        corps = render_template_string("""
        <h1>Statistiques des missions</h1>
        <p class="muted">Serveur {{ g.name if g else guild_id }}</p>

        {% if not stats.total_missions %}
        <div class="card">Aucune mission terminée pour l'instant sur ce serveur — les statistiques apparaîtront dès la première mission réussie ou échouée.</div>
        {% else %}
        <div class="card" style="display:flex;align-items:center;gap:34px;flex-wrap:wrap;">
          <div class="anneau-progression" data-cible="{{ stats.taux_reussite }}" style="--valeur:0;">
            <svg viewBox="0 0 120 120">
              <circle class="anneau-fond" cx="60" cy="60" r="52"></circle>
              <circle class="anneau-avant" cx="60" cy="60" r="52"></circle>
            </svg>
            <div class="anneau-texte"><span class="anneau-chiffre">0</span>%</div>
          </div>
          <div style="flex:1;min-width:220px;">
            <div class="muted" style="text-transform:uppercase;font-size:12px;letter-spacing:.5px;margin-bottom:6px;">Taux de réussite global</div>
            <div class="barre-duo">
              <div class="barre-duo-segment succes" style="--part:{{ stats.total_reussies }};" title="{{ stats.total_reussies }} réussies"></div>
              <div class="barre-duo-segment echec" style="--part:{{ stats.total_echouees }};" title="{{ stats.total_echouees }} échouées"></div>
            </div>
            <div class="row" style="gap:18px;margin-top:10px;font-size:12.5px;">
              <span class="muted"><span class="pastille succes"></span> {{ stats.total_reussies }} réussies</span>
              <span class="muted"><span class="pastille echec"></span> {{ stats.total_echouees }} échouées</span>
            </div>
          </div>
        </div>

        <div class="stats-grid">
          <div class="stat-card"><div class="valeur">{{ stats.total_reussies }}</div><div class="label">Missions réussies</div></div>
          <div class="stat-card"><div class="valeur">{{ stats.total_echouees }}</div><div class="label">Missions échouées</div></div>
          <div class="stat-card"><div class="valeur">{{ stats.temps_moyen_texte }}</div><div class="label">Temps moyen de complétion</div></div>
        </div>
        {% if not stats.nb_completions_chronometrees %}
        <p class="muted">Le temps moyen de complétion n'a pas encore de donnée exploitable (missions terminées avant la mise en place du chrono).</p>
        {% endif %}

        <h2>Popularité des missions</h2>
        <div class="card">
          <div class="barre-comparaison">
            <div class="barre-comparaison-ligne">
              <div class="barre-comparaison-entete">
                <strong>{{ stats.mission_plus_populaire[0] }}</strong>
                <span class="muted">{{ stats.mission_plus_populaire[1].categorie|capitalize }} — {{ stats.mission_plus_populaire[1].count }}×</span>
              </div>
              <div class="barre-comparaison-piste"><div class="barre-comparaison-remplissage plus" data-cible="100" style="--valeur:0;"></div></div>
            </div>
            <div class="barre-comparaison-ligne">
              <div class="barre-comparaison-entete">
                <strong>{{ stats.mission_moins_populaire[0] }}</strong>
                <span class="muted">{{ stats.mission_moins_populaire[1].categorie|capitalize }} — {{ stats.mission_moins_populaire[1].count }}×</span>
              </div>
              {% set ratio = (stats.mission_moins_populaire[1].count / stats.mission_plus_populaire[1].count * 100) if stats.mission_plus_populaire[1].count else 0 %}
              <div class="barre-comparaison-piste"><div class="barre-comparaison-remplissage moins" data-cible="{{ ratio }}" style="--valeur:0;"></div></div>
            </div>
          </div>
        </div>
        <p class="muted">Calculé sur {{ stats.nb_missions_distinctes }} mission(s) distincte(s) déjà attribuée(s) au moins une fois.</p>
        {% endif %}

        <script>
        (function() {
          var reduit = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          function animerValeur(el, cible, appliquer, duree) {
            if (reduit) { appliquer(cible); return; }
            var depart = null;
            function etape(h) {
              if (!depart) depart = h;
              var t = Math.min((h - depart) / duree, 1);
              appliquer(cible * (1 - Math.pow(1 - t, 3)));
              if (t < 1) requestAnimationFrame(etape);
              else appliquer(cible);
            }
            requestAnimationFrame(etape);
          }
          var anneau = document.querySelector(".anneau-progression");
          if (anneau) {
            var cible = parseFloat(anneau.dataset.cible) || 0;
            var chiffre = anneau.querySelector(".anneau-chiffre");
            animerValeur(anneau, cible, function(v) {
              anneau.style.setProperty("--valeur", v);
              chiffre.textContent = Math.round(v);
            }, 1100);
          }
          document.querySelectorAll(".barre-comparaison-remplissage").forEach(function(el) {
            var cible = parseFloat(el.dataset.cible) || 0;
            animerValeur(el, cible, function(v) { el.style.setProperty("--valeur", v); }, 900);
          });
        })();
        </script>
        """, stats=stats, g=g, guild_id=guild_id)
        return page_html("Statistiques", corps, connecte(), compte.get("role"))

    # ---------- Catalogue de missions (instructeur et plus, scope serveur) ----------

    @app.route("/admin/missions/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_missions(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        message = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "ajouter":
                cat = request.form.get("categorie")
                texte = request.form.get("texte", "").strip()
                delai = request.form.get("delai", "").strip() or "3 jours"
                points_texte = request.form.get("points", "").strip()
                points_mission = int(points_texte) if points_texte.isdigit() else None
                if cat in ("commune", "moyenne", "difficile", "royal") and texte:
                    deps["sauvegarder_mission_fichier"](guild_id, cat, texte, delai, points=points_mission)
                    message = "Mission ajoutée."
            elif action == "supprimer":
                cat = request.form.get("categorie")
                index = int(request.form.get("index", -1))
                structure = deps["charger_missions_fichier"](guild_id)
                if cat in structure and 0 <= index < len(structure[cat]):
                    structure[cat].pop(index)
                    deps["reecrire_toutes_missions"](guild_id, structure)
                    message = "Mission supprimée."
            elif action == "tout_supprimer":
                deps["vider_toutes_missions"](guild_id)
                message = "Catalogue vidé."
            elif action == "points":
                config_points = deps["charger_points_categories"](guild_id)
                for cat in ("commune", "moyenne", "difficile", "royal"):
                    valeur = request.form.get(f"points_{cat}", "").strip()
                    if valeur.isdigit():
                        config_points[cat] = int(valeur)
                deps["sauvegarder_points_categories"](guild_id, config_points)
                message = "Points par catégorie mis à jour."
            elif action == "modifier_points_mission":
                cat = request.form.get("categorie")
                index = int(request.form.get("index", -1))
                valeur = request.form.get("points_mission", "").strip()
                points_mission = int(valeur) if valeur.isdigit() else None
                if cat in ("commune", "moyenne", "difficile", "royal") and deps["definir_points_mission"](guild_id, cat, index, points_mission):
                    message = "Points de la mission mis à jour." if points_mission is not None else "Mission repassée aux points par défaut de sa catégorie."
                else:
                    message = "Mission introuvable."

        structure = deps["charger_missions_fichier"](guild_id)
        config_points = deps["charger_points_categories"](guild_id)
        corps = render_template_string("""
        <h1>Catalogue de missions</h1>
        <p class="muted">Serveur {{ guild_id }}</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}

        <div class="card">
          <h2 style="margin-top:0">🏅 Points par catégorie</h2>
          <p class="muted">Toutes les missions d'une même catégorie rapportent le même nombre de points, entièrement modifiable ici. S'applique dès la prochaine mission acceptée.</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="points">
            {% for cat in ["commune", "moyenne", "difficile", "royal"] %}
            <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;">{{ cat|capitalize }}
              <input type="number" min="0" step="1" name="points_{{ cat }}" value="{{ config_points.get(cat, 0) }}" style="width:100px">
            </label>
            {% endfor %}
            <button type="submit" style="align-self:flex-end;">Enregistrer</button>
          </form>
        </div>

        <div class="card">
          <h2 style="margin-top:0">Ajouter une mission</h2>
          <form method="post" class="row">
            <input type="hidden" name="action" value="ajouter">
            <select name="categorie">
              <option value="commune">Commune</option>
              <option value="moyenne">Moyenne</option>
              <option value="difficile">Difficile</option>
              <option value="royal">Royal</option>
            </select>
            <input name="texte" placeholder="Description de la mission" style="flex:1;min-width:220px" required>
            <input name="delai" placeholder="Délai (ex: 3 jours)" style="width:140px">
            <input type="number" min="0" step="1" name="points" placeholder="Points (défaut : catégorie)" style="width:190px">
            <button type="submit">Ajouter</button>
          </form>
        </div>

        {% for cat, missions in structure.items() %}
        <h2>{{ cat|capitalize }} ({{ missions|length }}) · <span class="muted" style="font-size:14px;">{{ config_points.get(cat, 0) }} pts / mission</span></h2>
        <div class="card">
          {% if not missions %}<p class="muted">Aucune mission.</p>{% endif %}
          {% if missions %}
          <table>
          {% for m in missions %}
            <tr>
              <td>{{ loop.index }}</td>
              <td>{{ m.texte }}</td>
              <td class="muted">{{ m.delai }}</td>
              <td>
                <form method="post" class="inline row" style="gap:6px;">
                  <input type="hidden" name="action" value="modifier_points_mission">
                  <input type="hidden" name="categorie" value="{{ cat }}">
                  <input type="hidden" name="index" value="{{ loop.index0 }}">
                  <input type="number" min="0" step="1" name="points_mission" value="{{ m.points if m.points is not none else '' }}" placeholder="{{ config_points.get(cat, 0) }} (défaut)" style="width:150px">
                  <button type="submit" title="Laisser vide pour repasser aux points de la catégorie">OK</button>
                </form>
              </td>
              <td>
                <form method="post" class="inline">
                  <input type="hidden" name="action" value="supprimer">
                  <input type="hidden" name="categorie" value="{{ cat }}">
                  <input type="hidden" name="index" value="{{ loop.index0 }}">
                  <button class="danger" type="submit" onclick="return confirm('Supprimer cette mission ?')">Suppr.</button>
                </form>
              </td>
            </tr>
          {% endfor %}
          </table>
          {% endif %}
        </div>
        {% endfor %}

        <form method="post" onsubmit="return confirm('Vider TOUT le catalogue de ce serveur ?')">
          <input type="hidden" name="action" value="tout_supprimer">
          <button class="danger" type="submit">Vider tout le catalogue</button>
        </form>
        """, structure=structure, message=message, guild_id=guild_id, config_points=config_points)
        return page_html("Catalogue de missions", corps, connecte(), compte.get("role"))

    # ---------- Attribuer une mission précise à un joueur (instructeur et plus, scope serveur) ----------
    # Contrairement au catalogue (qui liste les missions possibles) ou à
    # "Missions en cours" (qui suit les missions déjà lancées), cette page
    # sert à DÉCLENCHER une nouvelle mission depuis le site : on choisit le
    # joueur, la catégorie, la mission précise (ou un texte personnalisé),
    # le délai et les points, un par un — pas de tirage aléatoire. Un
    # nouveau salon de ticket est créé sur Discord (comme /openticket) et
    # le décret y est posté directement, chrono lancé immédiatement.

    @app.route("/admin/attribuer-mission/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_attribuer_mission(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        g = discord.utils.get(bot.guilds, id=guild_id)
        structure = deps["charger_missions_fichier"](guild_id)
        config_points = deps["charger_points_categories"](guild_id)
        message = None
        erreur = None

        if request.method == "POST":
            joueur_saisi = request.form.get("joueur_id", "").strip()
            cat = request.form.get("categorie", "").strip()
            mission_index = request.form.get(f"mission_{cat}", "").strip()
            texte_perso = request.form.get("texte_perso", "").strip()
            delai_saisi = request.form.get("delai", "").strip()
            points_saisi = request.form.get("points", "").strip()

            if cat not in ("commune", "moyenne", "difficile", "royal"):
                erreur = "Choisis une catégorie valide."
            else:
                mission_catalogue = None
                if mission_index.isdigit():
                    idx = int(mission_index)
                    if 0 <= idx < len(structure.get(cat, [])):
                        mission_catalogue = structure[cat][idx]

                if mission_catalogue:
                    texte = mission_catalogue["texte"]
                    delai_texte = delai_saisi or mission_catalogue.get("delai") or "3 jours"
                    if points_saisi.isdigit():
                        points = int(points_saisi)
                    else:
                        points = mission_catalogue.get("points")
                else:
                    texte = texte_perso
                    delai_texte = delai_saisi or "3 jours"
                    points = int(points_saisi) if points_saisi.isdigit() else None

                if not joueur_saisi:
                    erreur = "Indique un pseudo (ou ID Discord) de joueur."
                elif not texte:
                    erreur = "Choisis une mission dans le catalogue ou écris un texte de mission personnalisé."
                elif not g:
                    erreur = "Ce serveur Discord est introuvable (le bot n'y a peut-être plus accès)."
                else:
                    joueur_id_str, erreur_resolution = _resoudre_joueur(g, joueur_saisi)
                    if erreur_resolution:
                        erreur = erreur_resolution
                    else:
                        joueur_id = int(joueur_id_str)
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                deps["attribuer_mission_precise_site"](g, joueur_id, texte, delai_texte, cat, points, connecte()),
                                bot.loop,
                            )
                            resultat = future.result(timeout=15)
                        except Exception as e:
                            resultat = {"ok": False, "erreur": f"Erreur lors de la communication avec le bot Discord : {e}"}

                        if resultat.get("ok"):
                            message = f"✅ Mission attribuée à <@{joueur_id}> — salon créé : {resultat.get('channel_name')}."
                            deps["sauvegarder_log_disque"](
                                f"📜 Mission précise attribuée à <@{joueur_id}> ({cat}) depuis le site par {connecte()} : « {texte} »."
                            )
                        else:
                            erreur = resultat.get("erreur", "Erreur inconnue lors de l'attribution.")

        corps = render_template_string("""
        <h1>🎯 Attribuer une mission précise</h1>
        <p class="muted">Serveur {{ guild_id }} — choisis le joueur, la catégorie, puis la mission exacte (ou écris-en une nouvelle) à lui attribuer directement. Un nouveau salon de ticket sera créé sur Discord avec le décret déjà lancé.</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="card">
          <form method="post" id="form-attribuer">
            <p>
              <label class="muted">Joueur (pseudo, pseudo serveur ou ID Discord)</label><br>
              <input name="joueur_id" placeholder="ex : Mavie7620 ou 123456789012345678" style="width:100%;max-width:420px" required>
            </p>

            <p>
              <label class="muted">Catégorie</label><br>
              <select name="categorie" id="select-categorie" onchange="afficherCategorieMission(this.value)">
                <option value="commune">🟢 Commune</option>
                <option value="moyenne">🔵 Moyenne</option>
                <option value="difficile">🟠 Difficile</option>
                <option value="royal">🔴 Royal</option>
              </select>
            </p>

            {% for cat in ["commune", "moyenne", "difficile", "royal"] %}
            <div class="bloc-cat-mission" id="bloc-mission-{{ cat }}" style="{{ '' if cat == 'commune' else 'display:none;' }}">
              <label class="muted">Mission ({{ cat }}) — {{ config_points.get(cat, 0) }} pts par défaut</label><br>
              <select name="mission_{{ cat }}" style="width:100%;max-width:420px">
                <option value="">— Mission personnalisée (texte libre ci-dessous) —</option>
                {% for m in structure.get(cat, []) %}
                <option value="{{ loop.index0 }}">{{ m.texte }} ({{ m.delai }}{{ ", " ~ m.points ~ " pts" if m.points is not none else "" }})</option>
                {% endfor %}
              </select>
            </div>
            {% endfor %}

            <p>
              <label class="muted">Texte personnalisé (utilisé seulement si « Mission personnalisée » est sélectionné ci-dessus)</label><br>
              <textarea name="texte_perso" rows="2" style="width:100%;max-width:420px;font-family:inherit" placeholder="Décris la mission si tu ne choisis pas dans le catalogue"></textarea>
            </p>

            <div class="row">
              <label class="muted" style="display:flex;flex-direction:column;gap:4px;">Délai (optionnel — sinon celui de la mission choisie, ou 3 jours)
                <input name="delai" placeholder="ex : 3 jours" style="width:180px">
              </label>
              <label class="muted" style="display:flex;flex-direction:column;gap:4px;">Points (optionnel — sinon ceux de la mission/catégorie)
                <input type="number" min="0" step="1" name="points" style="width:180px">
              </label>
            </div>

            <button type="submit" style="margin-top:10px;">📜 Attribuer et lancer le chrono</button>
          </form>
        </div>

        <script>
        function afficherCategorieMission(cat) {
          document.querySelectorAll(".bloc-cat-mission").forEach(function (bloc) {
            bloc.style.display = (bloc.id === "bloc-mission-" + cat) ? "" : "none";
          });
        }
        </script>
        """, guild_id=guild_id, structure=structure, config_points=config_points, message=message, erreur=erreur)
        return page_html("Attribuer une mission", corps, connecte(), compte.get("role"))

    # ---------- Boutique (instructeur et plus, scope serveur) ----------

    STYLE_BOUTIQUE = """
    <style>
      .boutique-grille { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px; margin-top:18px; }
      .boutique-carte { display:flex; flex-direction:column; gap:6px; }
      .boutique-image { width:100%; height:150px; object-fit:cover; border-radius:10px; background:var(--bg-soft); }
      .boutique-inactif { opacity:0.55; }
    </style>
    """

    @app.route("/admin/boutique/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_boutique(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        message = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "ajouter":
                nom = request.form.get("nom", "").strip()
                description = request.form.get("description", "").strip()
                cout_texte = request.form.get("cout", "").strip()
                stock_texte = request.form.get("stock", "").strip()
                image_url = request.form.get("image_url", "").strip() or None
                cout = int(cout_texte) if cout_texte.isdigit() else None
                stock = int(stock_texte) if stock_texte.isdigit() else None
                image_fichier = _enregistrer_image_produit(request.files.get("image"))
                if nom and cout is not None:
                    deps["ajouter_produit_boutique"](
                        guild_id, nom, cout, description=description,
                        image=image_fichier, image_url=image_url, stock=stock,
                    )
                    message = "Produit ajouté."
                else:
                    message = "Le nom et le coût (en points) sont obligatoires."
            elif action == "supprimer":
                produit_id = request.form.get("produit_id")
                if deps["supprimer_produit_boutique"](guild_id, produit_id):
                    message = "Produit supprimé."
            elif action == "basculer_actif":
                produit_id = request.form.get("produit_id")
                produit = deps["obtenir_produit_boutique"](guild_id, produit_id)
                if produit:
                    deps["modifier_produit_boutique"](guild_id, produit_id, actif=not produit.get("actif", True))
                    message = "Disponibilité mise à jour."

        produits = deps["charger_boutique"](guild_id)
        corps = render_template_string(STYLE_DROPZONE + STYLE_BOUTIQUE + """
        <h1>🛒 Boutique</h1>
        <p class="muted">Serveur {{ guild_id }} — les achats des joueurs déduisent leurs points mais ne livrent rien automatiquement : c'est à un instructeur de remettre la récompense ensuite.</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}

        <div class="card">
          <h2 style="margin-top:0">Ajouter un produit</h2>
          <form method="post" enctype="multipart/form-data" class="row">
            <input type="hidden" name="action" value="ajouter">
            <input name="nom" placeholder="Nom du produit" required style="flex:1;min-width:180px">
            <input type="number" min="0" step="1" name="cout" placeholder="Coût (points)" required style="width:160px">
            <input type="number" min="0" step="1" name="stock" placeholder="Stock (vide = illimité)" style="width:190px">
            <input name="description" placeholder="Description (optionnel)" style="flex:1;min-width:220px">
            <div class="muted" style="display:flex;flex-direction:column;gap:4px;font-size:12px;">Image ({{ "|".join(extensions) }})
              <div class="dropzone">
                <input type="file" name="image" accept="image/*">
                <span class="dropzone-icone">🖼️</span>
                <span class="dropzone-texte">Glisser une image ici, ou cliquer</span>
              </div>
            </div>
            <input name="image_url" placeholder="OU URL d'image (si pas de fichier)" style="flex:1;min-width:220px">
            <button type="submit">Ajouter</button>
          </form>
        </div>

        <div class="boutique-grille">
          {% if not produits %}<div class="card muted">Aucun produit pour l'instant.</div>{% endif %}
          {% for p in produits %}
          <div class="card boutique-carte {{ '' if p.actif else 'boutique-inactif' }}">
            {% if p.image %}<img class="boutique-image" src="/boutique-images/{{ p.image }}" alt="{{ p.nom }}">
            {% elif p.image_url %}<img class="boutique-image" src="{{ p.image_url }}" alt="{{ p.nom }}">{% endif %}
            <h3 style="margin:0;">{{ p.nom }}</h3>
            {% if p.description %}<p class="muted" style="margin:0;">{{ p.description }}</p>{% endif %}
            <p style="margin:0;"><strong>{{ p.cout }} pts</strong>{% if p.stock is not none %} · <span class="muted">Stock : {{ p.stock }}</span>{% endif %}</p>
            {% if not p.actif %}<p class="muted" style="margin:0;">🚫 Désactivé (invisible pour les joueurs)</p>{% endif %}
            <div class="row" style="gap:8px;">
              <form method="post" class="inline">
                <input type="hidden" name="action" value="basculer_actif">
                <input type="hidden" name="produit_id" value="{{ p.id }}">
                <button type="submit" class="secondary">{{ "Désactiver" if p.actif else "Activer" }}</button>
              </form>
              <form method="post" class="inline">
                <input type="hidden" name="action" value="supprimer">
                <input type="hidden" name="produit_id" value="{{ p.id }}">
                <button class="danger" type="submit" onclick="return confirm('Supprimer ce produit ?')">Suppr.</button>
              </form>
            </div>
          </div>
          {% endfor %}
        </div>
        """ + SCRIPT_DROPZONE, produits=produits, message=message, guild_id=guild_id, extensions=sorted(EXTENSIONS_IMAGE_AUTORISEES))
        return page_html("Boutique", corps, connecte(), compte.get("role"))

    # ---------- Roue (instructeur et plus, scope serveur) ----------
    # Gestion des parts de la roue : ajout, modification (nom / % / points /
    # image), activation / désactivation, suppression. Le rééquilibrage
    # entre les parts (pour que le total des parts actives reste toujours
    # 100%) est entièrement géré côté bot (voir ajouter_part_roue /
    # modifier_part_roue / basculer_actif_part_roue / supprimer_part_roue
    # dans bot.py) : cette page ne fait qu'appeler ces fonctions et
    # réafficher le résultat.
    #
    # Il y a maintenant TROIS roues indépendantes (moyenne / difficile /
    # royal, voir deps["types_roue"]) : cette même page gère les trois, un
    # onglet à la fois (paramètre ?type=... dans l'URL, "moyenne" par
    # défaut) — tout ce qui est configuré ici (parts, %, images...) ne
    # s'applique qu'à la roue actuellement sélectionnée.

    TYPES_ROUE = deps["types_roue"]
    NOMS_TYPES_ROUE = deps["noms_types_roue"]

    PALETTE_ROUE = [
        "#e74c3c", "#3498db", "#2ecc71", "#f1c40f", "#9b59b6",
        "#1abc9c", "#e67e22", "#34495e", "#e84393", "#00cec9",
    ]

    def _couleur_part_roue(index):
        return PALETTE_ROUE[index % len(PALETTE_ROUE)]

    def _degrade_roue(parts):
        """Construit la chaîne CSS conic-gradient représentant la roue à
        partir des parts ACTIVES uniquement (dans leur ordre de stockage),
        chacune colorée par sa position dans la liste complète (pour que la
        couleur d'une part reste stable même si une autre est désactivée)."""
        actives = [(i, p) for i, p in enumerate(parts) if p.get("actif", True) and p["pourcentage"] > 0]
        if not actives:
            return "#2b2b33"
        segments = []
        curseur = 0.0
        for i, p in actives:
            debut = curseur
            fin = curseur + p["pourcentage"]
            segments.append(f"{_couleur_part_roue(i)} {debut:.4f}% {fin:.4f}%")
            curseur = fin
        return "conic-gradient(from 0deg, " + ", ".join(segments) + ")"

    STYLE_ROUE = """
    <style>
      .roue-liste { display:flex; flex-direction:column; gap:10px; margin-top:18px; }
      .roue-part { display:flex; align-items:center; gap:14px; }
      .roue-part-couleur { width:16px; height:16px; border-radius:50%; flex-shrink:0; }
      .roue-part-image { width:32px; height:32px; border-radius:6px; object-fit:cover; flex-shrink:0; }
      .roue-part-nom { flex:1; min-width:120px; }
      .roue-part-barre { flex:2; min-width:140px; height:10px; border-radius:6px; background:var(--bg-soft); overflow:hidden; }
      .roue-part-barre-remplie { height:100%; border-radius:6px; }
      .roue-part-inactif { opacity:0.5; }
      .roue-apercu { width:180px; height:180px; border-radius:50%; margin:0 auto 18px; border:4px solid var(--border); }
      .roue-onglets { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
      .roue-onglet { padding:8px 14px; border-radius:8px; background:var(--bg-soft); text-decoration:none; color:inherit; }
      .roue-onglet.actif { background:var(--accent, #5865f2); color:#fff; }
    </style>
    """

    def _onglets_roue_html(type_actif, base_url):
        liens = "".join(
            f'<a class="roue-onglet {"actif" if t == type_actif else ""}" href="{base_url}?type={t}">{NOMS_TYPES_ROUE[t]}</a>'
            for t in TYPES_ROUE
        )
        return f'<div class="roue-onglets">{liens}</div>'

    @app.route("/admin/roue/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_roue(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        message = None

        type_roue = request.values.get("type", "moyenne")
        if type_roue not in TYPES_ROUE:
            type_roue = "moyenne"

        if request.method == "POST":
            action = request.form.get("action")
            if action == "ajouter":
                nom = request.form.get("nom", "").strip()
                pourcentage_texte = request.form.get("pourcentage", "").strip()
                points_texte = request.form.get("points", "").strip()
                image_texte = request.form.get("image", "").strip()
                image_upload = _enregistrer_image_produit(request.files.get("image_fichier"))
                image = f"/boutique-images/{image_upload}" if image_upload else image_texte
                pourcentage = None
                if pourcentage_texte:
                    try:
                        pourcentage = max(0.0, min(100.0, float(pourcentage_texte.replace(",", "."))))
                    except ValueError:
                        pourcentage = None
                points = int(points_texte) if points_texte.isdigit() else 0
                if nom:
                    deps["ajouter_part_roue"](guild_id, type_roue, nom, pourcentage, points, image)
                    message = "Part ajoutée — les autres parts actives ont été réajustées automatiquement."
                else:
                    message = "Le nom de la part est obligatoire."
            elif action == "modifier":
                part_id = request.form.get("part_id")
                nouveau_nom = request.form.get("nom", "").strip() or None
                pourcentage_texte = request.form.get("pourcentage", "").strip()
                points_texte = request.form.get("points", "").strip()
                image_upload = _enregistrer_image_produit(request.files.get("image_fichier"))
                if image_upload:
                    nouvelle_image = f"/boutique-images/{image_upload}"
                else:
                    nouvelle_image = request.form.get("image", None)
                    if nouvelle_image is not None:
                        nouvelle_image = nouvelle_image.strip() or None
                nouveau_pourcentage = None
                if pourcentage_texte:
                    try:
                        nouveau_pourcentage = max(0.0, min(100.0, float(pourcentage_texte.replace(",", "."))))
                    except ValueError:
                        nouveau_pourcentage = None
                nouveaux_points = int(points_texte) if points_texte.isdigit() else None
                if deps["modifier_part_roue"](guild_id, type_roue, part_id, nom=nouveau_nom, pourcentage=nouveau_pourcentage, points=nouveaux_points, image=nouvelle_image):
                    message = "Part mise à jour — les autres parts actives ont été réajustées automatiquement." if nouveau_pourcentage is not None else "Part mise à jour."
                else:
                    message = "Part introuvable."
            elif action == "basculer_actif":
                part_id = request.form.get("part_id")
                if deps["basculer_actif_part_roue"](guild_id, type_roue, part_id):
                    message = "Statut mis à jour — les autres parts actives ont été réajustées automatiquement."
                else:
                    message = "Part introuvable."
            elif action == "supprimer":
                part_id = request.form.get("part_id")
                if deps["supprimer_part_roue"](guild_id, type_roue, part_id):
                    message = "Part supprimée — les autres parts actives ont été réajustées automatiquement."
                else:
                    message = "Part introuvable."

        parts = deps["charger_roue"](guild_id, type_roue)
        degrade = _degrade_roue(parts)
        onglets_html = _onglets_roue_html(type_roue, f"/admin/roue/{guild_id}")
        corps = render_template_string(STYLE_DROPZONE + STYLE_ROUE + """
        <h1>🎡 """ + NOMS_TYPES_ROUE[type_roue] + """</h1>
        <p class="muted">Serveur {{ guild_id }} — le total des parts actives reste toujours 100% : fixer le % d'une part réajuste automatiquement toutes les autres, sur CETTE roue uniquement.</p>
        """ + onglets_html + """
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}

        <div class="roue-apercu" style="background:{{ degrade }};"></div>

        <div class="card">
          <h2 style="margin-top:0">Ajouter une part</h2>
          <form method="post" enctype="multipart/form-data" class="row">
            <input type="hidden" name="action" value="ajouter">
            <input type="hidden" name="type" value="{{ type_roue }}">
            <input name="nom" placeholder="Nom de la part (ex: Grade VIP, Rien, 50 points)" required style="flex:1;min-width:220px">
            <input type="number" min="0" max="100" step="0.01" name="pourcentage" placeholder="% (vide = part égale)" style="width:200px">
            <input type="number" min="0" step="1" name="points" placeholder="Points offerts (0 = aucun)" style="width:200px">
            <div class="dropzone">
              <input type="file" name="image_fichier" accept="image/*">
              <span class="dropzone-icone">🖼️</span>
              <span class="dropzone-texte">Glisser une image ici, ou cliquer</span>
            </div>
            <input name="image" placeholder="OU URL de l'image externe" style="flex:1;min-width:220px">
            <button type="submit">Ajouter</button>
          </form>
        </div>

        <div class="roue-liste">
          {% if not parts %}<div class="card muted">Aucune part configurée pour l'instant sur cette roue.</div>{% endif %}
          {% for p in parts %}
          <div class="card roue-part {{ '' if p.actif else 'roue-part-inactif' }}">
            <div class="roue-part-couleur" style="background:{{ couleurs[loop.index0] }};"></div>
            {% if p.image %}<img class="roue-part-image" src="{{ p.image }}" alt="">{% endif %}
            <div class="roue-part-nom">
              <strong>{{ p.nom }}</strong>{% if not p.actif %} <span class="muted">(désactivée)</span>{% endif %}
              {% if p.points %}<br><span class="muted">🏅 {{ p.points }} pts</span>{% endif %}
            </div>
            <div class="roue-part-barre"><div class="roue-part-barre-remplie" style="width:{{ p.pourcentage if p.actif else 0 }}%;background:{{ couleurs[loop.index0] }};"></div></div>
            <div style="width:60px;text-align:right;"><strong>{{ p.pourcentage }}%</strong></div>
            <form method="post" class="inline">
              <input type="hidden" name="action" value="basculer_actif">
              <input type="hidden" name="type" value="{{ type_roue }}">
              <input type="hidden" name="part_id" value="{{ p.id }}">
              <button type="submit" class="secondary">{{ "Désactiver" if p.actif else "Activer" }}</button>
            </form>
            <form method="post" class="inline">
              <input type="hidden" name="action" value="supprimer">
              <input type="hidden" name="type" value="{{ type_roue }}">
              <input type="hidden" name="part_id" value="{{ p.id }}">
              <button class="danger" type="submit" onclick="return confirm('Supprimer cette part ?')">Suppr.</button>
            </form>
          </div>
          <details>
            <summary class="muted" style="cursor:pointer;">Modifier « {{ p.nom }} »</summary>
            <form method="post" enctype="multipart/form-data" class="row" style="margin-top:8px;">
              <input type="hidden" name="action" value="modifier">
              <input type="hidden" name="type" value="{{ type_roue }}">
              <input type="hidden" name="part_id" value="{{ p.id }}">
              <input name="nom" placeholder="Nouveau nom (vide = inchangé)" style="flex:1;min-width:200px">
              <input type="number" min="0" max="100" step="0.01" name="pourcentage" placeholder="Nouveau % (vide = inchangé)" style="width:220px">
              <input type="number" min="0" step="1" name="points" placeholder="Nouveaux points (vide = inchangé)" style="width:220px">
              <div class="dropzone">
                <input type="file" name="image_fichier" accept="image/*">
                <span class="dropzone-icone">🖼️</span>
                <span class="dropzone-texte">Glisser une nouvelle image, ou cliquer</span>
              </div>
              <input name="image" value="{{ p.image or '' }}" placeholder="OU URL de l'image (vide = inchangé)" style="flex:1;min-width:220px">
              <button type="submit">Valider</button>
            </form>
          </details>
          {% endfor %}
        </div>
        """ + SCRIPT_DROPZONE, parts=parts, message=message, guild_id=guild_id, degrade=degrade, type_roue=type_roue,
             couleurs=[_couleur_part_roue(i) for i in range(len(parts))])
        return page_html("Roue", corps, connecte(), compte.get("role"))

    # ---------- Missions en cours (instructeur et plus, scope serveur) ----------

    def _serialiser_mission_active(guild, joueur_id, m):
        """Transforme une mission active (dict interne du bot) en dict
        JSON-compatible, réutilisé par la page et par l'API temps réel."""
        membre = guild.get_member(joueur_id) if guild else None
        return {
            "joueur_id": joueur_id,
            "nom": membre.display_name if membre else None,
            "texte": m["texte"],
            "cat": m["cat"],
            "date_debut": int(m["date_debut"].timestamp()),
            "date_fin": int(m["date_fin"].timestamp()),
            "duree_totale": m["duree_totale"].total_seconds(),
            "en_attente": bool(m.get("en_attente", False)),
        }

    @app.route("/admin/api/missions-actives/<int:guild_id>")
    @role_required("instructeur")
    def api_missions_actives(guild_id):
        """Endpoint JSON interrogé en continu par la page 'Missions en
        cours' pour l'affichage en temps réel (ajout/fin de mission
        détectés sans recharger la page) et pour le chrono de chacune."""
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        missions_actives = deps["missions_actives"]
        g = discord.utils.get(bot.guilds, id=guild_id)
        actives = [
            _serialiser_mission_active(g, joueur_id, m)
            for joueur_id, m in missions_actives.get(guild_id, {}).items()
        ]
        actives.sort(key=lambda x: x["date_fin"])
        reponse = jsonify({
            "actives": actives,
            "count": len(actives),
            "serveur_temps": int(datetime.now().timestamp()),
        })
        # Empêche le navigateur (ou un éventuel cache intermédiaire) de
        # resservir une ancienne réponse à cet endpoint interrogé en
        # continu par le JS de la page "Missions en cours".
        reponse.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return reponse

    @app.route("/admin/missions-actives/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_missions_actives(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        missions_actives = deps["missions_actives"]
        message = None
        erreur = None
        g = discord.utils.get(bot.guilds, id=guild_id)

        if request.method == "POST":
            joueur_id = int(request.form.get("joueur_id"))
            action = request.form.get("action", "statut")
            if guild_id in missions_actives and joueur_id in missions_actives[guild_id]:
                m_info = missions_actives[guild_id][joueur_id]
                channel = bot.get_channel(m_info["channel_id"])

                if action == "temps":
                    duree_texte = request.form.get("duree", "").strip()
                    retirer = request.form.get("sens") == "retirer"
                    if not duree_texte:
                        erreur = "Indique une durée (ex : 2h, 1 jour)."
                    else:
                        delta = deps["extraire_duree"](duree_texte)
                        if retirer:
                            m_info["date_fin"] -= delta
                            m_info["duree_totale"] -= delta
                        else:
                            m_info["date_fin"] += delta
                            m_info["duree_totale"] += delta
                        deps["sauvegarder_log_disque"](f"⏱️ Temps {'retiré' if retirer else 'ajouté'} ({duree_texte}) sur la mission du joueur {joueur_id} depuis le site par {connecte()}.")
                        message = f"{'Retiré' if retirer else 'Ajouté'} {duree_texte} avec succès."

                        # Prévient le ticket + le salon de validation qu'un
                        # instructeur a changé le temps depuis le site web.
                        timestamp_fin = int(m_info["date_fin"].timestamp())
                        texte_notif = (
                            f"⏱️ **Temps {'retiré' if retirer else 'ajouté'} par un instructeur depuis le site web** : {duree_texte}.\n"
                            f"Nouvelle échéance : <t:{timestamp_fin}:R> (<t:{timestamp_fin}:f>)."
                        )
                        if channel:
                            try:
                                future = asyncio.run_coroutine_threadsafe(channel.send(texte_notif), bot.loop)
                                future.result(timeout=10)
                            except Exception:
                                pass
                        if g:
                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    deps["envoyer_double_notification"](g, "", f"⏱️ {connecte()} a {'retiré' if retirer else 'ajouté'} {duree_texte} sur la mission de <@{joueur_id}> depuis le site web."),
                                    bot.loop,
                                )
                                future.result(timeout=10)
                            except Exception:
                                pass
                else:
                    statut = request.form.get("statut")
                    if not channel:
                        erreur = "Salon du ticket introuvable (peut-être déjà supprimé) : action annulée."
                    else:
                        try:
                            if statut == "succes":
                                future = asyncio.run_coroutine_threadsafe(deps["action_accepter_mission"](joueur_id, channel), bot.loop)
                                message = "Mission marquée comme réussie."
                            else:
                                future = asyncio.run_coroutine_threadsafe(deps["action_refuser_mission"](joueur_id, channel), bot.loop)
                                message = "Mission marquée comme échouée."
                            future.result(timeout=10)
                            deps["sauvegarder_log_disque"](f"⚖️ Mission du joueur {joueur_id} marquée '{statut}' depuis le site par {connecte()}.")
                        except Exception as e:
                            erreur = f"Erreur lors de la notification Discord : {e}"
        actives = [
            _serialiser_mission_active(g, joueur_id, m)
            for joueur_id, m in missions_actives.get(guild_id, {}).items()
        ]
        actives.sort(key=lambda x: x["date_fin"])
        emojis_cat = {"commune": "🟢", "moyenne": "🔵", "difficile": "🟠", "royal": "🔴"}

        corps = render_template_string("""
        <div class="row" style="justify-content:space-between;align-items:flex-start;">
          <div>
            <h1>Missions en cours</h1>
            <p class="muted">Serveur {{ guild_id }} — <span id="compteur-total">{{ actives|length }}</span> mission(s) active(s)</p>
          </div>
          <div style="text-align:right;">
            <span class="live-indicateur"><span class="live-dot"></span>Temps réel</span>
            <div class="muted" style="font-size:11px;margin-top:4px;">Actualisé <span id="dernier-refresh">à l'instant</span></div>
          </div>
        </div>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
        <p class="muted">ℹ️ Les actions ci-dessous mettent à jour les fichiers du bot ET envoient un message dans le ticket Discord du joueur (ainsi que dans le salon de validation). La liste et les chronos se mettent à jour tout seuls, inutile de recharger la page.</p>

        <div id="missions-container">
        {% for m in actives %}
        <div class="card mission-carte" id="mission-{{ m.joueur_id }}" data-joueur="{{ m.joueur_id }}" data-fin="{{ m.date_fin }}" data-debut="{{ m.date_debut }}" data-duree="{{ m.duree_totale }}">
          <div class="row" style="justify-content:space-between;align-items:flex-start;">
            <div>
              <strong>{% if m.nom %}{{ m.nom }}{% else %}Joueur {{ m.joueur_id }}{% endif %}</strong>
              <span class="muted">· {{ m.joueur_id }}</span>
              <span class="muted">— {{ emojis_cat.get(m.cat, "📜") }} {{ m.cat|capitalize }}</span>
              {% if m.en_attente %}<span class="pill attente">⏳ En attente de validation</span>{% endif %}
              <div style="margin-top:4px;">{{ m.texte }}</div>
            </div>
            <div style="text-align:right;">
              <div class="chrono" data-chrono="{{ m.joueur_id }}">--</div>
              <div class="muted" style="font-size:11px;margin-top:2px;" data-fin-lisible="{{ m.joueur_id }}"></div>
            </div>
          </div>
          <div class="row" style="justify-content:space-between;margin-top:14px;border-top:1px solid var(--border);padding-top:14px;">
            <div class="row">
              <form method="post" class="inline">
                <input type="hidden" name="joueur_id" value="{{ m.joueur_id }}">
                <input type="hidden" name="action" value="statut">
                <input type="hidden" name="statut" value="succes">
                <button type="submit">✅ Marquer réussie</button>
              </form>
              <form method="post" class="inline">
                <input type="hidden" name="joueur_id" value="{{ m.joueur_id }}">
                <input type="hidden" name="action" value="statut">
                <input type="hidden" name="statut" value="echec">
                <button class="danger" type="submit">❌ Marquer échouée</button>
              </form>
            </div>
            <form method="post" class="row">
              <input type="hidden" name="joueur_id" value="{{ m.joueur_id }}">
              <input type="hidden" name="action" value="temps">
              <input name="duree" placeholder="ex : 2h, 1 jour, 30min" style="width:160px" required>
              <select name="sens">
                <option value="ajouter">➕ Ajouter</option>
                <option value="retirer">➖ Retirer</option>
              </select>
              <button class="secondary" type="submit">Appliquer le temps</button>
            </form>
          </div>
        </div>
        {% endfor %}
        </div>
        <div id="missions-vide" class="card" {% if actives %}style="display:none;"{% endif %}>Aucune mission en cours sur ce serveur.</div>

        <script>
        (function () {
          // IMPORTANT : les ID Discord ("snowflakes") dépassent la limite
          // des entiers exacts en JavaScript (Number.MAX_SAFE_INTEGER).
          // Les embarquer comme nombre JS les fait arrondir silencieusement,
          // ce qui envoie un guild_id légèrement faux à l'API et fait
          // disparaître les missions. On les garde donc en chaîne partout.
          var GUILD_ID = "{{ guild_id }}";
          var EMOJIS_CAT = {"commune": "🟢", "moyenne": "🔵", "difficile": "🟠", "royal": "🔴"};
          var conteneur = document.getElementById("missions-container");
          var videMsg = document.getElementById("missions-vide");
          var totalEl = document.getElementById("compteur-total");
          var refreshEl = document.getElementById("dernier-refresh");

          function echapper(txt) {
            var d = document.createElement("div");
            d.textContent = txt == null ? "" : String(txt);
            return d.innerHTML;
          }

          function formatDuree(secondes) {
            var neg = secondes < 0;
            secondes = Math.abs(Math.round(secondes));
            var j = Math.floor(secondes / 86400); secondes -= j * 86400;
            var h = Math.floor(secondes / 3600); secondes -= h * 3600;
            var m = Math.floor(secondes / 60); secondes -= m * 60;
            var s = secondes;
            var parts = [];
            if (j) parts.push(j + "j");
            if (h || j) parts.push(h + "h");
            parts.push(m + "min");
            parts.push(s + "s");
            var texte = parts.join(" ");
            return neg ? "⏰ En retard de " + texte : texte + " restant";
          }

          function majChronos() {
            var maintenant = Date.now() / 1000;
            var cartes = conteneur.querySelectorAll(".mission-carte");
            cartes.forEach(function (carte) {
              var fin = parseFloat(carte.getAttribute("data-fin"));
              var debut = parseFloat(carte.getAttribute("data-debut"));
              var duree = parseFloat(carte.getAttribute("data-duree")) || (fin - debut) || 1;
              var restant = fin - maintenant;
              var joueurId = carte.getAttribute("data-joueur");
              var chronoEl = carte.querySelector('[data-chrono="' + joueurId + '"]');
              var finLisibleEl = carte.querySelector('[data-fin-lisible="' + joueurId + '"]');
              if (!chronoEl) return;
              chronoEl.textContent = formatDuree(restant);
              chronoEl.classList.remove("ok", "warn", "retard");
              var ratio = restant / duree;
              if (restant < 0) chronoEl.classList.add("retard");
              else if (ratio < 0.25) chronoEl.classList.add("warn");
              else chronoEl.classList.add("ok");
              if (finLisibleEl) {
                var dFin = new Date(fin * 1000);
                finLisibleEl.textContent = "Fin prévue : " + dFin.toLocaleDateString("fr-FR") + " " + dFin.toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"});
              }
            });
          }

          function construireCarte(m) {
            var emoji = EMOJIS_CAT[m.cat] || "📜";
            var nomAffiche = m.nom ? echapper(m.nom) : "Joueur " + m.joueur_id;
            var pillAttente = m.en_attente ? '<span class="pill attente">⏳ En attente de validation</span>' : "";
            return (
              '<div class="card mission-carte nouvelle" id="mission-' + m.joueur_id + '" data-joueur="' + m.joueur_id + '" data-fin="' + m.date_fin + '" data-debut="' + m.date_debut + '" data-duree="' + m.duree_totale + '">' +
                '<div class="row" style="justify-content:space-between;align-items:flex-start;">' +
                  '<div>' +
                    '<strong>' + nomAffiche + '</strong> <span class="muted">· ' + m.joueur_id + '</span>' +
                    ' <span class="muted">— ' + emoji + ' ' + echapper(m.cat.charAt(0).toUpperCase() + m.cat.slice(1)) + '</span>' +
                    pillAttente +
                    '<div style="margin-top:4px;">' + echapper(m.texte) + '</div>' +
                  '</div>' +
                  '<div style="text-align:right;">' +
                    '<div class="chrono" data-chrono="' + m.joueur_id + '">--</div>' +
                    '<div class="muted" style="font-size:11px;margin-top:2px;" data-fin-lisible="' + m.joueur_id + '"></div>' +
                  '</div>' +
                '</div>' +
                '<div class="row" style="justify-content:space-between;margin-top:14px;border-top:1px solid var(--border);padding-top:14px;">' +
                  '<div class="row">' +
                    '<form method="post" class="inline"><input type="hidden" name="joueur_id" value="' + m.joueur_id + '"><input type="hidden" name="action" value="statut"><input type="hidden" name="statut" value="succes"><button type="submit">✅ Marquer réussie</button></form>' +
                    '<form method="post" class="inline"><input type="hidden" name="joueur_id" value="' + m.joueur_id + '"><input type="hidden" name="action" value="statut"><input type="hidden" name="statut" value="echec"><button class="danger" type="submit">❌ Marquer échouée</button></form>' +
                  '</div>' +
                  '<form method="post" class="row">' +
                    '<input type="hidden" name="joueur_id" value="' + m.joueur_id + '">' +
                    '<input type="hidden" name="action" value="temps">' +
                    '<input name="duree" placeholder="ex : 2h, 1 jour, 30min" style="width:160px" required>' +
                    '<select name="sens"><option value="ajouter">➕ Ajouter</option><option value="retirer">➖ Retirer</option></select>' +
                    '<button class="secondary" type="submit">Appliquer le temps</button>' +
                  '</form>' +
                '</div>' +
              '</div>'
            );
          }

          function focusDansConteneur() {
            var actif = document.activeElement;
            return actif && conteneur.contains(actif) && (actif.tagName === "INPUT" || actif.tagName === "SELECT" || actif.tagName === "TEXTAREA");
          }

          function actualiser() {
            if (focusDansConteneur()) return; // on ne dérange pas quelqu'un en train de remplir un formulaire
            fetch("/admin/api/missions-actives/" + GUILD_ID, {headers: {"X-Requested-With": "XMLHttpRequest"}})
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (data) {
                if (!data) return;
                var idsActuels = Array.prototype.map.call(conteneur.querySelectorAll(".mission-carte"), function (c) { return c.getAttribute("data-joueur"); });
                var idsNouveaux = data.actives.map(function (m) { return String(m.joueur_id); });
                var identique = idsActuels.length === idsNouveaux.length && idsActuels.every(function (id, i) { return id === idsNouveaux[i]; });

                if (!identique) {
                  conteneur.innerHTML = data.actives.map(construireCarte).join("");
                  videMsg.style.display = data.actives.length ? "none" : "block";
                }
                totalEl.textContent = data.count;
                majChronos();
                var maintenant = new Date();
                refreshEl.textContent = maintenant.toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
              })
              .catch(function () { /* silencieux : on retentera au prochain cycle */ });
          }

          majChronos();
          setInterval(majChronos, 1000);
          setInterval(actualiser, 6000);
        })();
        </script>
        """, actives=actives, message=message, erreur=erreur, guild_id=guild_id, emojis_cat=emojis_cat)
        return page_html("Missions en cours", corps, connecte(), compte.get("role"))

    # ---------- Profils (instructeur et plus, scope serveur) ----------

    @app.route("/admin/profils/<int:guild_id>")
    @role_required("instructeur")
    def admin_profils(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        profils = deps["charger_profils"](guild_id)
        g = discord.utils.get(bot.guilds, id=guild_id)
        noms = {}
        if g:
            for jid in profils.keys():
                m = g.get_member(int(jid)) if jid.isdigit() else None
                if m:
                    noms[jid] = m.display_name
        corps = render_template_string("""
        <h1>Profils des joueurs</h1>
        <p class="muted">Serveur {{ guild_id }} — {{ profils|length }} profil(s)</p>
        {% if not profils %}<div class="card">Aucun profil enregistré sur ce serveur.</div>{% endif %}
        {% if profils %}
        <table>
          <tr><th>Joueur</th><th>Réussies</th><th>Échouées</th><th>🏅 Points</th><th>🎟️ Tickets</th><th></th></tr>
          {% for jid, p in profils.items() %}
          <tr>
            <td>
              {% if noms.get(jid) %}
              <strong style="font-size:15px;">{{ noms[jid] }}</strong><div class="muted" style="font-size:11px;">{{ jid }}</div>
              {% else %}
              {{ jid }}
              {% endif %}
            </td>
            <td>{{ p.total_reussies }}</td>
            <td>{{ p.total_echouees }}</td>
            <td>{{ p.total_points or 0 }}</td>
            <td>{{ p.tickets_roue or 0 }}</td>
            <td><a class="btnlink" href="/admin/profils/{{ guild_id }}/{{ jid }}">Historique</a></td>
          </tr>
          {% endfor %}
        </table>
        {% endif %}
        """, profils=profils, guild_id=guild_id, noms=noms)
        return page_html("Profils", corps, connecte(), compte.get("role"))

    @app.route("/admin/profils/<int:guild_id>/<joueur_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_profil_detail(guild_id, joueur_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        peut_modifier = niveau_role(compte.get("role")) >= niveau_role("instructeur")
        message = None
        erreur = None

        if request.method == "POST":
            if not peut_modifier:
                abort(403)
            profils = deps["charger_profils"](guild_id)
            profil_courant = profils.get(str(joueur_id))
            if not profil_courant:
                abort(404)
            action = request.form.get("action")

            if action == "ajouter":
                statut = request.form.get("statut", "Succès")
                categorie = request.form.get("categorie", "commune")
                texte = request.form.get("texte", "").strip()
                if not texte:
                    erreur = "Décris la mission à ajouter."
                else:
                    points_gagnes = 0
                    if statut == "Succès":
                        profil_courant["total_reussies"] += 1
                        points_gagnes = deps["points_pour_categorie"](guild_id, categorie)
                        profil_courant["total_points"] = profil_courant.get("total_points", 0) + points_gagnes
                    else:
                        profil_courant["total_echouees"] += 1
                    deps["ajouter_historique"](int(joueur_id), profils, texte, statut, categorie, points=points_gagnes)
                    deps["sauvegarder_profils"](guild_id, profils)
                    deps["sauvegarder_log_disque"](f"📝 Entrée ajoutée à l'historique du joueur {joueur_id} depuis le site par {connecte()}.")
                    message = "Entrée ajoutée à l'historique."

            elif action == "retirer":
                index = int(request.form.get("index", -1))
                hist = profil_courant["historique"]
                if 0 <= index < len(hist):
                    entree = hist.pop(index)
                    if entree.get("statut") == "Succès":
                        profil_courant["total_reussies"] = max(0, profil_courant["total_reussies"] - 1)
                        profil_courant["total_points"] = max(0, profil_courant.get("total_points", 0) - entree.get("points", 0))
                    else:
                        profil_courant["total_echouees"] = max(0, profil_courant["total_echouees"] - 1)
                    deps["sauvegarder_profils"](guild_id, profils)
                    deps["sauvegarder_log_disque"](f"🗑️ Entrée retirée de l'historique du joueur {joueur_id} depuis le site par {connecte()}.")
                    message = "Entrée retirée de l'historique."
                else:
                    erreur = "Entrée introuvable."

            elif action == "reset":
                profil_courant["total_reussies"] = 0
                profil_courant["total_echouees"] = 0
                profil_courant["total_points"] = 0
                profil_courant["tickets_roue"] = 0
                profil_courant["historique"] = []
                deps["sauvegarder_profils"](guild_id, profils)
                deps["sauvegarder_log_disque"](f"♻️ Profil du joueur {joueur_id} réinitialisé depuis le site par {connecte()}.")
                message = "Profil réinitialisé."

            elif action == "donner_tickets":
                # Attribution manuelle de tickets de roue à ce joueur, via
                # son profil Discord — c'est le "système admin" qui permet
                # d'offrir des tickets sans passer par une mission.
                quantite_texte = request.form.get("quantite", "").strip()
                try:
                    quantite = int(quantite_texte)
                except ValueError:
                    quantite = 0
                if quantite == 0:
                    erreur = "Indique une quantité de tickets différente de 0."
                else:
                    nouveau_total = deps["ajouter_tickets_roue"](guild_id, int(joueur_id), quantite)
                    deps["sauvegarder_log_disque"](f"🎟️ {quantite:+d} ticket(s) de roue pour le joueur {joueur_id} (total : {nouveau_total}) depuis le site par {connecte()}.")
                    message = f"Tickets mis à jour — {joueur_id} a maintenant {nouveau_total} ticket(s) de roue."

        profils = deps["charger_profils"](guild_id)
        profil = profils.get(str(joueur_id))
        if not profil:
            abort(404)

        g = discord.utils.get(bot.guilds, id=guild_id)
        pseudo_joueur = None
        if g and str(joueur_id).isdigit():
            m = g.get_member(int(joueur_id))
            if m:
                pseudo_joueur = m.display_name

        corps = render_template_string("""
        {% if pseudo_joueur %}
        <h1 style="margin-bottom:2px;">{{ pseudo_joueur }}</h1>
        <p class="muted" style="font-size:11px;margin-top:0;">ID : {{ joueur_id }}</p>
        {% else %}
        <h1>Historique — Joueur {{ joueur_id }}</h1>
        {% endif %}
        <p class="muted">Serveur {{ guild_id }} — {{ profil.total_reussies }} réussies / {{ profil.total_echouees }} échouées — 🏅 {{ profil.total_points or 0 }} points — 🎟️ {{ profil.tickets_roue or 0 }} ticket(s) de roue</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        {% if peut_modifier %}
        <div class="card">
          <h2 style="margin-top:0">🎟️ Donner / retirer des tickets de roue</h2>
          <p class="muted" style="margin-top:0;">Un ticket permet à ce joueur d'activer la roue de son choix (moyenne, difficile ou royale) sur <a href="/roue">/roue</a>. Solde actuel : <strong>{{ profil.tickets_roue or 0 }}</strong>.</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="donner_tickets">
            <input type="number" name="quantite" step="1" placeholder="ex: 1 ou -1" style="width:160px" required>
            <button type="submit">Valider</button>
          </form>
        </div>

        <div class="card">
          <h2 style="margin-top:0">Ajouter une entrée manuellement</h2>
          <form method="post" class="row">
            <input type="hidden" name="action" value="ajouter">
            <select name="statut">
              <option value="Succès">Succès</option>
              <option value="Échec">Échec</option>
            </select>
            <select name="categorie">
              <option value="commune">Commune</option>
              <option value="moyenne">Moyenne</option>
              <option value="difficile">Difficile</option>
              <option value="royal">Royal</option>
            </select>
            <input name="texte" placeholder="Description de la mission" style="flex:1;min-width:220px" required>
            <button type="submit">Ajouter</button>
          </form>
        </div>

        <div class="card row" style="justify-content:space-between;">
          <div><strong>Zone dangereuse</strong><div class="muted">Remet à zéro tout l'historique et les compteurs de ce joueur.</div></div>
          <form method="post" class="inline" onsubmit="return confirm('Réinitialiser TOUT le profil de ce joueur ? Action irréversible.')">
            <input type="hidden" name="action" value="reset">
            <button class="danger" type="submit">Réinitialiser ce profil</button>
          </form>
        </div>
        {% endif %}

        <table>
          <tr><th>Date</th><th>Catégorie</th><th>Mission</th><th>Statut</th><th>🏅 Points</th>{% if peut_modifier %}<th></th>{% endif %}</tr>
          {% for h in profil.historique %}
          <tr>
            <td>{{ h.date }}</td>
            <td>{{ h.categorie }}</td>
            <td>{{ h.texte }}</td>
            <td>{{ h.statut }}</td>
            <td>{{ h.points or 0 if h.statut == "Succès" else "—" }}</td>
            {% if peut_modifier %}
            <td>
              <form method="post" class="inline" onsubmit="return confirm('Retirer cette entrée de l\\'historique ?')">
                <input type="hidden" name="action" value="retirer">
                <input type="hidden" name="index" value="{{ loop.index0 }}">
                <button class="danger" type="submit">Retirer</button>
              </form>
            </td>
            {% endif %}
          </tr>
          {% endfor %}
        </table>
        """, profil=profil, joueur_id=joueur_id, guild_id=guild_id, peut_modifier=peut_modifier, message=message, erreur=erreur, pseudo_joueur=pseudo_joueur)
        return page_html("Historique", corps, connecte(), compte.get("role"))

    # ---------- Admin : blâmes (Osiris) — instructeur et plus, scope serveur ----------

    def _executer_async_blame(coro):
        """Exécute une coroutine du bot (envoi d'avertissement/procès Discord)
        depuis une route Flask synchrone, sur la boucle asyncio du bot."""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
            future.result(timeout=10)
        except Exception as e:
            print(f"[BLÂMES SITE] Erreur exécution async : {e}")

    def _resoudre_joueur(g, texte):
        """Accepte soit un ID Discord, soit un pseudo/pseudo serveur/nom
        d'utilisateur, et retourne (id_str, erreur). Si plusieurs membres
        correspondent au pseudo saisi, retourne une erreur demandant de
        préciser (ou d'utiliser l'ID)."""
        texte = (texte or "").strip()
        if not texte:
            return None, "Indique un pseudo ou un ID Discord de joueur."
        if texte.isdigit():
            return texte, None
        if not g:
            return None, "Serveur introuvable pour rechercher ce pseudo."
        recherche = texte.lstrip("@").lower()
        exactes = [
            m for m in g.members
            if m.display_name.lower() == recherche
            or m.name.lower() == recherche
            or (getattr(m, "global_name", None) or "").lower() == recherche
        ]
        if len(exactes) == 1:
            return str(exactes[0].id), None
        if len(exactes) > 1:
            return None, f"Plusieurs membres correspondent au pseudo « {texte} », précise-le ou utilise son ID Discord."
        partielles = [
            m for m in g.members
            if recherche in m.display_name.lower() or recherche in m.name.lower()
        ]
        if len(partielles) == 1:
            return str(partielles[0].id), None
        if len(partielles) > 1:
            return None, f"Plusieurs membres correspondent au pseudo « {texte} », précise-le ou utilise son ID Discord."
        return None, f"Aucun membre trouvé avec le pseudo « {texte} »."

    @app.route("/admin/blames/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_blames(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        g = discord.utils.get(bot.guilds, id=guild_id)
        message = None
        erreur = None

        if request.method == "POST":
            joueur_saisi = request.form.get("joueur_id", "").strip()
            raison = request.form.get("raison", "").strip()
            if not joueur_saisi or not raison:
                erreur = "Indique un pseudo (ou ID Discord) de joueur et un motif."
            else:
                joueur_id, erreur_resolution = _resoudre_joueur(g, joueur_saisi)
                if erreur_resolution:
                    erreur = erreur_resolution
                else:
                    nouveau, nb = deps["ajouter_blame"](guild_id, joueur_id, raison, compte.get("discord_id") or "site")
                    deps["sauvegarder_log_disque"](f"⚖️ Blâme ajouté au joueur {joueur_id} depuis le site par {connecte()}.")
                    if g:
                        _executer_async_blame(deps["envoyer_notification_blame"](g, nouveau, nb))
                        _executer_async_blame(deps["traiter_seuils_blame"](g, joueur_id))
                    message = "Blâme ajouté."

        blames = deps["obtenir_blames_actifs"](guild_id)
        par_joueur = {}
        for b in blames:
            par_joueur.setdefault(b["joueur_id"], []).append(b)

        noms = {}
        if g:
            for jid in par_joueur.keys():
                m = g.get_member(int(jid)) if jid.isdigit() else None
                if m:
                    noms[jid] = m.display_name

        corps = render_template_string("""
        <h1>⚖️ Blâmes — Osiris</h1>
        <p class="muted">Serveur {{ g.name if g else guild_id }} — un blâme s'efface automatiquement 2 semaines après son ajout. Avertissement automatique à 2 blâmes, procès au-delà de {{ seuil_proces }}.</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="card">
          <h2 style="margin-top:0">Infliger un blâme</h2>
          <form method="post" class="row">
            <input name="joueur_id" placeholder="Pseudo ou ID Discord du joueur" required>
            <input name="raison" placeholder="Motif du blâme" style="flex:1;min-width:220px" required>
            <button type="submit">Ajouter</button>
          </form>
        </div>

        {% if not par_joueur %}<div class="card">Aucun blâme actif sur ce serveur.</div>{% endif %}
        {% for jid, liste in par_joueur.items() %}
        <div class="card row" style="justify-content:space-between;">
          <div>
            <strong>{{ noms.get(jid, jid) }}</strong>
            <div class="muted">{{ liste|length }} blâme(s) actif(s){% if liste|length > seuil_proces %} — ⚠️ seuil de procès dépassé{% endif %}</div>
          </div>
          <a class="btnlink" href="/admin/blames/{{ guild_id }}/{{ jid }}">Détail</a>
        </div>
        {% endfor %}
        """, guild_id=guild_id, g=g, par_joueur=par_joueur, noms=noms, message=message, erreur=erreur,
             seuil_proces=deps["seuil_proces_blame"])
        return page_html("Blâmes", corps, connecte(), compte.get("role"))

    @app.route("/admin/blames/<int:guild_id>/<joueur_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_blame_detail(guild_id, joueur_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        g = discord.utils.get(bot.guilds, id=guild_id)
        message = None
        erreur = None

        if request.method == "POST":
            action = request.form.get("action")
            if action == "ajouter":
                raison = request.form.get("raison", "").strip()
                if not raison:
                    erreur = "Décris le motif du blâme."
                else:
                    nouveau, nb = deps["ajouter_blame"](guild_id, joueur_id, raison, compte.get("discord_id") or "site")
                    deps["sauvegarder_log_disque"](f"⚖️ Blâme ajouté au joueur {joueur_id} depuis le site par {connecte()}.")
                    if g:
                        _executer_async_blame(deps["envoyer_notification_blame"](g, nouveau, nb))
                        _executer_async_blame(deps["traiter_seuils_blame"](g, joueur_id))
                    message = "Blâme ajouté."
            elif action == "retirer":
                index = int(request.form.get("index", -1))
                retire = deps["retirer_blame_par_index"](guild_id, joueur_id, index)
                if retire:
                    deps["sauvegarder_log_disque"](f"🗑️ Blâme retiré au joueur {joueur_id} depuis le site par {connecte()}.")
                    message = "Blâme retiré."
                else:
                    erreur = "Blâme introuvable."

        actifs = deps["obtenir_blames_actifs"](guild_id, joueur_id)
        pseudo_joueur = None
        if g and str(joueur_id).isdigit():
            m = g.get_member(int(joueur_id))
            if m:
                pseudo_joueur = m.display_name

        corps = render_template_string("""
        {% if pseudo_joueur %}
        <h1 style="margin-bottom:2px;">{{ pseudo_joueur }}</h1>
        <p class="muted" style="font-size:11px;margin-top:0;">ID : {{ joueur_id }}</p>
        {% else %}
        <h1>Blâmes — Joueur {{ joueur_id }}</h1>
        {% endif %}
        <p class="muted">Serveur {{ guild_id }} — {{ actifs|length }} blâme(s) actif(s){% if actifs|length > seuil_proces %} — ⚠️ seuil de procès dépassé{% endif %}</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="card">
          <h2 style="margin-top:0">Ajouter un blâme</h2>
          <form method="post" class="row">
            <input type="hidden" name="action" value="ajouter">
            <input name="raison" placeholder="Motif du blâme" style="flex:1;min-width:220px" required>
            <button type="submit">Ajouter</button>
          </form>
        </div>

        <table>
          <tr><th>Date</th><th>Motif</th><th></th></tr>
          {% for b in actifs %}
          <tr>
            <td>{{ b.date }}</td>
            <td>{{ b.raison }}</td>
            <td>
              <form method="post" class="inline" onsubmit="return confirm('Retirer ce blâme ?')">
                <input type="hidden" name="action" value="retirer">
                <input type="hidden" name="index" value="{{ loop.index0 }}">
                <button class="danger" type="submit">Retirer</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </table>
        """, actifs=actifs, joueur_id=joueur_id, guild_id=guild_id, message=message, erreur=erreur,
             pseudo_joueur=pseudo_joueur, seuil_proces=deps["seuil_proces_blame"])
        return page_html("Blâme — Détail", corps, connecte(), compte.get("role"))

    # ---------- Admin : comptes du site (instructeur et plus) ----------

    def _envoyer_code_confirmation(acteur, description, donnees_action):
        """Génère un code de confirmation à 6 chiffres pour une action
        sensible (suppression de compte, changement de rôle) et l'envoie
        par MP Discord à l'acteur — celui qui vient de déclencher l'action,
        pas la cible. Renvoie le token à valider ensuite via l'action
        "confirmer", ou None si l'envoi n'a pas pu avoir lieu (pas de
        compte Discord relié à l'acteur, DM impossible...) — l'appelant
        doit alors refuser l'action plutôt que de l'exécuter sans confirmation."""
        discord_id = acteur.get("discord_id")
        if not discord_id:
            return None
        try:
            discord_id_int = int(discord_id)
        except (TypeError, ValueError):
            return None

        code = f"{secrets.randbelow(1000000):06d}"
        token = secrets.token_urlsafe(16)

        async def _envoyer():
            utilisateur = bot.get_user(discord_id_int) or await bot.fetch_user(discord_id_int)
            await utilisateur.send(
                "🔐 **Confirmation requise — Site Valerius**\n"
                f"{description}\n"
                f"Code de confirmation : `{code}`\n"
                "⚠️ Valable 5 minutes. Si tu n'es pas à l'origine de cette action, "
                "ignore ce message et préviens un propriétaire."
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_envoyer(), bot.loop)
            future.result(timeout=10)
        except Exception:
            return None

        with _VERROU_CONFIRMATIONS:
            for tok in [t for t, info in CONFIRMATIONS_EN_ATTENTE.items() if info["expire"] < datetime.now()]:
                del CONFIRMATIONS_EN_ATTENTE[tok]

            CONFIRMATIONS_EN_ATTENTE[token] = {
                "code": code,
                "login_acteur": connecte(),
                "expire": datetime.now() + DUREE_VALIDITE_CONFIRMATION,
                "description": description,
                "donnees_action": donnees_action,
            }
        return token

    @app.route("/admin/comptes", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_comptes():
        message = None
        erreur = None
        mot_de_passe_genere = None
        confirmation_en_attente = None
        acteur = compte_connecte()
        acteur_role = acteur.get("role")
        # Seul un Propriétaire a le pouvoir total : attribuer n'importe quel
        # rôle (y compris Propriétaire) et choisir n'importe quel serveur.
        # Un instructeur reste cantonné à son propre serveur, et ne peut
        # attribuer que des rôles strictement inférieurs au sien.
        acteur_super = acteur_role == "proprietaire"

        def peut_gerer(role_cible):
            return acteur_super or niveau_role(role_cible) < niveau_role(acteur_role)

        def peut_attribuer(role_demande):
            return acteur_super or niveau_role(role_demande) < niveau_role(acteur_role)

        def _executer_suppression(login_cible):
            """Exécute réellement la suppression, après confirmation MP."""
            comptes_locaux = charger_comptes()
            if login_cible in comptes_locaux:
                del comptes_locaux[login_cible]
                sauvegarder_comptes(comptes_locaux)
                deps["sauvegarder_log_disque"](f"🗑️ Compte « {login_cible} » supprimé (confirmé par MP) par {connecte()}.")

        def _executer_modification(donnees):
            """Exécute réellement la modification (dont le changement de
            rôle), après confirmation MP. Reprend exactement la logique de
            la branche "modifier" d'origine."""
            comptes_locaux = charger_comptes()
            login_cible = donnees["login"]
            cible = comptes_locaux.get(login_cible)
            if not cible:
                return
            role_avant = cible.get("role")
            nouveau_guild = donnees["nouveau_guild"]
            if not donnees["acteur_super"]:
                nouveau_guild = donnees["acteur_guild"]
            cible["role"] = donnees["nouveau_role"]
            cible["guild_id"] = nouveau_guild or None
            cible["discord_id"] = donnees["nouveau_discord_id"] or None
            if donnees["acteur_super"] and donnees["nouveau_mdp"]:
                _definir_mot_de_passe(cible, donnees["nouveau_mdp"])
                cible["must_change_password"] = False
            if donnees["acteur_super"] and donnees["nouveau_login"] and donnees["nouveau_login"] != login_cible:
                del comptes_locaux[login_cible]
                comptes_locaux[donnees["nouveau_login"]] = cible
                login_cible = donnees["nouveau_login"]
            sauvegarder_comptes(comptes_locaux)
            deps["sauvegarder_log_disque"](
                f"🛡️ Rôle de « {login_cible} » changé : {ROLE_LABELS.get(role_avant, role_avant)} → "
                f"{ROLE_LABELS.get(donnees['nouveau_role'], donnees['nouveau_role'])} (confirmé par MP) par {connecte()}."
            )
            return login_cible

        if request.method == "POST":
            action = request.form.get("action")
            comptes = charger_comptes()

            if action == "creer":
                login = request.form.get("login", "").strip()
                role_demande = request.form.get("role", "malgache")
                discord_id = request.form.get("discord_id", "").strip() or None
                guild_id = request.form.get("guild_id", "").strip() or None
                if not acteur_super:
                    guild_id = acteur.get("guild_id")
                if not login:
                    erreur = "Identifiant requis."
                elif login in comptes:
                    erreur = "Cet identifiant existe déjà."
                elif role_demande not in ROLES_ORDRE or not peut_attribuer(role_demande):
                    erreur = "Tu ne peux pas attribuer ce rôle."
                else:
                    mot_de_passe_genere = _generer_mot_de_passe()
                    comptes[login] = {
                        "role": role_demande,
                        "discord_id": discord_id,
                        "guild_id": guild_id,
                        "must_change_password": True
                    }
                    _definir_mot_de_passe(comptes[login], mot_de_passe_genere)
                    sauvegarder_comptes(comptes)
                    message = f"Compte « {login} » créé."

            elif action == "supprimer":
                # Action sensible : ne s'exécute pas immédiatement, un code
                # de confirmation est d'abord envoyé par MP Discord à l'acteur.
                login = request.form.get("login")
                cible = comptes.get(login)
                if login == COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = "Impossible de supprimer le compte propriétaire."
                elif not cible:
                    erreur = "Compte introuvable."
                elif not peut_gerer(cible.get("role")):
                    erreur = "Tu n'as pas l'autorité pour supprimer ce compte."
                else:
                    token = _envoyer_code_confirmation(
                        acteur, f"Confirmer la suppression du compte « {login} ».",
                        {"type": "supprimer", "login": login}
                    )
                    if not token:
                        erreur = ("Action sensible : la suppression d'un compte nécessite une confirmation "
                                  "par MP Discord, mais ton propre compte n'a pas d'ID Discord relié pour la recevoir. "
                                  "Demande à un Propriétaire de l'ajouter dans « Comptes ».")
                    else:
                        confirmation_en_attente = {"token": token, "description": f"Suppression du compte « {login} »"}
                        message = "Un code de confirmation vient d'être envoyé par MP Discord. Entre-le ci-dessous pour valider l'action."

            elif action == "reinitialiser":
                login = request.form.get("login")
                cible = comptes.get(login)
                if not cible:
                    erreur = "Compte introuvable."
                elif login == COMPTE_PROPRIETAIRE_LOGIN and connecte() != COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = f"Seul le compte {COMPTE_PROPRIETAIRE_LOGIN} peut réinitialiser son propre mot de passe."
                elif not peut_gerer(cible.get("role")):
                    erreur = "Tu n'as pas l'autorité pour réinitialiser ce compte."
                else:
                    mot_de_passe_genere = _generer_mot_de_passe()
                    _definir_mot_de_passe(comptes[login], mot_de_passe_genere)
                    comptes[login]["must_change_password"] = True
                    sauvegarder_comptes(comptes)
                    message = f"Mot de passe de « {login} » réinitialisé."

            elif action == "modifier":
                login = request.form.get("login")
                nouveau_role = request.form.get("role", "")
                nouveau_guild = request.form.get("guild_id", "").strip()
                nouveau_discord_id = request.form.get("discord_id", "").strip()
                nouveau_login = request.form.get("nouveau_login", "").strip()
                nouveau_mdp = request.form.get("nouveau_mdp", "")
                cible = comptes.get(login)
                if not cible:
                    erreur = "Compte introuvable."
                elif login == COMPTE_PROPRIETAIRE_LOGIN and connecte() != COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = f"Seul le compte {COMPTE_PROPRIETAIRE_LOGIN} peut modifier ses propres informations."
                elif login == connecte() and login != COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = "Tu ne peux pas modifier ton propre compte depuis cette page."
                elif not peut_gerer(cible.get("role")):
                    erreur = "Tu n'as pas l'autorité pour modifier ce compte."
                elif nouveau_role not in ROLES_ORDRE or not peut_attribuer(nouveau_role):
                    erreur = "Tu ne peux pas attribuer ce rôle."
                elif login == COMPTE_PROPRIETAIRE_LOGIN and nouveau_role != "proprietaire":
                    erreur = "Le compte propriétaire historique doit toujours rester Propriétaire."
                elif acteur_super and nouveau_login and nouveau_login != login and login == COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = "Impossible de renommer le compte propriétaire historique."
                elif acteur_super and nouveau_login and nouveau_login != login and nouveau_login in comptes:
                    erreur = "Cet identifiant est déjà pris."
                elif acteur_super and nouveau_mdp and len(nouveau_mdp) < LONGUEUR_MIN_MOT_DE_PASSE:
                    erreur = f"Le nouveau mot de passe doit faire au moins {LONGUEUR_MIN_MOT_DE_PASSE} caractères."
                elif nouveau_role != cible.get("role"):
                    # Changement de rôle : action sensible, confirmation par
                    # MP Discord requise avant application (les autres champs
                    # modifiés dans la même soumission sont appliqués en même
                    # temps, une fois le code confirmé).
                    donnees_action = {
                        "type": "modifier", "login": login, "nouveau_role": nouveau_role,
                        "nouveau_guild": nouveau_guild, "nouveau_discord_id": nouveau_discord_id,
                        "nouveau_login": nouveau_login, "nouveau_mdp": nouveau_mdp,
                        "acteur_super": acteur_super, "acteur_guild": acteur.get("guild_id"),
                    }
                    token = _envoyer_code_confirmation(
                        acteur,
                        f"Confirmer le changement de rôle de « {login} » : "
                        f"{ROLE_LABELS.get(cible.get('role'))} → {ROLE_LABELS.get(nouveau_role)}.",
                        donnees_action
                    )
                    if not token:
                        erreur = ("Action sensible : un changement de rôle nécessite une confirmation "
                                  "par MP Discord, mais ton propre compte n'a pas d'ID Discord relié pour la recevoir. "
                                  "Demande à un Propriétaire de l'ajouter dans « Comptes ».")
                    else:
                        confirmation_en_attente = {"token": token, "description": f"Changement de rôle de « {login} »"}
                        message = "Un code de confirmation vient d'être envoyé par MP Discord. Entre-le ci-dessous pour valider l'action."
                else:
                    if not acteur_super:
                        nouveau_guild = acteur.get("guild_id")
                    cible["role"] = nouveau_role
                    cible["guild_id"] = nouveau_guild or None
                    cible["discord_id"] = nouveau_discord_id or None
                    if acteur_super and nouveau_mdp:
                        _definir_mot_de_passe(cible, nouveau_mdp)
                        cible["must_change_password"] = False
                    if acteur_super and nouveau_login and nouveau_login != login:
                        del comptes[login]
                        comptes[nouveau_login] = cible
                        login = nouveau_login
                    sauvegarder_comptes(comptes)
                    message = f"Compte « {login} » mis à jour."

            elif action == "confirmer":
                # Validation du code de confirmation reçu par MP Discord
                # pour une suppression de compte ou un changement de rôle.
                token = request.form.get("token", "")
                code_saisi = request.form.get("code", "").strip()
                donnees_a_executer = None
                with _VERROU_CONFIRMATIONS:
                    info = CONFIRMATIONS_EN_ATTENTE.get(token)
                    if not info or info["login_acteur"] != connecte():
                        erreur = "Confirmation introuvable ou déjà utilisée. Relance l'action."
                    elif datetime.now() > info["expire"]:
                        del CONFIRMATIONS_EN_ATTENTE[token]
                        erreur = "Le code de confirmation a expiré. Relance l'action."
                    elif code_saisi != info["code"]:
                        erreur = "Code incorrect."
                    else:
                        donnees_a_executer = info["donnees_action"]
                        del CONFIRMATIONS_EN_ATTENTE[token]
                if donnees_a_executer is not None:
                    if donnees_a_executer["type"] == "supprimer":
                        _executer_suppression(donnees_a_executer["login"])
                        message = f"Compte « {donnees_a_executer['login']} » supprimé."
                    elif donnees_a_executer["type"] == "modifier":
                        login_final = _executer_modification(donnees_a_executer)
                        message = f"Compte « {login_final or donnees_a_executer['login']} » mis à jour."

        comptes = charger_comptes()
        if not acteur_super:
            comptes = {l: c for l, c in comptes.items() if str(c.get("guild_id")) == str(acteur.get("guild_id"))}

        guilds = list(bot.guilds)
        noms_guildes = {str(g.id): g.name for g in guilds}
        roles_attribuables = [r for r in ROLES_ORDRE if peut_attribuer(r)]

        lignes_comptes = []
        for login, c in comptes.items():
            nom_serveur = noms_guildes.get(str(c.get("guild_id"))) if c.get("guild_id") else None
            lignes_comptes.append((login, c, nom_serveur))

        corps = render_template_string("""
        <h1>Comptes du site</h1>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
        {% if mot_de_passe %}<div class="flash ok">Mot de passe temporaire (note-le, il ne sera plus jamais affiché) : <strong>{{ mot_de_passe }}</strong></div>{% endif %}

        {% if confirmation_en_attente %}
        <div class="card">
          <h2 style="margin-top:0">🔐 Confirmation requise</h2>
          <p class="muted">{{ confirmation_en_attente.description }} — un code à 6 chiffres vient d'être envoyé par MP Discord, valable 5 minutes.</p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="confirmer">
            <input type="hidden" name="token" value="{{ confirmation_en_attente.token }}">
            <input name="code" placeholder="Code à 6 chiffres" maxlength="6" required style="flex:1;min-width:160px">
            <button type="submit">Confirmer l'action</button>
          </form>
        </div>
        {% endif %}

        <div class="card">
          <h2 style="margin-top:0">Créer un compte</h2>
          <form method="post" class="row">
            <input type="hidden" name="action" value="creer">
            <input name="login" placeholder="Identifiant" required>
            <select name="role">
              {% for r in roles_attribuables %}
              <option value="{{ r }}">{{ role_labels[r] }}</option>
              {% endfor %}
            </select>
            <input name="discord_id" placeholder="ID Discord (optionnel)">
            {% if acteur_super %}
            <select name="guild_id">
              <option value="">— Aucun serveur —</option>
              {% for g in guilds %}
              <option value="{{ g.id }}">{{ g.name }}</option>
              {% endfor %}
            </select>
            {% else %}
            <span class="muted">Serveur : {{ nom_serveur_acteur or acteur_guild or '—' }}</span>
            {% endif %}
            <button type="submit">Créer</button>
          </form>
          <p class="muted">Le mot de passe temporaire s'affiche une seule fois après la création — il devra être changé à la première connexion.</p>
        </div>

        {% if acteur_super %}<p class="muted">En tant que Propriétaire, tu peux tout modifier sur un compte (identifiant, mot de passe, rôle, ID Discord, serveur) directement depuis la colonne « Modifier ». Laisse un champ vide pour ne pas le changer.</p>{% endif %}
        <table>
          <tr><th>Identifiant</th><th>Rôle</th><th>Discord ID</th><th>Serveur</th><th>Modifier</th><th>Actions</th></tr>
          {% for login, c, nom_serveur in lignes_comptes %}
          <tr>
            <td>{{ login }}</td>
            <td><span class="badge {{ c.role }}">{{ role_labels.get(c.role, c.role) }}</span></td>
            <td>{{ c.discord_id or '—' }}</td>
            <td>{{ nom_serveur or '—' }}</td>
            <td>
              {% if (login == proprietaire and connecte_login == proprietaire) or (login != connecte_login and login != proprietaire and peut_gerer(c.role)) %}
              <form method="post" class="row inline">
                <input type="hidden" name="action" value="modifier">
                <input type="hidden" name="login" value="{{ login }}">
                <select name="role">
                  {% for r in roles_attribuables %}
                  <option value="{{ r }}" {% if r == c.role %}selected{% endif %}>{{ role_labels[r] }}</option>
                  {% endfor %}
                </select>
                <input name="discord_id" value="{{ c.discord_id or '' }}" placeholder="ID Discord" style="width:130px">
                {% if acteur_super %}
                <input name="nouveau_login" placeholder="Nouvel identifiant" style="width:140px">
                <input name="nouveau_mdp" placeholder="Nouveau mot de passe" style="width:150px">
                <select name="guild_id">
                  <option value="">— Aucun —</option>
                  {% for g in guilds %}
                  <option value="{{ g.id }}" {% if g.id|string == c.guild_id|string %}selected{% endif %}>{{ g.name }}</option>
                  {% endfor %}
                </select>
                {% endif %}
                <button class="secondary" type="submit">Enregistrer</button>
              </form>
              {% else %}
              <span class="muted">—</span>
              {% endif %}
            </td>
            <td class="row">
              {% if (login == proprietaire and connecte_login == proprietaire) or (login != proprietaire and peut_gerer(c.role)) %}
              <form method="post" class="inline">
                <input type="hidden" name="action" value="reinitialiser">
                <input type="hidden" name="login" value="{{ login }}">
                <button class="secondary" type="submit">Réinit. mdp</button>
              </form>
              {% endif %}
              {% if login != proprietaire and peut_gerer(c.role) %}
              <form method="post" class="inline" onsubmit="return confirm('Supprimer ce compte ?')">
                <input type="hidden" name="action" value="supprimer">
                <input type="hidden" name="login" value="{{ login }}">
                <button class="danger" type="submit">Suppr.</button>
              </form>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </table>
        """, lignes_comptes=lignes_comptes, message=message, erreur=erreur, mot_de_passe=mot_de_passe_genere,
             proprietaire=COMPTE_PROPRIETAIRE_LOGIN, guilds=guilds, roles_attribuables=roles_attribuables,
             role_labels=ROLE_LABELS, acteur_super=acteur_super, acteur_guild=acteur.get("guild_id"),
             nom_serveur_acteur=noms_guildes.get(str(acteur.get("guild_id"))), connecte_login=connecte(),
             peut_gerer=peut_gerer, confirmation_en_attente=confirmation_en_attente)
        return page_html("Comptes", corps, connecte(), acteur_role)

    # ---------- Admin : Intelligence Royale (IA, gratuite via Groq) ----------

    @app.route("/ia", methods=["GET", "POST"])
    @login_required
    def admin_ia():
        """Permet de poser une question à l'IA du bot directement depuis le
        site, sans passer par Discord. Accessible à TOUT compte connecté,
        quel que soit son rôle (y compris "malgache") — ce n'est plus un
        outil d'Administration. Chaque compte connecté a sa propre mémoire
        de conversation (clé ("site", login)), séparée de celles utilisées
        côté Discord."""
        compte = compte_connecte()
        cle_ia = ("site", connecte())
        reponse = None
        erreur = None

        if request.method == "POST":
            if request.form.get("reset"):
                deps["reinitialiser_historique_ia_cle"](cle_ia)
            else:
                question = request.form.get("question", "").strip()
                if not question:
                    erreur = "Écris une question avant d'envoyer."
                else:
                    try:
                        g_id = compte.get("guild_id")
                        d_id = compte.get("discord_id")
                        future = asyncio.run_coroutine_threadsafe(
                            deps["interroger_ia"](
                                cle_ia, question,
                                guild_id=int(g_id) if g_id else None,
                                joueur_id=int(d_id) if d_id else None,
                            ), bot.loop
                        )
                        reponse, erreur_ia = future.result(timeout=30)
                        if erreur_ia:
                            erreur = erreur_ia
                    except Exception as e:
                        erreur = f"❌ Erreur lors de la requête à l'IA : {e}"

        corps = render_template_string("""
        <h1>🔮 Intelligence Royale de Valerius</h1>
        <p class="muted">Pose une question à l'IA du bot directement depuis le site (mémoire de conversation propre à ton compte, indépendante de Discord).</p>
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
        {% if reponse %}
        <div class="card">
          <strong>🔮 Réponse :</strong>
          <p style="white-space:pre-wrap;">{{ reponse }}</p>
        </div>
        {% endif %}
        <div class="card">
          <form method="post">
            <p>
              <textarea name="question" rows="4" placeholder="Ta question..." required
                style="width:100%;font-family:inherit;font-size:14px;padding:12px 15px;border-radius:10px;border:1px solid var(--border);background:var(--bg-soft);color:var(--text)"></textarea>
            </p>
            <button type="submit">Envoyer</button>
          </form>
          <form method="post" style="margin-top:10px;">
            <input type="hidden" name="reset" value="1">
            <button type="submit" class="secondary">🧹 Réinitialiser la mémoire</button>
          </form>
        </div>
        """, reponse=reponse, erreur=erreur)
        return page_html("Intelligence Royale", corps, connecte(), compte.get("role"))

    # ---------- Admin : sauvegardes (proprietaire uniquement — accès total) ----------

    @app.route("/admin/backup", methods=["GET", "POST"])
    @role_required("proprietaire")
    def admin_backup():
        message = None
        erreur = None
        if request.method == "POST":
            fichier = request.files.get("fichier")
            if fichier:
                try:
                    donnees = json.loads(fichier.read().decode("utf-8"))
                    nb_restaurees, nb_fichiers = deps["restaurer_donnees_backup"](donnees)
                    message = f"Restauration réussie : {nb_fichiers} fichier(s) et {nb_restaurees} mission(s) en cours réinjectés."
                except Exception as e:
                    erreur = f"Erreur lors de la restauration : {e}"
        corps = render_template_string("""
        <h1>Sauvegardes</h1>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
        <div class="card">
          <h2 style="margin-top:0">Télécharger une sauvegarde complète</h2>
          <a class="btnlink" href="/admin/backup/telecharger">📦 Télécharger maintenant</a>
          <p class="muted">Une sauvegarde automatique est aussi envoyée toutes les 2 heures dans le salon Discord dédié (missions, profils, rangs, rankups, demandes de rang, notifications, blâmes, comptes du site et code d'activation inclus).</p>
        </div>
        <div class="card">
          <h2 style="margin-top:0">Restaurer une sauvegarde</h2>
          <form method="post" enctype="multipart/form-data">
            <input type="file" name="fichier" accept=".json" required>
            <button type="submit" onclick="return confirm('Restaurer va remplacer les données actuelles. Continuer ?')">Restaurer</button>
          </form>
        </div>
        """, message=message, erreur=erreur)
        return page_html("Sauvegardes", corps, connecte(), "proprietaire")

    @app.route("/admin/backup/telecharger")
    @role_required("proprietaire")
    def admin_backup_telecharger():
        buffer, nom_fichier, _taille = deps["generer_backup_complet"]()
        return send_file(buffer, as_attachment=True, download_name=nom_fichier, mimetype="application/json")

    # ---------- Tableau de bord (proprietaire uniquement) ----------

    @app.route("/admin/dashboard")
    @role_required("proprietaire")
    def admin_dashboard():
        guilds = list(bot.guilds)
        nb_membres = sum(g.member_count or 0 for g in guilds)
        missions_actives = deps["missions_actives"]
        nb_missions_actives = sum(len(j) for j in missions_actives.values())
        latence_ms = round(bot.latency * 1000) if bot.latency else 0
        depart = deps["bot_start_time"]
        delta = datetime.now() - depart
        jours, reste = delta.days, delta.seconds
        heures, reste = divmod(reste, 3600)
        minutes = reste // 60
        uptime = f"{jours}j {heures}h {minutes}mn" if jours else f"{heures}h {minutes}mn"

        lignes_serveurs = []
        for g in guilds:
            nb_actives_g = len(missions_actives.get(g.id, {}))
            lignes_serveurs.append((g, nb_actives_g))

        corps = render_template_string("""
        <h1>Tableau de bord</h1>
        <p class="muted">Vue d'ensemble globale, tous serveurs confondus.</p>

        <div class="stats-grid">
          <div class="stat-card"><div class="valeur">{{ guilds|length }}</div><div class="label">Serveurs</div></div>
          <div class="stat-card"><div class="valeur">{{ nb_membres }}</div><div class="label">Membres au total</div></div>
          <div class="stat-card"><div class="valeur">{{ nb_missions_actives }}</div><div class="label">Missions en cours</div></div>
          <div class="stat-card"><div class="valeur">{{ latence_ms }} ms</div><div class="label">Latence Discord</div></div>
          <div class="stat-card"><div class="valeur">{{ uptime }}</div><div class="label">En ligne depuis</div></div>
        </div>

        <h2>Détail par serveur</h2>
        {% for g, nb_actives_g in lignes_serveurs %}
        <div class="card row" style="justify-content:space-between;">
          <div><strong>{{ g.name }}</strong><div class="muted">ID : {{ g.id }} — {{ g.member_count }} membres</div></div>
          <div class="row">
            <span class="pill {{ 'on' if nb_actives_g else 'off' }}">{{ nb_actives_g }} mission(s) en cours</span>
            <a class="btnlink" href="/admin/statistiques/{{ g.id }}">Statistiques</a>
            <a class="btnlink" href="/admin/serveurs">Gérer</a>
          </div>
        </div>
        {% endfor %}
        """, guilds=guilds, nb_membres=nb_membres, nb_missions_actives=nb_missions_actives,
             latence_ms=latence_ms, uptime=uptime, lignes_serveurs=lignes_serveurs)
        return page_html("Tableau de bord", corps, connecte(), "proprietaire")

    # ---------- Logs du bot (proprietaire uniquement) ----------

    @app.route("/admin/logs")
    @role_required("proprietaire")
    def admin_logs():
        logs = deps["charger_logs_recents"](200)
        corps = render_template_string("""
        <h1>Logs du bot</h1>
        <p class="muted">Les {{ logs|length }} derniers événements (les plus récents en premier). Les mêmes logs partent aussi en MP Discord.</p>
        <div class="card" style="padding:0;">
          {% if not logs %}
          <div style="padding:20px;" class="muted">Aucun log pour l'instant.</div>
          {% endif %}
          {% for l in logs %}
          <div class="log-entry">
            <span class="date">{{ l.date }}</span>
            <span class="texte">{{ l.texte }}</span>
          </div>
          {% endfor %}
        </div>
        <p class="muted"><a href="/admin/logs">🔄 Rafraîchir</a></p>
        """, logs=logs)
        return page_html("Logs", corps, connecte(), "proprietaire")

    # ---------- Sécurité : code d'activation (proprietaire uniquement) ----------

    @app.route("/admin/securite", methods=["GET", "POST"])
    @role_required("proprietaire")
    def admin_securite():
        message = None
        erreur = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "changer_code":
                nouveau_code = request.form.get("nouveau_code", "").strip()
                if len(nouveau_code) < 6:
                    erreur = "Le nouveau code doit faire au moins 6 caractères."
                else:
                    deps["sauvegarder_code_verrou"](nouveau_code)
                    deps["sauvegarder_log_disque"](f"🔑 Code d'activation changé depuis le site par {connecte()}.")
                    message = "Code d'activation mis à jour avec succès."
            elif action == "verrouiller":
                guild_id = int(request.form.get("guild_id"))
                deps["guildes_deverrouillees"].discard(guild_id)
                nom_guild = next((g.name for g in bot.guilds if g.id == guild_id), guild_id)
                deps["sauvegarder_log_disque"](f"🔒 Serveur **{nom_guild}** reverrouillé depuis le site par {connecte()}.")
                message = "Serveur reverrouillé."
            elif action == "deverrouiller":
                guild_id = int(request.form.get("guild_id"))
                deps["guildes_deverrouillees"].add(guild_id)
                nom_guild = next((g.name for g in bot.guilds if g.id == guild_id), guild_id)
                deps["sauvegarder_log_disque"](f"🔓 Serveur **{nom_guild}** déverrouillé depuis le site par {connecte()}.")
                message = "Serveur déverrouillé."
            elif action == "maintenance_on":
                deps["definir_maintenance"](True)
                deps["sauvegarder_log_disque"](f"🛠️ Mode maintenance ACTIVÉ depuis le site par {connecte()}.")
                annoncer_maintenance(True)
                try:
                    future = asyncio.run_coroutine_threadsafe(deps["sauvegarder_totale_maintenance"](), bot.loop)
                    stat = future.result(timeout=60)
                except Exception as e:
                    stat = {"erreur_fichier": str(e)}
                if stat.get("fichier_local"):
                    details = []
                    details.append("salon Discord ✅" if stat.get("discord") else f"salon Discord ❌ ({stat.get('erreur_discord', 'échec')})")
                    details.append(f"Drive : {stat['drive']} fichier(s) envoyé(s)" if stat.get("drive") else f"Drive ❌ ({stat.get('erreur_drive', 'non configuré ou échec')})")
                    message = "Mode maintenance activé, bot muet pour tout le monde sauf le Propriétaire. Sauvegarde complète effectuée — " + " · ".join(details) + "."
                else:
                    message = f"⚠️ Maintenance activée, mais la sauvegarde automatique a échoué : {stat.get('erreur_fichier', 'erreur inconnue')}."
                deps["sauvegarder_log_disque"](f"🗄️ Sauvegarde automatique avant maintenance : {stat}")
            elif action == "maintenance_off":
                deps["definir_maintenance"](False)
                deps["sauvegarder_log_disque"](f"✅ Mode maintenance DÉSACTIVÉ depuis le site par {connecte()}.")
                annoncer_maintenance(False)
                try:
                    future = asyncio.run_coroutine_threadsafe(deps["restaurer_apres_maintenance"](), bot.loop)
                    stat = future.result(timeout=60)
                except Exception as e:
                    stat = {"erreur_snapshot": str(e)}
                if stat.get("snapshot_applique"):
                    message = (
                        f"Mode maintenance désactivé, le bot répond de nouveau normalement. Réimportation effectuée — "
                        f"{stat.get('drive_restaures', 0)} fichier(s) retéléchargé(s) de Drive, "
                        f"{stat.get('fichiers_restaures', 0)} fichier(s) et {stat.get('missions_restaurees', 0)} mission(s) en cours réinjectés "
                        f"(état d'avant maintenance restauré)."
                    )
                else:
                    message = (
                        "Mode maintenance désactivé, le bot répond de nouveau normalement. "
                        f"⚠️ Réimportation automatique impossible : {stat.get('erreur_snapshot', 'aucun instantané disponible')}."
                    )
                deps["sauvegarder_log_disque"](f"🗄️ Réimportation automatique après maintenance : {stat}")

        code_actuel = deps["charger_code_verrou"]()
        maintenance_active = deps["charger_maintenance"]()
        guilds = list(bot.guilds)
        deverrouillees = deps["guildes_deverrouillees"]
        lignes_serveurs = [(g, g.id in deverrouillees) for g in guilds]

        corps = render_template_string("""
        <h1>Sécurité</h1>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="card row" style="justify-content:space-between;">
          <div>
            <h2 style="margin-top:0">Mode maintenance global</h2>
            <p class="muted">Bloque toutes les commandes sur tous les serveurs (sauf pour le Propriétaire), utile pour une mise à jour en cours. Activer déclenche automatiquement une sauvegarde complète (fichier local + salon Discord + Google Drive) ; désactiver réimporte automatiquement cet instantané (fichiers, images et missions en cours), en écrasant ce qui aurait changé entre-temps.</p>
          </div>
          <div class="row">
            <span class="pill {{ 'off' if maintenance_active else 'on' }}">{{ 'Maintenance active' if maintenance_active else 'Bot en service' }}</span>
            <form method="post" class="inline">
              {% if maintenance_active %}
              <input type="hidden" name="action" value="maintenance_off">
              <button type="submit" onclick="return confirm('Désactiver va réimporter automatiquement l\\'instantané pris à l\\'activation (fichiers, images, missions en cours), en écrasant les changements faits depuis. Continuer ?')">Désactiver la maintenance</button>
              {% else %}
              <input type="hidden" name="action" value="maintenance_on">
              <button class="danger" type="submit" onclick="return confirm('Activer la maintenance va rendre le bot muet sur tous les serveurs, puis déclencher une sauvegarde complète automatique (Discord + Drive). Continuer ?')">Activer la maintenance</button>
              {% endif %}
            </form>
          </div>
        </div>

        <div class="card">
          <h2 style="margin-top:0">Code d'activation</h2>
          <p class="muted">Code actuel : <strong>{{ code_actuel }}</strong></p>
          <form method="post" class="row">
            <input type="hidden" name="action" value="changer_code">
            <input name="nouveau_code" placeholder="Nouveau code (6 caractères min.)" style="flex:1;min-width:220px" required>
            <button type="submit" onclick="return confirm('Changer le code va invalider l\\'ancien sur tous les serveurs verrouillés. Continuer ?')">Changer le code</button>
          </form>
        </div>

        <h2>Verrouillage par serveur</h2>
        {% for g, deverrouille in lignes_serveurs %}
        <div class="card row" style="justify-content:space-between;">
          <div><strong>{{ g.name }}</strong><div class="muted">ID : {{ g.id }}</div></div>
          <div class="row">
            <span class="pill {{ 'on' if deverrouille else 'off' }}">{{ 'Déverrouillé' if deverrouille else 'Verrouillé' }}</span>
            <form method="post" class="inline">
              <input type="hidden" name="guild_id" value="{{ g.id }}">
              {% if deverrouille %}
              <input type="hidden" name="action" value="verrouiller">
              <button class="danger" type="submit">Reverrouiller</button>
              {% else %}
              <input type="hidden" name="action" value="deverrouiller">
              <button class="secondary" type="submit">Déverrouiller</button>
              {% endif %}
            </form>
          </div>
        </div>
        {% endfor %}
        """, message=message, erreur=erreur, code_actuel=code_actuel, lignes_serveurs=lignes_serveurs,
             maintenance_active=maintenance_active)
        return page_html("Sécurité", corps, connecte(), "proprietaire")

    # ---------- Envoyer un message dans un salon (proprietaire uniquement) ----------

    @app.route("/admin/message", methods=["GET", "POST"])
    @role_required("proprietaire")
    def admin_message():
        message = None
        erreur = None
        guilds = list(bot.guilds)
        guild_id_selectionne = request.values.get("guild_id", "")
        salons = []
        if guild_id_selectionne:
            g = discord.utils.get(guilds, id=int(guild_id_selectionne))
            if g:
                salons = [c for c in g.text_channels if c.permissions_for(g.me).send_messages]

        if request.method == "POST":
            channel_id = request.form.get("channel_id", "")
            texte = request.form.get("texte", "").strip()
            if not channel_id or not texte:
                erreur = "Choisis un salon et écris un message."
            else:
                channel = bot.get_channel(int(channel_id))
                if not channel:
                    erreur = "Salon introuvable (le bot n'a peut-être plus accès à ce salon)."
                else:
                    try:
                        future = asyncio.run_coroutine_threadsafe(channel.send(texte), bot.loop)
                        future.result(timeout=10)
                        deps["sauvegarder_log_disque"](f"✉️ Message envoyé depuis le site par {connecte()} dans #{channel.name} ({channel.guild.name}).")
                        message = f"Message envoyé dans #{channel.name} !"
                    except Exception as e:
                        erreur = f"Erreur lors de l'envoi : {e}"

        corps = render_template_string("""
        <h1>Envoyer un message</h1>
        <p class="muted">Envoie un message dans n'importe quel salon texte, sur n'importe quel serveur où le bot est présent.</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="card">
          <form method="get" class="row">
            <select name="guild_id" onchange="this.form.submit()">
              <option value="">— Choisir un serveur —</option>
              {% for g in guilds %}
              <option value="{{ g.id }}" {% if guild_id_selectionne == g.id|string %}selected{% endif %}>{{ g.name }}</option>
              {% endfor %}
            </select>
          </form>

          {% if salons %}
          <form method="post" style="margin-top:16px;">
            <input type="hidden" name="guild_id" value="{{ guild_id_selectionne }}">
            <p>
              <select name="channel_id" required style="width:100%">
                <option value="" disabled selected>Choisis un salon</option>
                {% for c in salons %}
                <option value="{{ c.id }}">#{{ c.name }}</option>
                {% endfor %}
              </select>
            </p>
            <p><textarea name="texte" rows="5" placeholder="Ton message..." required style="width:100%;font-family:inherit;font-size:14px;padding:12px 15px;border-radius:10px;border:1px solid var(--border);background:var(--bg-soft);color:var(--text)"></textarea></p>
            <button type="submit">Envoyer</button>
          </form>
          {% elif guild_id_selectionne %}
          <p class="muted" style="margin-top:16px;">Aucun salon accessible trouvé sur ce serveur.</p>
          {% endif %}
        </div>
        """, guilds=guilds, salons=salons, guild_id_selectionne=guild_id_selectionne, message=message, erreur=erreur)
        return page_html("Message", corps, connecte(), "proprietaire")

    # ---------- Recherche d'un joueur cross-serveurs (proprietaire uniquement) ----------

    @app.route("/admin/recherche-joueur", methods=["GET", "POST"])
    @role_required("proprietaire")
    def admin_recherche_joueur():
        resultats = []
        joueur_id = ""
        if request.method == "POST":
            joueur_id = request.form.get("joueur_id", "").strip()
            if joueur_id:
                for g in bot.guilds:
                    profils = deps["charger_profils"](g.id)
                    profil = profils.get(joueur_id)
                    if profil:
                        membre = g.get_member(int(joueur_id)) if joueur_id.isdigit() else None
                        resultats.append({
                            "guild": g, "profil": profil,
                            "nom": membre.display_name if membre else None
                        })

        corps = render_template_string("""
        <h1>Rechercher un joueur</h1>
        <p class="muted">Retrouve le profil d'un joueur (son ID Discord) sur tous les serveurs où il a un historique.</p>
        <div class="card">
          <form method="post" class="row">
            <input name="joueur_id" placeholder="ID Discord du joueur" value="{{ joueur_id }}" style="flex:1;min-width:220px" required>
            <button type="submit">Rechercher</button>
          </form>
        </div>
        {% if joueur_id and not resultats %}
        <div class="card muted">Aucun profil trouvé pour cet ID sur aucun serveur.</div>
        {% endif %}
        {% for r in resultats %}
        <div class="card row" style="justify-content:space-between;">
          <div>
            {% if r.nom %}
            <strong style="font-size:15px;">{{ r.nom }}</strong><div class="muted" style="font-size:11px;">{{ joueur_id }}</div>
            {% else %}
            <strong>{{ joueur_id }}</strong>
            {% endif %}
            — {{ r.guild.name }}
            <div class="muted">{{ r.profil.total_reussies }} réussie(s) — {{ r.profil.total_echouees }} échouée(s)</div>
          </div>
          <a class="btnlink" href="/admin/profils/{{ r.guild.id }}/{{ joueur_id }}">Voir l'historique</a>
        </div>
        {% endfor %}
        """, resultats=resultats, joueur_id=joueur_id)
        return page_html("Rechercher un joueur", corps, connecte(), "proprietaire")

    # ---------- Utilisateur (malgache) : son profil uniquement ----------

    @app.route("/mon-profil")
    @role_required("malgache")
    def mon_profil():
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs"))
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        profil = None
        rang_actuel = None
        if discord_id and guild_id:
            profils = deps["charger_profils"](int(guild_id))
            profil = profils.get(str(discord_id))
            rang_actuel = deps["obtenir_rang_joueur"](int(guild_id), discord_id)
        taux_reussite = None
        if profil:
            total = profil.get("total_reussies", 0) + profil.get("total_echouees", 0)
            if total:
                taux_reussite = round(profil.get("total_reussies", 0) / total * 100, 1)
        historique_connexions = compte.get("historique_connexions", [])
        corps = render_template_string("""
        <h1>Mon profil</h1>

        <div class="fiche-personnage">
          <div class="fiche-blason">
            <span>{{ rang_actuel.icone if rang_actuel else "🧭" }}</span>
          </div>
          <div class="fiche-identite">
            <div class="fiche-nom">{{ connecte }}</div>
            <div class="fiche-rang">{{ rang_actuel.nom if rang_actuel else "Aucun rang attribué" }}</div>
          </div>
          {% if taux_reussite is not none %}
          <div class="anneau-progression anneau-petit" data-cible="{{ taux_reussite }}" style="--valeur:0;">
            <svg viewBox="0 0 120 120">
              <circle class="anneau-fond" cx="60" cy="60" r="52"></circle>
              <circle class="anneau-avant" cx="60" cy="60" r="52"></circle>
            </svg>
            <div class="anneau-texte"><span class="anneau-chiffre">0</span>%</div>
          </div>
          {% endif %}
        </div>

        {% if not profil %}
          <div class="card">Aucun historique trouvé pour l'instant. Demande à un administrateur de vérifier que ton compte est bien relié à ton identifiant Discord et à ton serveur.</div>
        {% else %}
          <div class="stats-grid">
            <div class="stat-card"><div class="valeur">{{ profil.total_reussies }}</div><div class="label">Missions réussies</div></div>
            <div class="stat-card"><div class="valeur">{{ profil.total_echouees }}</div><div class="label">Missions échouées</div></div>
            <div class="stat-card"><div class="valeur">{{ profil.total_points or 0 }}</div><div class="label">🏅 Points</div></div>
          </div>
          <div class="card"><a class="btnlink" href="/boutique">🛒 Aller à la boutique</a></div>
          {% if profil.achats %}
          <h2>Mes achats</h2>
          <table>
            <tr><th>Date</th><th>Produit</th><th>Coût</th></tr>
            {% for a in profil.achats %}
            <tr><td>{{ a.date }}</td><td>{{ a.nom }}</td><td>{{ a.cout }} pts</td></tr>
            {% endfor %}
          </table>
          {% endif %}
          <h2>Historique</h2>
          <table>
            <tr><th>Date</th><th>Catégorie</th><th>Mission</th><th>Statut</th></tr>
            {% for h in profil.historique %}
            <tr>
              <td>{{ h.date }}</td>
              <td>{{ h.categorie }}</td>
              <td>{{ h.texte }}</td>
              <td>{{ h.statut }}</td>
            </tr>
            {% endfor %}
          </table>
        {% endif %}

        <h2>Historique des connexions</h2>
        <div class="card">
          <p class="muted">Repère ici toute connexion suspecte à ton compte. Si tu ne reconnais pas une adresse, change ton mot de passe immédiatement.</p>
          {% if not historique_connexions %}
          <p class="muted">Aucune connexion précédente enregistrée.</p>
          {% else %}
          <table>
            <tr><th>Date</th><th>Adresse IP</th></tr>
            {% for h in historique_connexions %}
            <tr><td>{{ h.date }}</td><td>{{ h.ip }}</td></tr>
            {% endfor %}
          </table>
          {% endif %}
        </div>

        <script>
        (function() {
          var anneau = document.querySelector(".anneau-progression");
          if (!anneau) return;
          var reduit = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
          var cible = parseFloat(anneau.dataset.cible) || 0;
          var chiffre = anneau.querySelector(".anneau-chiffre");
          if (reduit) { anneau.style.setProperty("--valeur", cible); chiffre.textContent = Math.round(cible); return; }
          var depart = null, duree = 1100;
          function etape(h) {
            if (!depart) depart = h;
            var t = Math.min((h - depart) / duree, 1);
            var v = cible * (1 - Math.pow(1 - t, 3));
            anneau.style.setProperty("--valeur", v);
            chiffre.textContent = Math.round(v);
            if (t < 1) requestAnimationFrame(etape);
          }
          requestAnimationFrame(etape);
        })();
        </script>
        """, profil=profil, historique_connexions=historique_connexions,
             rang_actuel=rang_actuel, taux_reussite=taux_reussite, connecte=connecte())
        return page_html("Mon profil", corps, connecte(), compte.get("role"))

    @app.route("/mon-casier")
    @role_required("malgache")
    def mon_casier():
        """Casier disciplinaire personnel (zone Osiris) : un compte de base
        ('malgache') y voit uniquement ses propres blâmes actifs — en
        lecture seule, aucune action possible depuis cette page."""
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs"))
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        actifs = []
        if discord_id and guild_id:
            actifs = deps["obtenir_blames_actifs"](int(guild_id), discord_id)
        corps = render_template_string("""
        <h1>⚖️ Mon casier — Osiris</h1>
        {% if not guild_id %}
        <div class="card">Ton compte n'est relié à aucun serveur pour l'instant. Demande à un administrateur de vérifier ton profil.</div>
        {% elif not actifs %}
        <div class="card">✅ Aucun blâme actif sur ton compte. Continue comme ça !</div>
        {% else %}
        <div class="card">
          <strong>{{ actifs|length }}</strong> blâme(s) actif(s)
          {% if actifs|length > seuil_proces %} — ⚠️ le seuil de procès ({{ seuil_proces }}) est dépassé{% endif %}
        </div>
        <table>
          <tr><th>Date</th><th>Motif</th></tr>
          {% for b in actifs %}
          <tr><td>{{ b.date }}</td><td>{{ b.raison }}</td></tr>
          {% endfor %}
        </table>
        <p class="muted">Chaque blâme s'efface automatiquement 2 semaines après son ajout. Un avertissement officiel est envoyé automatiquement à partir de 2 blâmes actifs.</p>
        {% endif %}
        """, actifs=actifs, guild_id=guild_id, seuil_proces=deps["seuil_proces_blame"])
        return page_html("Mon casier", corps, connecte(), compte.get("role"))

    @app.route("/mon-catalogue")
    @role_required("malgache")
    def mon_catalogue():
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs"))
        guild_id = compte.get("guild_id")
        structure = deps["charger_missions_fichier"](int(guild_id)) if guild_id else None
        corps = render_template_string("""
        <h1>Catalogue des missions</h1>
        {% if not guild_id %}
          <div class="card">Ton compte n'est relié à aucun serveur pour l'instant. Demande à un instructeur de vérifier ton profil.</div>
        {% else %}
          {% for cat, missions in structure.items() %}
          <h2>{{ cat|capitalize }} ({{ missions|length }})</h2>
          <div class="card">
            {% if not missions %}<p class="muted">Aucune mission disponible.</p>{% endif %}
            {% if missions %}
            <table>
            {% for m in missions %}
              <tr><td>{{ loop.index }}</td><td>{{ m.texte }}</td><td class="muted">Délai : {{ m.delai }}</td></tr>
            {% endfor %}
            </table>
            {% endif %}
          </div>
          {% endfor %}
        {% endif %}
        """, structure=structure, guild_id=guild_id)
        return page_html("Catalogue", corps, connecte(), compte.get("role"))

    # ---------- Boutique (compte de base) ----------

    @app.route("/boutique", methods=["GET", "POST"])
    @role_required("malgache")
    def boutique():
        compte = compte_connecte()
        # Accessible à tout compte connecté (malgache, instructeur,
        # propriétaire) : l'icône 🛒 de la nav mène toujours ici. La
        # configuration des produits (ajout/suppression/prix) reste, elle,
        # réservée au staff dans Administration → /admin/boutique/<guild_id>.
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        message = None
        erreur = None

        if request.method == "POST":
            if not guild_id or not discord_id:
                erreur = "Ton compte doit être relié à un serveur et à Discord pour acheter."
            else:
                produit_id = request.form.get("produit_id", "")
                succes, texte, produit = deps["acheter_produit_boutique"](int(guild_id), discord_id, produit_id)
                if succes:
                    message = f"🎉 {texte} Un instructeur te contactera pour la remise."
                else:
                    erreur = texte

        produits = []
        solde = 0
        if guild_id:
            produits = [p for p in deps["charger_boutique"](int(guild_id)) if p.get("actif", True)]
            if discord_id:
                profils = deps["charger_profils"](int(guild_id))
                solde = profils.get(str(discord_id), {}).get("total_points", 0)

        corps = render_template_string(STYLE_BOUTIQUE + """
        <h1>🛒 Boutique</h1>
        {% if not guild_id %}
        <div class="card">Ton compte n'est relié à aucun serveur pour l'instant. Demande à un instructeur de vérifier ton profil.</div>
        {% elif not discord_id %}
        <div class="card">Ton compte n'est relié à aucun compte Discord pour l'instant — relie-le depuis <a href="/parametres">tes paramètres</a> pour pouvoir acheter.</div>
        {% else %}
        <div class="card">Ton solde : <strong>{{ solde }} pts</strong></div>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        <div class="boutique-grille">
          {% if not produits %}<div class="card muted">Aucun produit disponible pour l'instant.</div>{% endif %}
          {% for p in produits %}
          {% set rupture = p.stock is not none and p.stock <= 0 %}
          {% set pas_assez = solde < p.cout %}
          <div class="card boutique-carte">
            {% if p.image %}<img class="boutique-image" src="/boutique-images/{{ p.image }}" alt="{{ p.nom }}">
            {% elif p.image_url %}<img class="boutique-image" src="{{ p.image_url }}" alt="{{ p.nom }}">{% endif %}
            <h3 style="margin:0;">{{ p.nom }}</h3>
            {% if p.description %}<p class="muted" style="margin:0;">{{ p.description }}</p>{% endif %}
            <p style="margin:0;"><strong>{{ p.cout }} pts</strong>{% if p.stock is not none %} · <span class="muted">Stock : {{ p.stock }}</span>{% endif %}</p>
            <form method="post">
              <input type="hidden" name="produit_id" value="{{ p.id }}">
              <button type="submit" {{ "disabled" if rupture or pas_assez else "" }}>
                {{ "Rupture de stock" if rupture else ("Pas assez de points" if pas_assez else "Acheter") }}
              </button>
            </form>
          </div>
          {% endfor %}
        </div>
        {% endif %}
        """, guild_id=guild_id, discord_id=discord_id, produits=produits, solde=solde, message=message, erreur=erreur)
        return page_html("Boutique", corps, connecte(), compte.get("role"))

    # ---------- Roue (compte de base) ----------
    # Accessible à tout compte connecté, comme la boutique : l'icône 🎡 de
    # la nav mène toujours ici. La configuration des parts (ajout/%/points/
    # image) reste réservée au staff dans Administration → /admin/roue/<guild_id>.
    # Le tirage lui-même est décidé côté bot (deps["jouer_roue"], appelée
    # via /api/roue/tourner) : la page ne fait qu'animer visuellement le
    # résultat renvoyé par le serveur, jamais un tirage côté navigateur.
    #
    # Il y a TROIS roues (moyenne / difficile / royal) : le joueur choisit
    # laquelle activer via les onglets (?type=...), et chaque tirage
    # consomme UN ticket de roue générique (gagné en terminant une mission
    # moyenne, difficile ou royale, ou offert par un instructeur) — le
    # ticket n'est pas lié à une roue précise, il ouvre n'importe laquelle.

    STYLE_ROUE_PUBLIC = STYLE_ROUE + """
    <style>
      .roue-zone { display:flex; flex-direction:column; align-items:center; gap:22px; margin-top:18px; }
      .roue-conteneur { position:relative; width:260px; height:260px; }
      .roue-disque { width:100%; height:100%; border-radius:50%; border:6px solid var(--border); transition:transform 4.5s cubic-bezier(0.12,0.72,0.1,1); }
      .roue-fleche { position:absolute; top:-14px; left:50%; transform:translateX(-50%); font-size:28px; z-index:2; }
      .roue-resultat { min-height:32px; font-size:18px; text-align:center; }
      .roue-legende { display:flex; flex-wrap:wrap; gap:10px 18px; justify-content:center; max-width:480px; }
      .roue-legende-item { display:flex; align-items:center; gap:6px; font-size:13px; }
      .roue-legende-puce { width:12px; height:12px; border-radius:50%; }
      .roue-legende-image { width:18px; height:18px; border-radius:4px; object-fit:cover; }
    </style>
    """

    @app.route("/roue", methods=["GET"])
    @role_required("malgache")
    def roue_page():
        compte = compte_connecte()
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")

        type_roue = request.args.get("type", "moyenne")
        if type_roue not in TYPES_ROUE:
            type_roue = "moyenne"

        solde = 0
        tickets = 0
        parts_completes = []
        if guild_id:
            parts_completes = deps["charger_roue"](int(guild_id), type_roue)
            if discord_id:
                profils = deps["charger_profils"](int(guild_id))
                solde = profils.get(str(discord_id), {}).get("total_points", 0)
                tickets = deps["obtenir_tickets_roue"](int(guild_id), discord_id)
        # Couleur = position dans la liste COMPLÈTE (comme _degrade_roue),
        # pour que la couleur d'une part reste identique entre cette page et
        # la page d'administration, même si des parts inactives existent.
        actives_indexees = [(i, p) for i, p in enumerate(parts_completes) if p.get("actif", True) and p["pourcentage"] > 0]
        parts = [p for _, p in actives_indexees]
        couleurs_actives = [_couleur_part_roue(i) for i, _ in actives_indexees]
        degrade = _degrade_roue(parts_completes)
        parts_js = [{"id": p["id"], "nom": p["nom"], "pourcentage": p["pourcentage"]} for p in parts]
        onglets_html = _onglets_roue_html(type_roue, "/roue")

        corps = render_template_string(STYLE_ROUE_PUBLIC + """
        <h1>🎡 """ + NOMS_TYPES_ROUE[type_roue] + """</h1>
        """ + onglets_html + """
        {% if not guild_id %}
        <div class="card">Ton compte n'est relié à aucun serveur pour l'instant. Demande à un instructeur de vérifier ton profil.</div>
        {% elif not discord_id %}
        <div class="card">Ton compte n'est relié à aucun compte Discord pour l'instant — relie-le depuis <a href="/parametres">tes paramètres</a> pour pouvoir jouer.</div>
        {% elif not parts %}
        <div class="card muted">Cette roue n'a aucune part active pour l'instant. Reviens plus tard, ou essaie une autre roue !</div>
        {% else %}
        <div class="card row" style="justify-content:space-between;">
          <div>Ton solde : <strong id="roue-solde">{{ solde }} pts</strong></div>
          <div>🎟️ Tickets de roue : <strong id="roue-tickets">{{ tickets }}</strong></div>
        </div>
        {% if tickets <= 0 %}
        <div class="card muted">Tu n'as aucun ticket de roue pour l'instant. Termine une mission <strong>moyenne, difficile ou royale</strong> pour en gagner un !</div>
        {% endif %}
        <div class="roue-zone">
          <div class="roue-conteneur">
            <div class="roue-fleche">🔻</div>
            <div class="roue-disque" id="roue-disque" style="background:{{ degrade }};"></div>
          </div>
          <button id="roue-bouton" type="button" {{ "disabled" if tickets <= 0 else "" }}>Tourner la roue (1 🎟️)</button>
          <div class="roue-resultat" id="roue-resultat"></div>
          <div class="roue-legende">
            {% for p in parts %}
            <div class="roue-legende-item">
              <span class="roue-legende-puce" style="background:{{ couleurs[loop.index0] }};"></span>
              {% if p.image %}<img class="roue-legende-image" src="{{ p.image }}" alt="">{% endif %}
              {{ p.nom }} ({{ p.pourcentage }}%)
            </div>
            {% endfor %}
          </div>
        </div>
        <script>
        (function(){
          var parts = {{ parts_js|tojson }};
          var typeRoue = {{ type_roue|tojson }};
          var disque = document.getElementById('roue-disque');
          var bouton = document.getElementById('roue-bouton');
          var resultat = document.getElementById('roue-resultat');
          var solde = document.getElementById('roue-solde');
          var tickets = document.getElementById('roue-tickets');
          var rotationActuelle = 0;

          function angleMilieuPart(id) {
            // Reconstitue la position angulaire (en degrés, 0° = haut,
            // sens horaire) du MILIEU de la part `id` dans la roue affichée
            // (même ordre que le conic-gradient côté serveur : cumul des
            // pourcentages des parts actives, dans l'ordre reçu).
            var curseurPct = 0;
            for (var i = 0; i < parts.length; i++) {
              if (parts[i].id === id) {
                return (curseurPct + parts[i].pourcentage / 2) / 100 * 360;
              }
              curseurPct += parts[i].pourcentage;
            }
            return 0;
          }

          bouton.addEventListener('click', function(){
            bouton.disabled = true;
            resultat.textContent = '';
            fetch('/api/roue/tourner', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({type: typeRoue})
            })
              .then(function(r){ return r.json(); })
              .then(function(data){
                if (data.erreur) {
                  resultat.textContent = '❌ ' + data.erreur;
                  bouton.disabled = (parseInt(tickets.textContent, 10) <= 0);
                  return;
                }
                if (tickets) { tickets.textContent = Math.max(0, parseInt(tickets.textContent, 10) - 1); }
                var milieuPartDeg = angleMilieuPart(data.id);
                // La flèche pointe vers le haut (0°) : on tourne la roue pour
                // que le milieu de la part gagnante arrive sous la flèche,
                // plus plusieurs tours complets pour l'effet visuel.
                var toursSupp = 4 + Math.floor(Math.random() * 3);
                var angleCible = toursSupp * 360 + ((360 - milieuPartDeg) % 360);
                rotationActuelle += angleCible;
                disque.style.transform = 'rotate(' + rotationActuelle + 'deg)';
                setTimeout(function(){
                  var texte = '🎯 ' + data.nom;
                  if (data.points) { texte += ' — +' + data.points + ' points !'; }
                  resultat.textContent = texte;
                  if (data.points && solde) {
                    solde.textContent = (parseInt(solde.textContent, 10) + data.points) + ' pts';
                  }
                  bouton.disabled = (tickets && parseInt(tickets.textContent, 10) <= 0);
                }, 4600);
              })
              .catch(function(){
                resultat.textContent = "❌ Une erreur est survenue.";
                bouton.disabled = false;
              });
          });
        })();
        </script>
        {% endif %}
        """, guild_id=guild_id, discord_id=discord_id, parts=parts, solde=solde, tickets=tickets, degrade=degrade,
             couleurs=couleurs_actives, parts_js=parts_js, type_roue=type_roue)
        return page_html("Roue", corps, connecte(), compte.get("role"))

    @app.route("/api/roue/tourner", methods=["POST"])
    @role_required("malgache")
    def api_roue_tourner():
        compte = compte_connecte()
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        if not guild_id or not discord_id:
            return jsonify({"erreur": "Ton compte doit être relié à un serveur et à Discord pour jouer."}), 400
        donnees = request.get_json(silent=True) or {}
        type_roue = donnees.get("type", "moyenne")
        if type_roue not in TYPES_ROUE:
            return jsonify({"erreur": "Cette roue n'existe pas."}), 400
        gagnante, erreur = deps["jouer_roue"](int(guild_id), type_roue, discord_id)
        if erreur:
            return jsonify({"erreur": erreur}), 400
        return jsonify({
            "id": gagnante["id"],
            "nom": gagnante["nom"],
            "points": gagnante.get("points", 0),
            "pourcentage": gagnante["pourcentage"],
            "image": gagnante.get("image", ""),
        })

    # ---------- Notifications (cloche 🔔) ----------
    # Accessibles à TOUT compte connecté, y compris :
    #  - les comptes sans discord_id relié (comptes "site" purs) : on
    #    utilise alors leur identifiant de connexion comme clé de
    #    notification (préfixé "site:" pour ne jamais entrer en collision
    #    avec un vrai ID Discord, qui est toujours numérique) ;
    #  - le(s) compte(s) Propriétaire, même sans guild_id fixe assigné : un
    #    Propriétaire ayant accès à tous les serveurs (voir guild_autorise),
    #    sa cloche agrège les notifications de TOUS les serveurs où le bot
    #    est présent plutôt que d'être limitée à un seul.
    # Pour l'instant alimentées par tout ce qui concerne les missions (fin
    # de mission, succès, échec, demande de validation...) ; conçues pour
    # être réutilisées plus tard par d'autres systèmes (ex: rankup) via la
    # même file de notifications.

    ICONES_CATEGORIE_NOTIF = {
        "mission": "🎯",
        "rankup": "🎖️",
    }

    def _notifs_contexte(compte, login):
        """Renvoie (liste_guild_ids, identifiant) pour la cloche 🔔 d'un
        compte connecté. liste_guild_ids est une liste vide si rien n'est
        consultable pour ce compte (pas de serveur assigné et pas
        Propriétaire) : la cloche reste alors affichée mais à 0, sans
        jamais planter."""
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        identifiant = str(discord_id) if discord_id else f"site:{login}"

        if guild_id:
            return [int(guild_id)], identifiant
        if compte.get("role") == "proprietaire":
            return [g.id for g in bot.guilds], identifiant
        return [], identifiant

    @app.route("/api/notifications/non-lues")
    @login_required
    def api_notifications_non_lues():
        compte = compte_connecte()
        guild_ids, identifiant = _notifs_contexte(compte, connecte())
        if not guild_ids:
            return jsonify({"count": 0})
        return jsonify({"count": deps["compter_notifications_non_lues_multi"](guild_ids, identifiant)})

    @app.route("/api/notifications/flux")
    @login_required
    def api_notifications_flux():
        """Flux temps réel (Server-Sent Events) pour la cloche 🔔 : le
        navigateur garde cette connexion ouverte et reçoit immédiatement le
        nouveau nombre de notifications non lues dès qu'une notification est
        ajoutée côté bot, sans avoir à raffraîchir ni à attendre un sondage."""
        compte = compte_connecte()
        guild_ids, identifiant = _notifs_contexte(compte, connecte())
        if not guild_ids:
            return ("", 204)

        # Un compte Propriétaire peut suivre plusieurs serveurs à la fois :
        # on s'abonne à une clé par serveur, pour être réveillé par une
        # notification survenant sur n'importe lequel d'entre eux.
        cles = [(guild_id, str(identifiant)) for guild_id in guild_ids]
        file_attente = queue.Queue()
        with _VERROU_ABONNES_NOTIFICATIONS:
            for cle in cles:
                _ABONNES_NOTIFICATIONS.setdefault(cle, []).append(file_attente)

        def flux():
            try:
                # Compte initial immédiat, pour que la cloche soit juste dès
                # l'ouverture de la connexion (avant même la première notif).
                yield f"data: {deps['compter_notifications_non_lues_multi'](guild_ids, identifiant)}\n\n"
                while True:
                    try:
                        file_attente.get(timeout=25)
                        yield f"data: {deps['compter_notifications_non_lues_multi'](guild_ids, identifiant)}\n\n"
                    except queue.Empty:
                        # Ping pour garder la connexion ouverte à travers les
                        # proxys/hébergeurs qui coupent les connexions inactives.
                        yield ": ping\n\n"
            finally:
                with _VERROU_ABONNES_NOTIFICATIONS:
                    for cle in cles:
                        liste = _ABONNES_NOTIFICATIONS.get(cle)
                        if liste and file_attente in liste:
                            liste.remove(file_attente)
                            if not liste:
                                _ABONNES_NOTIFICATIONS.pop(cle, None)

        return Response(flux(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    @app.route("/api/maj/non-lues")
    @login_required
    def api_maj_non_lues():
        compte = compte_connecte()
        return jsonify({"count": _compter_maj_non_lues(compte)})

    @app.route("/mises-a-jour")
    @login_required
    def mises_a_jour():
        compte = compte_connecte()
        maj_liste = charger_maj()
        peut_gerer = niveau_role(compte.get("role")) >= niveau_role("instructeur")

        # Visite = marque tout comme lu (le compteur du badge repart à 0).
        comptes = charger_comptes()
        login = connecte()
        if login in comptes:
            comptes[login]["maj_vues_le"] = datetime.now().isoformat()
            sauvegarder_comptes(comptes)

        corps = render_template_string("""
        <h1>🆕 Mises à jour du site</h1>
        <p class="muted">Retrouve ici tout ce qui a changé récemment sur le site.</p>
        {% if not maj_liste %}
        <div class="card muted">Aucune mise à jour publiée pour le moment.</div>
        {% else %}
        <div style="display:flex;flex-direction:column;gap:10px;">
          {% for m in maj_liste %}
          <div class="notif-item lue" style="align-items:flex-start;">
            <span class="notif-icone">🆕</span>
            <div style="flex:1;">
              <div class="notif-texte"><strong>{{ m.titre }}</strong><br>{{ m.texte }}</div>
              <div class="muted" style="margin-top:4px;">{{ m.date }}</div>
            </div>
            {% if peut_gerer %}
            <form method="POST" action="/mises-a-jour/supprimer" class="inline" onsubmit="return confirm('Supprimer cette mise à jour ?');">
              <input type="hidden" name="maj_id" value="{{ m.id }}">
              <button type="submit" class="danger" style="padding:6px 10px;">🗑️</button>
            </form>
            {% endif %}
          </div>
          {% endfor %}
        </div>
        {% endif %}
        {% if peut_gerer %}
        <div class="card" style="margin-top:22px;">
          <h3 style="margin-top:0;">➕ Publier une mise à jour</h3>
          <form method="POST" action="/mises-a-jour/ajouter">
            <input type="text" name="titre" placeholder="Titre" required style="width:100%;margin-bottom:8px;">
            <textarea name="texte" placeholder="Détails de la mise à jour" required style="width:100%;min-height:90px;"></textarea>
            <button type="submit" style="margin-top:10px;">Publier</button>
          </form>
        </div>
        {% endif %}
        """, maj_liste=maj_liste, peut_gerer=peut_gerer)
        return page_html("Mises à jour", corps, connecte(), compte.get("role"))

    @app.route("/mises-a-jour/ajouter", methods=["POST"])
    @role_required("instructeur")
    def mises_a_jour_ajouter():
        titre = (request.form.get("titre") or "").strip()
        texte = (request.form.get("texte") or "").strip()
        if titre and texte:
            maj_liste = charger_maj()
            maintenant = datetime.now()
            maj_liste.insert(0, {
                "id": secrets.token_hex(6),
                "titre": titre,
                "texte": texte,
                "date_iso": maintenant.isoformat(),
                "date": maintenant.strftime("%d/%m/%Y à %H:%M"),
            })
            sauvegarder_maj(maj_liste)
        return redirect("/mises-a-jour")

    @app.route("/mises-a-jour/supprimer", methods=["POST"])
    @role_required("instructeur")
    def mises_a_jour_supprimer():
        maj_id = request.form.get("maj_id")
        maj_liste = [m for m in charger_maj() if m.get("id") != maj_id]
        sauvegarder_maj(maj_liste)
        return redirect("/mises-a-jour")

    @app.route("/notifications")
    @login_required
    def notifications():
        compte = compte_connecte()
        guild_ids, identifiant = _notifs_contexte(compte, connecte())
        notifs = deps["obtenir_notifications_multi"](guild_ids, identifiant) if guild_ids else []
        if guild_ids:
            deps["marquer_notifications_lues_multi"](guild_ids, identifiant)
        corps = render_template_string("""
        <h1>🔔 Notifications</h1>
        {% if not guild_ids %}
        <div class="card">Ton compte n'est relié à aucun profil joueur pour l'instant, donc pas encore de notifications à afficher.</div>
        {% elif not notifs %}
        <div class="card muted">Rien de nouveau pour le moment.</div>
        {% else %}
        <div style="display:flex;flex-direction:column;gap:10px;">
          {% for n in notifs %}
          <div class="notif-item {{ 'lue' if n.lu else 'non-lue' }}">
            <span class="notif-icone">{{ icones.get(n.categorie, "🔔") }}</span>
            <div>
              <div class="notif-texte">{{ n.texte }}</div>
              <div class="muted" style="margin-top:4px;">{{ n.date }}</div>
            </div>
          </div>
          {% endfor %}
        </div>
        {% endif %}
        """, notifs=notifs, guild_ids=guild_ids, icones=ICONES_CATEGORIE_NOTIF)
        return page_html("Notifications", corps, connecte(), compte.get("role"))

    # ================= SYSTÈME DES RANGS — "SIRIUS" =================

    def _parse_lignes(texte):
        return [l.strip() for l in (texte or "").splitlines() if l.strip()]

    # ---------- Catalogue des rangs (Propriétaire uniquement) ----------

    @app.route("/admin/rangs/<int:guild_id>", methods=["GET", "POST"])
    @role_required("proprietaire")
    def admin_rangs(guild_id):
        g = discord.utils.get(bot.guilds, id=guild_id)
        erreur = None
        message = None
        rangs = sorted(deps["charger_rangs"](guild_id), key=lambda r: r["ordre"])

        if request.method == "POST":
            action = request.form.get("action")
            if action == "supprimer":
                rang_id = request.form.get("rang_id")
                rangs = [r for r in rangs if r["id"] != rang_id]
                deps["sauvegarder_rangs"](guild_id, rangs)
                message = "Rang supprimé."
            elif action == "enregistrer":
                rang_id = request.form.get("rang_id", "").strip().lower().replace(" ", "_")
                nom = request.form.get("nom", "").strip()
                if not rang_id or not nom:
                    erreur = "L'identifiant et le nom sont obligatoires."
                else:
                    ancien = next((r for r in rangs if r["id"] == rang_id), None)
                    blames_max_brut = request.form.get("blames_max", "").strip()
                    nouveau = {
                        "id": rang_id,
                        "nom": nom,
                        "icone": request.form.get("icone", "🎖️").strip() or "🎖️",
                        "groupe": request.form.get("groupe") or "Recrue",
                        "ordre": int(request.form.get("ordre") or 0),
                        "unique": request.form.get("unique") == "on",
                        "conditions": {
                            "semaines_min": int(request.form.get("semaines_min") or 0),
                            "blames_max": int(blames_max_brut) if blames_max_brut != "" else None,
                            "missions_min": {
                                cat: int(request.form.get(f"missions_min_{cat}") or 0)
                                for cat in ("commune", "moyenne", "difficile", "royal")
                                if int(request.form.get(f"missions_min_{cat}") or 0) > 0
                            },
                            # Les combinaisons "OU" avancées ne sont pas éditables depuis ce
                            # formulaire simplifié : on préserve celles déjà présentes.
                            "missions_alt": (ancien["conditions"].get("missions_alt", []) if ancien else []),
                            "manuel": _parse_lignes(request.form.get("manuel")),
                        },
                        "debloque": _parse_lignes(request.form.get("debloque")),
                    }
                    rangs = [r for r in rangs if r["id"] != rang_id] + [nouveau]
                    deps["sauvegarder_rangs"](guild_id, rangs)
                    message = f"Rang « {nom} » enregistré."
                    rangs = sorted(deps["charger_rangs"](guild_id), key=lambda r: r["ordre"])

        rangs_vue = []
        for r in rangs:
            r = dict(r)
            r["manuel_texte"] = "\n".join(r.get("conditions", {}).get("manuel", []))
            r["debloque_texte"] = "\n".join(r.get("debloque", []))
            rangs_vue.append(r)

        corps = render_template_string("""
        <h1>📜 Catalogue des rangs</h1>
        <p class="muted">Serveur {{ g.name if g else guild_id }} — réservé aux Propriétaires. Les combinaisons "OU" avancées (ex: 3 missions communes OU 1 moyenne) ne sont pas éditables ici et restent telles quelles.</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        {% for r in rangs_vue %}
        <div class="card">
          <form method="post">
            <div class="row">
              <input name="rang_id" value="{{ r.id }}" readonly style="width:140px;background:var(--panel-2)">
              <input name="icone" value="{{ r.icone }}" style="width:60px;text-align:center">
              <input name="nom" value="{{ r.nom }}" required style="flex:1;min-width:160px">
              <select name="groupe">
                {% for grp in groupes %}<option value="{{ grp }}" {{ "selected" if r.groupe==grp else "" }}>{{ grp }}</option>{% endfor %}
              </select>
              <input name="ordre" type="number" value="{{ r.ordre }}" style="width:70px" title="Ordre (0 = plus bas)">
              <label class="muted" style="display:flex;align-items:center;gap:4px;"><input type="checkbox" name="unique" {{ "checked" if r.unique else "" }}> Unique</label>
            </div>
            <div class="row" style="margin-top:10px;">
              <label class="muted">Semaines mini <input name="semaines_min" type="number" value="{{ r.conditions.semaines_min or 0 }}" style="width:60px"></label>
              <label class="muted">Blâmes max <input name="blames_max" type="number" value="{{ '' if r.conditions.blames_max is none else r.conditions.blames_max }}" style="width:60px" placeholder="aucun"></label>
              {% for cat in ["commune","moyenne","difficile","royal"] %}
              <label class="muted">Min. {{ cat }} <input name="missions_min_{{ cat }}" type="number" value="{{ r.conditions.missions_min.get(cat, 0) }}" style="width:55px"></label>
              {% endfor %}
            </div>
            <p style="margin-top:10px;"><label class="muted">Conditions manuelles (une par ligne)</label><br>
            <textarea name="manuel" rows="3" style="width:100%;font-family:inherit;">{{ r.manuel_texte }}</textarea></p>
            <p><label class="muted">Débloque (une par ligne)</label><br>
            <textarea name="debloque" rows="3" style="width:100%;font-family:inherit;">{{ r.debloque_texte }}</textarea></p>
            <div class="row">
              <button type="submit" name="action" value="enregistrer">💾 Enregistrer</button>
              <button type="submit" name="action" value="supprimer" class="secondary" onclick="return confirm('Supprimer ce rang ?');">🗑️ Supprimer</button>
            </div>
          </form>
        </div>
        {% endfor %}

        <h2>➕ Ajouter un rang</h2>
        <div class="card">
          <form method="post">
            <div class="row">
              <input name="rang_id" placeholder="identifiant unique (ex: baron)" required style="width:180px">
              <input name="icone" placeholder="icône" value="🎖️" style="width:60px;text-align:center">
              <input name="nom" placeholder="Nom affiché" required style="flex:1;min-width:160px">
              <select name="groupe">{% for grp in groupes %}<option value="{{ grp }}">{{ grp }}</option>{% endfor %}</select>
              <input name="ordre" type="number" value="{{ rangs_vue|length }}" style="width:70px">
              <label class="muted" style="display:flex;align-items:center;gap:4px;"><input type="checkbox" name="unique"> Unique</label>
            </div>
            <div class="row" style="margin-top:10px;">
              <label class="muted">Semaines mini <input name="semaines_min" type="number" value="0" style="width:60px"></label>
              <label class="muted">Blâmes max <input name="blames_max" type="number" style="width:60px" placeholder="aucun"></label>
              {% for cat in ["commune","moyenne","difficile","royal"] %}
              <label class="muted">Min. {{ cat }} <input name="missions_min_{{ cat }}" type="number" value="0" style="width:55px"></label>
              {% endfor %}
            </div>
            <p><textarea name="manuel" rows="2" placeholder="Conditions manuelles, une par ligne" style="width:100%;font-family:inherit;"></textarea></p>
            <p><textarea name="debloque" rows="2" placeholder="Débloque, une par ligne" style="width:100%;font-family:inherit;"></textarea></p>
            <button type="submit" name="action" value="enregistrer">➕ Créer ce rang</button>
          </form>
        </div>
        """, rangs_vue=rangs_vue, groupes=deps["groupes_rangs"], g=g, guild_id=guild_id, message=message, erreur=erreur)
        return page_html("Catalogue des rangs", corps, connecte(), compte_connecte().get("role"))

    # ---------- Demandes de rang (instructeur et plus, scope serveur) ----------

    @app.route("/admin/demandes-rang/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_demandes_rang(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        message = None

        if request.method == "POST":
            demande_id = request.form.get("demande_id")
            decision = request.form.get("decision")
            commentaire = request.form.get("commentaire", "").strip() or None
            forcer = request.form.get("forcer") == "1"
            if decision in ("accepte", "refuse"):
                resultat = deps["traiter_demande_rang"](guild_id, demande_id, decision, compte.get("discord_id") or connecte(), commentaire, forcer=forcer)
                if resultat == "conditions_non_remplies":
                    message = ("⚠️ Ce joueur ne remplit pas encore toutes les conditions automatiques de ce rang : "
                                "la promotion a été bloquée. Coche « forcer » et confirme si tu veux le promouvoir quand même.")
                elif not resultat:
                    message = "Cette demande n'existe plus ou a déjà été traitée."
                else:
                    message = "Décision enregistrée."

        demandes_attente = deps["obtenir_demandes_rang"](guild_id, statut="en_attente")
        demandes_traitees = [d for d in deps["obtenir_demandes_rang"](guild_id) if d["statut"] != "en_attente"][:20]

        def _rang_de(demande):
            return deps["obtenir_rang_par_id"](guild_id, demande["rang_id"])

        # On résout les ID Discord (demandeur ET instructeur qui a traité)
        # en pseudos lisibles, quand le serveur est bien en cache du bot.
        g = discord.utils.get(bot.guilds, id=guild_id)

        def _pseudo(discord_id):
            if not discord_id:
                return None
            m = g.get_member(int(discord_id)) if g and str(discord_id).isdigit() else None
            return m.display_name if m else None

        def _pseudo_joueur(demande):
            return _pseudo(demande.get("joueur_id")) or f"‹{demande.get('joueur_id')}›"

        def _pseudo_instructeur(demande):
            return _pseudo(demande.get("traite_par")) or (f"‹{demande.get('traite_par')}›" if demande.get("traite_par") else None)

        corps = render_template_string("""
        <h1>📥 Demandes de rang</h1>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}

        {% if not demandes_attente %}
        <div class="card muted">Aucune demande en attente.</div>
        {% endif %}
        {% for d in demandes_attente %}
        {% set rang = rang_de(d) %}
        <div class="card">
          <div class="row" style="justify-content:space-between;">
            <div><strong>{{ pseudo_joueur(d) }}</strong> souhaite devenir <strong>{{ rang.icone if rang else "" }} {{ rang.nom if rang else d.rang_id }}</strong></div>
            <span class="muted">{{ d.date }}</span>
          </div>
          <p class="notif-texte" style="margin-top:8px;">{{ d.motivation }}</p>
          <h3 style="margin-top:14px;">Vérification automatique</h3>
          <ul>
            {% for c in d.rapport_auto.auto %}
            <li>{{ "✅" if c.ok else "❌" }} {{ c.libelle }} <span class="muted">(constaté : {{ c.valeur_actuelle }})</span></li>
            {% endfor %}
            {% for c in d.rapport_auto.manuel %}
            <li>❔ {{ c.libelle }} <span class="muted">— à vérifier toi-même</span></li>
            {% endfor %}
            {% if not d.rapport_auto.auto and not d.rapport_auto.manuel %}
            <li class="muted">Aucune condition particulière pour ce rang.</li>
            {% endif %}
          </ul>
          <form method="post" class="row" style="margin-top:12px;" onsubmit="return true;">
            <input type="hidden" name="demande_id" value="{{ d.id }}">
            <input type="hidden" name="forcer" value="0">
            <input name="commentaire" placeholder="Commentaire (optionnel)" style="flex:1;min-width:200px;">
            {% if d.rapport_auto.toutes_auto_ok %}
            <button type="submit" name="decision" value="accepte">✅ Accepter</button>
            {% else %}
            <button type="submit" name="decision" value="accepte"
              onclick="if(!confirm('⚠️ Ce joueur ne remplit pas toutes les conditions automatiques. Le promouvoir quand même ?')){return false;} this.form.querySelector('input[name=forcer]').value='1'; return true;">
              ⚠️ Forcer l'acceptation
            </button>
            {% endif %}
            <button type="submit" name="decision" value="refuse" class="secondary">❌ Refuser</button>
          </form>
        </div>
        {% endfor %}

        <h2 style="margin-top:26px;">Historique récent</h2>
        {% if not demandes_traitees %}
        <div class="card muted">Aucune demande traitée pour l'instant.</div>
        {% endif %}
        {% for d in demandes_traitees %}
        {% set rang = rang_de(d) %}
        <div class="card row" style="justify-content:space-between;">
          <div>
            {{ pseudo_joueur(d) }} — {{ rang.nom if rang else d.rang_id }}
            <div class="muted">{{ "✅ Accepté" if d.statut == "accepte" else "❌ Refusé" }} le {{ d.date_traitement }}{% if pseudo_instructeur(d) %} par {{ pseudo_instructeur(d) }}{% endif %}{% if d.commentaire %} — {{ d.commentaire }}{% endif %}</div>
          </div>
        </div>
        {% endfor %}
        """, demandes_attente=demandes_attente, demandes_traitees=demandes_traitees, rang_de=_rang_de,
             pseudo_joueur=_pseudo_joueur, pseudo_instructeur=_pseudo_instructeur, message=message)
        return page_html("Demandes de rang", corps, connecte(), compte.get("role"))

    # ---------- Demande de rang (compte de base) ----------

    @app.route("/demande-rang", methods=["GET", "POST"])
    @role_required("malgache")
    def demande_rang():
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs_rangs"))
        discord_id = compte.get("discord_id")
        guild_id = int(compte["guild_id"]) if compte.get("guild_id") else None
        erreur = None
        message = None
        rangs = []
        rang_actuel = None
        rang_choisi = None
        rapport = None
        mes_demandes = []

        if guild_id:
            rangs = sorted(deps["charger_rangs"](guild_id), key=lambda r: r["ordre"])
            rang_actuel = deps["obtenir_rang_joueur"](guild_id, discord_id)
            mes_demandes = deps["obtenir_demandes_rang"](guild_id, joueur_id=discord_id)[:10]

        demande_en_cours = any(d["statut"] == "en_attente" for d in mes_demandes)
        rang_id_choisi = request.values.get("rang_id")
        if guild_id and rang_id_choisi:
            rang_choisi = deps["obtenir_rang_par_id"](guild_id, rang_id_choisi)
            if rang_choisi:
                g = discord.utils.get(bot.guilds, id=guild_id)
                if g:
                    rapport = deps["verifier_conditions_rang"](g, discord_id, rang_choisi)

        if request.method == "POST" and request.form.get("action") == "soumettre":
            motivation = request.form.get("motivation", "").strip()
            if not guild_id:
                erreur = "Ton compte n'est relié à aucun serveur."
            elif demande_en_cours:
                erreur = "Tu as déjà une demande en attente de traitement."
            elif not rang_choisi:
                erreur = "Choisis d'abord un rang."
            elif len(motivation) < 30:
                erreur = "Ta motivation est trop courte — détaille davantage tes raisons et tes motivations."
            else:
                deps["creer_demande_rang"](guild_id, discord_id, rang_choisi["id"], motivation)
                message = "Ta demande a bien été envoyée aux instructeurs !"
                rang_choisi = None
                rapport = None
                demande_en_cours = True
                mes_demandes = deps["obtenir_demandes_rang"](guild_id, joueur_id=discord_id)[:10]

        corps = render_template_string("""
        <h1>🎖️ Demande de rang</h1>
        {% if not guild_id %}
        <div class="card">Ton compte n'est relié à aucun serveur pour l'instant. Demande à un instructeur de vérifier ton profil.</div>
        {% else %}
        <div class="card">Ton rang actuel : <strong>{{ rang_actuel.icone if rang_actuel else "" }} {{ rang_actuel.nom if rang_actuel else "—" }}</strong></div>

        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        {% if demande_en_cours %}
        <div class="card muted">Tu as déjà une demande en attente de traitement par les instructeurs.</div>
        {% else %}
        <div class="card">
          <label class="muted">Choisis un rang à demander :</label>
          <div class="frise-rangs">
            {% set ns = namespace(prev=None) %}
            {% for r in rangs %}
            {% if ns.prev is not none and ns.prev != r.groupe %}<div class="frise-separateur"></div>{% endif %}
            {% set ns.prev = r.groupe %}
            <a class="frise-item {{ 'actuel' if rang_actuel and rang_actuel.id == r.id else '' }} {{ 'selectionne' if rang_choisi and rang_choisi.id == r.id else '' }}"
               href="/demande-rang?rang_id={{ r.id }}#choix-rang" title="{{ r.nom }}{{ ' (Unique)' if r.unique else '' }}">
              <span class="frise-noeud"><span class="frise-icone">{{ r.icone }}</span></span>
              <span class="frise-label">{{ r.nom }}</span>
              {% if rang_actuel and rang_actuel.id == r.id %}<span class="frise-ici">Toi</span>{% endif %}
            </a>
            {% endfor %}
          </div>
        </div>

        {% if rang_choisi %}
        <div class="card" id="choix-rang">
          <h2>{{ rang_choisi.icone }} {{ rang_choisi.nom }}</h2>
          <h3>Conditions</h3>
          <ul>
            {% for c in rapport.auto %}
            <li>{{ "✅" if c.ok else "❌" }} {{ c.libelle }} <span class="muted">(actuellement : {{ c.valeur_actuelle }})</span></li>
            {% endfor %}
            {% for c in rapport.manuel %}
            <li>❔ {{ c.libelle }} <span class="muted">(vérifié par un instructeur)</span></li>
            {% endfor %}
            {% if not rapport.auto and not rapport.manuel %}
            <li class="muted">Aucune condition particulière.</li>
            {% endif %}
          </ul>
          {% if rang_choisi.debloque %}
          <h3>Débloque</h3>
          <ul>{% for d in rang_choisi.debloque %}<li>{{ d }}</li>{% endfor %}</ul>
          {% endif %}
          <form method="post">
            <input type="hidden" name="rang_id" value="{{ rang_choisi.id }}">
            <input type="hidden" name="action" value="soumettre">
            <p><label class="muted">Ta motivation — détaille pourquoi tu mérites ce rang :</label></p>
            <p><textarea name="motivation" rows="6" required style="width:100%;font-family:inherit;font-size:14px;padding:12px 15px;border-radius:10px;border:1px solid var(--border);background:var(--bg-soft);color:var(--text)"></textarea></p>
            <button type="submit">Envoyer ma demande</button>
          </form>
        </div>
        {% endif %}
        {% endif %}

        {% if mes_demandes %}
        <h2 style="margin-top:26px;">Mes demandes</h2>
        {% for d in mes_demandes %}
        <div class="card muted">
          {{ d.date }} — {{ {"en_attente": "⏳ En attente", "accepte": "✅ Acceptée", "refuse": "❌ Refusée"}.get(d.statut, d.statut) }}
          {% if d.commentaire %} — {{ d.commentaire }}{% endif %}
        </div>
        {% endfor %}
        {% endif %}
        {% endif %}
        """, guild_id=guild_id, rang_actuel=rang_actuel, rangs=rangs, groupes=deps["groupes_rangs"],
             rang_choisi=rang_choisi, rapport=rapport, demande_en_cours=demande_en_cours,
             mes_demandes=mes_demandes, message=message, erreur=erreur)
        return page_html("Demande de rang", corps, connecte(), compte.get("role"))
