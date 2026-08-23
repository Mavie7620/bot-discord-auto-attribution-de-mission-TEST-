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
Six rôles, du plus faible au plus fort :
  recrue < membre < instructeur < admin < super_admin < proprietaire

- N'importe qui avec le lien peut créer un compte via /inscription.
  Le compte créé est toujours "recrue" au départ, et l'inscrit doit
  choisir le serveur Discord auquel il appartient. Ce choix est
  DÉFINITIF de son côté : lui seul ne peut plus le changer ensuite.
- "instructeur" et plus : accède à /admin/serveurs, mais seulement
  au(x) serveur(s) qui lui sont assignés (sauf proprietaire : tous).
- "admin" et plus : peut en plus gérer le catalogue de missions et
  les comptes du site (créer/modifier/supprimer), mais seulement
  pour son propre serveur, et seulement des comptes d'un rôle
  strictement inférieur au sien (impossible de créer/modifier un
  compte super_admin ou proprietaire si on n'est pas soi-même
  proprietaire). C'est un admin (ou plus) qui peut changer le
  serveur assigné à un compte.
- "super_admin" (Super Modo) : mêmes droits qu'un admin, mais
  reste cantonné à SON SEUL serveur assigné — il n'a aucune vue
  ni aucun accès sur les autres serveurs.
- "proprietaire" (Propriétaire) : seul rang avec un accès total et
  global, sur tous les serveurs, y compris les sauvegardes
  complètes (/admin/backup). C'est aussi le seul rang habilité à
  attribuer le rôle "proprietaire" ou "super_admin" à un autre
  compte, ou à changer le serveur assigné à n'importe quel compte.
  Le compte historique MAVIE7620 est toujours proprietaire.
