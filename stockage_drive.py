# -*- coding: utf-8 -*-
"""
================= STOCKAGE EXTERNE — GOOGLE DRIVE (compte personnel, OAuth) =================
Ce module synchronise les fichiers de données du bot (JSON, images de la
boutique...) avec un dossier Google Drive, pour qu'ils survivent aux
redémarrages du service (le disque de Render, en plan gratuit, est effacé
à chaque redéploiement/redémarrage — voir les commentaires autour de
SALON_BACKUP_AUTO_ID dans bot.py).

Le dossier Drive sert de dossier CONSULTABLE par un humain, en plus d'être
la source de restauration au démarrage :
- Au démarrage, AVANT de se connecter à Discord, le bot restaure tous les
  fichiers connus depuis ce dossier Drive (voir restaurer_tout_depuis_drive
  dans bot.py).
- Une tâche périodique pousse ensuite une copie à jour de chaque fichier
  vers Drive, à intervalles réguliers (voir synchronisation_drive dans
  bot.py).

Ceci reste un COMPLÉMENT au système de backup Discord existant (qui
continue de fonctionner normalement, toutes les 2h, vers un salon) :
Drive donne un dossier consultable à l'humain et une copie individuelle
fichier par fichier, Discord reste une sauvegarde de secours (au format
archive unique) au cas où Drive serait mal configuré ou temporairement
indisponible.

================= POURQUOI OAUTH ET PAS UN COMPTE DE SERVICE ? =================
Un compte de service Google n'a AUCUN quota de stockage propre : il ne peut
PAS créer de fichiers dans le Drive personnel d'un compte Gmail classique,
même si ce dossier lui est partagé en "Éditeur" (Google renvoie l'erreur
"Service Accounts do not have storage quota"). Les comptes de service ne
peuvent écrire que dans un "Drive Partagé" ("Shared Drive"), une
fonctionnalité réservée aux comptes Google Workspace payants.

Ce module utilise donc l'authentification OAuth "utilisateur" : le bot
s'authentifie comme TON PROPRE compte Google (celui qui possède le dossier
Drive), via un "refresh token" obtenu UNE SEULE FOIS en local. Les fichiers
créés appartiennent alors à ton quota de stockage personnel (gratuit sur un
compte Gmail classique), donc ça fonctionne sans rien payer.

================= CONFIGURATION (à faire une seule fois) =================
1. Aller sur https://console.cloud.google.com, créer un projet (ou en
   réutiliser un), puis activer l'API "Google Drive API"
   (menu "API et services" > "Bibliothèque").
2. "API et services" > "Écran de consentement OAuth" : choisis le type
   "Externe", renseigne juste un nom d'appli + un e-mail de contact, et
   ajoute TON propre compte Google dans la liste des "Utilisateurs test".
   Le statut "Test" suffit largement pour un usage personnel — pas besoin
   de faire valider l'appli par Google.
3. "API et services" > "Identifiants" > "Créer des identifiants" >
   "ID client OAuth" > type d'application "Application de bureau"
   ("Desktop app"). Note le "Client ID" et le "Client Secret" affichés
   (ou télécharge le JSON, ils sont dedans).
4. EN LOCAL, sur ton ordinateur (jamais sur Render), installe les paquets
   nécessaires puis lance le script obtenir_refresh_token.py fourni à
   côté de ce fichier :
       pip install google-auth-oauthlib google-api-python-client google-auth
       python obtenir_refresh_token.py
   Ça ouvre ton navigateur : connecte-toi avec TON compte Google (celui
   qui possède le dossier Drive) et autorise l'accès. Le script affiche
   ensuite les 3 valeurs à copier (Client ID, Client Secret, Refresh
   Token).
5. Ouvre Google Drive avec ton compte, crée un dossier (ex: "Backups
   Valerius"), récupère son ID dans l'URL après "/folders/"
   (https://drive.google.com/drive/folders/CET_ID_LA) — copie bien
   uniquement l'ID, rien après (pas de "?usp=sharing").
6. Sur Render (ton service > Environment), définis 4 variables :
   - GOOGLE_OAUTH_CLIENT_ID
   - GOOGLE_OAUTH_CLIENT_SECRET
   - GOOGLE_OAUTH_REFRESH_TOKEN   (celui affiché à l'étape 4)
   - GOOGLE_DRIVE_DOSSIER_ID      (l'ID récupéré à l'étape 5)
7. Ajoute à requirements.txt (si pas déjà présent) :
     google-api-python-client
     google-auth

Sans ces 4 variables (ou sans ces paquets installés), ce module se
désactive proprement (drive_disponible() renvoie False) : le bot continue
de fonctionner normalement, juste sans synchronisation Drive — le backup
Discord existant suffit alors à lui seul.
"""
import os
import io

