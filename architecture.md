---
# Partie machine : description structurée de l'architecture (YAML). La partie humaine suit après le bloc.
name: claude-monitor
description: Tableau de bord local de l'utilisation de Claude Code (tokens, limites, agents)
url: http://claude.local
updated: 2026-10-04
runtime: {language: python, version: "3.9+", dependencies: stdlib-only}
components:
  - id: statusline
    path: ~/.claude/statusline-command.sh
    role: Statusline Claude Code ; exporte rate_limits vers ratelimits.json à chaque affichage
    writes: [ratelimits.json]
  - id: server
    path: server.py
    role: Serveur HTTP (ThreadingHTTPServer) + watcher + rafraîchissement des tarifs
    listen: 127.0.0.1:8765
    threads:
      - {name: watcher, period_s: 1, does: "collect() incrémental des JSONL, calibration auto, diffusion SSE"}
      - {name: price_refresher, period_s: 3600, does: "relit pricing.md au plus 1 fois par 24 h"}
    reads: ["~/.claude/projects/*/*.jsonl", "~/.claude/projects/*/*/subagents/*.jsonl", ratelimits.json, limits.json, pricing.json, index.html]
    writes: [limits.json, pricing.json, logs/]
  - id: page
    path: index.html
    role: Interface unique (HTML/CSS/JS inline), mise à jour en SSE, repli en polling 2 s
  - id: launchagent
    path: ~/Library/LaunchAgents/local.claude-monitor.plist
    role: Lance server.py à l'ouverture de session (RunAtLoad, KeepAlive), sans root
  - id: pf
    path: /etc/pf.anchors/claude-monitor
    source: system/claude-monitor.pf
    role: "Redirection lo0 127.0.0.1:80 -> 127.0.0.1:8765 (ancre com.apple/claude-monitor)"
    loaded_by: /Library/LaunchDaemons/local.claude-monitor-pf.plist
  - id: hosts
    path: /etc/hosts
    role: "claude.local -> 127.0.0.1 et ::1"
endpoints:
  - {method: GET, path: /, returns: index.html}
  - {method: GET, path: /api/data, returns: "dernier instantané JSON (_snap)"}
  - {method: GET, path: /api/live, returns: "flux SSE (events: full, action, agent), 5 connexions max, ping 5 s"}
  - {method: POST, path: /api/plan, body: '{"plan": "pro" | "max5"}', effect: "écrit calibrated.plan dans limits.json et relance la calibration"}
data_files:
  - {path: ratelimits.json, writer: statusline, schema: '{at, five_hour: {used_percentage, resets_at}, seven_day: {used_percentage, resets_at}}', time_format: epoch-seconds}
  - {path: limits.json, writer: server, schema: '{pro: {block, week}, max5: {block, week}, calibrated: {date, plan, block_pct, week_pct, auto}}', time_format: iso8601}
  - {path: pricing.json, writer: server, schema: '{fetched_at, source, models: {modèle: [entrée, cache 5m, cache 1h, cache lu, sortie]}}'}
snapshot_keys: [generated, files, usage, daily, actions, subagents, sim, rl, pricing]
security:
  - Écoute sur 127.0.0.1 uniquement ; verify_request refuse tout client hors boucle locale
  - En-tête Host limité à claude.local, localhost, 127.0.0.1 (anti DNS rebinding)
  - POST limité à /api/plan, JSON obligatoire, Origin étranger refusé, corps <= 256 octets
  - Données exposées limitées aux noms d'outils, descriptions tronquées à 120 caractères, compteurs ; motifs de secrets masqués
  - Seul appel sortant : docs.claude.com pricing.md (repli docs.anthropic.com)
tests: {command: "python3 test_server.py", expects: [OK, OK incrémental, OK simulation, OK tarifs, OK calibration auto]}
---

# Architecture de Claude Monitor

Claude Monitor est un tableau de bord local qui montre l'utilisation de Claude Code sur ce Mac. Il est accessible à l'adresse http://claude.local. Tout fonctionne en local, avec la bibliothèque standard Python uniquement.

## Vue d'ensemble

```
Claude Code ──(JSON stdin)──> statusline-command.sh ──> ratelimits.json ─┐
Claude Code ──(transcripts)──> ~/.claude/projects/**/*.jsonl ────────────┤
                                                                          ▼
                                          server.py (127.0.0.1:8765)
                                          ├─ watcher (1 s) : collect(), calibration auto ──> limits.json
                                          ├─ price_refresher (24 h) ──> pricing.json
                                          └─ HTTP : /, /api/data, /api/live (SSE), /api/plan
                                                                          ▲
Navigateur ──> claude.local:80 ──(/etc/hosts + pf lo0)──> 127.0.0.1:8765 ┘
```

