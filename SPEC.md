# Funds Manager Skill — Architecture Specification

## Overview

The `funds_manager` skill (`valory/funds_manager:0.1.0`) is a reusable AEA skill that monitors autonomous agent wallet balances across EVM-compatible blockchains. It queries on-chain balances via multicall, compares them against configured thresholds, and reports deficits to the calling system so wallets can be topped up.

The skill is consumed by four agents: **Optimus**, **Omenstrat**, **Polystrat**, and **Agents Fun (meme-ooorr)**.

---

## Core Skill Architecture

### Data Model (`models.py`)

Fund requirements are nested four levels deep:

```
FundRequirements (root)
  └─ ChainRequirements        (keyed by chain name: "optimism", "gnosis", etc.)
       └─ AccountRequirements  (keyed by account name: "agent" or "safe")
            └─ TokenRequirement (keyed by token address)
                 ├─ topup: int        # target balance in wei after top-up
                 ├─ threshold: int    # minimum balance before a deficit is raised
                 ├─ is_native: bool   # true for native gas tokens (ETH, xDAI, POL)
                 ├─ balance: int      # populated at query time
                 ├─ deficit: int      # populated at query time
                 └─ decimals: int     # populated at query time
```

A native token is identified by its address being `0x0000000000000000000000000000000000000000` or `0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`.

The `Params` model reads three configuration maps from `skill.yaml` / `service.yaml`:
- `rpc_urls` — chain name to RPC endpoint URL
- `safe_contract_addresses` — chain name to Gnosis Safe address
- `fund_requirements` — the nested structure above

### Behaviour (`behaviours.py`)

`FundsManagerBehaviour` extends `SimpleBehaviour`. It does **not** participate in the FSM round chain. It operates independently as a callable service.

#### Registration

On `setup()`, the behaviour registers itself into the agent's shared state:

```python
self.context.shared_state["get_funds_status"] = self.get_funds_status
```

This makes `get_funds_status()` available to any other skill or handler in the same agent process.

#### `get_funds_status()` Flow

