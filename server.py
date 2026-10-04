#!/usr/bin/env python3
"""Tableau de bord local Claude Code. Lecture seule sur ~/.claude. stdlib uniquement."""
import glob, ipaddress, json, os, queue, re, threading, time, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CLAUDE_MONITOR_PORT", 80))  # port d'écoute, sur 127.0.0.1 uniquement
HOSTS = {"claude.local", "127.0.0.1", "localhost"}  # en-têtes Host acceptés (anti DNS rebinding)
ROOT = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_AGE = 8 * 86400
ACTIVE = 600
SECRET = re.compile(r"(sk-[\w-]{8,}|ghp_\w+|xox[bp]-\S+|eyJ[\w-]{10,}|[A-Za-z0-9+/_-]{32,}|password|passwd|secret|token=|api[_-]?key)", re.I)

_cache = {}  # path -> {"size","mtime","off","st"} ; muté uniquement par le thread watcher
MAX_SSE = 5          # connexions /api/live simultanées
KEEPALIVE = 5        # secondes entre deux pings SSE
LIMITS = os.path.join(HERE, "limits.json")  # seule écriture autorisée (calibration automatique)
W = (1, 5, 1.25, 0.1)  # poids relatifs : entrée, sortie, cache créé, cache lu
MULT = {"block": 5, "week": 3.5}  # Max 5x = MULT x Pro
RATELIMITS = os.path.join(HERE, "ratelimits.json")  # écrit par ~/.claude/statusline-command.sh (limites réelles 5 h / 7 j)
PRICING = os.path.join(HERE, "pricing.json")  # cache des tarifs API publics (seul appel sortant du serveur)
PRICE_URLS = ("https://docs.claude.com/en/docs/about-claude/pricing.md",
              "https://docs.anthropic.com/en/docs/about-claude/pricing.md")
# Secours si aucun cache : tarifs publics relevés le 2026-10-03, $/MTok [entrée, cache 5m, cache 1h, cache lu, sortie].
FALLBACK = {"fetched_at": None, "source": "secours", "models": {
    "fable-5.1": [10, 12.5, 20, 0.25, 50], "fable-5": [10, 12.5, 20, 1, 50],
    "opus-5.5": [4, 5, 8, 0.2, 20], "opus-5": [5, 6.25, 10, 0.5, 25], "opus-4.8": [5, 6.25, 10, 0.5, 25],
    "opus-4.7": [5, 6.25, 10, 0.5, 25], "opus-4.6": [5, 6.25, 10, 0.5, 25], "opus-4.5": [5, 6.25, 10, 0.5, 25],
    "opus-4.1": [15, 18.75, 30, 1.5, 75], "opus-4": [15, 18.75, 30, 1.5, 75],
    "sonnet-5.5": [2, 2.5, 4, 0.2, 10], "sonnet-5": [2, 2.5, 4, 0.2, 10], "sonnet-4.6": [3, 3.75, 6, 0.3, 15],
    "sonnet-4.5": [3, 3.75, 6, 0.3, 15], "sonnet-4": [3, 3.75, 6, 0.3, 15],
    "haiku-4.5": [1, 1.25, 2, 0.1, 5], "haiku-3.5": [0.8, 1, 1.6, 0.08, 4]}}
_prices = None       # tarifs en mémoire ; rafraîchis par le watcher au plus 1 fois par 24 h
_auto = {}           # dernier relevé statusline déjà utilisé pour la calibration automatique
_snap = {}           # dernier résultat de collect(), servi par /api/data
_subs, _subs_lock = [], threading.Lock()


def safe(s, n=120):
    s = " ".join(str(s or "").split())
    return "[masqué]" if SECRET.search(s) else s[:n]


