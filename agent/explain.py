"""Explanation layer. Builds a compact context from the investigation (evidence claims,
hypotheses, decisions, the policy rules cited) and asks the LLM for the analyst summary
and the SAR narrative. The LLM sees structured facts, never raw rows.

Backend: `claude -p` (Claude Code headless) when USE_LLM=1, else a deterministic template.
"""
import json
import os
import signal
import subprocess

POLICY_NOTES = {
    "R2": "Customer denies: BLOCK_CARD + CREATE_CASE; FILE_REPORT if exposure > $1,000 or shared device / another card's fraud.",
    "R6": "Shared origin across cards: name it, CREATE_CASE, FILE_REPORT, MONITOR_CONNECTED_CARDS.",
    "R9": "Undocumented coordinated pattern: CREATE_CASE, FILE_REPORT, ESCALATE_TO_ANALYST; describe it in own words.",
    "3a": "SAR when fraud is confirmed or strongly suspected and exposure > $1,000, shared origin, or coordinated/undocumented.",
}


def money(x):
    return f"${x:,.2f}"


def context(inv):
    c, f = inv.c, inv.f
    rows = [inv.tl.txn(int(t)) for t in sorted(inv.amounts, key=int)]
    return {
        "case_id": c["case_id"], "trigger": c["trigger_text"], "customer": c["customer_id"], "card": c["card_id"],
        "flagged": {"id": str(f["tid"]), "ts": str(f["ts"]), "amount": f["amt"], "channel": f["channel"], "product": f["pcd"],
                    "region": f["addr1"], "device": f["dev"]},
        "verdict": inv.verdict, "fraud_probability_initial": inv.p_initial, "fraud_probability_final": inv.p_final,
        "pattern": inv.pattern, "pattern_description": inv.pattern_description,
        "affected": [{"id": str(r["tid"]), "ts": str(r["ts"]), "amount": r["amt"], "channel": r["channel"], "region": r["addr1"],
                      "device": r["dev"]} for r in rows],
        "exposure": round(sum(inv.amounts.values()), 2),
        "connected_cards": inv.connected_cards, "devices": inv.connected_devices,
        "evidence": [e["claim"] for e in inv.evidence],
        "evidence_requests": inv.requests,
        "initial_actions": [a["action"] + " (" + a["reason"] + ")" for a in inv.init],
        "final_actions": [a["action"] + " (" + a["reason"] + ")" for a in inv.final],
        "what_changed": inv.what,
        "similar_prior_cases": inv.similar,
        "retrieved_guidance": [{"id": p["id"], "section": p["section"], "text": p["text"][:700]}
                               for p in getattr(inv, "passages", [])][:8],
    }


