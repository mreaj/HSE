"""Query engine: intent -> retrieve -> per-user security trim -> generate (streaming)."""
import json
import urllib.request

from config import load_config
import graph_client as gc
import vectorstore as vs
import ingest

OLLAMA_URL = "http://localhost:11434"

SYSTEM = ("You are an HSE (Health, Safety & Environment) assistant. "
          "Answer ONLY from the provided context. If the context is insufficient, say you "
          "don't have enough information in the indexed documents. Be precise and cite which "
          "document each fact comes from.")


def _count_intent(q):
    ql = q.lower()
    return any(k in ql for k in ["how many document", "number of document", "how many file",
                                 "document count", "how many standard"])


def prepare_answer(query, user_token):
    """Return a dict describing how to answer:
      {"mode": "direct", "text": ...}                      -> just show text
      {"mode": "rag", "prompt": ..., "references": [...], "blocked": [...]}
    """
    if _count_intent(query):
        reg = ingest.REGISTRY
        n = sum(1 for v in reg.values() if v.get("n_chunks", 0) > 0)
        sites = sorted({v.get("source", "") for v in reg.values()})
        return {"mode": "direct",
                "text": f"There are **{n} documents** indexed (across {len(sites)} source(s))."}

    cfg = load_config()
    qv = vs.embed(query)[0]
    hits = vs.search(qv, cfg["TOP_K"] * 2)
    if not hits:
        return {"mode": "direct", "text": "I don't have any indexed documents that cover this yet."}

    accessible, blocked, refs, cache = [], [], {}, {}
    for h in hits:
        p = h.payload
        key = p["item_id"]
        if p.get("access") == "public":
            ok = True
        else:
            if key not in cache:
                cache[key] = gc.user_can_access(user_token, p.get("drive_id"), key)
            ok = cache[key]
        if ok:
            accessible.append(p)
            refs[key] = {"name": p["name"], "web_url": p["web_url"]}
        else:
            blocked.append(p["name"])

    accessible = accessible[:cfg["TOP_K"]]
    blocked = sorted(set(blocked) - {r["name"] for r in refs.values()})

    if not accessible:
        if not user_token:
            return {"mode": "direct",
                    "text": ("The relevant documents require SharePoint access. Please **sign in** "
                             "(top of the page) so I can check your permissions.")}
        return {"mode": "direct",
                "text": ("The most relevant documents exist, but **you don't have access** to them "
                         "in SharePoint:\n" + "\n".join(f"- {n}" for n in blocked))}

    ctx = "\n\n".join(f"[{i+1}] (from: {p['name']})\n{p['chunk']}"
                      for i, p in enumerate(accessible))
    prompt = f"{SYSTEM}\n\nContext:\n{ctx}\n\nQuestion: {query}\n\nAnswer:"
    return {"mode": "rag", "prompt": prompt,
            "references": list(refs.values()), "blocked": blocked}


def stream_tokens(prompt):
    """Generator yielding incremental token strings from Ollama."""
    cfg = load_config()
    body = json.dumps({"model": cfg["LLM_MODEL"], "prompt": prompt, "stream": True}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.decode("utf-8").strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("response"):
                yield obj["response"]
            if obj.get("done"):
                break


def references_md(references, blocked):
    out = ""
    if references:
        out += "\n\n**References**\n" + "\n".join(
            f"- [{r['name']}]({r['web_url']})" if r["web_url"] else f"- {r['name']}"
            for r in references)
    if blocked:
        out += ("\n\n> ⚠️ Some related documents were **not used** because you don't have access "
                "to them in SharePoint: " + ", ".join(blocked))
    return out
