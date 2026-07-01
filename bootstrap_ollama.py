"""Portable Ollama: install (no admin rights), serve, and manage models.

Windows-focused (matches the target admin machine). On macOS/Linux, install Ollama the
normal way and this module will just detect/serve the existing binary.
"""
import os
import ssl
import json
import time
import shutil
import zipfile
import subprocess
import urllib.request

import certifi

# SSL fix for restricted / corporate networks
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

BASE_DIR = os.path.expanduser(os.path.join("~", "ollama"))
ZIP_PATH = os.path.expanduser(os.path.join("~", "ollama.zip"))
MODELS_DIR = os.path.expanduser(os.path.join("~", "ollama_models"))
OLLAMA_URL = "http://localhost:11434"

_state = {"exe": None}


def _find_exe():
    if os.path.exists(BASE_DIR):
        for root, _, files in os.walk(BASE_DIR):
            for f in files:
                if f.lower() in ("ollama.exe", "ollama"):
                    return os.path.join(root, f)
    return shutil.which("ollama")


def install_ollama(log=print):
    exe = _find_exe()
    if exe:
        _state["exe"] = exe
        log(f"Ollama already available: {exe}")
        return exe

    log("Looking up latest Ollama release...")
    req = urllib.request.Request(
        "https://api.github.com/repos/ollama/ollama/releases/latest",
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        release = json.loads(resp.read())
    tag = release["tag_name"]
    log(f"Latest version: {tag}")

    download_url = None
    for asset in release.get("assets", []):
        if asset["name"].lower() == "ollama-windows-amd64.zip":
            download_url = asset["browser_download_url"]
            break
    if not download_url:
        download_url = (f"https://github.com/ollama/ollama/releases/download/"
                        f"{tag}/ollama-windows-amd64.zip")

    log("Downloading Ollama...")
    req2 = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req2, timeout=300) as resp, open(ZIP_PATH, "wb") as f:
        shutil.copyfileobj(resp, f)
    log("Extracting...")
    os.makedirs(BASE_DIR, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "r") as z:
        z.extractall(BASE_DIR)
    os.remove(ZIP_PATH)

    exe = _find_exe()
    if not exe:
        raise FileNotFoundError("ollama binary not found after extraction")
    _state["exe"] = exe
    log(f"Ollama installed: {exe}")
    return exe


def _env():
    env = dict(os.environ)
    env["PATH"] = BASE_DIR + os.pathsep + env.get("PATH", "")
    env["OLLAMA_MODELS"] = MODELS_DIR
    os.makedirs(MODELS_DIR, exist_ok=True)
    return env


def is_running():
    try:
        urllib.request.urlopen(OLLAMA_URL, timeout=2)
        return True
    except Exception:
        return False


def ensure_serving(log=print):
    if is_running():
        return
    subprocess.Popen("ollama serve", shell=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())
    for _ in range(20):
        time.sleep(1)
        if is_running():
            log("Ollama server ready.")
            return
    raise RuntimeError("Ollama server did not start")


def list_models():
    r = subprocess.run("ollama list", shell=True, capture_output=True, text=True, env=_env())
    names = []
    for line in r.stdout.splitlines()[1:]:
        if line.strip():
            names.append(line.split()[0])
    return names


def pull_model(model, log=print):
    """Pull a model, streaming progress lines to `log`. Returns True on success."""
    if model.split(":")[0] in " ".join(list_models()):
        log(f"{model} already present")
        return True
    log(f"Pulling {model} ...")
    proc = subprocess.Popen(f"ollama pull {model}", shell=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", bufsize=1, env=_env())
    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    ok = proc.returncode == 0
    log(f"{model} ready!" if ok else f"Failed to pull {model}")
    return ok


def bootstrap(llm_model, embed_model, log=print):
    """One-time: install + serve + ensure both models are present."""
    install_ollama(log)
    ensure_serving(log)
    pull_model(embed_model, log)
    pull_model(llm_model, log)
    log("Bootstrap complete.")
    return True
