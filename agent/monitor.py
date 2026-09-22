"""Monitor the exam period on its own: pick up risk-score alerts, triage them, investigate a budget.

The bank's model raises an alert when it scores a transaction at or above ALERT (0.90).
Most of those are legitimate, so the monitor triages every alert before spending an
investigation on it:

  1. one alert per card per 72 hours (later alerts join the first one's episode);
  2. skip cards and customers in the benchmark pack, so the 20 answers stay independent;
  3. rank by the graph-feature scorer, which is trained on closed-case outcomes and
     disagrees with the bank's score often;
  4. investigate the top of that list (likely fraud) plus the highest bank scores the
     scorer calls clean (likely false alarms), to show both the catch and the clearance.

Each picked alert becomes a case (MON-001, ...) run through the same agent, policy lint,
GraphRAG and graph write-back as the benchmark. Output goes to monitor/cases and
monitor/traces; monitor/triage.json lists every alert and what happened to it.

    MONITOR=1 USE_LLM=1 .venv/bin/python agent/monitor.py [budget]
"""
import json
import os
import sys
from datetime import timedelta

import duckdb

os.environ["MONITOR"] = "1"   # lets later monitor cases retrieve earlier ones from memory
import agent
from tools import DB

ALERT = 0.90
START, END = "2016-11-01", "2017-01-01"
ROOT = os.path.join(os.path.dirname(__file__), "..", "monitor")


def alerts():
    con = duckdb.connect(DB, read_only=True)
    bench_cards = {r[0] for r in con.execute("select card_id from cp").fetchall()}
    bench_custs = {r[0] for r in con.execute("select customer_id from cp").fetchall()}
    cur = con.execute("""select tid, ts, amt, channel, addr1, card_id, customer_id, rs, p_model from t
                         where ts>=? and ts<? and rs>=? order by ts""", (START, END, ALERT))
    cols = [x[0] for x in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    last, out = {}, []
    for r in rows:
        if r["card_id"] in bench_cards or r["customer_id"] in bench_custs:
            r["triage"] = "skipped: benchmark card or customer"
        elif r["card_id"] in last and r["ts"] - last[r["card_id"]] < timedelta(hours=72):
            r["triage"] = "merged: same card within 72 hours of an earlier alert"
        else:
            last[r["card_id"]] = r["ts"]
            r["triage"] = "queued"
        out.append(r)
    return out


def pick(queue, budget):
    q = [r for r in queue if r["triage"] == "queued"]
    n_hot = budget * 2 // 3
    hot = sorted(q, key=lambda r: -r["p_model"])[:n_hot]
    cold = sorted([r for r in q if r["p_model"] < 0.05 and r not in hot], key=lambda r: -r["rs"])[:budget - n_hot]
    return sorted(hot + cold, key=lambda r: r["ts"])   # memory grows in time order


def case_row(i, r):
    where = "online" if r["channel"] == "online" else f"in billing region {r['addr1']}"
    return {"case_id": f"MON-{i:03d}", "opened_at": r["ts"] + timedelta(hours=1), "trigger_type": "risk_score",
            "trigger_text": f"Monitor alert: real-time model scored transaction {r['tid']} (${r['amt']:,.2f}, {where}) at {r['rs']:.2f}. "
                            f"Picked up by the agent's own monitoring of the exam period.",
            "flagged_txn_id": r["tid"], "card_id": r["card_id"], "customer_id": r["customer_id"], "risk_score": round(r["rs"], 2)}


def main():
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    queue = alerts()
    picked = pick(queue, budget)
    agent.OUT = os.path.join(ROOT, "cases")
    agent.TRACE = os.path.join(ROOT, "traces")
    os.makedirs(ROOT, exist_ok=True)
    ids = {}
    for i, r in enumerate(picked, 1):
        case = case_row(i, r)
        ids[r["tid"]] = case["case_id"]
        a = agent.run_case(case)
        cz = a["case"]
        print(f"{case['case_id']} txn {r['tid']} rs={r['rs']:.2f} scorer={r['p_model']:.2f} -> {cz['verdict']:<10} p={cz['fraud_probability']:.2f} "
              f"{cz['pattern']:<28} final={[x['action'] for x in a['next_best_actions']['final']]}", flush=True)
    for r in queue:
        if r["tid"] in ids:
            r["triage"] = f"investigated as {ids[r['tid']]}"
        elif r["triage"] == "queued":
            r["triage"] = "queued: below the investigation budget"
    summary = {"alert_threshold": ALERT, "period": [START, END], "budget": budget, "alerts": len(queue),
               "by_triage": {k: sum(1 for r in queue if r["triage"].split(":")[0].split(" as ")[0] == k)
                             for k in ("skipped", "merged", "investigated", "queued")},
               "alerts_list": [{"txn_id": str(r["tid"]), "ts": str(r["ts"]), "card_id": r["card_id"], "amount": r["amt"],
                                "bank_score": round(r["rs"], 2), "scorer": round(r["p_model"], 3), "triage": r["triage"]} for r in queue]}
    with open(os.path.join(ROOT, "case_pack.json"), "w") as fh:
        json.dump([case_row(i, r) for i, r in enumerate(picked, 1)], fh, indent=2, default=str)
    with open(os.path.join(ROOT, "triage.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "alerts_list"}))


if __name__ == "__main__":
    main()
