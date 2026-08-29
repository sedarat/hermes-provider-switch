#!/usr/bin/env python3
"""
hermes-provider-switch — one command moves an entire Hermes install to a new
LLM provider: the main profile, every bot profile, and every historical
session row in every state.db.

Why this exists
---------------
`hermes model` only rewrites the *default* provider in config.yaml. Every
session that already exists keeps its own copy of the model + provider inside
state.db (sessions.model / sessions.model_config). When a provider dies —
credit exhausted, endpoint shut down, local relay removed — those sessions
stay pinned to a dead route and fail the moment you reopen them. Bot profiles
are worse: they pin the provider in their *own* config.yaml, so a scheduled
job wakes up on a dead endpoint long after you fixed the main config.

This script discovers everything (no hardcoded profile or session counts),
verifies the new credentials BEFORE touching anything, backs up every file it
will modify, and then rewrites all of it in one pass.

Usage
-----
    # what am I running right now, and what is dead?
    python provider_switch.py status

    # verify a key without changing anything
    python provider_switch.py test --base-url https://x/v1 --api-key sk-...

    # preview the full switch
    python provider_switch.py switch --name myprov --base-url https://x/v1 \
        --api-key sk-... --model some-model --dry-run

    # do it (keeps the old provider entry in config)
    python provider_switch.py switch --name myprov --base-url https://x/v1 \
        --api-key sk-... --model some-model

    # do it and delete the old provider entirely
    python provider_switch.py switch ... --remove-provider custom:combo

    # undo the last switch
    python provider_switch.py rollback

Exit codes: 0 ok, 1 error, 2 verification failed (nothing was changed).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ── yaml: prefer ruamel so user comments survive a round trip ──────────
_YAML_RT = None
try:
    from ruamel.yaml import YAML as _RuamelYAML

    _YAML_RT = _RuamelYAML()
    _YAML_RT.preserve_quotes = True
    _YAML_RT.width = 4096
except Exception:  # pragma: no cover - ruamel is optional
    pass

try:
    import yaml as _pyyaml
except Exception:  # pragma: no cover
    _pyyaml = None

if _YAML_RT is None and _pyyaml is None:
    sys.exit(
        "No YAML library available. This script needs either ruamel.yaml "
        "(preferred, preserves comments) or pyyaml. Install one with your "
        "usual package manager, pinning a version, before running again."
    )

BACKUP_ROOT_NAME = "provider-switch-backups"
MANIFEST_NAME = "manifest.json"

# Billing buckets that are not a routable identity on their own.
BARE_PROVIDERS = {"", "custom", "auto", "none", "merged"}

# Cloudflare-fronted providers (gorouter, true-sota and friends) answer 403 to
# any request without a browser-shaped User-Agent, even with a valid key. A
# plain requests/urllib UA is enough to get blocked, so always send one.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")



# ══════════════════════════════════════════════════════════════════════
# discovery
# ══════════════════════════════════════════════════════════════════════
def hermes_home() -> Path:
    """Locate the Hermes home directory the same way Hermes itself does."""
    if env := os.environ.get("HERMES_HOME"):
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        cand = Path(base) / "hermes"
        if cand.exists():
            return cand
    for cand in (Path.home() / ".hermes",
                 Path.home() / ".local" / "share" / "hermes",
                 Path.home() / "Library" / "Application Support" / "hermes"):
        if cand.exists():
            return cand
    raise SystemExit("could not locate Hermes home — set HERMES_HOME")


class Profile:
    """One Hermes profile: its config, its secrets file, its session store."""

    def __init__(self, name: str, root: Path):
        self.name = name              # "default" for the main profile
        self.root = root
        self.config = root / "config.yaml"
        self.env = root / ".env"
        self.db = root / "state.db"

    def __repr__(self) -> str:
        return f"<Profile {self.name}>"

    @property
    def exists(self) -> bool:
        return self.config.exists() or self.db.exists()


def discover_profiles(home: Path) -> list[Profile]:
    """Main profile plus every profile under profiles/ — count is never assumed."""
    found = [Profile("default", home)]
    pdir = home / "profiles"
    if pdir.is_dir():
        for child in sorted(p for p in pdir.iterdir() if p.is_dir()):
            prof = Profile(child.name, child)
            if prof.exists:
                found.append(prof)
    return found


# ══════════════════════════════════════════════════════════════════════
# yaml helpers
# ══════════════════════════════════════════════════════════════════════
def load_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if _YAML_RT is not None:
        return _YAML_RT.load(text) or {}
    return _pyyaml.safe_load(text) or {}


def dump_yaml(path: Path, data: Any) -> None:
    if _YAML_RT is not None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            _YAML_RT.dump(data, fh)
    else:
        path.write_text(
            _pyyaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


# ══════════════════════════════════════════════════════════════════════
# naming
# ══════════════════════════════════════════════════════════════════════
def env_var_for(name: str) -> str:
    """HERMES_CUSTOM_<NAME>_API_KEY — the convention Hermes already uses."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"HERMES_CUSTOM_{slug}_API_KEY"