1. Deep-copy the configured `FundRequirements`.
2. Replace account names ("agent", "safe") with actual addresses:
   - `"agent"` resolves to `self.context.agent_address` (the agent's EOA).
   - `"safe"` resolves to the configured Safe address for that chain.
3. For each chain, construct a batch of multicall calls:
   - **Native tokens:** `getEthBalance(address)` on the Multicall3 contract (`0xcA11bde05977b3631167028862bE2a173976CA11`).
   - **ERC20 tokens:** `balanceOf(address)` and `decimals()` on the token contract.
4. Execute **one multicall per chain** via `w3multicall` against the chain's RPC.
5. Parse results, fill in `balance`, `decimals`, and compute `deficit`:
   ```
   if balance < threshold:
       deficit = max(topup - balance, 0)
   else:
       deficit = 0
   ```
6. Return the enriched `FundRequirements` object.

#### Response Serialization

`FundRequirements.get_response_body()` flattens the nested model into a JSON-friendly dict:

```json
{
  "<chain>": {
    "<account_address>": {
      "<token_address>": {
        "balance": "<stringified_int>",
        "deficit": "<stringified_int>",
        "decimals": <int>
      }
    }
  }
}
```

The `topup`, `threshold`, and `is_native` fields are excluded from the response.

---

## End-to-End Call Chain

The `/funds-status` endpoint is not called by the agent's own frontend UI. It is consumed server-to-server by the **olas-operate-middleware** (Pearl backend), which in turn serves the Pearl desktop app.

```
Pearl Desktop App (olas-operate-app)
│  frontend/service/Balance.ts
│  GET /api/v2/service/{id}/funding_requirements
│
▼
olas-operate-middleware
│  operate/cli.py — exposes the middleware HTTP API
│  operate/services/funding_manager.py — FundingManager.funding_requirements()
│  operate/services/service.py — Service.get_funding_requests()
│      requests.get("http://127.0.0.1:8716/funds-status", timeout=10)
│      Parses JSON, extracts "deficit" per chain/address/token
│
▼
Agent HTTP Handler (per-agent handlers.py)
│  Route: GET /funds-status
│  Handler: _handle_get_funds_status()
│  Calls self.context.shared_state["get_funds_status"]()
│  May apply agent-specific adjustments (see per-agent sections below)
│
▼
FundsManagerBehaviour.get_funds_status()  (this repo)
│  Multicall per chain → on-chain balance queries
│  Returns FundRequirements with balance/deficit filled in
│
▼
EVM Blockchains (via Multicall3 contract)
```

The Pearl frontend polls the middleware endpoint with exponential backoff via `BalancesAndRefillRequirementsProvider.tsx`. The middleware interprets the deficits and presents funding prompts to the user.

---

## Per-Agent Configuration and Integration

### 1. Optimus

| | |
|---|---|
| **Repository** | [valory-xyz/optimus](https://github.com/valory-xyz/optimus) |
| **Chain** | Optimism |
| **Service YAML** | `packages/valory/services/optimus/service.yaml` |
| **Handler** | `packages/valory/skills/optimus_abci/handlers.py` |

#### Fund Requirements

| Chain | Account | Token | Topup | Threshold |
|-------|---------|-------|-------|-----------|
| optimism | agent (EOA) | `0x0000...0000` (native ETH) | 0.0002 ETH (200,000,000,000,000 wei) | 0.0001 ETH (100,000,000,000,000 wei) |
| optimism | safe | `0x0000...0000` (native ETH) | 0 | 0 (disabled) |

#### Configuration

```yaml
# service.yaml override for valory/funds_manager:0.1.0
models:
  params:
    args:
      fund_requirements:
        optimism:
          agent:
            "0x0000000000000000000000000000000000000000":
              topup: 200000000000000
              threshold: 100000000000000
          safe:
            "0x0000000000000000000000000000000000000000":
              topup: 0
              threshold: 0
      rpc_urls:
        optimism: https://mainnet.optimism.io
      safe_contract_addresses:
        optimism: "0x0000000000000000000000000000000000000000"  # overridden at deploy time
```

#### Handler-Specific Logic

The Optimus handler (`_handle_get_funds_status`) adds two layers on top of the base `funds_manager` response:

1. **x402 Payment Deficit Injection:** If `use_x402` is enabled, the handler checks whether the EOA has enough USDC for x402 payments (threshold: 0.2 USDC, topup: 0.25 USDC). If not, it attempts an ETH-to-USDC swap via LiFi in a background thread. If the swap cannot be executed, the equivalent ETH deficit is injected into the response so the middleware funds the EOA with ETH instead.

2. **Withdrawal Mode:** When the agent is paused for withdrawal (`_is_in_withdrawal_mode`), the handler overrides the standard deficit. It counts remaining withdrawal actions and computes a minimal gas deficit (`1,000,000,000,000 wei/tx * 1.2 buffer`). If no actions remain, it returns `{}` (no funding needed).

#### Notes

- The Safe account has topup=0 and threshold=0, meaning it is effectively **not monitored** for funding.
- Only the agent EOA is funded, with a very small amount of ETH for gas on Optimism.

---

### 2. Omenstrat

| | |
|---|---|
| **Repository** | [valory-xyz/trader](https://github.com/valory-xyz/trader) |
| **Chain** | Gnosis |
| **Service YAML** | `packages/valory/services/trader_pearl/service.yaml` |
| **Handler** | `packages/valory/skills/trader_abci/handlers.py` |

#### Fund Requirements

| Chain | Account | Token | Topup | Threshold |
|-------|---------|-------|-------|-----------|
| gnosis | agent (EOA) | `0x0000...0000` (native xDAI) | 2.0 xDAI (2,000,000,000,000,000,000 wei) | 0.21 xDAI (210,000,000,000,000,000 wei) |
| gnosis | safe | `0x0000...0000` (native xDAI) | 5.0 xDAI (5,000,000,000,000,000,000 wei) | 1.0 xDAI (1,000,000,000,000,000,000 wei) |
| gnosis | safe | `0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d` (wxDAI) | 0 | 0 (tracking only) |

#### Configuration

```yaml
# service.yaml override for valory/funds_manager:0.1.0
models:
  params:
    args:
      fund_requirements:
        gnosis:
          agent:
            "0x0000000000000000000000000000000000000000":
              topup: 2000000000000000000
              threshold: 210000000000000000
          safe:
            "0x0000000000000000000000000000000000000000":
              topup: 5000000000000000000
              threshold: 1000000000000000000
            "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d":
              topup: 0
              threshold: 0
      rpc_urls:
        gnosis: https://rpc.gnosischain.com
      safe_contract_addresses:
        gnosis: "0x0000000000000000000000000000000000000000"  # overridden at deploy time
```

#### Handler-Specific Logic: Adjusted Funds Status

The trader handler calls `_get_adjusted_funds_status()` instead of returning the raw `funds_manager` response directly. For Gnosis:

- The handler reads the wxDAI balance from the `funds_manager` response (wxDAI is configured with topup=0/threshold=0, so it is only queried for its balance).
- The wxDAI balance is **added to the native xDAI balance** (1:1 peg, both 18 decimals).
- The deficit for native xDAI is recalculated using the combined balance:
  ```
  adjusted_balance = xDAI_balance + wxDAI_balance
  if adjusted_balance < threshold:
      deficit = topup - adjusted_balance
  else:
      deficit = 0
  ```
- This prevents unnecessary top-up prompts when the Safe holds sufficient value in wxDAI.

#### Notes

- Both agent EOA and Safe are actively monitored.
- wxDAI (`0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d`) is included purely as a balance-tracking entry so its value can be folded into the xDAI deficit calculation.

---

### 3. Polystrat

| | |
|---|---|
| **Repository** | [valory-xyz/trader](https://github.com/valory-xyz/trader) |
| **Chain** | Polygon |
| **Service YAML** | `packages/valory/services/polymarket_trader/service.yaml` |
| **Handler** | `packages/valory/skills/trader_abci/handlers.py` (same as Omenstrat) |

#### Fund Requirements

| Chain | Account | Token | Topup | Threshold |
|-------|---------|-------|-------|-----------|
| polygon | agent (EOA) | `0x0000...0000` (native POL) | 30.0 POL (30,000,000,000,000,000,000 wei) | 3.2 POL (3,200,000,000,000,000,000 wei) |
| polygon | safe | `0x0000...0000` (native POL) | 40.0 POL (40,000,000,000,000,000,000 wei) | 10.0 POL (10,000,000,000,000,000,000 wei) |
| polygon | safe | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (USDC.e bridged) | 65.0 USDC.e (65,000,000; 6 decimals) | 16.0 USDC.e (16,000,000; 6 decimals) |
| polygon | safe | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` (native USDC) | 0 | 0 (tracking only) |

#### Configuration

```yaml
# service.yaml override for valory/funds_manager:0.1.0
models:
  params:
    args:
      fund_requirements:
        polygon:
          agent:
            "0x0000000000000000000000000000000000000000":
              topup: 30000000000000000000
              threshold: 3200000000000000000
          safe:
            "0x0000000000000000000000000000000000000000":
              topup: 40000000000000000000
              threshold: 10000000000000000000
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174":
              topup: 65000000
              threshold: 16000000
            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359":
              topup: 0
              threshold: 0
      rpc_urls:
        polygon: https://1rpc.io/matic
      safe_contract_addresses:
        polygon: "0x0000000000000000000000000000000000000000"  # overridden at deploy time
```

#### Handler-Specific Logic: Adjusted Funds Status

For Polygon, the same `_get_adjusted_funds_status()` method applies a different adjustment:

- The handler reads the native USDC balance (configured with topup=0/threshold=0, tracking only).
- It fetches the **USDC-to-POL exchange rate** from CoinGecko (cached for 2 hours).
- The USDC balance is converted to its POL equivalent and added to the native POL balance:
  ```
  usdc_as_pol = usdc_balance * exchange_rate  # adjusted for decimal differences
  adjusted_balance = POL_balance + usdc_as_pol
  if adjusted_balance < threshold:
      deficit = topup - adjusted_balance
  else:
      deficit = 0
  ```
- This prevents unnecessary POL top-up prompts when the Safe holds sufficient value in USDC.

#### Notes

- Both agent EOA and Safe are actively monitored.
- USDC.e (bridged) at `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` has real topup/threshold values and is funded directly.
- Native USDC at `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` is tracked only and folded into the POL deficit calculation.
- This is the most complex configuration of any agent, with the most tokens and the highest absolute values.

---

### 4. Agents Fun (meme-ooorr)

| | |
|---|---|
| **Repository** | [valory-xyz/meme-ooorr](https://github.com/valory-xyz/meme-ooorr) |
| **Chain** | Base |
| **Service YAML** | `packages/dvilela/services/memeooorr/service.yaml` |
| **Handler** | `packages/dvilela/skills/memeooorr_abci/handlers.py` |

#### Fund Requirements

| Chain | Account | Token | Topup | Threshold |
|-------|---------|-------|-------|-----------|
| base | agent (EOA) | `0x0000...0000` (native ETH) | ~0.000326 ETH (325,700,000,000,000 wei) | ~0.000099 ETH (99,000,000,000,000 wei) |
| base | safe | `0x0000...0000` (native ETH) | ~0.001629 ETH (1,628,500,000,000,000 wei) | ~0.000099 ETH (99,000,000,000,000 wei) |

#### Configuration

```yaml
# service.yaml override for valory/funds_manager:0.1.0
models:
  params:
    args:
      fund_requirements:
        base:
          agent:
            "0x0000000000000000000000000000000000000000":
              topup: 325700000000000
              threshold: 99000000000000
          safe:
            "0x0000000000000000000000000000000000000000":
              topup: 1628500000000000
              threshold: 99000000000000
      rpc_urls:
        base: https://1rpc.io/base
      safe_contract_addresses:
        base: "0x0000000000000000000000000000000000000000"  # overridden at deploy time
```

#### Handler-Specific Logic

The meme-ooorr handler (`_handle_get_funds_status`) is straightforward:

- Calls `self.funds_status.get_response_body()` directly (no balance adjustments).
- If `use_x402` is enabled, submits an x402 swap task in a background thread (same pattern as Optimus).
- No withdrawal mode logic.

#### Notes

- Both agent EOA and Safe are monitored, with only native ETH on Base.
- The meme-ooorr FSM also has its own `CheckFundsBehaviour` in the round chain (`packages/dvilela/skills/memeooorr_abci/behaviour_classes/chain.py`) that does a simpler gas-balance check against `minimum_gas_balance` (0.00005 ETH) via the AEA ledger API. This is independent of the `funds_manager` skill — it gates the FSM state transitions, while `funds_manager` serves the external `/funds-status` endpoint for Pearl.

---

## Summary Comparison

| | Optimus | Omenstrat | Polystrat | Agents Fun |
|---|---|---|---|---|
| **Chain** | Optimism | Gnosis | Polygon | Base |
| **Agent EOA funded** | Yes (native ETH) | Yes (native xDAI) | Yes (native POL) | Yes (native ETH) |
| **Safe funded** | No (disabled) | Yes (native xDAI) | Yes (native POL + USDC.e) | Yes (native ETH) |
| **Tracking-only tokens** | None | wxDAI | Native USDC | None |
| **Balance adjustment** | None | wxDAI folded into xDAI | USDC→POL via CoinGecko | None |
| **Extra handler logic** | x402 USDC deficit injection, withdrawal mode gas calculation | None | None | x402 (if enabled) |
| **Agent EOA topup** | 0.0002 ETH | 2.0 xDAI | 30.0 POL | ~0.000326 ETH |
| **Agent EOA threshold** | 0.0001 ETH | 0.21 xDAI | 3.2 POL | ~0.000099 ETH |
| **Safe topup** | 0 | 5.0 xDAI | 40.0 POL | ~0.001629 ETH |
| **Safe threshold** | 0 | 1.0 xDAI | 10.0 POL | ~0.000099 ETH |
| **Handler file** | `optimus_abci/handlers.py` | `trader_abci/handlers.py` | `trader_abci/handlers.py` | `memeooorr_abci/handlers.py` |
| **Service YAML** | `services/optimus/service.yaml` | `services/trader_pearl/service.yaml` | `services/polymarket_trader/service.yaml` | `services/memeooorr/service.yaml` |
