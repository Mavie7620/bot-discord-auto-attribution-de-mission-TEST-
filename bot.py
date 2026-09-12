# -*- coding: utf-8 -*-
import discord
from discord import app_commands
from discord.ext import commands, tasks
import random
import os
import re
import json
import asyncio
import contextlib
import threading
from threading import Thread
from flask import Flask
from datetime import datetime, timedelta
import io
import glob
import base64
import site_web
import ia_outils
import stockage_drive
import requests
from groq import AsyncGroq

app = Flask('')

# NOTE : cette route servait de "ping" de survie pour Render/UptimeRobot.
# Elle a été déplacée de "/" vers "/ping" car "/" est maintenant utilisé
# par le vrai site web (site_web.py -> racine()) : les deux routes se
# disputaient la même URL, et comme cette route était enregistrée en
# premier (au chargement du module), c'est toujours elle qui répondait,
# empêchant d'accéder au site (redirection vers /connexion).
@app.route('/ping')
def home(): return "Le bot Valerius est vivant !"

def run_web():
    # threaded=True : indispensable pour que le flux temps réel de la
    # cloche 🔔 (/api/notifications/flux, une connexion HTTP maintenue
    # ouverte) ne bloque pas les autres pages du site pendant qu'il est
    # ouvert. Sans ça, le serveur de dev Flask traite les requêtes une par
    # une et le site semblerait "figé" tant qu'un flux reste connecté.
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), threaded=True)
def keep_alive():
    t = Thread(target=run_web)
    t.start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Second bot, dans le même processus : "Osiris" gère uniquement le système
# disciplinaire (blâmes / avertissements / procès). Il partage tout le reste
# du code avec Valerius (fichiers, permissions, verrou de serveur, logs) —
# seul son token Discord et ses commandes lui sont propres.
# ⚠️ Osiris doit être invité séparément sur chaque serveur, avec l'intent
# "Server Members Intent" activé sur le Discord Developer Portal, tout
# comme Valerius, pour pouvoir mettre en DM et lire les rôles des joueurs.
bot_osiris = commands.Bot(command_prefix="?", intents=intents)

# Troisième bot, toujours dans le même processus : "Sirius" gère uniquement
# le système des rangs du royaume (catalogue des rangs, demandes de
# promotion, vérification automatique des conditions). Comme Osiris, il
# partage tout le reste du code avec Valerius — seul son token et ses
# commandes lui sont propres.
# ⚠️ Sirius doit être créé sur le Discord Developer Portal (comme Valerius
# et Osiris), invité séparément sur le serveur avec l'intent "Server Members
# Intent" activé, et son token placé dans la variable d'environnement Render
# "sirius_id" (même convention que "osiris_id" pour Osiris).
bot_rangs = commands.Bot(command_prefix=">>", intents=intents)

BOT_START_TIME = datetime.now()

PROPRIETAIRE_ID = 1109866808321769472
WELCOME_CHANNEL_ID = 1534604841660190792
ATTENTE_MOOV_ID = 1534604587992875280
SALON_PALAIS_ROYAL_ID = 1519322938430722129
SALON_VALIDATION_MISSION_ID = 1534638388286853273
SALON_ANNONCE_MAINTENANCE_ID = 1517995293944057867
# Salon où Sirius poste les demandes de rang (visible par les Haut-gradés/
# instructeurs). Configurable via la variable d'environnement Render
# "SALON_DEMANDES_RANG_ID" — sur Render : Environment > Add Environment
# Variable, avec l'ID du salon Discord voulu (clic droit sur le salon en
# mode développeur > "Copier l'identifiant"). Si la variable n'est pas
# définie, la valeur ci-dessous sert de valeur par défaut.
SALON_DEMANDES_RANG_ID = int(os.environ.get("SALON_DEMANDES_RANG_ID", 1519322938430722129))

# ================= INTELLIGENCE ROYALE DE VALERIUS (IA — Groq, gratuit) =================
# Utilise l'API Groq (gratuite, https://console.groq.com) pour répondre aux
# questions des joueurs. Anciennement propulsée par Claude (Anthropic, payant) ;
# remplacée par Groq, qui donne accès gratuitement à des modèles open-source
# largement suffisants pour cet usage, avec une API très proche.
# NOTE : "llama-3.3-70b-versatile" a été décommissionné par Groq (juin 2026).
# Groq recommande de migrer vers "openai/gpt-oss-120b" (ou "qwen/qwen3.6-27b"),
# d'où le nouveau modèle par défaut ci-dessous. Voir
# https://console.groq.com/docs/deprecations pour la liste à jour.
# Clé API à définir sur Render (ou en local) via la variable d'environnement
# GROQ_API_KEY. Sans clé, la commande /ia (et la page du site) répondent
# simplement que l'IA n'est pas configurée, sans faire planter le bot.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
IA_MAX_TOKENS = 1024

client_ia = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ================= API NATIONSGLORY (données réelles du jeu) =================
# Jeton personnel récupéré sur https://nationsglory.readme.io (bouton "Log In"
# / "Get API Key"), à placer dans la variable d'environnement Render
# NATIONSGLORY_API_KEY. Sans clé, l'outil ci-dessous renvoie juste une erreur
# exploitable par l'IA ("je n'ai pas pu vérifier"), sans jamais faire planter
# le bot ni inventer de chiffres.
NATIONSGLORY_API_KEY = os.environ.get("NATIONSGLORY_API_KEY")
NATIONSGLORY_API_BASE = "https://publicapi.nationsglory.fr"


def _outil_joueurs_en_ligne_nationsglory(contexte, serveur=None):
    """Interroge GET /playercount sur l'API officielle NationsGlory et
    renvoie le nombre de joueurs en ligne / la capacité max, pour le
    serveur demandé (par défaut "mocha", notre serveur) ou pour tous les
    serveurs si `serveur="tous"`. `contexte` n'est pas utilisé ici (donnée
    publique, indépendante du serveur Discord) mais reste accepté pour
    respecter la signature commune à tous les outils de ia_outils.py."""
    if not NATIONSGLORY_API_KEY:
        return {"erreur": "Aucune clé API NationsGlory configurée (variable NATIONSGLORY_API_KEY manquante)."}
    try:
        reponse = requests.get(
            f"{NATIONSGLORY_API_BASE}/playercount",
            headers={"Authorization": f"Bearer {NATIONSGLORY_API_KEY}"},
            timeout=10,
        )
        reponse.raise_for_status()
        donnees = reponse.json()
    except Exception as e:
        return {"erreur": f"Impossible de contacter l'API NationsGlory : {e}"}

    serveur = (serveur or "mocha").strip().lower()
    if serveur in ("tous", "all", ""):
        return {"joueurs_par_serveur": donnees}
    infos = donnees.get(serveur)
    if not infos:
        return {"erreur": f"Serveur « {serveur} » inconnu de l'API NationsGlory."}
    return {"serveur": serveur, "joueurs_en_ligne": infos.get("players"), "capacite_max": infos.get("maxplayers")}


ia_outils.enregistrer_outil(
    nom="joueurs_en_ligne_nationsglory",
    description=(
        "Renvoie le nombre de joueurs actuellement en ligne (et la capacité "
        "maximale) sur un serveur NationsGlory. À utiliser dès qu'on demande "
        "combien de joueurs sont connectés sur le Mocha (ou un autre serveur "
        "NationsGlory). Sans argument, répond pour le serveur Mocha."
    ),
    parametres={
        "type": "object",
        "properties": {
            "serveur": {
                "type": "string",
                "description": (
                    "Nom du serveur NationsGlory en minuscules (ex: 'mocha', 'blue', "
                    "'orange'), ou 'tous' pour la liste complète. Par défaut : 'mocha'."
                ),
            }
        },
        "required": [],
    },
    executer=_outil_joueurs_en_ligne_nationsglory,
)


def _requete_nationsglory(chemin):
    """Petite aide interne partagée par tous les outils NationsGlory :
    fait un GET authentifié sur `chemin` (ex: "/user/MisterSand") et
    renvoie un tuple (donnees, erreur) où un seul des deux est rempli.
    Ne lève jamais d'exception : toute erreur réseau/HTTP est renvoyée
    sous forme de message lisible, jamais une donnée inventée."""
    if not NATIONSGLORY_API_KEY:
        return None, "Aucune clé API NationsGlory configurée (variable NATIONSGLORY_API_KEY manquante)."
    try:
        reponse = requests.get(
            f"{NATIONSGLORY_API_BASE}{chemin}",
            headers={"Authorization": f"Bearer {NATIONSGLORY_API_KEY}"},
            timeout=10,
        )
    except Exception as e:
        return None, f"Impossible de contacter l'API NationsGlory : {e}"
    if reponse.status_code == 400:
        return None, "Introuvable sur NationsGlory (pseudo, pays ou serveur inconnu)."
    try:
        reponse.raise_for_status()
    except Exception as e:
        return None, f"Erreur API NationsGlory ({reponse.status_code}) : {e}"
    try:
        return reponse.json(), None
    except Exception as e:
        return None, f"Réponse invalide de l'API NationsGlory : {e}"


def _formater_duree_secondes(secondes):
    """Convertit une durée en secondes (playtime brut de l'API) en texte
    lisible du style "36j 2h 15min". Renvoie None si la valeur est absente
    ou invalide, pour laisser l'appelant décider de l'affichage."""
    try:
        secondes = int(secondes)
    except (TypeError, ValueError):
        return None
    jours, reste = divmod(secondes, 86400)
    heures, reste = divmod(reste, 3600)
    minutes = reste // 60
    morceaux = []
    if jours:
        morceaux.append(f"{jours}j")
    if heures or jours:
        morceaux.append(f"{heures}h")
    morceaux.append(f"{minutes}min")
    return " ".join(morceaux)


def _outil_profil_joueur_nationsglory(contexte, pseudo, serveur=None):
    """Interroge GET /user/{username} sur l'API officielle NationsGlory :
    identité globale du joueur (pseudo, date de création du compte,
    dernière connexion, skin) et, pour le serveur demandé (par défaut
    "mocha"), ses données in-game : pays, rang, power/max_power, temps de
    jeu, statut en ligne et compétences (mineur/bûcheron/etc.). `contexte`
    n'est pas utilisé ici (donnée publique NationsGlory, indépendante du
    serveur Discord) mais reste accepté pour respecter la signature
    commune à tous les outils de ia_outils.py."""
    pseudo = (pseudo or "").strip()
    if not pseudo:
        return {"erreur": "Aucun pseudo NationsGlory fourni."}
    donnees, erreur = _requete_nationsglory(f"/user/{pseudo}")
    if erreur:
        return {"erreur": erreur}

    serveur = (serveur or "mocha").strip().lower()
    infos_serveur = (donnees.get("servers") or {}).get(serveur)
    skin = donnees.get("skin") or {}

    resultat = {
        "pseudo": donnees.get("username"),
        "date_creation_compte": donnees.get("created_at"),
        "derniere_connexion_globale": donnees.get("last_connection"),
        "skin_tete": skin.get("head"),
        "skin_corps": skin.get("body"),
        "serveur_consulte": serveur,
    }
    if not infos_serveur:
        resultat["erreur_serveur"] = f"Aucune donnée pour ce joueur sur le serveur « {serveur} »."
        return resultat

    competences = infos_serveur.get("skills")
    resultat.update({
        "pays": infos_serveur.get("country") or None,
        "rang_dans_le_pays": infos_serveur.get("country_rank") or None,
        "grades": infos_serveur.get("groups") or [],
        "power": infos_serveur.get("power"),
        "max_power": infos_serveur.get("max_power"),
        "temps_de_jeu_secondes": infos_serveur.get("playtime"),
        "temps_de_jeu_lisible": _formater_duree_secondes(infos_serveur.get("playtime")),
        "en_ligne": bool(infos_serveur.get("online")),
        "derniere_connexion_sur_ce_serveur": infos_serveur.get("last_connection"),
        "competences": competences if isinstance(competences, dict) else None,
    })
    return resultat


ia_outils.enregistrer_outil(
    nom="profil_joueur_nationsglory",
    description=(
        "Renvoie le profil complet d'un joueur NationsGlory : pseudo, date de "
        "création du compte, dernière connexion, skin, ainsi que ses données "
        "sur un serveur précis (pays, rang, power/max_power, temps de jeu, "
        "statut en ligne, compétences comme mineur/bûcheron/fermier/etc.). À "
        "utiliser dès qu'on demande le profil, les statistiques ou la fiche "
        "d'un joueur NationsGlory. Sans argument de serveur, répond pour Mocha."
    ),
    parametres={
        "type": "object",
        "properties": {
            "pseudo": {
                "type": "string",
                "description": "Pseudo exact du joueur NationsGlory à rechercher.",
            },
            "serveur": {
                "type": "string",
                "description": (
                    "Nom du serveur NationsGlory en minuscules (ex: 'mocha', "
                    "'blue', 'orange') dont on veut les statistiques en jeu. "
                    "Par défaut : 'mocha'."
                ),
            },
        },
        "required": ["pseudo"],
    },
    executer=_outil_profil_joueur_nationsglory,
)


def _outil_pays_nationsglory(contexte, pays, serveur=None):
    """Interroge GET /country/{server}/{country} sur l'API officielle
    NationsGlory : nom, chef, membres, power/power max, mmr, niveau,
    alliés et ennemis d'un pays, sur le serveur demandé (par défaut
    "mocha"). `contexte` n'est pas utilisé ici (donnée publique, voir
    _outil_profil_joueur_nationsglory ci-dessus)."""
    pays = (pays or "").strip()
    if not pays:
        return {"erreur": "Aucun nom de pays fourni."}
    serveur = (serveur or "mocha").strip().lower()
    donnees, erreur = _requete_nationsglory(f"/country/{serveur}/{pays}")
    if erreur:
        return {"erreur": erreur}
    return {
        "nom": donnees.get("name"),
        "serveur": donnees.get("server"),
        "chef": donnees.get("leader"),
        "date_creation": donnees.get("creation_date"),
        "description": donnees.get("description"),
        "nombre_membres": donnees.get("count_members"),
        "membres": donnees.get("members") or [],
        "power": donnees.get("power"),
        "power_max": donnees.get("maxpower"),
        "nombre_claims": donnees.get("count_claims"),
        "mmr": donnees.get("mmr"),
        "niveau": donnees.get("level"),
        "allies": donnees.get("allies") or [],
        "ennemis": donnees.get("ennemies") or [],
    }


ia_outils.enregistrer_outil(
    nom="pays_nationsglory",
    description=(
        "Renvoie les informations d'un pays (nation) NationsGlory : nom, "
        "chef, date de création, nombre et liste des membres, power/power "
        "max, mmr, niveau, alliés et ennemis. À utiliser dès qu'on demande "
        "des infos sur un pays/une nation NationsGlory. Sans argument de "
        "serveur, répond pour Mocha."
    ),
    parametres={
        "type": "object",
        "properties": {
            "pays": {
                "type": "string",
                "description": "Nom exact du pays NationsGlory à rechercher.",
            },
            "serveur": {
                "type": "string",
                "description": (
                    "Nom du serveur NationsGlory en minuscules (ex: 'mocha', "
                    "'blue', 'orange'). Par défaut : 'mocha'."
                ),
            },
        },
        "required": ["pays"],
    },
    executer=_outil_pays_nationsglory,
)


# ---- Outils "vraies données du royaume" (catalogue de missions, rang) ----
# Contrairement aux outils "personnels" historiques (consulter_blames,
# consulter_mission_active, consulter_historique_missions, gérés plus bas
# dans _executer_outil_ia), ceux-ci passent par le registre générique
# ia_outils.py. Le `contexte` reçu contient toujours guild_id/guild, et
# DEPUIS PEU aussi joueur_id (voir interroger_ia) quand la conversation est
# liée à un joueur précis — jamais fourni par l'IA elle-même, donc toujours
# fiable pour ne renvoyer QUE les données du joueur qui parle.

def _outil_catalogue_missions(contexte, categorie=None):
    """Aperçu PUBLIC du catalogue de missions du serveur (aucune donnée
    personnelle) : nombre de missions par catégorie, délai type, exemples."""
    guild_id = contexte.get("guild_id")
    if not guild_id:
        return {"erreur": "Aucun serveur identifié pour cette conversation."}
    structure = charger_missions_fichier(guild_id)
    categorie = (categorie or "").strip().lower() or None
    if categorie and categorie not in structure:
        return {"erreur": f"Catégorie « {categorie} » inconnue (attendu : commune, moyenne, difficile, royal)."}
    categories = [categorie] if categorie else list(structure.keys())
    return {
        "categories": {
            cat: {
                "nombre_missions": len(structure.get(cat, [])),
                "delai_type": structure[cat][0]["delai"] if structure.get(cat) else None,
                "exemples": [m["texte"] for m in structure.get(cat, [])[:3]],
            }
            for cat in categories
        }
    }


ia_outils.enregistrer_outil(
    nom="catalogue_missions",
    description=(
        "Renvoie un aperçu du catalogue de missions du serveur : nombre de "
        "missions disponibles par catégorie (commune, moyenne, difficile, "
        "royal), leur délai type et quelques exemples de missions. À "
        "utiliser dès qu'on demande quelles missions existent, combien il y "
        "en a, ou des exemples pour une catégorie donnée."
    ),
    parametres={
        "type": "object",
        "properties": {
            "categorie": {
                "type": "string",
                "description": (
                    "Catégorie précise à consulter : 'commune', 'moyenne', "
                    "'difficile' ou 'royal'. Omis = toutes les catégories."
                ),
            }
        },
        "required": [],
    },
    executer=_outil_catalogue_missions,
)


def _outil_rang_joueur(contexte):
    """Rang ACTUEL du joueur qui pose la question, plus le prochain rang
    visable et l'état de ses conditions (auto-vérifiées + manuelles). Le
    joueur est TOUJOURS celui de `contexte["joueur_id"]` (injecté par
    interroger_ia), jamais un argument fourni par l'IA."""
    guild_id = contexte.get("guild_id")
    guild = contexte.get("guild")
    joueur_id = contexte.get("joueur_id")
    if not guild_id or not joueur_id:
        return {"erreur": "Aucun joueur/serveur identifié pour cette conversation (ex: pas encore relié à un compte Discord)."}
    rang_actuel = obtenir_rang_joueur(guild_id, joueur_id)
    if not rang_actuel:
        return {"erreur": "Aucun catalogue de rangs configuré sur ce serveur."}
    resultat = {"rang_actuel": {"nom": rang_actuel["nom"], "groupe": rang_actuel["groupe"]}}
    if guild:
        rangs = sorted(charger_rangs(guild_id), key=lambda r: r["ordre"])
        suivants = [r for r in rangs if r["ordre"] > rang_actuel["ordre"] and not r.get("unique")]
        if suivants:
            prochain = suivants[0]
            rapport = verifier_conditions_rang(guild, joueur_id, prochain)
            resultat["prochain_rang"] = {
                "nom": prochain["nom"],
                "conditions_automatiques": rapport["auto"],
                "conditions_manuelles_a_verifier_par_un_instructeur": rapport["manuel"],
                "toutes_les_conditions_automatiques_sont_ok": rapport["toutes_auto_ok"],
            }
        else:
            resultat["prochain_rang"] = None
    return resultat


ia_outils.enregistrer_outil(
    nom="rang_joueur",
    description=(
        "Renvoie le rang ACTUEL du joueur qui pose la question, ainsi que "
        "le prochain rang visable et le détail de ses conditions (lesquelles "
        "sont déjà remplies, lesquelles ne le sont pas encore, et lesquelles "
        "doivent être vérifiées manuellement par un instructeur). À utiliser "
        "dès qu'on demande son rang actuel, ce qu'il lui manque pour monter "
        "de rang, ou s'il peut faire une demande de rang."
    ),
    parametres={"type": "object", "properties": {}, "required": []},
    executer=_outil_rang_joueur,
)


def _outil_demandes_rang_en_attente(contexte):
    """RÉSERVÉ AU STAFF : liste les demandes de rang encore en attente de
    traitement sur ce serveur. Le joueur qui pose la question doit être
    instructeur/propriétaire (vérifié via son rôle Discord réel, jamais
    déclaré par l'IA elle-même) ; sinon l'outil refuse plutôt que d'exposer
    les demandes des autres joueurs."""
    guild_id = contexte.get("guild_id")
    guild = contexte.get("guild")
    joueur_id = contexte.get("joueur_id")
    if not guild_id or not joueur_id:
        return {"erreur": "Aucun joueur/serveur identifié pour cette conversation."}
    membre = guild.get_member(int(joueur_id)) if guild and str(joueur_id).isdigit() else None
    est_staff = verifier_permissions_staff(membre) if membre else est_proprietaire(joueur_id)
    if not est_staff:
        return {"erreur": "Cette information est réservée au staff (instructeurs/propriétaire)."}
    demandes = [d for d in charger_demandes_rang(guild_id) if d.get("statut") == "en_attente"]
    maintenant = datetime.now()
    resultat = []
    for d in demandes:
        rang = obtenir_rang_par_id(guild_id, d.get("rang_id"))
        try:
            jours_attente = (maintenant - datetime.strptime(d["date"], "%d/%m/%Y à %H:%M")).days
        except Exception:
            jours_attente = None
        resultat.append({
            "joueur_id": d.get("joueur_id"),
            "rang_demande": rang["nom"] if rang else d.get("rang_id"),
            "date": d.get("date"),
            "jours_attente": jours_attente,
            "motivation": d.get("motivation"),
        })
    return {"nombre_en_attente": len(resultat), "demandes": resultat}


ia_outils.enregistrer_outil(
    nom="demandes_rang_en_attente",
    description=(
        "RÉSERVÉ AU STAFF (instructeur/propriétaire) : renvoie la liste des "
        "demandes de rang actuellement en attente de traitement sur ce "
        "serveur (joueur, rang demandé, date, motivation, jours d'attente). "
        "À utiliser dès qu'un membre du staff demande quelles demandes de "
        "rang sont en attente, à traiter, ou en retard. Refuse "
        "automatiquement si celui qui pose la question n'est pas staff."
    ),
    parametres={"type": "object", "properties": {}, "required": []},
    executer=_outil_demandes_rang_en_attente,
)


def _outil_statistiques_serveur(contexte):
    """Statistiques PUBLIQUES et agrégées du serveur (aucune donnée
    personnelle) : taux de réussite des missions, mission la plus/moins
    populaire, temps moyen de complétion. Réutilise le même calcul que la
    page /admin/statistiques du site, pour ne jamais afficher des chiffres
    incohérents entre le site et l'IA."""
    guild_id = contexte.get("guild_id")
    if not guild_id:
        return {"erreur": "Aucun serveur identifié pour cette conversation."}
    stats = site_web.calculer_stats_missions(guild_id, {"charger_profils": charger_profils})
    resultat = {
        "total_missions_terminees": stats["total_missions"],
        "total_reussies": stats["total_reussies"],
        "total_echouees": stats["total_echouees"],
        "taux_reussite_pourcent": stats["taux_reussite"],
        "temps_moyen_completion": stats["temps_moyen_texte"],
        "nombre_missions_distinctes_jouees": stats["nb_missions_distinctes"],
    }
    if stats["mission_plus_populaire"]:
        nom, info = stats["mission_plus_populaire"]
        resultat["mission_plus_populaire"] = {"texte": nom, "categorie": info["categorie"], "fois_attribuee": info["count"]}
    if stats["mission_moins_populaire"]:
        nom, info = stats["mission_moins_populaire"]
        resultat["mission_moins_populaire"] = {"texte": nom, "categorie": info["categorie"], "fois_attribuee": info["count"]}
    return resultat


ia_outils.enregistrer_outil(
    nom="statistiques_serveur",
    description=(
        "Renvoie les statistiques agrégées et publiques du serveur : taux "
        "de réussite des missions, mission la plus/moins populaire, temps "
        "moyen de complétion. À utiliser dès qu'on demande des statistiques "
        "générales du royaume (pas les stats personnelles d'un joueur, "
        "voir consulter_historique_missions pour ça)."
    ),
    parametres={"type": "object", "properties": {}, "required": []},
    executer=_outil_statistiques_serveur,
)

VALERIUS_IA_SYSTEM_PROMPT = (
    "Tu es l'Intelligence Royale de Valerius, un conseiller virtuel au service d'un "
    "Royaume géré sur Discord. Tu réponds aux joueurs et au staff avec courtoisie, "
    "clarté et un ton légèrement noble/royal, sans exagérer. Tu peux aider sur "
    "n'importe quel sujet (questions générales, aide sur le serveur, conseils, etc.). "
    "Reste concis : tes réponses doivent tenir dans un message Discord (évite les "
    "réponses interminables sauf si on te le demande explicitement). "
    "Tu as accès à des outils pour consulter les VRAIES données du joueur qui te "
    "parle (ses blâmes actifs, sa mission en cours, son historique de missions). "
    "Utilise-les systématiquement dès que la question porte sur SES données "
    "personnelles plutôt que de deviner ou d'inventer un chiffre. Si aucun outil "
    "n'est disponible pour cette conversation (ex: compte non relié à Discord), "
    "dis-le simplement et oriente vers la commande Discord correspondante."
)

# Contexte général du "pays" / royaume, pour que l'IA sache toujours répondre
# correctement même sans qu'on le lui reformule à chaque fois. Volontairement
# SANS aucune information sensible (pas d'ID Discord, pas de code de
# déverrouillage, pas de token) : uniquement des règles publiques du jeu de
# rôle, connues de tous les joueurs.
CONTEXTE_ROYAUME = (
    "Contexte du Royaume (à connaître pour bien répondre) :\n"
    "- Le site/bot gère deux systèmes distincts : « Valerius » (missions) et "
    "« Osiris » (discipline : blâmes, avertissements, rankups/deranks).\n"
    "- Missions Valerius : 4 catégories de difficulté croissante — "
    "🟢 Commune (délai ~3 jours), 🔵 Moyenne (~7 jours), 🟠 Difficile (~15 jours), "
    "🔴 Royal (~20 jours). Un joueur ouvre un ticket, choisit une difficulté, "
    "reçoit une mission aléatoire de cette catégorie avec un chrono, puis doit "
    "la valider (bouton « Finir la mission » ou /missionaccomplie) avant "
    "l'expiration, sous peine d'échec automatique.\n"
    "- Une fois la mission déclarée finie, un instructeur évalue : Accepter, "
    "Refuser, ou Demander une preuve (capture d'écran).\n"
    "- Rôles sur le site web, du plus faible au plus élevé : « Malgache » "
    "(compte de base, accès à son profil/catalogue/casier), « Instructeur » "
    "(gère les serveurs, missions, comptes), « Propriétaire » (accès total).\n"
    "- Système Osiris : les blâmes expirent automatiquement après 2 semaines."
)


def _generer_catalogue_commandes():
    """Construit dynamiquement la liste des commandes slash déclarées sur
    les deux bots (Valerius et Osiris), à partir de ce qui est réellement
    enregistré dans le code (bot.tree / bot_osiris.tree). Comme c'est généré
    à l'exécution, la liste reste toujours synchronisée avec le code, même
    si des commandes sont ajoutées/retirées plus tard — pas besoin de la
    maintenir à la main."""
    lignes = []
    for label, arbre in (("Valerius", bot.tree), ("Osiris", bot_osiris.tree)):
        commandes = sorted(arbre.get_commands(), key=lambda c: c.name)
        if not commandes:
            continue
        lignes.append(f"[{label}]")
        for cmd in commandes:
            desc = (cmd.description or "").strip()
            lignes.append(f"/{cmd.name} — {desc}" if desc else f"/{cmd.name}")
    return "\n".join(lignes)


# Mis en cache au premier appel : à ce moment-là, tous les décorateurs
# @bot.tree.command du fichier ont déjà été exécutés (le module est
# entièrement chargé avant que le bot ne tourne et ne réponde à une
# question), donc le catalogue est complet dès la première question posée.
_catalogue_commandes_cache = None

def obtenir_prompt_systeme_ia():
    """Assemble le prompt système complet envoyé à l'IA : personnalité +
    contexte du royaume + catalogue des commandes du bot. Permet à l'IA de
    répondre correctement si un joueur lui demande « comment fonctionne telle "
    "commande » ou « à quoi sert /xxx »."""
    global _catalogue_commandes_cache
    if _catalogue_commandes_cache is None:
        _catalogue_commandes_cache = _generer_catalogue_commandes()
    parties = [VALERIUS_IA_SYSTEM_PROMPT, CONTEXTE_ROYAUME]
    if _catalogue_commandes_cache:
        parties.append(
            "Liste des commandes disponibles sur le bot (nom — description) :\n"
            + _catalogue_commandes_cache
            + "\n\nSi on te demande comment utiliser une commande, explique-la "
            "clairement à partir de cette liste. Si une commande n'y figure "
            "pas, dis que tu ne la reconnais pas plutôt que d'inventer."
        )
    return "\n\n".join(parties)

# Mémoire de conversation en RAM (non persistée entre redémarrages), pour que
# l'IA garde le contexte des derniers échanges d'un joueur sur un salon donné.
IA_HISTORIQUE_MAX_MESSAGES = 10  # nombre de messages (user+assistant) conservés
_ia_historique = {}  # {(guild_id, channel_id, user_id): [ {"role":..., "content":...}, ... ]}

def _cle_ia(interaction: discord.Interaction):
    g_id = interaction.guild.id if interaction.guild else 0
    return (g_id, interaction.channel_id, interaction.user.id)

def reinitialiser_historique_ia(interaction: discord.Interaction):
    _ia_historique.pop(_cle_ia(interaction), None)

def reinitialiser_historique_ia_cle(cle):
    """Variante générique (utilisée aussi par le site web) : efface
    l'historique d'une clé quelconque, sans passer par une interaction Discord."""
    _ia_historique.pop(cle, None)

# ---------- Outils (function calling) : accès en LECTURE SEULE aux vraies
# données du joueur qui pose la question (jamais celles d'un autre — les
# outils ignorent complètement tout identifiant que le modèle pourrait
# inventer et utilisent toujours guild_id/joueur_id fournis par le code
# appelant, jamais par l'IA elle-même). ----------

def _outils_ia_disponibles():
    return [
        {
            "type": "function",
            "function": {
                "name": "consulter_blames",
                "description": (
                    "Renvoie le nombre de blâmes ACTIFS (non expirés) du joueur qui "
                    "pose la question, avec leur raison et leur date. À utiliser dès "
                    "qu'on demande combien de blâmes/avertissements on a, ou de voir "
                    "son casier disciplinaire."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "consulter_mission_active",
                "description": (
                    "Renvoie la mission actuellement en cours du joueur qui pose la "
                    "question (texte, catégorie, temps restant), ou indique qu'il n'a "
                    "aucune mission active. À utiliser dès qu'on demande sa mission "
                    "en cours, son temps restant, ou l'état de son ticket."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "consulter_historique_missions",
                "description": (
                    "Renvoie le bilan (nombre de missions réussies/échouées) et les "
                    "dernières missions de l'historique du joueur qui pose la "
                    "question. À utiliser dès qu'on demande son nombre de missions "
                    "réussies/échouées ou son historique."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limite": {
                            "type": "integer",
                            "description": "Nombre de missions récentes à renvoyer (par défaut 5, max 15).",
                        }
                    },
                    "required": [],
                },
            },
        },
    ] + ia_outils.definitions_pour_ia()

# Noms des outils "historiques" (données PERSONNELLES du joueur qui parle) :
# gérés par _executer_outil_ia ci-dessous, avec guild_id/joueur_id imposés
# par le code plutôt que par l'IA. Tout le reste (ex: données publiques
# NationsGlory) passe par le registre générique ia_outils.py.
NOMS_OUTILS_PERSONNELS = {"consulter_blames", "consulter_mission_active", "consulter_historique_missions"}

def _executer_outil_ia(nom, arguments, guild_id, joueur_id):
    """Exécute un outil demandé par l'IA. `guild_id`/`joueur_id` viennent
    TOUJOURS du code appelant (interaction Discord ou compte site connecté),
    jamais des arguments générés par le modèle : impossible pour l'IA de
    consulter les données d'un autre joueur que celui qui lui parle."""
    if not guild_id or not joueur_id:
        return {"erreur": "Aucun joueur/serveur identifié pour cette conversation (ex: pas encore relié à un compte Discord)."}
    try:
        if nom == "consulter_blames":
            actifs = obtenir_blames_actifs(guild_id, joueur_id)
            return {
                "nombre_blames_actifs": len(actifs),
                "blames": [{"raison": b.get("raison"), "date": b.get("date")} for b in actifs],
            }
        if nom == "consulter_mission_active":
            m = missions_actives.get(guild_id, {}).get(joueur_id)
            if not m:
                return {"mission_active": None}
            restant = m["date_fin"] - datetime.now()
            return {
                "mission_active": {
                    "texte": m["texte"],
                    "categorie": m["cat"],
                    "temps_restant": formater_duree(restant) if restant.total_seconds() > 0 else "délai dépassé (en attente de traitement)",
                    "en_attente_validation": m.get("en_attente", False),
                }
            }
        if nom == "consulter_historique_missions":
            limite = arguments.get("limite") or 5
            try:
                limite = max(1, min(int(limite), 15))
            except (TypeError, ValueError):
                limite = 5
            profils = charger_profils(guild_id)
            profil = profils.get(str(joueur_id))
            if not profil:
                return {"total_reussies": 0, "total_echouees": 0, "dernieres_missions": []}
            return {
                "total_reussies": profil.get("total_reussies", 0),
                "total_echouees": profil.get("total_echouees", 0),
                "dernieres_missions": [
                    {"texte": h.get("texte"), "statut": h.get("statut"), "categorie": h.get("categorie"), "date": h.get("date")}
                    for h in profil.get("historique", [])[:limite]
                ],
            }
        return {"erreur": f"Outil inconnu : {nom}"}
    except Exception as e:
        return {"erreur": f"Erreur interne lors de la consultation : {e}"}

IA_MAX_ALLERS_RETOURS_OUTILS = 3  # limite de sécurité anti-boucle infinie

