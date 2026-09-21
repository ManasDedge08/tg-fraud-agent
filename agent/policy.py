"""Fraud Policy v1.0 as code. The LLM never picks routes or rule numbers: this module does,
so every action in an answer file is policy-valid by construction. `lint` re-checks the
final answer and the agent refuses to emit anything that fails it."""

AUTO = {"ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER",
        "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE",
        "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"}
ALL = AUTO | {"DECLINE_TRANSACTION", "BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"}


def route(action, exposure):
    if action in AUTO:
        return "auto"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    return "L2"  # BLOCK_ALL_CARDS, FILE_REPORT


def act(action, exposure, reason):
    return {"action": action, "route": route(action, exposure), "reason": reason}


def sar_required(verdict, prob, exposure, shared_origin, coordinated):
    """3a: confirmed or strongly suspected, and one of: >$1,000, shared origin, coordinated/undocumented."""
    strong = verdict == "fraud" or prob >= 0.70
    return strong and (exposure > 1000 or shared_origin or coordinated)


def lint(ans):
    """Return a list of policy violations; empty means the answer may be emitted."""
    errs = []
    c, nba, sar = ans["case"], ans["next_best_actions"], ans["sar"]
    for stage in ("initial", "final"):
        for a in nba[stage]:
            if a["action"] not in ALL:
                errs.append(f"{stage}: unknown action {a['action']}")
            elif a["route"] != route(a["action"], c["exposure_usd"]):
                errs.append(f"{stage}: {a['action']} route {a['route']} should be {route(a['action'], c['exposure_usd'])}")
    final = [a["action"] for a in nba["final"]]
    if sar["file"] != ("FILE_REPORT" in final):
        errs.append("sar.file disagrees with FILE_REPORT in final actions")
    if "FILE_REPORT" in final and "CREATE_CASE" not in final and c["status"] != "closed_fraud":
        errs.append("a report needs a case behind it")
    if "BLOCK_ALL_CARDS" in final:
        errs.append("R10: BLOCK_ALL_CARDS needs two compromised cards; not supported by this agent")
    if c["verdict"] == "legitimate" and (c["affected_txn_ids"] or c["exposure_usd"] or sar["file"]):
        errs.append("legitimate verdict must have no affected txns, zero exposure, no SAR")
    exp = round(sum(ans["_amounts"].values()), 2) if c["affected_txn_ids"] else 0
    if abs(exp - c["exposure_usd"]) > 0.01:
        errs.append(f"exposure {c['exposure_usd']} != sum of affected {exp}")
    initial = [a["action"] for a in nba["initial"]]
    if ans["_single_signal"] and ans["_p_initial"] < 0.70 and any(a in initial for a in ("BLOCK_CARD", "BLOCK_ALL_CARDS")):
        errs.append("R1: block on a single weak signal")
    if c["pattern"] == "undocumented" and not c["pattern_description"]:
        errs.append("undocumented pattern needs a description")
    if sar["file"] and not sar["narrative"]:
        errs.append("SAR narrative missing")
    return errs
