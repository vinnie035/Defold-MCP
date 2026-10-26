# ROADMAP defold-mcp

Plan d'évolution consolidé à partir de 4 sources :
1. Comparaison **Fulviuus/defold-mcp** (TypeScript, headless, ~45 outils) — juin 2026
2. Manuel officiel **`defold.com/manuals/ai-agents/`**
3. Docs LLM officielles **`https://www.defold.com/llms.txt`**
4. Manuel officiel **`defold.com/manuals/automated-testing/`**

> ChadAragorn/defold-mcp : rejeté (single-file, superficiel, aucun élément unique — le scaffolding de fichiers se couvre déjà par écriture directe).

## État actuel (baseline)

- Dossier canonique : `C:\GAMEDEV\DEFOLD\defold-mcp`
  - `server.py` ≈ 1175 lignes, **57 outils** (`@mcp.tool()`)
  - `defold_client.py` 974 lignes, `.venv` (mcp 2.3.0 → `ToolAnnotations` dispo, `@mcp.tool(annotations=...)` supporté)
- `C:\GAMEDEV\DEFOLD\_tools\defold-mcp` = **copie obsolète** (server.py du 23/10, 862 lignes). À marquer deprecated ou supprimer après validation.
- Outils actuels : statut/éditeur (status, open_project, compile, run, console, preview, ref, clean_build, hot_reload), engine (engine_info, screenshot, inject_input, stop), Automation Bridge v2 (health, lifecycle, screen, resize, convert_coordinates, frame, scene, elements, element, click, drag, drag_path, key, wheel, input_*, pointer_*, recording_*, events*, state*, application_catalog, command*, marker, metal*), GUI éditeur (editor_screenshot, editor_find, editor_click, editor_type, editor_key).

## Règles de travail (sessions de implémentation)

