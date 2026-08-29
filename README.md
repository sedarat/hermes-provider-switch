# hermes-provider-switch

One command moves an entire [Hermes Agent](https://hermes-agent.nousresearch.com) install to a different LLM provider — the main profile, every bot profile, and **every historical session** in every `state.db`.

## The problem

`hermes model` only rewrites the *default* provider in `config.yaml`. Everything else keeps its own copy of the route:

| Where | What it stores | Touched by `hermes model`? |
|---|---|---|
| `config.yaml` → `model.*` | default provider + model for NEW sessions | ✅ yes |
| `state.db` → `sessions.model` / `model_config` | per-session provider + model | ❌ no |
| `profiles/<bot>/config.yaml` | that bot's own provider | ❌ no |
| `profiles/<bot>/state.db` | that bot's sessions | ❌ no |

So when a provider dies — credit exhausted, endpoint removed, local relay shut down — every old session and every bot **stays pinned to the dead route** and fails the moment it is reopened or fired by cron.

This tool fixes all of it in one pass.

## Install

```bash
pip install "ruamel.yaml==0.18.6"   # optional but recommended (preserves config comments)
git clone https://github.com/sedarat/hermes-provider-switch.git
```

No other dependencies — the script is stdlib-first and falls back to `urllib` + `pyyaml` when `requests`/`ruamel.yaml` are absent.

## Usage

```bash
# 1. Survey — every profile, provider, and how many sessions point at a dead route
python scripts/provider_switch.py status

# 2. Verify a key WITHOUT changing anything (probes /models + a 1-token chat)
python scripts/provider_switch.py test \
  --base-url https://example.com/v1 --api-key sk-... --model some-model

# 3. Dry-run the switch (prints the plan, writes nothing)
python scripts/provider_switch.py switch --name myprov \
  --base-url https://example.com/v1 --api-key sk-... --model some-model --dry-run

# 4. Execute (keeps the old provider entry so you can switch back)
python scripts/provider_switch.py switch --name myprov \
  --base-url https://example.com/v1 --api-key sk-... --model some-model

# 5. Roll back the last switch
python scripts/provider_switch.py rollback --yes
```

### Keep, delete, or shift the old provider

| Intent | Flag | Effect |
|---|---|---|
| Shift to X, keep the old one | *(default)* | old `custom_providers` entry stays — switch back anytime |
| Switch to X and delete the old one | `--remove-provider custom:old` | old entry removed **after** the new one is live |
| Also drop its secret | `+ --remove-key` | removes the old `HERMES_CUSTOM_*_API_KEY` too |

## Safety

- **Credentials are verified before anything is written.** A bad key aborts the switch with nothing changed (exit code 2).
- **Every file is backed up** into `$HERMES_HOME/provider-switch-backups/<stamp>/` with a manifest, and `rollback` restores them.
- **Removal always runs last**, so a failed switch never leaves you with neither provider.
- The live session holds its model in memory — pass its id to `--skip-session` and use the `/model` command inside that chat. Closed sessions pick up the new route with no restart needed.

## Notes

- **Cloudflare-fronted providers** (gorouter, true-sota, …) reject plain `python-requests` user agents even with a valid key — the script always sends a browser UA.
- `model_config` is written in **two shapes** (top-level `provider` for the TUI/desktop path, nested `gateway_runtime.provider` for the CLI resume path) plus `billing_provider`/`billing_base_url`, so resume restores the route on every surface.
- Bare `custom` / `auto` / `merged` / empty providers are not routable identities — the script persists `custom:<name>` and `status` counts the rest as "unroutable".

## License

MIT — see [LICENSE](LICENSE).
