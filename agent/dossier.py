"""Print a compact investigation dossier for each case. Used to hand-check cases
before encoding the decision logic."""
import sys

import duckdb

from tools import DB, Tools

con = duckdb.connect(DB, read_only=True)
cases = con.execute("select * from cp order by case_id").fetchall()
cols = [d[0] for d in con.description]

only = set(sys.argv[1:])
for row in cases:
    c = dict(zip(cols, row))
    if only and c["case_id"] not in only:
        continue
    tl = Tools(c["opened_at"])
    f = tl.txn(c["flagged_txn_id"])
    print("=" * 100)
    print(c["case_id"], c["trigger_type"], c["card_id"], "opened", c["opened_at"], "| flagged card in data:", f["card_id"])
    print(f"FLAG {f['tid']} {f['ts']} ${f['amt']} {f['pcd']} {f['channel']} addr1={f['addr1']} addr2={f['addr2']} dist1={f['dist1']} pe={f['pe']} re={f['re']} rs={f['rs']}")
    print(f"     dev={f['dev']} id_15={f['id_15']} proxy={f['id_23']} M4-6={f['M4']},{f['M5']},{f['M6']}")
    h = tl.card_history(c["card_id"])
    print(f"BASE n={h['n']} med=${h['med_amt']} p95=${h['p95_amt']} max=${h['max_amt']} home={h['home_region']} regions={h['regions']} products={h['products']} devices={h['n_devices']}")
    print("CARDS", [(x["card_id"], x["n"]) for x in tl.customer_cards(c["customer_id"])])
    print("WINDOW (-72h..+48h):")
    for w in tl.card_window(c["card_id"], f["ts"]):
        mark = "*" if w["tid"] == f["tid"] else " "
        print(f"  {mark}{w['tid']} {w['ts']} ${w['amt']:>8.2f} {w['pcd']} {w['channel'][:2]} a1={w['addr1']} rs={w['rs']:.2f} {w['id_15'] or ''} {w['id_23'] or ''} {(w['dev'] or '')[:60]}")
    if f["dev"]:
        g = tl.device_global(f["dev"])
        nb = tl.device_neighbors(f["dev"], f["ts"])
        print(f"DEVICE global cards={g['cards']} n={g['n']} {g['f']}..{g['l']}; 30d neighbours={len(nb)}")
        for x in nb[:12]:
            print(f"   {x['card_id']} n={x['n']} ${x['total']:.2f} {x['first_ts']}..{x['last_ts']} rs={x['avg_rs']:.2f}")
    if f["addr1"] is not None:
        ra = tl.region_activity(f["addr1"], f["ts"])
        new = [x for x in ra if x["prior_in_region"] == 0]
        print(f"REGION {f['addr1']} ±48h cards={len(ra)} new-to-region cards={len(new)}")
    pc = tl.prior_cases_for(c["customer_id"], [c["card_id"]])
    for p in pc[:5]:
        print(f"PRIOR {p['case_id']} {p['card_id']} {p['opened_at']} {p['outcome']} {p['pattern']} ${p['exposure_usd']}")
