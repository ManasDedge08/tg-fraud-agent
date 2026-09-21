"""Create the schema, install queries and load the graph into TigerGraph (Savanna).

    TG_HOST=https://<workspace>.i.tgcloud.io TG_SECRET=... python load_graph.py [schema|data|queries|all]
"""
import os
import sys

import duckdb
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
import tg  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB = os.path.join(ROOT, "data", "hh.duckdb")
BATCH = 5000


def gsql_file(c, name):
    text = open(os.path.join(ROOT, "gsql", name)).read()
    print(c.gsql(text))


def upsert_v(c, vtype, df, pk, attrs):
    for i in range(0, len(df), BATCH):
        c.upsertVertexDataFrame(df.iloc[i:i + BATCH], vtype, v_id=pk, attributes={a: a for a in attrs})
    print(f"  {vtype}: {len(df)}")


def upsert_e(c, src, etype, tgt, df, s, t, attrs=None):
    for i in range(0, len(df), BATCH):
        c.upsertEdgeDataFrame(df.iloc[i:i + BATCH], src, etype, tgt, from_id=s, to_id=t,
                              attributes={a: a for a in (attrs or [])})
    print(f"  {etype}: {len(df)}")


def data(c):
    con = duckdb.connect(DB, read_only=True)
    q = lambda s: con.execute(s).df()  # noqa: E731
    upsert_v(c, "Customer", q("select distinct customer_id id from t"), "id", [])
    upsert_v(c, "BankCard", q("select distinct card_id id, coalesce(card4,'') network, coalesce(card6,'') card_type from t"), "id",
             ["network", "card_type"])
    txn = q("""select tid::varchar id, strftime(ts,'%Y-%m-%d %H:%M:%S') ts, amt, pcd product, channel, coalesce(addr1::int::varchar,'') addr1,
               rs risk_score, p_model p_fraud, coalesce(id_15,'') id_15, coalesce(id_23,'') proxy_type,
               coalesce(M4,'') m4, coalesce(M5::varchar,'') m5, coalesce(M6::varchar,'') m6, uid,
               card_id, customer_id, dev, pe, re from t""")
    upsert_v(c, "Txn", txn, "id", ["ts", "amt", "product", "channel", "addr1", "risk_score", "p_fraud", "id_15", "proxy_type", "m4", "m5", "m6", "uid"])
    upsert_v(c, "DeviceProfile", q("select dev id, any_value(coalesce(DeviceType,'')) device_type from t where dev is not null group by 1"), "id", ["device_type"])
    upsert_v(c, "EmailDomain", q("select distinct pe id from t where pe is not null union select distinct re from t where re is not null"), "id", [])
    upsert_v(c, "BillingRegion", q("select distinct addr1::int::varchar id from t where addr1 is not null"), "id", [])
    upsert_e(c, "Customer", "OWNS", "BankCard", q("select distinct customer_id s, card_id d from t"), "s", "d")
    upsert_e(c, "BankCard", "MADE", "Txn", txn[["card_id", "id"]], "card_id", "id")
    upsert_e(c, "Txn", "FROM_DEVICE", "DeviceProfile", txn[txn.dev.notna()][["id", "dev"]], "id", "dev")
    upsert_e(c, "Txn", "PURCHASER_EMAIL", "EmailDomain", txn[txn.pe.notna()][["id", "pe"]], "id", "pe")
    upsert_e(c, "Txn", "RECIPIENT_EMAIL", "EmailDomain", txn[txn.re.notna()][["id", "re"]], "id", "re")
    upsert_e(c, "Txn", "BILLED_IN", "BillingRegion", txn[txn.addr1 != ""][["id", "addr1"]], "id", "addr1")
    upsert_e(c, "Txn", "NEXT_TXN", "Txn",
             q("select tid::varchar s, lead(tid) over (partition by card_id order by ts)::varchar d from t qualify d is not null"), "s", "d")
    closed_cases(c, q)


def closed_cases(c, q=None):
    if q is None:
        con = duckdb.connect(DB, read_only=True)
        q = lambda s: con.execute(s).df()  # noqa: E731
    ccs = q("""select case_id id, strftime(opened_at,'%Y-%m-%d %H:%M:%S') opened_at, outcome, pattern, exposure_usd exposure,
               report_filed::varchar report_filed, analyst_notes notes, card_id, txn_ids::varchar txn_ids, coalesce(connected_card_ids,'') conn from cc""")
    upsert_v(c, "ClosedCase", ccs, "id", ["opened_at", "outcome", "pattern", "exposure", "report_filed", "notes"])
    upsert_e(c, "ClosedCase", "ON_CARD", "BankCard", ccs[["id", "card_id"]], "id", "card_id")
    inv = ccs.assign(tid=ccs.txn_ids.str.split("|")).explode("tid").reset_index(drop=True)
    upsert_e(c, "ClosedCase", "INVOLVES", "Txn", inv[["id", "tid"]], "id", "tid")
    con_ = ccs[ccs.conn != ""].assign(cd=lambda x: x.conn.str.split("|")).explode("cd").reset_index(drop=True)
    upsert_e(c, "ClosedCase", "CONNECTED_TO", "BankCard", con_[["id", "cd"]], "id", "cd")
    upsert_e(c, "ClosedCase", "CLOSED_AS", "Pattern", ccs[["id", "pattern"]], "id", "pattern")
    policy_graph(c)


PATTERN_RULES = {
    "card_testing": ["R5"], "card_not_present_fraud": ["R1", "R2", "R3", "R4"], "card_not_present_new_device": ["R1", "R2", "R3", "R4"],
    "out_of_region_use": ["R2", "R3"], "account_takeover": ["R2", "R8"], "undocumented": ["R6", "R9"], "none": ["R3", "R7"],
}
RULE_ACTIONS = {
    "R1": ["VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"], "R2": ["BLOCK_CARD", "CREATE_CASE", "FILE_REPORT"], "R3": ["CLOSE_NO_FRAUD"],
    "R4": ["MONITOR_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST"], "R5": ["DECLINE_TRANSACTION", "STEP_UP_AUTH", "BLOCK_CARD"],
    "R6": ["CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"], "R7": ["CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"],
    "R8": ["ESCALATE_TO_ANALYST"], "R9": ["CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"],
}


def policy_graph(c):
    import policy
    for p, rules in PATTERN_RULES.items():
        c.upsertVertex("Pattern", p, {})
        for r in rules:
            c.upsertEdge("Pattern", p, "TRIGGERS", "PolicyRule", r)
    for r, acts in RULE_ACTIONS.items():
        c.upsertVertex("PolicyRule", r, {})
        for a in acts:
            c.upsertVertex("PolicyAction", a, {"route": policy.route(a, 0)})
            c.upsertEdge("PolicyRule", r, "REQUIRES", "PolicyAction", a)
    print("  policy graph loaded")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = tg.conn()
    if what in ("schema", "all"):
        gsql_file(c, "schema.gsql")
        c = tg.conn()
    if what in ("data", "all"):
        data(c)
    if what == "cases":
        closed_cases(c)
    if what in ("queries", "all"):
        gsql_file(c, "queries.gsql")