## Composants

| Composant | Fichier | Rôle |
|---|---|---|
| Statusline | `~/.claude/statusline-command.sh` | Affiche la ligne d'état de Claude Code et copie les limites réelles (`rate_limits`) dans `ratelimits.json`, avec une écriture atomique. |
| Serveur | `server.py` | Lit les transcripts, agrège les données, calibre les budgets et sert la page et l'API. |
| Page | `index.html` | Interface unique. Elle reçoit les mises à jour en SSE et passe en polling toutes les 2 s si le flux tombe. |
| Service | `~/Library/LaunchAgents/local.claude-monitor.plist` | Lance le serveur à l'ouverture de session et le relance s'il s'arrête. |
| Redirection | `/etc/pf.anchors/claude-monitor` (source : `system/`) | Redirige le port 80 vers le port 8765, uniquement sur la boucle locale. |
| Nom local | `/etc/hosts` | Fait pointer `claude.local` vers `127.0.0.1` et `::1`. |

## Flux de données

1. **Transcripts** : le watcher relit chaque seconde les fichiers JSONL modifiés depuis moins de 8 jours. La lecture est incrémentale (par offset). Le watcher en extrait :
   - les tokens par modèle, dédoublonnés par `message.id` ;
   - les appels d'outils ;
   - les sous-agents.
2. **Instantané** : `collect()` produit un objet avec les clés `usage`, `daily`, `actions`, `subagents`, `sim`, `rl` et `pricing`. S'il diffère du précédent, le serveur l'envoie à la page (événement `full`). Il envoie aussi des événements `action` et `agent` pour le flux Live.
3. **Limites réelles** : `rl` reprend `ratelimits.json`. La page en déduit :
   - le début de la session 5 h (`resets_at` − 5 h) et sa fin ;
   - le temps restant sur 5 h et sur 7 j ;
   - la date de réinitialisation de la semaine.

   Le compte à rebours est recalculé toutes les 30 s dans le navigateur.
4. **Simulation** : les tokens sont pondérés ainsi : entrée ×1, sortie ×5, cache créé ×1,25, cache lu ×0,1. Ils sont regroupés en blocs de 5 h, comme dans ccusage. Les pourcentages sont calculés par rapport aux budgets de `limits.json`.

## Calibration automatique

Le serveur recalibre à chaque nouveau relevé de `ratelimits.json`, si ce relevé a moins de 10 min :

- **budget = consommation pondérée sur la vraie fenêtre ÷ (pourcentage réel / 100)**, avec deux fenêtres : de fin − 5 h jusqu'à maintenant, et de fin − 7 j jusqu'à maintenant ;
- l'offre choisie est calibrée directement. L'autre offre est déduite : Max 5x = 5 × Pro sur 5 h, et 3,5 × Pro sur la semaine ;
- un pourcentage inférieur à 5 est ignoré, car trop imprécis.

L'offre se choisit dans la liste « Mon offre » de la page. Le choix passe par `POST /api/plan` et est stocké dans `calibrated.plan`.

**Limite** : seul l'usage de ce Mac est compté. Une utilisation sur claude.ai ou sur un autre poste fait baisser les budgets calculés.

## Sécurité

- Le serveur écoute sur `127.0.0.1` uniquement. Toute connexion qui ne vient pas de la boucle locale est refusée.
- L'en-tête Host doit être `claude.local`, `localhost` ou `127.0.0.1`. C'est une protection contre le DNS rebinding.
- Le seul POST accepté est `/api/plan`. Il exige du JSON, refuse les Origin étrangers et limite le corps à 256 octets.
- La page n'expose que des noms d'outils, des descriptions tronquées et des compteurs. Les motifs de secrets sont masqués.
- Le seul appel sortant est la lecture de la page des tarifs publics, au plus une fois par 24 h.

## Exploitation

```sh
launchctl kickstart -k gui/$(id -u)/local.claude-monitor   # redémarrer après une modification
python3 ~/claude-monitor/test_server.py                    # tests
tail -f ~/claude-monitor/logs/server.err.log               # erreurs
```

Le README contient l'installation détaillée : le service, la règle pf et la désinstallation.