def env_var_for_host(base_url: str) -> str | None:
    """Hermes derives its own key names from the HOST, e.g.

        https://gorouter.app/v1  ->  HERMES_CUSTOM_GOROUTER_APP_API_KEY

    A switch must reuse that exact name when it already exists, otherwise the
    .env grows a second variable holding the same secret and the two drift.
    """
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").strip()
    except Exception:
        host = ""
    if not host:
        return None
    return env_var_for(host)


def env_has_var(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", re.M)
    return bool(pat.search(path.read_text(encoding="utf-8", errors="ignore")))


def existing_key_env(profiles: list["Profile"], plain_name: str,
                     base_url: str) -> str | None:
    """Find the variable name this provider is ALREADY stored under.

    Priority: a custom_providers entry naming the same provider, then one
    pointing at the same base_url, then a host-derived variable that already
    exists in any .env. Returns None when the provider is genuinely new.
    """
    target_url = base_url.rstrip("/").lower()
    by_name = by_url = None
    for prof in profiles:
        if not prof.config.exists():
            continue
        cfg = load_yaml(prof.config)
        if not isinstance(cfg, dict):
            continue
        entries = cfg.get("custom_providers") or []
        items = (entries.items() if isinstance(entries, dict)
                 else [(e.get("name"), e) for e in entries if isinstance(e, dict)])
        for name, body in items:
            if not isinstance(body, dict):
                continue
            key_env = body.get("key_env")
            if not key_env:
                continue
            if _norm(name) == _norm(plain_name):
                by_name = by_name or key_env
            if str(body.get("base_url", "")).rstrip("/").lower() == target_url:
                by_url = by_url or key_env
    if by_name:
        return by_name
    if by_url:
        return by_url
    host_var = env_var_for_host(base_url)
    if host_var and any(env_has_var(p.env, host_var) for p in profiles):
        return host_var
    return None


def provider_identity(name: str) -> str:
    """The routable ``custom:<name>`` form stored in config and session rows."""
    return name if name.startswith("custom:") else f"custom:{name}"


def _norm(provider: str | None) -> str:
    return (provider or "").strip().lower().replace(" ", "")


# ══════════════════════════════════════════════════════════════════════
# credential verification (never skipped before a write)
# ══════════════════════════════════════════════════════════════════════
def _http_json(url: str, api_key: str, payload: dict | None, timeout: int,
               proxy: str | None) -> tuple[int, Any]:
    """POST when payload is given, else GET. Returns (status, parsed-or-text)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": BROWSER_UA,
    }
    try:
        import requests  # type: ignore

        proxies = {"http": proxy, "https": proxy} if proxy else None
        if payload is None:
            r = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
        else:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=timeout, proxies=proxies)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:500]
    except ImportError:
        import urllib.error
        import urllib.request

        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            opener = urllib.request.build_opener()
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(body)
                except Exception:
                    return resp.status, body[:500]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, body[:500]
        except Exception as e:
            return 0, str(e)


def verify_provider(base_url: str, api_key: str, model: str | None,
                    timeout: int = 45, proxy: str | None = None,
                    skip_chat: bool = False) -> dict:
    """Probe /models then a 1-token chat. Nothing is written unless this passes."""
    base = base_url.rstrip("/")
    out: dict[str, Any] = {"ok": False, "models": [], "model_ok": None,
                           "chat_ok": None, "errors": []}

    status, body = _http_json(f"{base}/models", api_key, None, timeout, proxy)
    if status == 200 and isinstance(body, dict):
        for item in (body.get("data") or []):
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                out["models"].append(mid)
        out["models_ok"] = True
    else:
        out["models_ok"] = False
        out["errors"].append(f"GET /models -> {status}: {str(body)[:200]}")

    if model and out["models"]:
        out["model_ok"] = model in out["models"]

    if skip_chat:
        out["ok"] = bool(out.get("models_ok"))
        return out

    probe_model = model or (out["models"][0] if out["models"] else None)
    if not probe_model:
        out["errors"].append("no model to probe with")
        return out

    status, body = _http_json(
        f"{base}/chat/completions", api_key,
        {"model": probe_model,
         "messages": [{"role": "user", "content": "ping"}],
         "max_tokens": 4, "stream": False},
        timeout, proxy)
    if status == 200 and isinstance(body, dict) and body.get("choices"):
        out["chat_ok"] = True
        out["ok"] = True
    else:
        out["chat_ok"] = False
        out["errors"].append(f"POST /chat/completions -> {status}: {str(body)[:200]}")
    return out


# ══════════════════════════════════════════════════════════════════════
# .env editing
# ══════════════════════════════════════════════════════════════════════
def set_env_var(path: Path, key: str, value: str) -> str:
    """Insert or replace KEY=value. Returns 'added' | 'updated' | 'unchanged'."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            if line.rstrip("\r\n").split("=", 1)[1].strip().strip("'\"") == value:
                return "unchanged"
            lines[i] = f"{key}={value}\n"
            path.write_text("".join(lines), encoding="utf-8", newline="")
            return "updated"
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines.append(f"{key}={value}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8", newline="")
    return "added"


def remove_env_var(path: Path, key: str) -> bool:
    if not path.exists():
        return False
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [ln for ln in lines if not pat.match(ln)]
    if len(kept) == len(lines):
        return False
    path.write_text("".join(kept), encoding="utf-8", newline="")
    return True


# ══════════════════════════════════════════════════════════════════════
# config.yaml editing
# ══════════════════════════════════════════════════════════════════════
def custom_provider_names(cfg: dict) -> list[str]:
    entries = cfg.get("custom_providers") or []
    if isinstance(entries, dict):          # older map-shaped config
        return list(entries.keys())
    return [e.get("name") for e in entries
            if isinstance(e, dict) and e.get("name")]


def upsert_custom_provider(cfg: dict, name: str, base_url: str, key_env: str,
                           model: str, models: list[str]) -> str:
    """Add or refresh the custom_providers entry. Both config shapes handled."""
    payload_models = {m: {} for m in models} if models else {}
    entries = cfg.get("custom_providers")

    if isinstance(entries, dict):
        action = "updated" if name in entries else "added"
        body = dict(entries.get(name) or {})
        body.update({"base_url": base_url, "key_env": key_env, "model": model})
        if payload_models:
            body["models"] = payload_models
            body["models_discovered"] = True
        entries[name] = body
        cfg["custom_providers"] = entries
        return action

    if entries is None:
        entries = []
        cfg["custom_providers"] = entries

    for entry in entries:
        # Case-insensitive match (#2026-08-29): a same-name-different-case
        # entry (e.g. existing "gorouter" vs a switch writing "Gorouter")
        # must UPDATE the existing entry, not add a case-variant duplicate.
        # An exact-case match wins first so the entry's stored casing is
        # only changed when no exact match exists.
        if isinstance(entry, dict) and entry.get("name") == name:
            entry["base_url"] = base_url
            entry["key_env"] = key_env
            entry["model"] = model
            if payload_models:
                entry["models"] = payload_models
                entry["models_discovered"] = True
            return "updated"
    for entry in entries:
        if isinstance(entry, dict) and _norm(entry.get("name")) == _norm(name):
            entry["name"] = name
            entry["base_url"] = base_url
            entry["key_env"] = key_env
            entry["model"] = model
            if payload_models:
                entry["models"] = payload_models
                entry["models_discovered"] = True
            return "updated"

    new_entry = {"name": name, "base_url": base_url, "key_env": key_env, "model": model}
    if payload_models:
        new_entry["models"] = payload_models
        new_entry["models_discovered"] = True
    entries.append(new_entry)
    return "added"


def drop_custom_provider(cfg: dict, name: str) -> bool:
    entries = cfg.get("custom_providers")
    if isinstance(entries, dict):
        return entries.pop(name, None) is not None
    if not isinstance(entries, list):
        return False
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and _norm(entry.get("name")) == _norm(name):
            entries.pop(i)
            return True
    return False


def set_active_model(cfg: dict, identity: str, model: str, base_url: str,
                     key_env: str) -> None:
    """Point model.* at the new provider, preserving unrelated keys."""
    block = cfg.get("model")
    if not isinstance(block, dict):
        block = {}
        cfg["model"] = block
    block["provider"] = identity
    block["default"] = model
    block["base_url"] = base_url
    block["api_key"] = "${%s}" % key_env


def config_summary(cfg: dict) -> dict:
    block = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    fb = cfg.get("fallback_model") if isinstance(cfg.get("fallback_model"), dict) else {}
    return {
        "provider": block.get("provider"),
        "model": block.get("default") or block.get("model"),
        "base_url": block.get("base_url"),
        "fallback_provider": fb.get("provider"),
        "fallback_model": fb.get("model") or fb.get("default"),
        "custom_providers": custom_provider_names(cfg),
    }


# ══════════════════════════════════════════════════════════════════════
# state.db editing
# ══════════════════════════════════════════════════════════════════════
def _row_provider(model_config: str | None, billing_provider: str | None) -> str:
    try:
        cfg = json.loads(model_config or "{}")
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}
    gw = cfg.get("gateway_runtime") if isinstance(cfg.get("gateway_runtime"), dict) else {}
    return cfg.get("provider") or gw.get("provider") or billing_provider or ""