def template(inv, ctx):
    c, f = inv.c, inv.f
    acts = ", ".join(a["action"] for a in inv.final)
    if inv.verdict == "legitimate":
        summary = (f"{c['trigger_type'].replace('_', ' ').capitalize()} on {money(f['amt'])} {f['channel'].replace('_', ' ')} transaction {f['tid']}. "
                   f"Graph evidence did not support fraud (probability {inv.p_initial:.2f} before verification). "
                   f"{inv.requests[0]['assumed_response'].split(' (simulated')[0] if inv.requests else 'No further evidence needed'}. "
                   f"Closed as legitimate: {acts}.")
    else:
        summary = (f"{'Undocumented pattern' if inv.pattern == 'undocumented' else inv.pattern.replace('_', ' ').capitalize()} on card {c['card_id']}: "
                   f"{len(ctx['affected'])} transaction(s) totalling {money(ctx['exposure'])}. "
                   + (f"Linked to {len(inv.connected_cards)} other cards through a shared device profile. " if inv.connected_cards else "")
                   + f"Fraud probability {inv.p_initial:.2f} before and {inv.p_final:.2f} after evidence. Final actions: {acts}.")
    narrative = ""
    if any(a["action"] == "FILE_REPORT" for a in inv.final):
        a = ctx["affected"]
        first, last = a[0]["ts"][:16], a[-1]["ts"][:16]
        narrative = (
            f"Between {first} and {last}, card {c['card_id']} held by customer {c['customer_id']} was used for {len(a)} "
            f"{'online ' if all(x['channel'] == 'online' for x in a) else ''}transaction(s) totalling {money(ctx['exposure'])} "
            f"({', '.join(x['id'] + ' ' + money(x['amount']) for x in a[:8])}). "
            + (f"{inv.pattern_description} " if inv.pattern == "undocumented" else f"The activity matches the {inv.pattern.replace('_', ' ')} pattern. ")
            + (f"The transactions came from device profile {inv.connected_devices[0]}. " if inv.connected_devices else "")
            + (f"The same device profile was used in the same period on {len(inv.connected_cards)} other customers' cards ({', '.join(inv.connected_cards[:8])}{'…' if len(inv.connected_cards) > 8 else ''}). " if inv.connected_cards else "")
            + (f"It matches closed fraud cases {', '.join(inv.similar[:4])}. " if inv.similar else "")
            + f"{inv.requests[0]['assumed_response'].split(' (simulated')[0] + '. ' if inv.requests else ''}"
            f"The activity is suspicious because it is inconsistent with the cardholder's history and fits a coordinated misuse of card data. "
            f"Actions: {acts}.")
    reason = ("R9/R6 and 3a: " if inv.pattern == "undocumented" else "R2 and 3a: ") + (
        "coordinated or shared-origin fraud" if (inv.coordinated or inv.shared_origin) else f"exposure {money(ctx['exposure'])}") \
        if narrative else ("3a: no fraud found, no report." if inv.verdict == "legitimate" else
                           f"3a: exposure {money(ctx['exposure'])} is under $1,000 and no shared device, region cluster or coordinated pattern; case only.")
    return {"summary": summary, "sar_narrative": narrative, "sar_reason": reason, "tokens": 0}


PROMPT = """You are the explanation step of a bank fraud-investigation agent. Using ONLY the facts in the JSON below, write:
1. "summary": 2-6 sentences an analyst can read: what triggered it, what the graph showed, the verdict, what happens next.
2. "sar_narrative": only if final_actions contain FILE_REPORT, else "". 6-12 sentences for a regulator, standing on its own:
   who (customer, card, devices, connected cards), what, when (dates), where (channel, region), how, why suspicious, actions taken.
Use retrieved_guidance (bank policy, known patterns, FinCEN SAR narrative guidance) for structure and wording: cite policy rule numbers it supports, and follow FinCEN's who/what/when/where/why/how. Never invent IDs, amounts, dates, time zones, device brands or facts not in FACTS. Plain prose. Reply with a single JSON object {"summary": "...", "sar_narrative": "..."} and nothing else.

FACTS:
"""


def narrate(inv):
    ctx = context(inv)
    out = template(inv, ctx)
    if os.environ.get("USE_LLM") != "1":
        return out
    try:
        p = subprocess.Popen(["claude", "-p", "--output-format", "json", "--model", os.environ.get("LLM_MODEL", "haiku")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             start_new_session=True, cwd="/tmp")
        try:
            stdout, _ = p.communicate(PROMPT + json.dumps(ctx, default=str), timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)  # a stalled model call must not hold up the case
            raise
        env = json.loads(stdout)
        text = env["result"].strip()
        text = text[text.index("{"): text.rindex("}") + 1]
        body = json.loads(text)
        u = env.get("usage", {})
        out["summary"] = body["summary"]
        if out["sar_narrative"] and body.get("sar_narrative"):
            out["sar_narrative"] = body["sar_narrative"]
        out["tokens"] = int(u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))
    except Exception as e:
        print(f"  LLM explain failed for {inv.c['case_id']}, using template: {e}")
    return out
