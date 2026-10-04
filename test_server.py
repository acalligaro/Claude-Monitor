"""Test minimal sur un faux JSONL : python3 test_server.py"""
import json, os, tempfile, time
from datetime import datetime, timezone
import server

now = time.time()
ts = datetime.fromtimestamp(now - 60, timezone.utc).isoformat().replace("+00:00", "Z")
u = {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}
base = {"sessionId": "sess-1234-abcd", "cwd": "/tmp/projet", "timestamp": ts, "isSidechain": False}
lines = [
    # même message.id sur deux lignes (un bloc chacun) : l'usage ne doit compter qu'une fois
    {**base, "type": "assistant", "message": {"id": "m1", "model": "claude-test", "usage": u,
        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo SECRET", "description": "Lister"}}]}},
    {**base, "type": "assistant", "message": {"id": "m1", "model": "claude-test", "usage": u,
        "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/a/b/notes.txt"}}]}},
    {**base, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET"}]}},
    {**base, "type": "assistant", "message": {"id": "m2", "model": "claude-test", "usage": u, "content": []}},
]
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "proj"))
    with open(os.path.join(root, "proj", "sess.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(l) for l in lines) + "\n")
    d = server.collect(root, now)

r = d["usage"]["today"][0]
assert (r["model"], r["input"], r["output"], r["cache_write"], r["cache_read"]) == ("claude-test", 20, 40, 60, 80), r
assert d["usage"]["5h"][0]["output"] == 40 and d["daily"][-1]["total"] == 200
acts = {a["tool"]: a for a in d["actions"]}
assert acts["Bash"]["state"] == "ok" and acts["Bash"]["label"] == "Lister"
assert acts["Read"]["state"] == "en cours" and acts["Read"]["label"] == "notes.txt"
assert "SECRET" not in json.dumps(d)
assert "sessions" not in d
assert server.safe("token=abc") == "[masqué]" and len(server.safe("x" * 31 + " y" * 200)) == 120
print("OK")

# Lecture incrémentale : seuls les octets ajoutés sont lus, une ligne partielle attend sa fin.
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "proj"))
    p = os.path.join(root, "proj", "s.jsonl")
    enc = lambda l: (json.dumps(l) + "\n").encode()
    with open(p, "wb") as f:
        f.write(enc(lines[0]))
    d1 = server.collect(root, now)
    off1 = server._cache[p]["off"]
    assert off1 == os.path.getsize(p) and len(d1["actions"]) == 1 and d1["actions"][0]["state"] == "en cours"
    partial = enc(lines[2])
    with open(p, "ab") as f:
        f.write(partial[:15])                      # ligne en cours d'écriture
    os.utime(p, (now + 1, now + 1))
    server.collect(root, now)
    assert server._cache[p]["off"] == off1        # partielle ignorée, offset inchangé
    with open(p, "ab") as f:
        f.write(partial[15:])
    os.utime(p, (now + 2, now + 2))
    d2 = server.collect(root, now)
    assert server._cache[p]["off"] == os.path.getsize(p)
    assert d2["actions"][0]["state"] == "ok"      # ⏳ -> ✓ grâce au tool_result ajouté
    ev = server.diff(d1, d2)
    assert [(e["kind"], e["id"], e["state"]) for e in ev] == [("action", "t1", "ok")], ev
    assert server.diff(d2, d2) == []
    with open(p, "wb") as f:                       # fichier tronqué/réécrit : relecture complète
        f.write(enc(lines[1]))
    os.utime(p, (now + 3, now + 3))
    d3 = server.collect(root, now)
    assert [a["tool"] for a in d3["actions"]] == ["Read"]
print("OK incrémental")

# Simulation Pro / Max 5x : pondération, blocs de 5 h, calibration.
from datetime import timedelta
assert server.weighted((10, 20, 30, 40)) == 10 + 100 + 37.5 + 4
t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
msgs = [(t0, 100), (t0 + timedelta(hours=4, minutes=59), 50),   # bloc 1 (150)
        (t0 + timedelta(hours=5), 10),                          # pile 5 h après le début : bloc 2
        (t0 + timedelta(hours=20), 40)]                         # après inactivité : bloc 3