def label(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Write", "Edit", "NotebookEdit", "MultiEdit"):
        return safe(os.path.basename(inp.get("file_path") or inp.get("notebook_path") or ""))
    if name.startswith("mcp__"):
        return safe(inp.get("description") or name.split("__", 2)[-1])
    if name == "Skill":
        return safe(inp.get("skill"))
    return safe(inp.get("description"))  # jamais input.command / prompt


def new_state(path):
    sub = os.path.basename(os.path.dirname(path)) == "subagents"
    return {"sub": sub, "agent": None, "session": None, "model": None, "last_ts": None,
            "usage": {}, "tools": [], "results": set(), "handback": False, "last_has_tool": False,
            "spawn_status": {}}


def feed(st, line):
    try:
        d = json.loads(line)
    except ValueError:
        return
    if not isinstance(d, dict):
        return
    st["session"] = d.get("sessionId") or st["session"]
    st["agent"] = d.get("agentId") or st["agent"]
    ts = d.get("timestamp")
    if ts:
        st["last_ts"] = ts
    m = d.get("message")
    if not isinstance(m, dict):
        return
    content = m.get("content") if isinstance(m.get("content"), list) else []
    if d.get("type") == "assistant":
        if m.get("model") and m["model"] != "<synthetic>":
            st["model"] = m["model"]
        u = m.get("usage")
        if isinstance(u, dict) and ts:
            # Une ligne par bloc de contenu, usage répété : dédoublonné par message.id.
            st["usage"][m.get("id") or d.get("uuid")] = (ts, st["model"] or "?", u.get("input_tokens") or 0,
                u.get("output_tokens") or 0, u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        has_tool = False
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                has_tool = True
                name = str(b.get("name") or "?")
                if name == "SubagentHandback":
                    st["handback"] = True
                st["tools"].append({"id": b.get("id"), "ts": ts, "name": name, "label": label(name, b.get("input"))})
        if content:
            st["last_has_tool"] = has_tool
    elif d.get("type") == "user":
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                st["results"].add(b.get("tool_use_id"))
                r = d.get("toolUseResult")
                if isinstance(r, dict) and r.get("agentId"):
                    st["spawn_status"][b.get("tool_use_id")] = str(r.get("status") or "")


def load(path):
    """Lecture incrémentale : ne relit que les octets ajoutés depuis la dernière fois."""
    s = os.stat(path)
    c = _cache.get(path)
    if c and c["size"] == s.st_size and c["mtime"] == s.st_mtime:
        return c["st"], s.st_mtime
    if not c or s.st_size < c["off"]:
        c = {"off": 0, "st": new_state(path)}
    with open(path, "rb") as f:
        f.seek(c["off"])
        data = f.read()
    end = data.rfind(b"\n") + 1  # ignorer une ligne partielle en cours d'écriture
    for line in data[:end].splitlines():
        feed(c["st"], line.decode("utf-8", "replace"))
    c.update(off=c["off"] + end, size=s.st_size, mtime=s.st_mtime)
    _cache[path] = c
    return c["st"], s.st_mtime


def weighted(tok):
    return sum(w * t for w, t in zip(W, tok))


def parse_pricing(md):
    """Tableau « Model | Base input | 5m cache writes | 1h cache writes | Cache hits | Output » -> {"opus-4.5": [5 prix]}."""
    out = {}
    for row in md.splitlines():
        m = re.match(r"\|\s*Claude (\w+)(?: ([\d.]+))?", row)
        p = re.findall(r"\$([\d.]+) / MTok", row)
        if m and len(p) == 5:
            out.setdefault(m[1].lower() + ("-" + m[2] if m[2] else ""), [float(x) for x in p])
    return out


def model_key(mid):
    """claude-opus-4-5-20251101 -> opus-4.5 ; claude-3-5-haiku-20241022 -> haiku-3.5 ; claude-fable-5-1[1m] -> fable-5.1."""
    parts = re.sub(r"\[.*", "", str(mid).lower()).split("-")
    fam = next((x for x in parts[1:] if x.isalpha()), "")
    ver = [x for x in parts[1:] if x.isdigit() and len(x) < 3]
    return fam + ("-" + ".".join(ver) if ver else "")


def cost(prices, model, v):
    """v = [entrée, sortie, cache créé (5m+1h), cache lu, dont cache 1h] -> $ ; None si modèle inconnu."""
    p = prices.get(model_key(model))
    if not p:
        return None
    return round((v[0] * p[0] + (v[2] - v[4]) * p[1] + v[4] * p[2] + v[3] * p[3] + v[1] * p[4]) / 1e6, 4)


def load_pricing(fetch=False, now=None):
    """Cache pricing.json ; si fetch et cache > 24 h : un essai par URL (urllib, 10 s). Échec -> cache, sinon secours."""
    try:
        with open(PRICING) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = None
    now = now or time.time()
    if fetch and not (cur and now - cur.get("ts", 0) < 86400):
        for url in PRICE_URLS:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "claude-monitor/1.0"}), timeout=10) as r:  # UA Python par défaut -> 403
                    models = parse_pricing(r.read(2_000_000).decode("utf-8", "replace"))
                if len(models) >= 3:
                    cur = {"fetched_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="minutes"),
                           "ts": now, "source": url, "models": models}
                    tmp = PRICING + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(cur, f, indent=2)
                    os.replace(tmp, PRICING)
                    break
            except (OSError, ValueError) as e:
                print("pricing:", url, e, flush=True)
    return cur or FALLBACK


