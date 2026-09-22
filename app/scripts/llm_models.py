"""Role-based LLM model selection from the cloud repertoire.

Reads /home/ralph/trader-v2/data/cloud_models.json and returns the right
model name for a given task. Falls back to the legacy local model if
cloud is unavailable.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

REPERTOIRE = Path('/home/ralph/trader-v2/data/cloud_models.json')

# Default per-role model names. Picked for the trader-v2 workload.
DEFAULT_BY_ROLE = {
    'decision':   'gpt-oss:120b:cloud',    # reasoning, chain-of-thought
    'narrative':  'kimi-k2.6:cloud',        # already in use; good prose
    'code':       'kimi-k2.7-code:cloud',   # code-specialized
    'analysis':   'deepseek-v4-flash:cloud',
    'fast':       'gemma4:31b:cloud',
}

LEGACY_FALLBACK = 'qwen2.5:7b'  # local; works when cloud is unreachable


@lru_cache(maxsize=1)
def _load_repertoire():
    if not REPERTOIRE.exists():
        return {}
    try:
        with open(REPERTOIRE) as f:
            return json.load(f).get('models', {})
    except Exception:
        return {}


def model_for(role: str) -> str:
    """Return the cloud model name for a given role.

    role: one of decision, narrative, code, analysis, fast.
    """
    rep = _load_repertoire()
    name = DEFAULT_BY_ROLE.get(role, LEGACY_FALLBACK)
    if name in rep:
        return name
    return LEGACY_FALLBACK


def list_models() -> dict:
    """Return the full repertoire dict for diagnostics."""
    return _load_repertoire()
