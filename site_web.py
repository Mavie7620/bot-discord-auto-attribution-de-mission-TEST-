# -*- coding: utf-8 -*-
"""
Site web d'administration de Valerius.
Tourne dans le MÊME processus Flask que le "keep_alive" du bot (bot.py) :
même serveur Render, un seul déploiement.

Ce module ne fait AUCUNE hypothèse sur les données du bot : toutes les
fonctions dont il a besoin (lecture/écriture des missions, des profils,
des backups...) lui sont injectées via configurer_site(app, bot, deps)
pour éviter tout import circulaire avec bot.py.
"""
import os
import json
import secrets
import functools

from flask import request, redirect, url_for, session, send_file, abort, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash

COMPTES_FILE = "valerius_comptes.json"
SECRET_KEY_FILE = "valerius_secret.key"
COMPTE_PROPRIETAIRE_LOGIN = "MAVIE7620"


# ================= GESTION DES COMPTES =================

def charger_comptes():
    if not os.path.exists(COMPTES_FILE):
        return {}
    try:
        with open(COMPTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sauvegarder_comptes(comptes):
    with open(COMPTES_FILE, "w", encoding="utf-8") as f:
        json.dump(comptes, f, indent=4, ensure_ascii=False)


def _generer_mot_de_passe():
    return secrets.token_urlsafe(9)


async def initialiser_compte_proprietaire(envoyer_log_proprietaire, bot):
    """À appeler une fois au démarrage (dans on_ready) : crée le compte
    propriétaire MAVIE7620 s'il n'existe pas encore, avec un mot de passe
    aléatoire envoyé en MP — jamais écrit en clair dans le code source,
    contrairement à l'ancien code d'activation."""
    comptes = charger_comptes()
    if COMPTE_PROPRIETAIRE_LOGIN in comptes:
        return

    mot_de_passe = _generer_mot_de_passe()
    comptes[COMPTE_PROPRIETAIRE_LOGIN] = {
        "password_hash": generate_password_hash(mot_de_passe),
        "role": "admin",
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
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,sans-serif; background:#14151a; color:#e7e7ea; }
  nav { display:flex; align-items:center; gap:20px; padding:14px 24px; background:#1b1c22; border-bottom:1px solid #2a2b33; }
  nav a { color:#cfcfe0; text-decoration:none; font-size:14px; }
  nav a:hover { color:#fff; }
  nav .brand { font-weight:700; color:#e0b64d; margin-right:auto; letter-spacing:.5px; }
  main { max-width:920px; margin:32px auto; padding:0 20px 60px; }
  h1 { font-size:22px; margin-bottom:4px; }
  h2 { font-size:17px; color:#cfcfe0; margin-top:32px; }
  .card { background:#1b1c22; border:1px solid #2a2b33; border-radius:10px; padding:18px 20px; margin:14px 0; }
  table { width:100%; border-collapse:collapse; margin-top:8px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #2a2b33; font-size:14px; }
  th { color:#9a9ab0; font-weight:600; }
  input, select, button { font-family:inherit; font-size:14px; padding:9px 12px; border-radius:7px; border:1px solid #33343e; background:#101116; color:#eee; }
  button { background:#e0b64d; color:#1b1c22; border:none; font-weight:600; cursor:pointer; }
  button:hover { filter:brightness(1.08); }
  button.danger { background:#d9534f; color:#fff; }
  button.secondary { background:#2a2b33; color:#eee; }
  .flash { padding:10px 14px; border-radius:8px; margin-bottom:14px; font-size:14px; }
  .flash.erreur { background:#3a1f22; color:#ff9d9d; border:1px solid #5c2b2f; }
  .flash.ok { background:#1f3a25; color:#9dff9d; border:1px solid #2b5c32; }
  .badge { display:inline-block; padding:2px 8px; border-radius:20px; font-size:12px; }
  .badge.admin { background:#3a2f1f; color:#e0b64d; }
  .badge.user { background:#22303a; color:#7ec8e3; }
  form.inline { display:inline; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .muted { color:#8f8fa3; font-size:13px; }
  a.btnlink { display:inline-block; padding:9px 14px; border-radius:7px; background:#2a2b33; color:#eee; text-decoration:none; font-size:14px; }
</style>
"""


def page_html(titre, corps, connecte=None, role=None):
    nav_liens = ""
    if connecte:
        if role == "admin":
            nav_liens = (
                '<a href="/admin/serveurs">Serveurs</a>'
                '<a href="/admin/comptes">Comptes</a>'
                '<a href="/admin/backup">Sauvegardes</a>'
            )
        else:
            nav_liens = '<a href="/mon-profil">Mon profil</a>'
        nav_liens += f'<span class="muted">{connecte}</span><a href="/deconnexion">Déconnexion</a>'
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

    def admin_required(f):
        @functools.wraps(f)
        def wrapper(*a, **kw):
            if not connecte():
                return redirect(url_for("connexion"))
            compte = compte_connecte()
            if not compte:
                session.clear()
                return redirect(url_for("connexion"))
            if compte.get("must_change_password"):
                return redirect(url_for("changer_mot_de_passe"))
            if compte.get("role") != "admin":
                abort(403)
            return f(*a, **kw)
        return wrapper

    # ---------- Authentification ----------

    @app.route("/")
    def racine():
        if connecte():
            compte = compte_connecte()
            if compte and compte.get("role") == "admin":
                return redirect(url_for("admin_serveurs"))
            return redirect(url_for("mon_profil"))
        return redirect(url_for("connexion"))

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
                if compte.get("role") == "admin":
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

    # ---------- Admin : serveurs ----------

    @app.route("/admin/serveurs")
    @admin_required
    def admin_serveurs():
        guilds = list(bot.guilds)
        corps = render_template_string("""
        <h1>Serveurs</h1>
        <p class="muted">Sélectionne un serveur pour gérer ses missions, ses profils ou ses missions en cours.</p>
        {% if not guilds %}<div class="card">Le bot n'est connecté à aucun serveur pour l'instant.</div>{% endif %}
        {% for g in guilds %}
        <div class="card row" style="justify-content:space-between;">
          <div><strong>{{ g.name }}</strong><div class="muted">ID : {{ g.id }} — {{ g.member_count }} membres</div></div>
          <div class="row">
            <a class="btnlink" href="/admin/missions/{{ g.id }}">Catalogue</a>
            <a class="btnlink" href="/admin/missions-actives/{{ g.id }}">Missions en cours</a>
            <a class="btnlink" href="/admin/profils/{{ g.id }}">Profils</a>
          </div>
        </div>
        {% endfor %}
        """, guilds=guilds)
        return page_html("Serveurs", corps, connecte(), "admin")

    # ---------- Admin : catalogue de missions ----------

    @app.route("/admin/missions/<int:guild_id>", methods=["GET", "POST"])
    @admin_required
    def admin_missions(guild_id):
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
        return page_html("Catalogue de missions", corps, connecte(), "admin")

    # ---------- Admin : missions en cours ----------

    @app.route("/admin/missions-actives/<int:guild_id>", methods=["GET", "POST"])
    @admin_required
    def admin_missions_actives(guild_id):
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
        return page_html("Missions en cours", corps, connecte(), "admin")

    # ---------- Admin : profils ----------

    @app.route("/admin/profils/<int:guild_id>")
    @admin_required
    def admin_profils(guild_id):
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
        return page_html("Profils", corps, connecte(), "admin")

    @app.route("/admin/profils/<int:guild_id>/<joueur_id>")
    @admin_required
    def admin_profil_detail(guild_id, joueur_id):
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
        return page_html("Historique", corps, connecte(), "admin")

    # ---------- Admin : comptes du site ----------

    @app.route("/admin/comptes", methods=["GET", "POST"])
    @admin_required
    def admin_comptes():
        message = None
        erreur = None
        mot_de_passe_genere = None
        if request.method == "POST":
            action = request.form.get("action")
            comptes = charger_comptes()
            if action == "creer":
                login = request.form.get("login", "").strip()
                role = request.form.get("role", "user")
                discord_id = request.form.get("discord_id", "").strip() or None
                guild_id = request.form.get("guild_id", "").strip() or None
                if not login:
                    erreur = "Identifiant requis."
                elif login in comptes:
                    erreur = "Cet identifiant existe déjà."
                else:
                    mot_de_passe_genere = _generer_mot_de_passe()
                    comptes[login] = {
                        "password_hash": generate_password_hash(mot_de_passe_genere),
                        "role": role if role in ("admin", "user") else "user",
                        "discord_id": discord_id,
                        "guild_id": guild_id,
                        "must_change_password": True
                    }
                    sauvegarder_comptes(comptes)
                    message = f"Compte « {login} » créé."
            elif action == "supprimer":
                login = request.form.get("login")
                if login == COMPTE_PROPRIETAIRE_LOGIN:
                    erreur = "Impossible de supprimer le compte propriétaire."
                elif login in comptes:
                    del comptes[login]
                    sauvegarder_comptes(comptes)
                    message = "Compte supprimé."
            elif action == "reinitialiser":
                login = request.form.get("login")
                if login in comptes:
                    mot_de_passe_genere = _generer_mot_de_passe()
                    comptes[login]["password_hash"] = generate_password_hash(mot_de_passe_genere)
                    comptes[login]["must_change_password"] = True
                    sauvegarder_comptes(comptes)
                    message = f"Mot de passe de « {login} » réinitialisé."

        comptes = charger_comptes()
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
              <option value="user">Utilisateur (voit son profil)</option>
              <option value="admin">Admin</option>
            </select>
            <input name="discord_id" placeholder="ID Discord (pour un compte user)">
            <input name="guild_id" placeholder="ID du serveur (pour un compte user)">
            <button type="submit">Créer</button>
          </form>
          <p class="muted">Le mot de passe temporaire s'affiche une seule fois après la création — il devra être changé à la première connexion.</p>
        </div>

        <table>
          <tr><th>Identifiant</th><th>Rôle</th><th>Discord ID</th><th>Serveur</th><th></th></tr>
          {% for login, c in comptes.items() %}
          <tr>
            <td>{{ login }}</td>
            <td><span class="badge {{ c.role }}">{{ c.role }}</span></td>
            <td>{{ c.discord_id or '—' }}</td>
            <td>{{ c.guild_id or '—' }}</td>
            <td class="row">
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
            </td>
          </tr>
          {% endfor %}
        </table>
        """, comptes=comptes, message=message, erreur=erreur, mot_de_passe=mot_de_passe_genere, proprietaire=COMPTE_PROPRIETAIRE_LOGIN)
        return page_html("Comptes", corps, connecte(), "admin")

    # ---------- Admin : sauvegardes ----------

    @app.route("/admin/backup", methods=["GET", "POST"])
    @admin_required
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
        return page_html("Sauvegardes", corps, connecte(), "admin")

    @app.route("/admin/backup/telecharger")
    @admin_required
    def admin_backup_telecharger():
        buffer, nom_fichier, _taille = deps["generer_backup_complet"]()
        return send_file(buffer, as_attachment=True, download_name=nom_fichier, mimetype="application/json")

    # ---------- Utilisateur non-admin : son profil uniquement ----------

    @app.route("/mon-profil")
    @login_required
    def mon_profil():
        compte = compte_connecte()
        if compte.get("role") == "admin":
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
