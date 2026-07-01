"""Microsoft Graph / SharePoint access.

Two identities:
  * APP-ONLY  (client credentials)  -> broad ingestion / crawling.
  * DELEGATED (authorization-code flow, per browser user) -> per-user security trimming.

The delegated flow is a real web sign-in: the app builds a Microsoft login URL, the user
signs in, Microsoft redirects back to REDIRECT_URI with a ?code=..., and we redeem it.
Register REDIRECT_URI as a *Web* redirect URI on the app registration.
"""
import time
import base64
import urllib.request
from urllib.parse import urlparse

import msal
import requests

from config import load_config

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]

_apps = {"conf": None, "conf_key": None}


def _authority():
    return f"https://login.microsoftonline.com/{load_config()['TENANT_ID']}"


def _conf_app():
    cfg = load_config()
    key = (cfg["CLIENT_ID"], cfg["TENANT_ID"])
    if _apps["conf"] is None or _apps["conf_key"] != key:
        _apps["conf"] = msal.ConfidentialClientApplication(
            cfg["CLIENT_ID"], authority=_authority(), client_credential=cfg["CLIENT_SECRET"])
        _apps["conf_key"] = key
    return _apps["conf"]


# ------------------------------------------------------------------ app-only token
def get_app_token():
    res = _conf_app().acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in res:
        raise RuntimeError(f"App token error: {res.get('error_description', res)}")
    return res["access_token"]


# ------------------------------------------------------------------ delegated (auth code)
def build_auth_url(state):
    cfg = load_config()
    return _conf_app().get_authorization_request_url(
        scopes=SCOPES, redirect_uri=cfg["REDIRECT_URI"], state=state)


def redeem_code(code):
    """Exchange an auth code for a user token. Returns (access_token, username, expires_at)."""
    cfg = load_config()
    res = _conf_app().acquire_token_by_authorization_code(
        code, scopes=SCOPES, redirect_uri=cfg["REDIRECT_URI"])
    if "access_token" not in res:
        raise RuntimeError(f"Sign-in failed: {res.get('error_description', res)}")
    username = res.get("id_token_claims", {}).get("preferred_username", "user")
    exp = time.time() + res.get("expires_in", 3600) - 120
    return res["access_token"], username, exp


# ------------------------------------------------------------------ HTTP helpers
def gget(url, token, **kw):
    return requests.get(url, headers={"Authorization": f"Bearer {token}"}, **kw)


def graph_get(url, token, tries=6, **kw):
    """GET with retry on 429 / 5xx (SharePoint throttles hard on large crawls)."""
    r = None
    for a in range(tries):
        r = gget(url, token, **kw)
        if r.status_code in (429, 503, 504) or 500 <= r.status_code < 600:
            wait = int(r.headers.get("Retry-After", min(2 ** a, 60)))
            time.sleep(min(wait, 60))
            continue
        return r
    return r


# ------------------------------------------------------------------ SharePoint
def resolve_site(site_url, token):
    p = urlparse(site_url)
    r = gget(f"{GRAPH}/sites/{p.netloc}:{p.path.rstrip('/')}", token)
    r.raise_for_status()
    return r.json()


def list_drives(site_id, token):
    r = gget(f"{GRAPH}/sites/{site_id}/drives", token)
    r.raise_for_status()
    return r.json().get("value", [])


def drive_by_name(site_id, token, library_name):
    for d in list_drives(site_id, token):
        if d["name"].lower() == library_name.lower():
            return d
    r = gget(f"{GRAPH}/sites/{site_id}/drive", token)
    r.raise_for_status()
    return r.json()


def iter_drive_files(site_id, drive_id, token, folder="root"):
    """Yield EVERY file in a drive, recursing folders.
    Paginates via @odata.nextLink (no 200-item cap) and recurses by folder id."""
    stack = [folder]
    while stack:
        fid = stack.pop()
        url = (f"{GRAPH}/sites/{site_id}/drives/{drive_id}/items/{fid}/children"
               "?$top=200&$select=id,name,file,folder,webUrl,size,eTag,lastModifiedDateTime")
        while url:
            r = graph_get(url, token)
            r.raise_for_status()
            data = r.json()
            for it in data.get("value", []):
                if it.get("folder"):
                    stack.append(it["id"])
                elif it.get("file"):
                    yield it
            url = data.get("@odata.nextLink")


def download_drive_item(drive_id, item_id, token):
    r = graph_get(f"{GRAPH}/drives/{drive_id}/items/{item_id}/content", token, allow_redirects=True)
    r.raise_for_status()
    return r.content


def encode_share_url(url):
    b = base64.b64encode(url.encode("utf-8")).decode("utf-8")
    return "u!" + b.rstrip("=").replace("/", "_").replace("+", "-")


def resolve_share(url, token):
    sid = encode_share_url(url)
    r = gget(f"{GRAPH}/shares/{sid}/driveItem?$select=id,name,webUrl,parentReference,file,eTag", token)
    r.raise_for_status()
    return r.json()


def http_fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "page"
    if "html" in ctype and not name.lower().endswith((".htm", ".html")):
        name += ".html"
    return data, name


def upload_item(site_id, drive_id, parent_path, filename, data, token):
    """Upload to SharePoint. Uses a resumable session for files > 4 MB (backup zips exceed this)."""
    path = f"{parent_path}/{filename}".strip("/")
    if len(data) < 4 * 1024 * 1024:
        url = f"{GRAPH}/sites/{site_id}/drives/{drive_id}/root:/{path}:/content"
        r = requests.put(url, headers={"Authorization": f"Bearer {token}"}, data=data)
        r.raise_for_status()
        return r.json()
    cs = requests.post(
        f"{GRAPH}/sites/{site_id}/drives/{drive_id}/root:/{path}:/createUploadSession",
        headers={"Authorization": f"Bearer {token}"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
    cs.raise_for_status()
    upload_url = cs.json()["uploadUrl"]
    CHUNK = 10 * 327680  # 3.2 MB (must be a multiple of 320 KiB)
    total = len(data)
    rr = None
    for start in range(0, total, CHUNK):
        end = min(start + CHUNK, total)
        chunk = data[start:end]
        rr = requests.put(upload_url, headers={
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{end - 1}/{total}"}, data=chunk)
        if rr.status_code not in (200, 201, 202):
            rr.raise_for_status()
    return rr.json() if rr is not None else {}


def download_backup(site_url, library, folder, name, token):
    site = resolve_site(site_url, token)
    drv = drive_by_name(site["id"], token, library)
    path = f"{folder}/{name}".strip("/")
    r = gget(f"{GRAPH}/sites/{site['id']}/drives/{drv['id']}/root:/{path}:/content",
             token, allow_redirects=True)
    return r


# ------------------------------------------------------------------ per-user access
def user_can_access(user_token, drive_id, item_id):
    """True only if the SIGNED-IN user can open the Graph item."""
    if not user_token or not drive_id:
        return False
    r = gget(f"{GRAPH}/drives/{drive_id}/items/{item_id}", user_token)
    return r.status_code == 200


def grant_app_to_site(site_url, role="read"):
    """One-time: grant THIS app access to a site (run by an admin)."""
    cfg = load_config()
    tok = get_app_token()
    site = resolve_site(site_url, tok)
    body = {"roles": [role],
            "grantedToIdentities": [{"application": {"id": cfg["CLIENT_ID"], "displayName": "HSE RAG"}}]}
    r = requests.post(f"{GRAPH}/sites/{site['id']}/permissions",
                      headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                      json=body)
    return r.status_code, r.text