def blocks(msgs):
    """Blocs de 5 h façon ccusage : un bloc démarre au premier message hors du bloc précédent. msgs = [(datetime, poids)]."""
    out = []
    for dt, w in sorted(msgs, key=lambda m: m[0]):
        if not out or dt >= out[-1]["start"] + timedelta(hours=5):
            out.append({"start": dt, "w": 0.0})
        out[-1]["w"] += w
    return out


def load_limits(path=None):
    try:
        with open(path or LIMITS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pro": {"block": 1254000, "week": 59720000}, "max5": {"block": 6271000, "week": 209000000}, "calibrated": None}


def load_ratelimits(path=None):
    try:
        with open(path or RATELIMITS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def simulate(msgs, now_dt, limits):
    bl = blocks(msgs)
    cur = bl[-1] if bl and now_dt < bl[-1]["start"] + timedelta(hours=5) else None
    week = sum(w for _, w in msgs)
    res = {"blocks": len(bl), "block_now_w": cur["w"] if cur else 0, "week_w": week,
           "block_now_start": cur["start"].isoformat(timespec="minutes") if cur else None,
           "calibrated": limits.get("calibrated"), "plans": {}}
    for plan in ("pro", "max5"):
        b, wk = limits[plan]["block"], limits[plan]["week"]
        res["plans"][plan] = {"avg": round(100 * sum(x["w"] for x in bl) / len(bl) / b, 1) if bl else 0,
                              "max": round(100 * max(x["w"] for x in bl) / b, 1) if bl else 0,
                              "now": round(100 * (cur["w"] if cur else 0) / b, 1),
                              "week": round(100 * week / wk, 1), "budget_block": b, "budget_week": wk}
    return res


def calibrate(limits, plan, block_w, week_w, block_pct=None, week_pct=None, now=None):
    """Budgets réels déduits des % affichés par /usage ; l'autre offre suit les multiplicateurs."""
    if plan not in ("pro", "max5"):
        raise ValueError("offre inconnue")
    new = json.loads(json.dumps(limits))
    for key, used, pct in (("block", block_w, block_pct), ("week", week_w, week_pct)):
        if pct is None:
            continue
        pct = float(pct)
        if not 0 < pct <= 100 or used <= 0:
            raise ValueError("pourcentage ou consommation invalide pour " + key)
        new[plan][key] = round(used * 100 / pct)
        other = "max5" if plan == "pro" else "pro"
        new[other][key] = round(new[plan][key] * MULT[key] if plan == "pro" else new[plan][key] / MULT[key])
    new["calibrated"] = {"date": (now or datetime.now().astimezone()).isoformat(timespec="minutes"), "plan": plan,
                         "block_pct": block_pct, "week_pct": week_pct}
    return new


def auto_calibrate(msgs, rl, limits, now):
    """Calibration automatique depuis les % réels de la statusline, sur les vraies fenêtres (fin - 5 h, fin - 7 j).
    Renvoie les nouveaux budgets, ou None si relevé périmé ou % trop faible (< 5 %, trop imprécis)."""
    if not rl or now - rl.get("at", 0) > ACTIVE:
        return None
    plan = (limits.get("calibrated") or {}).get("plan") or "max5"
    used = {}
    for key, lim, span in (("block", "five_hour", 5 * 3600), ("week", "seven_day", 7 * 86400)):
        l = rl.get(lim) or {}
        if (l.get("used_percentage") or 0) >= 5 and l.get("resets_at"):
            start = datetime.fromtimestamp(l["resets_at"] - span).astimezone()
            used[key] = (sum(w for dt, w in msgs if dt >= start), l["used_percentage"])
    used = {k: v for k, v in used.items() if v[0] > 0}
    if not used:
        return None
    new = calibrate(limits, plan, used.get("block", (0,))[0], used.get("week", (0,))[0],
                    used.get("block", (0, None))[1], used.get("week", (0, None))[1])
    new["calibrated"]["auto"] = True
    return new


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def collect(root=ROOT, now=None):
    now = now or time.time()
    files = glob.glob(os.path.join(root, "*", "*.jsonl")) + glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    states = []
    for p in files:
        try:
            if now - os.path.getmtime(p) > MAX_AGE:
                continue
            st, mt = load(p)
        except OSError:
            continue
        states.append((p, st, mt))
    for p in list(_cache):
        if p not in files:
            del _cache[p]

    nowdt = datetime.fromtimestamp(now).astimezone()
    today = nowdt.date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    cut5h = nowdt - timedelta(hours=5)
    z = lambda: [0, 0, 0, 0, 0]
    win = {"today": {}, "7d": {}, "5h": {}}
    per_day = {d: 0 for d in days}
    seen, sim_msgs, cut7d = set(), [], nowdt - timedelta(days=7)
    for _, st, _ in states:
        for mid, (ts, model, *tok) in st["usage"].items():
            if mid in seen:
                continue
            seen.add(mid)
            dt = parse_ts(ts)
            if not dt:
                continue
            if dt >= cut7d:
                sim_msgs.append((dt, weighted(tok)))
            day = dt.date().isoformat()
            keys = (["7d"] if day in per_day else []) + (["today"] if dt.date() == today else []) + (["5h"] if dt >= cut5h else [])
            for k in keys:
                acc = win[k].setdefault(model, z())
                for i in range(5):
                    acc[i] += tok[i]
            if day in per_day:
                per_day[day] += sum(tok[:4])  # tok[4] (cache 1h) est déjà compté dans tok[2]

    rl, limits = load_ratelimits(), load_limits()
    if rl and rl.get("at") != _auto.get("at"):  # une seule calibration par nouveau relevé
        _auto["at"] = rl.get("at")
        try:
            new = auto_calibrate(sim_msgs, rl, limits, now)
        except ValueError:
            new = None
        if new:
            tmp = LIMITS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new, f, indent=2, ensure_ascii=False)
            os.replace(tmp, LIMITS)
            limits = new

    pr = _prices or load_pricing()

    def fmt(w):
        return [{"model": m, "input": v[0], "output": v[1], "cache_write": v[2], "cache_read": v[3], "cost": cost(pr["models"], m, v)}
                for m, v in sorted(w.items(), key=lambda kv: -sum(kv[1][:4]))]

    actions, subagents = [], []
    for p, st, mt in states:
        sid = st["session"] or os.path.basename(p)[:-6]
        fresh = now - mt < ACTIVE
        if st["sub"] and "meta" not in st:
            try:
                with open(p[:-6] + ".meta.json") as f:
                    st["meta"] = json.load(f)
            except (OSError, ValueError):
                st["meta"] = {}
        who = ("sous-agent " + (st["agent"] or "")[:7]) if st["sub"] else "principal"
        name = (safe(st["meta"].get("description"), 40) or safe(st["meta"].get("agentType"), 40) or who) if st["sub"] else "principal"
        for t in st["tools"][-30:]:
            done = t["id"] in st["results"]
            actions.append({"id": t["id"], "ts": t["ts"], "session": sid[:8], "who": who, "name": name, "tool": t["name"], "label": t["label"],
                            "state": "ok" if done else ("en cours" if fresh else "sans résultat")})
        if st["sub"]:
            meta = st["meta"]
            done = st["handback"] or (not st["last_has_tool"] and st["tools"] and st["tools"][-1]["id"] in st["results"])
            state = "terminé" if done else ("en cours" if fresh else "inactif")
            subagents.append({"agent": (st["agent"] or "")[:7], "session": sid[:8], "type": safe(meta.get("agentType"), 40),
                              "description": safe(meta.get("description")), "state": state, "last": st["last_ts"],
                              "tools": len(st["tools"])})

    # Agents synchrones : le tool_result du parent (status != async_launched) confirme la fin.
    finished = {tid for _, st, _ in states for tid, s in st["spawn_status"].items() if s and s != "async_launched"}
    for _, st, _ in states:
        if st["sub"] and st["meta"].get("toolUseId") in finished:
            for sa in subagents:
                if sa["agent"] == (st["agent"] or "")[:7]:
                    sa["state"] = "terminé"

    actions.sort(key=lambda a: a["ts"] or "", reverse=True)
    subagents.sort(key=lambda a: a["last"] or "", reverse=True)
    return {"generated": nowdt.isoformat(timespec="seconds"), "files": len(states),
            "usage": {k: fmt(v) for k, v in win.items()}, "daily": [{"day": d, "total": per_day[d]} for d in days],
            "actions": actions[:30], "subagents": subagents[:20],
            "sim": simulate(sim_msgs, nowdt, limits),
            "rl": rl,
            "pricing": {"fetched_at": pr["fetched_at"], "source": pr["source"]}}


def diff(prev, cur):
    """Événements live : actions et sous-agents nouveaux ou dont l'état a changé."""
    old_a = {a["id"]: a["state"] for a in prev.get("actions", [])}
    old_s = {a["agent"]: a["state"] for a in prev.get("subagents", [])}
    ev = [{"kind": "action", **a} for a in reversed(cur["actions"]) if old_a.get(a["id"]) != a["state"]]
    ev += [{"kind": "agent", **a} for a in reversed(cur["subagents"]) if old_s.get(a["agent"]) != a["state"]]
    return ev


def price_refresher():
    """Thread daemon dédié : l'appel sortant (<= 2 x 10 s) ne bloque jamais le watcher."""
    global _prices
    while True:
        try:
            _prices = load_pricing(fetch=True)
        except Exception as e:
            print("pricing:", e, flush=True)
        time.sleep(3600)  # load_pricing ne refait l'appel que si le cache a plus de 24 h


def watcher(root=ROOT, interval=1.0):
    """Seul appelant de collect() : relit les JSONL modifiés (incrémental) chaque seconde et diffuse les changements."""
    global _snap
    while True:
        try:
            cur = collect(root)
            ev = diff(_snap, cur) if _snap else []
            if {**cur, "generated": 0} != {**_snap, "generated": 0}:
                ev.append({"kind": "full", "data": cur})  # toute la page se met à jour sans rechargement
            _snap = cur
            with _subs_lock:
                for q in _subs:
                    for e in ev:
                        q.put(e)
        except Exception as e:  # le watcher ne doit jamais mourir
            print("watcher:", e, flush=True)
        time.sleep(interval)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address):
        # Refus au niveau connexion de tout client hors boucle locale.
        try:
            return ipaddress.ip_address(client_address[0]).is_loopback
        except ValueError:
            return False


