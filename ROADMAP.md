# ROADMAP defold-mcp

Plan d'évolution consolidé à partir de 4 sources :
1. Comparaison **Fulviuus/defold-mcp** (TypeScript, headless, ~45 outils) — juin 2026
2. Manuel officiel **`defold.com/manuals/ai-agents/`**
3. Docs LLM officielles **`https://www.defold.com/llms.txt`**
4. Manuel officiel **`defold.com/manuals/automated-testing/`**

> ChadAragorn/defold-mcp : rejeté (single-file, superficiel, aucun élément unique — le scaffolding de fichiers se couvre déjà par écriture directe).

> **Statut (2026-10-26)** : P0–P7 ✅ (commits `32bc660` → `80dafec`, + fix Bob `7f66971`) — 57 → 64 outils, smoke test OK.
> P8 en cours : fichiers côté TrickyChallenge en place (test collection, settings, runner auto-suffisant, bridge publish) ; **blocage** : crash Bob (`StringIndexOutOfBoundsException` dans `ComponentsCounter.isFactoryType`) quand le bootstrap pointe sur `/test/test.collectionc`. Acceptation CI pas encore validée.

## État actuel (baseline)

- Dossier canonique : `C:\GAMEDEV\DEFOLD\defold-mcp`
  - `server.py` ≈ 1200 lignes, **64 outils** (`@mcp.tool()`) après P3–P7
  - `defold_client.py` 974 lignes, `.venv` (mcp 2.3.0 → `ToolAnnotations` dispo, `@mcp.tool(annotations=...)` supporté)
- `C:\GAMEDEV\DEFOLD\_tools\defold-mcp` = **supprimé** le 26/10 (copie obsolète).
- Outils actuels : statut/éditeur (status, open_project, compile, run, console, preview, ref, clean_build, hot_reload), engine (engine_info, screenshot, inject_input, stop), Automation Bridge v2 (health, lifecycle, screen, resize, convert_coordinates, frame, scene, elements, element, click, drag, drag_path, key, wheel, input_*, pointer_*, recording_*, events*, state*, application_catalog, command*, marker, metal*), GUI éditeur (editor_screenshot, editor_find, editor_click, editor_type, editor_key), + P3 `defold_docs`, + P4 `defold_bob_build`/`bob_bundle`/`bob_doctor`, + P5 `defold_editor_command`, + P6 `defold_screenshot_diff`, + P7 `defold_test_run`.

## Règles de travail (sessions de implémentation)

