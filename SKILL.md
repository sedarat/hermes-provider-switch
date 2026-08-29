---
name: hermes-provider-switch
description: Switch Hermes to a new LLM provider or API key.
---

# Hermes Provider Switch

One command moves an entire Hermes install to a different LLM provider: the
main profile, every bot profile, and every historical session row in every
`state.db`.

Triggers: "switch to provider X", "new API key, move everything over",
"this provider died, fix all my sessions", "go back to the old provider",
"which provider is each profile on?", Persian equivalents such as
«سویچ کن روی ...», «پروایدر جدید», «همه سشن‌ها رو عوض کن».

## Why this exists

`hermes model` rewrites only the **default** provider in `config.yaml`.
Everything else keeps its own copy of the route:

| Where | What it stores | Touched by `hermes model`? |
|---|---|---|
| `config.yaml` → `model.*` | default provider + model for NEW sessions | yes |
| `state.db` → `sessions.model` / `sessions.model_config` | per-session provider + model | **no** |
| `profiles/<bot>/config.yaml` | that bot's own provider | **no** |
| `profiles/<bot>/state.db` | that bot's sessions | **no** |

So when a provider dies (credit exhausted, endpoint removed, local relay shut
down) every old session and every bot stays pinned to the dead route and fails
the moment it is reopened or fired by cron. This tool fixes all of it.

Verified on a real install (2026-08-28): 53 sessions across 5 profiles, 42 of
them pinned to providers no longer in config → 0 after the switch.

## The script

`scripts/provider_switch.py` — stdlib-first (uses `requests` and
`ruamel.yaml` when available, falls back to `urllib` + `pyyaml`). Prefer the
copy under this skill's `scripts/` directory.

## Workflow

### 1. Always start with a survey

```bash
python scripts/provider_switch.py status
```

Discovers profiles by scanning `$HERMES_HOME/profiles/` — never assumes a
count. Reports per profile: configured model + provider, base_url, fallback,
custom_providers list, session count, and how many sessions point at a
provider that is **not** in config ("unroutable").

### 2. Verify the credentials before anything else

```bash
python scripts/provider_switch.py test \
  --base-url https://example.com/v1 --api-key sk-... --model some-model
```

Probes `GET /models` then a 1-token `POST /chat/completions`. Exit code 2
means not usable. `switch` runs this same check itself and **aborts before
writing anything** if it fails — so a bad key can never corrupt the config.

### 3. Dry run the switch

```bash
python scripts/provider_switch.py switch --name myprov \
  --base-url https://example.com/v1 --api-key sk-... \
  --model some-model --dry-run
```

Prints the plan table (per profile: total / to change / already correct /
skipped) and writes nothing.

### 4. Execute

```bash
python scripts/provider_switch.py switch --name myprov \
  --base-url https://example.com/v1 --api-key sk-... --model some-model \
  --skip-session <CURRENT_SESSION_ID>
```

Order of operations (each step gated on the previous succeeding):

1. verify credentials — abort on failure, nothing written
2. discover profiles, survey what will change
3. back up every `config.yaml`, `.env` and `state.db` it will touch, into
   `$HERMES_HOME/provider-switch-backups/<stamp>_<label>/` with a manifest
4. write the API key into each `.env`
5. upsert the `custom_providers` entry in each `config.yaml`
6. point `model.provider` / `model.default` / `model.base_url` /
   `model.api_key` at it
7. rewrite every session row in every `state.db`
8. optionally retire the old provider
9. re-read from disk and report per-profile pass/fail

Ask the user for the current session id only if you cannot determine it;
otherwise pass it automatically so the live chat is left alone.

### 5. Retiring the old provider

Two distinct user intents — keep them separate:

| Intent | Flag | Effect |
|---|---|---|
| "shift to X, keep the old one" | (default) | old `custom_providers` entry stays; you can switch back later |
| "switch to X and delete the old one" | `--remove-provider custom:old` | old entry removed from every config **after** the new one is live |
| also drop its secret | `+ --remove-key` | removes the old `HERMES_CUSTOM_*_API_KEY` too |

Removal always runs last, so a failed switch never leaves you with neither
provider.

### 6. Rollback

```bash
python scripts/provider_switch.py backups          # list
python scripts/provider_switch.py rollback         # preview newest
python scripts/provider_switch.py rollback --yes   # restore
```

## Key-name reuse (important)

Hermes derives its own `.env` variable names from the **host**:
`https://gorouter.app/v1` → `HERMES_CUSTOM_GOROUTER_APP_API_KEY`.

The script resolves the variable name in this order so a repeat switch never
leaves two variables holding the same secret:

1. explicit `--key-env`
2. the `key_env` of an existing `custom_providers` entry with the same name
3. the `key_env` of an existing entry with the same `base_url`
4. a host-derived name that already exists in some `.env`
5. otherwise a fresh host-derived name

## Known pitfalls

- **Cloudflare-fronted providers 403 without a browser User-Agent.**
  `gorouter.app`, `true-sota.com` and similar reject plain `python-requests`
  UAs even with a valid key. The script always sends a Chrome UA. Probing such
  an endpoint by hand needs `-A 'Mozilla/5.0 ...'` on curl.
- **`model_config` must be written in TWO shapes.** The CLI resume path reads
  `model_config.gateway_runtime.provider`; the TUI/desktop path reads the
  top-level `model_config.provider`. Writing only one leaves "No LLM provider
  configured" on resume. The script writes both, plus `billing_provider` /
  `billing_base_url`.
- **Bare `custom` is not a routable identity.** Always persist
  `custom:<name>`. Rows carrying bare `custom`, `auto`, `merged` or an empty
  provider are what `status` counts as unroutable.
- **A provider present in config is not necessarily alive.** `status` reports
  config membership, not liveness — an exhausted provider still shows as
  routable. Use `test` for liveness.
- **The session you are chatting in right now holds its model in memory.**
  Closed sessions pick up the new route with **no restart needed** (verified).
  For the LIVE session, rewriting its DB row is not enough: `/model <name>`
  typed inside the chat may still not take effect, and in practice a full
  **app restart was required** (verified 2026-08-29). So: run the switch
  WITHOUT `--skip-session` (so the live row is rewritten too), then tell the
  user to restart the desktop app. Do not claim the live session switched
  until they confirm.
- **Bot profiles pin the provider in their own config**, so they must be
  rewritten too or the next cron fires on a dead endpoint. The script handles
  every discovered profile; never hardcode how many there are.
- Direct edits to `config.yaml` by other tools may be blocked by Hermes; this
  script writes the file itself and preserves comments when `ruamel.yaml` is
  installed.

## Post-switch checks

```bash
python scripts/provider_switch.py status   # expect: 0 unroutable
hermes doctor                              # expect: no provider/model errors
```

Then open one previously-broken session in the GUI and send a message.
