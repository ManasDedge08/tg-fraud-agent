"""GraphRAG: text and graph retrieved together.

Corpus, all stored in TigerGraph with 384-d vectors (BAAI/bge-small-en-v1.5):
  - DocChunk: the fraud policy (one chunk per rule/section), the five known patterns,
    the dataset's "things to know", and FinCEN's SAR narrative guidance.
    Policy chunks are linked DocChunk -DESCRIBES_RULE-> PolicyRule and
    DocChunk -DESCRIBES_PATTERN-> Pattern, so the graph can reach them from a pattern.
  - ClosedCase.note_emb: every closed case's analyst notes, so case memory can be
    searched by meaning as well as by shared card or device.

Retrieval for a case (retrieve()):
  1. vector search (MCP tigergraph__search_top_k_similarity) over DocChunk and over
     closed-case notes, with a query written from the investigation's findings;
  2. graph expansion (installed query policy_docs): Pattern -> PolicyRule -> DocChunk
     for the pattern the agent identified;
  3. the union goes to the LLM as grounding, and each passage becomes a `document`
     evidence item with its section as the ref.

    python graphrag.py build   # schema change, embed, load (once)
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
README = os.path.join(ROOT, "data", "HHGOA_IEEE", "README.md")
FINCEN = os.path.join(ROOT, "data", "docs", "sar_guidance_narrative.pdf")
MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384

_embedder = None


def embed(texts):
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(MODEL)
    return [v.tolist() for v in _embedder.embed(list(texts))]


# --- corpus -------------------------------------------------------------------
PATTERN_KEYS = {"1": "card_testing", "2": "card_not_present_fraud", "3": "card_not_present_new_device",
                "4": "out_of_region_use", "5": "account_takeover"}


def corpus():
    """Return [(id, source, section, text, rule_ids, pattern_ids)]."""
    md = open(README).read()
    out = []
    # policy rules R1..R10
    for m in re.finditer(r"\*\*(R\d+)\. ([^*]+)\*\*(.+?)(?=\n\n)", md, re.S):
        rid, title, body = m.group(1), m.group(2).strip(), " ".join(m.group(3).split())
        out.append((f"POLICY-{rid}", "fraud_policy", f"Policy {rid}: {title}", f"{rid}. {title} {body}", [rid], []))
    # policy sections: actions, routing, 3a, 3b, 4-7
    pol = md[md.index("# Fraud Policy"): md.index("# Answer Format")]
    for m in re.finditer(r"### (\S+)\. ([^\n]+)\n(.+?)(?=\n### |\Z)", pol, re.S):
        num, title, body = m.group(1), m.group(2).strip(), m.group(3)
        if num == "3":
            continue  # rules are chunked individually above
        text = " ".join(re.sub(r"\|[-| ]+\|", " ", body).replace("|", " ").split())
        rules = sorted(set(re.findall(r"\bR\d+\b", text)))
        out.append((f"POLICY-S{num}", "fraud_policy", f"Policy §{num}: {title}", f"§{num} {title}. {text}"[:2400], rules, []))
    # five known patterns
    pat = md[md.index("## The five known fraud patterns"): md.index("## Regulatory references")]
    for m in re.finditer(r"\*\*(\d)\. ([^*]+)\*\*(.+?)(?=\n\n)", pat, re.S):
        n, title, body = m.group(1), m.group(2).strip(" ."), " ".join(m.group(3).split())
        out.append((f"PATTERN-{n}", "known_patterns", f"Known pattern {n}: {title}", f"{title}. {body}",
                    re.findall(r"\bR\d+\b", body), [PATTERN_KEYS[n]]))
    # things to know
    tk = md[md.index("## Things to know"): md.index("## Rules")]
    for i, b in enumerate(re.findall(r"- (.+)", tk)):
        out.append((f"GUIDE-{i + 1}", "dataset_guide", "Things to know", b.replace("**", ""), [], []))
    # FinCEN SAR narrative guidance, ~900-char chunks on paragraph boundaries
    from pypdf import PdfReader
    text = "\n".join(p.extract_text() or "" for p in PdfReader(FINCEN).pages)
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if len(p.split()) > 12]
    buf, k = "", 0
    for p in paras:
        if len(buf) + len(p) > 900 and buf:
            k += 1
            out.append((f"FINCEN-SAR-{k:02d}", "fincen_sar_narrative_guidance", "FinCEN SAR narrative guidance", buf, [], []))
            buf = ""
        buf += (" " if buf else "") + p
    if buf:
        k += 1
        out.append((f"FINCEN-SAR-{k:02d}", "fincen_sar_narrative_guidance", "FinCEN SAR narrative guidance", buf, [], []))
    return out


# --- build (schema + load) ------------------------------------------------------------
SCHEMA = f"""USE GRAPH Fraud
CREATE SCHEMA_CHANGE JOB graphrag_schema FOR GRAPH Fraud {{
  ADD VERTEX DocChunk (PRIMARY_ID id STRING, source STRING, section STRING, text STRING) WITH primary_id_as_attribute="true";
  ADD DIRECTED EDGE DESCRIBES_RULE (FROM DocChunk, TO PolicyRule) WITH REVERSE_EDGE="RULE_DOC";
  ADD DIRECTED EDGE DESCRIBES_PATTERN (FROM DocChunk, TO Pattern) WITH REVERSE_EDGE="PATTERN_DOC";
}}
RUN SCHEMA_CHANGE JOB graphrag_schema
DROP JOB graphrag_schema
CREATE SCHEMA_CHANGE JOB graphrag_vectors FOR GRAPH Fraud {{
  ALTER VERTEX DocChunk ADD VECTOR ATTRIBUTE emb(DIMENSION={DIM}, METRIC="COSINE");
  ALTER VERTEX ClosedCase ADD VECTOR ATTRIBUTE note_emb(DIMENSION={DIM}, METRIC="COSINE");
}}
RUN SCHEMA_CHANGE JOB graphrag_vectors
DROP JOB graphrag_vectors
"""

QUERY = """USE GRAPH Fraud
CREATE OR REPLACE QUERY policy_docs(VERTEX<Pattern> pat) {
  start = {pat};
  rules = SELECT r FROM start:x -(TRIGGERS>)- PolicyRule:r;
  via_rule = SELECT d FROM rules:r -(RULE_DOC>)- DocChunk:d;
  via_pattern = SELECT d FROM start:x -(PATTERN_DOC>)- DocChunk:d;
  docs = via_rule UNION via_pattern;
  PRINT docs[docs.section, docs.text];
}
INSTALL QUERY policy_docs
"""


def build(step="all"):
    import duckdb

    import mcp_graph
    import tg
    c = tg.conn()
    if step in ("all", "schema"):
        print(c.gsql(SCHEMA))
    g = mcp_graph.client()
    if step in ("all", "docs"):
        docs = corpus()
        vecs = embed(d[3] for d in docs)
        for i in range(0, len(docs), 50):
            batch = [{"vertex_id": d[0], "vector": v, "attributes": {"source": d[1], "section": d[2], "text": d[3]}}
                     for d, v in zip(docs[i:i + 50], vecs[i:i + 50])]
            r = g.call("tigergraph__upsert_vectors", graph_name="Fraud", vertex_type="DocChunk", vector_attribute="emb", vectors=batch)
            assert isinstance(r, dict) and r.get("success"), str(r)[:400]
        rule_edges = [(d[0], rid) for d in docs for rid in d[4]]
        pat_edges = [(d[0], p) for d in docs for p in d[5]]
        g.add_edges("DESCRIBES_RULE", "DocChunk", "PolicyRule", rule_edges)
        g.add_edges("DESCRIBES_PATTERN", "DocChunk", "Pattern", pat_edges)
        print(f"DocChunk: {len(docs)} chunks, {len(rule_edges)} rule links, {len(pat_edges)} pattern links")
    if step in ("all", "notes"):
        con = duckdb.connect(os.path.join(ROOT, "data", "hh.duckdb"), read_only=True)
        rows = con.execute("select case_id, analyst_notes from cc order by case_id").fetchall()
        vecs = embed(r[1] for r in rows)
        # bulk load goes over REST like the rest of the data load; retrieval uses MCP
        for i in range(0, len(rows), 500):
            c.upsertVertices("ClosedCase", [(r[0], {"note_emb": v}) for r, v in zip(rows[i:i + 500], vecs[i:i + 500])])
            print(f"  notes {min(i + 500, len(rows))}/{len(rows)}", flush=True)
        print(f"ClosedCase.note_emb: {len(rows)} notes")
    if step in ("all", "query"):
        print(c.gsql(QUERY))


# --- retrieval ---------------------------------------------------------------------------
def _hits(r):
    data = r.get("data", {}) if isinstance(r, dict) else {}
    res = data.get("results") or data.get("result") or data
    if isinstance(res, dict):
        res = res.get("results") or res.get("vertices") or list(res.values())
    out = []
    for h in res if isinstance(res, list) else []:
        if isinstance(h, dict) and "v_id" in h:
            out.append(h)
        elif isinstance(h, dict):  # {"v": [vertex, ...]} blocks from the vector search
            out += [v for blk in h.values() if isinstance(blk, list) for v in blk if isinstance(v, dict)]
    return out


def retrieve(inv, k_docs=5, k_cases=4):
    """Hybrid retrieval for one investigation. Returns (passages, similar_case_ids)."""
    import mcp_graph
    g = mcp_graph.client()
    findings = "; ".join(e["claim"] for e in inv.evidence if e["source"] != "customer")[:1500]
    query = (f"Card fraud investigation. Trigger: {inv.c['trigger_type']}. Pattern: {inv.pattern.replace('_', ' ')}. "
             f"Findings: {findings}")
    qv = embed([query])[0]
    passages, seen = [], set()

    inv.tl.calls.append("mcp:tigergraph__search_top_k_similarity(DocChunk.emb)")
    r = g.call("tigergraph__search_top_k_similarity", graph_name="Fraud", vertex_type="DocChunk",
               vector_attribute="emb", query_vector=qv, top_k=k_docs)
    for h in _hits(r):
        vid = h.get("v_id") or h.get("vertex_id") or h.get("id")
        a = h.get("attributes", h)
        if vid and vid not in seen:
            seen.add(vid)
            passages.append({"id": vid, "section": a.get("section", ""), "text": a.get("text", ""), "via": "vector",
                             "score": h.get("score") or h.get("distance")})

    inv.tl.calls.append("mcp:tigergraph__run_installed_query(policy_docs)")
    r = g.query("policy_docs", pat=inv.pattern)
    for blk in (r.get("data", {}).get("result") or []) if isinstance(r, dict) else []:
        for v in blk.get("docs", []):
            if v["v_id"] not in seen:
                seen.add(v["v_id"])
                a = v.get("attributes", {})
                passages.append({"id": v["v_id"], "section": a.get("docs.section", a.get("section", "")),
                                 "text": a.get("docs.text", a.get("text", "")), "via": "graph"})

    inv.tl.calls.append("mcp:tigergraph__search_top_k_similarity(ClosedCase.note_emb)")
    r = g.call("tigergraph__search_top_k_similarity", graph_name="Fraud", vertex_type="ClosedCase",
               vector_attribute="note_emb", query_vector=qv, top_k=k_cases)
    cases = [h.get("v_id") or h.get("vertex_id") or h.get("id") for h in _hits(r)]
    return passages, [c for c in cases if c]


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    step = sys.argv[2] if len(sys.argv) > 2 else "all"
    if sys.argv[1:2] == ["build"]:
        build(step)
    elif sys.argv[1:2] == ["corpus"]:
        for d in corpus():
            print(d[0], "|", d[2], "|", len(d[3]), d[4], d[5])