- Code : `C:\GAMEDEV\DEFOLD\defold-mcp` ; venv : `.venv\Scripts\python.exe`
- Après chaque phase : `py_compile server.py` + import smoke test (`import server`, compter les outils, vérifier annotations)
- Fichiers temporaires / artefacts → `ai_work/` du projet concerné (jamais à la racine d'un projet)
- Une phase = un commit logique ; ne pas mélanger les phases

---

## Phase 0 — Housekeeping ✅

- [x] Marquer `_tools\defold-mcp` comme deprecated (README : "moved to C:\GAMEDEV\DEFOLD\defold-mcp") ou supprimer après validation utilisateur → **dossier supprimé**
- [x] Vérifier `requirements.txt` complet et `mcp-config.example.json` à jour (`mcp>=2.3.0`, `pillow>=10.0`)

**Acceptation** : un seul emplacement canonique documenté. ✅

---

## Phase 1 — Niveaux de privilège + mode read-only *(source : manuel ai-agents)* ✅

Le manuel recommande de séparer **read / verification / modification / privileged**.

- [x] Importer `ToolAnnotations` (mcp.types) + factory `tool(level)` qui enveloppe `@mcp.tool(annotations=...)` et gate les outils modify/privileged si env `DEFOLD_MCP_READONLY=1` (renvoyer JSON `{success:false, error:"blocked: read-only"}`)
- [x] Remplacer les 57 `@mcp.tool()` par `@tool("<level>")` selon ce mapping :

| Level | Outils |
|---|---|
| **read** | status, console, ref, engine_info, preview, bridge_health, bridge_lifecycle, bridge_screen, bridge_frame, bridge_scene, bridge_elements, bridge_element, bridge_convert_coordinates, bridge_input_status, bridge_input_pending, bridge_recording_capabilities, bridge_recording_status, bridge_events_cursor, bridge_events, bridge_state, bridge_state_wait, bridge_application_catalog, bridge_command_status, bridge_metal_status, editor_screenshot, editor_find |
| **verify** | compile, clean_build, run |
| **modify** | open_project, stop, hot_reload, inject_input, bridge_resize_screen, bridge_click, bridge_drag, bridge_drag_path, bridge_key, bridge_wheel, bridge_input_cancel, bridge_input_flush, bridge_input_configure, bridge_pointer_open/move/hold/up, bridge_recording_start/stop, bridge_marker, bridge_metal, bridge_metal_stop, editor_click, editor_type, editor_key |
| **privileged** | bridge_command, bridge_command_cancel (dispatch de commandes applicatives arbitraires ≈ /eval runtime → à réserver) |

- [x] `defold_status` expose le mode actif (`mcp_mode: read-only|standard`) et les annotations par outil

**Acceptation** : py_compile OK ; smoke test : 57 outils, annotations cohérentes ; en read-only, un `defold_bridge_click` renvoie l'erreur structurée sans effet de bord. ✅ (commit `4562465`)

---

## Phase 2 — Résultats structurés + boucle verify *(source : ai-agents + automated-testing « failure reports »)* ✅

- [x] Standardiser les sorties des ops mutatives en JSON `{status, issues[], artifacts{}}` (run, open_project, stop, clean_build — compile est déjà OK)
- [x] Associer chaque op à une vérification déterministe documentée dans la docstring (ex. `defold_run` → relire `engine_info` + premières lignes de console ; `open_project` → `status`)
- [x] Convention : artefacts (screenshots, vidéos, diffs, logs) écrits sous `ai_work/artifacts/<run-id>/`

**Acceptation** : chaque outil mutatif renvoie un JSON uniformément parsable ; l'agent n'a pas besoin de texte libre pour décider du suivant. ✅ (commit `2ca8581`)

---

## Phase 3 — `defold_docs` headless *(source : llms.txt officiel, URLs vérifiées)* ✅

- [x] URLs (vérifiées le 26/10) : index `https://www.defold.com/llms/manuals.md` (~14 Ko), `/llms/apis.md` (~23 Ko), `/llms/examples.md` (~23 Ko) → liens vers .md individuels ; fichiers uniques `llms-apis.txt` ~1,7 Mo et `llms-full.txt` ~3,5 Mo (**à ne JAMAIS charger en contexte**)
- [x] Cache local `.cache/defold-docs/` des 3 index (TTL 24 h)
- [x] Outil `defold_docs(query)` : résolution via l'index → fetch de **un seul** .md ciblé → contenu rogné retourné
- [x] Fallback auto : `defold_ref` redirige vers `defold_docs` si l'éditeur est inatteignable
- [x] Docstring : avertissement version (llms docs = dernière release ; `/ref` = build exact de l'éditeur)

**Acceptation** : recherche doc sans éditeur ouvert, < 1 requête réseau par réponse après cache. ✅ (commit `3727b0d`)

---

## Phase 4 — Bob headless + build report JSON *(source : automated-testing « Headless tests and CI »)* ✅

- [x] `defold_bob_build` : `java -jar bob.jar --root . resolve build` avec `--build-report-json <path>` ; **parser le report JSON** (pas seulement l'exit status) → diagnostics structurés
- [x] `defold_bob_bundle` : bundle par plateforme, support `--settings <file>` et `--variant headless`. **Note** : Bob prend un seul argument `--settings <chemin de fichier>` (pas la paire nom/fichier du manuel) — corrigé en `7f66971`, avec `cwd=projet`
- [x] Résolution de bob : utiliser le bob de l'install Defold existante si présente, sinon cache par version dans `.cache/bob/` (approach Fulviuus) — JRE 25 + bob.jar mis en cache dans `.cache/java` / `.cache/bob`
- [x] `defold_bob_doctor` : java + bob + extender détectés

**Acceptation** : build CI reproductible **sans éditeur** ; le report JSON est exposé dans la réponse. ✅ (commits `cf522bc`, `7f66971`) — reste à valider sur bootstrap de test (voir P8).

---

## Phase 5 — Commande générique découvrable *(source : ai-agents « integration layer »)* ✅

- [x] `defold_editor_command(op, params)` : dispatch vers n'importe quel op `/command/*` découvert via `/openapi.json`, schéma des paramètres lu de la spec
- [x] Docstring : liste non exhaustive, découverte à la volée ; couvre endpoints actuels ET futurs sans nouveau code

**Acceptation** : un endpoint éditeur inconnu du serveur est utilisable sans modification du code. ✅ (commit `176615b`)

---

## Phase 6 — Diff visuel + baseline *(source : automated-testing « visual inspection »)* ✅

- [x] Ajouter `pillow` à `requirements.txt`
- [x] `defold_screenshot_diff(baseline_path?)` : % pixels changés + bounding boxes des zones ; image de différence sauvegardée sous `ai_work/artifacts/`
- [x] Convention baseline : `.baselines/<scene>.png` au niveau projet (gitignored) ; le manuel conseille diff déterministe > jugement multimodal, visuel = signal complémentaire à critères explicites

**Acceptation** : une régression visuelle produit métriques + image de diff, parsables par l'agent. ✅ (commit `7f15f5a`)

---

## Phase 7 — Collecteur `TEST {json}` *(source : automated-testing « Structured test results »)* ✅

Protocole officiel : préfixe unique + un JSON par ligne console, run id unique, événement final non ambigu (`pass/failure/crash/timeout`) :
```
TEST {"run":"8f13","event":"suite_start","tests":2}
TEST {"run":"8f13","event":"case","name":"player_moves","status":"pass","duration_ms":3}
TEST {"run":"8f13","event":"suite_end","status":"pass","passed":2,"failed":0}
```
- [x] Outil `defold_test_run(run_id?, timeout?)` : lancer/poller la console (et/ou événements bridge) jusqu'à l'événement final du run id ; couper sur completion / process terminé / erreur / timeout + limite de lignes ; ignorer le bruit engine
- [x] Résultat structuré : `{run, status, passed, failed, cases[], log_path}`

**Acceptation** : une suite de tests donne un verdict déterministe sans lecture libre des logs. ✅ (commit `80dafec`) — reste à valider end-to-end sur TrickyChallenge (voir P8).

---

## Phase 8 — Côté projet TrickyChallenge *(source : automated-testing + ai-agents AGENTS.md)* 🚧

- [x] Exposer les `automation_id` sur les éléments interactifs + publier l'état d'application (`app.ui.screen`) via la Lua API debug-only du bridge (`main/scripts/menu.script`, garde `type(automation_bridge) == "table"`)
- [x] Test collection dédiée : `/test/test.collectionc` + `test/settings` avec `[bootstrap] main_collection = /test/test.collectionc` ; **jamais** dans la config normale du repo (CI passe `--settings test/settings`) — ⚠️ build Bob avec ce bootstrap en crash (`ComponentsCounter.isFactoryType`), à corriger
- [x] Intégrer les tests de modules : **décision — runner auto-suffisant `test/runner.lua`** (pas de dépendance DefTest/Telescope) ; protocole `TEST {json}` conforme P7, exit code 0/1
- [x] Séparer logique pure (`main/logic/screen_manager.lua`, testable sans engine) du code engine-facing (`main/scripts/`)
- [x] `[project] write_log_file = 2` dans le settings de test (logs persistés, utile device/packagé)
- [x] Enrichir `TrickyChallenge/AGENTS.md` : structure + entry points, conventions, commandes build/test, protocole structuré, artefacts attendus (`ai_work/artifacts/`), bridge debug-only

**Acceptation** : un run de test headless (Phase 4) + collecteur (Phase 7) passe en CI sans éditeur et donne un verdict structuré. 🚧 **bloqué** par le crash Bob ci-dessus.

---

## Hors périmètre (décisions explicites)

- **Pas** d'exposition `/eval` ni shell au modèle (recommandation du manuel ai-agents ; on est déjà conforme)
- **Pas** de tests browser HTML5 (Playwright/Puppeteer + bridge JS `html5.run()`) tant que le web n'est pas un target
- **Pas** de transport HTTP multi-clients / app desktop (optionnel Fulviuus, non requis par le manuel stdio suffit)
- **Pas** d'outils scaffolding type ChadAragorn (écriture directe des fichiers par l'agent)
- **Pas** de stack RAG/embeddings lourde : index + fetch ciblé suffit pour les docs

## Ordre d'exécution recommandé

P0 → P1 → P2 (base) → P3, P4, P5, P6 (indépendants entre eux, parallélisables) → P7 (bénéficie de P2) → P8 (nécessite P4+P7 pour la CI complète).
