# funds-manager

An [Open Autonomy](https://github.com/valory-xyz/open-autonomy) skill that monitors an agent's fund balances — native and ERC20 — across the chains it operates on, and flags deficits against configured requirements.

> **Status:** this repo will eventually be upstreamed into [open-autonomy](https://github.com/valory-xyz/open-autonomy); it lives here for now to iterate independently.

## What's in this repo

| Package | Public ID | Description |
|---|---|---|
| Skill | `valory/funds_manager` | Periodic balance check for the agent EOA and its associated Safe across configured chains. Uses [w3multicall](https://pypi.org/project/w3multicall/) for batched RPC reads and compares results against a configured per-chain, per-token minimum. |

The skill exposes:

- A `FundsManagerBehaviour` that runs as a `SimpleBehaviour` inside the agent's AEA loop
- A `Params` model (rpc_urls, safe addresses, fund requirements) validated via pydantic

## Requirements

- Python `>=3.10, <3.15`
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
source .venv/bin/activate
autonomy packages sync
```

## Development

```bash
make format          # black + isort
make code-checks     # lint + type check
make security        # bandit + safety
make generators      # regenerate protocols + hashes
make common-checks-1 # hash + copyright + docs + deps
```

See `CONTRIBUTING.md` for the full pre-PR checklist.

## License

Licensed under Apache License 2.0.