"""
import os
import json
import secrets
import functools
from datetime import datetime

from flask import request, redirect, url_for, session, send_file, abort, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash

COMPTES_FILE = "valerius_comptes.json"
SECRET_KEY_FILE = "valerius_secret.key"
COMPTE_PROPRIETAIRE_LOGIN = "MAVIE7620"

ROLES_ORDRE = ["recrue", "membre", "instructeur", "admin", "super_admin", "proprietaire"]
ROLE_LABELS = {
    "recrue": "Recrue",
    "membre": "Membre",
    "instructeur": "Instructeur",
    "admin": "Admin",
    "super_admin": "Super Modo",
    "proprietaire": "Propriétaire",
}


def niveau_role(role):
    try:
        return ROLES_ORDRE.index(role)
    except (ValueError, TypeError):
        return 0


def guild_autorise(compte, guild_id):
    """True si ce compte a le droit de voir/gérer les données de ce serveur.
    Seul le rang Propriétaire a un accès global à tous les serveurs — le
    Super Modo (super_admin), lui, reste limité à son unique serveur assigné."""
    if not compte:
        return False
    if compte.get("role") == "proprietaire":
        return True
    return str(compte.get("guild_id")) == str(guild_id)


# ================= GESTION DES COMPTES =================

def _migrer_comptes(comptes):
    """Migration douce de l'ancien système (rôles 'admin'/'user') vers
    la nouvelle hiérarchie à 5 rôles, sans casser les comptes existants."""
    modifie = False
    for login, c in comptes.items():
        if c.get("role") == "user":
            c["role"] = "membre"
            modifie = True
        if login == COMPTE_PROPRIETAIRE_LOGIN and c.get("role") != "proprietaire":
            c["role"] = "proprietaire"
            modifie = True
        if c.get("role") not in ROLES_ORDRE:
            c["role"] = "recrue"
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
    --bg: #0f1015;
    --bg-soft: #14151c;
    --panel: #1b1c25;
    --panel-2: #21222c;
    --border: #2c2d3a;
    --text: #edeef5;
    --muted: #9294ab;
    --gold: #e8bd55;
    --gold-2: #f4d485;
    --red: #ef5a56;
    --green: #3fd68c;
    --shadow: 0 10px 30px -12px rgba(0,0,0,0.55);
  }
  * { box-sizing: border-box; }
  body {
    margin:0; font-family:"Segoe UI",-apple-system,Roboto,Inter,sans-serif;
    background:
      radial-gradient(1200px 600px at 15% -10%, rgba(232,189,85,0.08), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(79,140,214,0.06), transparent 55%),
      var(--bg);
    color:var(--text); min-height:100vh; line-height:1.5;
  }
  nav {
    display:flex; align-items:center; gap:22px; padding:16px 28px;
    background:rgba(20,21,28,0.85); backdrop-filter: blur(10px);
    border-bottom:1px solid var(--border); position:sticky; top:0; z-index:10;
  }
  nav a {
    color:var(--muted); text-decoration:none; font-size:14px; font-weight:500;
    padding:7px 12px; border-radius:8px; transition:all .15s ease;
  }
  nav a:hover { color:#fff; background:var(--panel-2); }
  nav .brand {
    font-weight:800; font-size:16px; letter-spacing:.6px; margin-right:auto;
    background:linear-gradient(135deg, var(--gold-2), var(--gold));
    -webkit-background-clip:text; background-clip:text; color:transparent;
  }
  main { max-width:1000px; margin:36px auto; padding:0 22px 70px; }
  h1 { font-size:26px; margin:0 0 6px; font-weight:800; letter-spacing:-.3px; }
  h2 { font-size:17px; color:#d7d8e6; margin-top:34px; margin-bottom:10px; font-weight:700; }
  .card {
    background:linear-gradient(180deg, var(--panel), var(--panel) 60%, var(--panel-2));
    border:1px solid var(--border); border-radius:16px; padding:22px 24px; margin:16px 0;
    box-shadow: var(--shadow); transition: border-color .2s ease, transform .15s ease;
  }
  .card:hover { border-color:#3a3c4c; }
  table { width:100%; border-collapse:collapse; margin-top:10px; }
  th, td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--border); font-size:14px; vertical-align:middle; }
  th { color:var(--muted); font-weight:700; font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  tr:hover td { background:rgba(255,255,255,0.02); }
  input, select, button {
    font-family:inherit; font-size:14px; padding:11px 15px; border-radius:10px;
    border:1px solid var(--border); background:var(--bg-soft); color:var(--text);
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  input:focus, select:focus {
    outline:none; border-color: var(--gold); box-shadow:0 0 0 3px rgba(232,189,85,0.15);
  }
  button {
    background:linear-gradient(135deg, var(--gold-2), var(--gold));
    color:#1b1406; border:none; font-weight:700; cursor:pointer;
    padding:12px 20px; letter-spacing:.2px;
    box-shadow: 0 6px 16px -6px rgba(232,189,85,0.5);
    transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
  }
  button:hover { transform:translateY(-1px); filter:brightness(1.05); box-shadow:0 10px 20px -6px rgba(232,189,85,0.6); }
  button:active { transform:translateY(0); }
  button.danger {
    background:linear-gradient(135deg, #ef6b67, var(--red)); color:#fff;
    box-shadow: 0 6px 16px -6px rgba(239,90,86,0.5);
  }
  button.danger:hover { box-shadow:0 10px 20px -6px rgba(239,90,86,0.6); }
  button.secondary {
    background:var(--panel-2); color:var(--text); border:1px solid var(--border);
    box-shadow:none;
  }
  button.secondary:hover { background:#2a2c38; box-shadow:none; }
  .flash { padding:13px 16px; border-radius:12px; margin-bottom:16px; font-size:14px; font-weight:500; border:1px solid transparent; }
  .flash.erreur { background:rgba(239,90,86,0.12); color:#ff9d9d; border-color:rgba(239,90,86,0.35); }
  .flash.ok { background:rgba(63,214,140,0.1); color:#8bf0c0; border-color:rgba(63,214,140,0.3); }
  .badge {
    display:inline-flex; align-items:center; padding:4px 12px; border-radius:20px;
    font-size:12px; font-weight:700; white-space:nowrap; letter-spacing:.2px;
  }
  .badge.recrue { background:#2a2b38; color:var(--muted); }
  .badge.membre { background:rgba(126,200,227,0.14); color:#7ec8e3; }
  .badge.instructeur { background:rgba(95,208,176,0.14); color:#5fd0b0; }
  .badge.admin { background:rgba(232,189,85,0.14); color:var(--gold-2); }
  .badge.super_admin { background:rgba(224,126,200,0.14); color:#e07ec8; }
  .badge.proprietaire {
    background:linear-gradient(135deg, rgba(255,209,102,0.2), rgba(255,209,102,0.08));
    color:#ffd166; border:1px solid rgba(255,209,102,0.5);
  }
  form.inline { display:inline; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .muted { color:var(--muted); font-size:13px; }
  a.btnlink {
    display:inline-flex; align-items:center; gap:6px; padding:11px 18px; border-radius:10px;
    background:var(--panel-2); border:1px solid var(--border); color:var(--text);
    text-decoration:none; font-size:14px; font-weight:600;
    transition: all .15s ease;
  }
  a.btnlink:hover { background:#2a2c38; border-color:#3a3c4c; transform:translateY(-1px); }

  .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:14px; margin:16px 0; }
  .stat-card {
    background:linear-gradient(180deg, var(--panel), var(--panel-2));
    border:1px solid var(--border); border-radius:14px; padding:18px 20px; box-shadow:var(--shadow);
  }
  .stat-card .valeur { font-size:28px; font-weight:800; color:var(--gold-2); line-height:1.2; }
  .stat-card .label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; margin-top:4px; }

  .log-entry {
    display:flex; gap:14px; padding:12px 16px; border-bottom:1px solid var(--border);
    font-size:13.5px; align-items:flex-start;
  }
  .log-entry:last-child { border-bottom:none; }
  .log-entry:hover { background:rgba(255,255,255,0.02); }
  .log-entry .date { color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; min-width:150px; }
  .log-entry .texte { color:#dcdde8; word-break:break-word; }

  .pill { display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:20px; font-size:12px; font-weight:700; }
  .pill.on { background:rgba(63,214,140,0.14); color:#8bf0c0; }
  .pill.off { background:rgba(239,90,86,0.14); color:#ff9d9d; }
</style>
"""


