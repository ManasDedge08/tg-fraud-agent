"""Build the local DuckDB mirror from the dataset CSVs: raw tables, the derived card_id,
the device-profile key, and the slim `t` table the tools read. Run before train_scorer.py."""
import os

import duckdb

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
SRC = os.path.join(ROOT, "HHGOA_IEEE")
con = duckdb.connect(os.path.join(ROOT, "hh.duckdb"))

for name, f in [("tx", "transactions.csv"), ("idn", "identity.csv"), ("cc", "closed_cases_history.csv"), ("cp", "case_pack.csv")]:
    con.execute(f"create or replace table {name} as select * from read_csv_auto('{os.path.join(SRC, f)}', sample_size=-1)")

# card_id is not a column: customer + rank of (network, type) reproduces every closed-case card ID
con.execute("""create or replace table cardmap as
  select customer_id, card4, card6,
         customer_id||'-K'||row_number() over (partition by customer_id order by card4 nulls first, card6 nulls first) card_id
  from (select distinct customer_id, card4, card6 from tx)""")

con.execute("""create or replace table t as
  select m.card_id, x.TransactionID tid, x.ts, x.TransactionAmt amt, x.ProductCD pcd, x.channel, x.addr1, x.addr2, x.dist1, x.dist2,
         x.P_emaildomain pe, x.R_emaildomain re, x.risk_score rs, x.customer_id, x.card4, x.card6,
         x.M1,x.M2,x.M3,x.M4,x.M5,x.M6,x.M7,x.M8,x.M9, x.C1,x.C2,x.C5,x.C13,x.C14,x.D1,x.D10,x.D15,
         i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33, i.id_34,
         case when i.TransactionID is not null then
           coalesce(i.DeviceInfo,'?')||' | '||coalesce(i.id_30,'?')||' | '||coalesce(i.id_31,'?')||' | '||coalesce(i.id_33,'?') end dev
  from tx x
  join cardmap m on m.customer_id=x.customer_id and m.card4 is not distinct from x.card4 and m.card6 is not distinct from x.card6
  left join idn i on i.TransactionID=x.TransactionID""")

check = con.execute("""select avg((m.card_id=cc.card_id)::int) from cc join tx x on x.TransactionID=cc.first_fraud_txn_id
                       join cardmap m on m.customer_id=x.customer_id and m.card4 is not distinct from x.card4
                       and m.card6 is not distinct from x.card6""").fetchone()[0]
print(f"card_id reproduces closed-case card IDs: {check:.1%}")
