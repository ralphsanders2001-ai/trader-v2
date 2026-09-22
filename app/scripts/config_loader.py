"""
Hot-reloadable config loader.

The daemon monitors config.py's mtime and reloads on change.
Dashboard edits write to config.py → daemon picks up within 5s.
"""
import os
import sys
import time
import importlib

CONFIG_PATH = "/home/ralph/trader-v2/config.py"
sys.path.insert(0, "/home/ralph/trader-v2")
import config

_mtime = 0
_loaded_at = time.time()


def get_mtime():
    try:
        return os.stat(CONFIG_PATH).st_mtime
    except OSError:
        return 0


def reload_if_changed():
    """Reload config if file changed on disk. Returns True if reloaded."""
    global _mtime, config
    current = get_mtime()
    if current > _mtime:
        try:
            # FIX 2026-08-10: When using TRADER_CONFIG_FILE (two-bot split),
            # sys.modules['config'] might be a different file. Reload it
            # instead of the local 'config' reference.
            import sys as _sys
            cfg_module = _sys.modules.get('config', config)
            importlib.reload(cfg_module)
            _mtime = current
            return True
        except Exception as e:
            print(f"[CONFIG] Reload failed: {e}")
            return False
    return False


def get(key, default=None):
    """Get a config value with dotted-path support (e.g. 'POLL_TIERS.standard')."""
    parts = key.split(".")
    obj = config
    for p in parts:
        if isinstance(obj, dict):
            obj = obj.get(p)
        else:
            obj = getattr(obj, p, None)
        if obj is None:
            return default
    return obj


def set_value(key, value):
    """
    Update a value in config and persist to config.py.
    Handles dotted-path keys like 'POLL_TIERS.standard'.
    """
    global _mtime, config
    parts = key.split(".")
    obj = config
    for p in parts[:-1]:
        if isinstance(obj, dict):
            obj = obj[p]
        else:
            obj = getattr(obj, p)
    final_key = parts[-1]

    old_value = obj[final_key] if isinstance(obj, dict) else getattr(obj, final_key)

    # Set value (with type coercion if old was numeric/bool)
    if isinstance(old_value, bool):
        new_value = bool(value) if isinstance(value, (bool, int)) else str(value).lower() in ("true", "1", "yes")
    elif isinstance(old_value, int):
        new_value = int(value)
    elif isinstance(old_value, float):
        new_value = float(value)
    elif isinstance(old_value, list):
        # Allow list override
        if isinstance(value, list):
            new_value = value
        else:
            new_value = [v.strip() for v in str(value).split(",") if v.strip()]
    elif isinstance(old_value, dict):
        # 2026-09-16: allow whole-dict write when caller passes a dict
        # (e.g. SYMBOL_SETTINGS table). Dotted path still supported for subkeys.
        if isinstance(value, dict):
            new_value = value
        else:
            raise ValueError(f"Use dotted path for dict values, e.g. '{key}'")
    else:
        new_value = str(value)

    if isinstance(obj, dict):
        obj[final_key] = new_value
    else:
        setattr(obj, final_key, new_value)

    # Persist to disk (so next daemon restart keeps the change)
    _persist_to_file(key, new_value)

    # Update mtime to skip next reload
    _mtime = get_mtime()

    return old_value, new_value


def _write_config_verified(lines):
    """Write config.py ONLY if the result is valid Python syntax.

    Prevents a bad autosave from producing a config.py that fails to import
    (2026-09-14: invalid lines broke `import config`, which took down market
    data, dashboard P&L, and the daemon's hot-reload with it).
    """
    src = "".join(lines)
    try:
        compile(src, CONFIG_PATH, "exec")
    except SyntaxError as e:
        raise ValueError(
            f"refusing to write config.py (syntax check failed): {e}"
        )
    with open(CONFIG_PATH, "w") as f:
        f.writelines(lines)


