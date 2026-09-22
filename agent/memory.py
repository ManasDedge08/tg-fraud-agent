"""Case memory: closed cases plus every case this agent writes.

Reads go through the same tool layer as the rest of the investigation. Writes go to
TigerGraph (Case vertex + HAS_EVIDENCE/AFFECTS/ON_CARD/SIMILAR_TO edges) when TG_HOST is
set, and always to a local mirror so later cases in the same run can retrieve them.
"""
import json
import os
from datetime import timedelta

import duckdb

import tg

MEM = os.path.join(os.path.dirname(__file__), "..", "data", "memory.duckdb")


def _mem():
    con = duckdb.connect(MEM)
    con.execute("""create table if not exists agent_case (graph_case_id varchar primary key, case_id varchar, card_id varchar,
                   customer_id varchar, opened_at timestamp, verdict varchar, pattern varchar, fraud_probability double,
                   exposure_usd double, device varchar, affected varchar, connected varchar, summary varchar)""")
    return con


def device_ring(tl, f, card):
    """Is this device profile one that confirmed-fraud closed cases already tie to several cards?"""
    dev = f["dev"]
    before = tl._q("device_ring.closed_cases",
                   """select distinct cc.case_id, cc.card_id from cc
                      join (select unnest(string_split(txn_ids::varchar,'|'))::bigint tid, case_id from cc) x on x.case_id=cc.case_id
                      join t on t.tid=x.tid
                      where t.dev=? and cc.pattern='undocumented' and cc.opened_at<?""", (dev, tl.as_of))
    cards_before = tl._q("device_ring.cards_before",
                         "select count(distinct card_id) n from t where dev=? and ts<?", (dev, tl.as_of - timedelta(days=45)))[0]["n"]
    now = tl._q("device_ring.cards_now",
                """select card_id, min(ts) f from t where dev=? and ts<=? and ts>=? and id_15='New'
                   group by 1 order by f""", (dev, tl.as_of, tl.as_of - timedelta(days=30)))
    others = [r["card_id"] for r in now if r["card_id"] != card]
    own = tl._q("device_ring.own_txns", "select tid, ts, amt from t where dev=? and card_id=? and ts<=? order by ts",
                (dev, card, tl.as_of))
    hit = len(before) >= 2 and len(others) >= 2 and f["id_23"] in ("IP_PROXY:ANONYMOUS", "IP_PROXY:HIDDEN")
    return {"hit": hit, "cases": [r["case_id"] for r in before], "cards_before": cards_before,
            "cards_now": others, "first_now": now[0]["f"] if now else None, "own_txns": own}


def structuring_cases(tl):
    rows = tl._q("similar_closed_cases.text('just under $500')",
                 "select case_id from cc where analyst_notes like '%just under $500%' and opened_at<? order by opened_at", (tl.as_of,))
    return [r["case_id"] for r in rows]


def episode(tl, card, f):
    """Other transactions in the same fraud episode: same client uid on this card, scored
    suspicious, within 7 days before the flagged one and before the case opened."""
    return tl._q("episode(uid)",
                 """select tid, ts, amt, p_model from t where card_id=? and uid=? and p_model>=0.30
                    and ts between ? and ? order by ts""",
                 (card, f["uid"], f["ts"] - timedelta(days=7), min(f["ts"] + timedelta(hours=48), tl.as_of)))


def similar_by_pattern(tl, pattern):
    """Closed cases with the same confirmed pattern, most recent first."""
    if pattern in ("none", "undocumented"):
        return []
    rows = tl._q(f"similar_closed_cases.pattern({pattern})",
                 "select case_id from cc where pattern=? and outcome='confirmed_fraud' and opened_at<? order by opened_at desc limit 2",
                 (pattern, tl.as_of))
    return [r["case_id"] for r in rows]


def agent_cases_like(tl, card, dev):
    """Cases this agent wrote earlier in the run that share the card or the device profile."""
    con = _mem()
    # benchmark answers stay reproducible from the benchmark alone; the monitor's own
    # cases (MON-*) are memory only for later monitor investigations
    own = "" if os.environ.get("MONITOR") == "1" else " and case_id not like 'MON-%'"
    rows = con.execute("select graph_case_id, case_id, verdict, pattern from agent_case where opened_at<? and (card_id=? or device=?)" + own,
                       (tl.as_of, card, dev)).fetchall()
    con.close()
    tl.calls.append("agent_case_memory")
    return rows


def write_case(inv, ans):
    c = inv.c
    gid = f"CASE-{c['opened_at'].year}-{c['case_id'].removeprefix('HHG-')}"   # MON-007 -> CASE-2016-MON-007
    con = _mem()
    con.execute("insert or replace into agent_case values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, c["case_id"], c["card_id"], c["customer_id"], c["opened_at"], ans["case"]["verdict"],
                 ans["case"]["pattern"], ans["case"]["fraud_probability"], ans["case"]["exposure_usd"], inv.f["dev"],
                 json.dumps(ans["case"]["affected_txn_ids"]), json.dumps(ans["case"]["connected_card_ids"]), ans["case"]["summary"]))
    con.close()
    ok = tg.upsert_case(gid, c, ans) if tg.enabled() else False
    return gid if ok else ""


def shared_device_burst(tl, f, card, hours=24):
    """R6: the flagged device profile used on other cards within a day, where those
    other transactions also score as suspicious."""
    if not f["dev"] or f["dev"].count("?") >= 3:
        return {"hit": False}
    rows = tl._q(f"device_neighbors(device, hours={hours})",
                 """select card_id, tid, ts, amt, p_model from t where dev=? and card_id<>? and ts between ? and ?
                    order by ts""", (f["dev"], card, f["ts"] - timedelta(hours=hours), min(f["ts"] + timedelta(hours=hours), tl.as_of)))
    cards = sorted({r["card_id"] for r in rows})
    sus = [r for r in rows if r["p_model"] >= 0.5]
    hit = 2 <= len(cards) <= 10 and len({r["card_id"] for r in sus}) >= 2
    return {"hit": hit, "cards": cards, "rows": rows}
