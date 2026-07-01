# HSE Document Assistant — Streamlit

RAG over a SharePoint site, with per-user access trimming, a local Qdrant store backed up
to SharePoint, and portable Ollama (no admin install). Converted from the Jupyter notebook.

## Files
- `app.py` — Streamlit UI (Chat + Admin), browser sign-in, streaming answers
- `config.py` — settings (JSON in your home dir; secrets can come from env vars)
- `bootstrap_ollama.py` — portable Ollama install / serve / model management
- `graph_client.py` — MSAL auth (app-only + delegated auth-code) + SharePoint/Graph
- `vectorstore.py` — Qdrant + embeddings + SharePoint backup/restore
- `ingest.py` — parse / chunk / embed / index / delete (incremental via eTag)
- `rag.py` — retrieve → per-user trim → stream

## Setup

1. **Install deps**
   ```
   pip install -r requirements.txt
   ```

2. **App registration (Entra)**
   - API permissions (already granted in your app): `Sites.Selected` (Application),
     `Sites.Selected` (Delegated), `User.Read` (Delegated).
   - Add a **Web** redirect URI: `http://localhost:8501` (must match `REDIRECT_URI`).
   - Ensure a **client secret** exists.
   - **Grant the app to your site once** (admin): call
     `graph_client.grant_app_to_site("https://…/sites/HSE", "read")` or use PnP.

3. **Configure** — first run creates `~/hse_rag_config.json`. Fill in `TENANT_ID`,
   `CLIENT_ID`, `CLIENT_SECRET`, `SITE_URL`. In production, set secrets via env vars
   `HSE_TENANT_ID`, `HSE_CLIENT_ID`, `HSE_CLIENT_SECRET` instead of on disk.

4. **Run**
   ```
   streamlit run app.py
   ```
   First launch downloads Ollama + the models (one-time).

## How it differs from the notebook
- **Real browser sign-in** (MSAL authorization-code flow) instead of device code, so each
  user gets their own delegated token — the per-user access checks now work for a
  multi-user deployment, not just a single admin.
- Streamlit execution model handled with `@st.cache_resource` (Ollama/Qdrant start once)
  and `st.session_state` (per-user token + chat history).
- Same fixed ingestion: paginated crawler (no 200-item cap), 429 retry, batch embeddings,
  resumable >4 MB backup upload, incremental eTag skipping.

## Still to decide for production (unchanged from the notebook review)
- **Qdrant embedded** is single-writer per process. For real concurrency run Qdrant as a
  service and keep SharePoint as cold backup only.
- **Scanned PDFs** need an OCR fallback (Tesseract/`ocrmypdf`) or they index as 0 chunks.
- Put Streamlit behind HTTPS and your identity provider; rotate the client secret.