def page_html(titre, corps, connecte=None, role=None):
    nav_liens = ""
    if connecte:
        niveau = niveau_role(role)
        liens = []
        if niveau >= niveau_role("instructeur"):
            liens.append('<a href="/admin/serveurs">Serveurs</a>')
        if niveau >= niveau_role("admin"):
            liens.append('<a href="/admin/comptes">Comptes</a>')
        if niveau >= niveau_role("proprietaire"):
            liens.append('<a href="/admin/dashboard">Tableau de bord</a>')
            liens.append('<a href="/admin/logs">Logs</a>')
            liens.append('<a href="/admin/securite">Sécurité</a>')
            liens.append('<a href="/admin/backup">Sauvegardes</a>')
        if niveau < niveau_role("instructeur"):
            liens.append('<a href="/mon-profil">Mon profil</a>')
        badge_icone = "👑 " if role == "proprietaire" else ""
        badge = f'<span class="badge {role}">{badge_icone}{ROLE_LABELS.get(role, role)}</span>' if role else ""
        nav_liens = "".join(liens) + f'<span class="muted">{connecte}</span>{badge}<a href="/deconnexion">Déconnexion</a>'
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titre} — Valerius</title>
{STYLE}
</head>
<body>
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
        hiérarchie recrue < membre < instructeur < admin < super_admin < proprietaire."""
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
                    "role": "recrue",
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
          <p class="muted">Ton compte sera créé avec le rôle <strong>Recrue</strong>. Un administrateur pourra ensuite te faire progresser.</p>
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
             peut_editer_catalogue=(niveau_role(compte.get("role")) >= niveau_role("admin")))
        return page_html("Serveurs", corps, connecte(), compte.get("role"))

    # ---------- Catalogue de missions (admin et plus, scope serveur) ----------

    @app.route("/admin/missions/<int:guild_id>", methods=["GET", "POST"])
    @role_required("admin")
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
        if request.method == "POST":
            joueur_id = int(request.form.get("joueur_id"))
            statut = request.form.get("statut")
            if guild_id in missions_actives and joueur_id in missions_actives[guild_id]:
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
                <input type="hidden" name="statut" value="succes">
                <button type="submit">✅ Marquer réussie</button>
              </form>
              <form method="post" class="inline">
                <input type="hidden" name="joueur_id" value="{{ joueur_id }}">
                <input type="hidden" name="statut" value="echec">
                <button class="danger" type="submit">❌ Marquer échouée</button>
              </form>
            </div>
          </div>
        </div>
        {% endfor %}
        """, actives=actives, message=message, guild_id=guild_id)
        return page_html("Missions en cours", corps, connecte(), compte.get("role"))

    # ---------- Profils (instructeur et plus, scope serveur) ----------

    @app.route("/admin/profils/<int:guild_id>")
    @role_required("instructeur")
    def admin_profils(guild_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        profils = deps["charger_profils"](guild_id)
        corps = render_template_string("""
        <h1>Profils des joueurs</h1>
        <p class="muted">Serveur {{ guild_id }} — {{ profils|length }} profil(s)</p>
        {% if not profils %}<div class="card">Aucun profil enregistré sur ce serveur.</div>{% endif %}
        {% if profils %}
        <table>
          <tr><th>Joueur (ID)</th><th>Réussies</th><th>Échouées</th><th></th></tr>
          {% for jid, p in profils.items() %}
          <tr>
            <td>{{ jid }}</td>
            <td>{{ p.total_reussies }}</td>
            <td>{{ p.total_echouees }}</td>
            <td><a class="btnlink" href="/admin/profils/{{ guild_id }}/{{ jid }}">Historique</a></td>
          </tr>
          {% endfor %}
        </table>
        {% endif %}
        """, profils=profils, guild_id=guild_id)
        return page_html("Profils", corps, connecte(), compte.get("role"))

    @app.route("/admin/profils/<int:guild_id>/<joueur_id>")
    @role_required("instructeur")
    def admin_profil_detail(guild_id, joueur_id):
        compte = compte_connecte()
        if not guild_autorise(compte, guild_id):
            abort(403)
        profils = deps["charger_profils"](guild_id)
        profil = profils.get(str(joueur_id))
        if not profil:
            abort(404)
        corps = render_template_string("""
        <h1>Historique — Joueur {{ joueur_id }}</h1>
        <p class="muted">Serveur {{ guild_id }} — {{ profil.total_reussies }} réussies / {{ profil.total_echouees }} échouées</p>
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
        """, profil=profil, joueur_id=joueur_id, guild_id=guild_id)
        return page_html("Historique", corps, connecte(), compte.get("role"))

    # ---------- Admin : comptes du site (admin et plus) ----------

    @app.route("/admin/comptes", methods=["GET", "POST"])
    @role_required("admin")
    def admin_comptes():
        message = None
        erreur = None
        mot_de_passe_genere = None
        acteur = compte_connecte()
        acteur_role = acteur.get("role")
        # Seul un Propriétaire a le pouvoir total : attribuer n'importe quel
        # rôle (y compris Propriétaire/Super Modo) et choisir n'importe quel
        # serveur. Un Super Modo, lui, reste cantonné à son propre serveur,
        # exactement comme un admin.
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
                role_demande = request.form.get("role", "recrue")
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
                else:
                    if not acteur_super:
                        nouveau_guild = acteur.get("guild_id")
                    cible["role"] = nouveau_role
                    cible["guild_id"] = nouveau_guild or None
                    cible["discord_id"] = nouveau_discord_id or None
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
                    message = "Code d'activation mis à jour avec succès."
            elif action == "verrouiller":
                guild_id = int(request.form.get("guild_id"))
                deps["guildes_deverrouillees"].discard(guild_id)
                message = "Serveur reverrouillé."
            elif action == "deverrouiller":
                guild_id = int(request.form.get("guild_id"))
                deps["guildes_deverrouillees"].add(guild_id)
                message = "Serveur déverrouillé."

        code_actuel = deps["charger_code_verrou"]()
        guilds = list(bot.guilds)
        deverrouillees = deps["guildes_deverrouillees"]
        lignes_serveurs = [(g, g.id in deverrouillees) for g in guilds]

        corps = render_template_string("""
        <h1>Sécurité</h1>
        {% if message %}<div class="flash ok">{{ message }}</div>{% endif %}
        {% if erreur %}<div class="flash erreur">{{ erreur }}</div>{% endif %}

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
        """, message=message, erreur=erreur, code_actuel=code_actuel, lignes_serveurs=lignes_serveurs)
        return page_html("Sécurité", corps, connecte(), "proprietaire")

    # ---------- Utilisateur (recrue/membre) : son profil uniquement ----------

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
