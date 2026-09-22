"""Bundle cases/ and traces/ into ui/data.js so the dashboard opens from disk with no server.

Also attaches each case's graph neighbourhood for the dashboard's graph panel: the card,
the flagged transaction, its device profile, billing region and purchaser email domain,
other cards that used the same device in the 30 days before the case opened, and the
closed cases on the card. Read from the local mirror of the graph (data/hh.duckdb), with
the same as_of cut-off the agent uses.
"""
import glob
import json
import os

import duckdb

ROOT = os.path.join(os.path.dirname(__file__), "..")
con = duckdb.connect(os.path.join(ROOT, "data", "hh.duckdb"), read_only=True)
pack = {r[0]: r for r in con.execute("select case_id, opened_at, flagged_txn_id, card_id from cp").fetchall()}
MON = os.path.join(ROOT, "monitor")
if os.path.exists(os.path.join(MON, "case_pack.json")):   # cases the agent opened from its own monitoring
    from datetime import datetime
    for r in json.load(open(os.path.join(MON, "case_pack.json"))):
        pack[r["case_id"]] = (r["case_id"], datetime.fromisoformat(r["opened_at"]), r["flagged_txn_id"], r["card_id"])


def neighbourhood(case_id, answer):
    _, as_of, tid, card = pack[case_id]
    f = con.execute("select tid, ts, amt, channel, pcd, addr1, pe, dev, id_15, id_23 from t where tid=?", (tid,)).fetchone()
    n = {"card": card, "txn": {"id": str(f[0]), "amt": f[2], "channel": f[3], "product": f[4]},
         "region": None if f[5] is None else str(int(f[5])), "email": f[6], "device": f[7],
         "device_new": f[8] == "New", "proxy": f[9]}
    n["region_cards"] = 0
    if f[5] is not None:
        n["region_cards"] = con.execute("""select count(distinct card_id) from t where addr1=? and card_id<>?
                                           and ts between ? - interval 2 day and ?""", (f[5], card, f[1], as_of)).fetchone()[0]
    n["device_cards"] = []
    if f[7]:
        rows = con.execute("""select card_id, count(*) n, max(p_model) p from t where dev=? and card_id<>?
                              and ts between ? - interval 30 day and ? group by 1 order by p desc, n desc""",
                           (f[7], card, as_of, as_of)).fetchall()
        conn = set(answer["case"]["connected_card_ids"])
        n["device_cards_total"] = len(rows)
        picked = [r for r in rows if r[0] in conn][:10]
        picked += [r for r in rows if r[0] not in conn][:max(0, 10 - len(picked))]
        n["device_cards"] = [{"id": r[0], "n": r[1], "p": round(r[2], 2), "connected": r[0] in conn} for r in picked]
    cc = con.execute("""select case_id, outcome, pattern from cc where card_id=? and opened_at<? order by opened_at desc limit 4""",
                     (card, as_of)).fetchall()
    n["closed_cases"] = [{"id": r[0], "outcome": r[1], "pattern": r[2]} for r in cc]
    n["closed_total"] = con.execute("select count(*) from cc where card_id=? and opened_at<?", (card, as_of)).fetchone()[0]
    return n


out = []
files = [(p, os.path.join(ROOT, "traces")) for p in sorted(glob.glob(os.path.join(ROOT, "cases", "HHG-*.json")))]
files += [(p, os.path.join(MON, "traces")) for p in sorted(glob.glob(os.path.join(MON, "cases", "MON-*.json")))]
for p, traces in files:
    a = json.load(open(p))
    a["_monitor"] = a["case_id"].startswith("MON-")
    tp = os.path.join(traces, os.path.basename(p))
    a["_trace"] = json.load(open(tp)) if os.path.exists(tp) else {}
    a["_graph"] = neighbourhood(a["case_id"], a)
    out.append(a)
with open(os.path.join(os.path.dirname(__file__), "data.js"), "w") as fh:
    fh.write("window.CASES = " + json.dumps(out, default=str) + ";\n")
    tri = os.path.join(MON, "triage.json")
    if os.path.exists(tri):
        t = json.load(open(tri))
        fh.write("window.TRIAGE = " + json.dumps({k: v for k, v in t.items() if k != "alerts_list"}) + ";\n")
print(f"bundled {len(out)} cases")