_SERVICE = None
_TENTATIVE_INIT_FAITE = False


def _construire_service():
    """Construit (une seule fois, puis met en cache) le client Google
    Drive à partir des identifiants OAuth (compte personnel). Renvoie None
    proprement si la configuration est absente/invalide ou si les
    paquets Google ne sont pas installés — ne lève jamais d'exception."""
    global _SERVICE, _TENTATIVE_INIT_FAITE
    if _SERVICE is not None:
        return _SERVICE
    if _TENTATIVE_INIT_FAITE:
        return None
    _TENTATIVE_INIT_FAITE = True

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,  # pas d'access token initial : il sera obtenu via le refresh_token au premier appel
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
        print("[Drive] Client Google Drive (OAuth, compte personnel) initialisé avec succès.")
        return _SERVICE
    except ModuleNotFoundError:
        print("[Drive] Paquets manquants (google-api-python-client / google-auth) — synchronisation Drive désactivée.")
        return None
    except Exception as e:
        print(f"[Drive] Impossible d'initialiser le client Google Drive : {e}")
        return None


def _dossier_id():
    return os.environ.get("GOOGLE_DRIVE_DOSSIER_ID")


def drive_disponible():
    """True si la synchronisation Drive est configurée et fonctionnelle
    (variables d'environnement présentes ET client initialisable)."""
    return bool(_dossier_id()) and _construire_service() is not None


def _trouver_fichier(nom_fichier):
    """Cherche un fichier par NOM exact dans le dossier configuré. Renvoie
    son ID Drive, ou None s'il n'existe pas encore (ou en cas d'erreur)."""
    service = _construire_service()
    if not service:
        return None
    nom_echappe = nom_fichier.replace("'", "\\'")
    requete = f"name = '{nom_echappe}' and '{_dossier_id()}' in parents and trashed = false"
    try:
        resultats = service.files().list(q=requete, fields="files(id, name)", pageSize=1).execute()
    except Exception as e:
        print(f"[Drive] Échec de la recherche de « {nom_fichier} » : {e}")
        return None
    fichiers = resultats.get("files", [])
    return fichiers[0]["id"] if fichiers else None


def uploader_fichier(nom_fichier, contenu_octets, type_mime="application/octet-stream"):
    """Envoie (crée ou met à jour) un fichier dans le dossier Drive
    configuré. `contenu_octets` doit être des bytes. Ne lève jamais
    d'exception : renvoie True en cas de succès, False sinon."""
    service = _construire_service()
    if not service:
        return False
    try:
        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(contenu_octets), mimetype=type_mime, resumable=False)
        fichier_id = _trouver_fichier(nom_fichier)
        if fichier_id:
            service.files().update(fileId=fichier_id, media_body=media).execute()
        else:
            metadonnees = {"name": nom_fichier, "parents": [_dossier_id()]}
            service.files().create(body=metadonnees, media_body=media, fields="id").execute()
        return True
    except Exception as e:
        print(f"[Drive] Échec de l'envoi de « {nom_fichier} » : {e}")
        return False


def telecharger_fichier(nom_fichier):
    """Télécharge le contenu (bytes) d'un fichier du dossier Drive, en le
    retrouvant par son nom. Renvoie None s'il n'existe pas ou en cas
    d'erreur — ne lève jamais d'exception."""
    service = _construire_service()
    if not service:
        return None
    try:
        from googleapiclient.http import MediaIoBaseDownload

        fichier_id = _trouver_fichier(nom_fichier)
        if not fichier_id:
            return None
        requete = service.files().get_media(fileId=fichier_id)
        buffer = io.BytesIO()
        telechargeur = MediaIoBaseDownload(buffer, requete)
        termine = False
        while not termine:
            _, termine = telechargeur.next_chunk()
        return buffer.getvalue()
    except Exception as e:
        print(f"[Drive] Échec du téléchargement de « {nom_fichier} » : {e}")
        return None


def lister_noms_fichiers():
    """Liste les noms de tous les fichiers présents dans le dossier Drive
    configuré (utilisé pour savoir quoi restaurer au démarrage). Renvoie
    une liste vide en cas d'erreur ou si Drive n'est pas configuré."""
    service = _construire_service()
    if not service:
        return []
    noms = []
    jeton_page = None
    try:
        while True:
            resultats = service.files().list(
                q=f"'{_dossier_id()}' in parents and trashed = false",
                fields="nextPageToken, files(name)",
                pageSize=100,
                pageToken=jeton_page,
            ).execute()
            noms.extend(f["name"] for f in resultats.get("files", []))
            jeton_page = resultats.get("nextPageToken")
            if not jeton_page:
                break
    except Exception as e:
        print(f"[Drive] Échec du listing du dossier : {e}")
        return []
    return noms