bl = server.blocks(msgs)
assert [b["w"] for b in bl] == [150, 10, 40] and bl[1]["start"] == t0 + timedelta(hours=5)
lim = {"pro": {"block": 100, "week": 1000}, "max5": {"block": 500, "week": 3500}, "calibrated": None}
sim = server.simulate(msgs, t0 + timedelta(hours=21), lim)
p = sim["plans"]["pro"]
assert (p["avg"], p["max"], p["now"], p["week"]) == (66.7, 150.0, 40.0, 20.0), p
assert sim["plans"]["max5"]["week"] == round(100 * 200 / 3500, 1)
assert server.simulate(msgs, t0 + timedelta(hours=30), lim)["plans"]["pro"]["now"] == 0   # plus de bloc en cours
# /usage indique 20 % de session et 50 % de semaine sur Max 5x
new = server.calibrate(lim, "max5", block_w=40, week_w=250, block_pct=20, week_pct=50)
assert new["max5"] == {"block": 200, "week": 500} and new["pro"] == {"block": 40, "week": round(500 / 3.5)}
assert new["calibrated"]["plan"] == "max5" and lim["max5"]["block"] == 500   # l'original n'est pas modifié
only_week = server.calibrate(lim, "pro", 40, 250, None, 25)
assert only_week["pro"] == {"block": 100, "week": 1000} and only_week["max5"]["week"] == 3500
for bad in (("free", 40, 250, 20, None), ("pro", 40, 250, 0, None), ("pro", 0, 250, 20, None)):
    try:
        server.calibrate(lim, *bad)
        raise AssertionError(bad)
    except ValueError:
        pass
print("OK simulation")

# Tarifs : parser sur un extrait figé de la page + coût sur un cas connu (aucun appel réseau).
md = """| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :---- | :---- | :---- | :---- | :---- | :---- |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Opus 4.1 ([retired](https://x)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Haiku 3.5 | $0.80 / MTok | $1 / MTok | $1.60 / MTok | $0.08 / MTok | $4 / MTok |
| **Billing unit** | Claude Consumption Unit (CCU) |"""
pr = server.parse_pricing(md)
assert pr == {"opus-5.5": [4, 5, 8, 0.2, 20], "opus-4.1": [15, 18.75, 30, 1.5, 75], "haiku-3.5": [0.8, 1, 1.6, 0.08, 4]}, pr
assert [server.model_key(m) for m in ("claude-opus-5-5", "claude-opus-4-1-20250805", "claude-3-5-haiku-20241022", "claude-fable-5-1[1m]")] \
    == ["opus-5.5", "opus-4.1", "haiku-3.5", "fable-5.1"]
# 1 M entrée, 1 M sortie, 2 M cache créé dont 1 M en 1 h, 10 M cache lu : 4 + 20 + 5 + 8 + 2 = 39 $
assert server.cost(pr, "claude-opus-5-5", [1e6, 1e6, 2e6, 10e6, 1e6]) == 39
assert server.cost(pr, "claude-test", [1, 1, 1, 1, 0]) is None   # inconnu : pas de prix inventé
print("OK tarifs")

# Calibration automatique depuis la statusline : vraie fenêtre, seuil 5 %, relevé périmé ignoré.
from datetime import datetime as _dt
_now = 1_000_000_000
_m = [(_dt.fromtimestamp(_now - 3600).astimezone(), 30.0), (_dt.fromtimestamp(_now - 6 * 3600).astimezone(), 70.0)]
_rl = {"at": _now, "five_hour": {"used_percentage": 30, "resets_at": _now + 4 * 3600},
       "seven_day": {"used_percentage": 50, "resets_at": _now + 86400}}
_a = server.auto_calibrate(_m, _rl, lim, _now)
# lim n'a pas d'offre calibrée : max5 par défaut. 30 pondérés = 30 % du bloc, 100 sur la vraie semaine = 50 %.
assert _a["max5"] == {"block": 100, "week": 200} and _a["pro"] == {"block": 20, "week": 57} and _a["calibrated"]["auto"], _a
assert server.auto_calibrate(_m, {**_rl, "at": _now - 3600}, lim, _now) is None
assert server.auto_calibrate(_m, {"at": _now, "five_hour": {"used_percentage": 2, "resets_at": _now}}, lim, _now) is None
print("OK calibration auto")
