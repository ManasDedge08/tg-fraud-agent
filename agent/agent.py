"""Fraud investigation agent.

Loop per case: trigger -> open case -> investigate (graph tools) -> assess (competing
hypotheses, calibrated probability) -> stop, or request evidence (simulated reply) ->
re-assess -> recommend (policy engine) -> explain (LLM) -> write case to graph.

The LLM writes the summary and the SAR narrative from the structured evidence. It does
not choose actions, routes or probabilities; those come from the scorer and policy.py.
"""
import json
import math
import os
import sys
import time
from datetime import timedelta

import duckdb

import explain
import graphrag
import memory
import policy
import signals as S
import tg
from tools import DB, Tools

OUT = os.path.join(os.path.dirname(__file__), "..", "cases")
TRACE = os.path.join(os.path.dirname(__file__), "..", "traces")

PROXY_RISK = {"IP_PROXY:ANONYMOUS", "IP_PROXY:HIDDEN"}


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def money(x):
    return f"${x:,.2f}"


def d(ts):
    return ts.strftime("%Y-%m-%d")


class Investigation:
    def __init__(self, case):
        self.c = case
        self.tl = Tools(case["opened_at"])
        self.steps = []          # the case timeline shown in the UI
        self.evidence = []
        self.hyp = {}            # hypothesis -> log-odds contribution
        self.amounts = {}        # affected txn id -> amount
        self.connected_cards = []
        self.connected_devices = []
        self.similar = []
        self.pattern = "none"
        self.pattern_description = ""
        self.shared_origin = False
        self.coordinated = False
        self.signals = 0         # independent evidence count

    # --- helpers -------------------------------------------------------------
    def step(self, kind, text, **data):
        self.steps.append({"step": len(self.steps) + 1, "kind": kind, "text": text, "tool_calls": len(self.tl.calls), **data})
        if os.environ.get("LIVE") == "1":
            print(f"  {len(self.steps):>2}. [{kind:<15}] {text[:180]}", flush=True)

    def ev(self, claim, source, ref, ids, weight=0.0, hyp=None):
        self.evidence.append({"claim": claim, "source": source, "ref": ref, "entity_ids": [str(i) for i in ids]})
        if weight:
            self.hyp[hyp or claim[:40]] = weight
            self.signals += 1

    def affect(self, rows):
        for r in rows:
            if r["ts"] <= self.c["opened_at"]:
                self.amounts[str(r["tid"])] = round(abs(r["amt"]), 2)

    # --- the investigation ---------------------------------------------------
    def run(self):
        c, tl = self.c, self.tl
        t0 = time.time()
        f = tl.txn(c["flagged_txn_id"])
        self.f = f
        card = c["card_id"]
        self.step("trigger", f"{c['trigger_type']}: {c['trigger_text']}")
        self.step("open_case", f"Case opened for card {card}, flagged transaction {f['tid']} ({money(f['amt'])}, {f['channel']}, product {f['pcd']}).")

        base = tl.card_history(card)
        self.base = base
        ref_hist = f"query:card_history(card_id={card}, days=120)"
        self.step("investigate", f"Baseline: {base['n']} transactions in 120 days, median {money(base['med_amt'] or 0)}, home region {base['home_region']}, products {base['products']}.")

        # prior: the calibrated scorer trained on closed-case outcomes
        pm = f["p_model"]
        self.p_model = pm
        self.hyp["scorer"] = logit(pm)
        self.signals += 1
        self.ev(f"Graph-feature scorer trained on 5,565 closed cases gives this transaction a calibrated fraud probability of {pm:.2f} (bank model score {f['rs']:.2f}); features include Vesta's unnamed C, D, M and V columns, used as signals only",
                "graph", "query:score_transaction(txn_id=%s)" % f["tid"], [f["tid"]])
        if c["trigger_type"] == "customer_report":
            self.hyp["customer_report"] = 0.8
            self.ev(f"Cardholder {c['customer_id']} reported the {money(f['amt'])} charge as not made by them", "customer",
                    f"trigger:{c['case_id']}", [c["customer_id"], f["tid"]])

        # pattern detectors
        ct = S.card_testing(tl, card, f)
        st = S.structuring(tl, card, f)
        dv = S.device_novelty(tl, card, f)
        rg = S.region_novelty(tl, card, f)
        rc = S.recurring(tl, card, f)
        mf = S.match_flag_anomaly(f)
        self.step("investigate", "Ran pattern queries: card_window, device_neighbors, region_cluster, recurring_charge.")

        ring = memory.device_ring(tl, f, card) if dv["online"] else None

        if ring and ring["hit"]:
            self.pattern = "undocumented"
            self.coordinated = self.shared_origin = True
            self.hyp["device_ring"] = 4.5
            self.connected_cards = ring["cards_now"]
            self.connected_devices = [f["dev"]]
            self.similar += ring["cases"]
            self.affect(ring["own_txns"])
            self.ev(f"Device profile '{f['dev']}' behind {f['id_23']} is marked New on this card and on {len(ring['cards_now'])} other cards since {d(ring['first_now'])}; each card shows 1-3 online purchases of $35-$250 from it and nothing else from that device",
                    "graph", f"query:device_neighbors(device='{f['dev']}', days=30)", [card] + ring["cards_now"][:25], 0)
            self.ev(f"The same device profile and proxy appear in {len(ring['cases'])} closed cases confirmed as fraud in Aug-Sep 2016 across {ring['cards_before']} cards, which analysts could not match to a documented pattern",
                    "graph", "query:similar_closed_cases(device_profile)", ring["cases"], 4.5, "device_ring")
            self.pattern_description = (
                f"A single device profile ({f['dev']}), always behind an anonymous proxy and always marked New, is used to make one to three "
                f"mid-sized online purchases on many unrelated customers' cards, then moves on. It hit {ring['cards_before']} cards in Aug-Sep 2016 "
                f"(closed cases {', '.join(ring['cases'][:4])}) and {len(ring['cards_now']) + 1} cards since {d(ring['first_now'])}. "
                "Found by traversing Transaction-FROM_DEVICE-DeviceProfile to other cards and matching the profile against closed-case memory.")
        sd = memory.shared_device_burst(tl, f, card) if not (ring and ring["hit"]) else {"hit": False}
        if sd["hit"]:
            self.shared_origin = True
            self.hyp["shared_device"] = 2.5
            self.connected_cards = sd["cards"]
            self.connected_devices = [f["dev"]]
            self.ev(f"Device profile '{f['dev']}' was used on {len(sd['cards'])} other cards within 24 hours ({', '.join(money(r['amt']) for r in sd['rows'])}), and the scorer rates those transactions as suspicious",
                    "graph", f"query:device_neighbors(device='{f['dev']}', hours=24)", sd["cards"] + [str(r["tid"]) for r in sd["rows"]], 2.5, "shared_device")
        if st["hit"]:
            self.pattern = "undocumented"
            self.coordinated = True
            self.hyp["structuring"] = 3.5
            self.affect(st["txns"])
            span = (st["txns"][-1]["ts"] - st["txns"][0]["ts"]).seconds // 60
            devs = sorted({w["dev"] for w in st["txns"] if w["dev"]})
            self.connected_devices = devs
            sim = memory.structuring_cases(tl)
            self.similar += sim
            self.ev(f"{len(st['txns'])} online purchases of {', '.join(money(w['amt']) for w in st['txns'])} within {span} minutes, each just under $500, from {len(devs)} device profiles marked New",
                    "graph", f"query:card_window(card_id={card}, hours=24)", [w["tid"] for w in st["txns"]], 0)
            self.ev(f"Closed cases {', '.join(sim)} record the same undocumented pattern: four online purchases within forty minutes, each just under a $500 authorization threshold",
                    "document", "closed_cases_history:analyst_notes", sim, 3.5, "structuring")
            self.pattern_description = (
                "Several online purchases on one card within an hour, each priced just under $500, which looks chosen to stay below a $500 "
                "authorization or review threshold. It affects cardholders whose card number is compromised; the same shape appears in five closed "
                f"cases from September 2016 ({', '.join(sim[:3])}). Found by scanning the card's transaction window for clustered amounts below the threshold.")
        if ct["hit"]:
            self.pattern = "card_testing"
            self.hyp["card_testing"] = 3.0
            self.affect(ct["small"] + ct["big"])
            self.ev(f"{len(ct['small'])} online authorizations under $5 within an hour, then {money(ct['big'][0]['amt'])}",
                    "graph", f"query:card_window(card_id={card}, hours=24)", [w["tid"] for w in ct["small"] + ct["big"]], 3.0, "card_testing")

        if dv["online"] and self.pattern != "undocumented":
            if dv["new_flag"] and not dv["seen_on_card_before"]:
                w = 0.4 + (0.5 if dv["proxy"] in PROXY_RISK else 0)
                self.hyp["new_device"] = w
                self.signals += 1
                self.ev(f"Device profile '{f['dev']}' is marked New and has never been used on {card}" + (f"; connection through {dv['proxy']}" if dv["proxy"] else ""),
                        "graph", f"query:device_neighbors(device='{f['dev']}')", [f["tid"]])
            elif dv["seen_on_card_before"]:
                self.hyp["known_device"] = -0.4
                self.signals += 1
                self.ev(f"Device profile '{f['dev']}' was used on this card {dv['seen_on_card_before']} times before", "graph",
                        f"query:device_neighbors(device='{f['dev']}')", [f["tid"]])
        if rg["known"] and f["channel"] == "in_person":
            if rg["prior_in_region"] == 0:
                home = rg["other_regions_same_window"]
                if rg["days_in_new"] >= 3 and not home:
                    self.hyp["trip"] = -1.2
                    self.ev(f"Several days of purchases in region {f['addr1']} and none at home: consistent with a trip", "graph",
                            f"query:region_cluster(addr1={f['addr1']})", [x["tid"] for x in rg["in_new"]], -1.2, "trip")
                else:
                    self.hyp["out_of_region"] = 1.0
                    self.signals += 1
                    self.ev(f"Card-present purchase in billing region {f['addr1']}, where this card has no history, while purchases continue in regions {home[:5]}",
                            "graph", f"query:region_cluster(addr1={f['addr1']}, hours=48)", [f["tid"]])
            else:
                self.hyp["known_region"] = -0.3
                self.signals += 1
                self.ev(f"Card has {rg['prior_in_region']} earlier transactions in billing region {f['addr1']}; not a new region", "graph",
                        f"query:region_cluster(addr1={f['addr1']})", [f["tid"]])
        if rc["hit"]:
            self.hyp["recurring"] = -2.5
            self.signals += 1
            self.ev(f"Same amount under product {f['pcd']} charged roughly monthly before ({', '.join(money(x['amt']) for x in rc['txns'][-3:])}); looks like the cardholder's own recurring charge",
                    "graph", f"query:recurring_charge(card_id={card})", [x["tid"] for x in rc["txns"][-3:]] + [f["tid"]])
            self.pattern = "none"
        if mf:
            self.hyp["match_flags"] = 0.2
            self.ev(f"Match flags {', '.join(mf)} show a mismatch (Vesta match flags; exact meaning unpublished)", "graph",
                    f"query:txn({f['tid']})", [f["tid"]])

        # the rest of the episode: same client uid, scored as suspicious, before the case opened
        if self.pattern in ("none", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use", "account_takeover"):
            ep = memory.episode(tl, card, f)
            self.episode_rows = ep
        prior = tl.prior_cases_for(c["customer_id"], [card])
        fraud_prior = [p for p in prior if p["outcome"] == "confirmed_fraud"]
        if prior:
            self.similar += [p["case_id"] for p in prior[:3]]
            self.ev(f"Card history holds {len(prior)} earlier closed cases ({len(fraud_prior)} confirmed fraud, {len(prior) - len(fraud_prior)} cleared); most recent {prior[0]['case_id']} ({prior[0]['pattern']}, {prior[0]['outcome']})",
                    "graph", f"query:similar_closed_cases(card_id={card})", [p["case_id"] for p in prior[:5]])
        if tg.enabled():
            self.graph_checks(card, f)
        self.step("assess", "Hypotheses scored: " + ", ".join(f"{k} {v:+.1f}" for k, v in self.hyp.items()))

        # --- assess ------------------------------------------------------------
        p0 = sigmoid(sum(self.hyp.values()))
        if self.pattern == "none" and p0 >= 0.30:
            self.pattern = self.known_pattern(dv, rg, mf)
        if p0 >= 0.30 and self.pattern not in ("undocumented",) and not self.amounts:
            self.affect([f] + [r for r in getattr(self, "episode_rows", []) if r["tid"] != f["tid"]])
        self.p_initial = round(p0, 2)
        self.similar = list(dict.fromkeys(self.similar + memory.similar_by_pattern(tl, self.pattern)))[:6]
        self.step("assess", f"Fraud probability {self.p_initial:.2f}; pattern {self.pattern}; {self.signals} independent signals.")

        if tg.enabled():
            self.policy_path()
        self.decide()
        self.latency = round(time.time() - t0, 2)
        return self

    # --- live graph calls through TigerGraph MCP ------------------------------
    def mcp(self, query, **params):
        import mcp_graph
        self.tl.calls.append(f"mcp:tigergraph__run_installed_query({query})")
        r = mcp_graph.client().query(query, **params)
        if not (isinstance(r, dict) and r.get("success")):
            return None
        return r.get("data", {}).get("result")

    def graph_checks(self, card, f):
        as_of = self.c["opened_at"].strftime("%Y-%m-%d %H:%M:%S")
        res = self.mcp("similar_closed_cases", card=card, as_of=as_of)
        if res:
            mine = [v["v_id"] for blk in res for v in blk.get("mine", [])]
            linked = [v["v_id"] for blk in res for v in blk.get("linked", [])]
            if mine:
                self.ev(f"Case memory: this card already has agent case(s) {', '.join(mine)} written to the graph earlier in this run",
                        "graph", f"mcp:similar_closed_cases(card={card})", mine)
            if linked:
                self.similar += linked[:2]
                self.ev(f"Closed cases {', '.join(linked[:4])} list this card as a connected card of another compromise",
                        "graph", f"mcp:similar_closed_cases(card={card})", linked[:6])
        if f["dev"]:
            res = self.mcp("closed_cases_by_device", dev=f["dev"], as_of=as_of)
            cases = [v for blk in (res or []) for v in blk.get("cases", [])]
            fraud = [v["v_id"] for v in cases if v["attributes"].get("outcome") == "confirmed_fraud"]
            cleared = [v["v_id"] for v in cases if v["attributes"].get("outcome") == "cleared"]
            self.step("investigate", f"MCP traversal DeviceProfile→Txn→ClosedCase: {len(fraud)} confirmed-fraud and {len(cleared)} cleared closed cases used this device profile.")
            self.device_cases = {"fraud": fraud, "cleared": cleared}
        if f["dev"] and f["id_15"] == "New" and f["id_23"]:
            # graph algorithm: connected components over cards and New-behind-proxy devices, last 30 days
            since = (self.c["opened_at"] - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            res = self.mcp("device_rings", since=since, until=as_of, min_cards=5, focus=card)
            if res and len(res) > 1:
                size = res[0].get("focus_ring_cards", 0)
                members = [v["v_id"] for v in res[1].get("ring_cards", []) if v["v_id"] != card]
                devs = [v["v_id"] for v in res[1].get("ring_devices", [])]
                self.step("investigate", f"Connected components (device_rings): {res[0].get('rings_at_min_size')} components of 5+ cards in 30 days; this card's has {size} cards and {len(devs)} device(s).")
                if size >= 5:
                    self.ev(f"Connected-components run over the card–device graph (New devices behind a proxy, 30 days) puts this card in a component of {size} cards joined by {len(devs)} device profile(s)",
                            "graph", f"mcp:device_rings(focus={card}, days=30)", members[:20] + devs)

    def policy_path(self):
        res = self.mcp("policy_for_pattern", pat=self.pattern)
        if res:
            rules = [v["v_id"] for blk in res for v in blk.get("rules", [])]
            acts = [v["v_id"] for blk in res for v in blk.get("acts", [])]
            self.step("assess", f"Policy graph for pattern {self.pattern}: rules {rules}; allowed actions {acts}.")

    def known_pattern(self, dv, rg, mf):
        f = self.f
        if f["channel"] == "in_person":
            return "out_of_region_use" if rg.get("prior_in_region") == 0 else ("account_takeover" if mf else "out_of_region_use")
        if dv.get("new_flag") and not dv.get("seen_on_card_before"):
            return "card_not_present_new_device"
        return "card_not_present_fraud"

    # --- decide ----------------------------------------------------------------
    def decide(self):
        c, f = self.c, self.f
        p0 = self.p_initial
        exp0 = round(sum(self.amounts.values()), 2)
        A = policy.act
        single = self.signals <= 1
        cust = c["trigger_type"] == "customer_report"
        self.requests = []
        init, final = [], []

        stop_now = (p0 >= 0.85 or p0 <= 0.15) and self.signals >= 2

        if self.pattern == "undocumented":
            init = [A("CREATE_CASE", exp0, "3a/R9: coordinated activity outside the known patterns"),
                    A("DECLINE_TRANSACTION", exp0, "R9: stop pending authorizations from the pattern while the case is reviewed"),
                    A("FILE_REPORT", exp0, "R9 and 3a: undocumented, coordinated abuse" + (" linked by a shared device profile (R6)" if self.shared_origin else "")),
                    A("ESCALATE_TO_ANALYST", exp0, "R9: new pattern goes to a human analyst")]
            if self.connected_cards:
                init.append(A("MONITOR_CONNECTED_CARDS", exp0, f"R6: {len(self.connected_cards)} other cards used the same device profile"))
            p1, verdict = max(p0, 0.9), "fraud"
            if not cust:
                self.requests.append({"type": "customer_validation", "asked_after_step": len(self.steps),
                                      "assumed_response": "Cardholder states they did not make the purchases from this device and still holds the card (simulated; consistent with the confirmed outcomes of the matching closed cases)"})
            final = [A("BLOCK_CARD", exp0, ("R2: customer denied" if True else "") + f"; exposure {money(exp0)} " + ("under" if exp0 <= 2500 else "over") + " $2,500"),
                     A("CREATE_CASE", exp0, "R2/R9"),
                     A("FILE_REPORT", exp0, "R9 and 3a: undocumented coordinated pattern" + ("; R6 shared device profile" if self.shared_origin else "") + (f"; exposure {money(exp0)} over $1,000" if exp0 > 1000 else "")),
                     A("ESCALATE_TO_ANALYST", exp0, "R9")]
            if self.connected_cards:
                final.append(A("MONITOR_CONNECTED_CARDS", exp0, f"R6: every card sharing the device profile ({len(self.connected_cards)})"))
            what = "Cardholder denial confirmed the unauthorized use, so the recommendation adds BLOCK_CARD under R2; report, escalation and monitoring stand." if not cust else \
                   "Customer's report already counts as a denial (R2); the block was added once the pattern evidence confirmed it."
            self.finish(p1, verdict, init, final, what,
                        "Pattern matched confirmed closed cases and the cardholder denial settled the verdict; further queries would not change the actions.")
            return

        if self.pattern == "card_testing":
            big_cleared = any(self.amounts[t] > 100 for t in self.amounts)
            init = [A("DECLINE_TRANSACTION", exp0, "R5: testing sequence observed"),
                    A("STEP_UP_AUTH", exp0, "R5"), A("CREATE_CASE", exp0, "3a: probability above 0.30")]
            if big_cleared:
                init.insert(0, A("BLOCK_CARD", exp0, "R5: a purchase over $100 already cleared"))

        if stop_now and p0 >= 0.85:
            basis = "R2: customer denies the charge and" if cust else "§6:"
            final = init or [A("BLOCK_CARD", exp0, f"{basis} {self.signals} independent signals put probability at {p0:.2f}; exposure {money(exp0)} "
                                                   + ("≤" if exp0 <= 2500 else ">") + " $2,500"),
                             A("CREATE_CASE", exp0, "3a: probability above 0.30" + ("; R6 shared origin" if self.shared_origin else ""))]
            if policy.sar_required("fraud", p0, exp0, self.shared_origin, self.coordinated):
                final.append(A("FILE_REPORT", exp0, ("R6: shared device profile links this card to other cards' suspicious activity" if self.shared_origin
                                                     else "R2/3a: exposure over $1,000")))
            if self.connected_cards:
                final.append(A("MONITOR_CONNECTED_CARDS", exp0, f"R6: {', '.join(self.connected_cards[:5])} share the device profile"))
            self.finish(p0, "fraud", final, final, "nothing", f"Probability {p0:.2f} with {self.signals} independent signals meets the §6 stopping rule.")
            return
        if stop_now and p0 <= 0.15 and not cust:
            final = [A("ALLOW_TRANSACTION", 0, f"§6: probability {p0:.2f} backed by {self.signals} independent signals"),
                     A("CLOSE_NO_FRAUD", 0, "Evidence points to legitimate activity")]
            self.finish(p0, "legitimate", final, final, "nothing",
                        f"Probability {p0:.2f} with {self.signals} independent signals meets the §6 stopping rule; the risk score alone did not hold up.")
            return

        # uncertain, or a customer dispute: gather evidence under R1
        online = f["channel"] == "online"
        ask = "VERIFY_WITH_CUSTOMER"
        init = init or []
        if not init:
            if cust:
                init = [A("CREATE_CASE", exp0, "3a: customer disputes a charge"),
                        A("VERIFY_WITH_CUSTOMER", exp0, "R1: graph evidence is " + ("weak" if p0 < 0.7 else "supportive") + "; confirm details of the disputed charge before blocking"),
                        A("MONITOR_CARD", exp0, "Raise monitoring while the dispute is open")]
            else:
                init = [A("CREATE_CASE", exp0, "3a: evidence requested" + (" and probability above 0.30" if p0 >= 0.30 else "")),
                        A("STEP_UP_AUTH" if online else ask, exp0, f"R1: probability {p0:.2f} rests on {'a single signal' if single else 'weak signals'}; verify before any block"),
                        A("MONITOR_CARD", exp0, "Raise monitoring for 72 hours while verification is pending")]
                if online:
                    init.insert(2, A(ask, exp0, "R1: ask the cardholder directly as well"))

        # simulated reply: the evidence simulator answers the way the graph evidence
        # (excluding the bank's risk score) points; the assumption is stated in the file
        deny = p0 >= 0.5
        if cust:
            resp = ("Cardholder repeats that they did not make the charge and has the card; the device and merchant are unknown to them (simulated, consistent with the graph evidence)"
                    if deny else
                    "Shown the merchant, date and device, the cardholder recognises the charge as their own (a household member's purchase on the account) and withdraws the dispute (simulated, consistent with the graph evidence)")
        else:
            resp = ("Cardholder says they did not make the transaction and still has the card (simulated, consistent with the graph evidence)"
                    if deny else "Cardholder confirms they made the transaction (simulated, consistent with the graph evidence)")
        self.requests.append({"type": "customer_validation", "asked_after_step": len(self.steps), "assumed_response": resp})
        if online and not cust:
            self.requests.append({"type": "step_up_auth", "asked_after_step": len(self.steps),
                                  "assumed_response": "One-time passcode " + ("not completed by the device owner" if deny else "completed on the cardholder's registered phone") + " (simulated)"})
        self.ev(resp.split(" (simulated")[0], "customer", "evidence_request:1", [c["customer_id"]])
        self.step("gather_evidence", f"Requested customer validation; assumed reply: {resp}")

        if deny:
            if not self.amounts:
                self.affect([f])
            exp = round(sum(self.amounts.values()), 2)
            p1 = round(min(0.95, sigmoid(logit(p0) + 1.6)), 2)
            if p1 < 0.7:
                verdict = "uncertain"
                final = [A("CREATE_CASE", exp, "3a"), A("MONITOR_CARD", exp, "R4-style hold while the analyst reviews"),
                         A("DECLINE_TRANSACTION", exp, "Pending authorizations held while evidence conflicts")]
                if exp > 500 or True:
                    final.append(A("ESCALATE_TO_ANALYST", exp, "R8: evidence conflicts (customer denial vs. weak graph signals)"))
                what = "Customer denial raised the probability but the graph evidence stays weak, so the case goes to an analyst under R8 instead of a block."
            else:
                verdict = "fraud"
                final = [A("BLOCK_CARD", exp, f"R2: customer denied; exposure {money(exp)} " + ("≤" if exp <= 2500 else ">") + " $2,500"),
                         A("CREATE_CASE", exp, "R2")]
                if policy.sar_required(verdict, p1, exp, self.shared_origin, self.coordinated):
                    final.append(A("FILE_REPORT", exp, "R2: " + ("exposure over $1,000" if exp > 1000 else "linked to a shared device or another card's fraud")))
                if self.connected_cards:
                    final.append(A("MONITOR_CONNECTED_CARDS", exp, "Linked cards share the device profile"))
                what = f"Customer denial raised probability from {p0:.2f} to {p1:.2f}; R2 now calls for a block" + (" and a report." if any(a["action"] == "FILE_REPORT" for a in final) else "; exposure and links stay below the report threshold, so case only.")
            self.finish(p1, verdict, init, final, what, "The customer's answer settled the question (§6); further queries would not change the actions.")
        else:
            self.amounts = {}
            p1 = round(max(0.03, sigmoid(logit(p0) - 2.2)), 2)
            final = [A("CLOSE_NO_FRAUD", 0, "R3: customer confirmed the transaction; confirmation noted in the case file")]
            if not cust:
                final.insert(0, A("ALLOW_TRANSACTION", 0, "R3"))
            what = f"Customer confirmation lowered probability from {p0:.2f} to {p1:.2f}; under R3 the case closes as legitimate and nothing is blocked."
            self.finish(p1, "legitimate", init, final, what, "The customer's confirmation settled the question (§6).")

    def finish(self, p, verdict, init, final, what, stop):
        c = self.c
        self.p_final = round(p, 2)
        self.verdict = verdict
        if verdict == "legitimate":
            self.amounts = {}
            self.pattern = "none"
            self.pattern_description = ""
            self.connected_cards = []
        exp = round(sum(self.amounts.values()), 2)
        for stage in (init, final):
            for a in stage:
                a["route"] = policy.route(a["action"], exp)
        self.init, self.final, self.what, self.stop = init, final, what, stop
        acts = [a["action"] for a in final]
        self.status = ("escalated" if "ESCALATE_TO_ANALYST" in acts else
                       "closed_fraud" if verdict == "fraud" else
                       "closed_legitimate" if verdict == "legitimate" else "open")
        self.step("recommend", f"Initial: {[a['action'] for a in init]}. Final: {acts}. {what}")

    # --- answer file -------------------------------------------------------------
    def answer(self, narr):
        c, f = self.c, self.f
        exp = round(sum(self.amounts.values()), 2)
        affected = sorted(self.amounts, key=int)
        file_sar = any(a["action"] == "FILE_REPORT" for a in self.final)
        rows = [self.tl.txn(int(t)) for t in affected] if file_sar else []
        ans = {
            "case_id": c["case_id"],
            "case": {
                "status": self.status,
                "verdict": self.verdict,
                "fraud_probability": self.p_final,
                "pattern": self.pattern,
                "pattern_description": self.pattern_description if self.pattern == "undocumented" else "",
                "affected_txn_ids": affected,
                "first_suspicious_txn_id": affected[0] if affected else "",
                "connected_card_ids": self.connected_cards,
                "connected_device_profiles": self.connected_devices if self.verdict != "legitimate" else [],
                "exposure_usd": exp,
                "evidence": self.evidence,
                "similar_prior_cases": self.similar,
                "summary": narr["summary"],
                "written_to_graph": False,
                "graph_case_id": "",
            },
            "evidence_requests": self.requests,
            "next_best_actions": {"initial": self.init, "final": self.final, "what_changed": self.what},
            "sar": {
                "file": file_sar,
                "reason": narr["sar_reason"],
                "narrative": narr["sar_narrative"] if file_sar else "",
                "subjects": ([c["customer_id"], c["card_id"]] + self.connected_cards + self.connected_devices) if file_sar else [],
                "total_amount_usd": exp if file_sar else 0,
                "activity_dates": [d(min(r["ts"] for r in rows)), d(max(r["ts"] for r in rows))] if file_sar and rows else [],
            },
            "stop_reason": self.stop,
            "tool_calls": len(self.tl.calls),
            "tokens": narr["tokens"],
            "latency_s": 0.0,
            "_amounts": self.amounts,
            "_single_signal": self.signals <= 1,
            "_p_initial": self.p_initial,
        }
        return ans


def ground(inv):
    """GraphRAG step: vector search over policy, typology and FinCEN text plus closed-case
    notes, expanded along Pattern -> PolicyRule -> DocChunk, all through MCP."""
    try:
        passages, note_cases = graphrag.retrieve(inv)
    except Exception as e:  # retrieval grounds the explanation; a slow vector index must not sink the case
        inv.step("retrieve", f"GraphRAG unavailable ({type(e).__name__}); explanation written from graph facts only.")
        return
    inv.passages = passages
    cited = set()
    for a in inv.final:
        cited |= set(__import__("re").findall(r"\bR\d+\b", a["reason"]))
    for p in passages:
        rid = p["id"].replace("POLICY-", "")
        if p["id"].startswith("POLICY-R") and rid in cited:
            inv.ev(f"Policy {rid} (retrieved by {p['via']} search): {p['text'][:260]}", "document", f"doc:{p['id']}", [p["id"]])
        elif p["id"].startswith("PATTERN-") and p["via"] == "graph":
            inv.ev(f"Known-pattern definition matched to this case: {p['text'][:260]}", "document", f"doc:{p['id']}", [p["id"]])
    fincen = [p for p in passages if p["id"].startswith("FINCEN")]
    if fincen and any(a["action"] == "FILE_REPORT" for a in inv.final):
        inv.ev("SAR narrative written against FinCEN narrative guidance passages retrieved for this case (who, what, when, where, how, why)",
               "document", "doc:" + ",".join(p["id"] for p in fincen[:3]), [p["id"] for p in fincen[:3]])
    new = [c for c in note_cases if c not in inv.similar]
    if new:
        inv.similar = (inv.similar + new[:2])[:6]
        inv.ev(f"Closed cases whose analyst notes read most like this investigation (vector search over ClosedCase.note_emb): {', '.join(new[:4])}",
               "graph", "mcp:search_top_k_similarity(ClosedCase.note_emb)", new[:4])
    inv.step("retrieve", f"GraphRAG: {sum(p['via'] == 'vector' for p in passages)} passages by vector search, "
                         f"{sum(p['via'] == 'graph' for p in passages)} by graph path Pattern→PolicyRule→DocChunk, "
                         f"{len(note_cases)} closed cases by note similarity.")


def run_case(case):
    t0 = time.time()
    inv = Investigation(case).run()
    inv.passages = []
    if tg.enabled():
        ground(inv)
    narr = explain.narrate(inv)
    ans = inv.answer(narr)
    errs = policy.lint(ans)
    if errs:
        raise SystemExit(f"{case['case_id']} failed policy lint: {errs}")
    gid = memory.write_case(inv, ans)
    ans["case"]["written_to_graph"] = bool(gid)
    ans["case"]["graph_case_id"] = gid or ""
    ans["tool_calls"] = len(inv.tl.calls) + 1
    ans["latency_s"] = round(time.time() - t0, 2)
    for k in ("_amounts", "_single_signal", "_p_initial"):
        ans.pop(k)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(TRACE, exist_ok=True)
    with open(os.path.join(OUT, f"{case['case_id']}.json"), "w") as fh:
        json.dump(ans, fh, indent=2, default=str)
    with open(os.path.join(TRACE, f"{case['case_id']}.json"), "w") as fh:
        json.dump({"steps": inv.steps, "hypotheses": inv.hyp, "tool_calls": inv.tl.calls,
                   "p_model": inv.p_model, "p_initial": inv.p_initial, "p_final": inv.p_final}, fh, indent=2, default=str)
    return ans


def main():
    con = duckdb.connect(DB, read_only=True)
    cur = con.execute("select * from cp order by opened_at")   # memory grows in time order
    cols = [x[0] for x in cur.description]
    cases = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    only = set(sys.argv[1:])
    for case in cases:
        if only and case["case_id"] not in only:
            continue
        if os.environ.get("LIVE") == "1":
            print(f"\n=== {case['case_id']}  {case['trigger_type']}  opened {case['opened_at']} ===", flush=True)
        a = run_case(case)
        cz = a["case"]
        print(f"{case['case_id']} {cz['verdict']:<10} p={cz['fraud_probability']:.2f} {cz['pattern']:<28} exp={cz['exposure_usd']:>8} sar={a['sar']['file']!s:<5} "
              f"init={[x['action'] for x in a['next_best_actions']['initial']]} final={[x['action'] for x in a['next_best_actions']['final']]}")


if __name__ == "__main__":
    main()