def scan_sessions(db: Path) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    except sqlite3.DatabaseError:
        con.close()
        return []
    if not cols:
        con.close()
        return []
    wanted = [c for c in ("id", "display_name", "title", "model", "model_config",
                          "message_count", "source", "billing_provider",
                          "last_activity_at") if c in cols]
    rows = con.execute(f"SELECT {', '.join(wanted)} FROM sessions").fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["_provider"] = _row_provider(d.get("model_config"), d.get("billing_provider"))
        d["_name"] = d.get("display_name") or d.get("title") or "(untitled)"
        out.append(d)
    return out


def rewrite_sessions(db: Path, identity: str, model: str, base_url: str,
                     api_mode: str = "chat_completions",
                     skip_ids: set[str] | None = None,
                     dry_run: bool = False) -> dict:
    """Repoint every session row at the new provider. Returns counts."""
    skip_ids = skip_ids or set()
    sessions = scan_sessions(db)
    if not sessions:
        return {"total": 0, "changed": 0, "skipped": 0, "already": 0}

    targets, already, skipped = [], 0, 0
    for s in sessions:
        if s["id"] in skip_ids:
            skipped += 1
            continue
        if s.get("model") == model and _norm(s["_provider"]) == _norm(identity):
            already += 1
            continue
        targets.append(s)

    if dry_run or not targets:
        return {"total": len(sessions), "changed": len(targets),
                "skipped": skipped, "already": already, "targets": targets}

    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    for s in targets:
        try:
            cfg = json.loads(s.get("model_config") or "{}")
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        cfg["model"] = model
        cfg["provider"] = identity
        cfg["base_url"] = base_url
        cfg["api_mode"] = api_mode
        # Both shapes matter: the CLI resume path reads gateway_runtime, the
        # TUI/desktop path reads the top-level keys.
        cfg["gateway_runtime"] = {"provider": identity, "base_url": base_url,
                                  "api_mode": api_mode}
        sets = ["model = ?", "model_config = ?"]
        vals: list[Any] = [model, json.dumps(cfg, ensure_ascii=False)]
        if "billing_provider" in cols:
            sets.append("billing_provider = ?")
            vals.append(identity)
        if "billing_base_url" in cols:
            sets.append("billing_base_url = ?")
            vals.append(base_url)
        vals.append(s["id"])
        con.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", vals)
    con.commit()
    con.close()
    return {"total": len(sessions), "changed": len(targets),
            "skipped": skipped, "already": already, "targets": targets}