async def interroger_ia(cle, question: str, guild_id=None, joueur_id=None):
    """Envoie la question (+ historique récent lié à `cle`) à l'IA (Groq).
    Retourne (texte, erreur). `cle` identifie la conversation : peut venir
    de _cle_ia(interaction) côté Discord, ou d'une clé propre au site web
    (ex: ("site", login)), pour que chacun garde son propre historique.
    Si `guild_id`/`joueur_id` sont fournis, l'IA peut consulter les VRAIES
    données de CE joueur (blâmes, mission active, historique) via des
    outils — jamais celles d'un autre joueur. Les outils publics/génériques
    enregistrés via ia_outils.py (ex: joueurs en ligne NationsGlory) restent
    disponibles même sans joueur_id, puisqu'ils ne dépendent d'aucun joueur."""
    if not client_ia:
        return None, "❌ L'Intelligence Royale n'est pas configurée : aucune clé API Groq (variable `GROQ_API_KEY`) n'a été définie."
    historique = _ia_historique.get(cle, [])
    messages = [{"role": "system", "content": obtenir_prompt_systeme_ia()}] + historique + [{"role": "user", "content": question}]
    outils = _outils_ia_disponibles()
    # joueur_id est inclus ici (en plus de guild_id/guild) pour que des
    # outils génériques enregistrés via ia_outils.py (ex: rang_joueur)
    # puissent eux aussi accéder aux VRAIES données du joueur qui parle,
    # sans jamais le recevoir comme argument fourni par l'IA elle-même.
    contexte_outils = {
        "guild_id": guild_id,
        "guild": discord.utils.get(bot.guilds, id=guild_id) if guild_id else None,
        "joueur_id": joueur_id,
    }
    try:
        texte = None
        for _ in range(IA_MAX_ALLERS_RETOURS_OUTILS):
            kwargs = dict(model=GROQ_MODEL, max_tokens=IA_MAX_TOKENS, messages=messages)
            if outils:
                kwargs["tools"] = outils
                kwargs["tool_choice"] = "auto"
            reponse = await client_ia.chat.completions.create(**kwargs)
            message = reponse.choices[0].message
            appels = getattr(message, "tool_calls", None)
            if not appels:
                texte = (message.content or "").strip()
                break
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {"id": a.id, "type": "function", "function": {"name": a.function.name, "arguments": a.function.arguments}}
                    for a in appels
                ],
            })
            for a in appels:
                nom_outil = a.function.name
                arguments_brutes = a.function.arguments or "{}"
                if nom_outil in NOMS_OUTILS_PERSONNELS:
                    if not guild_id or not joueur_id:
                        resultat = {"erreur": "Aucun joueur/serveur identifié pour cette conversation (ex: pas encore relié à un compte Discord)."}
                    else:
                        try:
                            arguments = json.loads(arguments_brutes)
                        except Exception:
                            arguments = {}
                        resultat = _executer_outil_ia(nom_outil, arguments, guild_id, joueur_id)
                    contenu = json.dumps(resultat, ensure_ascii=False)
                else:
                    contenu = await ia_outils.executer_outil(nom_outil, contexte_outils, arguments_brutes)
                messages.append({"role": "tool", "tool_call_id": a.id, "content": contenu})
        if texte is None:
            texte = "🤖 *(je n'ai pas réussi à obtenir une réponse claire, réessaie ta question)*"
        if not texte:
            return "🤖 *(l'IA n'a renvoyé aucun texte, réessaie ta question)*", None
        historique = historique + [{"role": "user", "content": question}, {"role": "assistant", "content": texte}]
        _ia_historique[cle] = historique[-IA_HISTORIQUE_MAX_MESSAGES:]
        return texte, None
    except Exception as e:
        return None, f"❌ Erreur lors de la requête à l'IA : {e}"

def decouper_texte(texte, taille=4000):
    """Découpe un texte en morceaux <= taille, sans couper un mot en deux si possible."""
    morceaux = []
    while len(texte) > taille:
        point_coupe = texte.rfind("\n", 0, taille)
        if point_coupe == -1:
            point_coupe = texte.rfind(" ", 0, taille)
        if point_coupe == -1:
            point_coupe = taille
        morceaux.append(texte[:point_coupe])
        texte = texte[point_coupe:].lstrip()
    if texte:
        morceaux.append(texte)
    return morceaux

def get_file_name(guild_id):
    return f"valerius_missions_{guild_id}.txt"

def get_profiles_file(guild_id):
    return f"valerius_profils_{guild_id}.json"

def get_active_missions_file(guild_id):
    return f"valerius_missions_actives_{guild_id}.json"

def get_points_file(guild_id):
    return f"valerius_points_{guild_id}.json"

# ================= POINTS PAR CATÉGORIE (entièrement configurables) =================
# Chaque mission ne rapporte PAS un nombre de points qui lui est propre :
# c'est sa CATÉGORIE (commune/moyenne/difficile/royal) qui détermine
# combien de points elle rapporte, et toutes les missions d'une même
# catégorie rapportent donc exactement le même nombre de points. Ces
# valeurs par défaut ne servent que tant que rien n'a été configuré ; elles
# sont modifiables à tout moment via /points_config ou le site web
# (page "Catalogue de missions"), serveur par serveur.
POINTS_PAR_DEFAUT_CATEGORIE = {"commune": 10, "moyenne": 25, "difficile": 50, "royal": 100}

def charger_points_categories(guild_id):
    """Charge la config des points par catégorie pour ce serveur. Complète
    automatiquement avec les valeurs par défaut si une catégorie manque
    encore (ex: fichier pas encore créé, ou nouvelle catégorie ajoutée)."""
    fichier = get_points_file(guild_id)
    points = dict(POINTS_PAR_DEFAUT_CATEGORIE)
    if os.path.exists(fichier):
        try:
            with open(fichier, "r", encoding="utf-8") as f:
                points.update(json.load(f))
        except Exception:
            pass
    return points

def sauvegarder_points_categories(guild_id, points):
    fichier = get_points_file(guild_id)
    with open(fichier, "w", encoding="utf-8") as f:
        json.dump(points, f, indent=4, ensure_ascii=False)

def points_pour_categorie(guild_id, categorie):
    """Nombre de points que rapporte N'IMPORTE QUELLE mission de cette
    catégorie sur ce serveur (0 si la catégorie est inconnue)."""
    return charger_points_categories(guild_id).get(categorie, 0)

