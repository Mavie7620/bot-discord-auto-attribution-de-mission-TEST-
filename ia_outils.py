# -*- coding: utf-8 -*-
"""
================= REGISTRE D'OUTILS POUR L'INTELLIGENCE ROYALE =================
Ce module ne contient AUCUNE logique métier propre à Valerius ou Osiris : il
fournit uniquement un registre générique d'"outils" (function calling) que
l'IA peut appeler pour aller chercher de VRAIES données au lieu d'en inventer.

Pourquoi un fichier séparé plutôt que de tout mettre dans bot.py ?
- Ça permet à N'IMPORTE QUEL bot (Valerius, Osiris, ou un futur bot ajouté
  plus tard) d'enregistrer ses propres outils avec `enregistrer_outil(...)`,
  sans jamais avoir à modifier ce fichier.
- Ajouter un nouveau bot plus tard = juste appeler `enregistrer_outil(...)`
  quelque part dans son propre code pour que ses données deviennent
  automatiquement accessibles à l'IA, dans TOUTES les interfaces (Discord ET
  site web), sans dupliquer le prompt système ni la boucle d'appel.

Utilisation typique (dans bot.py ou un futur fichier de bot) :

    import ia_outils

    def _outil_exemple(contexte, mon_parametre):
        # `contexte` est un dict fourni par interroger_ia() :
        # {"guild_id": ..., "guild": <discord.Guild ou None>,
        #  "joueur_id": ... (peut être None si la conversation n'est reliée
        #  à aucun joueur, ex: pas encore de compte Discord relié)}.
        # Ne JAMAIS accepter guild_id/joueur_id comme paramètres venant de
        # l'IA : ils sont injectés automatiquement pour éviter qu'elle
        # invente/mélange des ID de serveur ou de joueur (et consulte ainsi
        # les données d'un autre joueur que celui qui lui parle).
        return {"resultat": "..."}

    ia_outils.enregistrer_outil(
        nom="exemple",
        description="Explique clairement QUAND l'IA doit utiliser cet outil.",
        parametres={
            "type": "object",
            "properties": {"mon_parametre": {"type": "string", "description": "..."}},
            "required": ["mon_parametre"],
        },
        executer=_outil_exemple,
    )
"""
import inspect
import json

# {nom_outil: {"description":..., "parametres": <json schema>, "executer": <callable>}}
_OUTILS = {}


def enregistrer_outil(nom, description, parametres=None, executer=None):
    """Enregistre un outil utilisable par l'IA (function calling).

    - nom : identifiant unique, court, en minuscules (ex: "profil_joueur").
    - description : phrase claire indiquant QUAND l'IA doit s'en servir.
      C'est la seule chose que l'IA lit pour décider d'appeler l'outil :
      soyez précis et sans ambiguïté.
    - parametres : schéma JSON Schema des arguments visibles par l'IA.
      Ne PAS y inclure guild_id/guild : ils sont injectés automatiquement
      côté serveur via `contexte`, jamais fournis par l'IA elle-même.
    - executer : fonction `(contexte, **arguments) -> résultat` où `résultat`
      doit être sérialisable en JSON (dict/list/str/int/float/bool/None).
      Peut être une fonction normale OU une coroutine (async def).

    Peut aussi être utilisé comme décorateur :

        @enregistrer_outil("nom", "description", {...schema...})
        def _fonction(contexte, param1):
            ...
    """
    def decorateur(fonction):
        _OUTILS[nom] = {
            "description": description,
            "parametres": parametres or {"type": "object", "properties": {}},
            "executer": fonction,
        }
        return fonction

    if executer is not None:
        decorateur(executer)
        return None
    return decorateur


def outils_enregistres():
    """Liste les noms de tous les outils actuellement enregistrés (pratique
    pour vérifier au démarrage que rien ne manque, ex: dans /tutoadm)."""
    return sorted(_OUTILS.keys())


def definitions_pour_ia():
    """Renvoie la liste des définitions d'outils au format attendu par
    l'API OpenAI-compatible (Groq, et donc aussi si un jour on repasse sur
    OpenAI ou Anthropic via leur wrapper de compatibilité)."""
    return [
        {
            "type": "function",
            "function": {
                "name": nom,
                "description": info["description"],
                "parameters": info["parametres"],
            },
        }
        for nom, info in _OUTILS.items()
    ]


async def executer_outil(nom, contexte, arguments_json):
    """Exécute l'outil `nom` avec les arguments (chaîne JSON) fournis par
    l'IA, en y injectant `contexte` (guild_id, guild, ...) en premier
    argument. Ne lève JAMAIS d'exception : toute erreur est renvoyée sous
    forme de message exploitable par l'IA, pour qu'elle puisse répondre
    proprement ("je n'ai pas trouvé cette donnée") plutôt que de planter."""
    info = _OUTILS.get(nom)
    if not info:
        return json.dumps({"erreur": f"Outil « {nom} » inconnu."}, ensure_ascii=False)
    try:
        arguments = json.loads(arguments_json) if arguments_json else {}
        if not isinstance(arguments, dict):
            arguments = {}
    except Exception:
        arguments = {}
    try:
        resultat = info["executer"](contexte, **arguments)
        if inspect.isawaitable(resultat):
            resultat = await resultat
        return json.dumps(resultat, ensure_ascii=False, default=str)
    except TypeError as e:
        return json.dumps({"erreur": f"Argument(s) invalide(s) pour « {nom} » : {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"erreur": f"Erreur lors de l'exécution de « {nom} » : {e}"}, ensure_ascii=False)
