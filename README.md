# Claude Monitor

*Tableau de bord local de l'utilisation de Claude Code* · [English version](README.en.md)

Tableau de bord local, en lecture seule sur `~/.claude/projects` et limité à la bibliothèque standard Python. Il affiche :

- les tokens par modèle, avec un coût estimé au tarif API public (`pricing.json`, relu depuis https://docs.claude.com/en/docs/about-claude/pricing.md au plus 1 fois par 24 h ; table de secours en dur si la page est injoignable, « prix inconnu » si le modèle n'y figure pas). Seul appel sortant du serveur ;
- une simulation de l'utilisation des offres Pro et Max 5x ;
- un flux **Live** des agents et des actions, transmis en SSE ;
- les sous-agents.

Toute la page se met à jour sans rechargement. L'indicateur « ● Live » est vert quand le flux est connecté et orange pendant une reconnexion. Si le flux SSE est indisponible, la page interroge `/api/data` toutes les 2 s.

Adresse : **http://claude.local**

## Sécurité : accès local uniquement
- Le serveur écoute sur `127.0.0.1:8765` seulement. Il n'écoute ni sur `0.0.0.0`, ni sur `::`, ni sur une interface réseau.
- Toute connexion dont l'adresse cliente n'est pas en boucle locale est refusée.
- Seuls les en-têtes Host `claude.local`, `localhost` et `127.0.0.1` sont acceptés. Les autres reçoivent une 403.
- Le seul POST est `/api/plan` (choix de l'offre). Il exige du JSON et refuse les Origin étrangers, pour empêcher un autre site de l'appeler.
- La page n'expose que des noms d'outils, des descriptions courtes (120 caractères maximum) et des compteurs.
- Les seules écritures du serveur sont `limits.json`, à la calibration automatique, et les journaux dans `logs/`.

## Service (LaunchAgent utilisateur, sans root)
Le fichier `~/Library/LaunchAgents/local.claude-monitor.plist` utilise `/usr/bin/python3`, avec `CLAUDE_MONITOR_PORT=8765`, `RunAtLoad` et `KeepAlive`. Les journaux vont dans `~/claude-monitor/logs/`.

```sh
launchctl print gui/$(id -u)/local.claude-monitor | grep -E 'state|pid'             # état
launchctl kickstart -k gui/$(id -u)/local.claude-monitor                            # redémarrer (après une modification du code)
launchctl bootout gui/$(id -u)/local.claude-monitor                                 # arrêter (jusqu'à la prochaine session)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.claude-monitor.plist  # relancer
# désinstaller
launchctl bootout gui/$(id -u)/local.claude-monitor; rm ~/Library/LaunchAgents/local.claude-monitor.plist
```

## Port 80 vers 8765 : règle pf persistante (root, à lancer une fois)
Les fichiers sont dans `system/`. La règle n'agit que sur `lo0`, dans l'ancre `com.apple/claude-monitor`. Le fichier `/etc/pf.conf` n'est pas modifié : l'ancre est déjà incluse par `com.apple/*`.

```sh
sudo cp ~/claude-monitor/system/claude-monitor.pf /etc/pf.anchors/claude-monitor
sudo cp ~/claude-monitor/system/local.claude-monitor-pf.plist /Library/LaunchDaemons/
sudo chown root:wheel /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
sudo chmod 644 /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.claude-monitor-pf.plist
```

Pour retirer la règle :

```sh
sudo launchctl bootout system/local.claude-monitor-pf
sudo pfctl -a com.apple/claude-monitor -F all
sudo rm /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
```

Il faut aussi que `/etc/hosts` contienne `127.0.0.1 claude.local` et `::1 claude.local`.

> Quand la redirection est active, les connexions **directes** à `127.0.0.1:8765` restent bloquées en `SYN_RCVD`. Ce comportement a été observé : le même serveur répond normalement sur un autre port. Passez par http://claude.local ou http://127.0.0.1 (port 80).

## Simulation Pro et Max 5x (estimation, non officielle)
Anthropic ne publie aucune limite en tokens. Les chiffres affichés sont des **estimations**, recalibrées automatiquement (voir plus bas).

- **Unité : tokens pondérés par message**, dédoublonnés par `message.id` :
  `pondéré = entrée + 5×sortie + 1,25×cache_créé + 0,1×cache_lu`
- **Blocs de 5 h**, comme dans ccusage : un bloc commence au premier message qui suit la fin du bloc précédent, ou une période d'inactivité, et dure 5 h.
- **Session 5 h** : la jauge affiche la moyenne des blocs des 7 derniers jours. En dessous figurent le bloc en cours et le maximum.
- **Semaine** : total pondéré sur 7 jours glissants. Ce n'est pas l'heure de remise à zéro réelle de ton compte.
- **Couleurs** : vert sous 70 %, orange sous 90 %, rouge au-delà.

### Budgets par défaut (`limits.json`)
Source communautaire : https://finopsllm.com/research/claude-pro-max-tokens-limit, une étude de septembre 2026 sur des logs Claude Code. Elle donne :

- Max 5x : un bloc de 5 h s'arrête vers 39 M tokens bruts en médiane, dont 96 % de cache lu ;
- Pro : environ 1/5 de Max 5x sur 5 h ;
- Max 5x sur la semaine : au moins 1,3 Md de tokens bruts (borne basse), soit environ 3,5 fois Pro.

Conversion de brut en pondéré :

- Composition retenue : 96 % de cache lu. Les 4 % restants suivent le mix observé sur 7 jours dans tes JSONL au 3 octobre 2026 : entrée 0,02 %, sortie 9,86 %, cache créé 90,12 %.
- Poids moyen de ces 4 % = 0,0002×1 + 0,0986×5 + 0,9012×1,25 = 1,620
- Facteur k = 0,96×0,1 + 0,04×1,620 = **0,1608** token pondéré par token brut

| Offre | Bloc de 5 h | Semaine |
|---|---|---|
| Max 5x | 39 M × k = **6,27 M** | 1,3 Md × k = **209 M** |
| Pro | 6,27 M ÷ 5 = **1,254 M** | 209 M ÷ 3,5 = **59,7 M** |

### Calibration (automatique)
`~/.claude/statusline-command.sh` écrit les % réels (`rate_limits`) dans `ratelimits.json`. À chaque nouveau relevé de moins de 10 min, le serveur :

- calcule le budget de l'offre : `consommation pondérée ÷ (pourcentage / 100)`, sur les vraies fenêtres (fin − 5 h, fin − 7 j) ;
- en déduit l'autre offre : Max 5x = 5 × Pro sur 5 h, et 3,5 × Pro sur la semaine ;
- enregistre le résultat dans `limits.json`.

Un % sous 5 est ignoré, car trop imprécis. L'offre se choisit sur la page (liste « Mon offre », enregistrée dans `calibrated.plan` de `limits.json`, Max 5x par défaut). Un changement relance la calibration au cycle suivant.

Tu peux aussi modifier `limits.json` à la main. Il est relu à chaque cycle.

## Lancer à la main et tester
```sh
CLAUDE_MONITOR_PORT=8766 python3 ~/claude-monitor/server.py   # instance de test sur un autre port
python3 ~/claude-monitor/test_server.py                        # affiche OK, OK incrémental, OK simulation
```

Détails d'architecture : [architecture.md](architecture.md).

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE) : copie, modification et partage gratuits autorisés pour tout usage non commercial, à condition de conserver la ligne « Required Notice » (nom de l'auteur et source). Tout usage commercial demande un accord écrit.
