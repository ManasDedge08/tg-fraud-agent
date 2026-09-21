"""Train the fraud-probability scorer on closed-case outcomes.

Labels come only from closed_cases_history (July-October): transactions in confirmed
fraud cases are positive, cleared alerts and uninvestigated transactions are negative.
Features are the Vesta columns (used as unnamed signals) plus graph-derived ones:
the client uid (card1 + billing region + account start day), how many cards share the
device profile, and how new the device and region are to the card.

Validated on October (trained on July-September), then refit on all four months.
Scores every transaction so the agent's tools can read `p_fraud` like any attribute.
"""
import os

import duckdb
from sklearn.ensemble import HistGradientBoostingClassifier
import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, roc_auc_score

DB = os.path.join(os.path.dirname(__file__), "..", "data", "hh.duckdb")
con = duckdb.connect(DB)

con.execute("""
create or replace table labels as
select tid, max(y) y from (
  select unnest(string_split(txn_ids::varchar,'|'))::bigint tid, (outcome='confirmed_fraud')::int y from cc
) group by tid
""")

df = con.execute("""
with u as (
  select x.*, x.card1||'_'||coalesce(x.addr1::varchar,'na')||'_'||(floor(x.TransactionDT/86400)-coalesce(x.D1,0))::int uid,
         t.dev, t.card_id
  from tx x join t on t.tid=x.TransactionID
),
g as (
  select *,
    count(*) over (partition by uid) uid_n,
    avg(TransactionAmt) over (partition by uid) uid_amt_mean,
    TransactionAmt / nullif(avg(TransactionAmt) over (partition by card_id),0) amt_vs_card,
    count(*) over (partition by dev) dev_n,
    count(*) over (partition by card_id, ProductCD) card_pcd_n,
    count(*) over (partition by card_id, addr1) card_region_n
  from u
)
select g.*, i.* exclude (TransactionID), l.y
from g left join idn i on i.TransactionID=g.TransactionID
left join labels l on l.tid=g.TransactionID
""").df()

import pandas as pd
cat_cols = [c for c in df.columns if not (pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c])) and c not in ("uid", "ts", "customer_id", "card_id", "dev")]
for c in cat_cols:
    df[c] = df[c].astype("category").cat.codes.replace(-1, np.nan)
for c in df.columns:
    if pd.api.types.is_bool_dtype(df[c]):
        df[c] = df[c].astype(float)
drop = {"TransactionID", "TransactionDT", "ts", "customer_id", "card_id", "uid", "dev", "y", "risk_score", "card1"}
feats = [c for c in df.columns if c not in drop]

month = df["ts"].dt.month
labeled_period = month <= 10
df["y"] = df["y"].fillna(0).astype(int)

def fit(X, y):
    return HistGradientBoostingClassifier(learning_rate=0.08, max_iter=400, max_leaf_nodes=63,
                                          min_samples_leaf=50, l2_regularization=1.0, random_state=7).fit(X, y)

tr, va = labeled_period & (month <= 9), month == 10
m = fit(df.loc[tr, feats], df.loc[tr, "y"])
pv = m.predict_proba(df.loc[va, feats])[:, 1]
print("Oct AUC", roc_auc_score(df.loc[va, "y"], pv), "AP", average_precision_score(df.loc[va, "y"], pv))
print("risk_score AUC", roc_auc_score(df.loc[va, "y"], df.loc[va, "risk_score"]))

iso = IsotonicRegression(out_of_bounds="clip").fit(pv, df.loc[va, "y"])

m = fit(df.loc[labeled_period, feats], df.loc[labeled_period, "y"])
raw = m.predict_proba(df[feats])[:, 1]
df["p_model"] = iso.predict(raw)
df["raw_model"] = raw


con.register("s", df[["TransactionID", "raw_model", "p_model", "uid", "uid_n", "dev_n"]])
con.execute("create or replace table scores as select * from s")
con.execute("create or replace table t as select t.* exclude (raw_model, p_model, uid, uid_n, dev_n) , s.raw_model, s.p_model, s.uid, s.uid_n, s.dev_n from t join scores s on s.TransactionID=t.tid"
            if "p_model" in [r[0] for r in con.execute("describe t").fetchall()] else
            "create or replace table t as select t.*, s.raw_model, s.p_model, s.uid, s.uid_n, s.dev_n from t join scores s on s.TransactionID=t.tid")
print(con.execute("select cp.case_id, round(t.raw_model,3), round(t.p_model,3), t.uid_n from cp join t on t.tid=cp.flagged_txn_id order by 1").fetchall())
