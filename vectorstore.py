"""Local Qdrant (fast, in-process) + embeddings via Ollama + SharePoint backup/restore.

Concurrency note: this is a single-writer embedded store shared by one Streamlit process.
Reads (queries) are safe concurrently; writes/backups are serialised with a lock. For a
high-concurrency deployment, run Qdrant as a service instead and point the client at it.
"""
import os
import io
import json
import zipfile
import threading
import urllib.request

from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, VectorParams, PointStruct,
                                  Filter, FieldCondition, MatchValue)

from config import load_config
import graph_client as gc

QDRANT_PATH = os.path.expanduser(os.path.join("~", "hse_qdrant"))
COLLECTION = "hse_docs"
BACKUP_NAME = "qdrant_backup.zip"
OLLAMA_URL = "http://localhost:11434"
os.makedirs(QDRANT_PATH, exist_ok=True)

_lock = threading.RLock()
_state = {"client": None, "dim": None}


def get_client():
    if _state["client"] is None:
        _state["client"] = QdrantClient(path=QDRANT_PATH)
    return _state["client"]


def close_client():
    if _state["client"] is not None:
        _state["client"].close()
        _state["client"] = None


# ------------------------------------------------------------------ embeddings
def embed(texts):
    """Embed via Ollama; try the batch endpoint first, fall back to per-text."""
    if isinstance(texts, str):
        texts = [texts]
    cfg = load_config()
    try:
        body = json.dumps({"model": cfg["EMBED_MODEL"], "input": texts}).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        if data.get("embeddings"):
            return data["embeddings"]
    except Exception:
        pass
    out = []
    for t in texts:
        body = json.dumps({"model": cfg["EMBED_MODEL"], "prompt": t}).encode()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out.append(json.loads(r.read())["embedding"])
    return out


def ensure_collection():
    c = get_client()
    existing = [col.name for col in c.get_collections().collections]
    if COLLECTION in existing:
        return _state["dim"]
    dim = len(embed(["dimension probe"])[0])
    c.create_collection(COLLECTION,
                        vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    _state["dim"] = dim
    return dim


# ------------------------------------------------------------------ CRUD
def upsert(points):
    with _lock:
        get_client().upsert(collection_name=COLLECTION, points=points)


def delete_item(item_id):
    with _lock:
        get_client().delete(collection_name=COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="item_id", match=MatchValue(value=item_id))]))


def search(vector, limit):
    return get_client().query_points(COLLECTION, query=vector, limit=limit,
                                     with_payload=True).points


def count():
    try:
        return get_client().count(collection_name=COLLECTION).count
    except Exception:
        return 0


def make_point(vector, payload):
    import uuid
    return PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload)


# ------------------------------------------------------------------ SharePoint persistence
def backup_to_sharepoint():
    cfg = load_config()
    with _lock:
        close_client()  # flush + release locks before zipping
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(QDRANT_PATH):
                for f in files:
                    fp = os.path.join(root, f)
                    z.write(fp, os.path.relpath(fp, QDRANT_PATH))
        buf.seek(0)
        tok = gc.get_app_token()
        site = gc.resolve_site(cfg["SITE_URL"], tok)
        drv = gc.drive_by_name(site["id"], tok, cfg["PERSIST_LIBRARY"])
        gc.upload_item(site["id"], drv["id"], cfg["PERSIST_FOLDER"], BACKUP_NAME, buf.read(), tok)
        get_client()  # reopen
    return "Backed up Qdrant to SharePoint."


def restore_from_sharepoint():
    cfg = load_config()
    tok = gc.get_app_token()
    r = gc.download_backup(cfg["SITE_URL"], cfg["PERSIST_LIBRARY"],
                           cfg["PERSIST_FOLDER"], BACKUP_NAME, tok)
    if r.status_code != 200:
        ensure_collection()
        return "No backup found in SharePoint (fresh start)."
    with _lock:
        close_client()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extractall(QDRANT_PATH)
        get_client()
        ensure_collection()
    return "Restored Qdrant from SharePoint."
