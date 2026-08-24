# -*- coding: utf-8 -*-
"""
Site web d'administration de Valerius.
Tourne dans le MÊME processus Flask que le "keep_alive" du bot (bot.py) :
même serveur Render, un seul déploiement.

Ce module ne fait AUCUNE hypothèse sur les données du bot : toutes les
fonctions dont il a besoin (lecture/écriture des missions, des profils,
des backups...) lui sont injectées via configurer_site(app, bot, deps)
pour éviter tout import circulaire avec bot.py.

================= SYSTEME DE RÔLES =================
Trois rôles, du plus faible au plus fort :
  malgache < instructeur < proprietaire

- N'importe qui avec le lien peut créer un compte via /inscription.
  Le compte créé est toujours "malgache" au départ, et l'inscrit doit
  choisir le serveur Discord auquel il appartient. Ce choix est
  DÉFINITIF de son côté : lui seul ne peut plus le changer ensuite.
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
from datetime import datetime

import discord
from flask import request, redirect, url_for, session, send_file, abort, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash

COMPTES_FILE = "valerius_comptes.json"
SECRET_KEY_FILE = "valerius_secret.key"
COMPTE_PROPRIETAIRE_LOGIN = "MAVIE7620"

ROLES_ORDRE = ["malgache", "instructeur", "proprietaire"]
ROLE_LABELS = {
    "malgache": "Malgache",
    "instructeur": "Instructeur",
    "proprietaire": "Propriétaire",
}


def niveau_role(role):
    try:
        return ROLES_ORDRE.index(role)
    except (ValueError, TypeError):
        return 0


def guild_autorise(compte, guild_id):
    """True si ce compte a le droit de voir/gérer les données de ce serveur.
    Seul le rang Propriétaire a un accès global à tous les serveurs — un
    Instructeur, lui, reste limité à son unique serveur assigné."""
    if not compte:
        return False
    if compte.get("role") == "proprietaire":
        return True
    return str(compte.get("guild_id")) == str(guild_id)


# ================= GESTION DES COMPTES =================

ANCIENS_ROLES_VERS_NOUVEAUX = {
    "user": "malgache",
    "recrue": "malgache",
    "membre": "malgache",
    "admin": "instructeur",
    "super_admin": "instructeur",
}


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


def _generer_mot_de_passe():
    return secrets.token_urlsafe(9)


async def initialiser_compte_proprietaire(envoyer_log_proprietaire, bot):
    """À appeler une fois au démarrage (dans on_ready) : crée le compte
    propriétaire MAVIE7620 s'il n'existe pas encore, avec un mot de passe
    aléatoire envoyé en MP — jamais écrit en clair dans le code source."""
    comptes = charger_comptes()
    if COMPTE_PROPRIETAIRE_LOGIN in comptes:
        return

    mot_de_passe = _generer_mot_de_passe()
    comptes[COMPTE_PROPRIETAIRE_LOGIN] = {
        "password_hash": generate_password_hash(mot_de_passe),
        "role": "proprietaire",
        "discord_id": None,
        "guild_id": None,
        "must_change_password": True
    }
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
    --bg: #060608;
    --bg-soft: #0d0d11;
    --panel: #161619;
    --panel-2: #1e1e22;
    --border: #2b2b30;
    --text: #f2f2f4;
    --muted: #94949c;
    --gold: #e50914;
    --gold-2: #ff2d3e;
    --red: #e50914;
    --green: #3fd68c;
    --shadow: 0 10px 30px -12px rgba(0,0,0,0.7);
    --font-title: "Poppins", "Segoe UI", sans-serif;
    --font-body: "Inter", "Segoe UI", -apple-system, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin:0; font-family:var(--font-body);
    background:
      radial-gradient(1100px 550px at 15% -10%, rgba(229,9,20,0.16), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(229,9,20,0.08), transparent 55%),
      var(--bg);
    background-attachment: fixed, fixed, fixed;
    color:var(--text); min-height:100vh; line-height:1.5;
    animation: fade-in .35s ease;
  }
  @keyframes fade-in { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }
  nav {
    display:flex; align-items:center; gap:22px; padding:16px 28px;
    background:rgba(6,6,8,0.82); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    border-bottom:1px solid var(--border); position:sticky; top:0; z-index:10;
    box-shadow: 0 4px 24px -10px rgba(0,0,0,0.75);
  }
  nav a {
    color:var(--muted); text-decoration:none; font-size:14px; font-weight:600;
    padding:7px 12px; border-radius:8px; transition:all .15s ease;
  }
  nav a:hover { color:#fff; background:var(--panel-2); }
  nav .brand {
    font-family:var(--font-title); font-weight:800; font-size:19px; letter-spacing:.5px; margin-right:auto;
    color:var(--gold); text-transform:uppercase;
    text-shadow: 0 0 24px rgba(229,9,20,0.45);
  }
  main { max-width:1000px; margin:36px auto; padding:0 22px 70px; }
  h1 { font-family:var(--font-title); font-size:28px; margin:0 0 6px; font-weight:800; letter-spacing:.2px; }
  h2 { font-family:var(--font-title); font-size:18px; color:#e4e4e8; margin-top:34px; margin-bottom:10px; font-weight:700; letter-spacing:.2px; }
  .card {
    position:relative; overflow:hidden;
    background:linear-gradient(180deg, var(--panel), var(--panel) 60%, var(--panel-2));
    border:1px solid var(--border); border-radius:14px; padding:22px 24px; margin:16px 0;
    box-shadow: var(--shadow); transition: border-color .2s ease, transform .15s ease, box-shadow .2s ease;
  }
  .card::before {
    content:""; position:absolute; inset:0 0 auto 0; height:2px;
    background:linear-gradient(90deg, transparent, rgba(229,9,20,0.6), transparent);
  }
  .card:hover { border-color:#3a3a40; transform:translateY(-2px); box-shadow: 0 16px 36px -12px rgba(0,0,0,0.8); }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--border); font-size:14px; vertical-align:middle; }
  th { color:var(--muted); font-weight:700; font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:rgba(255,255,255,0.025); }
  input, select, button {
    font-family:inherit; font-size:14px; padding:11px 15px; border-radius:8px;
    border:1px solid var(--border); background:var(--bg-soft); color:var(--text);
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  input:focus, select:focus {
    outline:none; border-color: var(--gold); box-shadow:0 0 0 3px rgba(229,9,20,0.18);
  }
  button {
    background:var(--gold);
    color:#fff; border:none; font-weight:700; cursor:pointer;
    padding:12px 20px; letter-spacing:.2px; border-radius:8px;
    box-shadow: 0 6px 16px -6px rgba(229,9,20,0.55);
    transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
  }
  button:hover { transform:translateY(-1px); filter:brightness(1.12); box-shadow:0 10px 22px -6px rgba(229,9,20,0.65); }
  button:active { transform:translateY(0); }
  button.danger {
    background:#7a0d12; color:#fff;
    box-shadow: 0 6px 16px -6px rgba(122,13,18,0.6);
  }
  button.danger:hover { filter:brightness(1.25); box-shadow:0 10px 20px -6px rgba(122,13,18,0.7); }
  button.secondary {
    background:var(--panel-2); color:var(--text); border:1px solid var(--border);
    box-shadow:none;
  }
  button.secondary:hover { background:#2a2a30; box-shadow:none; }
  .flash { padding:13px 16px; border-radius:10px; margin-bottom:16px; font-size:14px; font-weight:500; border:1px solid transparent; }
  .flash.erreur { background:rgba(229,9,20,0.14); color:#ff9da1; border-color:rgba(229,9,20,0.4); }
  .flash.ok { background:rgba(63,214,140,0.1); color:#8bf0c0; border-color:rgba(63,214,140,0.3); }
  .badge {
    display:inline-flex; align-items:center; padding:4px 12px; border-radius:20px;
    font-size:12px; font-weight:700; white-space:nowrap; letter-spacing:.2px;
  }
  .badge.malgache { background:rgba(126,200,227,0.14); color:#7ec8e3; }
  .badge.instructeur { background:rgba(95,208,176,0.14); color:#5fd0b0; }
  .badge.proprietaire {
    background:linear-gradient(135deg, rgba(229,9,20,0.25), rgba(229,9,20,0.1));
    color:#ff525f; border:1px solid rgba(229,9,20,0.5);
  }
  form.inline { display:inline; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .muted { color:var(--muted); font-size:13px; }
  a.btnlink {
    display:inline-flex; align-items:center; gap:6px; padding:11px 18px; border-radius:8px;
    background:var(--panel-2); border:1px solid var(--border); color:var(--text);
    text-decoration:none; font-size:14px; font-weight:600;
    transition: all .15s ease;
  }
  a.btnlink:hover { background:#2a2a30; border-color:#3a3a40; transform:translateY(-1px); }

  .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:14px; margin:16px 0; }
  .stat-card {
    background:linear-gradient(180deg, var(--panel), var(--panel-2));
    border:1px solid var(--border); border-radius:12px; padding:18px 20px; box-shadow:var(--shadow);
    transition: transform .15s ease, border-color .15s ease;
  }
  .stat-card:hover { transform:translateY(-2px); border-color:rgba(229,9,20,0.45); }
  .stat-card .valeur { font-family:var(--font-title); font-size:28px; font-weight:800; color:var(--gold-2); line-height:1.2; text-shadow:0 0 18px rgba(229,9,20,0.3); }
  .stat-card .label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-top:4px; }

  .log-entry {
    display:flex; gap:14px; padding:12px 16px; border-bottom:1px solid var(--border);
    font-size:13.5px; align-items:flex-start;
  }
  .log-entry:last-child { border-bottom:none; }
  .log-entry:hover { background:rgba(255,255,255,0.025); }
  .log-entry .date { color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; min-width:150px; }
  .log-entry .texte { color:#dcdde8; word-break:break-word; }

  .pill { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:20px; font-size:12px; font-weight:700; }
  .pill.on { background:rgba(63,214,140,0.14); color:#8bf0c0; }
  .pill.off { background:rgba(229,9,20,0.16); color:#ff9da1; }

  #grille-curseur {
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    display: grid;
  }
  .grille-case {
    border: 1px solid rgba(229,9,20,0.05);
    background: transparent;
    transition: background .5s ease, border-color .5s ease, box-shadow .5s ease;
  }
  .grille-case.actif {
    background: rgba(229,9,20,0.4);
    border-color: rgba(229,9,20,0.75);
    box-shadow: 0 0 14px rgba(229,9,20,0.5) inset;
    transition: background .06s ease, border-color .06s ease, box-shadow .06s ease;
  }
  nav, main { position: relative; z-index: 1; }
</style>
<script>
(function () {
  var TAILLE_CASE = 46;
  var conteneur, cases = [], colonnes = 0, lignes = 0, actives = [];

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
    actives = [];
  }

  function allumer(index) {
    if (index >= 0 && index < cases.length) {
      cases[index].classList.add("actif");
      actives.push(index);
    }
  }

  function surMouvement(e) {
    if (!colonnes || !lignes) return;
    var col = Math.floor(e.clientX / (window.innerWidth / colonnes));
    var lig = Math.floor(e.clientY / (window.innerHeight / lignes));
    col = Math.max(0, Math.min(colonnes - 1, col));
    lig = Math.max(0, Math.min(lignes - 1, lig));

    actives.forEach(function (i) { if (cases[i]) cases[i].classList.remove("actif"); });
    actives = [];

    var centre = lig * colonnes + col;
    allumer(centre);
    if (col > 0) allumer(centre - 1);
    if (col < colonnes - 1) allumer(centre + 1);
    if (lig > 0) allumer(centre - colonnes);
    if (lig < lignes - 1) allumer(centre + colonnes);
  }

  function init() {
    conteneur = document.getElementById("grille-curseur");
    if (!conteneur) return;
    construireGrille();
    document.addEventListener("mousemove", surMouvement, { passive: true });
    var redim;
    window.addEventListener("resize", function () {
      clearTimeout(redim);
      redim = setTimeout(construireGrille, 200);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
</script>
"""


