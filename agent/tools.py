"""Investigation tools. Each function mirrors one GSQL query installed in TigerGraph
(see gsql/queries.gsql). The local backend runs the same logic on DuckDB so the agent
can run offline; every call is counted so the answer file can report tool_calls.

All queries only look at data at or before the case's opened_at: no peeking forward.
"""
import os
from datetime import timedelta

import duckdb

DB = os.path.join(os.path.dirname(__file__), "..", "data", "hh.duckdb")


class Tools:
    def __init__(self, as_of):
        self.con = duckdb.connect(DB, read_only=True)
        self.as_of = as_of
        self.calls = []

    def _q(self, name, sql, params=()):
        self.calls.append(name)
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # --- transaction and card ------------------------------------------------
    def txn(self, tid):
        r = self._q(f"txn({tid})", "select * from t where tid=?", (tid,))
        return r[0] if r else None

    def card_history(self, card_id, days=120):
        """Baseline of the card before the case: amounts, products, regions, devices."""
        lo = self.as_of - timedelta(days=days)
        base = self._q(
            f"card_history(card={card_id},days={days})",
            """select count(*) n, median(amt) med_amt, quantile_cont(amt,0.95) p95_amt, max(amt) max_amt,
                      min(ts) first_ts, max(ts) last_ts,
                      count(distinct addr1) n_regions, mode(addr1) home_region,
                      count(distinct dev) n_devices
               from t where card_id=? and ts<? and ts>=?""",
            (card_id, self.as_of, lo),
        )[0]
        base["products"] = {r["pcd"]: r["n"] for r in self._q(
            "card_history.products",
            "select pcd, count(*) n from t where card_id=? and ts<? and ts>=? group by 1 order by 2 desc",
            (card_id, self.as_of, lo))}
        base["regions"] = {str(r["addr1"]): r["n"] for r in self._q(
            "card_history.regions",
            "select addr1, count(*) n from t where card_id=? and ts<? and ts>=? group by 1 order by 2 desc limit 8",
            (card_id, self.as_of, lo))}
        return base

    def card_window(self, card_id, center, hours_before=72, hours_after=48):
        """Every transaction on the card around the flagged one (capped at opened_at)."""
        lo = center - timedelta(hours=hours_before)
        hi = min(center + timedelta(hours=hours_after), self.as_of)
        return self._q(
            f"card_window(card={card_id})",
            """select tid, ts, amt, pcd, channel, addr1, addr2, dist1, pe, re, rs, dev, id_15, id_23,
                      M4, M5, M6
               from t where card_id=? and ts between ? and ? order by ts""",
            (card_id, lo, hi),
        )

    def customer_cards(self, customer_id):
        return self._q(
            f"customer_cards({customer_id})",
            "select card_id, count(*) n, min(ts) first_ts, max(ts) last_ts from t where customer_id=? and ts<=? group by 1 order by 1",
            (customer_id, self.as_of),
        )

    # --- shared-origin traversals --------------------------------------------
    def device_neighbors(self, dev, center, days=30):
        """Other cards that used the same device profile in the window."""
        lo, hi = center - timedelta(days=days), self.as_of
        return self._q(
            "device_neighbors",
            """select card_id, count(*) n, min(ts) first_ts, max(ts) last_ts, sum(amt) total,
                      avg(rs) avg_rs, list(tid order by ts) tids
               from t where dev=? and ts between ? and ? group by 1 order by first_ts""",
            (dev, lo, hi),
        )

    def device_global(self, dev):
        return self._q("device_global",
                       "select count(distinct card_id) cards, count(*) n, min(ts) f, max(ts) l from t where dev=? and ts<=?",
                       (dev, self.as_of))[0]

    def region_activity(self, addr1, center, hours=48):
        """Cards transacting in a billing region in the window, and how new the region is to each."""
        lo, hi = center - timedelta(hours=hours), min(center + timedelta(hours=hours), self.as_of)
        return self._q(
            "region_cluster",
            """with w as (select * from t where addr1=? and ts between ? and ?)
               select w.card_id, count(*) n, sum(amt) total, list(tid order by ts) tids,
                      (select count(*) from t h where h.card_id=w.card_id and h.addr1=? and h.ts<?) prior_in_region
               from w group by w.card_id order by n desc""",
            (addr1, lo, hi, addr1, lo),
        )

    def email_neighbors(self, domain, field, center, hours=48):
        col = "re" if field == "R" else "pe"
        lo, hi = center - timedelta(hours=hours), min(center + timedelta(hours=hours), self.as_of)
        return self._q(
            f"email_neighbors({field})",
            f"select card_id, count(*) n, sum(amt) total from t where {col}=? and ts between ? and ? group by 1 order by n desc limit 25",
            (domain, lo, hi),
        )

    # --- case memory -----------------------------------------------------------
    def prior_cases_for(self, customer_id=None, card_ids=(), tids=()):
        """Closed cases touching this customer, these cards, or these transactions."""
        rows = self._q(
            "similar_closed_cases.structural",
            """select case_id, card_id, opened_at, outcome, pattern, exposure_usd, connected_card_ids, report_filed, analyst_notes, txn_ids
               from cc where opened_at < ? and (customer_id=? or list_has_any(?, [card_id]) or
                 list_has_any(string_split(coalesce(connected_card_ids,''),'|'), ?) or
                 list_has_any(string_split(txn_ids::varchar,'|'), ?))
               order by opened_at desc limit 12""",
            (self.as_of, customer_id, list(card_ids), list(card_ids), [str(t) for t in tids]),
        )
        return rows

    def cases_by_pattern(self, pattern, limit=5):
        return self._q(
            f"similar_closed_cases.pattern({pattern})",
            "select case_id, outcome, pattern, exposure_usd, analyst_notes from cc where pattern=? order by random() limit ?",
            (pattern, limit),
        )
