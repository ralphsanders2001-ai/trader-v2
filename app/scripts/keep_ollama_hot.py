#!/usr/bin/env python3
"""
Ollama Keep-Hot Script

Pre-loads ML/LLM models into VRAM and keeps them hot indefinitely.
Runs at boot via systemd to ensure models are available before
the trader daemon starts.

Retries on connection failure (Ollama may not be ready at boot).
"""
import sys
import time
import requests

OLLAMA_URL = "http://192.168.88.7:11434"
OLLAMA_READY_TIMEOUT = 60  # seconds

# Models to keep hot
MODELS = [
    "phi4-mini:3.8b",    # Primary LLM for decision layer
    "llama3.2:3b",       # Backup LLM (smaller, faster)
]


def wait_for_ollama(timeout=OLLAMA_READY_TIMEOUT):
    """Wait for Ollama to be reachable."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def keep_hot(model):
    """Pre-load a model and pin it."""
    try:
        print(f"[keep-hot] Loading {model}...")
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": "hi",
                "stream": False,
                "keep_alive": -1,  # never unload
            },
            timeout=120,
        )
        resp.raise_for_status()
        print(f"[keep-hot] ✓ {model} loaded and pinned")
        return True
    except Exception as e:
        print(f"[keep-hot] ✗ {model} failed: {e}")
        return False


def check_status():
    """Check what's currently hot."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        loaded = [m["name"] for m in data.get("models", [])]
        return loaded
    except Exception:
        return []


if __name__ == "__main__":
    print(f"[keep-hot] Checking Ollama at {OLLAMA_URL}")

    # Wait for Ollama to be reachable (may not be ready at boot)
    if not wait_for_ollama():
        print(f"[keep-hot] Ollama not reachable after {OLLAMA_READY_TIMEOUT}s timeout")
        sys.exit(1)

    print(f"[keep-hot] Ollama is reachable")
    loaded = check_status()
    print(f"[keep-hot] Currently loaded: {loaded}")

    all_ok = True
    for model in MODELS:
        if model not in loaded:
            ok = keep_hot(model)
            if not ok:
                all_ok = False
        else:
            print(f"[keep-hot] ✓ {model} already loaded")

    if all_ok:
        print("[keep-hot] All models hot")
        sys.exit(0)
    else:
        print("[keep-hot] Some models failed to load")
        sys.exit(1)
