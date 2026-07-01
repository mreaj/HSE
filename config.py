"""Configuration for the HSE RAG app. Stored as JSON in the user's home dir.

Secrets note: TENANT_ID / CLIENT_ID / CLIENT_SECRET can be overridden by environment
variables (HSE_TENANT_ID, HSE_CLIENT_ID, HSE_CLIENT_SECRET) so you don't have to keep the
secret on disk in production.
"""
import os
import json

CONFIG_PATH = os.path.expanduser(os.path.join("~", "hse_rag_config.json"))

DEFAULT_CONFIG = {
    # ---- Azure AD / Entra app registration ----
    "TENANT_ID":     "<your-tenant-id>",
    "CLIENT_ID":     "<your-app-client-id>",
    "CLIENT_SECRET": "<your-app-client-secret>",
    # Must be registered as a *Web* redirect URI on the app registration:
    "REDIRECT_URI":  "http://localhost:8501",

    # ---- The ONE SharePoint site this RAG indexes (its libraries + all subfolders) ----
    "SITE_URL": "<https://contoso.sharepoint.com/sites/HSE>",
    # Where admin uploads + the Qdrant backup live (same site):
    "PERSIST_LIBRARY": "Documents",
    "PERSIST_FOLDER":  "_hse_rag",

    # ---- Models (admin-selectable) ----
    "LLM_MODEL":   "mistral:7b",
    "EMBED_MODEL": "nomic-embed-text",

    # ---- Retrieval / generation ----
    "TOP_K": 6,
    "CHUNK_SIZE": 900,
    "CHUNK_OVERLAP": 150,

    # ---- Ingestion limits ----
    "MAX_FILE_MB": 50,
    "CHECKPOINT_EVERY": 200,

    # ---- Admin gate (empty = no password; set one for shared deployments) ----
    "ADMIN_PASSWORD": "",

    # ---- HSE suggested questions ----
    "SUGGESTED": [
        "What PPE is required for confined space entry?",
        "Summarise the permit-to-work procedure.",
        "How many documents are in the knowledge base?",
        "What does the standard say about working at height?",
        "What are the steps in incident reporting?",
    ],
}

_cache = {"mtime": None, "cfg": None}


def load_config():
    """Load config (cached by file mtime so admin edits are picked up but hot loops stay fast)."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None

    if _cache["cfg"] is not None and _cache["mtime"] == mtime:
        cfg = _cache["cfg"]
    elif os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        _cache["cfg"], _cache["mtime"] = cfg, mtime
    else:
        cfg = dict(DEFAULT_CONFIG)
        save_config(cfg)
        cfg = _cache["cfg"]

    # environment overrides for secrets
    cfg = dict(cfg)
    for env, key in (("HSE_TENANT_ID", "TENANT_ID"),
                     ("HSE_CLIENT_ID", "CLIENT_ID"),
                     ("HSE_CLIENT_SECRET", "CLIENT_SECRET"),
                     ("HSE_REDIRECT_URI", "REDIRECT_URI")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    _cache["cfg"] = dict(cfg)
    try:
        _cache["mtime"] = os.path.getmtime(CONFIG_PATH)
    except OSError:
        _cache["mtime"] = None