def charger_missions_fichier(guild_id):
    structure = {"commune": [], "moyenne": [], "difficile": [], "royal": []}
    file_name = get_file_name(guild_id)
    if not os.path.exists(file_name): return structure
    with open(file_name, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "|" not in line: continue
            # 4e champ optionnel = points spécifiques à CETTE mission (surcharge
            # le nombre de points de la catégorie). Absent/vide = utilise les
            # points de la catégorie. Rétro-compatible avec les anciennes
            # lignes à 3 champs (sans points).
            parts = line.split("|", 3)
            if len(parts) not in (3, 4): continue
            cat, texte, delai = parts[0], parts[1], parts[2]
            points_str = parts[3] if len(parts) == 4 else ""
            mission = {"texte": texte, "delai": delai}
            if points_str.strip().lstrip("-").isdigit():
                mission["points"] = int(points_str.strip())
            if cat in structure: structure[cat].append(mission)
    return structure

def reecrire_toutes_missions(guild_id, structure):
    file_name = get_file_name(guild_id)
    with open(file_name, "w", encoding="utf-8") as f:
        for cat, liste in structure.items():
            for m in liste:
                points_str = str(m["points"]) if m.get("points") is not None else ""
                f.write(f"{cat}|{m['texte']}|{m['delai']}|{points_str}\n")

def sauvegarder_mission_fichier(guild_id, categorie, texte, delai, points=None):
    file_name = get_file_name(guild_id)
    points_str = str(points) if points is not None else ""
    with open(file_name, "a", encoding="utf-8") as f: f.write(f"{categorie}|{texte}|{delai}|{points_str}\n")

def vider_toutes_missions(guild_id):
    file_name = get_file_name(guild_id)
    with open(file_name, "w", encoding="utf-8") as f:
        f.write("")

def definir_points_mission(guild_id, categorie, index, points):
    """Modifie (ou efface, si `points` est None) la surcharge de points
    d'UNE mission précise du catalogue, repérée par catégorie + position.
    Sans surcharge, la mission retombe sur les points de sa catégorie."""
    structure = charger_missions_fichier(guild_id)
    if categorie not in structure or not (0 <= index < len(structure[categorie])):
        return False
    if points is None:
        structure[categorie][index].pop("points", None)
    else:
        structure[categorie][index]["points"] = points
    reecrire_toutes_missions(guild_id, structure)
    return True

# ================= BOUTIQUE (achat de produits contre des points) =================
# Chaque serveur a son propre catalogue de produits, stocké dans un fichier
# JSON séparé (comme les missions et les points par catégorie). Un produit a
# un nom, un coût en points, une description optionnelle, une image
# (fichier uploadé OU URL externe) et un stock optionnel (None = illimité).
# L'achat lui-même déduit les points du profil du joueur et garde une trace
# dans son historique d'achats — il ne livre RIEN automatiquement (pas de
# rôle Discord donné, pas d'objet en jeu) : c'est un instructeur qui doit
# ensuite remettre la récompense manuellement, d'où le message affiché au
# joueur après achat.

def get_boutique_file(guild_id):
    return f"valerius_boutique_{guild_id}.json"

def charger_boutique(guild_id):
    fichier = get_boutique_file(guild_id)
    if not os.path.exists(fichier):
        return []
    try:
        with open(fichier, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sauvegarder_boutique(guild_id, produits):
    fichier = get_boutique_file(guild_id)
    with open(fichier, "w", encoding="utf-8") as f:
        json.dump(produits, f, indent=4, ensure_ascii=False)

def obtenir_produit_boutique(guild_id, produit_id):
    for p in charger_boutique(guild_id):
        if p["id"] == produit_id:
            return p
    return None

def ajouter_produit_boutique(guild_id, nom, cout, description="", image=None, image_url=None, stock=None):
    """Ajoute un produit au catalogue de ce serveur.
    - image : nom de fichier uploadé (stocké dans boutique_images/), ou None.
    - image_url : URL externe utilisée si aucun fichier n'a été uploadé.
    - stock : nombre d'unités disponibles, ou None pour illimité."""
    produits = charger_boutique(guild_id)
    produit = {
        "id": secrets.token_hex(8),
        "nom": nom,
        "description": description or "",
        "cout": cout,
        "image": image,
        "image_url": image_url,
        "stock": stock,
        "actif": True,
    }
    produits.append(produit)
    sauvegarder_boutique(guild_id, produits)
    return produit

def modifier_produit_boutique(guild_id, produit_id, **champs):
    produits = charger_boutique(guild_id)
    for p in produits:
        if p["id"] == produit_id:
            p.update(champs)
            sauvegarder_boutique(guild_id, produits)
            return p
    return None

def supprimer_produit_boutique(guild_id, produit_id):
    produits = charger_boutique(guild_id)
    nouveaux = [p for p in produits if p["id"] != produit_id]
    if len(nouveaux) == len(produits):
        return False
    sauvegarder_boutique(guild_id, nouveaux)
    return True

def acheter_produit_boutique(guild_id, joueur_id, produit_id):
    """Tente l'achat d'un produit par un joueur. Vérifie disponibilité,
    stock et solde de points AVANT de rien débiter. Renvoie toujours
    (succes: bool, message: str, produit: dict|None) — jamais d'exception,
    pour que le site puisse afficher un message clair dans tous les cas."""
    produit = obtenir_produit_boutique(guild_id, produit_id)
    if not produit or not produit.get("actif", True):
        return False, "Ce produit n'est plus disponible.", None
    if produit.get("stock") is not None and produit["stock"] <= 0:
        return False, "Ce produit est en rupture de stock.", None

    profils = charger_profils(guild_id)
    initialiser_profil(joueur_id, profils)
    s_id = str(joueur_id)
    solde = profils[s_id].get("total_points", 0)
    cout = produit.get("cout", 0)
    if solde < cout:
        return False, f"Points insuffisants ({solde}/{cout} pts).", None

    profils[s_id]["total_points"] = solde - cout
    profils[s_id].setdefault("achats", []).insert(0, {
        "produit_id": produit["id"],
        "nom": produit["nom"],
        "cout": cout,
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
    })
    sauvegarder_profils(guild_id, profils)

    if produit.get("stock") is not None:
        modifier_produit_boutique(guild_id, produit_id, stock=produit["stock"] - 1)

    return True, f"Achat de « {produit['nom']} » réussi pour {cout} pts !", produit


# ================= ROUE ALÉATOIRE (roue de la fortune configurable) =================
# Il existe maintenant TROIS roues indépendantes par serveur — une par
# catégorie de mission "à partir de moyenne" : "moyenne", "difficile" et
# "royal" (pas de roue "commune"). Chacune a son propre fichier JSON, sa
# propre liste de parts et son propre rééquilibrage automatique — elles ne
# partagent RIEN entre elles. Une part a un nom, un pourcentage de chances,
# un nombre de points optionnel donné au joueur qui tombe dessus (0 = pas de
# récompense), une image optionnelle (URL), et un statut actif/inactif.
#
# INVARIANT respecté à tout moment, pour CHAQUE roue séparément : la somme
# des pourcentages des parts ACTIVES vaut toujours 100 (aux arrondis près).
# C'est ce qui permet le rééquilibrage automatique demandé : dès qu'un admin
# fixe le pourcentage d'UNE part (en l'ajoutant, la modifiant, l'activant ou
# la désactivant), toutes les AUTRES parts actives DE CETTE ROUE sont
# automatiquement redimensionnées proportionnellement à leur poids actuel
# pour que le total retombe pile sur 100 — jamais besoin de retoucher les
# autres à la main.
#
# Pour jouer, un joueur doit dépenser un "ticket de roue" (voir plus bas,
# section TICKETS DE ROUE) : un ticket est générique, il permet d'activer
# N'IMPORTE LAQUELLE des trois roues au choix du joueur — il n'est PAS lié à
# la catégorie de mission qui l'a fait gagner.

TYPES_ROUE = ("moyenne", "difficile", "royal")
NOMS_TYPES_ROUE = {"moyenne": "🔵 Roue Moyenne", "difficile": "🟠 Roue Difficile", "royal": "🔴 Roue Royale"}

def _valider_type_roue(type_roue):
    if type_roue not in TYPES_ROUE:
        raise ValueError(f"Type de roue invalide : « {type_roue} » (attendu : {', '.join(TYPES_ROUE)}).")

def get_roue_file(guild_id, type_roue):
    _valider_type_roue(type_roue)
    return f"valerius_roue_{type_roue}_{guild_id}.json"

def charger_roue(guild_id, type_roue):
    fichier = get_roue_file(guild_id, type_roue)
    if not os.path.exists(fichier):
        return []
    try:
        with open(fichier, "r", encoding="utf-8") as f:
            parts = json.load(f)
    except Exception:
        return []
    # Compatibilité : les parts créées avant l'ajout du champ image.
    for p in parts:
        p.setdefault("image", "")
    return parts

def sauvegarder_roue(guild_id, type_roue, parts):
    fichier = get_roue_file(guild_id, type_roue)
    with open(fichier, "w", encoding="utf-8") as f:
        json.dump(parts, f, indent=4, ensure_ascii=False)

def obtenir_part_roue(guild_id, type_roue, part_id):
    for p in charger_roue(guild_id, type_roue):
        if p["id"] == part_id:
            return p
    return None

def _corriger_arrondi_roue(parts):
    """Corrige les micro-écarts d'arrondi (ex: 33.33 + 33.33 + 33.34 ≠ 100
    pile) pour que la somme des parts actives retombe EXACTEMENT sur 100.0,
    en ajustant la plus grande part active. Modifie `parts` sur place."""
    actives = [p for p in parts if p.get("actif", True)]
    if not actives:
        return
    total = round(sum(p["pourcentage"] for p in actives), 2)
    ecart = round(100 - total, 2)
    if ecart:
        plus_grande = max(actives, key=lambda p: p["pourcentage"])
        plus_grande["pourcentage"] = round(plus_grande["pourcentage"] + ecart, 2)

def _fixer_pourcentage_part_roue(parts, part_id, nouveau_pourcentage):
    """Fixe le pourcentage de la part `part_id` à `nouveau_pourcentage` et
    redimensionne PROPORTIONNELLEMENT toutes les AUTRES parts actives (à
    leur poids relatif actuel entre elles) pour que le total des parts
    actives reste exactement 100. Fonctionne aussi bien pour une part qu'on
    vient d'ajouter (à 0% pour l'instant) que pour une part existante qu'on
    modifie. Modifie `parts` sur place, ne renvoie rien."""
    nouveau_pourcentage = max(0.0, min(100.0, round(float(nouveau_pourcentage), 2)))
    autres_actives = [p for p in parts if p.get("actif", True) and p["id"] != part_id]
    reste = round(100 - nouveau_pourcentage, 2)

    if autres_actives:
        total_autres = sum(p["pourcentage"] for p in autres_actives)
        if total_autres > 0:
            for p in autres_actives:
                p["pourcentage"] = round(p["pourcentage"] / total_autres * reste, 2)
        else:
            part_egale = round(reste / len(autres_actives), 2)
            for p in autres_actives:
                p["pourcentage"] = part_egale

    for p in parts:
        if p["id"] == part_id:
            p["pourcentage"] = nouveau_pourcentage
            p["actif"] = True

    _corriger_arrondi_roue(parts)

def _repartir_apres_retrait_roue(parts, part_id_exclue):
    """Redimensionne toutes les parts actives restantes (en excluant
    `part_id_exclue`, qui vient d'être supprimée ou désactivée) pour que
    leur total remonte à 100, proportionnellement à leur poids actuel entre
    elles. Modifie `parts` sur place."""
    actives_restantes = [p for p in parts if p.get("actif", True) and p["id"] != part_id_exclue]
    if not actives_restantes:
        return
    total = sum(p["pourcentage"] for p in actives_restantes)
    if total > 0:
        for p in actives_restantes:
            p["pourcentage"] = round(p["pourcentage"] / total * 100, 2)
    else:
        part_egale = round(100 / len(actives_restantes), 2)
        for p in actives_restantes:
            p["pourcentage"] = part_egale
    _corriger_arrondi_roue(parts)

def ajouter_part_roue(guild_id, type_roue, nom, pourcentage=None, points=0, image=""):
    """Ajoute une nouvelle part à LA roue `type_roue` de ce serveur. Si
    `pourcentage` n'est pas précisé, la nouvelle part reçoit une part égale
    (100 / nombre de parts actives après ajout), et TOUTES les autres parts
    actives DE CETTE ROUE sont automatiquement réduites en proportion pour
    lui faire de la place."""
    parts = charger_roue(guild_id, type_roue)
    nb_actives_apres = len([p for p in parts if p.get("actif", True)]) + 1
    if pourcentage is None:
        pourcentage = 100 / nb_actives_apres

    nouvelle_part = {
        "id": secrets.token_hex(8),
        "nom": nom,
        "pourcentage": 0.0,
        "points": points or 0,
        "image": image or "",
        "actif": True,
    }
    parts.append(nouvelle_part)
    _fixer_pourcentage_part_roue(parts, nouvelle_part["id"], pourcentage)
    sauvegarder_roue(guild_id, type_roue, parts)
    return next(p for p in parts if p["id"] == nouvelle_part["id"])

def modifier_part_roue(guild_id, type_roue, part_id, nom=None, pourcentage=None, points=None, image=None):
    """Modifie une part existante de la roue `type_roue`. Si `pourcentage`
    est fourni, TOUTES les autres parts actives DE CETTE ROUE se
    rééquilibrent automatiquement (voir _fixer_pourcentage_part_roue) ;
    `nom`/`points`/`image` se modifient sans impact sur les pourcentages des
    autres parts. Renvoie la part mise à jour, ou None si elle n'existe pas."""
    parts = charger_roue(guild_id, type_roue)
    part = next((p for p in parts if p["id"] == part_id), None)
    if not part:
        return None
    if pourcentage is not None:
        part["actif"] = True  # on ne peut fixer un % précis que sur une part active
        _fixer_pourcentage_part_roue(parts, part_id, pourcentage)
    if nom is not None:
        part["nom"] = nom
    if points is not None:
        part["points"] = points
    if image is not None:
        part["image"] = image
    sauvegarder_roue(guild_id, type_roue, parts)
    return next(p for p in parts if p["id"] == part_id)

def basculer_actif_part_roue(guild_id, type_roue, part_id):
    """Active/désactive une part de la roue `type_roue` sans la supprimer :
    - désactivation : son pourcentage est libéré et redistribué
      proportionnellement aux autres parts actives (total ramené à 100) ;
    - réactivation : elle récupère une part égale entre toutes les parts
      actives, et les autres se réduisent automatiquement en proportion.
    Renvoie la part mise à jour, ou None si elle n'existe pas."""
    parts = charger_roue(guild_id, type_roue)
    part = next((p for p in parts if p["id"] == part_id), None)
    if not part:
        return None
    if part.get("actif", True):
        part["actif"] = False
        _repartir_apres_retrait_roue(parts, part_id)
    else:
        nb_actives_apres = len([p for p in parts if p.get("actif", True)]) + 1
        _fixer_pourcentage_part_roue(parts, part_id, 100 / nb_actives_apres)
    sauvegarder_roue(guild_id, type_roue, parts)
    return next(p for p in parts if p["id"] == part_id)

def supprimer_part_roue(guild_id, type_roue, part_id):
    """Supprime définitivement une part de la roue `type_roue` et redistribue
    son pourcentage (si elle était active) proportionnellement aux parts
    actives restantes, pour que leur total reste 100. Renvoie False si la
    part n'existe pas."""
    parts = charger_roue(guild_id, type_roue)
    part = next((p for p in parts if p["id"] == part_id), None)
    if not part:
        return False
    etait_active = part.get("actif", True)
    nouveaux = [p for p in parts if p["id"] != part_id]
    if etait_active:
        _repartir_apres_retrait_roue(nouveaux, part_id)
    sauvegarder_roue(guild_id, type_roue, nouveaux)
    return True

def tourner_roue(guild_id, type_roue):
    """Tire une part au hasard sur la roue `type_roue`, pondérée par son
    pourcentage. Renvoie None s'il n'y a aucune part active avec un
    pourcentage > 0."""
    actives = [p for p in charger_roue(guild_id, type_roue) if p.get("actif", True) and p["pourcentage"] > 0]
    if not actives:
        return None
    return random.choices(actives, weights=[p["pourcentage"] for p in actives], k=1)[0]

def jouer_roue(guild_id, type_roue, joueur_id):
    """Consomme un ticket de roue du joueur (voir TICKETS DE ROUE ci-dessous),
    tire une part sur la roue `type_roue` choisie et, si elle offre des
    points, les crédite immédiatement sur le profil du joueur.

    Renvoie un tuple (gagnante, erreur) :
    - en cas de succès : (part_gagnante_dict, None) ;
    - en cas d'échec (pas de ticket, roue vide, type invalide) :
      (None, "message d'erreur lisible par le joueur"). Si le ticket avait
      déjà été consommé au moment où on découvre que la roue est vide, il
      est automatiquement remboursé.

    Utilisée par le site web (page /roue) : encapsule vérification du
    ticket + tirage + récompense en une seule opération, pour que le site
    n'ait jamais à manipuler les profils/tickets lui-même."""
    try:
        _valider_type_roue(type_roue)
    except ValueError:
        return None, "Cette roue n'existe pas."

    if not retirer_ticket_roue(guild_id, joueur_id):
        return None, "Tu n'as aucun ticket de roue. Termine une mission moyenne, difficile ou royale pour en gagner un."

    gagnante = tourner_roue(guild_id, type_roue)
    if not gagnante:
        ajouter_tickets_roue(guild_id, joueur_id, 1)  # roue vide : on rembourse le ticket
        return None, "Cette roue n'a aucune part active pour l'instant."

    if gagnante.get("points"):
        profils = charger_profils(guild_id)
        initialiser_profil(joueur_id, profils)
        s_id = str(joueur_id)
        profils[s_id]["total_points"] = profils[s_id].get("total_points", 0) + gagnante["points"]
        sauvegarder_profils(guild_id, profils)
    return gagnante, None


# ================= TICKETS DE ROUE =================
# Un ticket de roue est générique (pas lié à une catégorie précise) : il est
# stocké directement dans le profil du joueur (profils[id]["tickets_roue"],
# un simple compteur entier) et permet d'activer LA ROUE DE SON CHOIX parmi
# les trois (moyenne / difficile / royal). Deux façons d'en obtenir :
# - automatiquement, à la validation d'une mission moyenne/difficile/royale
#   (voir action_accepter_mission) ;
# - manuellement, un instructeur/propriétaire peut en offrir depuis la fiche
#   du joueur sur le site web (/admin/profils/<guild_id>/<joueur_id>).

def obtenir_tickets_roue(guild_id, joueur_id):
    profils = charger_profils(guild_id)
    return profils.get(str(joueur_id), {}).get("tickets_roue", 0)

def ajouter_tickets_roue(guild_id, joueur_id, quantite):
    """Ajoute (ou retire, si `quantite` est négatif) des tickets de roue au
    profil du joueur, sans jamais descendre sous 0. Renvoie le nouveau
    total."""
    profils = charger_profils(guild_id)
    initialiser_profil(joueur_id, profils)
    s_id = str(joueur_id)
    profils[s_id]["tickets_roue"] = max(0, profils[s_id].get("tickets_roue", 0) + quantite)
    sauvegarder_profils(guild_id, profils)
    return profils[s_id]["tickets_roue"]

def retirer_ticket_roue(guild_id, joueur_id):
    """Consomme UN ticket de roue si le joueur en a au moins un. Renvoie
    True si un ticket a bien été consommé, False s'il n'en avait aucun
    (dans ce cas, rien n'est modifié)."""
    profils = charger_profils(guild_id)
    initialiser_profil(joueur_id, profils)
    s_id = str(joueur_id)
    if profils[s_id].get("tickets_roue", 0) <= 0:
        return False
    profils[s_id]["tickets_roue"] -= 1
    sauvegarder_profils(guild_id, profils)
    return True


def charger_profils(guild_id):
    profiles_file = get_profiles_file(guild_id)
    if not os.path.exists(profiles_file): return {}
    try:
        with open(profiles_file, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def sauvegarder_profils(guild_id, profils):
    profiles_file = get_profiles_file(guild_id)
    with open(profiles_file, "w", encoding="utf-8") as f: json.dump(profils, f, indent=4, ensure_ascii=False)

def initialiser_profil(p_id, profils):
    s_id = str(p_id)
    if s_id not in profils:
        profils[s_id] = {
            "total_reussies": 0,
            "total_echouees": 0,
            "total_points": 0,
            "tickets_roue": 0,
            "historique": []
        }
    else:
        # Compatibilité : les profils créés avant l'ajout du système de
        # points / tickets de roue n'ont pas encore ces champs.
        profils[s_id].setdefault("total_points", 0)
        profils[s_id].setdefault("tickets_roue", 0)

def ajouter_historique(p_id, profils, texte, statut, cat="inconnu", duree_secondes=None, points=0):
    s_id = str(p_id)
    initialiser_profil(p_id, profils)
    profils[s_id]["historique"].insert(0, {
        "texte": texte,
        "statut": statut,
        "categorie": cat,
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
        "duree_secondes": duree_secondes,
        # Points réellement gagnés sur CETTE entrée (figés au moment de la
        # validation) : permet de les retirer correctement plus tard même
        # si la config des points par catégorie a changé entre-temps.
        "points": points
    })

DELAI_MIN_REPETITION_MISSION = timedelta(days=7)

def mission_recemment_donnee(guild_id, joueur_id, texte, delai=DELAI_MIN_REPETITION_MISSION):
    """True si ce joueur a déjà eu cette mission (texte strictement
    identique) sur ce serveur il y a moins de `delai` (par défaut 7 jours),
    peu importe si elle a été réussie ou échouée."""
    profils = charger_profils(guild_id)
    profil = profils.get(str(joueur_id))
    if not profil:
        return False
    limite = datetime.now() - delai
    for entree in profil.get("historique", []):
        if entree.get("texte") != texte:
            continue
        try:
            date_entree = datetime.strptime(entree.get("date", ""), "%d/%m/%Y à %H:%M")
        except (ValueError, TypeError):
            continue
        if date_entree >= limite:
            return True
    return False

def choisir_mission_sans_repetition(guild_id, joueur_id, missions_liste, delai=DELAI_MIN_REPETITION_MISSION):
    """Tire une mission au hasard dans `missions_liste` en excluant celles
    que le joueur a déjà eues il y a moins de `delai`. S'il n'en reste
    aucune de "fraîche" (catégorie trop restreinte), on retombe sur
    l'ensemble complet plutôt que de bloquer l'attribution."""
    fraiches = [m for m in missions_liste if not mission_recemment_donnee(guild_id, joueur_id, m["texte"], delai)]
    return random.choice(fraiches) if fraiches else random.choice(missions_liste)

# ================= MISSION À REFAIRE (échec / abandon) =================
# Quand une mission échoue ou est abandonnée, on la mémorise ici pour ce
# joueur/serveur. Tant qu'elle n'est pas réussie, la prochaine ouverture de
# ticket lui réattribue automatiquement CETTE mission (peu importe le
# bouton de catégorie cliqué) au lieu d'un tirage aléatoire.

def definir_mission_a_refaire(guild_id, joueur_id, texte, delai_texte, cat, points=None):
    profils = charger_profils(guild_id)
    initialiser_profil(joueur_id, profils)
    profils[str(joueur_id)]["mission_a_refaire"] = {"texte": texte, "delai": delai_texte, "cat": cat, "points": points}
    sauvegarder_profils(guild_id, profils)

def obtenir_mission_a_refaire(guild_id, joueur_id):
    profils = charger_profils(guild_id)
    profil = profils.get(str(joueur_id))
    if not profil:
        return None
    return profil.get("mission_a_refaire")

def effacer_mission_a_refaire(guild_id, joueur_id):
    profils = charger_profils(guild_id)
    profil = profils.get(str(joueur_id))
    if profil and "mission_a_refaire" in profil:
        del profil["mission_a_refaire"]
        sauvegarder_profils(guild_id, profils)

def extraire_duree(delai_texte):
    """Parse une durée à partir d'un texte libre.
    Accepte aussi bien les formats compacts ("2h", "3j", "45min")
    que les formats avec espace ("2 heures", "3 jours", "45 minutes")."""
    if not delai_texte:
        return timedelta(days=3)

    texte = delai_texte.lower().replace("pour dans", "").replace("pour", "").strip()
    texte = texte.replace(",", ".")

    # Capture un nombre (entier ou décimal) suivi (avec ou sans espace) d'une unité alphabétique
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*([a-zéèêûî]+)", texte)

    for valeur_str, unite in matches:
        try:
            valeur = float(valeur_str)
        except ValueError:
            continue

        if "min" in unite or unite == "mn":
            return timedelta(minutes=valeur)
        if unite == "h" or "heure" in unite or "hour" in unite:
            return timedelta(hours=valeur)
        if "semaine" in unite or "week" in unite or unite.startswith("sem"):
            return timedelta(weeks=valeur)
        if "mois" in unite or "month" in unite:
            return timedelta(days=valeur * 30)
        if unite == "j" or "jour" in unite or "day" in unite:
            return timedelta(days=valeur)

    return timedelta(days=3)

def barre_progression(temps_ecoule, duree_totale, longueur=12):
    """Construit une barre de progression textuelle (🟩/⬛) à partir du temps
    écoulé et de la durée totale d'une mission. Retourne (barre, ratio 0-1)."""
    if duree_totale.total_seconds() <= 0:
        ratio = 1.0
    else:
        ratio = temps_ecoule.total_seconds() / duree_totale.total_seconds()
    ratio = max(0.0, min(1.0, ratio))
    remplies = round(ratio * longueur)
    barre = "🟩" * remplies + "⬛" * (longueur - remplies)
    return barre, ratio

def formater_duree(delta):
    """Formate un timedelta en texte lisible 'Xj Xh Xmn Xs' (sans les unités à 0)."""
    total = int(max(0, delta.total_seconds()))
    jours, reste = divmod(total, 86400)
    heures, reste = divmod(reste, 3600)
    minutes, secondes = divmod(reste, 60)
    parts = []
    if jours: parts.append(f"{jours}j")
    if heures: parts.append(f"{heures}h")
    if minutes: parts.append(f"{minutes}mn")
    if not parts or secondes: parts.append(f"{secondes}s")
    return " ".join(parts)

missions_actives = {}

# Verrou partagé entre le thread Flask (site web) et le thread asyncio du
# bot : les deux threads peuvent lire/modifier missions_actives en même
# temps, ce qui n'est pas garanti thread-safe par défaut en Python.
# Toute suppression/modification de missions_actives depuis site_web.py
# ou depuis la boucle d'expiration automatique doit passer par ce verrou.
verrou_missions = threading.Lock()

TEXTE_ECHEC = (
    "⚖️ **[ ORDRE DE MISSION ÉCHOUÉ ]** ⚖️ \n"
    "**D'après l'article V — Rappel :**\n"
    "- **Refuser ou abandonner une mission attribuée sans raison valable peut être sanctionné.**\n"
    "- *L'État récompense l'investissement et la persévérance.*\n"
    "- *Les missions constituent l'un des principaux moyens de progresser au sein de Valerius.*"
)

# ================= HIÉRARCHIE : PROPRIÉTAIRE(S) & SUPER MODO(S) =================
# PROPRIÉTAIRE :
#   - Rang le plus élevé, global (tous les serveurs).
#   - Seul rang à recevoir l'intégralité des logs, toujours en message privé.
#   - Littéralement accès à tout ce que le bot peut faire, partout, y compris
#     le contournement du verrouillage par code.
#   - MAVIE7620 (PROPRIETAIRE_ID) est propriétaire par défaut et ne peut pas
#     être retiré. D'autres comptes peuvent être promus/rétrogradés par un
#     propriétaire existant via /ajouter_proprietaire et /retirer_proprietaire.
#
# SUPER MODO :
#   - Nommé par un propriétaire via /nommer_supermodo.
#   - Pouvoir de "staff" identique à un instructeur/admin, mais UNIQUEMENT
#     sur le serveur où il a été nommé — aucun accès ni aucun log sur les
#     autres serveurs.
PROPRIETAIRES_FILE = "valerius_proprietaires.json"
SUPERMODOS_FILE = "valerius_supermodos.json"

def charger_proprietaires():
    """Retourne l'ensemble des ID Discord ayant le rang Propriétaire.
    Le propriétaire historique (PROPRIETAIRE_ID) est toujours inclus."""
    proprietaires = {PROPRIETAIRE_ID}
    try:
        with open(PROPRIETAIRES_FILE, "r", encoding="utf-8") as f:
            proprietaires |= {int(x) for x in json.load(f).get("ids", [])}
    except Exception:
        pass
    return proprietaires

def sauvegarder_proprietaires(proprietaires):
    with open(PROPRIETAIRES_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": [i for i in proprietaires if i != PROPRIETAIRE_ID]}, f, ensure_ascii=False)

def est_proprietaire(user_id):
    return int(user_id) in charger_proprietaires()

def charger_supermodos():
    """dict {str(user_id): guild_id} — un Super Modo n'est valable que sur CE serveur."""
    try:
        with open(SUPERMODOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def sauvegarder_supermodos(supermodos):
    with open(SUPERMODOS_FILE, "w", encoding="utf-8") as f:
        json.dump(supermodos, f, ensure_ascii=False)

def est_super_modo(user_id, guild_id):
    if guild_id is None:
        return False
    return charger_supermodos().get(str(user_id)) == guild_id

class VueVerrouillable(discord.ui.View):
    """Vue de base : bloque automatiquement TOUS les boutons (pas seulement
    les commandes slash) tant que le serveur n'a pas saisi le bon code
    d'activation via /deverrouiller."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if est_proprietaire(interaction.user.id):
            return True
        if interaction.guild is not None and guilde_verrouillee(interaction.guild):
            try:
                await interaction.response.send_message(MESSAGE_VERROU, ephemeral=True)
            except Exception:
                pass
            return False
        return True

def verifier_permissions_staff(user):
    # Propriétaire : accès total, sur tous les serveurs, sans exception.
    if est_proprietaire(user.id):
        return True
    # Super Modo : mêmes droits que le staff, mais UNIQUEMENT sur le serveur
    # où il a été nommé (voir /nommer_supermodo).
    guild = getattr(user, "guild", None)
    if guild is not None and est_super_modo(user.id, guild.id):
        return True
    if not hasattr(user, "roles"):
        return False
    roles_noms = [r.name for r in user.roles]
    return user.guild_permissions.administrator or "[ [ 𝔦𝔫𝔰𝔱𝔯𝔲𝔠𝔱𝔢𝔲𝔯 ] ]" in roles_noms or "[ Palais Royal ]" in roles_noms or "Palais Royal" in roles_noms or any(r.permissions.manage_channels or r.permissions.administrator for r in user.roles)

LOGS_FILE = "valerius_logs_globaux.jsonl"
LOGS_MAX_CONSERVES = 500

def sauvegarder_log_disque(texte_log):
    """Ajoute une ligne au fichier de logs (persiste entre redémarrages) et
    tronque le fichier pour ne garder que les LOGS_MAX_CONSERVES plus récents."""
    try:
        ligne = json.dumps({"date": datetime.now().strftime("%d/%m/%Y à %H:%M:%S"), "texte": texte_log}, ensure_ascii=False)
        with open(LOGS_FILE, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            lignes = f.readlines()
        if len(lignes) > LOGS_MAX_CONSERVES:
            with open(LOGS_FILE, "w", encoding="utf-8") as f:
                f.writelines(lignes[-LOGS_MAX_CONSERVES:])
    except Exception as e:
        print(f"[LOGS DISQUE] Erreur d'écriture : {e}")

def charger_logs_recents(limite=200):
    """Renvoie les logs les plus récents en premier (liste de dicts date/texte)."""
    if not os.path.exists(LOGS_FILE):
        return []
    try:
        with open(LOGS_FILE, "r", encoding="utf-8") as f:
            lignes = f.readlines()
    except Exception:
        return []
    logs = []
    for ligne in reversed(lignes[-limite:]):
        ligne = ligne.strip()
        if not ligne: continue
        try:
            logs.append(json.loads(ligne))
        except Exception:
            continue
    return logs

async def envoyer_log_proprietaire(bot_instance, texte_log, view=None, guild_target=None, joueur_id_target=None):
    """Envoie le log complet en message privé à TOUS les comptes Propriétaires,
    et seulement à eux — c'est le seul rang à recevoir l'intégralité des logs.
    Le log est aussi toujours persisté sur disque pour être consultable sur le site."""
    sauvegarder_log_disque(texte_log)
    au_moins_un_envoye = False
    for owner_id in charger_proprietaires():
        membre = bot_instance.get_user(owner_id)
        if not membre:
            try:
                membre = await bot_instance.fetch_user(owner_id)
            except Exception:
                membre = None

        if membre:
            try:
                v = view(guild_target, joueur_id_target) if (view and guild_target and joueur_id_target) else view
                await membre.send(f"📋 **[LOG GLOBAL ABSOLU - VALERIUS]** : {texte_log}", view=v)
                au_moins_un_envoye = True
            except Exception:
                pass

    if not au_moins_un_envoye:
        print(f"[LOG GLOBAL ABSOLU CONSOLE] {texte_log}")

async def envoyer_double_notification(guild, msg_ticket, msg_missions, view=None, joueur_id=None):
    salon_missions = guild.get_channel(SALON_VALIDATION_MISSION_ID) or discord.utils.get(guild.text_channels, name="validation-mission")
    if salon_missions:
        try: 
            v_obj = view(joueur_id) if (view and joueur_id and callable(view)) else view
            await salon_missions.send(msg_missions, view=v_obj)
        except Exception as e:
            print(f"Erreur envoi salon validation: {e}")
    
    await envoyer_log_proprietaire(bot, f"[{guild.name}] {msg_missions}", view=VueEvaluationMissionMP if view else None, guild_target=guild, joueur_id_target=joueur_id)

    # Toute notification liée à une mission (fin, succès, échec, demande de
    # validation...) est aussi déposée côté site web pour ce joueur, afin
    # d'alimenter la cloche 🔔 de notifications de son compte.
    if joueur_id is not None:
        ajouter_notification(guild.id, joueur_id, msg_missions, categorie="mission")

# ================= NOTIFICATIONS SITE WEB (cloche 🔔) =================
# Stockage simple, par serveur, des notifications destinées à un joueur
# précis, affichées côté site (icône cloche qui passe au rouge quand il y
# a du nouveau). Pour l'instant utilisé pour tout ce qui concerne les
# missions (fin de mission, succès/échec, demande de validation...), mais
# conçu pour être réutilisé par d'autres systèmes (rankup, etc.) via la
# `categorie` passée à ajouter_notification.

MAX_NOTIFICATIONS_PAR_JOUEUR = 100

def get_notifications_file(guild_id):
    return f"valerius_notifications_{guild_id}.json"

def charger_notifications(guild_id):
    file_name = get_notifications_file(guild_id)
    if not os.path.exists(file_name):
        return []
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sauvegarder_notifications(guild_id, notifications):
    with open(get_notifications_file(guild_id), "w", encoding="utf-8") as f:
        json.dump(notifications, f, indent=4, ensure_ascii=False)

def ajouter_notification(guild_id, joueur_id, texte, categorie="mission", lien=None):
    """Ajoute une notification pour `joueur_id`. `categorie` sert à choisir
    l'icône côté site (ex: 'mission' -> 🔔, 'rankup' -> 🎖️). Retourne
    l'entrée créée, ou None si joueur_id est vide."""
    if not joueur_id:
        return None
    notifications = charger_notifications(guild_id)
    entree = {
        "id": secrets.token_hex(8),
        "joueur_id": str(joueur_id),
        "texte": texte,
        "categorie": categorie,
        "lien": lien,
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
        "lu": False,
    }
    notifications.insert(0, entree)
    # Ne garde que les MAX_NOTIFICATIONS_PAR_JOUEUR plus récentes de CE
    # joueur, pour ne pas laisser le fichier grossir indéfiniment.
    ids_de_ce_joueur = [n["id"] for n in notifications if n["joueur_id"] == entree["joueur_id"]]
    if len(ids_de_ce_joueur) > MAX_NOTIFICATIONS_PAR_JOUEUR:
        a_retirer = set(ids_de_ce_joueur[MAX_NOTIFICATIONS_PAR_JOUEUR:])
        notifications = [n for n in notifications if n["id"] not in a_retirer]
    sauvegarder_notifications(guild_id, notifications)
    # Réveille en temps réel toute connexion ouverte sur le site (cloche 🔔)
    # pour ce joueur, sans attendre le prochain sondage automatique.
    try:
        site_web.notifier_maj_notifications(guild_id, joueur_id)
    except Exception as e:
        print(f"Erreur notification temps réel (cloche) : {e}")
    return entree

def obtenir_notifications(guild_id, joueur_id):
    return [n for n in charger_notifications(guild_id) if n["joueur_id"] == str(joueur_id)]

def compter_notifications_non_lues(guild_id, joueur_id):
    return sum(1 for n in obtenir_notifications(guild_id, joueur_id) if not n.get("lu"))

def marquer_notifications_lues(guild_id, joueur_id):
    notifications = charger_notifications(guild_id)
    joueur_id = str(joueur_id)
    modifie = False
    for n in notifications:
        if n["joueur_id"] == joueur_id and not n.get("lu"):
            n["lu"] = True
            modifie = True
    if modifie:
        sauvegarder_notifications(guild_id, notifications)
        try:
            site_web.notifier_maj_notifications(guild_id, joueur_id)
        except Exception as e:
            print(f"Erreur notification temps réel (cloche) : {e}")

# ---- Variantes multi-serveurs (cloche 🔔 des comptes Propriétaire) ----
# Un compte Propriétaire n'a pas forcément de guild_id unique assigné (il a
# accès à TOUS les serveurs) : ces variantes agrègent donc les notifications
# d'une liste de serveurs au lieu d'un seul, pour que sa cloche fonctionne
# elle aussi, comme demandé.

def obtenir_notifications_multi(guild_ids, joueur_id):
    """Comme obtenir_notifications, mais agrège plusieurs serveurs. Chaque
    notification renvoyée porte en plus son 'guild_id' d'origine."""
    resultat = []
    for guild_id in guild_ids:
        for n in obtenir_notifications(guild_id, joueur_id):
            n = dict(n)
            n["guild_id"] = guild_id
            resultat.append(n)

    def _clef_tri(n):
        try:
            return datetime.strptime(n["date"], "%d/%m/%Y à %H:%M")
        except Exception:
            return datetime.min

    resultat.sort(key=_clef_tri, reverse=True)
    return resultat

def compter_notifications_non_lues_multi(guild_ids, joueur_id):
    return sum(compter_notifications_non_lues(g, joueur_id) for g in guild_ids)

def marquer_notifications_lues_multi(guild_ids, joueur_id):
    for g in guild_ids:
        marquer_notifications_lues(g, joueur_id)

# ================= SYSTÈME DE BLÂMES — "OSIRIS" =================
# Module disciplinaire, distinct de la gestion des missions (Valerius).
# Règles :
#  - un blâme s'efface automatiquement 2 semaines après son ajout ;
#  - dès 2 blâmes actifs, un avertissement officiel est envoyé automatiquement
#    (un bouton permet aussi de le déclencher manuellement à tout moment) ;
#  - au-delà de 7 blâmes actifs (donc à partir du 8e), un procès est ouvert :
#    le Palais Royal est notifié dans son salon dédié.
# Le site web peut ajouter/retirer un blâme via /admin/blames/<guild_id>.

DUREE_EXPIRATION_BLAME = timedelta(days=14)
SEUIL_AVERTISSEMENT_BLAME = 2
SEUIL_PROCES_BLAME = 7

def get_blames_file(guild_id):
    return f"valerius_blames_{guild_id}.json"

def charger_blames(guild_id):
    file_name = get_blames_file(guild_id)
    if not os.path.exists(file_name): return []
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sauvegarder_blames(guild_id, blames):
    with open(get_blames_file(guild_id), "w", encoding="utf-8") as f:
        json.dump(blames, f, indent=4, ensure_ascii=False)

def nettoyer_blames_expires(guild_id):
    """Retire les blâmes vieux de plus de DUREE_EXPIRATION_BLAME (2 semaines).
    Retourne (blames_actifs, blames_expires_retires)."""
    blames = charger_blames(guild_id)
    limite = datetime.now() - DUREE_EXPIRATION_BLAME
    actifs, expires = [], []
    for b in blames:
        try:
            date_b = datetime.strptime(b.get("date", ""), "%d/%m/%Y à %H:%M")
        except (ValueError, TypeError):
            actifs.append(b)
            continue
        (actifs if date_b >= limite else expires).append(b)
    if expires:
        sauvegarder_blames(guild_id, actifs)
    return actifs, expires

def obtenir_blames_actifs(guild_id, joueur_id=None):
    actifs, _ = nettoyer_blames_expires(guild_id)
    if joueur_id is not None:
        return [b for b in actifs if str(b.get("joueur_id")) == str(joueur_id)]
    return actifs

def ajouter_blame(guild_id, joueur_id, raison, auteur_id):
    """Ajoute un blâme. Retourne (blame_créé, nombre_de_blâmes_actifs_du_joueur)."""
    actifs, _ = nettoyer_blames_expires(guild_id)
    nouveau = {
        "joueur_id": str(joueur_id),
        "raison": raison,
        "auteur_id": str(auteur_id),
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
    }
    actifs.append(nouveau)
    sauvegarder_blames(guild_id, actifs)
    nb_joueur = len([b for b in actifs if str(b["joueur_id"]) == str(joueur_id)])
    return nouveau, nb_joueur

def retirer_blame_par_index(guild_id, joueur_id, index):
    """Retire le blâme à la position `index` (0-based) dans la liste des
    blâmes actifs de CE joueur (même ordre que obtenir_blames_actifs).
    Retourne le blâme retiré, ou None si introuvable."""
    actifs, _ = nettoyer_blames_expires(guild_id)
    joueur_id = str(joueur_id)
    indices_joueur = [i for i, b in enumerate(actifs) if str(b.get("joueur_id")) == joueur_id]
    if not (0 <= index < len(indices_joueur)):
        return None
    i_reel = indices_joueur[index]
    retire = actifs.pop(i_reel)
    sauvegarder_blames(guild_id, actifs)
    return retire

def _lister_guildes_avec_fichier(prefixe, suffixe=".json"):
    ids = []
    try:
        for nom in os.listdir("."):
            if nom.startswith(prefixe) and nom.endswith(suffixe):
                coeur = nom[len(prefixe):-len(suffixe)]
                if coeur.isdigit():
                    ids.append(int(coeur))
    except Exception:
        pass
    return ids

def _resoudre_guild_osiris(guild):
    """Le système disciplinaire doit toujours agir en tant qu'Osiris, y
    compris quand ces fonctions sont appelées depuis le site web (qui ne
    connaît la guilde que via le cache de Valerius). On re-résout donc la
    guilde via bot_osiris avant tout envoi de MP ou de message de salon."""
    if guild is None:
        return None
    return bot_osiris.get_guild(guild.id) or guild

async def envoyer_notification_blame(guild, blame, nb_actifs):
    """Envoie un MP au joueur concerné avec le détail complet du blâme qu'il
    vient de recevoir (motif, auteur, date, nombre de blâmes actifs et date
    d'expiration automatique). Retourne True si le MP a pu être envoyé."""
    guild = _resoudre_guild_osiris(guild)
    if guild is None:
        return False
    joueur_id = blame.get("joueur_id")
    membre = guild.get_member(int(joueur_id)) if str(joueur_id).isdigit() else None
    if not membre:
        return False

    auteur_id = blame.get("auteur_id")
    auteur_membre = guild.get_member(int(auteur_id)) if str(auteur_id).isdigit() else None
    nom_auteur = auteur_membre.display_name if auteur_membre else "le Palais Royal (via le site web)"

    try:
        date_ajout = datetime.strptime(blame.get("date", ""), "%d/%m/%Y à %H:%M")
        date_expiration = (date_ajout + DUREE_EXPIRATION_BLAME).strftime("%d/%m/%Y à %H:%M")
    except (ValueError, TypeError):
        date_expiration = "dans 2 semaines"

    embed = discord.Embed(
        title="⚖️ Vous avez reçu un blâme — Osiris",
        description=f"Un blâme vient de vous être infligé sur **{guild.name}**.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(name="Motif", value=blame.get("raison", "Non précisé"), inline=False)
    embed.add_field(name="Infligé par", value=nom_auteur, inline=True)
    embed.add_field(name="Date", value=blame.get("date", "inconnue"), inline=True)
    embed.add_field(name="Blâmes actifs", value=f"**{nb_actifs}** — un procès s'ouvre au-delà de {SEUIL_PROCES_BLAME}", inline=False)
    embed.add_field(name="Expiration automatique", value=date_expiration, inline=False)
    embed.set_footer(text=f"{guild.name} • Système disciplinaire Osiris")

    try:
        await membre.send(embed=embed)
        return True
    except Exception:
        return False

class VueAvertirJoueur(VueVerrouillable):
    """Bouton permettant au staff d'envoyer un avertissement officiel à
    tout moment, indépendamment du seuil automatique de 2 blâmes."""
    def __init__(self, joueur_id=None):
        super().__init__(timeout=None)
        self.joueur_id = joueur_id

    @discord.ui.button(label="⚠️ Avertir maintenant", style=discord.ButtonStyle.danger, custom_id="valerius_avertir_manuel")
    async def avertir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
            return
        if not self.joueur_id:
            await interaction.response.send_message("❌ Impossible de retrouver le joueur concerné par ce blâme.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await envoyer_avertissement(interaction.guild, self.joueur_id, interaction.user)
        if ok:
            await interaction.followup.send(f"⚠️ Avertissement envoyé à <@{self.joueur_id}>.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Impossible d'envoyer l'avertissement (joueur introuvable ou MP fermés, et salon du Palais Royal introuvable).", ephemeral=True)

async def envoyer_avertissement(guild, joueur_id, auteur=None):
    """Envoie un avertissement (embed) en MP au joueur + log dans le salon
    du Palais Royal. Retourne True si au moins une notification est partie."""
    guild = _resoudre_guild_osiris(guild)
    if guild is None:
        return False
    envoye = False
    nb_actifs = len(obtenir_blames_actifs(guild.id, joueur_id))
    embed = discord.Embed(
        title="⚠️ Avertissement Officiel — Osiris",
        description=(
            f"Vous avez fait l'objet de **{nb_actifs} blâme(s)** actif(s) au sein du Royaume.\n"
            "Ceci est un avertissement formel : toute récidive pourra entraîner des sanctions plus lourdes, "
            "jusqu'au procès devant le Palais Royal."
        ),
        color=discord.Color.orange()
    )
    embed.set_footer(text=f"{guild.name} • Système disciplinaire Osiris")

    membre = guild.get_member(int(joueur_id)) if str(joueur_id).isdigit() else None
    if membre:
        try:
            await membre.send(embed=embed)
            envoye = True
        except Exception:
            pass

    salon_cible = guild.get_channel(SALON_PALAIS_ROYAL_ID)
    if salon_cible:
        try:
            mention_auteur = f" (déclenché par {auteur.mention})" if auteur else " (déclenché automatiquement)"
            await salon_cible.send(f"⚠️ Avertissement envoyé à <@{joueur_id}>{mention_auteur}.", embed=embed)
            envoye = True
        except Exception:
            pass

    await envoyer_log_proprietaire(bot_osiris, f"[{guild.name}] ⚠️ Avertissement envoyé à <@{joueur_id}> ({nb_actifs} blâme(s) actif(s)).")
    return envoye

async def declencher_proces(guild, joueur_id, blames_actifs):
    """Ouvre un procès : notifie et ping le Palais Royal dans son salon dédié."""
    guild = _resoudre_guild_osiris(guild)
    if guild is None:
        return
    role_palais = discord.utils.get(guild.roles, name="[ Palais Royal ]") or discord.utils.get(guild.roles, name="Palais Royal")
    salon_cible = guild.get_channel(SALON_PALAIS_ROYAL_ID)
    mention_role = role_palais.mention if role_palais else "@Palais Royal"

    embed = discord.Embed(
        title="🚨 Ouverture d'un Procès Royal",
        description=f"<@{joueur_id}> cumule désormais **{len(blames_actifs)} blâmes actifs** — le seuil de {SEUIL_PROCES_BLAME} est dépassé.",
        color=discord.Color.red()
    )
    liste_raisons = "\n".join(f"**{i}.** {b['raison']} *({b['date']})*" for i, b in enumerate(blames_actifs, start=1))
    if liste_raisons:
        embed.add_field(name="Motifs des blâmes", value=liste_raisons[:1024], inline=False)
    embed.set_footer(text=f"{guild.name} • Système disciplinaire Osiris")

    if salon_cible:
        try:
            await salon_cible.send(f"🚨 {mention_role} ! Un procès doit être ouvert contre <@{joueur_id}>.", embed=embed)
        except Exception as e:
            print(f"Erreur envoi procès: {e}")
    await envoyer_log_proprietaire(bot_osiris, f"[{guild.name}] 🚨 PROCÈS déclenché contre <@{joueur_id}> ({len(blames_actifs)} blâmes actifs).")

async def traiter_seuils_blame(guild, joueur_id):
    """À appeler après l'ajout d'un blâme (commande ou site web) : déclenche
    avertissement / procès si les seuils sont franchis."""
    actifs = obtenir_blames_actifs(guild.id, joueur_id)
    nb = len(actifs)
    if nb == SEUIL_AVERTISSEMENT_BLAME:
        await envoyer_avertissement(guild, joueur_id)
    if nb > SEUIL_PROCES_BLAME:
        await declencher_proces(guild, joueur_id, actifs)

# ================= SYSTÈME DE RANKUP / DÉRANK — "OSIRIS" =================
# Historique des changements de rang (promotions & rétrogradations), géré
# entièrement par Osiris (comme le système de blâmes). Chaque événement est
# conservé pour pouvoir être consulté avec /rangs.

def get_rankups_file(guild_id):
    return f"osiris_rankups_{guild_id}.json"

def charger_rankups(guild_id):
    file_name = get_rankups_file(guild_id)
    if not os.path.exists(file_name): return []
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sauvegarder_rankups(guild_id, rankups):
    with open(get_rankups_file(guild_id), "w", encoding="utf-8") as f:
        json.dump(rankups, f, indent=4, ensure_ascii=False)

def ajouter_rankup(guild_id, joueur_id, type_action, ancien_rang, nouveau_rang, auteur_id, raison=None):
    """type_action : 'promotion' ou 'retrogradation'. Retourne l'entrée créée."""
    rankups = charger_rankups(guild_id)
    entree = {
        "joueur_id": str(joueur_id),
        "type": type_action,
        "ancien_rang": ancien_rang,
        "nouveau_rang": nouveau_rang,
        "auteur_id": str(auteur_id),
        "raison": raison,
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
    }
    rankups.insert(0, entree)
    sauvegarder_rankups(guild_id, rankups)
    return entree

def obtenir_rankups(guild_id, joueur_id=None):
    rankups = charger_rankups(guild_id)
    if joueur_id is not None:
        return [r for r in rankups if str(r.get("joueur_id")) == str(joueur_id)]
    return rankups

def retirer_rankup_par_index(guild_id, joueur_id, index):
    """Retire l'entrée à la position `index` (0-based) dans l'historique de
    CE joueur (même ordre que obtenir_rankups, du plus récent au plus ancien)."""
    rankups = charger_rankups(guild_id)
    joueur_id = str(joueur_id)
    indices_joueur = [i for i, r in enumerate(rankups) if str(r.get("joueur_id")) == joueur_id]
    if not (0 <= index < len(indices_joueur)):
        return None
    i_reel = indices_joueur[index]
    retire = rankups.pop(i_reel)
    sauvegarder_rankups(guild_id, rankups)
    return retire

# ================= SYSTÈME DES RANGS DU ROYAUME — "SIRIUS" =================
# Catalogue des rangs (modifiable par les Propriétaires sur le site),
# demandes de rang faites par les joueurs (avec vérification automatique
# de certaines conditions à partir de l'historique du joueur), et page de
# traitement des demandes pour les instructeurs.

GROUPES_RANGS = ["Recrue", "Membre", "Officier", "Unique"]

def rangs_par_defaut():
    """Catalogue de départ, basé sur la hiérarchie fournie par l'utilisateur.
    Entièrement modifiable ensuite depuis /admin/rangs/<guild_id> (Propriétaire)."""
    return [
        {
            "id": "recrue", "nom": "RECRUE", "icone": "🪖", "groupe": "Recrue",
            "ordre": 0, "unique": False,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [],
                           "manuel": ["Rejoindre le royaume"]},
            "debloque": ["Un BK dans l'espace recrue", "Un stuff de départ", "L'accès au F home",
                         "L'accès au système de mission", "L'accès à l'entreprise agricole", "L'accès aux spawners"],
        },
        {
            "id": "ecuyer", "nom": "ECUYER", "icone": "🪖", "groupe": "Recrue",
            "ordre": 1, "unique": False,
            "conditions": {"semaines_min": 1, "blames_max": None, "missions_min": {},
                           "missions_alt": [{"commune": 3}, {"moyenne": 1}], "manuel": []},
            "debloque": ["Accès à la zone \"Potion\"", "Accès à l'entreprise de réparation",
                         "Accès à de plus grands BK (à venir)"],
        },
        {
            "id": "chevalier", "nom": "CHEVALIER", "icone": "🪖", "groupe": "Recrue",
            "ordre": 2, "unique": False,
            "conditions": {"semaines_min": 3, "blames_max": 3, "missions_min": {"commune": 5, "moyenne": 1},
                           "missions_alt": [], "manuel": []},
            "debloque": ["Accès au diplôme", "Accès à l'entreprise de build / terraforming", "La Banque"],
        },
        {
            "id": "lieutenant", "nom": "LIEUTENANT", "icone": "🛡️", "groupe": "Membre",
            "ordre": 3, "unique": False,
            "conditions": {"semaines_min": 5, "blames_max": None,
                           "missions_min": {"commune": 5, "moyenne": 3, "difficile": 1}, "missions_alt": [],
                           "manuel": ["Avoir l'approbation de la majorité des Haut-gradés",
                                      "Réussir un examen et passer un entretien",
                                      "Réussir l'examen \"Diplomatie 1\""]},
            "debloque": ["Le grade Haut-gradé", "Les métiers fondamentaux",
                         "Les permissions dans la base lunaire du royaume", "L'accès à l'entreprise de pétrole"],
        },
        {
            "id": "capitaine", "nom": "CAPITAINE", "icone": "🛡️", "groupe": "Membre",
            "ordre": 4, "unique": False,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [], "manuel": []},
            "debloque": ["BK avec un espace dédié aux panneaux solaires"],
        },
        {
            "id": "marechal", "nom": "MARÉCHAL", "icone": "🛡️", "groupe": "Membre",
            "ordre": 5, "unique": False,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [], "manuel": []},
            "debloque": [],
        },
        {
            "id": "duc", "nom": "DUC", "icone": "⚜️", "groupe": "Officier",
            "ordre": 6, "unique": False,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [], "manuel": []},
            "debloque": [],
        },
        {
            "id": "grand_duc", "nom": "GRAND-DUC", "icone": "⚜️", "groupe": "Officier",
            "ordre": 7, "unique": False,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [],
                           "manuel": ["Avoir une fusée personnelle"]},
            "debloque": [],
        },
        {
            "id": "archiduc", "nom": "ARCHIDUC", "icone": "👑", "groupe": "Unique",
            "ordre": 8, "unique": True,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [], "manuel": []},
            "debloque": [],
        },
        {
            "id": "roi", "nom": "ROI", "icone": "👑", "groupe": "Unique",
            "ordre": 9, "unique": True,
            "conditions": {"semaines_min": 0, "blames_max": None, "missions_min": {}, "missions_alt": [], "manuel": []},
            "debloque": [],
        },
    ]

def get_rangs_file(guild_id):
    return f"valerius_rangs_{guild_id}.json"

def charger_rangs(guild_id):
    file_name = get_rangs_file(guild_id)
    if not os.path.exists(file_name):
        rangs = rangs_par_defaut()
        sauvegarder_rangs(guild_id, rangs)
        return rangs
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return rangs_par_defaut()

def sauvegarder_rangs(guild_id, rangs):
    with open(get_rangs_file(guild_id), "w", encoding="utf-8") as f:
        json.dump(rangs, f, indent=4, ensure_ascii=False)

def obtenir_rang_par_id(guild_id, rang_id):
    return next((r for r in charger_rangs(guild_id) if r["id"] == rang_id), None)

def obtenir_rang_joueur(guild_id, joueur_id):
    """Rang actuel d'un joueur. Si aucun rang n'a jamais été attribué, il
    est considéré au rang le plus bas du catalogue (ordre minimal)."""
    profils = charger_profils(guild_id)
    profil = profils.get(str(joueur_id), {})
    rang_id = profil.get("rang")
    rangs = charger_rangs(guild_id)
    if rang_id:
        r = next((r for r in rangs if r["id"] == rang_id), None)
        if r:
            return r
    return min(rangs, key=lambda r: r["ordre"]) if rangs else None

def definir_rang_joueur(guild_id, joueur_id, rang_id):
    profils = charger_profils(guild_id)
    initialiser_profil(joueur_id, profils)
    profils[str(joueur_id)]["rang"] = rang_id
    sauvegarder_profils(guild_id, profils)

def compter_missions_reussies_par_categorie(guild_id, joueur_id):
    profils = charger_profils(guild_id)
    profil = profils.get(str(joueur_id), {})
    compteur = {"commune": 0, "moyenne": 0, "difficile": 0, "royal": 0}
    for entree in profil.get("historique", []):
        if entree.get("statut") == "Succès" and entree.get("categorie") in compteur:
            compteur[entree["categorie"]] += 1
    return compteur

def obtenir_semaines_anciennete(guild, joueur_id):
    """Ancienneté dans le royaume = date d'arrivée du membre sur le serveur
    Discord (approximation raisonnable de son temps de jeu réel)."""
    membre = guild.get_member(int(joueur_id)) if str(joueur_id).isdigit() else None
    if not membre or not membre.joined_at:
        return None
    delta = datetime.now(membre.joined_at.tzinfo) - membre.joined_at
    return delta.days // 7

def verifier_conditions_rang(guild, joueur_id, rang):
    """Vérifie automatiquement ce qui peut l'être (ancienneté, blâmes,
    missions réussies) à partir de l'historique du joueur. Le reste
    (approbations, examens, entretiens, objets possédés en jeu...) est
    listé sous 'manuel', à vérifier par un instructeur."""
    conditions = rang.get("conditions", {})
    rapport = {"auto": [], "manuel": [], "toutes_auto_ok": True}

    semaines_min = conditions.get("semaines_min") or 0
    if semaines_min:
        semaines = obtenir_semaines_anciennete(guild, joueur_id)
        ok = semaines is not None and semaines >= semaines_min
        rapport["auto"].append({"libelle": f"Avoir au moins {semaines_min} semaine(s) de jeu dans le royaume",
                                 "ok": ok, "valeur_actuelle": semaines})
        rapport["toutes_auto_ok"] = rapport["toutes_auto_ok"] and ok

    blames_max = conditions.get("blames_max")
    if blames_max is not None:
        nb_blames = len(obtenir_blames_actifs(guild.id, joueur_id))
        ok = nb_blames <= blames_max
        rapport["auto"].append({"libelle": f"Avoir au maximum {blames_max} blâme(s) actif(s)",
                                 "ok": ok, "valeur_actuelle": nb_blames})
        rapport["toutes_auto_ok"] = rapport["toutes_auto_ok"] and ok

    compteur = compter_missions_reussies_par_categorie(guild.id, joueur_id)
    missions_min = conditions.get("missions_min") or {}
    for cat, minimum in missions_min.items():
        if minimum:
            ok = compteur.get(cat, 0) >= minimum
            rapport["auto"].append({"libelle": f"Avoir réussi au moins {minimum} mission(s) {cat}(s)",
                                     "ok": ok, "valeur_actuelle": compteur.get(cat, 0)})
            rapport["toutes_auto_ok"] = rapport["toutes_auto_ok"] and ok

    missions_alt = conditions.get("missions_alt") or []
    if missions_alt:
        resultats_alt = [all(compteur.get(cat, 0) >= mini for cat, mini in combi.items()) for combi in missions_alt]
        alt_ok = any(resultats_alt)
        libelle_alt = " OU ".join(
            " et ".join(f"{mini} {cat}(s)" for cat, mini in combi.items()) for combi in missions_alt
        )
        rapport["auto"].append({"libelle": f"Avoir réussi au moins : {libelle_alt}",
                                 "ok": alt_ok, "valeur_actuelle": compteur})
        rapport["toutes_auto_ok"] = rapport["toutes_auto_ok"] and alt_ok

    for texte in conditions.get("manuel", []):
        rapport["manuel"].append({"libelle": texte})

    return rapport

def get_demandes_rang_file(guild_id):
    return f"valerius_demandes_rang_{guild_id}.json"

def charger_demandes_rang(guild_id):
    file_name = get_demandes_rang_file(guild_id)
    if not os.path.exists(file_name):
        return []
    try:
        with open(file_name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def sauvegarder_demandes_rang(guild_id, demandes):
    with open(get_demandes_rang_file(guild_id), "w", encoding="utf-8") as f:
        json.dump(demandes, f, indent=4, ensure_ascii=False)

def _notifier_instructeurs_nouvelle_demande(guild_id, joueur_id, rang):
    """Dépose une notification (cloche du site) chez chaque instructeur/
    propriétaire de ce serveur pour prévenir d'une nouvelle demande de rang."""
    try:
        comptes = site_web.charger_comptes()
    except Exception:
        return
    for compte in comptes.values():
        role = compte.get("role")
        if role not in ("instructeur", "proprietaire"):
            continue
        if role != "proprietaire" and str(compte.get("guild_id")) != str(guild_id):
            continue
        cible = compte.get("discord_id")
        if cible and str(cible) != str(joueur_id):
            ajouter_notification(guild_id, cible,
                f"📥 Nouvelle demande de rang : <@{joueur_id}> souhaite devenir {rang['nom']}.",
                categorie="rankup")

def _poster_discord_sync(coro_factory):
    """Planifie une coroutine sur la boucle de Sirius depuis un contexte
    synchrone (les routes Flask du site le sont). N'échoue jamais bruyamment
    si Sirius n'est pas connecté : le site reste utilisable sans lui."""
    try:
        future = asyncio.run_coroutine_threadsafe(coro_factory(), bot_rangs.loop)
        future.result(timeout=10)
    except Exception as e:
        print(f"Erreur planification message Sirius: {e}")

def _texte_decret_royal(joueur_texte, rang_texte):
    """Texte officiel du Décret Royal de promotion (même formulation partout :
    /rankup ET acceptation d'une demande de rang). `joueur_texte` et
    `rang_texte` sont déjà formatés pour Discord (mention <@id> ou
    .mention, nom du rang en gras ou mention de rôle)."""
    return (
        "◈═══════◈ ◈═══════◈ 𝔇é𝔠𝔯𝔢𝔱 ℜ𝔬𝔶𝔞𝔩 ◈═══════◈ ◈═══════◈\n\n"
        f"𝔓𝔬𝔲𝔯 𝔰𝔬𝔫 𝔢𝔫𝔤𝔞𝔤𝔢𝔪𝔢𝔫𝔱, 𝔰𝔞 𝔩𝔬𝔶𝔞𝔲𝔱é 𝔢𝔱 𝔰𝔢𝔰 𝔰𝔢𝔯𝔳𝔦𝔠𝔢𝔰 𝔢𝔫𝔳𝔢𝔯𝔰 𝔩𝔢 ℜ𝔬𝔶𝔞𝔲𝔪𝔢, {joueur_texte} 𝔢𝔰𝔱 𝔬𝔣𝔣𝔦𝔠𝔦𝔢𝔩𝔩𝔢𝔪𝔢𝔫𝔱 𝔭𝔯𝔬𝔪𝔲 𝔞𝔲 𝔯𝔞𝔫𝔤 𝔡𝔢 {rang_texte} .\n\n"
        f"𝔔𝔲𝔢 𝔠𝔢𝔱𝔱𝔢 𝔭𝔯𝔬𝔪𝔬𝔱𝔦𝔬𝔫 𝔰𝔬𝔦𝔱 𝔭𝔬𝔯𝔱é𝔢 𝔞𝔳𝔢𝔠 𝔥𝔬𝔫𝔫𝔢𝔲𝔯 𝔢𝔱 𝔪𝔞𝔯𝔮𝔲𝔢 𝔩𝔢 𝔡é𝔟𝔲𝔱 𝔡𝔢 𝔫𝔬𝔲𝔳𝔢𝔩𝔩𝔢𝔰 𝔯𝔢𝔰𝔭𝔬𝔫𝔰𝔞𝔟𝔦𝔩𝔦𝔱é𝔰. 𝔉é𝔩𝔦𝔠𝔦𝔱𝔞𝔱𝔦𝔬𝔫𝔰 à {joueur_texte}!\n\n"
        "𝔥𝔞𝔰𝔦𝔫𝔞 𝔥𝔬 𝔞𝔫'𝔫𝔶 𝔉𝔞𝔫𝔧𝔞𝔨𝔞𝔫𝔞! 𝔊𝔩𝔬𝔦𝔯𝔢 𝔞𝔲 ℜ𝔬𝔶𝔞𝔲𝔪𝔢."
    )

def annoncer_nouvelle_demande_rang(guild_id, joueur_id, rang_id, demande):
    """Les demandes de rang ne sont volontairement PLUS publiées sur Discord
    (plus aucun embed dans SALON_DEMANDES_RANG_ID) : seul le staff est prévenu
    via la cloche de notifications du site. Seule une demande ACCEPTÉE (voir
    annoncer_decision_rang) ou un /rankup manuel affichent le Décret Royal."""
    rang = obtenir_rang_par_id(guild_id, rang_id)
    if not rang:
        return
    _notifier_instructeurs_nouvelle_demande(guild_id, joueur_id, rang)

def creer_demande_rang(guild_id, joueur_id, rang_id, motivation):
    guild = bot.get_guild(int(guild_id))
    rang = obtenir_rang_par_id(guild_id, rang_id)
    if not rang:
        return None
    rapport_auto = verifier_conditions_rang(guild, joueur_id, rang) if guild else {"auto": [], "manuel": [], "toutes_auto_ok": False}
    demandes = charger_demandes_rang(guild_id)
    entree = {
        "id": secrets.token_hex(8),
        "joueur_id": str(joueur_id),
        "rang_id": rang_id,
        "motivation": motivation,
        "date": datetime.now().strftime("%d/%m/%Y à %H:%M"),
        "statut": "en_attente",
        "rapport_auto": rapport_auto,
        "traite_par": None,
        "date_traitement": None,
        "commentaire": None,
    }
    demandes.insert(0, entree)
    sauvegarder_demandes_rang(guild_id, demandes)
    annoncer_nouvelle_demande_rang(guild_id, joueur_id, rang_id, entree)
    return entree

def obtenir_demandes_rang(guild_id, statut=None, joueur_id=None):
    demandes = charger_demandes_rang(guild_id)
    if statut:
        demandes = [d for d in demandes if d["statut"] == statut]
    if joueur_id is not None:
        demandes = [d for d in demandes if d["joueur_id"] == str(joueur_id)]
    return demandes

def obtenir_demande_rang_par_id(guild_id, demande_id):
    return next((d for d in charger_demandes_rang(guild_id) if d["id"] == demande_id), None)

def annoncer_decision_rang(guild_id, joueur_id, rang, decision, commentaire=None):
    """Seule une demande ACCEPTÉE publie le Décret Royal dans le salon
    (SALON_DEMANDES_RANG_ID), avec exactement le même texte que /rankup.
    Un refus ne s'affiche JAMAIS sur Discord : le joueur est prévenu
    uniquement via la notification du site (voir traiter_demande_rang)."""
    if decision != "accepte":
        return
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return

    async def _envoyer():
        salon = guild.get_channel(SALON_DEMANDES_RANG_ID)
        if not salon:
            return
        message_decret = _texte_decret_royal(f"<@{joueur_id}>", f"**{rang['nom']}**")
        try:
            await salon.send(message_decret)
        except Exception as e:
            print(f"Erreur annonce décision de rang: {e}")

    _poster_discord_sync(_envoyer)

def traiter_demande_rang(guild_id, demande_id, decision, instructeur_id, commentaire=None, forcer=False):
    """decision : 'accepte' ou 'refuse'. Si acceptée, applique le rankup et
    change le rang effectif du joueur — mais SEULEMENT si le joueur remplit
    bien toutes les conditions automatiques (ancienneté, blâmes, missions)
    du rang demandé. Cette vérification est refaite ICI, au moment de la
    décision (et pas seulement recopiée depuis la création de la demande),
    pour refléter la situation la plus à jour du joueur : par exemple s'il
    a fini une mission ou reçu un blâme depuis qu'il a fait sa demande.

    Si une condition automatique n'est pas remplie et que `forcer` n'est
    pas True, la demande N'EST PAS modifiée (elle reste "en_attente") et la
    fonction renvoie la chaîne "conditions_non_remplies" (à distinguer de
    None = demande introuvable/déjà traitée) : à charge de l'appelant
    d'avertir l'instructeur et de lui proposer, s'il le souhaite vraiment,
    de forcer la promotion en connaissance de cause (via `forcer=True`).
    Les conditions "manuelles" (entretien, examen...) ne sont, elles,
    jamais vérifiables automatiquement : elles n'entrent pas dans ce blocage.

    Retourne la demande mise à jour, "conditions_non_remplies", ou None."""
    demandes = charger_demandes_rang(guild_id)
    demande = next((d for d in demandes if d["id"] == demande_id), None)
    if not demande or demande["statut"] != "en_attente":
        return None
    rang = obtenir_rang_par_id(guild_id, demande["rang_id"])
    if not rang:
        return None

    if decision == "accepte":
        guild = bot.get_guild(int(guild_id))
        rapport_frais = (
            verifier_conditions_rang(guild, demande["joueur_id"], rang)
            if guild else demande.get("rapport_auto", {"toutes_auto_ok": False})
        )
        # On met à jour le rapport stocké avec cette vérification fraîche,
        # pour que l'historique reflète la situation réelle au moment de la
        # décision (et pas seulement celle du jour de la demande).
        demande["rapport_auto"] = rapport_frais
        if not rapport_frais.get("toutes_auto_ok", False) and not forcer:
            sauvegarder_demandes_rang(guild_id, demandes)
            return "conditions_non_remplies"

    demande["statut"] = decision
    demande["traite_par"] = str(instructeur_id)
    demande["date_traitement"] = datetime.now().strftime("%d/%m/%Y à %H:%M")
    demande["commentaire"] = commentaire
    sauvegarder_demandes_rang(guild_id, demandes)

    if decision == "accepte":
        ancien_rang = obtenir_rang_joueur(guild_id, demande["joueur_id"])
        definir_rang_joueur(guild_id, demande["joueur_id"], rang["id"])
        ajouter_rankup(guild_id, demande["joueur_id"], "promotion",
                       ancien_rang["nom"] if ancien_rang else None, rang["nom"],
                       instructeur_id, raison="Demande de rang validée")
        ajouter_notification(guild_id, demande["joueur_id"],
            f"🎖️ Ta demande pour devenir {rang['nom']} a été **acceptée** !", categorie="rankup")
    else:
        ajouter_notification(guild_id, demande["joueur_id"],
            f"📋 Ta demande pour devenir {rang['nom']} a été refusée." + (f" Motif : {commentaire}" if commentaire else ""),
            categorie="rankup")
        # On efface l'indicateur d'éligibilité pour ce rang : si le joueur
        # remplit toujours les conditions automatiques, la boucle proactive
        # (voir _notifier_joueurs_eligibles_rang) pourra le reprévenir plus
        # tard, au lieu de rester silencieuse pour toujours après un refus.
        profils = charger_profils(guild_id)
        profil = profils.get(str(demande["joueur_id"]))
        if profil and profil.get("eligibilite_notifiee") == rang["id"]:
            profil["eligibilite_notifiee"] = None
            sauvegarder_profils(guild_id, profils)

    annoncer_decision_rang(guild_id, demande["joueur_id"], rang, decision, commentaire)
    return demande

@tasks.loop(hours=2)
async def verifier_blames_expires_periodique():
    for g_id in _lister_guildes_avec_fichier("valerius_blames_"):
        nettoyer_blames_expires(g_id)

@verifier_blames_expires_periodique.before_loop
async def avant_verifier_blames_expires_periodique():
    await bot.wait_until_ready()

@verifier_blames_expires_periodique.error
async def verifier_blames_expires_periodique_erreur(erreur):
    print(f"[BLÂMES] Erreur boucle de nettoyage : {erreur}")

# ================= RAPPELS AUTOMATIQUES (cloche 🔔 + MP) =================
# Étend le système de notifications existant (ajouter_notification) avec de
# vrais rappels proactifs, envoyés une seule fois par événement (pas de
# spam) grâce à un indicateur posé sur l'élément concerné une fois le
# rappel envoyé :
#  - mission bientôt expirée (encore active, plus assez de temps restant) ;
#  - demande de rang en attente depuis trop longtemps (relance le staff) ;
#  - joueur devenu éligible à un nouveau rang (le prévient, mais NE dépose
#    JAMAIS de candidature à sa place — voir _notifier_joueurs_eligibles_rang).

SEUIL_RAPPEL_MISSION = timedelta(hours=3)  # rappel envoyé une fois passé ce seuil de temps restant
SEUIL_RAPPEL_DEMANDE_RANG = timedelta(days=2)  # relance si la demande attend depuis plus longtemps
DELAI_ENTRE_RELANCES_DEMANDE_RANG = timedelta(days=2)  # espace les relances suivantes

async def _rappeler_missions_bientot_expirees():
    """Envoie un MP (+ notification site) au joueur dont la mission active
    passe sous SEUIL_RAPPEL_MISSION de temps restant, une seule fois par
    mission (indicateur 'rappel_expiration_envoye' sur l'entrée en mémoire)."""
    maintenant = datetime.now()
    for guild_id, j_dict in list(missions_actives.items()):
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        for joueur_id, m_info in list(j_dict.items()):
            try:
                if m_info.get("en_attente", False) or m_info.get("rappel_expiration_envoye"):
                    continue
                temps_restant = m_info["date_fin"] - maintenant
                if timedelta(0) < temps_restant <= SEUIL_RAPPEL_MISSION:
                    m_info["rappel_expiration_envoye"] = True
                    texte = (
                        f"⏳ Ta mission *\"{m_info['texte']}\"* expire bientôt "
                        f"(il te reste {formater_duree(temps_restant)}) ! Pense à la valider à temps."
                    )
                    membre = guild.get_member(int(joueur_id)) if str(joueur_id).isdigit() else None
                    if membre:
                        try:
                            await membre.send(f"⏳ **Rappel — Valerius**\n{texte}")
                        except Exception as e:
                            print(f"[RAPPELS] MP impossible pour {joueur_id} (mission bientôt expirée) : {e}")
                    ajouter_notification(guild_id, joueur_id, texte, categorie="rappel")
            except Exception as e:
                print(f"[RAPPELS] Erreur sur guild={guild_id} joueur={joueur_id} (mission) : {e}")
                continue

async def _rappeler_demandes_rang_en_attente():
    """Relance le staff (salon des demandes + cloche des instructeurs) pour
    toute demande de rang encore 'en_attente' depuis plus de
    SEUIL_RAPPEL_DEMANDE_RANG, en espaçant les relances suivantes de
    DELAI_ENTRE_RELANCES_DEMANDE_RANG (indicateur 'dernier_rappel' sur la
    demande, persisté sur disque)."""
    maintenant = datetime.now()
    for guild_id in _lister_guildes_avec_fichier("valerius_demandes_rang_"):
        try:
            demandes = charger_demandes_rang(guild_id)
        except Exception as e:
            print(f"[RAPPELS] Erreur chargement demandes de rang guild={guild_id} : {e}")
            continue
        modifie = False
        for demande in demandes:
            try:
                if demande.get("statut") != "en_attente":
                    continue
                date_creation = datetime.strptime(demande["date"], "%d/%m/%Y à %H:%M")
                dernier_rappel = demande.get("dernier_rappel")
                reference = datetime.fromisoformat(dernier_rappel) if dernier_rappel else date_creation
                seuil = DELAI_ENTRE_RELANCES_DEMANDE_RANG if dernier_rappel else SEUIL_RAPPEL_DEMANDE_RANG
                if maintenant - reference < seuil:
                    continue
                rang = obtenir_rang_par_id(guild_id, demande["rang_id"])
                nom_rang = rang["nom"] if rang else demande["rang_id"]
                jours_attente = (maintenant - date_creation).days
                _notifier_instructeurs_nouvelle_demande(
                    guild_id, demande["joueur_id"],
                    {"nom": f"{nom_rang} (⏰ en attente depuis {jours_attente} jour(s))"},
                )
                demande["dernier_rappel"] = maintenant.isoformat()
                modifie = True
            except Exception as e:
                print(f"[RAPPELS] Erreur sur une demande de rang guild={guild_id} : {e}")
                continue
        if modifie:
            sauvegarder_demandes_rang(guild_id, demandes)

def _a_deja_une_demande_en_attente(guild_id, joueur_id, rang_id):
    """Vrai si le joueur a déjà une demande de rang 'en_attente' pour ce
    rang précis (inutile de le prévenir s'il a déjà postulé)."""
    return any(
        d.get("joueur_id") == str(joueur_id) and d.get("rang_id") == rang_id and d.get("statut") == "en_attente"
        for d in charger_demandes_rang(guild_id)
    )

async def _notifier_joueurs_eligibles_rang():
    """Parcourt tous les joueurs de chaque serveur et, dès que l'un d'eux
    remplit TOUTES les conditions automatiquement vérifiables (ancienneté,
    blâmes, missions...) pour son prochain rang, le prévient (MP + cloche 🔔
    du site) qu'il peut désormais postuler.

    IMPORTANT : ceci ne dépose PAS de candidature à sa place — la demande de
    rang reste à faire par le joueur lui-même (bouton du site / commande) ;
    Sirius se contente de le prévenir qu'il est temps de le faire. Un
    indicateur 'eligibilite_notifiee' (persisté sur le profil) empêche de le
    reprévenir en boucle pour le même rang ; il est remis à zéro dès que le
    rang visé change (promotion) ou qu'une demande refusée le laisse encore
    éligible (voir traiter_demande_rang)."""
    for guild_id in _lister_guildes_avec_fichier("valerius_profils_"):
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        try:
            rangs = sorted(charger_rangs(guild_id), key=lambda r: r["ordre"])
            profils = charger_profils(guild_id)
        except Exception as e:
            print(f"[RAPPELS] Erreur chargement rangs/profils guild={guild_id} : {e}")
            continue
        if not rangs:
            continue
        modifie = False
        for joueur_id, profil in list(profils.items()):
            try:
                rang_actuel = obtenir_rang_joueur(guild_id, joueur_id)
                if not rang_actuel:
                    continue
                suivants = [r for r in rangs if r["ordre"] > rang_actuel["ordre"] and not r.get("unique")]
                if not suivants:
                    continue
                prochain = suivants[0]
                if profil.get("eligibilite_notifiee") == prochain["id"]:
                    continue  # déjà prévenu pour ce rang précis
                rapport = verifier_conditions_rang(guild, joueur_id, prochain)
                if not rapport["toutes_auto_ok"]:
                    continue
                if _a_deja_une_demande_en_attente(guild_id, joueur_id, prochain["id"]):
                    profil["eligibilite_notifiee"] = prochain["id"]
                    modifie = True
                    continue
                texte = (
                    f"🌟 Tu remplis désormais toutes les conditions automatiques pour "
                    f"devenir **{prochain['nom']}** ! Ta candidature n'est pas envoyée "
                    f"automatiquement : pense à déposer ta demande de rang toi-même "
                    f"(site ou commande dédiée)."
                )
                membre = guild.get_member(int(joueur_id)) if str(joueur_id).isdigit() else None
                if membre:
                    try:
                        await membre.send(f"🌟 **Sirius — Système des rangs**\n{texte}")
                    except Exception as e:
                        print(f"[RAPPELS] MP impossible pour {joueur_id} (éligibilité rang) : {e}")
                ajouter_notification(guild_id, joueur_id, texte, categorie="rankup")
                profil["eligibilite_notifiee"] = prochain["id"]
                modifie = True
            except Exception as e:
                print(f"[RAPPELS] Erreur sur une éligibilité de rang guild={guild_id} joueur={joueur_id} : {e}")
                continue
        if modifie:
            sauvegarder_profils(guild_id, profils)

@tasks.loop(minutes=30)
async def verifier_rappels_periodique():
    await _rappeler_missions_bientot_expirees()
    await _rappeler_demandes_rang_en_attente()
    await _notifier_joueurs_eligibles_rang()

@verifier_rappels_periodique.before_loop
async def avant_verifier_rappels_periodique():
    await bot.wait_until_ready()

@verifier_rappels_periodique.error
async def verifier_rappels_periodique_erreur(erreur):
    print(f"[RAPPELS] La boucle a planté et va être redémarrée : {erreur}")
    if not verifier_rappels_periodique.is_running():
        verifier_rappels_periodique.start()

class VueFermerTicket(VueVerrouillable):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Fermer le ticket", style=discord.ButtonStyle.danger, custom_id="btn_fermer_ticket")
    async def fermer_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - Clic bouton Fermer le ticket par {interaction.user.name} dans {interaction.channel.name} ({interaction.guild.name})")
        await interaction.response.send_message("⚙️ Suppression du salon en cours...", ephemeral=True)
        
        g_id = interaction.guild.id
        if g_id in missions_actives:
            for j_id, m_info in list(missions_actives[g_id].items()):
                if m_info.get("channel_id") == interaction.channel.id:
                    del missions_actives[g_id][j_id]
                    break

        try: await interaction.channel.delete()
        except Exception: pass

class VueButinRecupere(VueVerrouillable):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📦 Butin récupéré", style=discord.ButtonStyle.primary, custom_id="btn_butin_recupere")
    async def butin_recupere(self, interaction: discord.Interaction, button: discord.ui.Button):
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - Clic bouton Butin récupéré par {interaction.user.name} dans {interaction.channel.name} ({interaction.guild.name})")
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        await interaction.channel.send("✅ **Le butin a été récupéré avec succès par l'instructeur.**", view=VueFermerTicket())

class VueAccueilArrivant(VueVerrouillable):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="👑 Je suis greyjoy", style=discord.ButtonStyle.danger, custom_id="btn_greyjoy")
    async def greyjoy_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_palais = discord.utils.get(interaction.guild.roles, name="[ Palais Royal ]") or discord.utils.get(interaction.guild.roles, name="Palais Royal")
        salon_cible = interaction.guild.get_channel(SALON_PALAIS_ROYAL_ID)
        
        mention_role = role_palais.mention if role_palais else "@[ Palais Royal ]"
        utilisateur = interaction.user
        
        if salon_cible:
            try:
                await salon_cible.send(f"🚨 {mention_role} ! Le membre {utilisateur.mention} ({utilisateur.name}) s'identifie en tant que Greyjoy.")
                await interaction.response.send_message(f"✅ Un haut gradé a été prévenu dans le salon {salon_cible.mention} !", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Erreur lors de l'envoi du message dans le salon dédié : {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"🚨 Un haut gradé a été ping : {mention_role} ! Le membre {utilisateur.mention} ({utilisateur.name}) s'identifie en tant que Greyjoy. (Salon cible introuvable par ID)", ephemeral=True)

    @discord.ui.button(label="👤 Je suis un visiteurs", style=discord.ButtonStyle.secondary, custom_id="btn_visiteur")
    async def visiteur_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_etranger = discord.utils.get(interaction.guild.roles, name="[💥] Etranger [💥]") or discord.utils.get(interaction.guild.roles, name="etranger")
        if role_etranger:
            try:
                await interaction.user.add_roles(role_etranger)
                await interaction.response.send_message(f"✅ Rôle **{role_etranger.name}** attribué avec succès !", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Erreur lors de l'attribution du rôle : {e}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Le rôle `[💥] Etranger [💥]` est introuvable sur ce serveur. Contactez un admin.", ephemeral=True)

    @discord.ui.button(label="⚔️ Je souhaite etre recruter", style=discord.ButtonStyle.success, custom_id="btn_recrutement")
    async def recrutement_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_recrutement = discord.utils.get(interaction.guild.roles, name="en cours de recrutement")
        salon_attente = interaction.guild.get_channel(ATTENTE_MOOV_ID) or discord.utils.get(interaction.guild.text_channels, name="attente-moov")
        
        if not role_recrutement:
            await interaction.response.send_message("❌ Le rôle `en cours de recrutement` est introuvable sur ce serveur.", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role_recrutement)
            
            if salon_attente:
                await salon_attente.set_permissions(interaction.user, read_messages=True, send_messages=True, connect=True)
                await interaction.response.send_message(f"✅ Tu as obtenu le rôle **en cours de recrutement** et l'accès au salon {salon_attente.mention} !", ephemeral=True)
            else:
                await interaction.response.send_message("✅ Rôle attribué, mais le salon `attente moov` est introuvable avec cet ID.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Une erreur est survenue : {e}", ephemeral=True)

class VueGestionJoueurMission(VueVerrouillable):
    def __init__(self, joueur_id=None):
        super().__init__(timeout=None)
        self.joueur_id = joueur_id

    @discord.ui.button(label="🏁 Finir la mission", style=discord.ButtonStyle.success, custom_id="joueur_finir_mission")
    async def joueur_finir(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_id = interaction.guild.id
        current_joueur_id = self.joueur_id
        if not current_joueur_id and g_id in missions_actives:
            for j_id, m_info in missions_actives[g_id].items():
                if m_info.get("channel_id") == interaction.channel.id:
                    current_joueur_id = j_id
                    break

        if current_joueur_id and interaction.user.id != current_joueur_id and not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Cet objectif ne t'appartient pas.", ephemeral=True)
            return

        target_id = current_joueur_id if current_joueur_id else interaction.user.id
        if g_id not in missions_actives or target_id not in missions_actives[g_id]:
            await interaction.response.send_message("❌ Tu n'as aucune mission active sur ce serveur.", ephemeral=True)
            return

        m_info = missions_actives[g_id][target_id]
        if not m_info.get("en_attente", False):
            m_info["en_attente"] = True
            m_info["moment_gel"] = datetime.now()

        for child in self.children: child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await interaction.response.defer(ephemeral=True)

        role_instructeur = discord.utils.get(interaction.guild.roles, name="[ 🎴[Instruction] ]")
        mention_ins = role_instructeur.mention if role_instructeur else '@[ 🎴[Instruction] ]'

        member_obj = interaction.guild.get_member(target_id)
        if member_obj:
            await interaction.channel.set_permissions(member_obj, read_messages=True, send_messages=False)
        
        await interaction.channel.send(f"💬 <@{target_id}>, un instructeur a été notifié. Votre demande va être traitée dans les plus brefs délais.")
        
        msg_fin = (
            f"📢 {mention_ins} ! <@{target_id}> déclare avoir fini sa mission via l'interface : *\"{m_info['texte']}\"* !\n"
            f"⏱️ **Le chrono est mis en pause.** Choisissez l'action appropriée :"
        )
        await envoyer_double_notification(interaction.guild, msg_fin, f"📢 {mention_ins} — <@{target_id}> demande une validation pour : *\"{m_info['texte']}\"* dans {interaction.channel.mention}", view=VueEvaluationMission, joueur_id=target_id)
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - JOUEUR FINIR : {interaction.user.name} a cliqué Finir la mission sur {interaction.guild.name}")

    @discord.ui.button(label="❌ Abandonner", style=discord.ButtonStyle.danger, custom_id="joueur_abandonner_mission")
    async def joueur_abandonner(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_id = interaction.guild.id
        current_joueur_id = self.joueur_id
        if not current_joueur_id and g_id in missions_actives:
            for j_id, m_info in missions_actives[g_id].items():
                if m_info.get("channel_id") == interaction.channel.id:
                    current_joueur_id = j_id
                    break

        target_id = current_joueur_id if current_joueur_id else interaction.user.id
        if current_joueur_id and interaction.user.id != current_joueur_id and not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Tu ne peux pas abandonner la mission de quelqu'un d'autre.", ephemeral=True)
            return

        if g_id not in missions_actives or target_id not in missions_actives[g_id]:
            await interaction.response.send_message("❌ Tu n'as pas de mission active à abandonner sur ce serveur.", ephemeral=True)
            return

        for child in self.children: child.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            await interaction.response.defer(ephemeral=True)
            
        await action_refuser_mission(target_id, interaction.channel)
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - JOUEUR ABANDONNER : {interaction.user.name} a abandonné sa mission sur {interaction.guild.name}")

class VueEvaluationMission(VueVerrouillable):
    def __init__(self, joueur_id=None):
        super().__init__(timeout=None)
        self.joueur_id = joueur_id

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success, custom_id="eval_accepter")
    async def eval_accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour évaluer cet ordre.", ephemeral=True)
            return
        
        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass
        
        target_j_id = self.joueur_id
        g_id = interaction.guild.id
        if not target_j_id and g_id in missions_actives:
            for j_id, m_info in missions_actives[g_id].items():
                if m_info.get("channel_id") == interaction.channel.id:
                    target_j_id = j_id
                    break

        chan_cible = interaction.channel
        if target_j_id and g_id in missions_actives and target_j_id in missions_actives[g_id]:
            c = bot.get_channel(missions_actives[g_id][target_j_id]["channel_id"])
            if c: chan_cible = c

        if target_j_id:
            await action_accepter_mission(target_j_id, chan_cible)
        else:
            await interaction.followup.send("❌ Impossible de lier cette évaluation à un joueur actif.", ephemeral=True)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="eval_refuser")
    async def eval_refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour évaluer cet ordre.", ephemeral=True)
            return
        
        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass
        
        target_j_id = self.joueur_id
        g_id = interaction.guild.id
        if not target_j_id and g_id in missions_actives:
            for j_id, m_info in missions_actives[g_id].items():
                if m_info.get("channel_id") == interaction.channel.id:
                    target_j_id = j_id
                    break

        chan_cible = interaction.channel
        if target_j_id and g_id in missions_actives and target_j_id in missions_actives[g_id]:
            c = bot.get_channel(missions_actives[g_id][target_j_id]["channel_id"])
            if c: chan_cible = c

        if target_j_id:
            await action_refuser_mission(target_j_id, chan_cible)
        else:
            await interaction.followup.send("❌ Impossible de lier cette évaluation à un joueur actif.", ephemeral=True)

    @discord.ui.button(label="📸 Demander des preuves", style=discord.ButtonStyle.primary, custom_id="eval_preuve")
    async def eval_preuve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not verifier_permissions_staff(interaction.user):
            await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire.", ephemeral=True)
            return

        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass
        
        target_j_id = self.joueur_id
        target_guild = interaction.guild
        g_id = interaction.guild.id
        if not target_j_id and g_id in missions_actives:
            for j_id, m_info in missions_actives[g_id].items():
                if m_info.get("channel_id") == interaction.channel.id:
                    target_j_id = j_id
                    break

        chan_cible = interaction.channel
        if target_j_id and g_id in missions_actives and target_j_id in missions_actives[g_id]:
            c = bot.get_channel(missions_actives[g_id][target_j_id]["channel_id"])
            if c: chan_cible = c

        if target_j_id:
            await action_demander_preuve(target_j_id, chan_cible, target_guild)
        else:
            await interaction.followup.send("❌ Impossible de lier cette demande à un joueur actif.", ephemeral=True)

class VueEvaluationMissionMP(VueVerrouillable):
    def __init__(self, guild_target, joueur_id):
        super().__init__(timeout=None)
        self.guild_target = guild_target
        self.joueur_id = joueur_id

    @discord.ui.button(label="✅ Accepter (MP)", style=discord.ButtonStyle.success, custom_id="eval_mp_accepter")
    async def eval_mp_accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass

        chan_cible = None
        if self.guild_target and self.guild_target.id in missions_actives and self.joueur_id in missions_actives[self.guild_target.id]:
            chan_cible = bot.get_channel(missions_actives[self.guild_target.id][self.joueur_id]["channel_id"])
        if not chan_cible and self.guild_target:
            chan_cible = self.guild_target.get_channel(SALON_VALIDATION_MISSION_ID) or discord.utils.get(self.guild_target.text_channels, name="validation-mission")
        
        if chan_cible:
            await action_accepter_mission(self.joueur_id, chan_cible)
            await interaction.followup.send("✅ Mission acceptée avec succès depuis les logs !", ephemeral=True)
        else:
            await interaction.followup.send("❌ Salon introuvable pour cette mission.", ephemeral=True)

    @discord.ui.button(label="❌ Refuser (MP)", style=discord.ButtonStyle.danger, custom_id="eval_mp_refuser")
    async def eval_mp_refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass

        chan_cible = None
        if self.guild_target and self.guild_target.id in missions_actives and self.joueur_id in missions_actives[self.guild_target.id]:
            chan_cible = bot.get_channel(missions_actives[self.guild_target.id][self.joueur_id]["channel_id"])
        if not chan_cible and self.guild_target:
            chan_cible = self.guild_target.get_channel(SALON_VALIDATION_MISSION_ID) or discord.utils.get(self.guild_target.text_channels, name="validation-mission")
        
        if chan_cible:
            await action_refuser_mission(self.joueur_id, chan_cible)
            await interaction.followup.send("❌ Mission refusée depuis les logs.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Salon introuvable pour cette mission.", ephemeral=True)

    @discord.ui.button(label="📸 Preuve (MP)", style=discord.ButtonStyle.primary, custom_id="eval_mp_preuve")
    async def eval_mp_preuve(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        try: await interaction.response.edit_message(view=self)
        except Exception: pass

        chan_cible = None
        if self.guild_target and self.guild_target.id in missions_actives and self.joueur_id in missions_actives[self.guild_target.id]:
            chan_cible = bot.get_channel(missions_actives[self.guild_target.id][self.joueur_id]["channel_id"])
        
        if chan_cible and self.guild_target:
            await action_demander_preuve(self.joueur_id, chan_cible, self.guild_target)
            await interaction.followup.send("📸 Demande de preuve transmise depuis les logs.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Salon introuvable pour cette mission.", ephemeral=True)

async def action_accepter_mission(joueur_id, channel):
    guild = channel.guild
    g_id = guild.id
    if g_id in missions_actives and joueur_id in missions_actives[g_id]:
        m_info = missions_actives[g_id][joueur_id]
        profils = charger_profils(g_id)
        initialiser_profil(joueur_id, profils)
        profils[str(joueur_id)]["total_reussies"] += 1
        # La mission a-t-elle des points spécifiques (surcharge) ? Sinon on
        # retombe sur les points de sa catégorie.
        points_override = m_info.get("points_override")
        points_gagnes = points_override if points_override is not None else points_pour_categorie(g_id, m_info["cat"])
        profils[str(joueur_id)]["total_points"] += points_gagnes
        duree_secondes = (datetime.now() - m_info["date_debut"]).total_seconds()
        ajouter_historique(joueur_id, profils, m_info["texte"], "Succès", m_info["cat"], duree_secondes, points=points_gagnes)
        # Mission enfin réussie : on efface le "à refaire" s'il y en avait un.
        if "mission_a_refaire" in profils.get(str(joueur_id), {}):
            del profils[str(joueur_id)]["mission_a_refaire"]
        sauvegarder_profils(g_id, profils)
        del missions_actives[g_id][joueur_id]

        # Mission moyenne/difficile/royale réussie : un ticket de roue est
        # offert (utilisable ensuite sur la roue de son choix, voir /roue).
        # Pas de ticket pour les missions "commune".
        texte_ticket = ""
        if m_info["cat"] in ("moyenne", "difficile", "royal"):
            total_tickets = ajouter_tickets_roue(g_id, joueur_id, 1)
            texte_ticket = f"\n🎟️ **+1 ticket de roue** (total : {total_tickets}) — utilisable sur la roue de ton choix sur le site !"

        msg = f"✅ **Mission Validée** ! L'objectif est consigné comme réussi dans le grand registre.\n🏅 **+{points_gagnes} points** (total : {profils[str(joueur_id)]['total_points']}).{texte_ticket}\n\n🚚 **Un instructeur va venir récupérer le butin.**"
        await channel.send(msg, view=VueButinRecupere())
        await envoyer_double_notification(guild, msg, f"✅ **Mission accomplie** par <@{joueur_id}> : *\"{m_info['texte']}\"*", joueur_id=joueur_id)
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ACTION ACCEPTER MISSION : Joueur {joueur_id} validé sur {guild.name}")
        return True
    return False

async def action_refuser_mission(joueur_id, channel):
    guild = channel.guild
    g_id = guild.id
    if g_id in missions_actives and joueur_id in missions_actives[g_id]:
        m_info = missions_actives[g_id][joueur_id]
        profils = charger_profils(g_id)
        initialiser_profil(joueur_id, profils)
        profils[str(joueur_id)]["total_echouees"] += 1
        duree_secondes = (datetime.now() - m_info["date_debut"]).total_seconds()
        ajouter_historique(joueur_id, profils, m_info["texte"], "Échec", m_info["cat"], duree_secondes)
        # Mission échouée/abandonnée/refusée : on la mémorise pour que le
        # joueur retombe automatiquement dessus à sa prochaine tentative.
        profils[str(joueur_id)]["mission_a_refaire"] = {"texte": m_info["texte"], "delai": m_info.get("delai_texte", ""), "cat": m_info["cat"], "points": m_info.get("points_override")}
        sauvegarder_profils(g_id, profils)
        del missions_actives[g_id][joueur_id]
        
        msg = f"↩️ **Mission Terminée (Refusé/Échec)**.\n\n{TEXTE_ECHEC}"
        await channel.send(msg, view=VueFermerTicket())
        await envoyer_double_notification(guild, msg, f"❌ **Mission échouée/refusée** pour <@{joueur_id}> : *\"{m_info['texte']}\"*", joueur_id=joueur_id)
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ACTION REFUSER MISSION : Joueur {joueur_id} échoué sur {guild.name}")
        return True
    return False

async def action_demander_preuve(joueur_id, channel, guild):
    g_id = guild.id
    if g_id in missions_actives and joueur_id in missions_actives[g_id]:
        m_info = missions_actives[g_id][joueur_id]
        m_info["en_attente"] = True
        
        member = guild.get_member(joueur_id)
        if member:
            await channel.set_permissions(member, read_messages=True, send_messages=True)
            
        role_instructeur = discord.utils.get(guild.roles, name="[ 🎴[Instruction] ]")
        mention_ins = role_instructeur.mention if role_instructeur else "@[ 🎴[Instruction] ]"
        
        msg_ticket = f"⚠️ <@{joueur_id}>, **{mention_ins} veuillez nous fournir une preuve de l'accomplissement de votre mission (veuillez envoyer une image ou une photo valide).**"
        msg_log_missions = f"📸 {mention_ins} — Une demande de preuve a été envoyée à <@{joueur_id}> dans son ticket {channel.mention}.\nMerci de valider ou refuser ci-dessous une fois la preuve examinée :"
        
        await channel.send(msg_ticket)
        await envoyer_double_notification(guild, msg_ticket, msg_log_missions, view=VueEvaluationMission(joueur_id), joueur_id=joueur_id)
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ACTION PREUVE : Demandée pour le joueur {joueur_id} sur {guild.name}")
        return True
    return False

async def attribuer_mission_precise_site(guild, joueur_id, texte, delai_texte, cat, points, attribue_par):
    """Attribue une mission PRÉCISE (texte, délai, catégorie et destinataire
    tous choisis un par un, typiquement depuis le formulaire du site web —
    contrairement à /attribuer_mission qui tire une mission au hasard dans
    la catégorie). Crée un nouveau salon de ticket comme le fait /openticket,
    puis y poste directement le décret déjà choisi (le joueur n'a donc pas
    à choisir de difficulté : le chrono démarre immédiatement).

    Ne lève jamais d'exception : renvoie toujours un dict {"ok": bool, ...},
    avec une clé "erreur" lisible par un humain si "ok" est False, pour un
    affichage direct sur le site."""
    g_id = guild.id
    joueur = guild.get_member(joueur_id)
    if not joueur:
        return {"ok": False, "erreur": "Ce membre est introuvable sur ce serveur Discord (a-t-il bien quitté/rejoint récemment ?)."}

    if g_id in missions_actives and joueur_id in missions_actives[g_id]:
        return {"ok": False, "erreur": f"{joueur.display_name} a déjà une mission active en cours sur ce serveur."}

    try:
        role_instructeur = discord.utils.get(guild.roles, name="[ 🎴[Instruction] ]")
        role_palais = discord.utils.get(guild.roles, name="[ Palais Royal ]") or discord.utils.get(guild.roles, name="Palais Royal")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            joueur: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True),
        }
        if role_instructeur:
            overwrites[role_instructeur] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        if role_palais:
            overwrites[role_palais] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

        nom_salon = f"📜-ordre-{joueur.name}"
        ticket_channel = await guild.create_text_channel(name=nom_salon, overwrites=overwrites)
    except Exception as e:
        return {"ok": False, "erreur": f"Impossible de créer le salon du ticket : {e}"}

    duree = extraire_duree(delai_texte)
    date_fin = datetime.now() + duree
    timestamp_discord = int(date_fin.timestamp())

    if g_id not in missions_actives:
        missions_actives[g_id] = {}

    missions_actives[g_id][joueur_id] = {
        "texte": texte,
        "delai_texte": delai_texte,
        "date_debut": datetime.now(),
        "date_fin": date_fin,
        "duree_totale": duree,
        "cat": cat,
        "channel_id": ticket_channel.id,
        "alerte_moitie": False,
        "alerte_un_quart": False,
        "en_attente": False,
        "points_override": points,
    }

    emoji_cat = {"commune": "🟢", "moyenne": "🔵", "difficile": "🟠", "royal": "🔴"}.get(cat, "📜")
    embed_mission = discord.Embed(title="📜 DÉCRET IMPÉRIAL ATTRIBUÉ PAR L'ADMINISTRATION", color=discord.Color.gold())
    embed_mission.add_field(name="🎯 Objectif", value=f"*{texte}*", inline=False)
    embed_mission.add_field(name="🏷️ Catégorie", value=f"{emoji_cat} {cat.capitalize()}", inline=True)
    embed_mission.add_field(name="⏳ Temps imparti", value=f"<t:{timestamp_discord}:R> (soit le <t:{timestamp_discord}:f>)", inline=False)
    embed_mission.set_thumbnail(url=joueur.display_avatar.url)
    embed_mission.set_footer(text=f"Attribué par {attribue_par} depuis le site web.")

    try:
        await ticket_channel.send(content=joueur.mention, embed=embed_mission, view=VueGestionJoueurMission(joueur_id))
    except Exception as e:
        return {"ok": False, "erreur": f"Le salon {ticket_channel.mention} a été créé mais l'envoi du décret a échoué : {e}"}

    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ATTRIBUTION SITE : Mission précise attribuée à {joueur_id} sur {guild.name} par {attribue_par}.")
    return {"ok": True, "channel_id": ticket_channel.id, "channel_mention": ticket_channel.mention, "channel_name": ticket_channel.name}


async def gerer_expiration_automatique(guild, channel_id, joueur_id):
    await asyncio.sleep(3600)
    g_id = guild.id
    if g_id not in missions_actives or joueur_id not in missions_actives[g_id]:
        channel = bot.get_channel(channel_id)
        if not channel: return
        
        expiration_time = int((datetime.now() + timedelta(hours=1)).timestamp())
        member = guild.get_member(joueur_id)
        mention_joueur = member.mention if member else f"<@{joueur_id}>"
        
        msg_expiration_auto = (
            f"⚠️ {mention_joueur}, **attention : aucune mission n'a été sélectionnée depuis 1 heure.**\n"
            f"Cet ordre de mission sera définitivement supprimé et annulé **<t:{expiration_time}:R>** (<t:{expiration_time}:t>)."
        )
        try: await channel.send(msg_expiration_auto)
        except Exception: return

        await asyncio.sleep(3600)
        if g_id not in missions_actives or joueur_id not in missions_actives[g_id]:
            channel_final = bot.get_channel(channel_id)
            if channel_final:
                try:
                    await channel_final.delete(reason="Expiration de l'ordre de mission")
                    await envoyer_double_notification(guild, "", f"🗑️ Le ticket d'ordre de {mention_joueur} a été supprimé automatiquement pour inactivité.")
                    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ERREUR EXPIRATION AUTO : Ticket de {joueur_id} supprimé pour inactivité sur {guild.name}")
                except Exception as e:
                    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ERREUR EXPIRATION AUTO : {e}")

@tasks.loop(seconds=1)
async def verifier_temps_missions():
    maintenant = datetime.now()

    for guild_id, j_dict in list(missions_actives.items()):
        guild = bot.get_guild(guild_id)
        if not guild: continue
        missions_a_retirer = []

        for joueur_id, m_info in list(j_dict.items()):
            try:
                if m_info.get("en_attente", False): continue

                channel = bot.get_channel(m_info["channel_id"])
                if not channel: continue

                duree_totale = m_info["duree_totale"]
                date_debut = m_info["date_debut"]
                date_fin = m_info["date_fin"]
                temps_restant = date_fin - maintenant
                temps_ecoule = maintenant - date_debut

                if maintenant > date_fin:
                    print(f"[VERIFIER_TEMPS_MISSIONS] Expiration détectée : guild={guild_id} joueur={joueur_id} "
                          f"date_fin={date_fin.isoformat()} maintenant={maintenant.isoformat()} "
                          f"duree_totale={duree_totale} texte={m_info.get('texte')!r}")
                    missions_a_retirer.append(joueur_id)
                    profils = charger_profils(guild_id)
                    initialiser_profil(joueur_id, profils)
                    profils[str(joueur_id)]["total_echouees"] += 1
                    duree_secondes = (maintenant - date_debut).total_seconds()
                    ajouter_historique(joueur_id, profils, m_info["texte"], "Échec", m_info["cat"], duree_secondes)
                    # Mission échouée par dépassement du délai : à refaire.
                    profils[str(joueur_id)]["mission_a_refaire"] = {"texte": m_info["texte"], "delai": m_info.get("delai_texte", ""), "cat": m_info["cat"], "points": m_info.get("points_override")}
                    sauvegarder_profils(guild_id, profils)

                    role_instructeur = discord.utils.get(guild.roles, name="[ 🎴[Instruction] ]")
                    mention_ins = role_instructeur.mention if role_instructeur else '@[ 🎴[Instruction] ]'

                    msg_echec = (
                        f"🚨 **MISSION ÉCHOUÉE** 🚨\nLe temps imparti est écoulé ! La mission de <@{joueur_id}> a échoué.\n"
                        f"📢 {mention_ins}, un citoyen a failli à son devoir.\n\n{TEXTE_ECHEC}"
                    )
                    await channel.send(msg_echec, view=VueFermerTicket())
                    await envoyer_double_notification(guild, msg_echec, f"🚨 <@{joueur_id}> a dépassé le temps imparti pour sa mission : *\"{m_info['texte']}\"* !", joueur_id=joueur_id)
                    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - TEMPS ECOULE : Mission échouée par dépassement pour {joueur_id} sur {guild.name}")

                elif temps_restant <= (duree_totale / 4) and not m_info["alerte_un_quart"]:
                    m_info["alerte_un_quart"] = True
                    m_info["alerte_moitie"] = True
                    jours = temps_restant.days
                    heures, reste = divmod(temps_restant.seconds, 3600)
                    minutes, secondes = divmod(reste, 60)
                    await channel.send(f"⏳ **CRITIQUE** <@{joueur_id}> : -25% du temps restant ! Reste : `{jours}j {heures}h {minutes}mn {secondes}s` !")
                    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ALERTE 25% : Temps critique pour le joueur {joueur_id} sur {guild.name}")
                elif temps_ecoule >= (duree_totale / 2) and not m_info["alerte_moitie"]:
                    m_info["alerte_moitie"] = True
                    await channel.send(f"🌑 **MI-PARCOURS** <@{joueur_id}> : la moitié du temps s'est écoulée !")
            except Exception as e:
                # Une erreur sur UNE mission ne doit jamais interrompre la
                # boucle pour les autres missions/serveurs, et ne doit
                # surtout jamais provoquer de suppression silencieuse.
                print(f"[VERIFIER_TEMPS_MISSIONS] ERREUR sur guild={guild_id} joueur={joueur_id} : {e}")
                continue

        if missions_a_retirer:
            with verrou_missions:
                for joueur_id in missions_a_retirer:
                    if guild_id in missions_actives and joueur_id in missions_actives[guild_id]:
                        del missions_actives[guild_id][joueur_id]

@verifier_temps_missions.error
async def verifier_temps_missions_erreur(erreur):
    # Par défaut, une exception non gérée arrête définitivement une
    # tasks.loop sans que ça se voie côté site web (les missions restent
    # simplement "figées" en mémoire). On journalise et on redémarre.
    print(f"[VERIFIER_TEMPS_MISSIONS] La boucle a planté et va être redémarrée : {erreur}")
    if not verifier_temps_missions.is_running():
        verifier_temps_missions.start()

class VueBoutonTicket(VueVerrouillable):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎟️ Ouvrir un Ticket de Mission", style=discord.ButtonStyle.green, custom_id="btn_ouvrir_ticket")
    async def ouvrir_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        joueur = interaction.user
        g_id = guild.id
        
        if g_id in missions_actives and joueur.id in missions_actives[g_id]:
            await interaction.response.send_message("Vous avez déjà une mission active sur ce serveur !", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        role_instructeur = discord.utils.get(guild.roles, name="[ 🎴[Instruction] ]")
        role_palais = discord.utils.get(guild.roles, name="[ Palais Royal ]") or discord.utils.get(guild.roles, name="Palais Royal")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            joueur: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        }
        
        if role_instructeur: overwrites[role_instructeur] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
        if role_palais: overwrites[role_palais] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

        nom_salon = f"📜-ordre-{joueur.name}"
        ticket_channel = await guild.create_text_channel(name=nom_salon, overwrites=overwrites, category=interaction.channel.category if interaction.channel.category else None)

        embed_ticket = discord.Embed(
            title="⚖️ CENTRE DE SÉLECTION DES DÉCRETS ⚖️",
            description=f"Bienvenue {joueur.mention}.\nChoisis la difficulté de l'objectif que tu souhaites accomplir aujourd'hui pour Valerius.",
            color=discord.Color.dark_red()
        )
        embed_ticket.add_field(name="🟢 Commune", value="Objectif rapide, délai court.", inline=True)
        embed_ticket.add_field(name="🔵 Moyenne", value="Bon équilibre effort/récompense.", inline=True)
        embed_ticket.add_field(name="🟠 Difficile", value="Investissement conséquent.", inline=True)
        embed_ticket.add_field(name="🔴 Royal", value="Le sommet du prestige.", inline=True)
        embed_ticket.set_thumbnail(url=joueur.display_avatar.url)
        embed_ticket.set_footer(text="Un seul choix possible — une fois validé, le chrono démarre immédiatement.")
        await ticket_channel.send(embed=embed_ticket, view=VueChoixDifficulte(joueur.id))
        
        asyncio.create_task(gerer_expiration_automatique(guild, ticket_channel.id, joueur.id))
        await interaction.followup.send(f"✅ Ton ticket a été créé ici : {ticket_channel.mention}", ephemeral=True)

class VueChoixDifficulte(VueVerrouillable):
    def __init__(self, joueur_id):
        super().__init__(timeout=600)
        self.joueur_id = joueur_id

    async def attribuer_mission_bouton(self, interaction: discord.Interaction, cat: str):
        if interaction.user.id != self.joueur_id:
            await interaction.response.send_message("❌ Ce ticket ne t'appartient pas.", ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        if guild_id not in missions_actives:
            missions_actives[guild_id] = {}

        if self.joueur_id in missions_actives[guild_id]:
            await interaction.response.send_message("Vous avez déjà une mission active sur ce serveur !", ephemeral=True)
            return

        # Si ce joueur a une mission échouée/abandonnée en attente, il doit
        # d'abord la refaire : on la lui redonne telle quelle, peu importe
        # le bouton de catégorie cliqué, au lieu d'un tirage aléatoire.
        mission_a_refaire = obtenir_mission_a_refaire(guild_id, self.joueur_id)
        rappel_mission = False
        if mission_a_refaire:
            mission_choisie = {"texte": mission_a_refaire["texte"], "delai": mission_a_refaire.get("delai", ""), "points": mission_a_refaire.get("points")}
            cat = mission_a_refaire.get("cat", cat)
            rappel_mission = True
        else:
            missions_dispo = charger_missions_fichier(guild_id)
            if not missions_dispo[cat]:
                await interaction.response.send_message(f"❌ Plus de mission disponible dans la catégorie `{cat.upper()}` sur ce serveur.", ephemeral=True)
                return
            mission_choisie = choisir_mission_sans_repetition(guild_id, self.joueur_id, missions_dispo[cat])

        duree = extraire_duree(mission_choisie["delai"])
        date_fin = datetime.now() + duree
        timestamp_discord = int(date_fin.timestamp())

        missions_actives[guild_id][self.joueur_id] = {
            "texte": mission_choisie["texte"], "delai_texte": mission_choisie["delai"],
            "date_debut": datetime.now(), "date_fin": date_fin, "duree_totale": duree,
            "cat": cat, "channel_id": interaction.channel.id, "alerte_moitie": False, "alerte_un_quart": False, "en_attente": False,
            "points_override": mission_choisie.get("points")
        }

        for child in self.children:
            child.disabled = True

        emoji_cat = {"commune": "🟢", "moyenne": "🔵", "difficile": "🟠", "royal": "🔴"}.get(cat, "📜")
        titre_embed = "🔁 MISSION PRÉCÉDENTE À TERMINER" if rappel_mission else "📜 DECRET ATTRIBUÉ ET CHRONO LANCÉ"
        embed_mission = discord.Embed(title=titre_embed, color=discord.Color.gold())
        if rappel_mission:
            embed_mission.description = "⚠️ Ta mission précédente a échoué ou a été abandonnée. Tu dois la refaire avant de pouvoir en obtenir une nouvelle."
        embed_mission.add_field(name="🎯 Objectif", value=f"*{mission_choisie['texte']}*", inline=False)
        embed_mission.add_field(name="🏷️ Catégorie", value=f"{emoji_cat} {cat.capitalize()}", inline=True)
        embed_mission.add_field(name="⏳ Temps restant réel", value=f"<t:{timestamp_discord}:R> (soit le <t:{timestamp_discord}:f>)", inline=False)
        embed_mission.set_thumbnail(url=interaction.user.display_avatar.url)
        embed_mission.set_footer(text="Utilise les boutons ci-dessous pour gérer ton ordre.")

        await interaction.response.edit_message(view=self)
        await interaction.channel.send(content=f"{interaction.user.mention}", embed=embed_mission, view=VueGestionJoueurMission(self.joueur_id))

    @discord.ui.button(label="🟢 Commune", style=discord.ButtonStyle.secondary, custom_id="btn_commune")
    async def btn_commune(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attribuer_mission_bouton(interaction, "commune")

    @discord.ui.button(label="🔵 Moyenne", style=discord.ButtonStyle.primary, custom_id="btn_moyenne")
    async def btn_moyenne(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attribuer_mission_bouton(interaction, "moyenne")

    @discord.ui.button(label="🟠 Difficile", style=discord.ButtonStyle.success, custom_id="btn_difficile")
    async def btn_difficile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attribuer_mission_bouton(interaction, "difficile")

    @discord.ui.button(label="🔴 Royal", style=discord.ButtonStyle.danger, custom_id="btn_royal")
    async def btn_royal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.attribuer_mission_bouton(interaction, "royal")

@bot.command(name="import")
async def importer_missions(ctx, mode: str = "texte"):
    if not verifier_permissions_staff(ctx.author):
        await ctx.send("❌ Permission refusée.")
        return

    guild_id = ctx.guild.id

    if mode.lower() == "tout":
        missions_a_restaurer = [
            ("commune", "récolter 3 stacks de diamants", "3 jours"),
            ("commune", "récolter 3 minerais obscur", "3 jours"),
            ("commune", "récolter 2 fibres de bois millénaires", "3 jours"),
            ("commune", "récolter un dc de blé", "3 jours"),
            ("commune", "récolter 1 dc de buche de bois ( tout type )", "3 jours"),
            ("commune", "faire 15 potions ( force 2, vitesse 1 ou vitesse 2 )", "3 jours"),
            ("commune", "récolter un stack de pomme rouge", "3 jours"),
            ("commune", "craft 3 stockage d'energie", "3 jours"),
            ("commune", "craft 3 stack de steel compressé", "3 jours"),
            ("commune", "récolter 32 minerais d'ashtone ( minerais se trouvant sur le plafond du nether )", "3 jours"),
            
            ("moyenne", "crafter un paneau solaire de tier 1", "7 jours"),
            ("moyenne", "récolter un stack de blocs de diamants", "7 jours"),
            ("moyenne", "recolter un dc de mais", "7 jours"),
            ("moyenne", "récolter 5 fibre de bois millénaires", "7 jours"),
            ("moyenne", "récolter un dc de glowstone", "7 jours"),
            ("moyenne", "récolter 7 minerais obscur", "7 jours"),
            ("moyenne", "récolter un coffre de stack de laine", "7 jours"),
            ("moyenne", "faire un dc de potion de soin jetable", "7 jours"),
            ("moyenne", "récolter 5 stack de blocs d'or", "7 jours"),
            ("moyenne", "faire un dc de potion au choix ( force 2, vitesse 1, vitesse 2 ou invisibilité )", "7 jours"),
            ("moyenne", "récolter 2 tiber", "7 jours"),
            ("moyenne", "craft un écotron", "7 jours"),
            ("moyenne", "produire 32 lingot d'eco", "7 jours"),
            ("moyenne", "récolter 3 zirconiums", "7 jours"),
            ("moyenne", "craft 1 biogenerateur", "7 jours"),
            ("moyenne", "Recruter un joueur", "7 jours"),
            ("moyenne", "craft 10 bouteilles de gaz", "7 jours"),
            ("moyenne", "craft un chargeur électrique", "7 jours"),
            ("moyenne", "craft 6 stockage d'energie amélioré", "7 jours"),
            ("moyenne", "récolter un dc de tournesol", "7 jours"),
            ("moyenne", "récolté 3 stack d'ashtone ( minerais se trouvant sur le plafond du nether )", "7 jours"),
            
            ("difficile", "crafter un paneau solaire de tier 2", "15 jours"),
            ("difficile", "récolter 15 minerais obscur", "15 jours"),
            ("difficile", "récolter 8 tiber", "15 jours"),
            ("difficile", "craft 3 paneaux solaires T2", "15 jours"),
            ("difficile", "craft une pelle electrique", "15 jours"),
            ("difficile", "produire 7 stack d'éco", "15 jours"),
            ("difficile", "récolter 12 fibre de bois millénaires", "15 jours"),
            ("difficile", "récolter 10 zirconiums", "15 jours"),
            ("difficile", "Craft 3 biogenerateurs", "15 jours"),
            ("difficile", "recruter 3 joueurs", "15 jours"),
            ("difficile", "craft 1 extracteur a gaz", "15 jours"),
            ("difficile", "craft un tracteur", "15 jours"),
            
            ("royal", "craft un pétrolier", "20 jours"),
            ("royal", "craft un serveur", "20 jours"),
            ("royal", "récolter 30 minerais obscur", "20 jours"),
            ("royal", "récolter 30 fibre de bois millénaires", "20 jours"),
            ("royal", "craft 10 paneaux solaires T2", "20 jours"),
            ("royal", "récolter 25 zirconiums", "20 jours")
        ]

        for cat, texte, temps in missions_a_restaurer:
            sauvegarder_mission_fichier(guild_id, cat, texte, temps)
        
        await ctx.send(f"✅ **Succès !** Les {len(missions_a_restaurer)} missions officielles ont toutes été injectées pour ce serveur.")
        return

    if ctx.message.attachments:
        nb_ajoutees = 0
        try:
            attachement = ctx.message.attachments[0]
            contenu_bytes = await attachement.read()
            lignes = contenu_bytes.decode("utf-8").splitlines()
            for ligne in lignes:
                ligne = ligne.strip()
                if not ligne or "|" not in ligne: continue
                parts = ligne.split("|", 2)
                if len(parts) == 3:
                    cat, texte, delai = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    if cat in ["commune", "moyenne", "difficile", "royal"]:
                        sauvegarder_mission_fichier(guild_id, cat, texte, delai)
                        nb_ajoutees += 1
            await ctx.send(f"✅ **Succès !** {nb_ajoutees} missions ont été importées à partir du fichier `.txt` pour ce serveur.")
            return
        except Exception as e:
            await ctx.send(f"❌ Erreur lors de la lecture du fichier joint : {e}")
            return

    await ctx.send("📥 **Envoie ou colle ton bloc de missions** (ou glisse ton fichier `.txt` exporté) dans les 60 secondes :")

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for('message', timeout=60.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("⏱️ Temps écoulé. Commande annulée.")
        return

    if msg.attachments:
        nb_ajoutees = 0
        try:
            attachement = msg.attachments[0]
            contenu_bytes = await attachement.read()
            lignes = contenu_bytes.decode("utf-8").splitlines()
            for ligne in lignes:
                ligne = ligne.strip()
                if not ligne or "|" not in ligne: continue
                parts = ligne.split("|", 2)
                if len(parts) == 3:
                    cat, texte, delai = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    if cat in ["commune", "moyenne", "difficile", "royal"]:
                        sauvegarder_mission_fichier(guild_id, cat, texte, delai)
                        nb_ajoutees += 1
            await ctx.send(f"✅ **Succès !** {nb_ajoutees} missions ont été importées depuis le fichier pour ce serveur.")
            return
        except Exception as e:
            await ctx.send(f"❌ Erreur : {e}")
            return

    lignes = msg.content.split('\n')
    nb_ajoutees = 0
    categorie_actuelle = "commune"

    correspondance_categories = {
        "commune": "commune",
        "moyenne": "moyenne",
        "difficile": "difficile",
        "royal": "royal",
        "décret royal": "royal",
        "ordre majeur": "difficile"
    }

    delais = {
        "commune": "3 jours",
        "moyenne": "7 jours",
        "difficile": "15 jours",
        "royal": "20 jours"
    }

    for ligne in lignes:
        ligne_propre = ligne.strip()
        if not ligne_propre: continue

        if "|" in ligne_propre:
            parts = ligne_propre.split("|", 2)
            if len(parts) == 3:
                cat, texte, delai = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if cat in ["commune", "moyenne", "difficile", "royal"]:
                    sauvegarder_mission_fichier(guild_id, cat, texte, delai)
                    nb_ajoutees += 1
                    continue

        ligne_lower = ligne_propre.lower()
        found_cat = False
        for key, val in correspondance_categories.items():
            if key in ligne_lower:
                categorie_actuelle = val
                found_cat = True
                break
        if found_cat: continue

        texte_mission = ligne_propre
        for prefixe in ["1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10.", "-", "•"]:
            if texte_mission.startswith(prefixe):
                texte_mission = texte_mission[len(prefixe):].strip()
                break

        if not texte_mission: continue

        temps = delais.get(categorie_actuelle, "7 jours")
        sauvegarder_mission_fichier(guild_id, categorie_actuelle, texte_mission, temps)
        nb_ajoutees += 1

    await ctx.send(f"✅ **Succès !** {nb_ajoutees} missions ont été importées dynamiquement pour ce serveur.")

@bot.command(name="export")
async def exporter_missions(ctx):
    if not verifier_permissions_staff(ctx.author):
        await ctx.send("❌ Permission refusée.")
        return

    file_name = get_file_name(ctx.guild.id)
    if not os.path.exists(file_name):
        await ctx.send("❌ Aucun fichier de missions trouvé pour ce serveur.")
        return

    try:
        with open(file_name, "r", encoding="utf-8") as f:
            contenu = f.read()

        if not contenu.strip():
            await ctx.send("⚠️ Le fichier de missions de ce serveur est vide.")
            return

        buffer = io.BytesIO(contenu.encode("utf-8"))
        buffer.seek(0)
        
        fichier_discord = discord.File(buffer, filename=file_name)
        await ctx.send("📥 **Voici l'export complet des missions de ce serveur :**", file=fichier_discord)
    except Exception as e:
        await ctx.send(f"❌ Erreur lors de l'export : {e}")

@bot.command(name="delall")
async def supprimer_toutes_missions_cmd(ctx):
    if not verifier_permissions_staff(ctx.author):
        await ctx.send("❌ Permission refusée.")
        return
    vider_toutes_missions(ctx.guild.id)
    await ctx.send("🗑️ **Toutes les missions de ce serveur ont été supprimées avec succès !**")


# ================= SYSTEME DE VERROUILLAGE PAR CODE =================
import secrets

VERROU_FILE = "valerius_verrou.json"

guildes_deverrouillees = set()

def _generer_code_aleatoire():
    # Code lisible mais imprévisible, jamais stocké en clair dans le code source
    # (contrairement à l'ancien "maDaGa2026" visible sur un repo GitHub public).
    return secrets.token_urlsafe(9)

def charger_code_verrou():
    try:
        with open(VERROU_FILE, "r", encoding="utf-8") as f:
            code = json.load(f).get("code")
            if code:
                return code
    except Exception:
        pass

    # Priorité à une variable d'environnement (comme le token Discord),
    # sinon on génère un code aléatoire unique qu'on sauvegarde localement.
    code_env = os.environ.get("VALERIUS_CODE_ACTIVATION")
    code = code_env if code_env else _generer_code_aleatoire()
    sauvegarder_code_verrou(code)
    if not code_env:
        asyncio.create_task(envoyer_log_proprietaire(
            bot,
            f"🔑 Aucun code d'activation existant : un nouveau code a été généré automatiquement : `{code}`\n"
            f"Conserve-le précieusement (il est aussi inclus dans les `/total_backup`)."
        ))
    return code

def sauvegarder_code_verrou(nouveau_code):
    with open(VERROU_FILE, "w", encoding="utf-8") as f:
        json.dump({"code": nouveau_code}, f, ensure_ascii=False)

def guilde_verrouillee(guild):
    return guild is not None and guild.id not in guildes_deverrouillees

MESSAGE_VERROU = (
    "🔒 **VALERIUS EST VERROUILLÉ SUR CE SERVEUR**\n"
    "Aucune commande n'est utilisable tant que le code d'activation n'a pas été saisi.\n"
    "Un administrateur doit utiliser : `/deverrouiller code:<le code>`"
)

MAINTENANCE_FILE = "valerius_maintenance.json"

def charger_maintenance():
    try:
        with open(MAINTENANCE_FILE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("actif", False))
    except Exception:
        return False

def definir_maintenance(actif):
    with open(MAINTENANCE_FILE, "w", encoding="utf-8") as f:
        json.dump({"actif": bool(actif)}, f, ensure_ascii=False)

MESSAGE_MAINTENANCE = (
    "🛠️ **VALERIUS EST EN MAINTENANCE**\n"
    "Le bot est temporairement indisponible, réessaie un peu plus tard."
)

async def envoyer_demande_code(guild):
    salon = guild.system_channel
    if salon is None or not salon.permissions_for(guild.me).send_messages:
        salon = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    if salon:
        try:
            await salon.send(MESSAGE_VERROU)
        except Exception:
            pass

async def verrou_interaction_check(interaction: discord.Interaction):
    nom = interaction.command.name if interaction.command else ""
    if nom in ("deverrouiller", "changer_code"):
        return True
    if est_proprietaire(interaction.user.id):
        return True
    if charger_maintenance():
        try:
            await interaction.response.send_message(MESSAGE_MAINTENANCE, ephemeral=True)
        except Exception:
            pass
        return False
    if interaction.guild is None:
        return True
    if guilde_verrouillee(interaction.guild):
        try:
            await interaction.response.send_message(MESSAGE_VERROU, ephemeral=True)
        except Exception:
            pass
        return False
    return True

bot.tree.interaction_check = verrou_interaction_check
# Osiris partage exactement le même verrou de serveur que Valerius : le code
# d'activation saisi via /deverrouiller (commande Valerius) déverrouille les
# deux bots en même temps, car ils lisent le même fichier + la même variable
# en mémoire (guildes_deverrouillees), le tout dans le même processus.
bot_osiris.tree.interaction_check = verrou_interaction_check
# Sirius (bot_rangs) partage lui aussi exactement le même verrou : sans
# cette ligne, ses commandes (/rangs, /demanderrang, /validerrang, ...)
# restaient utilisables même sur un serveur verrouillé pour Valerius.
bot_rangs.tree.interaction_check = verrou_interaction_check

@bot.check
async def verrou_commandes_prefixe(ctx):
    if est_proprietaire(ctx.author.id):
        return True
    if charger_maintenance():
        try:
            await ctx.send(MESSAGE_MAINTENANCE)
        except Exception:
            pass
        return False
    if ctx.guild is None:
        return True
    if guilde_verrouillee(ctx.guild):
        try:
            await ctx.send(MESSAGE_VERROU)
        except Exception:
            pass
        return False
    return True

@bot.event
async def on_guild_join(guild):
    guildes_deverrouillees.discard(guild.id)
    await envoyer_demande_code(guild)
    await envoyer_log_proprietaire(bot, f"🔒 Bot ajouté sur **{guild.name}** — code d'activation demandé.")

@bot.tree.command(name="deverrouiller", description="Déverrouille le bot sur ce serveur à l'aide du code d'activation.")
@app_commands.describe(code="Le code d'activation du bot")
async def deverrouiller(interaction: discord.Interaction, code: str):
    if interaction.guild is None:
        return await interaction.response.send_message("❌ Commande utilisable uniquement sur un serveur.", ephemeral=True)
    if not guilde_verrouillee(interaction.guild):
        return await interaction.response.send_message("✅ Le bot est déjà déverrouillé sur ce serveur.", ephemeral=True)
    if code == charger_code_verrou():
        guildes_deverrouillees.add(interaction.guild.id)
        await interaction.response.send_message("🔓 **Code accepté !** Valerius est désormais actif sur ce serveur.", ephemeral=True)
        await envoyer_log_proprietaire(bot, f"🔓 Déverrouillage réussi sur **{interaction.guild.name}** par {interaction.user}.")
    else:
        await interaction.response.send_message("❌ **Code incorrect.** Sale clown, va dormir.", ephemeral=True)
        await envoyer_log_proprietaire(bot, f"⚠️ Tentative de code échouée sur **{interaction.guild.name}** par {interaction.user}.")

@bot.tree.command(name="changer_code", description="Change le code d'activation global du bot (Admin/Propriétaire).")
@app_commands.describe(ancien_code="Le code actuel", nouveau_code="Le nouveau code")
async def changer_code(interaction: discord.Interaction, ancien_code: str, nouveau_code: str):
    est_admin = est_proprietaire(interaction.user.id) or (interaction.guild and verifier_permissions_staff(interaction.user))
    if not est_admin:
        return await interaction.response.send_message("⛔ Seuls les administrateurs peuvent changer le code.", ephemeral=True)
    if ancien_code != charger_code_verrou():
        return await interaction.response.send_message("❌ Ancien code incorrect.", ephemeral=True)
    if len(nouveau_code.strip()) < 4:
        return await interaction.response.send_message("❌ Le nouveau code doit contenir au moins 4 caractères.", ephemeral=True)
    sauvegarder_code_verrou(nouveau_code.strip())
    await interaction.response.send_message("✅ **Code d'activation modifié avec succès.**", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"🔑 Code d'activation modifié par {interaction.user} sur **{interaction.guild.name if interaction.guild else 'MP'}**.")
# ================= FIN SYSTEME DE VERROUILLAGE =================

# ================= GESTION DES PROPRIÉTAIRES & SUPER MODOS =================

@bot.tree.command(name="ajouter_proprietaire", description="[Propriétaire] Donne le rang Propriétaire (accès total, tous serveurs) à un compte.")
@app_commands.describe(utilisateur="Le compte à promouvoir Propriétaire")
async def ajouter_proprietaire(interaction: discord.Interaction, utilisateur: discord.User):
    if not est_proprietaire(interaction.user.id):
        return await interaction.response.send_message("⛔ Seul un Propriétaire peut faire ça.", ephemeral=True)
    proprietaires = charger_proprietaires()
    if utilisateur.id in proprietaires:
        return await interaction.response.send_message("✅ Ce compte est déjà Propriétaire.", ephemeral=True)
    proprietaires.add(utilisateur.id)
    sauvegarder_proprietaires(proprietaires)
    await interaction.response.send_message(f"👑 {utilisateur.mention} est désormais **Propriétaire** (accès total, tous serveurs, tous les logs).", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"👑 {interaction.user} ({interaction.user.id}) a nommé {utilisateur} ({utilisateur.id}) Propriétaire.")

@bot.tree.command(name="retirer_proprietaire", description="[Propriétaire] Retire le rang Propriétaire à un compte.")
@app_commands.describe(utilisateur="Le compte à rétrograder")
async def retirer_proprietaire(interaction: discord.Interaction, utilisateur: discord.User):
    if not est_proprietaire(interaction.user.id):
        return await interaction.response.send_message("⛔ Seul un Propriétaire peut faire ça.", ephemeral=True)
    if utilisateur.id == PROPRIETAIRE_ID:
        return await interaction.response.send_message("❌ Impossible de retirer le Propriétaire historique (MAVIE7620).", ephemeral=True)
    proprietaires = charger_proprietaires()
    if utilisateur.id not in proprietaires:
        return await interaction.response.send_message("❌ Ce compte n'est pas Propriétaire.", ephemeral=True)
    proprietaires.discard(utilisateur.id)
    sauvegarder_proprietaires(proprietaires)
    await interaction.response.send_message(f"✅ {utilisateur.mention} n'est plus Propriétaire.", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"⚠️ {interaction.user} ({interaction.user.id}) a retiré le rang Propriétaire à {utilisateur} ({utilisateur.id}).")

@bot.tree.command(name="liste_proprietaires", description="[Propriétaire] Liste tous les comptes Propriétaires du bot.")
async def liste_proprietaires(interaction: discord.Interaction):
    if not est_proprietaire(interaction.user.id):
        return await interaction.response.send_message("⛔ Seul un Propriétaire peut faire ça.", ephemeral=True)
    lignes = []
    for pid in charger_proprietaires():
        u = bot.get_user(pid)
        suffixe = " *(historique)*" if pid == PROPRIETAIRE_ID else ""
        lignes.append(f"👑 {u.mention if u else '`' + str(pid) + '`'}{suffixe}")
    embed = discord.Embed(title="👑 Propriétaires de Valerius", description="\n".join(lignes) or "Aucun.", color=discord.Color.gold())
    embed.set_footer(text="Accès total • Tous les serveurs • Tous les logs")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="nommer_supermodo", description="[Propriétaire] Nomme un Super Modo, valable UNIQUEMENT sur ce serveur.")
@app_commands.describe(utilisateur="Le compte à nommer Super Modo sur ce serveur")
async def nommer_supermodo(interaction: discord.Interaction, utilisateur: discord.Member):
    if not est_proprietaire(interaction.user.id):
        return await interaction.response.send_message("⛔ Seul un Propriétaire peut faire ça.", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("❌ Commande utilisable uniquement sur un serveur.", ephemeral=True)
    supermodos = charger_supermodos()
    supermodos[str(utilisateur.id)] = interaction.guild.id
    sauvegarder_supermodos(supermodos)
    await interaction.response.send_message(f"🛡️ {utilisateur.mention} est désormais **Super Modo**, uniquement sur **{interaction.guild.name}**.", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"🛡️ {interaction.user} a nommé {utilisateur} ({utilisateur.id}) Super Modo sur **{interaction.guild.name}** ({interaction.guild.id}).")

@bot.tree.command(name="revoquer_supermodo", description="[Propriétaire] Retire le rang Super Modo à un compte.")
@app_commands.describe(utilisateur="Le compte à révoquer")
async def revoquer_supermodo(interaction: discord.Interaction, utilisateur: discord.User):
    if not est_proprietaire(interaction.user.id):
        return await interaction.response.send_message("⛔ Seul un Propriétaire peut faire ça.", ephemeral=True)
    supermodos = charger_supermodos()
    if str(utilisateur.id) not in supermodos:
        return await interaction.response.send_message("❌ Ce compte n'est pas Super Modo.", ephemeral=True)
    del supermodos[str(utilisateur.id)]
    sauvegarder_supermodos(supermodos)
    await interaction.response.send_message(f"✅ {utilisateur.mention} n'est plus Super Modo.", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"⚠️ {interaction.user} a retiré le rang Super Modo à {utilisateur} ({utilisateur.id}).")

@bot.tree.command(name="liste_supermodos", description="[Staff] Liste les Super Modos et leur serveur assigné.")
async def liste_supermodos(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    supermodos = charger_supermodos()
    lignes = []
    for uid, gid in supermodos.items():
        u = bot.get_user(int(uid))
        g = bot.get_guild(gid)
        lignes.append(f"🛡️ {u.mention if u else '`' + uid + '`'} — **{g.name if g else gid}**")
    embed = discord.Embed(title="🛡️ Super Modos de Valerius", description="\n".join(lignes) or "Aucun.", color=discord.Color.blurple())
    embed.set_footer(text="Chaque Super Modo n'agit que sur son serveur assigné")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= FIN GESTION DES PROPRIÉTAIRES & SUPER MODOS =================

@bot.event
async def on_ready():
    if not verifier_temps_missions.is_running(): verifier_temps_missions.start()
    if not sauvegarde_automatique.is_running(): sauvegarde_automatique.start()
    if not verifier_rappels_periodique.is_running(): verifier_rappels_periodique.start()
    if not synchronisation_drive.is_running(): synchronisation_drive.start()
    
    bot.add_view(VueBoutonTicket())
    bot.add_view(VueFermerTicket())
    bot.add_view(VueButinRecupere())
    bot.add_view(VueAccueilArrivant())
    bot.add_view(VueGestionJoueurMission())
    bot.add_view(VueEvaluationMission())

    await site_web.initialiser_compte_proprietaire(envoyer_log_proprietaire, bot)

    await envoyer_log_proprietaire(bot, f"🚀 **Bot Valerius démarré avec succès !** Connecté et opérationnel.")

    guildes_deverrouillees.clear()
    for g in bot.guilds:
        await envoyer_demande_code(g)
    await envoyer_log_proprietaire(bot, f"🔒 Bot verrouillé sur {len(bot.guilds)} serveur(s). Code d'activation requis.")

    salon_accueil = bot.get_channel(WELCOME_CHANNEL_ID)
    if salon_accueil:
        try:
            async for msg in salon_accueil.history(limit=10):
                if msg.author == bot.user and "Bienvenue" in msg.content:
                    break
            else:
                embed_accueil = discord.Embed(
                    title="⚖️ BIENVENUE ⚖️",
                    description="Veuillez sélectionner ci-dessous votre statut ou votre intention en arrivant sur le serveur :",
                    color=discord.Color.gold()
                )
                await salon_accueil.send(content="Bienvenue", embed=embed_accueil, view=VueAccueilArrivant())
        except Exception as e:
            print(f"Erreur envoi panneau d'accueil automatique : {e}")

    try:
        synced = await bot.tree.sync()
        print(f"Bot Valerius Pro — {len(synced)} Commandes Slash synchronisées !")
    except Exception as e:
        print(f"Erreur de synchronisation slash: {e}")

@bot_osiris.event
async def on_ready():
    if not verifier_blames_expires_periodique.is_running():
        verifier_blames_expires_periodique.start()

    bot_osiris.add_view(VueAvertirJoueur())

    try:
        synced = await bot_osiris.tree.sync()
        print(f"Bot Osiris — {len(synced)} commande(s) slash synchronisée(s) !")
    except Exception as e:
        print(f"Erreur de synchronisation slash (Osiris): {e}")

    await envoyer_log_proprietaire(bot_osiris, "⚖️ **Bot Osiris démarré avec succès !** Système disciplinaire opérationnel.")

@bot_rangs.event
async def on_ready():
    try:
        synced = await bot_rangs.tree.sync()
        print(f"Bot Sirius — {len(synced)} commande(s) slash synchronisée(s) !")
    except Exception as e:
        print(f"Erreur de synchronisation slash (Sirius): {e}")

    await envoyer_log_proprietaire(bot_rangs, "🎖️ **Bot Sirius démarré avec succès !** Système des rangs opérationnel.")

@bot.event
async def on_message(message):
    await bot.process_commands(message)

    if message.author.bot: return
    if message.channel.name and "📜-ordre-" in message.channel.name:
        joueur_id = message.author.id
        g_id = message.guild.id
        if g_id in missions_actives and joueur_id in missions_actives[g_id] and missions_actives[g_id][joueur_id].get("en_attente", False):
            contient_image = False
            
            if message.attachments:
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        contient_image = True
                        break
                    elif any(att.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp']):
                        contient_image = True
                        break
            
            if not contient_image and message.embeds:
                for emb in message.embeds:
                    if emb.image or emb.thumbnail:
                        contient_image = True
                        break

            if contient_image:
                await message.channel.send(f"💬 <@{joueur_id}>, image/preuve bien reçue et transmise aux instructeurs !")
                msg_p = f"📸 **Preuve reçue** pour la mission de <@{joueur_id}>. En attente de l'analyse finale de l'administration :"
                await envoyer_double_notification(message.guild, msg_p, f"📸 Preuve d'accomplissement déposée par <@{joueur_id}> dans {message.channel.mention}.", view=VueEvaluationMission(joueur_id), joueur_id=joueur_id)
            else:
                await message.channel.send(f"❌ <@{joueur_id}>, votre message n'a pas été validé comme preuve car **aucune image ni photo n'a été détectée**. Veuillez envoyer une capture d'écran ou une image valide pour que l'administration puisse l'analyser.")

async def generer_panneau_aide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚖️ TABLEAU DES ORDRES DE VALERIUS ⚖️",
        description="*Toutes les commandes disponibles pour toi, classées par rang.*",
        color=discord.Color.gold()
    )
    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    citoyen_desc = (
        "⚔️ **SYSTÈME DE QUÊTES**\n"
        "Ouvre un ticket d'ordre privé dans la catégorie dédiée.\n\n"
        "`/missionaccomplie` ↳ Déclarer la fin de ta tâche active.\n"
        "`/missions_en_cours` ↳ Statut complet de ton contrat.\n"
        "`/tuto` ↳ Guide complet du citoyen.\n\n"
        "📊 **ARCHIVES PERSONNELLES**\n"
        "`/historique` ↳ Consulte ton bilan d'objectifs."
    )
    embed.add_field(name="👥 ESPACE DES CITOYENS", value=citoyen_desc, inline=False)
    if verifier_permissions_staff(interaction.user):
        admin_desc = (
            "🚨 **HAUT COMMANDEMENT (ADMIN / INSTRUCTEUR / SUPER MODO)**\n"
            "`/tutoadm` ↳ Manuel de l'administration.\n"
            "`/openticket @joueur` ↳ Ouvrir un ticket pour un citoyen.\n"
            "`/fermerticket` ↳ Fermer un salon de ticket.\n"
            "`/attribuer_mission` ↳ Assigner une mission auto à un joueur.\n"
            "`/export_actives` | `/import_actives` ↳ Sauvegarder/Restaurer les missions en cours.\n"
            "`/mission_expiration` ↳ Lancer l'alerte d'inactivité (1h).\n"
            "`/missionaccepter` | `/missionrefuser` | `/missionpreuve` | `/ajouterhistorique`\n"
            "`/liste_supermodos` ↳ Voir les Super Modos actifs sur ce serveur.\n\n"
            "📁 **BASE DE DONNÉES**\n"
            "`/listemissions` | `/addmission` | `/delmission` | `/resetmissions`\n"
            "*(Commandes texte : `!export` / `!import` / `!delall`)*"
        )
        embed.add_field(name="👑 ADMINISTRATION", value=admin_desc, inline=False)
    if est_proprietaire(interaction.user.id):
        proprio_desc = (
            "👑 **RÉSERVÉ AU(X) PROPRIÉTAIRE(S) — ACCÈS TOTAL, TOUS SERVEURS**\n"
            "`/total_backup` | `/total_restore` ↳ Sauvegarde/Restauration globale du bot.\n"
            "`/ajouter_proprietaire` | `/retirer_proprietaire` | `/liste_proprietaires`\n"
            "`/nommer_supermodo` | `/revoquer_supermodo` ↳ Gérer les Super Modos par serveur.\n"
            "`/changer_code` ↳ Modifier le code d'activation global."
        )
        embed.add_field(name="🔑 PROPRIÉTAIRE", value=proprio_desc, inline=False)
    embed.set_footer(text="Valerius Pro • Que la fortune te sourie", icon_url=interaction.client.user.display_avatar.url if interaction.client.user else None)
    await interaction.response.send_message(embed=embed, view=VueBoutonTicket())

@bot.tree.command(name="aide", description="Affiche le tableau de bord des quêtes de Valerius.")
async def aide(interaction: discord.Interaction):
    await generer_panneau_aide(interaction)

@bot.tree.command(name="help", description="Affiche le tableau de bord des quêtes de Valerius.")
async def help_cmd(interaction: discord.Interaction):
    await generer_panneau_aide(interaction)

@bot.tree.command(name="tuto", description="Guide d'utilisation pour mener à bien tes décrets.")
async def tuto(interaction: discord.Interaction):
    embed_tuto = discord.Embed(
        title="📜 GUIDE DU CITOYEN DE VALERIUS 📜",
        description="Suis ces instructions impériales pour mener à bien tes décrets sans subir les foudres de l'Article V !",
        color=discord.Color.green()
    )
    embed_tuto.add_field(
        name="🎟️ Étape 1 : Ouvrir l'Ordre",
        value="Rends-toi dans la catégorie dédiée et utilise `/aide` ou `/help` pour obtenir le bouton vert d'ouverture de ticket.",
        inline=False
    )
    embed_tuto.add_field(
        name="📜 Étape 2 : Sélectionner sa Difficulté",
        value="Dans ton ticket, choisis ton contrat : `Commune`, `Moyenne`, `Difficile` ou `Royal`. Le chrono démarre instantanément !",
        inline=False
    )
    embed_tuto.add_field(
        name="🏁 Étape 3 : Déclarer l'accomplissement",
        value="Une fois ton objectif réalisé en jeu, utilise le bouton vert **Finir la mission** ou la commande `/missionaccomplie`.",
        inline=False
    )
    embed_tuto.set_footer(text="Valerius • Que la fortune te sourie")
    await interaction.response.send_message(embed=embed_tuto, ephemeral=True)

@bot.tree.command(name="site", description="Affiche le lien du site web (toujours à jour, même si l'adresse change).")
async def site(interaction: discord.Interaction):
    # RENDER_EXTERNAL_URL est fournie automatiquement par Render et reflète
    # toujours l'URL réelle du déploiement en cours (donc "à jour en direct",
    # sans jamais avoir besoin de modifier le code si l'adresse change).
    # SITE_URL sert de solution de secours si le bot tourne ailleurs que sur Render.
    lien = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SITE_URL")

    embed_site = discord.Embed(
        title="🌐 Site Officiel du Royaume",
        color=discord.Color.blue()
    )
    if lien:
        embed_site.description = f"Accède au site ici : {lien}"
    else:
        embed_site.description = (
            "⚠️ Aucune URL détectée pour l'instant.\n"
            "Définis la variable d'environnement `SITE_URL` sur Render "
            "(ou vérifie que le service web est bien démarré)."
        )
    embed_site.set_footer(text="Valerius • Portail du Royaume")
    await interaction.response.send_message(embed=embed_site, ephemeral=True)

@bot.tree.command(name="openticket", description="Ouvre un ticket de mission pour un citoyen spécifique (Staff uniquement).")
@app_commands.describe(joueur="Le citoyen pour qui ouvrir le ticket d'ordre")
async def openticket(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour ouvrir un ticket pour autrui.", ephemeral=True)
        return

    guild = interaction.guild
    g_id = guild.id

    if g_id in missions_actives and joueur.id in missions_actives[g_id]:
        await interaction.response.send_message(f"❌ {joueur.mention} a déjà une mission active en cours sur ce serveur !", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    role_instructeur = discord.utils.get(guild.roles, name="[ 🎴[Instruction] ]")
    role_palais = discord.utils.get(guild.roles, name="[ Palais Royal ]") or discord.utils.get(guild.roles, name="Palais Royal")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        joueur: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
    }
    
    if role_instructeur: overwrites[role_instructeur] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)
    if role_palais: overwrites[role_palais] = discord.PermissionOverwrite(read_messages=True, send_messages=True, view_channel=True)

    nom_salon = f"📜-ordre-{joueur.name}"
    ticket_channel = await guild.create_text_channel(name=nom_salon, overwrites=overwrites, category=interaction.channel.category if interaction.channel.category else None)

    embed_ticket = discord.Embed(
        title="⚖️ CENTRE DE SÉLECTION DES DÉCRETS ⚖️",
        description=f"Ticket ouvert par l'administration pour {joueur.mention}.\nChoisis la difficulté de l'objectif que tu souhaites accomplir aujourd'hui pour Valerius.",
        color=discord.Color.dark_red()
    )
    await ticket_channel.send(embed=embed_ticket, view=VueChoixDifficulte(joueur.id))
    
    asyncio.create_task(gerer_expiration_automatique(guild, ticket_channel.id, joueur.id))
    await interaction.followup.send(f"✅ Le ticket pour {joueur.mention} a été créé avec succès : {ticket_channel.mention}", ephemeral=True)

@bot.tree.command(name="fermerticket", description="Ferme et supprime immédiatement le salon du ticket actuel (Staff uniquement).")
async def fermerticket(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour fermer ce ticket.", ephemeral=True)
        return
    
    await interaction.response.send_message("⚙️ Fermeture et suppression du salon du ticket par l'administration...", ephemeral=True)
    
    g_id = interaction.guild.id
    if g_id in missions_actives:
        for j_id, m_info in list(missions_actives[g_id].items()):
            if m_info.get("channel_id") == interaction.channel.id:
                del missions_actives[g_id][j_id]
                break

    try:
        await interaction.channel.delete(reason=f"Fermé par l'administrateur {interaction.user.name}")
    except Exception as e:
        print(f"Erreur suppression salon: {e}")

@bot.tree.command(name="attribuer_mission", description="Attribue automatiquement et directement une mission d'une catégorie à un joueur.")
@app_commands.describe(joueur="Le citoyen destinataire", categorie="commune, moyenne, difficile, royal")
async def attribuer_mission(interaction: discord.Interaction, joueur: discord.Member, categorie: str):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour attribuer un décret.", ephemeral=True)
        return

    cat = categorie.lower().strip()
    if cat in ["commune", "commun"]: cat = "commune"
    elif cat in ["moyenne", "moyen"]: cat = "moyenne"
    elif cat in ["difficile"]: cat = "difficile"
    elif cat in ["royal", "royale"]: cat = "royal"
    else:
        await interaction.response.send_message("❌ Catégorie invalide. Choisis entre : commune, moyenne, difficile, royal.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id not in missions_actives:
        missions_actives[guild_id] = {}

    if joueur.id in missions_actives[guild_id]:
        await interaction.response.send_message(f"❌ {joueur.mention} a déjà une mission active en cours sur ce serveur !", ephemeral=True)
        return

    missions_dispo = charger_missions_fichier(guild_id)
    if not missions_dispo[cat]:
        await interaction.response.send_message(f"❌ Plus aucune mission disponible dans la catégorie `{cat.upper()}` sur ce serveur.", ephemeral=True)
        return

    mission_choisie = choisir_mission_sans_repetition(guild_id, joueur.id, missions_dispo[cat])
    duree = extraire_duree(mission_choisie["delai"])
    date_fin = datetime.now() + duree
    timestamp_discord = int(date_fin.timestamp())

    missions_actives[guild_id][joueur.id] = {
        "texte": mission_choisie["texte"], 
        "delai_texte": mission_choisie["delai"],
        "date_debut": datetime.now(), 
        "date_fin": date_fin, 
        "duree_totale": duree,
        "cat": cat, 
        "channel_id": interaction.channel.id, 
        "alerte_moitie": False, 
        "alerte_un_quart": False, 
        "en_attente": False,
        "points_override": mission_choisie.get("points")
    }

    embed_mission = discord.Embed(title="📜 DÉCRET IMPÉRIAL ATTRIBUÉ PAR L'ADMINISTRATION", color=discord.Color.gold())
    embed_mission.add_field(name="🎯 Objectif", value=f"*{mission_choisie['texte']}*", inline=False)
    embed_mission.add_field(name="📊 Difficulté", value=f"`{cat.upper()}`", inline=True)
    embed_mission.add_field(name="⏳ Temps imparti", value=f"<t:{timestamp_discord}:R> (soit le <t:{timestamp_discord}:f>)", inline=False)
    embed_mission.set_thumbnail(url=joueur.display_avatar.url)
    embed_mission.set_footer(text=f"Attribué par {interaction.user.display_name}")

    await interaction.response.send_message(content=f"✅ Mission attribuée avec succès à {joueur.mention} dans ce salon !", embed=embed_mission, view=VueGestionJoueurMission(joueur.id))

@bot.tree.command(name="export_actives", description="Exporte et envoie un fichier .txt de toutes les missions actuellement en cours.")
async def export_actives(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    actives_serveur = {}
    if guild_id in missions_actives:
        for j_id, m_data in missions_actives[guild_id].items():
            chan = bot.get_channel(m_data["channel_id"])
            if chan and chan.guild.id == guild_id:
                actives_serveur[str(j_id)] = {
                    "texte": m_data["texte"],
                    "delai_texte": m_data["delai_texte"],
                    "date_debut": m_data["date_debut"].isoformat(),
                    "date_fin": m_data["date_fin"].isoformat(),
                    "duree_totale_seconds": m_data["duree_totale"].total_seconds(),
                    "cat": m_data["cat"],
                    "channel_id": m_data["channel_id"],
                    "alerte_moitie": m_data["alerte_moitie"],
                    "alerte_un_quart": m_data["alerte_un_quart"],
                    "en_attente": m_data["en_attente"],
                    "points_override": m_data.get("points_override")
                }

    if not actives_serveur:
        await interaction.response.send_message("⚠️ Aucune mission active en cours sur ce serveur à exporter.", ephemeral=True)
        return

    contenu_json = json.dumps(actives_serveur, indent=4, ensure_ascii=False)
    buffer = io.BytesIO(contenu_json.encode("utf-8"))
    buffer.seek(0)

    nom_fichier = f"valerius_missions_actives_{guild_id}.txt"
    fichier_discord = discord.File(buffer, filename=nom_fichier)
    
    await interaction.response.send_message("📥 **Voici le fichier de sauvegarde de toutes les missions en cours :**", file=fichier_discord, ephemeral=True)

@bot.tree.command(name="import_actives", description="Réinjecte les missions en cours à l'aide d'un fichier .txt attaché.")
@app_commands.describe(fichier="Le fichier .txt contenant les missions en cours exportées")
async def import_actives(interaction: discord.Interaction, fichier: discord.Attachment):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    if guild_id not in missions_actives:
        missions_actives[guild_id] = {}

    try:
        contenu_bytes = await fichier.read()
        donnees = json.loads(contenu_bytes.decode("utf-8"))
        
        if "missions_actives" in donnees:
            donnees = donnees["missions_actives"].get(str(guild_id), {})

        nb_restaurees = 0
        for str_j_id, m_data in donnees.items():
            if not str_j_id.isdigit():
                continue
                
            j_id = int(str_j_id)
            missions_actives[guild_id][j_id] = {
                "texte": m_data["texte"],
                "delai_texte": m_data["delai_texte"],
                "date_debut": datetime.fromisoformat(m_data["date_debut"]),
                "date_fin": datetime.fromisoformat(m_data["date_fin"]),
                "duree_totale": timedelta(seconds=m_data["duree_totale_seconds"]),
                "cat": m_data["cat"],
                "channel_id": m_data["channel_id"],
                "alerte_moitie": m_data["alerte_moitie"],
                "alerte_un_quart": m_data["alerte_un_quart"],
                "en_attente": m_data["en_attente"],
                "points_override": m_data.get("points_override")
            }
            nb_restaurees += 1

        await interaction.followup.send(f"✅ **Succès !** {nb_restaurees} missions en cours ont été réinjectées et restaurées avec succès.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur lors de la lecture ou de la réinjection du fichier : {e}", ephemeral=True)

SALON_BACKUP_AUTO_ID = 1539678291919634452
TAILLE_MAX_DISCORD = 8 * 1024 * 1024

def _lister_fichiers_texte_a_sauvegarder():
    """Renvoie la liste de TOUS les fichiers de données du bot à sauvegarder
    (texte/JSON/JSONL/clé), utilisée à la fois par /total_backup et
    /sync_drive — donc les deux restent automatiquement synchronisés entre
    eux.

    Balayage LARGE par préfixe ("valerius_"/"osiris_") plutôt qu'une liste
    de motifs figée au cas par cas : ça évite d'oublier un fichier
    existant (c'est arrivé : rankups, blâmes, demandes de rang,
    notifications, rangs, missions actives, logs, clé secrète... n'étaient
    pas inclus avant) ET couvre automatiquement tout futur fichier de
    données créé plus tard (nouveau système, nouveau bot), sans jamais
    avoir à retoucher cette fonction. Comme Render (plan gratuit) efface
    tout le disque à chaque redémarrage, l'exhaustivité ici est critique."""
    motifs = (
        "valerius_*.json", "valerius_*.txt", "valerius_*.jsonl", "valerius_*.key",
        "osiris_*.json", "osiris_*.txt", "osiris_*.jsonl", "osiris_*.key",
    )
    fichiers = set()
    for motif in motifs:
        fichiers.update(glob.glob(motif))
    return sorted(fichiers)


def _lister_images_boutique():
    """Renvoie la liste des chemins des images de produits de la boutique
    (dossier plat, tous serveurs confondus)."""
    return glob.glob(os.path.join(site_web.DOSSIER_IMAGES_BOUTIQUE, "*"))


def generer_backup_complet():
    donnees_globales = {
        "missions_actives": {},
        "fichiers_disques": {},
        "images_boutique_base64": {},
    }

    for g_id, j_dict in missions_actives.items():
        donnees_globales["missions_actives"][str(g_id)] = {}
        for j_id, m_data in j_dict.items():
            donnees_globales["missions_actives"][str(g_id)][str(j_id)] = {
                "texte": m_data["texte"],
                "delai_texte": m_data["delai_texte"],
                "date_debut": m_data["date_debut"].isoformat(),
                "date_fin": m_data["date_fin"].isoformat(),
                "duree_totale_seconds": m_data["duree_totale"].total_seconds(),
                "cat": m_data["cat"],
                "channel_id": m_data["channel_id"],
                "alerte_moitie": m_data["alerte_moitie"],
                "alerte_un_quart": m_data["alerte_un_quart"],
                "en_attente": m_data["en_attente"],
                "points_override": m_data.get("points_override")
            }

    # Fichiers texte/JSON (profils, missions, boutique, comptes du site...).
    contenu_fichiers = {}
    for f_path in _lister_fichiers_texte_a_sauvegarder():
        if os.path.exists(f_path):
            with open(f_path, "r", encoding="utf-8") as f:
                contenu_fichiers[f_path] = f.read()
    donnees_globales["fichiers_disques"] = contenu_fichiers

    # Images de la boutique (binaires) : encodées en base64 pour pouvoir
    # tenir dans le même fichier JSON de backup que le reste.
    images_base64 = {}
    for chemin_image in _lister_images_boutique():
        try:
            with open(chemin_image, "rb") as f:
                images_base64[os.path.basename(chemin_image)] = base64.b64encode(f.read()).decode("ascii")
        except Exception:
            pass
    donnees_globales["images_boutique_base64"] = images_base64

    json_data = json.dumps(donnees_globales, indent=4, ensure_ascii=False)
    donnees_octets = json_data.encode("utf-8")
    buffer = io.BytesIO(donnees_octets)
    buffer.seek(0)

    timestamp_sauvegarde = datetime.now().strftime("%Y-%m-%d_%H-%M")
    nom_fichier = f"total_backup_valerius_{timestamp_sauvegarde}.json"
    return buffer, nom_fichier, len(donnees_octets)

@bot.tree.command(name="total_backup", description="[Propriétaire] Exporte une archive complete de TOUTES les données du bot (tous serveurs).")
async def total_backup(interaction: discord.Interaction):
    if not est_proprietaire(interaction.user.id):
        await interaction.response.send_message("⛔ Cette sauvegarde contient les données de tous les serveurs : réservée aux Propriétaires.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    buffer, nom_fichier, _taille = generer_backup_complet()
    fichier_discord = discord.File(buffer, filename=nom_fichier)

    await interaction.followup.send("📦 **Voici ta sauvegarde complète !** Les salons, les missions en cours et les profils sont inclus.", file=fichier_discord, ephemeral=True)

@tasks.loop(hours=2)
async def sauvegarde_automatique():
    buffer = None
    try:
        salon = bot.get_channel(SALON_BACKUP_AUTO_ID)
        if salon is None:
            try:
                salon = await bot.fetch_channel(SALON_BACKUP_AUTO_ID)
            except Exception:
                salon = None
        if salon is None:
            print(f"[Sauvegarde auto] Salon {SALON_BACKUP_AUTO_ID} introuvable. Sauvegarde annulée.")
            return

        buffer, nom_fichier, taille = generer_backup_complet()

        if taille > TAILLE_MAX_DISCORD:
            message_erreur = (
                f"⚠️ **Sauvegarde automatique impossible** : le fichier fait "
                f"{taille / (1024 * 1024):.2f} Mo, ce qui dépasse la limite d'envoi Discord."
            )
            print(f"[Sauvegarde auto] {message_erreur}")
            try:
                await salon.send(message_erreur)
            except Exception as e:
                print(f"[Sauvegarde auto] Impossible de prévenir le salon : {e}")
            return

        await salon.send(
            content=f"🗄️ **Sauvegarde automatique** — {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            file=discord.File(buffer, filename=nom_fichier)
        )
        print(f"[Sauvegarde auto] Sauvegarde envoyée : {nom_fichier} ({taille} octets)")
    except Exception as e:
        print(f"[Sauvegarde auto] Erreur pendant la sauvegarde : {e}")
    finally:
        try:
            if buffer is not None:
                buffer.close()
        except Exception:
            pass

@sauvegarde_automatique.before_loop
async def avant_sauvegarde_automatique():
    await bot.wait_until_ready()

# ================= SYNCHRONISATION GOOGLE DRIVE (dossier consultable) =================
# Complète la sauvegarde Discord ci-dessus (archive unique toutes les 2h) par
# une copie FICHIER PAR FICHIER dans un dossier Google Drive personnel,
# consultable directement depuis Drive. Voir stockage_drive.py pour la
# configuration (2 variables d'environnement à définir). Sans configuration,
# toutes les fonctions ci-dessous sont des no-op silencieux : le bot
# continue de fonctionner normalement.

PREFIXE_IMAGE_DRIVE = "boutique_image__"
INTERVALLE_SYNC_DRIVE_MINUTES = 15


def synchroniser_vers_drive():
    """Envoie une copie à jour de chaque fichier de données (JSON/TXT +
    images de la boutique) vers le dossier Drive configuré. Renvoie le
    nombre de fichiers effectivement envoyés (0 si Drive n'est pas
    configuré ou indisponible)."""
    if not stockage_drive.drive_disponible():
        return 0
    nb_envoyes = 0
    for f_path in _lister_fichiers_texte_a_sauvegarder():
        try:
            with open(f_path, "r", encoding="utf-8") as f:
                contenu = f.read().encode("utf-8")
            if stockage_drive.uploader_fichier(f_path, contenu, "application/json"):
                nb_envoyes += 1
        except Exception as e:
            print(f"[Drive] Échec de la lecture de « {f_path} » avant envoi : {e}")
    for chemin_image in _lister_images_boutique():
        try:
            with open(chemin_image, "rb") as f:
                contenu = f.read()
            nom_drive = PREFIXE_IMAGE_DRIVE + os.path.basename(chemin_image)
            if stockage_drive.uploader_fichier(nom_drive, contenu, "application/octet-stream"):
                nb_envoyes += 1
        except Exception as e:
            print(f"[Drive] Échec de la lecture de l'image « {chemin_image} » avant envoi : {e}")
    return nb_envoyes


def restaurer_tout_depuis_drive():
    """Télécharge et réinjecte TOUS les fichiers présents dans le dossier
    Drive configuré. À appeler UNE FOIS au démarrage, avant que le bot ne
    se connecte à Discord, pour retrouver l'état d'avant coupure sur un
    disque Render effacé. Ne fait rien (silencieusement) si Drive n'est
    pas configuré. Renvoie le nombre de fichiers restaurés."""
    if not stockage_drive.drive_disponible():
        print("[Drive] Non configuré (variables d'environnement absentes) — restauration au démarrage ignorée.")
        return 0
    noms = stockage_drive.lister_noms_fichiers()
    nb_restaures = 0
    for nom in noms:
        contenu = stockage_drive.telecharger_fichier(nom)
        if contenu is None:
            continue
        try:
            if nom.startswith(PREFIXE_IMAGE_DRIVE):
                os.makedirs(site_web.DOSSIER_IMAGES_BOUTIQUE, exist_ok=True)
                nom_local = nom[len(PREFIXE_IMAGE_DRIVE):]
                with open(os.path.join(site_web.DOSSIER_IMAGES_BOUTIQUE, nom_local), "wb") as f:
                    f.write(contenu)
            else:
                # Fichiers JSON/TXT : réécrits tels quels, au même chemin
                # relatif que celui utilisé lors de l'envoi.
                with open(nom, "w", encoding="utf-8") as f:
                    f.write(contenu.decode("utf-8"))
            nb_restaures += 1
        except Exception as e:
            print(f"[Drive] Échec de la restauration de « {nom} » : {e}")
    print(f"[Drive] Restauration au démarrage terminée : {nb_restaures} fichier(s) réinjecté(s).")
    return nb_restaures


@tasks.loop(minutes=INTERVALLE_SYNC_DRIVE_MINUTES)
async def synchronisation_drive():
    if not stockage_drive.drive_disponible():
        return
    # uploader_fichier() fait des appels réseau bloquants (googleapiclient
    # n'a pas d'API asyncio) : on les pousse dans un thread pour ne pas
    # geler la boucle d'événements Discord pendant la synchronisation.
    nb = await asyncio.to_thread(synchroniser_vers_drive)
    if nb:
        print(f"[Drive] Synchronisation périodique : {nb} fichier(s) envoyé(s).")

@synchronisation_drive.before_loop
async def avant_synchronisation_drive():
    await bot.wait_until_ready()

@bot.tree.command(name="sync_drive", description="[Propriétaire] Force une synchronisation immédiate vers Google Drive et affiche le statut.")
async def sync_drive(interaction: discord.Interaction):
    if not est_proprietaire(interaction.user.id):
        await interaction.response.send_message("⛔ Réservé aux Propriétaires.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not stockage_drive.drive_disponible():
        await interaction.followup.send(
            "⚠️ Google Drive n'est pas configuré (variables GOOGLE_OAUTH_CLIENT_ID / "
            "GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN / GOOGLE_DRIVE_DOSSIER_ID "
            "manquantes ou invalides). Voir stockage_drive.py pour la marche à suivre.",
            ephemeral=True,
        )
        return
    nb_data = len(_lister_fichiers_texte_a_sauvegarder())
    nb_images = len(_lister_images_boutique())
    nb = await asyncio.to_thread(synchroniser_vers_drive)
    await interaction.followup.send(
        f"✅ Synchronisation Drive terminée : {nb} fichier(s) envoyé(s) "
        f"(sur {nb_data} fichier(s) de données + {nb_images} image(s) détecté(s) localement).",
        ephemeral=True,
    )

def restaurer_donnees_backup(donnees, channel_fallback=None):
    """Réinjecte un backup total (fichiers disque + images boutique +
    missions en cours). Fonction partagée entre /total_restore (Discord)
    et le site web admin."""
    fichiers_disques = donnees.get("fichiers_disques", {})
    for f_path, f_contenu in fichiers_disques.items():
        with open(f_path, "w", encoding="utf-8") as f:
            f.write(f_contenu)

    images_base64 = donnees.get("images_boutique_base64", {})
    if images_base64:
        os.makedirs(site_web.DOSSIER_IMAGES_BOUTIQUE, exist_ok=True)
    for nom_image, contenu_base64 in images_base64.items():
        try:
            chemin = os.path.join(site_web.DOSSIER_IMAGES_BOUTIQUE, os.path.basename(nom_image))
            with open(chemin, "wb") as f:
                f.write(base64.b64decode(contenu_base64))
        except Exception as e:
            print(f"[Restauration] Échec de la réinjection de l'image « {nom_image} » : {e}")

    global missions_actives
    with verrou_missions:
        missions_actives.clear()

    m_actives_sauvegardees = donnees.get("missions_actives", {})
    nb_restaurees = 0
    for str_g_id, j_dict in m_actives_sauvegardees.items():
        g_id = int(str_g_id)
        with verrou_missions:
            missions_actives[g_id] = {}
        guild_obj = bot.get_guild(g_id)

        for str_j_id, m_data in j_dict.items():
            j_id = int(str_j_id)
            old_channel_id = m_data["channel_id"]
            target_channel = bot.get_channel(old_channel_id)
            if not target_channel and guild_obj and channel_fallback:
                target_channel = channel_fallback
            final_channel_id = target_channel.id if target_channel else old_channel_id

            missions_actives[g_id][j_id] = {
                "texte": m_data["texte"],
                "delai_texte": m_data["delai_texte"],
                "date_debut": datetime.fromisoformat(m_data["date_debut"]),
                "date_fin": datetime.fromisoformat(m_data["date_fin"]),
                "duree_totale": timedelta(seconds=m_data["duree_totale_seconds"]),
                "cat": m_data["cat"],
                "channel_id": final_channel_id,
                "alerte_moitie": m_data["alerte_moitie"],
                "alerte_un_quart": m_data["alerte_un_quart"],
                "en_attente": m_data["en_attente"],
                "points_override": m_data.get("points_override")
            }
            nb_restaurees += 1

    return nb_restaurees, len(fichiers_disques) + len(images_base64)

# ================= SAUVEGARDE/RESTAURATION AUTOMATIQUE AUTOUR DE LA MAINTENANCE =================
# Déclenchées automatiquement par le bouton "Activer/Désactiver la
# maintenance" du site web (voir site_web.py, page Sécurité), pour ne
# jamais risquer de perdre l'état du bot pendant une intervention :
# - à l'ACTIVATION : sauvegarde complète immédiate (fichiers + images +
#   missions en cours), envoyée à la fois dans le salon Discord dédié ET
#   synchronisée sur Google Drive, en plus d'être gardée en local dans
#   SNAPSHOT_MAINTENANCE_FICHIER (nommé "valerius_*.json" pour être
#   automatiquement repris par toutes les sauvegardes/synchros futures,
#   sans rien à ajouter ailleurs).
# - à la DÉSACTIVATION : retélécharge d'abord tout depuis Drive (au cas où
#   le disque Render aurait été effacé pendant la maintenance), PUIS
#   réinjecte cet instantané pour restaurer exactement l'état d'avant
#   maintenance, missions en cours comprises.
SNAPSHOT_MAINTENANCE_FICHIER = "valerius_snapshot_avant_maintenance.json"


async def sauvegarder_totale_maintenance():
    """Sauvegarde complète déclenchée à l'activation de la maintenance
    depuis le site (fichier local + Discord + Drive). Ne lève jamais
    d'exception : renvoie un dict de statut affichable directement sur le
    site (voir /admin/securite)."""
    resultat = {"fichier_local": False, "discord": False, "drive": 0}

    try:
        buffer, nom_fichier, taille = generer_backup_complet()
        contenu = buffer.getvalue()
        with open(SNAPSHOT_MAINTENANCE_FICHIER, "wb") as f:
            f.write(contenu)
        resultat["fichier_local"] = True
    except Exception as e:
        resultat["erreur_fichier"] = str(e)
        return resultat

    try:
        salon = bot.get_channel(SALON_BACKUP_AUTO_ID)
        if salon is None:
            try:
                salon = await bot.fetch_channel(SALON_BACKUP_AUTO_ID)
            except Exception:
                salon = None
        if salon is None:
            resultat["erreur_discord"] = f"Salon {SALON_BACKUP_AUTO_ID} introuvable."
        elif taille > TAILLE_MAX_DISCORD:
            resultat["erreur_discord"] = f"Fichier trop volumineux pour Discord ({taille / (1024 * 1024):.2f} Mo)."
        else:
            await salon.send(
                content=f"🛠️ **Sauvegarde automatique avant maintenance** — {datetime.now().strftime('%d/%m/%Y %H:%M')}",
                file=discord.File(io.BytesIO(contenu), filename=nom_fichier),
            )
            resultat["discord"] = True
    except Exception as e:
        resultat["erreur_discord"] = str(e)

    try:
        resultat["drive"] = await asyncio.to_thread(synchroniser_vers_drive)
    except Exception as e:
        resultat["erreur_drive"] = str(e)

    return resultat


async def restaurer_apres_maintenance():
    """Réimportation complète déclenchée à la désactivation de la
    maintenance depuis le site : retélécharge d'abord tout depuis Drive,
    puis réapplique l'instantané pris à l'activation (missions en cours
    comprises). Ne lève jamais d'exception : renvoie un dict de statut
    affichable directement sur le site."""
    resultat = {"drive_restaures": 0, "snapshot_applique": False}

    try:
        resultat["drive_restaures"] = await asyncio.to_thread(restaurer_tout_depuis_drive)
    except Exception as e:
        resultat["erreur_drive"] = str(e)

    try:
        if os.path.exists(SNAPSHOT_MAINTENANCE_FICHIER):
            with open(SNAPSHOT_MAINTENANCE_FICHIER, "r", encoding="utf-8") as f:
                donnees = json.load(f)
            nb_restaurees, nb_fichiers = restaurer_donnees_backup(donnees)
            resultat["snapshot_applique"] = True
            resultat["missions_restaurees"] = nb_restaurees
            resultat["fichiers_restaures"] = nb_fichiers
        else:
            resultat["erreur_snapshot"] = (
                "Aucun instantané local retrouvé (ni sur le disque, ni sur Drive) : "
                "rien à réimporter, l'état actuel n'a pas été modifié."
            )
    except Exception as e:
        resultat["erreur_snapshot"] = str(e)

    # Le snapshot et/ou Drive peuvent contenir une VIEILLE version de
    # valerius_maintenance.json (actif=True, capturée au moment de
    # l'ACTIVATION de la maintenance, puisque ce fichier correspond au
    # motif générique "valerius_*.json" utilisé pour les sauvegardes). On
    # force donc explicitement l'état "désactivé" ici, en dernier, pour
    # garantir qu'aucune restauration ne puisse réactiver la maintenance
    # malgré le clic de désactivation depuis le site.
    definir_maintenance(False)

    return resultat

@bot.tree.command(name="total_restore", description="[Propriétaire] Restaure TOUTES les données du bot (tous serveurs) à partir d'un fichier de backup.")
@app_commands.describe(fichier="Le fichier .json de sauvegarde totale")
async def total_restore(interaction: discord.Interaction, fichier: discord.Attachment):
    if not est_proprietaire(interaction.user.id):
        await interaction.response.send_message("⛔ Cette restauration touche les données de tous les serveurs : réservée aux Propriétaires.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        contenu_bytes = await fichier.read()
        donnees = json.loads(contenu_bytes.decode("utf-8"))
        nb_restaurees, nb_fichiers = restaurer_donnees_backup(donnees, channel_fallback=interaction.channel)
        await interaction.followup.send(f"✅ **Restauration réussie !** {nb_fichiers} fichier(s) et {nb_restaurees} mission(s) en cours réinjectés.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur lors de la restauration du fichier : {e}", ephemeral=True)

@bot.tree.command(name="missions_en_cours", description="Affiche le statut de votre mission active.")
async def missions_en_cours(interaction: discord.Interaction):
    joueur_id = interaction.user.id
    g_id = interaction.guild.id
    if g_id not in missions_actives or joueur_id not in missions_actives[g_id]:
        await interaction.response.send_message("⚪ Tu n'as aucune mission active actuellement sur ce serveur.", ephemeral=True)
        return
    m = missions_actives[g_id][joueur_id]
    ts = int(m["date_fin"].timestamp())
    if m.get("en_attente", False):
        await interaction.response.send_message(f"👤 <@{joueur_id}> [**{m['cat'].upper()}**] -> *\"{m['texte']}\"* 🛑 **GELÉ (En attente d'évaluation)**", ephemeral=True)
    else:
        await interaction.response.send_message(f"👤 <@{joueur_id}> [**{m['cat'].upper()}**] -> *\"{m['texte']}\"* Fin : <t:{ts}:R>", ephemeral=True)

@bot.tree.command(name="missionaccomplie", description="Déclare l'objectif en cours comme accompli.")
async def missionaccomplie(interaction: discord.Interaction):
    joueur = interaction.user
    g_id = interaction.guild.id
    role_instructeur = discord.utils.get(interaction.guild.roles, name="[ 🎴[Instruction] ]")
    mention_ins = role_instructeur.mention if role_instructeur else '@[ 🎴[Instruction] ]'
    
    if g_id in missions_actives and joueur.id in missions_actives[g_id]:
        m_info = missions_actives[g_id][joueur.id]
        if not m_info.get("en_attente", False):
            m_info["en_attente"] = True
            m_info["moment_gel"] = datetime.now()
            
        await interaction.channel.set_permissions(joueur, read_messages=True, send_messages=False)
        await interaction.response.send_message(f"💬 {joueur.mention}, votre demande a été envoyée aux instructeurs. Votre chrono est gelé.")
        
        msg_comp = (
            f"📢 {mention_ins} ! {joueur.mention} déclare avoir fini sa mission : *\"{m_info['texte']}\"* !\n"
            f"⏱️ **Le chrono est mis en pause.** Choisissez l'action appropriée :"
        )
        await envoyer_double_notification(interaction.guild, msg_comp, f"📢 {mention_ins} — <@{joueur.id}> a fini sa mission : *\"{m_info['texte']}\"* dans {interaction.channel.mention}", view=VueEvaluationMission(joueur.id), joueur_id=joueur.id)
        return
    await interaction.response.send_message("❌ Tu n'as aucune mission active en cours sur ce serveur.", ephemeral=True)

@bot.tree.command(name="historique", description="Affiche l'historique de vos décrets passés.")
@app_commands.describe(joueur="Le joueur dont vous voulez voir le casier.")
async def historique(interaction: discord.Interaction, joueur: discord.Member = None):
    cible = joueur or interaction.user
    profils = charger_profils(interaction.guild.id)
    initialiser_profil(cible.id, profils)
    
    userData = profils[str(cible.id)]
    hist = userData["historique"]
    
    embed = discord.Embed(title=f"📜 {cible.display_name}", description="**ARCHIVES ET PARCHEMIN**", color=discord.Color.blue())
    embed.set_thumbnail(url=cible.display_avatar.url)
    embed.set_footer(text=f"ID Discord : {cible.id}")
    embed.add_field(name="📊 Bilan des Objectifs", value=f"🟢 **RÉUSSIES :** `{userData['total_reussies']}`\n🔴 **ÉCHOUÉES :** `{userData['total_echouees']}`\n🏅 **POINTS :** `{userData.get('total_points', 0)}`", inline=False)
    
    if not hist:
        embed.add_field(name="📜 Historique des Décrets", value="*Aucune mission enregistrée dans le grand registre.*", inline=False)
    else:
        hist_lignes = []
        for item in hist:
            icone = "✅" if item["statut"] == "Succès" else "❌"
            cat_nom = item.get("categorie", "inconnu").upper()
            hist_lignes.append(f"{icone} **[{item['date']}]** `[{cat_nom}]` — {item['texte']}")
            
        corps_historique = "\n".join(hist_lignes)
        if len(corps_historique) > 1024: corps_historique = corps_historique[:1000] + "\n*...*"
        embed.add_field(name="📜 Historique des Décrets", value=corps_historique, inline=False)
        
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ajouterhistorique", description="Ajoute manuellement une mission dans l'historique et les compteurs d'un joueur.")
@app_commands.describe(joueur="Le citoyen ciblé", statut="Succes ou Echec", categorie="commune, moyenne, difficile ou royal", texte="Description de la mission")
@app_commands.choices(statut=[
    app_commands.Choice(name="Succès", value="Succès"),
    app_commands.Choice(name="Echec", value="Échec")
])
@app_commands.choices(categorie=[
    app_commands.Choice(name="Commune", value="commune"),
    app_commands.Choice(name="Moyenne", value="moyenne"),
    app_commands.Choice(name="Difficile", value="difficile"),
    app_commands.Choice(name="Royal", value="royal")
])
async def ajouterhistorique(interaction: discord.Interaction, joueur: discord.Member, statut: str, categorie: str, texte: str):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    g_id = interaction.guild.id
    profils = charger_profils(g_id)
    initialiser_profil(joueur.id, profils)

    points_gagnes = 0
    if statut == "Succès":
        profils[str(joueur.id)]["total_reussies"] += 1
        points_gagnes = points_pour_categorie(g_id, categorie)
        profils[str(joueur.id)]["total_points"] += points_gagnes
    else:
        profils[str(joueur.id)]["total_echouees"] += 1

    ajouter_historique(joueur.id, profils, texte, statut, categorie, points=points_gagnes)
    sauvegarder_profils(g_id, profils)

    texte_points = f"\n🏅 +{points_gagnes} points" if statut == "Succès" else ""
    await interaction.response.send_message(f"✅ Ajouté avec succès dans l'historique de {joueur.mention} !\nStatut : **{statut}** | Catégorie : **{categorie.upper()}** — *{texte}*{texte_points}", ephemeral=True)

@bot.tree.command(name="retirerhistorique", description="Retire une entrée de l'historique d'un joueur et ajuste ses compteurs.")
@app_commands.describe(joueur="Le citoyen ciblé", position="Position dans l'historique (1 = la plus récente)")
async def retirerhistorique(interaction: discord.Interaction, joueur: discord.Member, position: int = 1):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    g_id = interaction.guild.id
    profils = charger_profils(g_id)
    initialiser_profil(joueur.id, profils)
    hist = profils[str(joueur.id)]["historique"]
    index = position - 1

    if index < 0 or index >= len(hist):
        await interaction.response.send_message(f"❌ Position invalide. Cet historique contient {len(hist)} entrée(s).", ephemeral=True)
        return

    entree = hist.pop(index)
    if entree.get("statut") == "Succès":
        profils[str(joueur.id)]["total_reussies"] = max(0, profils[str(joueur.id)]["total_reussies"] - 1)
        profils[str(joueur.id)]["total_points"] = max(0, profils[str(joueur.id)].get("total_points", 0) - entree.get("points", 0))
    else:
        profils[str(joueur.id)]["total_echouees"] = max(0, profils[str(joueur.id)]["total_echouees"] - 1)
    sauvegarder_profils(g_id, profils)

    await interaction.response.send_message(f"🗑️ Entrée retirée de l'historique de {joueur.mention} : *\"{entree.get('texte', '?')}\"* ({entree.get('statut', '?')}).", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - HISTORIQUE : {interaction.user.name} a retiré une entrée de l'historique de {joueur.name} sur {interaction.guild.name}")

@bot.tree.command(name="temps_mission", description="Ajoute ou retire du temps sur la mission active d'un joueur.")
@app_commands.describe(joueur="Le joueur concerné", duree="Durée à ajouter/retirer (ex: 2h, 1 jour, 30min)", retirer="Retirer ce temps au lieu de l'ajouter")
async def temps_mission(interaction: discord.Interaction, joueur: discord.Member, duree: str, retirer: bool = False):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    g_id = interaction.guild.id
    if g_id not in missions_actives or joueur.id not in missions_actives[g_id]:
        await interaction.response.send_message(f"❌ {joueur.mention} n'a aucune mission active sur ce serveur.", ephemeral=True)
        return

    delta = extraire_duree(duree)
    m_info = missions_actives[g_id][joueur.id]
    ancienne_echeance = m_info["date_fin"]

    if retirer:
        m_info["date_fin"] -= delta
        m_info["duree_totale"] -= delta
    else:
        m_info["date_fin"] += delta
        m_info["duree_totale"] += delta

    # Garde-fous : jamais de durée totale nulle/négative ni d'échéance déjà dans le passé
    if m_info["duree_totale"].total_seconds() <= 0:
        m_info["duree_totale"] = timedelta(seconds=1)
    if m_info["date_fin"] <= datetime.now():
        m_info["date_fin"] = datetime.now() + timedelta(seconds=1)

    maintenant = datetime.now()
    timestamp_ancien = int(ancienne_echeance.timestamp())
    timestamp_nouveau = int(m_info["date_fin"].timestamp())
    verbe = "retiré" if retirer else "ajouté"
    emoji_verbe = "➖" if retirer else "➕"
    couleur = discord.Color.orange() if retirer else discord.Color.green()

    temps_ecoule = maintenant - m_info["date_debut"]
    barre, ratio = barre_progression(temps_ecoule, m_info["duree_totale"])
    temps_restant_txt = formater_duree(m_info["date_fin"] - maintenant)

    # Ré-arme les alertes si on redonne assez de marge, pour qu'elles se redéclenchent normalement
    if not retirer:
        if ratio < 0.5: m_info["alerte_moitie"] = False
        if ratio < 0.75: m_info["alerte_un_quart"] = False

    embed_temps = discord.Embed(title="⏱️ CHRONO DE MISSION MODIFIÉ", color=couleur)
    embed_temps.add_field(name="🎯 Objectif", value=f"*{m_info['texte']}*", inline=False)
    embed_temps.add_field(name=f"{emoji_verbe} Modification", value=f"**{duree}** {verbe} par {interaction.user.mention}", inline=False)
    embed_temps.add_field(name="📆 Ancienne échéance", value=f"<t:{timestamp_ancien}:f>", inline=True)
    embed_temps.add_field(name="🆕 Nouvelle échéance", value=f"<t:{timestamp_nouveau}:R>\n(<t:{timestamp_nouveau}:f>)", inline=True)
    embed_temps.add_field(name="⏳ Temps restant", value=f"`{temps_restant_txt}`", inline=True)
    embed_temps.add_field(name="📊 Progression du chrono", value=f"{barre} `{round(ratio * 100)}%`", inline=False)
    embed_temps.set_thumbnail(url=joueur.display_avatar.url)
    embed_temps.set_footer(text="Le décompte a été ajusté par l'administration — reste attentif à l'échéance.")

    await interaction.response.send_message(f"✅ Temps mis à jour pour {joueur.mention}.", ephemeral=True)

    salon_ticket = bot.get_channel(m_info["channel_id"]) or interaction.channel
    try:
        await salon_ticket.send(content=f"{joueur.mention}", embed=embed_temps)
    except Exception as e:
        await envoyer_log_proprietaire(bot, f"LOG ABSOLU - ERREUR NOTIF TEMPS MISSION : impossible de notifier le ticket de {joueur.name} sur {interaction.guild.name} ({e})")

    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - TEMPS MISSION : {interaction.user.name} a {verbe} {duree} sur la mission de {joueur.name} sur {interaction.guild.name}")


@app_commands.describe(joueur="Le citoyen propriétaire du ticket d'ordre")
async def mission_expiration(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire pour exécuter cette sentence.", ephemeral=True)
        return
    
    g_id = interaction.guild.id
    if g_id in missions_actives and joueur.id in missions_actives[g_id]:
        await interaction.response.send_message("❌ Impossible de lancer l'expiration : une mission est déjà activement en cours pour ce joueur sur ce serveur.", ephemeral=True)
        return

    expiration_time = int((datetime.now() + timedelta(hours=1)).timestamp())
    msg_alerte = (
        f"⚠️ {joueur.mention}, **attention : cet ordre de mission va être supprimé <t:{expiration_time}:R> (<t:{expiration_time}:t>)** car aucune mission n'a été sélectionnée.\n"
        f" Veuillez choisir un décret avant la fin du décompte réglementaire."
    )
    
    await interaction.response.send_message("🚨 Alerte d'inactivité lancée. Le salon expirera dans une heure si aucune action n'est entreprise.")
    await interaction.channel.send(msg_alerte)
    
    target_channel_id = interaction.channel.id
    await asyncio.sleep(3600)
    
    if g_id not in missions_actives or joueur.id not in missions_actives[g_id]:
        channel_to_del = bot.get_channel(target_channel_id)
        if channel_to_del:
            try:
                await channel_to_del.delete(reason="Expiration de l'ordre de mission")
                await envoyer_double_notification(interaction.guild, "", f"🗑️ Le ticket d'ordre de {joueur.mention} a été automatiquement supprimé pour inactivité.")
            except Exception as e:
                print(f"Erreur suppression salon expiré: {e}")

@bot.tree.command(name="tutoadm", description="Manuel réglementaire pour l'administration des ordres.")
async def tutoadm(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Tu n'as pas l'autorité nécessaire.", ephemeral=True)
        return
    embed_tuto = discord.Embed(
        title="👑 MANUEL DE L'ADMINISTRATION & DE L'INSTRUCTION 👑",
        description="Ce guide récapitule vos privilèges pour encadrer le système de missions de Valerius.",
        color=discord.Color.red()
    )
    embed_tuto.add_field(
        name="📥 1. Gestion des Demandes",
        value="Lorsqu'un joueur finit son ordre, l'alerte dans `#validation-mission` et dans vos messages privés contient les boutons d'évaluation (`Accepter`, `Refuser`, `Demander des preuves`).",
        inline=False
    )
    embed_tuto.add_field(
        name="🛠️ 2. Commandes d'Urgence Manuelles",
        value="`/openticket @joueur` -> Ouvrir un ticket\n`/fermerticket` -> Fermer instantanément un salon de ticket\n`/attribuer_mission` -> Assigner une mission auto\n`/ajouterhistorique @joueur [Succes/Echec] [categorie] [texte]` -> Ajouter une mission à l'historique\n`/export_actives` & `/import_actives` -> Sauvegarder/Recharger les missions en cours (ce serveur)\n`/missionaccepter` / `/missionrefuser` / `/missionpreuve`\n`/points_config [categorie] [points]` -> Définir combien de points rapporte une catégorie\n`/points_categories` -> Voir la config actuelle des points",
        inline=False
    )
    embed_tuto.add_field(
        name="⚖️ 3. Décrets & Discipline (sur le bot Osiris ⚖️)",
        value="`/rankup @joueur @nouveau_rang` -> Publie le Décret Royal de promotion (retire aussi l'ancien rôle si précisé)\n`/derank @joueur @ancien_rang` -> Publie le Décret de Rétrogradation (attribue un nouveau rôle si précisé)\n`/rangs @joueur` -> Historique des rankups/déranks du joueur\n`/retirerrang @joueur [numero]` -> Retire une entrée de l'historique des rangs\n\n`/blam @joueur [raison]` -> Inflige un blâme (expire seul après 2 semaines ; avertissement auto à 2, procès au-delà de 7)\n`/blames @joueur` -> Liste ses blâmes actifs\n`/retirerblam @joueur [numero]` -> Retire un blâme précis (gérable aussi depuis le site web)",
        inline=False
    )
    embed_tuto.add_field(
        name="🔮 3bis. Intelligence Royale (IA — Valerius)",
        value="`/ia [question]` -> Pose une question à l'IA de Valerius (gratuite, aussi accessible depuis le site)\n`/ia_reset` -> Réinitialise la mémoire de conversation de l'IA sur ce salon",
        inline=False
    )
    embed_tuto.add_field(
        name="🛡️ 4. Ton rang sur ce serveur",
        value=(f"🛡️ Super Modo (limité à **{interaction.guild.name}**)" if est_super_modo(interaction.user.id, interaction.guild.id) and not est_proprietaire(interaction.user.id)
               else "👑 Propriétaire (accès total, tous serveurs)" if est_proprietaire(interaction.user.id)
               else "🚨 Staff (rôle/permission du serveur)"),
        inline=False
    )
    if est_proprietaire(interaction.user.id):
        embed_tuto.add_field(
            name="👑 5. Réservé aux Propriétaires",
            value="`/total_backup` & `/total_restore` -> Sauvegarde/Restauration de **TOUS** les serveurs\n`/ajouter_proprietaire` / `/retirer_proprietaire` / `/liste_proprietaires`\n`/nommer_supermodo` / `/revoquer_supermodo` -> Un Super Modo n'agit que sur le serveur où il est nommé\n`/changer_code` -> Code d'activation global",
            inline=False
        )
    embed_tuto.set_footer(text="Valerius Pro • Manuel de l'administration")
    await interaction.response.send_message(embed=embed_tuto, ephemeral=True)

@bot.tree.command(name="missionaccepter", description="Valide et force manuellement le succès de la mission d'un joueur.")
@app_commands.describe(joueur="Le citoyen à valider")
async def missionaccepter(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer()
    reussite = await action_accepter_mission(joueur.id, interaction.channel)
    if reussite:
        await interaction.followup.send(f"✅ Mission de {joueur.mention} acceptée manuellement.")
    else:
        await interaction.followup.send("❌ Ce joueur n'a aucune mission active sur ce serveur.")

@bot.tree.command(name="missionrefuser", description="Force manuellement l'échec de la mission d'un joueur.")
@app_commands.describe(joueur="Le citoyen à pénaliser")
async def missionrefuser(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer()
    reussite = await action_refuser_mission(joueur.id, interaction.channel)
    if reussite:
        await interaction.followup.send(f"❌ Mission de {joueur.mention} refusée avec échec consigné.")
    else:
        await interaction.followup.send("❌ Ce joueur n'a aucune mission active sur ce serveur.")

@bot.tree.command(name="missionpreuve", description="Exige l'envoi d'une capture d'écran de preuve dans le ticket.")
@app_commands.describe(joueur="Le citoyen ciblé")
async def missionpreuve(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer()
    reussite = await action_demander_preuve(joueur.id, interaction.channel, interaction.guild)
    if reussite:
        await interaction.followup.send(f"📸 Demande de preuve transmise à {joueur.mention}.")
    else:
        await interaction.followup.send("❌ Ce joueur n'a aucune mission active sur ce serveur.")

@bot.tree.command(name="resetmissions", description="Supprime et vide définitivement toutes les missions de ce serveur.")
async def resetmissions(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    vider_toutes_missions(interaction.guild.id)
    await interaction.response.send_message("🗑️ **Toutes les missions de ce serveur ont été effacées avec succès !**", ephemeral=True)

@bot.tree.command(name="listemissions", description="Affiche l'index complet du catalogue des décrets.")
async def listemissions(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return

    missions_dispo = charger_missions_fichier(interaction.guild.id)
    lignes = ["⚖️ **ARCHIVES DES MISSIONS DISPONIBLES (SUR CE SERVEUR)** ⚖️ \n"]
    
    for cat in ["commune", "moyenne", "difficile", "royal"]:
        lignes.append(f"\n__**{cat.upper()} :**__\n")
        if not missions_dispo[cat]:
            lignes.append("*Aucune mission disponible*\n")
        else:
            for i, m in enumerate(missions_dispo[cat], start=1):
                lignes.append(f"**{i}.** {m['texte']} *(Délai : {m['delai']})*\n")
    
    messages = []
    message_actuel = ""
    for ligne in lignes:
        if len(message_actuel) + len(ligne) > 1900:
            messages.append(message_actuel)
            message_actuel = ligne
        else:
            message_actuel += ligne
            
    if message_actuel:
        messages.append(message_actuel)
        
    await interaction.response.send_message(messages[0], ephemeral=True)
    for msg in messages[1:]:
        await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="points_config", description="[Staff] Définit combien de points rapporte n'importe quelle mission d'une catégorie.")
@app_commands.describe(categorie="commune, moyenne, difficile ou royal", points="Nombre de points que rapportera CHAQUE mission de cette catégorie")
@app_commands.choices(categorie=[
    app_commands.Choice(name="Commune", value="commune"),
    app_commands.Choice(name="Moyenne", value="moyenne"),
    app_commands.Choice(name="Difficile", value="difficile"),
    app_commands.Choice(name="Royal", value="royal"),
])
async def points_config(interaction: discord.Interaction, categorie: app_commands.Choice[str], points: int):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    if points < 0:
        await interaction.response.send_message("❌ Le nombre de points ne peut pas être négatif.", ephemeral=True)
        return
    g_id = interaction.guild.id
    config = charger_points_categories(g_id)
    config[categorie.value] = points
    sauvegarder_points_categories(g_id, config)
    await interaction.response.send_message(f"✅ Toutes les missions **{categorie.name.upper()}** rapportent désormais **{points} points** sur ce serveur.", ephemeral=True)
    await envoyer_log_proprietaire(bot, f"LOG ABSOLU - POINTS CONFIG : {interaction.user.name} a fixé les points de la catégorie {categorie.value} à {points} sur {interaction.guild.name}")

@bot.tree.command(name="points_categories", description="Affiche combien de points rapporte chaque catégorie de mission sur ce serveur.")
async def points_categories_cmd(interaction: discord.Interaction):
    config = charger_points_categories(interaction.guild.id)
    lignes = "\n".join(f"• **{cat.upper()}** : `{pts}` points" for cat, pts in config.items())
    embed = discord.Embed(title="🏅 Points par catégorie de mission", description=lignes, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="addmission", description="Ajoute une nouvelle quête au catalogue global du serveur.")
@app_commands.describe(categorie="commune, moyenne, difficile, royal", texte="Contenu de l'objectif", temps="Exemple: 2h, 3j, 45min", points="(Optionnel) Points spécifiques à CETTE mission, sinon ceux de sa catégorie")
async def addmission(interaction: discord.Interaction, categorie: str, texte: str, temps: str, points: int = None):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    cat = categorie.lower().strip()
    if cat in ["commune", "commun"]: cat = "commune"
    elif cat in ["moyenne", "moyen"]: cat = "moyenne"
    elif cat in ["difficile"]: cat = "difficile"
    elif cat in ["royal", "royale"]: cat = "royal"
    else:
        await interaction.response.send_message("❌ Catégorie invalide.", ephemeral=True)
        return
    if points is not None and points < 0:
        await interaction.response.send_message("❌ Le nombre de points ne peut pas être négatif.", ephemeral=True)
        return

    sauvegarder_mission_fichier(interaction.guild.id, cat, texte, temps, points=points)
    texte_points = f" — 🏅 {points} pts (surcharge)" if points is not None else ""
    await interaction.response.send_message(f"⚖️ **Mission ajoutée pour ce serveur !** (`{cat}` : *{texte}* pendant {temps}{texte_points})", ephemeral=True)

@bot.tree.command(name="delmission", description="Supprime une mission existante du fichier de configuration.")
@app_commands.describe(categorie="commune, moyenne, difficile, royal", numero="Le numéro affiché sur le /listemissions")
async def delmission(interaction: discord.Interaction, categorie: str, numero: int):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    cat = categorie.lower().strip()
    if cat in ["commune", "commun"]: cat = "commune"
    elif cat in ["moyenne", "moyen"]: cat = "moyenne"
    elif cat in ["difficile"]: cat = "difficile"
    elif cat in ["royal", "royale"]: cat = "royal"
    
    index = numero - 1
    guild_id = interaction.guild.id
    missions_dispo = charger_missions_fichier(guild_id)
    if cat in missions_dispo and 0 <= index < len(missions_dispo[cat]):
        retiree = missions_dispo[cat].pop(index)
        reecrire_toutes_missions(guild_id, missions_dispo)
        await interaction.response.send_message(f"🗑️ Mission *\"{retiree['texte']}\"* supprimée de l'index de ce serveur.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Numéro introuvable dans cette catégorie.", ephemeral=True)

# ================= INTELLIGENCE ROYALE DE VALERIUS (commandes) =================
@bot.tree.command(name="ia", description="Pose une question à l'Intelligence Royale de Valerius (IA gratuite).")
@app_commands.describe(question="Ta question pour l'IA")
async def ia(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    texte, erreur = await interroger_ia(
        _cle_ia(interaction), question,
        guild_id=interaction.guild.id if interaction.guild else None,
        joueur_id=interaction.user.id,
    )
    if erreur:
        await interaction.followup.send(erreur, ephemeral=True)
        return

    morceaux = decouper_texte(texte, 4000)
    premier_embed = discord.Embed(
        title="🔮 Intelligence Royale de Valerius",
        description=morceaux[0],
        color=discord.Color.blurple()
    )
    premier_embed.set_footer(text=f"Demandé par {interaction.user.display_name}")
    await interaction.followup.send(embed=premier_embed)
    for suite in morceaux[1:]:
        await interaction.channel.send(embed=discord.Embed(description=suite, color=discord.Color.blurple()))

@bot.tree.command(name="ia_reset", description="Efface la mémoire de conversation de l'IA pour ce salon (repart de zéro).")
async def ia_reset(interaction: discord.Interaction):
    reinitialiser_historique_ia(interaction)
    await interaction.response.send_message("🧹 Mémoire de l'Intelligence Royale réinitialisée pour ce salon.", ephemeral=True)

# ================= API NATIONSGLORY (commandes publiques) =================
NOMS_COMPETENCES = {
    "miner": "⛏️ Mineur",
    "lumberjack": "🪓 Bûcheron",
    "farmer": "🌾 Fermier",
    "builder": "🔨 Bâtisseur",
    "hunter": "🏹 Chasseur",
    "engineer": "⚙️ Ingénieur",
}

@bot.tree.command(name="profil_ng", description="Affiche le profil NationsGlory d'un joueur (par défaut sur le serveur Mocha).")
@app_commands.describe(pseudo="Pseudo exact du joueur NationsGlory", serveur="Serveur NationsGlory (défaut : mocha)")
async def profil_ng(interaction: discord.Interaction, pseudo: str, serveur: str = "mocha"):
    await interaction.response.defer(thinking=True)
    donnees, erreur = _requete_nationsglory(f"/user/{pseudo}")
    if erreur:
        await interaction.followup.send(f"❌ {erreur}", ephemeral=True)
        return

    serveur = (serveur or "mocha").strip().lower()
    infos = (donnees.get("servers") or {}).get(serveur)
    skin = donnees.get("skin") or {}

    embed = discord.Embed(
        title=f"🎮 Profil NationsGlory — {donnees.get('username', pseudo)}",
        color=discord.Color.gold(),
    )
    if skin.get("head"):
        embed.set_thumbnail(url=skin["head"])
    embed.add_field(name="📅 Compte créé le", value=donnees.get("created_at") or "Inconnu", inline=True)
    embed.add_field(name="🕐 Dernière connexion (globale)", value=donnees.get("last_connection") or "Inconnue", inline=True)

    if not infos:
        embed.add_field(name="⚠️ Serveur", value=f"Aucune donnée pour ce joueur sur « {serveur} ».", inline=False)
    else:
        statut = "🟢 En ligne" if infos.get("online") else "🔴 Hors ligne"
        pays = infos.get("country") or "Sans pays"
        rang = infos.get("country_rank") or "—"
        power = infos.get("power")
        max_power = infos.get("max_power")
        temps_jeu = _formater_duree_secondes(infos.get("playtime")) or "Inconnu"

        embed.add_field(name=f"🌍 Serveur {serveur.capitalize()}", value=statut, inline=True)
        embed.add_field(name="🏳️ Pays", value=pays, inline=True)
        embed.add_field(name="🎖️ Rang", value=rang, inline=True)
        embed.add_field(name="💪 Power", value=f"{power}/{max_power}" if power is not None else "—", inline=True)
        embed.add_field(name="⏱️ Temps de jeu", value=temps_jeu, inline=True)
        embed.add_field(name="🕐 Dernière connexion (serveur)", value=infos.get("last_connection") or "Inconnue", inline=True)

        competences = infos.get("skills")
        if isinstance(competences, dict):
            lignes = [f"{NOMS_COMPETENCES.get(cle, cle.capitalize())} : **{valeur}**" for cle, valeur in competences.items()]
            embed.add_field(name="🛠️ Compétences", value="\n".join(lignes), inline=False)
        else:
            embed.add_field(name="🛠️ Compétences", value="Non disponibles sur ce serveur.", inline=False)

    embed.set_footer(text="Données officielles NationsGlory")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="pays_ng", description="Affiche les informations d'un pays NationsGlory (par défaut sur le serveur Mocha).")
@app_commands.describe(pays="Nom exact du pays NationsGlory", serveur="Serveur NationsGlory (défaut : mocha)")
async def pays_ng(interaction: discord.Interaction, pays: str, serveur: str = "mocha"):
    await interaction.response.defer(thinking=True)
    serveur = (serveur or "mocha").strip().lower()
    donnees, erreur = _requete_nationsglory(f"/country/{serveur}/{pays}")
    if erreur:
        await interaction.followup.send(f"❌ {erreur}", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🏰 {donnees.get('name', pays)}",
        description=donnees.get("description") or None,
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="🌍 Serveur", value=(donnees.get("server") or serveur).capitalize(), inline=True)
    embed.add_field(name="👑 Chef", value=donnees.get("leader") or "Inconnu", inline=True)
    embed.add_field(name="📅 Créé le", value=donnees.get("creation_date") or "Inconnue", inline=True)
    embed.add_field(name="👥 Membres", value=str(donnees.get("count_members", 0)), inline=True)
    power = donnees.get("power")
    power_max = donnees.get("maxpower")
    embed.add_field(name="💪 Power", value=f"{power}/{power_max}" if power is not None else "—", inline=True)
    embed.add_field(name="📊 MMR / Niveau", value=f"{donnees.get('mmr', '—')} / {donnees.get('level', '—')}", inline=True)

    allies = donnees.get("allies") or []
    ennemis = donnees.get("ennemies") or []
    embed.add_field(name="🤝 Alliés", value=", ".join(allies) if allies else "Aucun", inline=False)
    embed.add_field(name="⚔️ Ennemis", value=", ".join(ennemis) if ennemis else "Aucun", inline=False)

    fichier_drapeau = None
    drapeau_b64 = donnees.get("flag")
    if drapeau_b64:
        try:
            import base64
            octets_drapeau = base64.b64decode(drapeau_b64)
            fichier_drapeau = discord.File(io.BytesIO(octets_drapeau), filename="drapeau.png")
            embed.set_thumbnail(url="attachment://drapeau.png")
        except Exception:
            fichier_drapeau = None

    embed.set_footer(text="Données officielles NationsGlory")
    if fichier_drapeau:
        await interaction.followup.send(embed=embed, file=fichier_drapeau)
    else:
        await interaction.followup.send(embed=embed)

# ================= RANKUP / DÉRANK — "OSIRIS" (commandes) =================
@bot_osiris.tree.command(name="rankup", description="Décret Royal : promeut un joueur à un nouveau rang et publie l'annonce officielle.")
@app_commands.describe(
    joueur="Le citoyen promu",
    nouveau_rang="Le rôle Discord correspondant au nouveau rang",
    ancien_rang="(Optionnel) Rôle à retirer au joueur",
    salon="(Optionnel) Salon où publier le décret (par défaut : salon actuel)"
)
async def rankup(interaction: discord.Interaction, joueur: discord.Member, nouveau_rang: discord.Role, ancien_rang: discord.Role = None, salon: discord.TextChannel = None):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    erreurs = []
    try:
        await joueur.add_roles(nouveau_rang, reason=f"Rankup automatique par {interaction.user}")
    except Exception as e:
        erreurs.append(f"Impossible d'ajouter le rôle **{nouveau_rang.name}** : {e}")

    if ancien_rang:
        try:
            await joueur.remove_roles(ancien_rang, reason=f"Rankup automatique par {interaction.user}")
        except Exception as e:
            erreurs.append(f"Impossible de retirer le rôle **{ancien_rang.name}** : {e}")

    salon_cible = salon or interaction.channel
    message_decret = _texte_decret_royal(joueur.mention, f"{nouveau_rang.mention}")
    try:
        await salon_cible.send(message_decret)
    except Exception as e:
        erreurs.append(f"Impossible d'envoyer le décret dans {salon_cible.mention} : {e}")

    ajouter_rankup(interaction.guild.id, joueur.id, "promotion", ancien_rang.name if ancien_rang else None, nouveau_rang.name, interaction.user.id)
    await envoyer_log_proprietaire(bot_osiris, f"[{interaction.guild.name}] 👑 Rankup : {joueur} promu à {nouveau_rang.name} par {interaction.user}.")

    if erreurs:
        await interaction.followup.send("⚠️ Décret publié avec des erreurs :\n" + "\n".join(erreurs), ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Décret Royal publié dans {salon_cible.mention} !", ephemeral=True)

@bot_osiris.tree.command(name="derank", description="Rétrograde un joueur : retire son rang et publie l'annonce officielle.")
@app_commands.describe(
    joueur="Le citoyen rétrogradé",
    ancien_rang="Le rôle Discord à retirer (rang actuel du joueur)",
    nouveau_rang="(Optionnel) Rôle à attribuer après la rétrogradation",
    raison="(Optionnel) Motif de la rétrogradation",
    salon="(Optionnel) Salon où publier le décret (par défaut : salon actuel)"
)
async def derank(interaction: discord.Interaction, joueur: discord.Member, ancien_rang: discord.Role, nouveau_rang: discord.Role = None, raison: str = None, salon: discord.TextChannel = None):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    erreurs = []
    try:
        await joueur.remove_roles(ancien_rang, reason=f"Dérank par {interaction.user}" + (f" ({raison})" if raison else ""))
    except Exception as e:
        erreurs.append(f"Impossible de retirer le rôle **{ancien_rang.name}** : {e}")

    if nouveau_rang:
        try:
            await joueur.add_roles(nouveau_rang, reason=f"Dérank par {interaction.user}" + (f" ({raison})" if raison else ""))
        except Exception as e:
            erreurs.append(f"Impossible d'ajouter le rôle **{nouveau_rang.name}** : {e}")

    salon_cible = salon or interaction.channel
    message_decret = (
        "◈═══════◈ ◈═══════◈ **Décret de Rétrogradation** ◈═══════◈ ◈═══════◈\n\n"
        f"Par ordre du Palais Royal, {joueur.mention} est rétrogradé du rang de {ancien_rang.mention}"
        + (f" au rang de {nouveau_rang.mention}" if nouveau_rang else "")
        + ".\n\n"
        + (f"**Motif :** {raison}\n\n" if raison else "")
        + "*Hasina ho an'ny Fanjakana! Gloire au Royaume.*"
    )
    try:
        await salon_cible.send(message_decret)
    except Exception as e:
        erreurs.append(f"Impossible d'envoyer le décret dans {salon_cible.mention} : {e}")

    # MP personnel au joueur rétrogradé (envoyé une seule fois : la commande
    # /derank n'est déclenchée qu'une fois par le staff pour cet événement,
    # contrairement au rappel d'éligibilité au rankup qui, lui, tourne en
    # boucle et se protège via le drapeau 'eligibilite_notifiee').
    texte_mp = (
        f"⬇️ Tu as été rétrogradé du rang de **{ancien_rang.name}**"
        + (f" vers **{nouveau_rang.name}**" if nouveau_rang else "")
        + "."
        + (f"\n**Motif :** {raison}" if raison else "")
    )
    try:
        await joueur.send(f"⬇️ **Osiris — Décret de Rétrogradation**\n{texte_mp}")
    except Exception as e:
        erreurs.append(f"Impossible d'envoyer le MP à {joueur.mention} (MP fermés ?) : {e}")
    ajouter_notification(interaction.guild.id, joueur.id, texte_mp, categorie="rankup")

    ajouter_rankup(interaction.guild.id, joueur.id, "retrogradation", ancien_rang.name, nouveau_rang.name if nouveau_rang else None, interaction.user.id, raison)
    await envoyer_log_proprietaire(bot_osiris, f"[{interaction.guild.name}] ⬇️ Dérank : {joueur} rétrogradé de {ancien_rang.name} par {interaction.user}" + (f" ({raison})" if raison else "") + ".")

    if erreurs:
        await interaction.followup.send("⚠️ Décret publié avec des erreurs :\n" + "\n".join(erreurs), ephemeral=True)
    else:
        await interaction.followup.send(f"✅ Décret de rétrogradation publié dans {salon_cible.mention} !", ephemeral=True)

@bot_osiris.tree.command(name="rangs", description="Affiche l'historique des rankups/déranks d'un joueur.")
@app_commands.describe(joueur="Le citoyen concerné")
async def rangs(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    historique = obtenir_rankups(interaction.guild.id, joueur.id)
    if not historique:
        await interaction.response.send_message(f"ℹ️ {joueur.mention} n'a aucun changement de rang enregistré.", ephemeral=True)
        return

    embed = discord.Embed(title=f"👑 Historique des rangs de {joueur.display_name}", color=discord.Color.gold())
    for i, r in enumerate(historique[:15], start=1):
        promotion = r.get("type") == "promotion"
        emoji = "⬆️" if promotion else "⬇️"
        titre = f"{emoji} {'Promotion' if promotion else 'Rétrogradation'} n°{i} — {r.get('date', 'date inconnue')}"
        valeur = f"{r.get('ancien_rang') or '—'} ➜ {r.get('nouveau_rang') or '—'}"
        if r.get("raison"):
            valeur += f"\nMotif : {r['raison']}"
        embed.add_field(name=titre, value=valeur, inline=False)
    if len(historique) > 15:
        embed.set_footer(text=f"{len(historique)} changement(s) au total — les 15 plus récents sont affichés")
    else:
        embed.set_footer(text=f"{len(historique)} changement(s) de rang enregistré(s)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot_osiris.tree.command(name="retirerrang", description="Retire une entrée précise de l'historique des rangs d'un joueur (numéro visible via /rangs).")
@app_commands.describe(joueur="Le citoyen concerné", numero="Le numéro de l'entrée à retirer (voir /rangs)")
async def retirerrang(interaction: discord.Interaction, joueur: discord.Member, numero: int):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    retire = retirer_rankup_par_index(interaction.guild.id, joueur.id, numero - 1)
    if retire:
        await interaction.response.send_message(f"🗑️ Entrée n°{numero} retirée de l'historique des rangs de {joueur.mention}.", ephemeral=True)
        await envoyer_log_proprietaire(bot_osiris, f"[{interaction.guild.name}] 🗑️ Entrée de rang retirée pour {joueur} par {interaction.user}.")
    else:
        await interaction.response.send_message("❌ Entrée introuvable pour ce joueur (vérifie le numéro avec /rangs).", ephemeral=True)

@bot_osiris.tree.command(name="blam", description="Inflige un blâme à un joueur (expire automatiquement après 2 semaines).")
@app_commands.describe(joueur="Le citoyen concerné", raison="Motif du blâme")
async def blam(interaction: discord.Interaction, joueur: discord.Member, raison: str):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer()

    nouveau, nb = ajouter_blame(interaction.guild.id, joueur.id, raison, interaction.user.id)
    mp_envoye = await envoyer_notification_blame(interaction.guild, nouveau, nb)

    embed = discord.Embed(
        title="⚖️ Blâme infligé — Osiris",
        description=f"{joueur.mention} reçoit un blâme.",
        color=discord.Color.dark_gold()
    )
    embed.add_field(name="Motif", value=raison, inline=False)
    embed.add_field(name="Blâmes actifs", value=f"**{nb}** — un procès s'ouvre au-delà de {SEUIL_PROCES_BLAME}", inline=False)
    embed.add_field(name="MP au joueur", value="✅ Envoyé" if mp_envoye else "⚠️ Impossible (MP fermés ou joueur introuvable)", inline=False)
    embed.set_footer(text=f"Infligé par {interaction.user.display_name} • Expire dans 2 semaines")

    await interaction.followup.send(embed=embed, view=VueAvertirJoueur(joueur.id))
    await envoyer_log_proprietaire(bot_osiris, f"[{interaction.guild.name}] ⚖️ Blâme infligé à {joueur} par {interaction.user} : {raison} ({nb} actif(s)).")

    await traiter_seuils_blame(interaction.guild, joueur.id)

@bot_osiris.tree.command(name="blames", description="Affiche les blâmes actifs d'un joueur.")
@app_commands.describe(joueur="Le citoyen concerné")
async def blames(interaction: discord.Interaction, joueur: discord.Member):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    actifs = obtenir_blames_actifs(interaction.guild.id, joueur.id)
    if not actifs:
        await interaction.response.send_message(f"✅ {joueur.mention} n'a aucun blâme actif.", ephemeral=True)
        return
    embed = discord.Embed(title=f"⚖️ Blâmes actifs de {joueur.display_name}", color=discord.Color.dark_gold())
    for i, b in enumerate(actifs, start=1):
        embed.add_field(name=f"Blâme n°{i} — {b['date']}", value=b['raison'], inline=False)
    embed.set_footer(text=f"{len(actifs)} blâme(s) actif(s) • Un blâme expire 2 semaines après son ajout • Procès au-delà de {SEUIL_PROCES_BLAME}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot_osiris.tree.command(name="retirerblam", description="Retire un blâme précis d'un joueur (numéro visible via /blames).")
@app_commands.describe(joueur="Le citoyen concerné", numero="Le numéro du blâme à retirer (voir /blames)")
async def retirerblam(interaction: discord.Interaction, joueur: discord.Member, numero: int):
    if not verifier_permissions_staff(interaction.user):
        await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
        return
    retire = retirer_blame_par_index(interaction.guild.id, joueur.id, numero - 1)
    if retire:
        await interaction.response.send_message(f"🗑️ Blâme n°{numero} retiré à {joueur.mention}.", ephemeral=True)
        await envoyer_log_proprietaire(bot_osiris, f"[{interaction.guild.name}] 🗑️ Blâme retiré à {joueur} par {interaction.user} (raison retirée : {retire['raison']}).")
    else:
        await interaction.response.send_message("❌ Blâme introuvable pour ce joueur (vérifie le numéro avec /blames).", ephemeral=True)

# ================= COMMANDES DE SIRIUS (SYSTÈME DES RANGS) =================

async def _autocomplete_rangs(interaction: discord.Interaction, valeur_actuelle: str):
    if not interaction.guild:
        return []
    rangs = charger_rangs(interaction.guild.id)
    valeur_actuelle = (valeur_actuelle or "").lower()
    return [
        app_commands.Choice(name=f"{r['icone']} {r['nom']} ({r['groupe']})", value=r["id"])
        for r in sorted(rangs, key=lambda r: r["ordre"])
        if valeur_actuelle in r["nom"].lower()
    ][:25]

async def _autocomplete_demandes_en_attente(interaction: discord.Interaction, valeur_actuelle: str):
    if not interaction.guild:
        return []
    demandes = obtenir_demandes_rang(interaction.guild.id, statut="en_attente")
    valeur_actuelle = (valeur_actuelle or "").lower()
    choix = []
    for d in demandes:
        rang = obtenir_rang_par_id(interaction.guild.id, d["rang_id"])
        membre = interaction.guild.get_member(int(d["joueur_id"]))
        nom_joueur = membre.display_name if membre else d["joueur_id"]
        libelle = f"{nom_joueur} → {rang['nom'] if rang else d['rang_id']} ({d['date']})"
        if valeur_actuelle in libelle.lower():
            choix.append(app_commands.Choice(name=libelle[:100], value=d["id"]))
    return choix[:25]

@bot_rangs.tree.command(name="rangs", description="Affiche le catalogue complet des rangs du royaume.")
async def rangs_catalogue(interaction: discord.Interaction):
    rangs = sorted(charger_rangs(interaction.guild.id), key=lambda r: r["ordre"])
    embed = discord.Embed(title="🎖️ Catalogue des rangs — Sirius", color=discord.Color.gold())
    for groupe in GROUPES_RANGS:
        rangs_groupe = [r for r in rangs if r["groupe"] == groupe]
        if not rangs_groupe:
            continue
        lignes = []
        for r in rangs_groupe:
            lignes.append(f"{r['icone']} **{r['nom']}**" + (" 👑 *(unique)*" if r.get("unique") else ""))
        embed.add_field(name=f"— {groupe} —", value="\n".join(lignes), inline=False)
    embed.set_footer(text="Utilise /monrang pour voir ta progression, ou /demanderrang pour postuler.")
    await interaction.response.send_message(embed=embed)

@bot_rangs.tree.command(name="monrang", description="Affiche ton rang actuel et ta progression vers le suivant.")
async def monrang(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    rang_actuel = obtenir_rang_joueur(guild_id, interaction.user.id)
    rangs = sorted(charger_rangs(guild_id), key=lambda r: r["ordre"])
    embed = discord.Embed(title=f"🎖️ Progression de {interaction.user.display_name}", color=discord.Color.gold())
    if not rang_actuel:
        embed.description = "Aucun catalogue de rangs n'est configuré sur ce serveur."
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    embed.add_field(name="Rang actuel", value=f"{rang_actuel['icone']} **{rang_actuel['nom']}**", inline=False)

    rang_suivant = next((r for r in rangs if r["ordre"] > rang_actuel["ordre"]), None)
    if rang_suivant:
        rapport = verifier_conditions_rang(interaction.guild, interaction.user.id, rang_suivant)
        lignes = []
        for c in rapport["auto"]:
            lignes.append(f"{'✅' if c['ok'] else '❌'} {c['libelle']} *(actuel : {c['valeur_actuelle']})*")
        for c in rapport["manuel"]:
            lignes.append(f"🔎 {c['libelle']} *(vérifié manuellement par un instructeur)*")
        embed.add_field(
            name=f"Prochain rang : {rang_suivant['icone']} {rang_suivant['nom']}",
            value="\n".join(lignes) if lignes else "Aucune condition particulière.",
            inline=False
        )
        embed.set_footer(text="Utilise /demanderrang une fois prêt pour postuler.")
    else:
        embed.add_field(name="Prochain rang", value="Tu as atteint le sommet de la hiérarchie ! 👑", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot_rangs.tree.command(name="demanderrang", description="Soumets une demande de promotion vers un rang du catalogue.")
@app_commands.describe(rang="Le rang visé", motivation="Explique pourquoi tu mérites ce rang")
@app_commands.autocomplete(rang=_autocomplete_rangs)
async def demanderrang(interaction: discord.Interaction, rang: str, motivation: str):
    guild_id = interaction.guild.id
    cible = obtenir_rang_par_id(guild_id, rang)
    if not cible:
        return await interaction.response.send_message("❌ Rang inconnu (choisis-le dans la liste proposée).", ephemeral=True)

    demandes_en_cours = obtenir_demandes_rang(guild_id, statut="en_attente", joueur_id=interaction.user.id)
    if demandes_en_cours:
        return await interaction.response.send_message(
            "⚠️ Tu as déjà une demande de rang en attente de traitement. Merci de patienter.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    demande = creer_demande_rang(guild_id, interaction.user.id, rang, motivation)
    if not demande:
        return await interaction.followup.send("❌ Impossible de créer la demande (rang introuvable).", ephemeral=True)

    await interaction.followup.send(
        f"📥 Ta demande pour devenir **{cible['nom']}** a bien été transmise aux instructeurs !", ephemeral=True
    )
    await envoyer_log_proprietaire(
        bot_rangs, f"[{interaction.guild.name}] 📥 Nouvelle demande de rang : {interaction.user} → {cible['nom']}."
    )

@bot_rangs.tree.command(name="mesdemandes", description="Affiche l'historique de tes demandes de rang.")
async def mesdemandes(interaction: discord.Interaction):
    demandes = obtenir_demandes_rang(interaction.guild.id, joueur_id=interaction.user.id)
    if not demandes:
        return await interaction.response.send_message("Tu n'as encore soumis aucune demande de rang.", ephemeral=True)
    embed = discord.Embed(title="📋 Tes demandes de rang", color=discord.Color.gold())
    icones_statut = {"en_attente": "⏳", "accepte": "✅", "refuse": "❌"}
    for d in demandes[:10]:
        rang = obtenir_rang_par_id(interaction.guild.id, d["rang_id"])
        embed.add_field(
            name=f"{icones_statut.get(d['statut'], '•')} {rang['nom'] if rang else d['rang_id']} — {d['date']}",
            value=f"Statut : **{d['statut']}**" + (f"\n> {d['commentaire']}" if d.get("commentaire") else ""),
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot_rangs.tree.command(name="demandesrang", description="[Staff] Liste les demandes de rang en attente de traitement.")
async def demandesrang(interaction: discord.Interaction):
    if not verifier_permissions_staff(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    demandes = obtenir_demandes_rang(interaction.guild.id, statut="en_attente")
    if not demandes:
        return await interaction.response.send_message("✅ Aucune demande de rang en attente.", ephemeral=True)
    embed = discord.Embed(title="📥 Demandes de rang en attente", color=discord.Color.gold())
    for d in demandes[:20]:
        rang = obtenir_rang_par_id(interaction.guild.id, d["rang_id"])
        membre = interaction.guild.get_member(int(d["joueur_id"]))
        auto_ok = "✅" if d.get("rapport_auto", {}).get("toutes_auto_ok") else "⚠️"
        embed.add_field(
            name=f"{membre.display_name if membre else d['joueur_id']} → {rang['nom'] if rang else d['rang_id']} {auto_ok}",
            value=f"*{(d.get('motivation') or '—')[:200]}*\n📅 {d['date']}",
            inline=False
        )
    embed.set_footer(text="Utilise /validerrang ou /refuserrang pour traiter une demande.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot_rangs.tree.command(name="validerrang", description="[Staff] Accepte une demande de rang en attente.")
@app_commands.describe(demande="La demande à valider", commentaire="Commentaire optionnel",
                        forcer="Promouvoir quand même si les conditions automatiques ne sont pas remplies (défaut: non)")
@app_commands.autocomplete(demande=_autocomplete_demandes_en_attente)
async def validerrang(interaction: discord.Interaction, demande: str, commentaire: str = None, forcer: bool = False):
    if not verifier_permissions_staff(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    resultat = traiter_demande_rang(interaction.guild.id, demande, "accepte", interaction.user.id, commentaire, forcer=forcer)
    if resultat == "conditions_non_remplies":
        demande_obj = obtenir_demande_rang_par_id(interaction.guild.id, demande)
        rapport = demande_obj.get("rapport_auto", {}) if demande_obj else {}
        lignes = [f"{'✅' if c.get('ok') else '❌'} {c['libelle']} (constaté : {c.get('valeur_actuelle')})"
                  for c in rapport.get("auto", [])]
        detail = "\n".join(lignes) if lignes else "—"
        return await interaction.followup.send(
            "⚠️ Ce joueur ne remplit pas encore toutes les conditions automatiques de ce rang, "
            "la promotion a été **bloquée** :\n" + detail +
            "\n\nSi tu veux quand même le promouvoir, relance la commande avec `forcer: Vrai`.",
            ephemeral=True
        )
    if not resultat:
        return await interaction.followup.send("❌ Demande introuvable ou déjà traitée.", ephemeral=True)
    rang = obtenir_rang_par_id(interaction.guild.id, resultat["rang_id"])
    await interaction.followup.send(f"🎖️ Demande acceptée : <@{resultat['joueur_id']}> devient **{rang['nom']}** !", ephemeral=True)
    suffixe_force = " (⚠️ forcée malgré des conditions automatiques non remplies)" if forcer else ""
    await envoyer_log_proprietaire(
        bot_rangs, f"[{interaction.guild.name}] 🎖️ Demande de rang acceptée pour <@{resultat['joueur_id']}> ({rang['nom']}) par {interaction.user}."
        + suffixe_force
    )

@bot_rangs.tree.command(name="refuserrang", description="[Staff] Refuse une demande de rang en attente.")
@app_commands.describe(demande="La demande à refuser", commentaire="Motif du refus (optionnel)")
@app_commands.autocomplete(demande=_autocomplete_demandes_en_attente)
async def refuserrang(interaction: discord.Interaction, demande: str, commentaire: str = None):
    if not verifier_permissions_staff(interaction.user):
        return await interaction.response.send_message("❌ Permission refusée.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    resultat = traiter_demande_rang(interaction.guild.id, demande, "refuse", interaction.user.id, commentaire)
    if not resultat:
        return await interaction.followup.send("❌ Demande introuvable ou déjà traitée.", ephemeral=True)
    rang = obtenir_rang_par_id(interaction.guild.id, resultat["rang_id"])
    await interaction.followup.send(f"📋 Demande refusée : <@{resultat['joueur_id']}> pour {rang['nom']}.", ephemeral=True)
    await envoyer_log_proprietaire(
        bot_rangs, f"[{interaction.guild.name}] 📋 Demande de rang refusée pour <@{resultat['joueur_id']}> ({rang['nom']}) par {interaction.user}."
    )

# ================= FIN COMMANDES DE SIRIUS =================

# ================= SITE WEB D'ADMINISTRATION =================
# Même app Flask que keep_alive() : un seul process, un seul serveur Render.
site_web.configurer_site(app, bot, {
    "charger_missions_fichier": charger_missions_fichier,
    "sauvegarder_mission_fichier": sauvegarder_mission_fichier,
    "reecrire_toutes_missions": reecrire_toutes_missions,
    "vider_toutes_missions": vider_toutes_missions,
    "charger_profils": charger_profils,
    "sauvegarder_profils": sauvegarder_profils,
    "initialiser_profil": initialiser_profil,
    "ajouter_historique": ajouter_historique,
    "charger_points_categories": charger_points_categories,
    "sauvegarder_points_categories": sauvegarder_points_categories,
    "points_pour_categorie": points_pour_categorie,
    "definir_points_mission": definir_points_mission,
    "missions_actives": missions_actives,
    "verrou_missions": verrou_missions,
    "generer_backup_complet": generer_backup_complet,
    "restaurer_donnees_backup": restaurer_donnees_backup,
    "sauvegarder_totale_maintenance": sauvegarder_totale_maintenance,
    "restaurer_apres_maintenance": restaurer_apres_maintenance,
    "charger_logs_recents": charger_logs_recents,
    "sauvegarder_log_disque": sauvegarder_log_disque,
    "bot_start_time": BOT_START_TIME,
    "charger_code_verrou": charger_code_verrou,
    "sauvegarder_code_verrou": sauvegarder_code_verrou,
    "guildes_deverrouillees": guildes_deverrouillees,
    "charger_maintenance": charger_maintenance,
    "definir_maintenance": definir_maintenance,
    "salon_annonce_maintenance_id": SALON_ANNONCE_MAINTENANCE_ID,
    "extraire_duree": extraire_duree,
    "action_accepter_mission": action_accepter_mission,
    "action_refuser_mission": action_refuser_mission,
    "attribuer_mission_precise_site": attribuer_mission_precise_site,
    "envoyer_double_notification": envoyer_double_notification,
    "formater_duree": formater_duree,
    "obtenir_blames_actifs": obtenir_blames_actifs,
    "ajouter_blame": ajouter_blame,
    "retirer_blame_par_index": retirer_blame_par_index,
    "traiter_seuils_blame": traiter_seuils_blame,
    "envoyer_notification_blame": envoyer_notification_blame,
    "seuil_proces_blame": SEUIL_PROCES_BLAME,
    "interroger_ia": interroger_ia,
    "reinitialiser_historique_ia_cle": reinitialiser_historique_ia_cle,
    "ajouter_notification": ajouter_notification,
    "obtenir_notifications": obtenir_notifications,
    "compter_notifications_non_lues": compter_notifications_non_lues,
    "marquer_notifications_lues": marquer_notifications_lues,
    "obtenir_notifications_multi": obtenir_notifications_multi,
    "compter_notifications_non_lues_multi": compter_notifications_non_lues_multi,
    "marquer_notifications_lues_multi": marquer_notifications_lues_multi,
    "charger_rangs": charger_rangs,
    "sauvegarder_rangs": sauvegarder_rangs,
    "obtenir_rang_par_id": obtenir_rang_par_id,
    "obtenir_rang_joueur": obtenir_rang_joueur,
    "verifier_conditions_rang": verifier_conditions_rang,
    "creer_demande_rang": creer_demande_rang,
    "obtenir_demandes_rang": obtenir_demandes_rang,
    "obtenir_demande_rang_par_id": obtenir_demande_rang_par_id,
    "traiter_demande_rang": traiter_demande_rang,
    "groupes_rangs": GROUPES_RANGS,
    "charger_boutique": charger_boutique,
    "obtenir_produit_boutique": obtenir_produit_boutique,
    "ajouter_produit_boutique": ajouter_produit_boutique,
    "modifier_produit_boutique": modifier_produit_boutique,
    "supprimer_produit_boutique": supprimer_produit_boutique,
    "acheter_produit_boutique": acheter_produit_boutique,
    "charger_roue": charger_roue,
    "obtenir_part_roue": obtenir_part_roue,
    "ajouter_part_roue": ajouter_part_roue,
    "modifier_part_roue": modifier_part_roue,
    "basculer_actif_part_roue": basculer_actif_part_roue,
    "supprimer_part_roue": supprimer_part_roue,
    "tourner_roue": tourner_roue,
    "jouer_roue": jouer_roue,
    "types_roue": TYPES_ROUE,
    "noms_types_roue": NOMS_TYPES_ROUE,
    "obtenir_tickets_roue": obtenir_tickets_roue,
    "ajouter_tickets_roue": ajouter_tickets_roue,
    "retirer_ticket_roue": retirer_ticket_roue,
})

print("[Démarrage] Vérification de la synchronisation Google Drive...")
restaurer_tout_depuis_drive()

keep_alive()

async def main():
    token_valerius = os.environ.get("DIS_TOKEN") or os.environ.get("DISCORD_TOKEN")
    # Variables Render existantes côté utilisateur : "osiris_id" et "sirius_id"
    # (fallback sur "ascalon_id" pour ne pas casser le déploiement tant que
    # la variable Render n'a pas été renommée après le passage Ascalon → Sirius).
    token_osiris = os.environ.get("osiris_id") or os.environ.get("OSIRIS_TOKEN") or os.environ.get("OSIRIS_ID")
    token_rangs = (
        os.environ.get("sirius_id") or os.environ.get("SIRIUS_TOKEN") or os.environ.get("SIRIUS_ID")
        or os.environ.get("ascalon_id") or os.environ.get("ASCALON_TOKEN") or os.environ.get("ASCALON_ID")
    )

    if not token_valerius:
        print("Erreur : Aucun token Discord trouvé pour Valerius (DIS_TOKEN / DISCORD_TOKEN).")
        return

    bots_a_lancer = [("Valerius", bot, token_valerius)]
    if token_osiris:
        bots_a_lancer.append(("Osiris", bot_osiris, token_osiris))
    else:
        print("⚠️ Aucun token trouvé pour Osiris (variable 'osiris_id') — il ne démarrera pas.")
    if token_rangs:
        bots_a_lancer.append(("Sirius", bot_rangs, token_rangs))
    else:
        print("⚠️ Aucun token trouvé pour Sirius (variable 'sirius_id') — il ne démarrera pas.")

    if len(bots_a_lancer) == 1:
        async with bot:
            await bot.start(token_valerius)
        return

    async with contextlib.AsyncExitStack() as pile:
        for _, b, _ in bots_a_lancer:
            await pile.enter_async_context(b)
        await asyncio.gather(*(b.start(t) for _, b, t in bots_a_lancer))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