def page_html(titre, corps, connecte=None, role=None):
    nav_liens = ""
    if connecte:
        niveau = niveau_role(role)
        liens = []
        if niveau >= niveau_role("instructeur"):
            liens.append('<a href="/admin/serveurs">Serveurs</a>')
            liens.append('<a href="/admin/comptes">Comptes</a>')
        if niveau >= niveau_role("proprietaire"):
            liens.append('<a href="/admin/dashboard">Tableau de bord</a>')
            liens.append('<a href="/admin/logs">Logs</a>')
            liens.append('<a href="/admin/securite">Sécurité</a>')
            liens.append('<a href="/admin/message">Message</a>')
            liens.append('<a href="/admin/recherche-joueur">Rechercher un joueur</a>')
            liens.append('<a href="/admin/backup">Sauvegardes</a>')
        if niveau < niveau_role("instructeur"):
            liens.append('<a href="/mon-profil">Mon profil</a>')
            liens.append('<a href="/mon-catalogue">Catalogue</a>')
        badge_icone = "👑 " if role == "proprietaire" else ""
        badge = f'<span class="badge {role}">{badge_icone}{ROLE_LABELS.get(role, role)}</span>' if role else ""
        nav_liens = "".join(liens) + f'<span class="muted">{connecte}</span>{badge}<a href="/deconnexion">Déconnexion</a>'
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titre} — Valerius</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@600;700;800&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E⚖️%3C/text%3E%3C/svg%3E">
{STYLE}
</head>
<body>
<div id="grille-curseur"></div>
<nav><span class="brand">⚖️ VALERIUS</span>{nav_liens}</nav>
<main>
{corps}
</main>
</body>
</html>"""


# ================= ROUTES =================

def configurer_site(app, bot, deps):
    """Enregistre toutes les routes du site sur l'app Flask déjà utilisée
    par le keep_alive du bot. `deps` est un dict de fonctions/objets du
    bot dont le site a besoin (voir bot.py pour la liste exacte)."""
    app.secret_key = _obtenir_secret_key()

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
            if compte.get("must_change_password") and request.endpoint != "changer_mot_de_passe":
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
                if compte.get("must_change_password") and request.endpoint != "changer_mot_de_passe":
                    return redirect(url_for("changer_mot_de_passe"))
                if niveau_role(compte.get("role")) < niveau_role(min_role):
                    abort(403)
                return f(*a, **kw)
            return wrapper
        return decorateur

    # ---------- Authentification ----------

    @app.route("/")
    def racine():
        if connecte():
            compte = compte_connecte()
            if compte:
                if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
                    return redirect(url_for("admin_serveurs"))
                return redirect(url_for("mon_profil"))
        return redirect(url_for("connexion"))

    @app.route("/inscription", methods=["GET", "POST"])
    def inscription():
        erreur = None
        if request.method == "POST":
            login = request.form.get("login", "").strip()
            mdp = request.form.get("mot_de_passe", "")
            confirmation = request.form.get("confirmation", "")
            discord_id = request.form.get("discord_id", "").strip() or None
            guild_id = request.form.get("guild_id", "").strip()
            comptes = charger_comptes()
            guilds_valides = {str(g.id) for g in bot.guilds}
            if not login:
                erreur = "Identifiant requis."
            elif login in comptes:
                erreur = "Cet identifiant est déjà pris."
            elif len(mdp) < 6:
                erreur = "Le mot de passe doit faire au moins 6 caractères."
            elif mdp != confirmation:
                erreur = "La confirmation ne correspond pas."
            elif guild_id not in guilds_valides:
                erreur = "Merci de sélectionner un serveur Discord valide."
            else:
                comptes[login] = {
                    "password_hash": generate_password_hash(mdp),
                    "role": "malgache",
                    "discord_id": discord_id,
                    "guild_id": guild_id,
                    "must_change_password": False
                }
                sauvegarder_comptes(comptes)
                session.clear()
                session["login"] = login
                session.permanent = True
                return redirect(url_for("mon_profil"))
        guilds = list(bot.guilds)
        corps = render_template_string("""
        <div class="card" style="max-width:440px;margin:60px auto;">
          <h1>Créer un compte</h1>
          <p class="muted">Ton compte sera créé avec le rôle <strong>Malgache</strong>. Un instructeur pourra ensuite te faire progresser.</p>
          {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
          {% if not guilds %}
          <div class="flash erreur">Le bot n'est connecté à aucun serveur pour l'instant. Réessaie plus tard.</div>
          {% else %}
          <form method="post">
            <p><input name="login" placeholder="Identifiant" required style="width:100%"></p>
            <p><input name="mot_de_passe" type="password" placeholder="Mot de passe (6 caractères min.)" required style="width:100%"></p>
            <p><input name="confirmation" type="password" placeholder="Confirmer le mot de passe" required style="width:100%"></p>
            <p><input name="discord_id" placeholder="Ton ID Discord (optionnel, recommandé)" style="width:100%"></p>
            <p>
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
        """, erreur=erreur, guilds=guilds)
        return page_html("Créer un compte", corps)

    @app.route("/connexion", methods=["GET", "POST"])
    def connexion():
        erreur = None
        if request.method == "POST":
            login = request.form.get("login", "").strip()
            mdp = request.form.get("mot_de_passe", "")
            compte = charger_comptes().get(login)
            if compte and check_password_hash(compte["password_hash"], mdp):
                session.clear()
                session["login"] = login
                session.permanent = True
                if compte.get("must_change_password"):
                    return redirect(url_for("changer_mot_de_passe"))
                if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
                    return redirect(url_for("admin_serveurs"))
                return redirect(url_for("mon_profil"))
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
        </div>
        """, erreur=erreur)
        return page_html("Connexion", corps)

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
            elif len(nouveau) < 6:
                erreur = "Le nouveau mot de passe doit faire au moins 6 caractères."
            elif nouveau != confirmation:
                erreur = "La confirmation ne correspond pas."
            else:
                compte["password_hash"] = generate_password_hash(nouveau)
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
        <h1>Serveurs</h1>
        <p class="muted">Sélectionne un serveur pour gérer ses missions, ses profils ou ses missions en cours.</p>
        {% if not guilds %}<div class="card">Aucun serveur accessible pour ton compte. {% if not super_admin %}Ton compte n'est assigné à aucun serveur, ou celui-ci n'est plus accessible au bot.{% endif %}</div>{% endif %}
        {% for g in guilds %}
        <div class="card row" style="justify-content:space-between;">
          <div><strong>{{ g.name }}</strong><div class="muted">ID : {{ g.id }} — {{ g.member_count }} membres</div></div>
          <div class="row">
            {% if peut_editer_catalogue %}
            <a class="btnlink" href="/admin/missions/{{ g.id }}">Catalogue</a>
            {% endif %}
            <a class="btnlink" href="/admin/missions-actives/{{ g.id }}">Missions en cours</a>
            <a class="btnlink" href="/admin/profils/{{ g.id }}">Profils</a>
          </div>
        </div>
        {% endfor %}
        """, guilds=guilds, super_admin=(compte.get("role") == "proprietaire"),
             peut_editer_catalogue=(niveau_role(compte.get("role")) >= niveau_role("instructeur")))
        return page_html("Serveurs", corps, connecte(), compte.get("role"))

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
                if cat in ("commune", "moyenne", "difficile", "royal") and texte:
                    deps["sauvegarder_mission_fichier"](guild_id, cat, texte, delai)
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

        structure = deps["charger_missions_fichier"](guild_id)
        corps = render_template_string("""
        <h1>Catalogue de missions</h1>
        <p class="muted">Serveur {{ guild_id }}</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}

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
            <button type="submit">Ajouter</button>
          </form>
        </div>

        {% for cat, missions in structure.items() %}
        <h2>{{ cat|capitalize }} ({{ missions|length }})</h2>
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
        """, structure=structure, message=message, guild_id=guild_id)
        return page_html("Catalogue de missions", corps, connecte(), compte.get("role"))

    # ---------- Missions en cours (instructeur et plus, scope serveur) ----------

    @app.route("/admin/missions-actives/<int:guild_id>", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_missions_actives(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        missions_actives = deps["missions_actives"]
        message = None
        erreur = None
        if request.method == "POST":
            joueur_id = int(request.form.get("joueur_id"))
            action = request.form.get("action", "statut")
            if guild_id in missions_actives and joueur_id in missions_actives[guild_id]:
                if action == "temps":
                    duree_texte = request.form.get("duree", "").strip()
                    retirer = request.form.get("sens") == "retirer"
                    if not duree_texte:
                        erreur = "Indique une durée (ex : 2h, 1 jour)."
                    else:
                        delta = deps["extraire_duree"](duree_texte)
                        m_info = missions_actives[guild_id][joueur_id]
                        if retirer:
                            m_info["date_fin"] -= delta
                            m_info["duree_totale"] -= delta
                        else:
                            m_info["date_fin"] += delta
                            m_info["duree_totale"] += delta
                        deps["sauvegarder_log_disque"](f"⏱️ Temps {'retiré' if retirer else 'ajouté'} ({duree_texte}) sur la mission du joueur {joueur_id} depuis le site par {connecte()}.")
                        message = f"{'Retiré' if retirer else 'Ajouté'} {duree_texte} avec succès."
                else:
                    statut = request.form.get("statut")
                    m_info = missions_actives[guild_id][joueur_id]
                    profils = deps["charger_profils"](guild_id)
                    deps["initialiser_profil"](joueur_id, profils)
                    if statut == "succes":
                        profils[str(joueur_id)]["total_reussies"] += 1
                        deps["ajouter_historique"](joueur_id, profils, m_info["texte"], "Succès", m_info["cat"])
                        message = "Mission marquée comme réussie."
                    else:
                        profils[str(joueur_id)]["total_echouees"] += 1
                        deps["ajouter_historique"](joueur_id, profils, m_info["texte"], "Échec", m_info["cat"])
                        message = "Mission marquée comme échouée."
                    deps["sauvegarder_profils"](guild_id, profils)
                    del missions_actives[guild_id][joueur_id]

        actives = list(missions_actives.get(guild_id, {}).items())
        corps = render_template_string("""
        <h1>Missions en cours</h1>
        <p class="muted">Serveur {{ guild_id }} — {{ actives|length }} mission(s) active(s)</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}
        <p class="muted">⚠️ Ces actions mettent à jour les fichiers du bot mais n'envoient PAS de message dans Discord. Pour un suivi avec notifications, utilise les boutons du ticket ou les commandes slash.</p>
        {% if not actives %}<div class="card">Aucune mission en cours sur ce serveur.</div>{% endif %}
        {% for joueur_id, m in actives %}
        <div class="card">
          <div class="row" style="justify-content:space-between;">
            <div>
              <strong>Joueur {{ joueur_id }}</strong> — <span class="muted">{{ m.cat }}</span>
              <div>{{ m.texte }}</div>
              <div class="muted">Fin prévue : {{ m.date_fin.strftime('%d/%m/%Y %H:%M') }}{% if m.en_attente %} · en attente de validation{% endif %}</div>
            </div>
            <div class="row">
              <form method="post" class="inline">
                <input type="hidden" name="joueur_id" value="{{ joueur_id }}">
                <input type="hidden" name="action" value="statut">
                <input type="hidden" name="statut" value="succes">
                <button type="submit">✅ Marquer réussie</button>
              </form>
              <form method="post" class="inline">
                <input type="hidden" name="joueur_id" value="{{ joueur_id }}">
                <input type="hidden" name="action" value="statut">
                <input type="hidden" name="statut" value="echec">
                <button class="danger" type="submit">❌ Marquer échouée</button>
              </form>
            </div>
          </div>
          <form method="post" class="row" style="margin-top:14px;border-top:1px solid var(--border);padding-top:14px;">
            <input type="hidden" name="joueur_id" value="{{ joueur_id }}">
            <input type="hidden" name="action" value="temps">
            <input name="duree" placeholder="ex : 2h, 1 jour, 30min" style="width:160px" required>
            <select name="sens">
              <option value="ajouter">➕ Ajouter</option>
              <option value="retirer">➖ Retirer</option>
            </select>
            <button class="secondary" type="submit">Appliquer le temps</button>
          </form>
        </div>
        {% endfor %}
        """, actives=actives, message=message, erreur=erreur, guild_id=guild_id)
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
          <tr><th>Joueur</th><th>Réussies</th><th>Échouées</th><th></th></tr>
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
                    if statut == "Succès":
                        profil_courant["total_reussies"] += 1
                    else:
                        profil_courant["total_echouees"] += 1
                    deps["ajouter_historique"](int(joueur_id), profils, texte, statut, categorie)
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
                profil_courant["historique"] = []
                deps["sauvegarder_profils"](guild_id, profils)
                deps["sauvegarder_log_disque"](f"♻️ Profil du joueur {joueur_id} réinitialisé depuis le site par {connecte()}.")
                message = "Profil réinitialisé."

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
        <p class="muted">Serveur {{ guild_id }} — {{ profil.total_reussies }} réussies / {{ profil.total_echouees }} échouées</p>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

        {% if peut_modifier %}
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
          <tr><th>Date</th><th>Catégorie</th><th>Mission</th><th>Statut</th>{% if peut_modifier %}<th></th>{% endif %}</tr>
          {% for h in profil.historique %}
          <tr>
            <td>{{ h.date }}</td>
            <td>{{ h.categorie }}</td>
            <td>{{ h.texte }}</td>
            <td>{{ h.statut }}</td>
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

    # ---------- Admin : comptes du site (instructeur et plus) ----------

    @app.route("/admin/comptes", methods=["GET", "POST"])
    @role_required("instructeur")
    def admin_comptes():
        message = None
        erreur = None
        mot_de_passe_genere = None
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
                        "password_hash": generate_password_hash(mot_de_passe_genere),
                        "role": role_demande,
                        "discord_id": discord_id,
                        "guild_id": guild_id,
                        "must_change_password": True
                    }
                    sauvegarder_comptes(comptes)
                    message = f"Compte « {login} » créé."

            elif action == "supprimer":
                login = request.form.get("login")
                cible = comptes.get(login)
                if login == COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = "Impossible de supprimer le compte propriétaire."
                elif not cible:
                    erreur = "Compte introuvable."
                elif not peut_gerer(cible.get("role")):
                    erreur = "Tu n'as pas l'autorité pour supprimer ce compte."
                else:
                    del comptes[login]
                    sauvegarder_comptes(comptes)
                    message = "Compte supprimé."

            elif action == "reinitialiser":
                login = request.form.get("login")
                cible = comptes.get(login)
                if not cible:
                    erreur = "Compte introuvable."
                elif not peut_gerer(cible.get("role")):
                    erreur = "Tu n'as pas l'autorité pour réinitialiser ce compte."
                else:
                    mot_de_passe_genere = _generer_mot_de_passe()
                    comptes[login]["password_hash"] = generate_password_hash(mot_de_passe_genere)
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
                elif login == connecte():
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
                elif acteur_super and nouveau_mdp and len(nouveau_mdp) < 6:
                    erreur = "Le nouveau mot de passe doit faire au moins 6 caractères."
                else:
                    if not acteur_super:
                        nouveau_guild = acteur.get("guild_id")
                    cible["role"] = nouveau_role
                    cible["guild_id"] = nouveau_guild or None
                    cible["discord_id"] = nouveau_discord_id or None
                    if acteur_super and nouveau_mdp:
                        cible["password_hash"] = generate_password_hash(nouveau_mdp)
                        cible["must_change_password"] = False
                    if acteur_super and nouveau_login and nouveau_login != login:
                        del comptes[login]
                        comptes[nouveau_login] = cible
                        login = nouveau_login
                    sauvegarder_comptes(comptes)
                    message = f"Compte « {login} » mis à jour."

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
              {% if login != connecte_login and peut_gerer(c.role) %}
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
              {% if peut_gerer(c.role) %}
              <form method="post" class="inline">
                <input type="hidden" name="action" value="reinitialiser">
                <input type="hidden" name="login" value="{{ login }}">
                <button class="secondary" type="submit">Réinit. mdp</button>
              </form>
              {% if login != proprietaire %}
              <form method="post" class="inline" onsubmit="return confirm('Supprimer ce compte ?')">
                <input type="hidden" name="action" value="supprimer">
                <input type="hidden" name="login" value="{{ login }}">
                <button class="danger" type="submit">Suppr.</button>
              </form>
              {% endif %}
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </table>
        """, lignes_comptes=lignes_comptes, message=message, erreur=erreur, mot_de_passe=mot_de_passe_genere,
             proprietaire=COMPTE_PROPRIETAIRE_LOGIN, guilds=guilds, roles_attribuables=roles_attribuables,
             role_labels=ROLE_LABELS, acteur_super=acteur_super, acteur_guild=acteur.get("guild_id"),
             nom_serveur_acteur=noms_guildes.get(str(acteur.get("guild_id"))), connecte_login=connecte(),
             peut_gerer=peut_gerer)
        return page_html("Comptes", corps, connecte(), acteur_role)

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
          <p class="muted">Une sauvegarde automatique est aussi envoyée toutes les 2 heures dans le salon Discord dédié (missions, profils, comptes du site et code d'activation inclus).</p>
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
                message = "Mode maintenance activé : le bot ne répond plus qu'au Propriétaire."
            elif action == "maintenance_off":
                deps["definir_maintenance"](False)
                deps["sauvegarder_log_disque"](f"✅ Mode maintenance DÉSACTIVÉ depuis le site par {connecte()}.")
                annoncer_maintenance(False)
                message = "Mode maintenance désactivé : le bot répond de nouveau normalement."

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
            <p class="muted">Bloque toutes les commandes sur tous les serveurs (sauf pour le Propriétaire), utile pour une mise à jour en cours.</p>
          </div>
          <div class="row">
            <span class="pill {{ 'off' if maintenance_active else 'on' }}">{{ 'Maintenance active' if maintenance_active else 'Bot en service' }}</span>
            <form method="post" class="inline">
              {% if maintenance_active %}
              <input type="hidden" name="action" value="maintenance_off">
              <button type="submit">Désactiver la maintenance</button>
              {% else %}
              <input type="hidden" name="action" value="maintenance_on">
              <button class="danger" type="submit" onclick="return confirm('Activer la maintenance va rendre le bot muet sur tous les serveurs. Continuer ?')">Activer la maintenance</button>
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
    @login_required
    def mon_profil():
        compte = compte_connecte()
        if niveau_role(compte.get("role")) >= niveau_role("instructeur"):
            return redirect(url_for("admin_serveurs"))
        discord_id = compte.get("discord_id")
        guild_id = compte.get("guild_id")
        profil = None
        if discord_id and guild_id:
            profils = deps["charger_profils"](int(guild_id))
            profil = profils.get(str(discord_id))
        corps = render_template_string("""
        <h1>Mon profil</h1>
        {% if not profil %}
          <div class="card">Aucun historique trouvé pour l'instant. Demande à un administrateur de vérifier que ton compte est bien relié à ton identifiant Discord et à ton serveur.</div>
        {% else %}
          <div class="card">
            <strong>{{ profil.total_reussies }}</strong> mission(s) réussie(s) —
            <strong>{{ profil.total_echouees }}</strong> mission(s) échouée(s)
          </div>
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
        """, profil=profil)
        return page_html("Mon profil", corps, connecte(), compte.get("role"))

    @app.route("/mon-catalogue")
    @login_required
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
