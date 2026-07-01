"""HSE Document RAG — Streamlit app.

Run:  streamlit run app.py
"""
import uuid
import streamlit as st

from config import load_config, save_config
import bootstrap_ollama as boot
import graph_client as gc
import vectorstore as vs
import ingest
import rag

st.set_page_config(page_title="HSE Document Assistant", page_icon="🦺", layout="wide")


# ------------------------------------------------------------------ helpers
def config_ready(cfg):
    return not any(str(cfg.get(k, "")).startswith("<") for k in
                   ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "SITE_URL"))


@st.cache_resource(show_spinner=False)
def bootstrap(llm, embed_model, do_restore):
    logs = []
    boot.bootstrap(llm, embed_model, log=logs.append)
    if do_restore:
        try:
            logs.append(vs.restore_from_sharepoint())
        except Exception as e:
            logs.append(f"Restore skipped: {e}")
    else:
        vs.ensure_collection()
    return logs


# ------------------------------------------------------------------ auth (delegated, browser)
def handle_auth():
    cfg = load_config()
    qp = st.query_params
    if "code" in qp and "token" not in st.session_state:
        try:
            token, username, exp = gc.redeem_code(qp["code"])
            st.session_state.token = token
            st.session_state.username = username
            st.session_state.token_exp = exp
        except Exception as e:
            st.session_state.auth_error = str(e)
        st.query_params.clear()
        st.rerun()


def sign_in_widget():
    cfg = load_config()
    if st.session_state.get("username"):
        st.success(f"Signed in as {st.session_state['username']}")
        if st.button("Sign out"):
            for k in ("token", "username", "token_exp"):
                st.session_state.pop(k, None)
            st.rerun()
        return
    state = st.session_state.setdefault("oauth_state", uuid.uuid4().hex)
    try:
        url = gc.build_auth_url(state)
        st.link_button("🔐 Sign in with Microsoft", url, type="primary")
    except Exception as e:
        st.error(f"Cannot build sign-in URL: {e}")
    st.caption("Sign in so answers respect the documents you're allowed to see in SharePoint.")
    if st.session_state.get("auth_error"):
        st.error(st.session_state.pop("auth_error"))


# ------------------------------------------------------------------ chat tab
def run_query(q):
    st.session_state.messages.append({"role": "user", "content": q})
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        info = rag.prepare_answer(q, st.session_state.get("token"))
        if info["mode"] == "direct":
            st.markdown(info["text"])
            st.session_state.messages.append({"role": "assistant", "content": info["text"]})
        else:
            text = st.write_stream(rag.stream_tokens(info["prompt"]))
            tail = rag.references_md(info["references"], info["blocked"])
            if tail:
                st.markdown(tail)
            st.session_state.messages.append({"role": "assistant", "content": text + tail})


def chat_tab():
    cfg = load_config()
    st.session_state.setdefault("messages", [])

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    if not st.session_state.messages:
        st.markdown("#### Suggested questions")
        cols = st.columns(2)
        for i, s in enumerate(cfg["SUGGESTED"]):
            if cols[i % 2].button(s, key=f"sug_{i}", use_container_width=True):
                run_query(s)
                st.rerun()

    q = st.chat_input("Ask about HSE procedures, standards, PPE, permits, incidents…")
    if q:
        run_query(q)
        st.rerun()