def _persist_to_file(key, value):
    """Write key=value to config.py — MERGE IN PLACE (2026-09-16 rewrite).

    Root cause of 5x config.py truncations: earlier versions could rebuild the
    file from a partial key list. This version NEVER rewrites the file from
    memory: it re-reads the CURRENT lines from disk, replaces only the target
    key's expression (bracket-depth aware for lists/dicts), and writes back.
    Dotted keys route to _persist_dict_to_file (subkey rewrite, line-wise).
    """
    if "." in key:
        return _persist_dict_to_file(key, value)

    # Serialize the value as a python literal line
    if isinstance(value, str):
        line = f'{key} = "{value}"\n'
    elif isinstance(value, bool):
        line = f'{key} = {str(value)}\n'
    elif isinstance(value, (int, float)):
        line = f'{key} = {value}\n'
    elif isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            inner = ", ".join(f'"{v}"' for v in value)
        else:
            inner = ", ".join(str(v) for v in value)
        line = f'{key} = [{inner}]\n'
    elif isinstance(value, dict):
        line = f'{key} = {value!r}\n'
    else:
        line = f'{key} = {repr(value)}\n'

    import fcntl
    lock_f = open(CONFIG_PATH + ".lock", "w")
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    try:
        with open(CONFIG_PATH, "r") as f:
            lines = f.readlines()

        import re
        pattern = re.compile(rf"^(\s*)({re.escape(key)})(\s*)(=)(\s*)")
        found = False
        for i, ln in enumerate(lines):
            m = pattern.match(ln)
            if m:
                indent = m.group(1)
                # Replace the WHOLE bracketed expression (multi-line list/dict safe)
                if lines[i].rstrip().endswith(("[", "{")) or (key in ln and ln.rstrip().endswith(("[", "{"))):
                    open_ch = "[" if "[" in lines[i] and lines[i].rstrip().endswith("[") else "{"
                    close_ch = "]" if open_ch == "[" else "}"
                    depth = 0
                    j = i
                    while j < len(lines):
                        depth += lines[j].count(open_ch) - lines[j].count(close_ch)
                        if depth == 0:
                            break
                        j += 1
                    if j > i:
                        lines[i:j + 1] = [indent + line.lstrip()]
                        found = True
                        break
                lines[i] = indent + line.lstrip()
                found = True
                break

        if not found:
            # Append at end — file on disk is NEVER rebuilt from memory
            lines.append(f"\n# Auto-updated by dashboard\n{line}")

        _write_config_verified(lines)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _persist_dict_to_file(key, value):
    """
    Persist a single subkey in a dict value, e.g. 'POLL_TIERS.standard' = 10
    within POLL_TIERS = { "standard": 10 }.

    Rewrites the dict literal in place, LINE-WISE, so inline # comments on
    other rows survive. (2026-09-14: the old version split the body on ','
    which also split inside trailing comments and mangled config.py.)
    """
    parts = key.split(".")
    dict_name = parts[0]
    sub_key = parts[1]

    if isinstance(value, str):
        val_str = f'"{value}"'
    elif isinstance(value, bool):
        val_str = str(value)
    else:
        val_str = str(value)

    with open(CONFIG_PATH, "r") as f:
        lines = f.readlines()

    import re
    open_pat = re.compile(rf"^\s*({re.escape(dict_name)})\s*=\s*\{{")
    entry_pat = re.compile(
        r'^(\s*)"([^"]+)"(\s*:\s*)([^#{}]*?)(\s*,?)(\s*)(#.*)?$'
    )

    start = None
    for i, ln in enumerate(lines):
        if open_pat.match(ln):
            start = i
            break
    if start is None:
        raise ValueError(f"dict '{dict_name}' not found in config.py")

    end = None
    for j in range(start, len(lines)):
        if j > start and "}" in lines[j]:
            end = j
            break
    if end is None:
        raise ValueError(f"dict '{dict_name}' not terminated in config.py")

    # Update existing subkey row in place (keep indent, comma, comment)
    for j in range(start + 1, end):
        m = entry_pat.match(lines[j])
        if m and m.group(2) == sub_key:
            lines[j] = f'{m.group(1)}"{sub_key}"{m.group(3)}{val_str}{m.group(5)}{m.group(6)}{m.group(7) or ""}\n'
            _write_config_verified(lines)
            return

    # Insert as a new row right after the opening line.
    # Trailing comma is legal in Python dicts, so this is always safe.
    lines.insert(start + 1, f'    "{sub_key}": {val_str},\n')
    _write_config_verified(lines)
