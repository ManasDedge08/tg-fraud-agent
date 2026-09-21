"""Pattern detectors. Each returns a dict with `hit` plus the entity IDs it rests on,
so every finding can become an evidence item with a real ref."""
from datetime import timedelta


def _online(w):
    return w["channel"] == "online"


def card_testing(tools, card_id, f):
    """R5: 3+ small online authorizations within an hour, followed by a larger purchase."""
    win = tools.card_window(card_id, f["ts"], hours_before=24, hours_after=24)
    small = [w for w in win if _online(w) and w["amt"] < 5]
    for i, s in enumerate(small):
        burst = [x for x in small if s["ts"] <= x["ts"] <= s["ts"] + timedelta(hours=1)]
        if len(burst) >= 3:
            end = burst[-1]["ts"]
            big = [w for w in win if w["ts"] > end and w["ts"] <= end + timedelta(hours=6) and w["amt"] >= 20]
            if big:
                return {"hit": True, "small": burst, "big": big[:3]}
    return {"hit": False}


def structuring(tools, card_id, f):
    """Undocumented (closed cases CC-3748 etc.): several online purchases each just under $500 within an hour."""
    win = tools.card_window(card_id, f["ts"], hours_before=24, hours_after=24)
    near = [w for w in win if _online(w) and 400 <= w["amt"] < 500]
    for s in near:
        grp = [x for x in near if s["ts"] <= x["ts"] <= s["ts"] + timedelta(minutes=60)]
        if len(grp) >= 3:
            return {"hit": True, "txns": grp}
    return {"hit": False}


def burst(tools, card_id, f, base):
    """Pattern 2: 2-4 online purchases within 48h that don't fit the card's history."""
    win = tools.card_window(card_id, f["ts"], hours_before=48, hours_after=0)
    p95 = base.get("p95_amt") or 0
    odd = [w for w in win if _online(w) and (w["amt"] > max(p95, 1) or base["products"].get(w["pcd"], 0) <= 2)]
    return {"hit": 2 <= len(odd) <= 6, "txns": odd}


def device_novelty(tools, card_id, f):
    if not f["dev"]:
        return {"online": False}
    prior = tools._q("device_on_card_before",
                     "select count(*) n from t where card_id=? and dev=? and ts<?", (card_id, f["dev"], f["ts"]))[0]["n"]
    nb = tools.device_neighbors(f["dev"], f["ts"], days=30)
    others = [x for x in nb if x["card_id"] != card_id]
    glob = tools.device_global(f["dev"])
    return {"online": True, "new_flag": f["id_15"] == "New", "seen_on_card_before": prior,
            "proxy": f["id_23"], "neighbours_30d": others, "global_cards": glob["cards"]}


def region_novelty(tools, card_id, f):
    """Pattern 4: card-present use in a region new to the card; is home activity continuing?"""
    if f["addr1"] is None:
        return {"known": False}
    prior = tools._q("region_on_card_before",
                     "select count(*) n from t where card_id=? and addr1=? and ts<?",
                     (card_id, f["addr1"], f["ts"] - timedelta(hours=72)))[0]["n"]
    win = tools.card_window(card_id, f["ts"], hours_before=72, hours_after=24)
    in_new = [w for w in win if w["addr1"] == f["addr1"]]
    days_in_new = len({w["ts"].date() for w in in_new})
    return {"known": True, "prior_in_region": prior, "in_new": in_new, "days_in_new": days_in_new,
            "other_regions_same_window": sorted({w["addr1"] for w in win if w["addr1"] not in (None, f["addr1"])})}


def recurring(tools, card_id, f):
    """R7: same amount (within 2%), same product code, roughly monthly before the flagged charge."""
    rows = tools._q("recurring_charge",
                    """select tid, ts, amt, pcd from t where card_id=? and pcd=? and ts<? and ts>=?
                       and abs(amt-?)<=0.02*? order by ts""",
                    (card_id, f["pcd"], f["ts"], f["ts"] - timedelta(days=150), f["amt"], f["amt"]))
    gaps = [(b["ts"] - a["ts"]).days for a, b in zip(rows, rows[1:] + [f])]
    monthly = [g for g in gaps if 25 <= g <= 35]
    return {"hit": len(monthly) >= 2, "txns": rows, "gaps": gaps}


def match_flag_anomaly(f):
    bad = [k for k in ("M4", "M5", "M6") if f.get(k) in ("M2", False)]
    return bad