- Code : `C:\GAMEDEV\DEFOLD\defold-mcp` ; venv : `.venv\Scripts\python.exe`
- Après chaque phase : `py_compile server.py` + import smoke test (`import server`, compter les outils, vérifier annotations)
- Fichiers temporaires / artefacts → `ai_work/` du projet concerné (jamais à la racine d'un projet)
- Une phase = un commit logique ; ne pas mélanger les phases

---

## Phase 0 — Housekeeping

- [ ] Marquer `_tools\defold-mcp` comme deprecated (README : "moved to C:\GAMEDEV\DEFOLD\defold-mcp") ou supprimer après validation utilisateur
- [ ] Vérifier `requirements.txt` complet et `mcp-config.example.json` à jour

**Acceptation** : un seul emplacement canonique documenté.

---

## Phase 1 — Niveaux de privilège + mode read-only *(source : manuel ai-agents)*

Le manuel recommande de séparer **read / verification / modification / privileged**.

- [ ] Importer `ToolAnnotations` (mcp.types) + factory `tool(level)` qui enveloppe `@mcp.tool(annotations=...)` et gate les outils modify/privileged si env `DEFOLD_MCP_READONLY=1` (renvoyer JSON `{success:false, error:"blocked: read-only"}`)
- [ ] Remplacer les 57 `@mcp.tool()` par `@tool("<level>")` selon ce mapping :

| Level | Outils |
|---|---|
| **read** | status, console, ref, engine_info, preview, bridge_health, bridge_lifecycle, bridge_screen, bridge_frame, bridge_scene, bridge_elements, bridge_element, bridge_convert_coordinates, bridge_input_status, bridge_input_pending, bridge_recording_capabilities, bridge_recording_status, bridge_events_cursor, bridge_events, bridge_state, bridge_state_wait, bridge_application_catalog, bridge_command_status, bridge_metal_status, editor_screenshot, editor_find |
| **verify** | compile, clean_build, run |
| **modify** | open_project, stop, hot_reload, inject_input, bridge_resize_screen, bridge_click, bridge_drag, bridge_drag_path, bridge_key, bridge_wheel, bridge_input_cancel, bridge_input_flush, bridge_input_configure, bridge_pointer_open/move/hold/up, bridge_recording_start/stop, bridge_marker, bridge_metal, bridge_metal_stop, editor_click, editor_type, editor_key |
| **privileged** | bridge_command, bridge_command_cancel (dispatch de commandes applicatives arbitraires ≈ /eval runtime → à réserver) |

- [ ] `defold_status` expose le mode actif (`mcp_mode: read-only|standard`) et les annotations par outil

**Acceptation** : py_compile OK ; smoke test : 57 outils, annotations cohérentes ; en read-only, un `defold_bridge_click` renvoie l'erreur structurée sans effet de bord.

---

## Phase 2 — Résultats structurés + boucle verify *(source : ai-agents + automated-testing « failure reports »)*

- [ ] Standardiser les sorties des ops mutatives en JSON `{status, issues[], artifacts{}}` (run, open_project, stop, clean_build — compile est déjà OK)
- [ ] Associer chaque op à une vérification déterministe documentée dans la docstring (ex. `defold_run` → relire `engine_info` + premières lignes de console ; `open_project` → `status`)
- [ ] Convention : artefacts (screenshots, vidéos, diffs, logs) écrits sous `ai_work/artifacts/<run-id>/`

**Acceptation** : chaque outil mutatif renvoie un JSON uniformément parsable ; l'agent n'a pas besoin de texte libre pour décider du suivant.

---

## Phase 3 — `defold_docs` headless *(source : llms.txt officiel, URLs vérifiées)*

- [ ] URLs (vérifiées le 26/10) : index `https://www.defold.com/llms/manuals.md` (~14 Ko), `/llms/apis.md` (~23 Ko), `/llms/examples.md` (~23 Ko) → liens vers .md individuels ; fichiers uniques `llms-apis.txt` ~1,7 Mo et `llms-full.txt` ~3,5 Mo (**à ne JAMAIS charger en contexte**)
- [ ] Cache local `.cache/defold-docs/` des 3 index (TTL 24 h)
- [ ] Outil `defold_docs(query)` : résolution via l'index → fetch de **un seul** .md ciblé → contenu rogné retourné
- [ ] Fallback auto : `defold_ref` redirige vers `defold_docs` si l'éditeur est inatteignable
- [ ] Docstring : avertissement version (llms docs = dernière release ; `/ref` = build exact de l'éditeur)

**Acceptation** : recherche doc sans éditeur ouvert, < 1 requête réseau par réponse après cache.

---

## Phase 4 — Bob headless + build report JSON *(source : automated-testing « Headless tests and CI »)*

- [ ] `defold_bob_build` : `java -jar bob.jar --root . resolve build` avec `--build-report-json <path>` ; **parser le report JSON** (pas seulement l'exit status) → diagnostics structurés
- [ ] `defold_bob_bundle` : bundle par plateforme, support `--settings <file>` (ex. `/test.settings`) et `--variant headless` ; commande de référence du manuel : `java -jar bob.jar --root . --settings test /test.settings --platform x86_64-linux --variant headless --archive --bundle-output build/test-bundle resolve build bundle`
- [ ] Résolution de bob : utiliser le bob de l'install Defold existante si présente, sinon cache par version dans `.cache/bob/` (approach Fulviuus) — décision à trancher ici
- [ ] `defold_bob_doctor` (optionnel) : java + bob + extender détectés

**Acceptation** : build CI reproductible **sans éditeur** ; le report JSON est exposé dans la réponse.

---

## Phase 5 — Commande générique découvrable *(source : ai-agents « integration layer »)*

- [ ] `defold_editor_command(op, params)` : dispatch vers n'importe quel op `/command/*` découvert via `/openapi.json`, schéma des paramètres lu de la spec
- [ ] Docstring : liste non exhaustive, découverte à la volée ; couvre endpoints actuels ET futurs sans nouveau code

**Acceptation** : un endpoint éditeur inconnu du serveur est utilisable sans modification du code.

---

## Phase 6 — Diff visuel + baseline *(source : automated-testing « visual inspection »)*

- [ ] Ajouter `pillow` à `requirements.txt`
- [ ] `defold_screenshot_diff(baseline_path?)` : % pixels changés + bounding boxes des zones ; image de différence sauvegardée sous `ai_work/artifacts/`
- [ ] Convention baseline : `.baselines/<scene>.png` au niveau projet (gitignored) ; le manuel conseille diff déterministe > jugement multimodal, visuel = signal complémentaire à critères explicites

**Acceptation** : une régression visuelle produit métriques + image de diff, parsables par l'agent.

---

## Phase 7 — Collecteur `TEST {json}` *(source : automated-testing « Structured test results »)*

Protocole officiel : préfixe unique + un JSON par ligne console, run id unique, événement final non ambigu (`pass/failure/crash/timeout`) :
```
TEST {"run":"8f13","event":"suite_start","tests":2}
TEST {"run":"8f13","event":"case","name":"player_moves","status":"pass","duration_ms":3}
TEST {"run":"8f13","event":"suite_end","status":"pass","passed":2,"failed":0}
```
- [ ] Outil `defold_test_run(run_id?, timeout?)` : lancer/poller la console (et/ou événements bridge) jusqu'à l'événement final du run id ; couper sur completion / process terminé / erreur / timeout + limite de lignes ; ignorer le bruit engine
- [ ] Résultat structuré : `{run, status, passed, failed, cases[], log_path}`

**Acceptation** : une suite de tests donne un verdict déterministe sans lecture libre des logs.

---

## Phase 8 — Côté projet TrickyChallenge *(source : automated-testing + ai-agents AGENTS.md)*

- [ ] Exposer les `automation_id` sur les éléments interactifs + publier l'état d'application (ex. `"screen" = "gameplay"`) via la Lua API debug-only du bridge — c'est exactement ce que l'exemple officiel attend
- [ ] Test collection dédiée : `/test/test.collectionc` + `/test.settings` avec `[bootstrap] main_collection = /test/test.collectionc` ; **jamais** dans la config normale du repo (CI passe `--settings`)
- [ ] Intégrer **DefTest** (lib communautaire basée sur Telescope : suites, setup/teardown, mocks des APIs Defold, LuaCov) pour les tests de modules
- [ ] Séparer logique pure (modules testables sans engine) du code engine-facing
- [ ] `[log] write_log_file` dans le settings de test (logs persistés, utile device/packagé)
- [ ] Enrichir `TrickyChallenge/AGENTS.md` : structure + entry points, conventions, commandes build/test, artefacts attendus (`ai_work/artifacts/`), fichiers interdits, ops nécessitant approbation, limites plateforme

**Acceptation** : un run de test headless (Phase 4) + collecteur (Phase 7) passe en CI sans éditeur et donne un verdict structuré.

---

## Hors périmètre (décisions explicites)

- **Pas** d'exposition `/eval` ni shell au modèle (recommandation du manuel ai-agents ; on est déjà conforme)
- **Pas** de tests browser HTML5 (Playwright/Puppeteer + bridge JS `html5.run()`) tant que le web n'est pas un target
- **Pas** de transport HTTP multi-clients / app desktop (optionnel Fulviuus, non requis par le manuel stdio suffit)
- **Pas** d'outils scaffolding type ChadAragorn (écriture directe des fichiers par l'agent)
- **Pas** de stack RAG/embeddings lourde : index + fetch ciblé suffit pour les docs

## Ordre d'exécution recommandé

P0 → P1 → P2 (base) → P3, P4, P5, P6 (indépendants entre eux, parallélisables) → P7 (bénéficie de P2) → P8 (nécessite P4+P7 pour la CI complète).
