"""TigerGraph access. Bulk loading uses pyTigerGraph directly (load_graph.py); everything
the agent does at run time goes through the TigerGraph MCP server (mcp_graph.py).
Enabled when TG_HOST is set in .env and OFFLINE is not 1."""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def enabled():
    return bool(os.environ.get("TG_HOST")) and os.environ.get("OFFLINE") != "1"


def conn():
    import pyTigerGraph as tgc
    c = tgc.TigerGraphConnection(host=os.environ["TG_HOST"], graphname=os.environ.get("TG_GRAPH", "Fraud"),
                                 gsqlSecret=os.environ.get("TG_SECRET"), tgCloud=True)
    if os.environ.get("TG_SECRET"):
        c.getToken(os.environ["TG_SECRET"])
    return c


def upsert_case(gid, case, ans):
    """Write the case into the graph as case memory, through MCP tools."""
    import mcp_graph
    try:
        g = mcp_graph.client()
        cz = ans["case"]
        r = g.add_node("FraudCase", gid, {"case_id": case["case_id"], "opened_at": str(case["opened_at"]), "status": cz["status"],
                                          "verdict": cz["verdict"], "pattern": cz["pattern"], "fraud_probability": cz["fraud_probability"],
                                          "exposure_usd": cz["exposure_usd"], "summary": cz["summary"]})
        if not (isinstance(r, dict) and r.get("success")):
            raise RuntimeError(str(r)[:300])
        g.add_edges("CASE_ON_CARD", "FraudCase", "BankCard", [(gid, case["card_id"])])
        g.add_edges("CASE_PATTERN", "FraudCase", "Pattern", [(gid, cz["pattern"])])
        if cz["affected_txn_ids"]:
            g.add_edges("AFFECTS", "FraudCase", "Txn", [(gid, t) for t in cz["affected_txn_ids"]])
        if cz["connected_card_ids"]:
            g.add_edges("CASE_CONNECTED_TO", "FraudCase", "BankCard", [(gid, x) for x in cz["connected_card_ids"]])
        if cz["connected_device_profiles"]:
            g.add_edges("CASE_DEVICE", "FraudCase", "DeviceProfile", [(gid, x) for x in cz["connected_device_profiles"]])
        if cz["similar_prior_cases"]:
            g.add_edges("SIMILAR_TO", "FraudCase", "ClosedCase", [(gid, x) for x in cz["similar_prior_cases"]])
        return True
    except Exception as e:  # graph down: keep the local mirror, and the file says written_to_graph=false
        print(f"  TigerGraph write failed for {gid}: {e}")
        return False