class H(BaseHTTPRequestHandler):
    timeout = 30  # une connexion inactive (préconnexion du navigateur) ne garde pas un thread indéfiniment
    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in HOSTS:
            self.send_error(403, "Host non autorise")
            return
        if self.path == "/api/live":
            return self.live()
        if self.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body, ctype = f.read(), "text/html; charset=utf-8"
        elif self.path == "/api/data":
            body, ctype = json.dumps(_snap).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        origin = self.headers.get("Origin")
        # Host connu + Origin local + JSON obligatoire (un formulaire d'un autre site ne peut pas l'envoyer sans pré-vol CORS).
        if host not in HOSTS or (origin and origin.split("://")[-1].split(":")[0].lower() not in HOSTS) \
                or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_error(403, "Requete refusee")
            return
        if self.path != "/api/plan":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256:
                raise ValueError("trop grand")
            plan = json.loads(self.rfile.read(n) or b"{}").get("plan")
            if plan not in ("pro", "max5"):
                raise ValueError("offre inconnue")
            lim = load_limits()
            lim["calibrated"] = {**(lim.get("calibrated") or {}), "plan": plan,
                                 "date": datetime.now().astimezone().isoformat(timespec="minutes")}
            tmp = LIMITS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(lim, f, indent=2, ensure_ascii=False)
            os.replace(tmp, LIMITS)
            _auto.clear()  # recalibre la nouvelle offre au prochain cycle du watcher
            code, body = 200, {"ok": True}
        except (ValueError, TypeError, AttributeError) as e:
            code, body = 400, {"ok": False, "error": str(e)}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def live(self):
        q = queue.Queue()
        with _subs_lock:
            if len(_subs) >= MAX_SSE:
                self.send_error(503, "Trop de connexions live")
                return
            _subs.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"retry: 3000\ndata: " + json.dumps({"kind": "full", "init": True, "data": _snap}).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = b"data: " + json.dumps(q.get(timeout=KEEPALIVE)).encode() + b"\n\n"
                except queue.Empty:  # keep-alive visible côté page (preuve que le flux vit)
                    msg = b'data: {"kind": "ping"}\n\n'
                self.wfile.write(msg)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _subs_lock:
                _subs.remove(q)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Claude monitor : http://claude.local{'' if PORT == 80 else ':%d' % PORT}  (127.0.0.1:{PORT})", flush=True)
    try:
        srv = Server(("127.0.0.1", PORT), H)
    except PermissionError:
        # macOS n'autorise un port < 1024 sans root que sur 0.0.0.0, pas sur 127.0.0.1 (voir README).
        raise SystemExit(f"Port {PORT} refusé sur 127.0.0.1 sans root. Lancez avec CLAUDE_MONITOR_PORT=8765 "
                         "et la redirection pf du README, ou en root.")
    _snap = collect()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=price_refresher, daemon=True).start()
    srv.serve_forever()
