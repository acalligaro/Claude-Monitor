# Claude Monitor

*Local dashboard for Claude Code usage* · [Version française](README.md)

Local dashboard, read-only on `~/.claude/projects` and limited to the Python standard library. It shows:

- tokens per model, with a cost estimated at the public API rate (`pricing.json`, re-read from https://docs.claude.com/en/docs/about-claude/pricing.md at most once every 24 h; hard-coded fallback table if the page is unreachable, « prix inconnu » (unknown price) if the model is not listed). This is the server's only outgoing call;
- a simulation of usage for the Pro and Max 5x plans;
- a **Live** feed of agents and actions, streamed over SSE;
- sub-agents.

The whole page updates without reloading. The « ● Live » indicator is green when the feed is connected and orange while reconnecting. If the SSE feed is unavailable, the page polls `/api/data` every 2 s. The page is in French only.

Address: **http://claude.local**

## Security: local access only
- The server listens on `127.0.0.1:8765` only. It does not listen on `0.0.0.0`, on `::`, or on any network interface.
- Any connection whose client address is not loopback is refused.
- Only the Host headers `claude.local`, `localhost` and `127.0.0.1` are accepted. Others get a 403.
- The only POST is `/api/plan` (plan selection). It requires JSON and refuses foreign Origins, to prevent another site from calling it.
- The page only exposes tool names, short descriptions (120 characters maximum) and counters.
- The server only writes `limits.json`, during automatic calibration, and the logs in `logs/`.

## Service (user LaunchAgent, no root)
The file `~/Library/LaunchAgents/local.claude-monitor.plist` uses `/usr/bin/python3`, with `CLAUDE_MONITOR_PORT=8765`, `RunAtLoad` and `KeepAlive`. Logs go to `~/claude-monitor/logs/`.

```sh
launchctl print gui/$(id -u)/local.claude-monitor | grep -E 'state|pid'             # état
launchctl kickstart -k gui/$(id -u)/local.claude-monitor                            # redémarrer (après une modification du code)
launchctl bootout gui/$(id -u)/local.claude-monitor                                 # arrêter (jusqu'à la prochaine session)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.claude-monitor.plist  # relancer
# désinstaller
launchctl bootout gui/$(id -u)/local.claude-monitor; rm ~/Library/LaunchAgents/local.claude-monitor.plist
```

The comments in this block mean, in order: status, restart (after a code change), stop (until the next session), start again, uninstall.

## Port 80 to 8765: persistent pf rule (root, run once)
The files are in `system/`. The rule only acts on `lo0`, in the `com.apple/claude-monitor` anchor. The file `/etc/pf.conf` is not modified: the anchor is already included by `com.apple/*`.

```sh
sudo cp ~/claude-monitor/system/claude-monitor.pf /etc/pf.anchors/claude-monitor
sudo cp ~/claude-monitor/system/local.claude-monitor-pf.plist /Library/LaunchDaemons/
sudo chown root:wheel /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
sudo chmod 644 /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/local.claude-monitor-pf.plist
```

To remove the rule:

```sh
sudo launchctl bootout system/local.claude-monitor-pf
sudo pfctl -a com.apple/claude-monitor -F all
sudo rm /etc/pf.anchors/claude-monitor /Library/LaunchDaemons/local.claude-monitor-pf.plist
```

`/etc/hosts` must also contain `127.0.0.1 claude.local` and `::1 claude.local`.

> When the redirect is active, **direct** connections to `127.0.0.1:8765` stay stuck in `SYN_RCVD`. This behavior has been observed: the same server answers normally on another port. Use http://claude.local or http://127.0.0.1 (port 80).

## Pro and Max 5x simulation (estimate, unofficial)
Anthropic publishes no token limits. The figures shown are **estimates**, recalibrated automatically (see below).

- **Unit: weighted tokens per message**, deduplicated by `message.id`:
  `weighted = input + 5×output + 1.25×cache_created + 0.1×cache_read`
- **5 h blocks**, as in ccusage: a block starts at the first message after the end of the previous block, or after a period of inactivity, and lasts 5 h.
- **5 h session**: the gauge shows the average of the blocks over the last 7 days. Below it are the current block and the maximum.
- **Week**: weighted total over a rolling 7 days. This is not your account's actual reset time.
- **Colors**: green under 70 %, orange under 90 %, red above.

### Default budgets (`limits.json`)
Community source: https://finopsllm.com/research/claude-pro-max-tokens-limit, a September 2026 study of Claude Code logs. It gives:

- Max 5x: a 5 h block stops at around 39 M raw tokens (median), 96 % of which is cache read;
- Pro: about 1/5 of Max 5x over 5 h;
- Max 5x over the week: at least 1.3 B raw tokens (lower bound), about 3.5 times Pro.

Conversion from raw to weighted:

- Composition used: 96 % cache read. The remaining 4 % follow the 7-day mix observed in your JSONL files on 3 October 2026: input 0.02 %, output 9.86 %, cache created 90.12 %.
- Average weight of these 4 % = 0.0002×1 + 0.0986×5 + 0.9012×1.25 = 1.620
- Factor k = 0.96×0.1 + 0.04×1.620 = **0.1608** weighted token per raw token

| Plan | 5 h block | Week |
|---|---|---|
| Max 5x | 39 M × k = **6.27 M** | 1.3 B × k = **209 M** |
| Pro | 6.27 M ÷ 5 = **1.254 M** | 209 M ÷ 3.5 = **59.7 M** |

### Calibration (automatic)
`~/.claude/statusline-command.sh` writes the real percentages (`rate_limits`) to `ratelimits.json`. On each new reading less than 10 min old, the server:

- computes the plan's budget: `weighted usage ÷ (percentage / 100)`, over the real windows (end − 5 h, end − 7 d);
- derives the other plan: Max 5x = 5 × Pro over 5 h, and 3.5 × Pro over the week;
- saves the result to `limits.json`.

A percentage under 5 is ignored, as too imprecise. The plan is chosen on the page (« Mon offre » (my plan) list, saved in `calibrated.plan` of `limits.json`, Max 5x by default). A change triggers recalibration on the next cycle.

You can also edit `limits.json` by hand. It is re-read on every cycle.

## Run manually and test
```sh
CLAUDE_MONITOR_PORT=8766 python3 ~/claude-monitor/server.py   # instance de test sur un autre port
python3 ~/claude-monitor/test_server.py                        # affiche OK, OK incrémental, OK simulation
```

The first command starts a test instance on another port. The second prints `OK`, `OK incrémental`, `OK simulation`.

Architecture details: [architecture.md](architecture.md) (in French).

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free to copy, modify and share for any noncommercial use, provided the "Required Notice" line (author name and source) is kept. Any commercial use requires written agreement.
