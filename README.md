# Defold MCP Server

Un serveur **MCP** (Model Context Protocol) pour le moteur **Defold**. Il permet à un
agent IA de piloter l'éditeur Defold et le jeu en cours d'exécution : compiler,
lancer, lire les logs, capturer des captures d'écran, chercher la doc API, et
piloter le jeu via l'**Automation Bridge v2** (inspection de scène, saisie,
enregistrement vidéo, événements, états, commandes, marqueurs).

> Defold est conçu pour l'automatisation HTTP : l'éditeur expose un serveur local
> (port dans `.internal/editor.port`) et un build de debug expose un « engine
> service ». Ce MCP est un fin client HTTP sur ces deux surfaces.

## Fichiers

| Fichier | Rôle |
|---|---|
| `defold_client.py` | Client HTTP (éditeur + engine service). Sans dépendance MCP, testable seul. |
| `server.py` | Serveur MCP (FastMCP) exposant les outils. |
| `mock_editor.py` | Éditeur + engine service simulés pour tester sans Defold. |
| `test_client.py` | Tests du client contre le mock. |
| `requirements.txt` | Dépendances. |

## Outils exposés

| Outil | Description |
|---|---|
| `defold_status` | Éditeur installé ? projet ouvert ? port ? version ? commandes dispo ? |
| `defold_open_project` | Connecter à l'éditeur (ou le lancer si `launch=true`). |
| `defold_compile` | Compiler sans lancer. Retourne `success` + `issues`. |
| `defold_run` | Compiler + lancer (build debug). |
| `defold_console` | Lire les logs (dernières lignes + régions sémantiques). |
| `defold_preview` | Rendre une ressource (collection, GUI, atlas…) en **PNG**. |
| `defold_ref` | Chercher la doc API embarquée dans l'éditeur. |
| `defold_engine_info` | `/ping`, `/info`, `/state` du jeu en cours. |
| `defold_screenshot` | Capture **runtime** via l'extension *Automation Bridge v2* (option `after_frames`). |
| `defold_inject_input` | Injecter un clic souris via l'*Automation Bridge v2*. |
| `defold_stop` | Arrêter l'éditeur lancé par ce serveur (best-effort). |
| `defold_clean_build` | Clean build : supprimer les artefacts et recompiler. |
| `defold_hot_reload` | Hot-reload : recharger les fichiers modifiés dans le jeu en cours. |
| `defold_bridge_health` | Santé du pont v2 : version, capacités, écran, périphériques. |
| `defold_bridge_lifecycle` | Stages du runtime (ordre d'exécution). |
| `defold_bridge_screen` | Rectangles nommés, scale d'affichage, convention de coordonnées. |
| `defold_bridge_resize_screen` | Redimensionner la fenêtre native du jeu. |
| `defold_bridge_convert_coordinates` | Convertir un point entre espaces de coordonnées. |
| `defold_bridge_frame` | Frame moteur et scene_sequence courants. |
| `defold_bridge_scene` | Arborescence runtime complète du jeu. |
| `defold_bridge_elements` | Recherche d'éléments runtime (filtres + pagination). |
| `defold_bridge_element` | Détail d'un élément (bounds + propriétés). |
| `defold_bridge_click` | Clic par x/y ou par id d'élément (bouton, modificateurs, visualisation). |
| `defold_bridge_drag` | Glisser-déposer entre deux points ou deux éléments. |
| `defold_bridge_drag_path` | Trajectoire de glisser multi-points. |
| `defold_bridge_key` | Saisie de texte et/ou touches spéciales (`{KEY_ENTER}`…). |
| `defold_bridge_wheel` | Molette souris (pas, position, élément). |
| `defold_bridge_input_status` | Statut d'un input injecté. |
| `defold_bridge_input_pending` | File FIFO des inputs en attente. |
| `defold_bridge_input_cancel` | Annuler un input en file/actif. |
| `defold_bridge_input_flush` | Vider la file d'inputs de la session. |
| `defold_bridge_input_configure` | Acquérir/renouveler le lease du contrôleur d'input. |
| `defold_bridge_pointer_open` | Ouvrir une session pointeur sous lease (renvoie un id). |
| `defold_bridge_pointer_move` | Déplacer un pointeur ouvert. |
| `defold_bridge_pointer_hold` | Maintenir un pointeur ouvert. |
| `defold_bridge_pointer_up` | Relâcher un pointeur ouvert. |
| `defold_bridge_recording_capabilities` | Disponibilité de l'enregistrement vidéo natif. |
| `defold_bridge_recording_status` | Enregistrement actif ou dernier finalisé. |
| `defold_bridge_recording_start` | Démarrer l'enregistrement vidéo (MP4). |
| `defold_bridge_recording_stop` | Arrêter et finaliser l'enregistrement. |
| `defold_bridge_events_cursor` | Curseurs d'événements (next/oldest). |
| `defold_bridge_events` | Long-poll des événements du jeu. |
| `defold_bridge_state` | États publiés (tous ou par nom exact). |
| `defold_bridge_state_wait` | Attendre une nouvelle révision d'état. |
| `defold_bridge_application_catalog` | Commandes/états/événements enregistrés par le jeu. |
| `defold_bridge_command` | Soumettre une commande nommée (renvoie un id). |
| `defold_bridge_command_status` | Statut d'une commande soumise. |
| `defold_bridge_command_cancel` | Annuler une commande en attente. |
| `defold_bridge_marker` | Insérer un marker de timeline. |
| `defold_bridge_metal` | Trace GPU Metal (macOS uniquement). |
| `defold_bridge_metal_status` | Statut de la trace Metal. |
| `defold_bridge_metal_stop` | Arrêter la trace Metal. |

## Installation

```bash
cd tools/defold-mcp
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## Configuration (LM Studio Bionic)

Le serveur MCP doit être configuré **manuellement** dans l'interface de Bionic
(Settings → MCP / « MCP servers ») : ajouter un serveur **stdio** avec :

- **Command** : `C:\GAMEDEV\DEFOLD\TrickyChallenge\tools\defold-mcp\.venv\Scripts\python.exe`
- **Args** : `C:\GAMEDEV\DEFOLD\TrickyChallenge\tools\defold-mcp\server.py`
- **Env** (optionnel) : `DEFOLD_PROJECT=C:\GAMEDEV\DEFOLD\TrickyChallenge`

## Utilisation typique (par un agent)

1. `defold_status` — vérifier que l'éditeur est installé/ouvert.
2. `defold_open_project` — ouvrir/lancer le projet.
3. `defold_compile` — compiler et corriger les `issues`.
4. `defold_run` — lancer le jeu.
5. `defold_console` — lire les logs.
6. `defold_screenshot` / `defold_preview` — voir l'état visuel.
7. `defold_inject_input` — interagir.
8. `defold_bridge_scene` / `defold_bridge_click` / `defold_bridge_key` — inspecter et piloter le jeu via l'Automation Bridge v2.

## Notes importantes

- **API expérimentale** : l'API HTTP de l'éditeur peut changer entre versions.
  `/openapi.json` est la source de vérité ; le client le lit pour découvrir les
  commandes disponibles.
- **Préview ≠ screenshot runtime** : `/preview/` rend une ressource statique
  (pas d'objets dynamiques ni post-processing). Pour un vrai screenshot du jeu,
  il faut l'engine service + l'extension **Automation Bridge**.
- **Automation Bridge v2** : tous les outils `defold_bridge_*` (ainsi que
  `defold_screenshot` et `defold_inject_input`) nécessitent que le jeu soit en
  cours d'exécution et que l'extension debug officielle `automation-bridge`
  soit installée dans le projet. Sans elle, ces outils renvoient une erreur
  claire. Les outils `defold_bridge_metal*` ne fonctionnent que sur macOS.
- **Lancement de l'éditeur** : la découverte passe par
  `%LOCALAPPDATA%\Defold\installations.json` (champ `launcherPath`), puis les
  emplacements courants, puis le `PATH`. Le lancement attend la création de
  `.internal/editor.port` puis la réponse de `/openapi.json`.

## Tests (sans Defold installé)

```bash
python tools/defold-mcp/test_client.py
```

Cela démarre le mock (éditeur + engine service) et valide toute la logique HTTP :
découverte du port, openapi, compile/run, console, découverte de l'engine
service, preview PNG, ref, screenshot, input, et les chemins d'erreur.

## Limites connues

- Defold n'étant pas installé sur cette machine, les chemins de
  **lancement/exécution/screenshot réels** ne sont pas testés en conditions
  réelles ici — ils sont construits de façon robuste (gestion gracieuse de
  « non installé / non en cours ») et validés via le mock.
