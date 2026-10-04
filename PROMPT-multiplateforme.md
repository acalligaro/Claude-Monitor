Crée un tableau de bord local de suivi de mon utilisation de Claude Code. Mon OS : <macOS | Linux | Windows>. Utilise un agent pour le faire.

## Contraintes (non négociables)
- Python 3, bibliothèque standard uniquement : aucune dépendance (ni pip, ni npm).
- Le serveur écoute sur 127.0.0.1 UNIQUEMENT, jamais sur 0.0.0.0. Aucun accès depuis le réseau.
- Ne lis aucune clé API, aucun credential, aucun token. Lis uniquement les logs JSONL de Claude Code.
- N'exécute toi-même aucune commande admin (sudo ou administrateur). Donne-les-moi à lancer, avec une explication ligne par ligne.
- Ne supprime aucun fichier existant.
- Dossier : `~/claude-monitor/` (Windows : `%USERPROFILE%\claude-monitor\`). Fichiers : `server.py`, `index.html`, `test_server.py`, `README.md`, `limits.json`, `pricing.json`.

## Données
- Source : `~/.claude/projects/**/*.jsonl` (Windows : `%USERPROFILE%\.claude\projects\`). Les sous-agents sont dans `subagents/`.
- Lecture incrémentale : garde un offset par fichier, ne relis que les fichiers modifiés, ignore ceux de plus de 8 jours.
- Tokens par modèle : input, output, cache_creation (sépare `ephemeral_1h_input_tokens`), cache_read. Regroupe-les sur 3 périodes : bloc de 5 h en cours, jour, 7 jours glissants.
- Actions : chaque `tool_use` et son `tool_result`, avec :
  - l'outil, un détail court et l'état (⏳, ✓ ou « sans résultat ») ;
  - l'agent : principal, ou sous-agent avec sa description ;
  - l'ID de session sur 8 caractères.

## Serveur
- Utilise `ThreadingHTTPServer` avec un timeout de 30 s. Un serveur mono-thread bloque le live.
- `GET /` : la page.
- `GET /api/data` : un instantané JSON.
- `GET /api/live` : un flux SSE.
  - Un événement `full` à chaque changement.
  - Des événements `action` et `agent` pour les nouveautés.
  - Un heartbeat toutes les 15 s.
  - Le watcher vérifie les logs toutes les ~1 s.
- `POST /api/calibrate` : écrit uniquement dans `limits.json`.

### Tarifs
- Un thread daemon dédié lit la page publique https://docs.claude.com/en/docs/about-claude/pricing.md.
  - Avec urllib, un User-Agent explicite (celui de Python par défaut reçoit une 403) et un timeout de 10 s.
- Il extrait le tableau en $ par million de tokens : Input, écriture cache 5 min, écriture cache 1 h, lecture cache, Output.
- Il met le résultat en cache dans `pricing.json` (`fetched_at`, URL source) et le rafraîchit au plus une fois par 24 h.
- Si la page est injoignable, il garde le cache. S'il n'y a pas de cache, il utilise une table de secours en dur, marquée « secours ».
- Associe les ID de modèle par famille et version, après normalisation. Un modèle introuvable affiche « prix inconnu », jamais un prix inventé.
- Coût = in × Input + cache5m × écriture 5 min + cache1h × écriture 1 h + cache_read × lecture + out × Output.

## Page (un seul `index.html`, aucun CDN)
- Pleine largeur, grille CSS `grid-auto-flow: dense`. Nombre de colonnes selon la largeur :

  | Largeur | Colonnes |
  |---|---|
  | moins de 1000 px | 1 |
  | 1000 px et plus | 2 |
  | 1400 px et plus | 3 |
  | 1800 px et plus | 4 |

  Pas de vide, pas de défilement horizontal de la page.

### Bloc Utilisation (tokens)
- Il occupe environ 2/3 de la rangée.
- Une ligne par modèle, avec une colonne « Coût est. ($) » et un total, pour les 3 périodes.
- Nombres courts (« 1,8 M », « 291 k ») en `tabular-nums`. En-têtes courts, avec le libellé complet en `title`.
- Aucun défilement horizontal à partir de 1000 px de large.
- Mention sous le tableau : « Estimation au tarif API public. Abonnement non facturé au token. Prix du <date> ».

### Bloc Simulation (à côté d'Utilisation)
- 2 lignes, Pro et Max 5x. Colonnes : « % moyen par session 5 h » et « % semaine ».
- Tokens pondérés = in + 5×out + 1,25×cache_create + 0,1×cache_read.
- Budgets par défaut (des estimations) :

  | Offre | Bloc 5 h | Semaine |
  |---|---|---|
  | Max 5x | 6,27 M | 209 M |
  | Pro | 1,254 M | 59,7 M |

- Formulaire de calibration dans un `<details>` replié : je saisis mon offre et les % lus dans `/usage`, et les budgets sont recalculés.

### Bloc Live : agents et actions
- Flux continu : les nouveautés arrivent en tête avec un flash, 150 lignes au maximum.
- Chaque ligne affiche :
  - l'heure (la date complète en `title`) ;
  - une icône d'état ;
  - un badge agent ;
  - l'ID de session sur 8 caractères, en petit ;
  - l'outil et le détail ;
  - « sans résultat » écrit en clair quand c'est le cas.
- Bouton Pause : les événements reçus pendant la pause attendent dans une file.
- Indicateur « ● Live » avec l'heure de la dernière mise à jour.
- Si le SSE coupe, la page interroge le serveur toutes les 2 s.

### Bloc Sous-agents
- Pour chaque sous-agent : sa description, son début, sa fin, et son état (en cours ou terminé).

## Démarrage automatique et accès http://claude.local (port 80)
Le serveur tourne sans droits admin sur 127.0.0.1:8765. Le port 80 est obtenu par une redirection locale.

### macOS
- **Service** : LaunchAgent `~/Library/LaunchAgents/local.claude-monitor.plist`, avec `RunAtLoad` et `KeepAlive`. Logs dans `~/claude-monitor/logs/`.
- **Nom** : ajoute `127.0.0.1 claude.local` dans `/etc/hosts`.
- **Port 80** : règle pf dans l'ancre `com.apple/claude-monitor`. N'utilise JAMAIS `pfctl -f -` seul : cette commande remplace toutes les règles pf.
  - Règle : `rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port 8765`
  - Persistance : un LaunchDaemon `local.claude-monitor-pf` qui exécute `pfctl -a com.apple/claude-monitor -f /etc/pf.anchors/claude-monitor && pfctl -E`.
- **Redémarrer** : `launchctl kickstart -k gui/$(id -u)/local.claude-monitor`

### Linux
- **Service** : service systemd utilisateur `~/.config/systemd/user/claude-monitor.service`, avec `Restart=always`. Puis `systemctl --user enable --now claude-monitor` et `loginctl enable-linger $USER`.
- **Nom** : ajoute `127.0.0.1 claude.local` dans `/etc/hosts`.
  - Si mDNS (Avahi) intercepte les noms en `.local`, vérifie que la ligne `hosts:` de `/etc/nsswitch.conf` passe par `files` en premier.
  - Sinon, utilise `claude.localhost`.
- **Port 80** : redirection nftables limitée à la machine locale.
  `nft add table ip claude; nft add chain ip claude out '{ type nat hook output priority -100; }'; nft add rule ip claude out ip daddr 127.0.0.1 tcp dport 80 redirect to :8765`
  - Rends-la persistante via `/etc/nftables.conf` ou une unité systemd système.
  - Alternative : `setcap cap_net_bind_service` sur une copie dédiée de python, pour écouter directement sur le port 80. Présente les compromis des deux options.
- **Gérer** : `systemctl --user restart claude-monitor` et `journalctl --user -u claude-monitor`

### Windows
- **Service** : tâche planifiée « À l'ouverture de session » qui lance `pythonw.exe server.py`, sans fenêtre, avec redémarrage en cas d'échec. Crée-la avec `Register-ScheduledTask` (PowerShell) ou `schtasks /Create ... /SC ONLOGON`.
- **Nom** : ajoute `127.0.0.1 claude.local` dans `C:\Windows\System32\drivers\etc\hosts` (droits admin).
- **Port 80** : dans PowerShell en admin, `netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=80 connectaddress=127.0.0.1 connectport=8765`.
  - Cette redirection persiste après un redémarrage.
  - Vérifie d'abord que le port 80 est libre : IIS ou HTTP.sys peuvent déjà l'occuper.
- **Pare-feu** : aucune règle entrante à créer, tout reste sur 127.0.0.1.
- **Gérer** : `schtasks /Run /TN claude-monitor` et `schtasks /End /TN claude-monitor`

## Tests et vérifications (obligatoires)
- `test_server.py` utilise de simples `assert`, sans framework. Il couvre :
  - la lecture incrémentale ;
  - le regroupement par période ;
  - la simulation ;
  - l'extraction des tarifs depuis un extrait markdown figé ;
  - l'association des ID de modèle ;
  - un calcul de coût connu.
- Après le démarrage, vérifie :
  - `http://claude.local/` renvoie 200 et `/api/data` répond ;
  - le port écoute uniquement sur 127.0.0.1 : `lsof -nP -iTCP:8765 -sTCP:LISTEN` ou `ss -ltnp` (macOS et Linux), `netstat -ano | findstr 8765` (Windows) ;
  - un accès depuis l'IP réseau de la machine est refusé ;
  - dans le navigateur, à 1440 px puis 1920 px de large : `scrollWidth <= clientWidth` sur la page et sur le bloc Utilisation ;
  - « ● Live » se met à jour sans recharger la page.
- Rapport final en français, court : fichiers créés, résultats des tests, commandes admin à me faire lancer.