# ══════════════════════════════════════════════════════════════════════
# backup / rollback
# ══════════════════════════════════════════════════════════════════════
def make_backup(home: Path, profiles: list[Profile], label: str) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = home / BACKUP_ROOT_NAME / f"{stamp}_{label}"
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"created": stamp, "label": label,
                                "home": str(home), "files": []}
    for prof in profiles:
        pdir = dest / prof.name
        pdir.mkdir(parents=True, exist_ok=True)
        for src in (prof.config, prof.env, prof.db):
            if not src.exists():
                continue
            shutil.copy2(src, pdir / src.name)
            manifest["files"].append({"profile": prof.name,
                                      "original": str(src),
                                      "backup": str(pdir / src.name)})
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def latest_backup(home: Path) -> Path | None:
    root = home / BACKUP_ROOT_NAME
    if not root.is_dir():
        return None
    dirs = [d for d in root.iterdir() if d.is_dir() and (d / MANIFEST_NAME).exists()]
    return max(dirs, key=lambda d: d.name) if dirs else None


def restore_backup(path: Path) -> list[str]:
    manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    restored = []
    for item in manifest.get("files", []):
        src, dst = Path(item["backup"]), Path(item["original"])
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(str(dst))
    return restored


# ══════════════════════════════════════════════════════════════════════
# commands
# ══════════════════════════════════════════════════════════════════════
def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("  (none)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print("  " + line)
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def cmd_status(args) -> int:
    home = hermes_home()
    profiles = discover_profiles(home)
    print(f"Hermes home: {home}")
    print(f"Profiles discovered: {len(profiles)}\n")

    grand_total = grand_orphan = 0
    for prof in profiles:
        cfg = load_yaml(prof.config)
        summary = config_summary(cfg) if isinstance(cfg, dict) else {}
        known = {_norm(f"custom:{n}") for n in (summary.get("custom_providers") or [])}
        known |= {_norm(n) for n in (summary.get("custom_providers") or [])}

        sessions = scan_sessions(prof.db)
        buckets: dict[str, int] = {}
        orphans = 0
        for s in sessions:
            p = s["_provider"] or "(none)"
            buckets[p] = buckets.get(p, 0) + 1
            n = _norm(p)
            if n in BARE_PROVIDERS or (n.startswith("custom:") and n not in known):
                orphans += 1
        grand_total += len(sessions)
        grand_orphan += orphans

        print(f"── profile: {prof.name}")
        print(f"   config      : {summary.get('model')} @ {summary.get('provider')}")
        print(f"   base_url    : {summary.get('base_url')}")
        if summary.get("fallback_provider"):
            print(f"   fallback    : {summary.get('fallback_model')} @ "
                  f"{summary.get('fallback_provider')}")
        print(f"   providers   : {', '.join(summary.get('custom_providers') or []) or '(none)'}")
        # Case-insensitive duplicate detection (#2026-08-29): "gorouter" and
        # "Gorouter" look like two providers but resolve to the same
        # custom:<name> identity down two different case paths — the exact
        # confusion that caused a real-world mis-switch. Surface it loudly;
        # don't let it hide silently in the provider list above.
        _seen: dict[str, list[str]] = {}
        for _pname in (summary.get("custom_providers") or []):
            _seen.setdefault(_norm(_pname), []).append(_pname)
        _dupe_groups = [names for names in _seen.values() if len(names) > 1]
        if _dupe_groups:
            for names in _dupe_groups:
                print(f"   \u26a0\ufe0f  DUPLICATE (case-insensitive): "
                      f"{' / '.join(names)} — these collide as the same "
                      f"provider identity. Run `switch` on either name to "
                      f"merge them; do not treat them as separate providers.")
        print(f"   sessions    : {len(sessions)}  (unroutable: {orphans})")
        if buckets:
            rows = [[p, str(c)] for p, c in sorted(buckets.items(), key=lambda x: -x[1])]
            _print_table(["session provider", "count"], rows)
        print()

    print(f"TOTAL: {grand_total} sessions across {len(profiles)} profiles; "
          f"{grand_orphan} pinned to a provider that is not in config.")
    return 0


def cmd_test(args) -> int:
    print(f"Probing {args.base_url} ...")
    res = verify_provider(args.base_url, args.api_key, args.model,
                          timeout=args.timeout, proxy=args.proxy,
                          skip_chat=args.no_chat)
    print(f"  GET  /models          : {'ok' if res.get('models_ok') else 'FAILED'}"
          f"  ({len(res['models'])} models)")
    if res["models"]:
        preview = ", ".join(res["models"][:12])
        print(f"    {preview}{' ...' if len(res['models']) > 12 else ''}")
    if args.model:
        print(f"  model '{args.model}' listed : {res.get('model_ok')}")
    if not args.no_chat:
        print(f"  POST /chat/completions: {'ok' if res.get('chat_ok') else 'FAILED'}")
    for err in res["errors"]:
        print(f"  ! {err}")
    print(f"\nRESULT: {'USABLE' if res['ok'] else 'NOT USABLE'}")
    return 0 if res["ok"] else 2


def cmd_switch(args) -> int:
    home = hermes_home()
    profiles = discover_profiles(home)
    identity = provider_identity(args.name)
    plain_name = identity.split(":", 1)[1]
    base_url = args.base_url.rstrip("/")

    # Reuse the variable this provider is already stored under, so a repeat
    # switch never leaves two .env names holding the same secret.
    if args.key_env:
        key_env, key_env_note = args.key_env, "explicit --key-env"
    elif reused := existing_key_env(profiles, plain_name, base_url):
        key_env, key_env_note = reused, "reusing existing variable"
    else:
        key_env = env_var_for_host(base_url) or env_var_for(plain_name)
        key_env_note = "new variable"

    print(f"Hermes home        : {home}")
    print(f"Profiles discovered: {len(profiles)} "
          f"({', '.join(p.name for p in profiles)})")
    print(f"Target provider    : {identity}")
    print(f"Target model       : {args.model}")
    print(f"Base URL           : {base_url}")
    print(f"Key env var        : {key_env}  ({key_env_note})")
    print()

    # ── 1. verify BEFORE anything is written ───────────────────────────
    if args.skip_verify:
        print("!! verification skipped by --skip-verify")
        models: list[str] = []
    else:
        print("Verifying credentials ...")
        res = verify_provider(base_url, args.api_key, args.model,
                              timeout=args.timeout, proxy=args.proxy,
                              skip_chat=args.no_chat)
        for err in res["errors"]:
            print(f"  ! {err}")
        if not res["ok"]:
            print("\nABORTED: provider did not verify. Nothing was changed.")
            return 2
        models = res["models"]
        print(f"  ok — {len(models)} models listed"
              + ("" if args.no_chat else ", chat probe passed"))
        if args.model and models and args.model not in models:
            print(f"  ! warning: '{args.model}' is not in the provider's model list")

    # ── 2. survey what will change ─────────────────────────────────────
    plan = []
    for prof in profiles:
        stats = rewrite_sessions(prof.db, identity, args.model, base_url,
                                 skip_ids=set(args.skip_session or []),
                                 dry_run=True)
        plan.append((prof, stats))

    print("\nPlan:")
    _print_table(
        ["profile", "config", "sessions", "to change", "already", "skipped"],
        [[p.name,
          "yes" if p.config.exists() else "-",
          str(s["total"]), str(s["changed"]), str(s["already"]), str(s["skipped"])]
         for p, s in plan])
    total_changed = sum(s["changed"] for _, s in plan)
    print(f"\n  {total_changed} session rows will be repointed at {identity}.")
    if args.remove_provider:
        print(f"  provider '{args.remove_provider}' will be REMOVED from every "
              f"config after the switch.")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # ── 3. backup everything we are about to touch ─────────────────────
    backup = make_backup(home, profiles, f"switch_to_{plain_name}")
    print(f"\nBackup: {backup}")

    # ── 4. write configs + secrets ─────────────────────────────────────
    print("\nWriting configs:")
    for prof in profiles:
        actions = []
        if prof.env.exists() or prof.name == "default":
            actions.append("env:" + set_env_var(prof.env, key_env, args.api_key))
        if prof.config.exists():
            cfg = load_yaml(prof.config)
            if not isinstance(cfg, dict):
                print(f"  {prof.name}: unexpected config shape, skipped")
                continue
            actions.append("provider:" + upsert_custom_provider(
                cfg, plain_name, base_url, key_env, args.model, models))
            set_active_model(cfg, identity, args.model, base_url, key_env)
            actions.append("model:set")
            dump_yaml(prof.config, cfg)
        print(f"  {prof.name}: {', '.join(actions) or 'nothing to do'}")

    # ── 5. rewrite every session row ───────────────────────────────────
    print("\nRewriting sessions:")
    grand = 0
    for prof in profiles:
        stats = rewrite_sessions(prof.db, identity, args.model, base_url,
                                 skip_ids=set(args.skip_session or []))
        grand += stats["changed"]
        print(f"  {prof.name}: {stats['changed']} changed, "
              f"{stats['already']} already correct, {stats['skipped']} skipped "
              f"(of {stats['total']})")

    # ── 6. optionally retire the old provider ──────────────────────────
    if args.remove_provider:
        old_plain = args.remove_provider.split(":", 1)[-1]
        old_env = env_var_for(old_plain)
        print(f"\nRemoving old provider '{args.remove_provider}':")
        for prof in profiles:
            bits = []
            if prof.config.exists():
                cfg = load_yaml(prof.config)
                if isinstance(cfg, dict) and drop_custom_provider(cfg, old_plain):
                    dump_yaml(prof.config, cfg)
                    bits.append("config entry removed")
            if args.remove_key and remove_env_var(prof.env, old_env):
                bits.append(f"{old_env} removed")
            print(f"  {prof.name}: {', '.join(bits) or 'not present'}")

    # ── 7. verify the written state ────────────────────────────────────
    print("\nVerification (re-read from disk):")
    ok = True
    for prof in profiles:
        cfg = load_yaml(prof.config) if prof.config.exists() else {}
        summary = config_summary(cfg) if isinstance(cfg, dict) else {}
        bad = [s for s in scan_sessions(prof.db)
               if s["id"] not in set(args.skip_session or [])
               and _norm(s["_provider"]) != _norm(identity)]
        cfg_ok = _norm(summary.get("provider")) == _norm(identity)
        ok = ok and cfg_ok and not bad
        print(f"  {prof.name}: config={'ok' if cfg_ok else 'MISMATCH'}"
              f" ({summary.get('model')} @ {summary.get('provider')}), "
              f"sessions off-target={len(bad)}")

    print(f"\n{'DONE' if ok else 'DONE WITH WARNINGS'} — {grand} session rows "
          f"moved to {args.model} @ {identity}")
    print(f"Rollback with: python {Path(__file__).name} rollback --backup {backup}")
    if not args.skip_verify:
        print("Note: a session that is OPEN right now keeps its model in memory — "
              "type /model in that chat, or reopen it.")
    return 0 if ok else 1


def cmd_rollback(args) -> int:
    home = hermes_home()
    path = Path(args.backup).expanduser() if args.backup else latest_backup(home)
    if not path or not (path / MANIFEST_NAME).exists():
        print("No backup found." if not path else f"Not a backup dir: {path}")
        return 1
    manifest = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    print(f"Restoring from {path} (created {manifest.get('created')}, "
          f"label {manifest.get('label')})")
    for item in manifest.get("files", []):
        print(f"  {item['profile']}: {Path(item['original']).name}")
    if not args.yes:
        print("\nRe-run with --yes to actually restore.")
        return 0
    restored = restore_backup(path)
    print(f"\nRestored {len(restored)} files.")
    return 0


def cmd_list_backups(args) -> int:
    home = hermes_home()
    root = home / BACKUP_ROOT_NAME
    if not root.is_dir():
        print("No backups yet.")
        return 0
    rows = []
    for d in sorted(root.iterdir()):
        mf = d / MANIFEST_NAME
        if not mf.is_dir() and mf.exists():
            m = json.loads(mf.read_text(encoding="utf-8"))
            rows.append([d.name, m.get("label", ""), str(len(m.get("files", [])))])
    _print_table(["backup", "label", "files"], rows)
    return 0


# ══════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="provider_switch",
        description="Move an entire Hermes install (all profiles, all sessions) "
                    "to a new LLM provider.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="show every profile, provider and session route")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("test", help="verify a provider's key without changing anything")
    p.add_argument("--base-url", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--model")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--proxy", help="optional outbound proxy URL for the probe")
    p.add_argument("--no-chat", action="store_true", help="only probe /models")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("switch", help="switch everything to a new provider")
    p.add_argument("--name", required=True, help="short provider name, e.g. gorouter")
    p.add_argument("--base-url", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--key-env", help="override the .env variable name")
    p.add_argument("--remove-provider", help="delete this provider from configs afterwards")
    p.add_argument("--remove-key", action="store_true",
                   help="with --remove-provider, also drop its .env key")
    p.add_argument("--skip-session", action="append", metavar="ID",
                   help="leave this session id untouched (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-verify", action="store_true",
                   help="do not probe the provider first (not recommended)")
    p.add_argument("--no-chat", action="store_true", help="verify with /models only")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--proxy")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("rollback", help="restore the files from a backup")
    p.add_argument("--backup", help="backup dir (default: most recent)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("backups", help="list available backups")
    p.set_defaults(func=cmd_list_backups)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