# ------------------------------------------------------------------ admin tab
def admin_tab():
    cfg = load_config()

    if cfg["ADMIN_PASSWORD"]:
        if not st.session_state.get("is_admin"):
            pw = st.text_input("Admin password", type="password")
            if st.button("Unlock"):
                st.session_state.is_admin = (pw == cfg["ADMIN_PASSWORD"])
                st.rerun()
            if not st.session_state.get("is_admin"):
                st.stop()

    st.subheader("Site")
    st.write(f"Indexing **{cfg['SITE_URL']}** and all its child folders.")
    libs = st.text_input("Restrict to libraries (comma-separated, blank = all)")
    if st.button("🔄 Sync site (crawl + index)"):
        libraries = [x.strip() for x in libs.split(",") if x.strip()] or None
        area = st.empty()
        buf = []
        def log(m):
            buf.append(str(m))
            area.code("\n".join(buf[-200:]))
        with st.spinner("Crawling and indexing…"):
            try:
                ingest.sync_site(cfg["SITE_URL"], libraries=libraries, log=log)
            except Exception as e:
                buf.append(f"ERROR: {e}")
                area.code("\n".join(buf[-200:]))

    st.divider()
    st.subheader("Upload documents")
    files = st.file_uploader("Files (uploaded to SharePoint + indexed)",
                             accept_multiple_files=True)
    if st.button("⬆️ Upload + index") and files:
        buf = []
        area = st.empty()
        tok = gc.get_app_token()
        site = gc.resolve_site(cfg["SITE_URL"], tok)
        drv = gc.drive_by_name(site["id"], tok, cfg["PERSIST_LIBRARY"])
        for f in files:
            data = f.read()
            try:
                item = gc.upload_item(site["id"], drv["id"], cfg["PERSIST_FOLDER"],
                                      f.name, data, tok)
                ingest.index_document(site["id"], drv["id"], item, cfg["SITE_URL"],
                                      log=buf.append)
            except Exception as e:
                buf.append(f"ERROR {f.name}: {e}")
            area.code("\n".join(buf[-200:]))
        vs.backup_to_sharepoint()
        buf.append("Done + backed up.")
        area.code("\n".join(buf[-200:]))

    st.divider()
    st.subheader("Add a document by direct URL")
    st.caption("A SharePoint/OneDrive link stays access-checked per user; any other public "
               "URL is indexed as **public** (visible to everyone).")
    url = st.text_input("Document URL")
    if st.button("🔗 Add + index URL") and url:
        buf = []
        area = st.empty()
        try:
            n = ingest.index_by_url(url, log=buf.append)
            if n:
                vs.backup_to_sharepoint()
                buf.append("Done + backed up.")
        except Exception as e:
            buf.append(f"ERROR: {e}")
        area.code("\n".join(buf))

    st.divider()
    st.subheader("Indexed documents")
    reg = ingest.REGISTRY
    st.write(f"{len(reg)} documents · {vs.count()} vectors")
    if reg:
        rows = [{"Name": v["name"], "Access": v.get("access", "graph"),
                 "Chunks": v.get("n_chunks", 0), "item_id": k} for k, v in reg.items()]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        to_del = st.selectbox("Delete a document",
                              options=[""] + [r["item_id"] for r in rows],
                              format_func=lambda x: "" if not x else
                              next((r["Name"] for r in rows if r["item_id"] == x), x))
        if st.button("🗑️ Delete document + vectors") and to_del:
            ingest.delete_document(to_del)
            st.success("Deleted.")
            st.rerun()

    st.divider()
    st.subheader("Model")
    models = boot.list_models()
    current = cfg["LLM_MODEL"]
    choice = st.selectbox("LLM model", options=models or [current],
                          index=(models.index(current) if current in models else 0))
    col1, col2 = st.columns(2)
    if col1.button("Use selected model"):
        cfg["LLM_MODEL"] = choice
        save_config(cfg)
        st.success(f"LLM set to {choice}")
    new_model = col2.text_input("Pull new model (e.g. llama3.1:8b)")
    if col2.button("Pull model") and new_model:
        area = st.empty()
        buf = []
        def log(m):
            buf.append(str(m))
            area.code("\n".join(buf[-40:]))
        with st.spinner(f"Pulling {new_model}…"):
            boot.pull_model(new_model, log=log)

    st.divider()
    st.subheader("Suggested questions")
    sug = st.text_area("One per line", value="\n".join(cfg["SUGGESTED"]), height=140)
    if st.button("Save suggestions"):
        cfg["SUGGESTED"] = [l.strip() for l in sug.splitlines() if l.strip()]
        save_config(cfg)
        st.success("Saved.")

    st.divider()
    st.subheader("Vector store")
    c1, c2 = st.columns(2)
    if c1.button("Backup → SharePoint"):
        st.info(vs.backup_to_sharepoint())
    if c2.button("Restore ← SharePoint"):
        st.info(vs.restore_from_sharepoint())


# ------------------------------------------------------------------ main
def main():
    cfg = load_config()
    st.title("🦺 HSE Document Assistant")

    if not config_ready(cfg):
        st.warning("Configuration is incomplete. Edit `~/hse_rag_config.json` "
                   "(TENANT_ID, CLIENT_ID, CLIENT_SECRET, SITE_URL) and reload.")
        st.stop()

    handle_auth()
    try:
        with st.spinner("Starting Ollama and loading models (first run downloads them)…"):
            bootstrap(cfg["LLM_MODEL"], cfg["EMBED_MODEL"], config_ready(cfg))
    except Exception as e:
        st.error(f"LLM backend unavailable: {e}")
        st.info("This app must run where Ollama is reachable — e.g. your Windows machine. "
                "Streamlit Community Cloud cannot run a local LLM. Point OLLAMA_URL at a "
                "reachable endpoint, or run the app on the host where Ollama runs.")
        st.stop()

    with st.container():
        sign_in_widget()

    chat, admin = st.tabs(["💬 Chat", "⚙️ Admin"])
    with chat:
        chat_tab()
    with admin:
        admin_tab()


if __name__ == "__main__":
    main()
