# Tech Spec: Auto-Transfer Safe → EOA for Native Gas Funding

**Status:** Draft
**Date:** 2026-03-18
**Repo:** `valory-xyz/funds-manager`

---

## 1. Problem Statement

Pearl's agent wallet uses two addresses: the **Agent EOA** (gas wallet, submits transactions) and the **Agent Safe** (funds wallet, holds operating capital). Users interact with a single funding flow, but these wallets operate independently.

**What the user sees:**
- Agent EOA drains to ~0 native tokens (accelerated by x402 payment usage)
- Agent Safe still holds native tokens (e.g., 50 POL)
- Pearl alerts: "Fund your agent with 30 POL"
- User opens wallet, sees 50 POL already there
- User is confused — believes the agent is already funded

**What actually happens:**
- The 50 POL sits in the Safe, unreachable by the EOA
- The EOA has no gas and cannot submit any transaction
- The agent appears "running" but is effectively stalled

**Goal:** When the Agent EOA is low on native gas, the agent should automatically transfer native tokens from Agent Safe → Agent EOA, without user intervention.

---

## 2. Current Architecture

### 2.1 Current Funding Flow

```
Pearl Frontend
  → GET /api/v2/service/{id}/funding_requirements           (middleware)
    → requests.get("http://127.0.0.1:8716/funds-status")    (middleware → agent)
      → HttpHandler._handle_get_funds_status()               (per-agent handler)
        → self.context.shared_state["get_funds_status"]()    (funds_manager skill)
          → FundsManagerBehaviour.get_funds_status()          (this repo)
            → Multicall on-chain balance queries
```

1. **Agent** (`funds_manager` skill): Queries balances via multicall. Computes `deficit = max(topup - balance, 0) if balance < threshold else 0`. Exposes via `/funds-status`.
2. **Middleware** (`olas-operate-middleware`): Polls `/funds-status`. Passes deficits to frontend via `/api/v2/service/{id}/funding_requirements`.
3. **User**: Sees funding alert on Pearl UI. Approves funding.
4. **Frontend**: Calls middleware `/fund` endpoint with user-approved amounts.
5. **Middleware**: Transfers from MasterSafe → Agent EOA and/or Agent Safe.

### 2.2 Current Skill Design

- `FundsManagerBehaviour` is a **`SimpleBehaviour`** — it does not participate in the FSM round chain.
- It registers `get_funds_status()` into `shared_state` during `setup()`.
- It **only reads** balances. It never executes transactions.
- Each agent's HTTP handler calls `get_funds_status()` when the `/funds-status` endpoint is hit, optionally applying agent-specific adjustments.

### 2.3 Per-Agent Handler Customizations (Current)

Each agent's HTTP handler applies additional logic **after** calling `get_funds_status()`. There are two types of customization:

#### Balance Adjustments (Read-Only)

These modify the **reported deficit** by folding alternative token balances into the native balance, so users aren't asked to fund native tokens when the Safe holds equivalent value in another token. No on-chain transactions are executed.

| Agent | Adjustment | Detail |
|-------|-----------|--------|
| **Omenstrat** | wxDAI → xDAI folding | Safe's wxDAI balance added to xDAI balance (1:1 peg, same decimals). Reduces reported Safe native deficit. |
| **Polystrat** | USDC → POL conversion | Safe's USDC balance converted to POL-equivalent via CoinGecko exchange rate (2h cache, fallback rate 0.089935). Added to POL balance. Reduces reported Safe native deficit. |

#### x402 USDC Swap (On-Chain EOA Transaction)

All four agents support x402 payments. When `use_x402=true`, the handler executes an **ETH→USDC swap on the EOA** via LiFi DEX aggregator. This is an actual on-chain transaction, not a read-only adjustment.

**How it works:**
1. On every `/funds-status` poll, `_submit_x402_swap_if_idle()` fires `_ensure_sufficient_funds_for_x402_payments` in a **background thread** (ThreadPoolExecutor).
2. Checks EOA USDC balance against `x402_payment_requirements.threshold`.
3. If below threshold: fetches LiFi quote for ETH→USDC, signs and submits the swap transaction **from the EOA** (not the Safe). The swap spends EOA ETH to buy USDC.
4. If swap succeeds: EOA now has USDC. `shared_state.sufficient_funds_for_x402_payments = True`.
5. If swap fails: stores `x402_eth_deficit` in shared_state.

**How x402 failure affects `/funds-status` response:**

| Agent | On x402 swap failure | Effect on `/funds-status` |
|-------|---------------------|--------------------------|
| **Optimus** | `_inject_x402_eth_deficit()` adds the ETH cost of the failed swap to the EOA native deficit in the response. | MW sees inflated EOA ETH deficit → funds EOA with extra ETH for retry. |
| **Omenstrat** | Same `_submit_x402_swap_if_idle()` mechanism (shared handler code). | Same injection if swap fails. |
| **Polystrat** | Same mechanism (shared handler code). | Same injection if swap fails. |
| **Agents Fun** | Fires `_submit_x402_swap_if_idle()` but does **not** inject ETH deficit into response on failure. | No effect on `/funds-status` response. Swap failure is silent. |

**Key detail:** The x402 swap runs in a background thread **outside the FSM**. It uses the EOA's own nonce (not the Safe's internal nonce). This means it could theoretically conflict with other EOA transactions at the nonce level, though not with Safe transactions.

#### Optimus-Only: Withdrawal Mode

When Optimus is paused for withdrawal (`_is_in_withdrawal_mode`), the handler overrides the entire `/funds-status` response. It counts remaining withdrawal actions and computes a minimal gas deficit per action (`1,000,000,000,000 wei/tx * 1.2 buffer`). If no actions remain, returns `{}` (no funding needed).

### 2.4 Current Per-Agent EOA/Safe Configuration

| Agent | Chain | EOA Native Topup | EOA Native Threshold | Safe Native Topup | Safe Native Threshold |
|-------|-------|-----------------|---------------------|------------------|----------------------|
| **Optimus** | Optimism | 0.0002 ETH | 0.0001 ETH | 0 (disabled) | 0 (disabled) |
| **Omenstrat** | Gnosis | 2.0 xDAI | 0.21 xDAI | 5.0 xDAI | 1.0 xDAI |
| **Polystrat** | Polygon | 30.0 POL | 3.2 POL | 40.0 POL | 10.0 POL |
| **Agents Fun** | Base | ~0.000326 ETH | ~0.000099 ETH | ~0.001629 ETH | ~0.000099 ETH |

---

## 3. Design Decisions

| Decision | Resolution | Rationale |
|----------|-----------|-----------|
| Single-agent or multi-agent? | **Single agent (1-of-1) only** | All current deployments are 1-of-1. |
| Modify `get_funds_status()`? | **No — zero code changes** | Keep existing `/funds-status` interaction, response format, and deficit logic completely unchanged. |
| How does the auto-transfer work? | **New FSM AbciApp in this repo** | A reusable `FundsManagerAbciApp` that agents import and compose into their FSM. The FSM round checks EOA balance and executes Safe→EOA transfer independently of `/funds-status`. |
| How to avoid false EOA alerts? | **Reconfigure EOA threshold to `safe_tx_value`** | The existing `fund_requirements` EOA native threshold is lowered so `/funds-status` only reports an EOA deficit when the EOA literally cannot execute a Safe tx. The FSM round uses its own (higher) threshold. |
| Safe nonce conflicts? | **None** | The FSM serializes all Safe transactions. While the auto-transfer round is executing, no other Safe transaction can run. |
| Optimus Safe enablement? | **Yes, required** | Optimus must enable Safe native funding (non-zero topup/threshold) to use auto-transfer. |

---

## 4. Proposed Solution

### 4.1 Two Independent Systems, One Skill

The solution adds a new FSM component alongside the existing SimpleBehaviour. They operate independently:

| Component | What it does | Changes? |
|-----------|-------------|----------|
| `FundsManagerBehaviour` (existing SimpleBehaviour) | Reads balances, computes deficits, serves `/funds-status` | **No code changes.** Only config changes (EOA threshold lowered). |
| `FundsManagerAbciApp` (new FSM AbciApp) | Checks EOA balance, auto-transfers from Safe → EOA | **New.** Lives in this repo. Agents import and compose. |

These two systems share the same skill config (`Params` model) but use **different thresholds**:

```
                                     ┌─────────────────────────────────┐
                                     │     funds_manager skill         │
                                     │     (this repo)                 │
                                     │                                 │
  /funds-status (MW polling)         │  FundsManagerBehaviour           │
  ◄──────────────────────────────────┤  (SimpleBehaviour, unchanged)   │
  Uses: fund_requirements config     │  threshold = safe_tx_value      │
  EOA deficit only when EOA is       │  (very low — last resort alert) │
  critically low                     │                                 │
                                     ├─────────────────────────────────┤
                                     │                                 │
  Agent FSM cycle                    │  FundsManagerAbciApp             │
  ──────────────────────────────────►│  (new FSM rounds)               │
  Uses: auto_transfer config         │  threshold = operational level  │
  Transfers Safe → EOA when          │  (e.g., 0.21 xDAI)             │
  EOA is operationally low           │                                 │
                                     └─────────────────────────────────┘
```

### 4.2 New Funding Flow (With Auto-Transfer)

Three funding paths, prioritized:

```
PATH 1 — Auto-Transfer (normal case, no user action):
    Agent FSM cycle start
      → FundsManagerAbciApp.CheckFundsRound
        → EOA < auto_transfer_threshold?
        → Yes, and Safe has native funds
        → PrepareSafeToEOATransferRound → TransactionSettlementAbci
        → EOA topped up. User sees nothing.

PATH 2 — User Funds Safe (Safe is depleted):
    Auto-transfer depletes Safe (or Safe was already empty)
      → Safe < safe_threshold (existing fund_requirements config)
      → /funds-status reports Safe deficit (existing code, no changes)
      → MW → Frontend → User sees: "Fund your Safe with X"
      → User approves → MW funds Safe from MasterSafe
      → Next FSM cycle: auto-transfer resumes from Safe → EOA

PATH 3 — MW Funds EOA Directly (last resort):
    EOA < safe_tx_value (cannot execute any transaction)
      → /funds-status reports EOA deficit (existing code, no changes)
      → MW → Frontend → User sees: "Fund your agent with X"
      → User approves → MW funds EOA directly from MasterSafe
      → Next FSM cycle: auto-transfer resumes normally
```

### 4.3 What Changes, What Doesn't

| Component | Changes? | Detail |
|-----------|----------|--------|
| `get_funds_status()` code | **No** | Zero changes to balance checking, deficit calculation, or response format. |
| `/funds-status` response format | **No** | Identical JSON structure. MW reads it exactly the same way. |
| Per-agent HTTP handlers | **No** | wxDAI folding, USDC→POL conversion, x402 — all unchanged. |
| `olas-operate-middleware` | **No** | Reads deficits, forwards to frontend. No code changes. |
| `olas-operate-app` (frontend) | **No** | Displays whatever MW reports. No UI changes. |
| `fund_requirements` config values | **Yes** | EOA native threshold lowered to `safe_tx_value`. |
| `funds_manager` skill config | **Yes** | New `auto_transfer` parameters added to `Params` model. |
| `funds_manager` skill code | **Yes** | New FSM rounds, behaviours, payloads added to the skill. |
| Per-agent `composition.py` | **Yes** | Each agent imports and composes `FundsManagerAbciApp`. |

---

## 5. End-to-End Walkthrough: New Flow

This section traces the complete lifecycle across all components using Omenstrat (Gnosis) as a concrete example. It shows exactly how config flows into behaviour, what each component sees, and what happens at every step.

### 5.1 Configuration (Starting Point)

The agent's `service.yaml` configures the `funds_manager` skill with two sets of parameters:

```yaml
# In packages/valory/services/trader_pearl/service.yaml
# Override for valory/funds_manager:0.1.0

models:
  params:
    args:
      # ── Used by SimpleBehaviour (get_funds_status / /funds-status endpoint) ──
      fund_requirements:
        gnosis:
          agent:
            "0x0000000000000000000000000000000000000000":
              topup: 10000000000000000        # 0.01 xDAI (just enough for a few Safe txs)
              threshold: 1000000000000000      # 0.001 xDAI (safe_tx_value — last resort alert)
          safe:
            "0x0000000000000000000000000000000000000000":
              topup: 5000000000000000000       # 5.0 xDAI
              threshold: 1000000000000000000   # 1.0 xDAI
            "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d":
              topup: 0                          # wxDAI — tracking only
              threshold: 0

      rpc_urls:
        gnosis: https://rpc.gnosischain.com

      safe_contract_addresses:
        gnosis: "0xSafeAddress"

      # ── Used by FSM round (auto-transfer logic) ──
      auto_transfer:
        gnosis:
          threshold: 210000000000000000       # 0.21 xDAI — triggers auto-transfer
          topup: 2000000000000000000          # 2.0 xDAI — target EOA balance
```

**Two thresholds, two systems, one skill:**

| Parameter | Value | Used by | Purpose |
|-----------|-------|---------|---------|
| `fund_requirements.agent.threshold` | 0.001 xDAI | `get_funds_status()` (SimpleBehaviour) | Last-resort alert — only tells MW "fund EOA directly" when EOA can't even execute a Safe tx |
| `auto_transfer.threshold` | 0.21 xDAI | `CheckFundsRound` (FSM round) | Operational threshold — triggers auto-transfer from Safe → EOA |
| `fund_requirements.safe.threshold` | 1.0 xDAI | `get_funds_status()` (SimpleBehaviour) | Tells MW "fund the Safe" when Safe is low |

### 5.2 Agent Startup

```
1. Agent process starts.

2. FundsManagerBehaviour.setup() runs:
   → Reads fund_requirements, rpc_urls, safe_contract_addresses from Params.
   → Reads auto_transfer from Params (new).
   → Registers get_funds_status() into shared_state (existing, unchanged).

3. Agent FSM starts its cycle.
   → FundsManagerAbciApp is the FIRST app in the composition chain.
```

### 5.3 FSM Cycle: Auto-Transfer Check

**On-chain state:** EOA = 0.1 xDAI, Safe = 3.0 xDAI

```
4. CheckFundsRound runs:
   → Reads auto_transfer config for "gnosis":
       threshold = 0.21 xDAI, topup = 2.0 xDAI
   → Queries EOA native balance via RPC: 0.1 xDAI
   → Queries Safe native balance via RPC: 3.0 xDAI

   → Decision: EOA (0.1) < auto_transfer_threshold (0.21)? YES
   → Safe has native funds (3.0 > 0)? YES
   → Gas estimate: EOA (0.1) can afford Safe tx gas (~0.0001 xDAI)? YES
   → transfer_amount = min(3.0, 2.0 - 0.1) = 1.9 xDAI

   → Event: TRANSFER → proceeds to PrepareSafeToEOATransferRound

5. PrepareSafeToEOATransferRound:
   → Builds Safe execTransaction payload:
       to:    0xAgentEOA
       value: 1900000000000000000 (1.9 xDAI in wei)
       data:  0x
   → Proceeds to TransactionSettlementAbci

6. TransactionSettlementAbci (standard open-autonomy flow):
   → Agent signs the Safe transaction hash
   → Agent submits execTransaction call to the Safe contract on Gnosis
   → Gas paid by EOA (~0.0001 xDAI)
   → Transaction confirms on-chain

   → On-chain state is now: EOA ≈ 2.0 xDAI, Safe ≈ 1.1 xDAI

7. FinishedFundsTransferRound:
   → Returns to agent's main FSM (trading logic, etc.)
```

**At this point:** EOA is topped up. Safe is still above its 1.0 xDAI threshold. No user action needed.

### 5.4 Middleware Polls `/funds-status` (Asynchronous)

Meanwhile (independent of the FSM cycle), the middleware polls the agent:

```
8. Middleware (olas-operate-middleware):
   → service.py: requests.get("http://127.0.0.1:8716/funds-status", timeout=10)

9. Agent HTTP Handler receives the request:
   → Calls self.context.shared_state["get_funds_status"]()
   → This is FundsManagerBehaviour.get_funds_status() — UNCHANGED code

10. FundsManagerBehaviour.get_funds_status() runs:
    → Queries on-chain balances via multicall (same as always):
        EOA native balance:  2.0 xDAI  (just topped up by FSM round)
        Safe native balance: 1.1 xDAI  (just transferred 1.9 to EOA)
        Safe wxDAI balance:  0.5 wxDAI  (unchanged)

    → Computes deficits using fund_requirements config:
        EOA:  2.0 xDAI > 0.001 threshold → deficit = 0
        Safe: 1.1 xDAI > 1.0 threshold  → deficit = 0
        wxDAI: (topup=0, threshold=0)   → deficit = 0

    → Returns FundRequirements object

11. Agent HTTP Handler (trader_abci/handlers.py):
    → _get_adjusted_funds_status(): folds wxDAI into xDAI (unchanged logic)
    → Adjusted Safe balance = 1.1 + 0.5 = 1.6 xDAI. Still > 1.0. deficit = 0.
    → Returns HTTP response:

    {
      "gnosis": {
        "0xAgentEOA": {
          "0x0000...0000": { "balance": "2000000000000000000", "deficit": "0", "decimals": 18 }
        },
        "0xSafeAddress": {
          "0x0000...0000": { "balance": "1100000000000000000", "deficit": "0", "decimals": 18 },
          "0xe91D...a97d": { "balance": "500000000000000000", "deficit": "0", "decimals": 18 }
        }
      }
    }

12. Middleware (service.py → get_funding_requests()):
    → Parses response. All deficits = "0".
    → funding_requests = {} (empty)

13. Middleware (funding_manager.py → funding_requirements()):
    → Returns empty funding requirements to frontend.

14. Frontend (olas-operate-app):
    → BalancesAndRefillRequirementsProvider receives empty requirements.
    → No alert shown to user.

15. User:
    → Sees nothing. Agent running normally.
```

### 5.5 What Happens When Safe Gets Depleted

**On-chain state after several auto-transfers:** EOA = 0.1 xDAI, Safe = 0.3 xDAI

```
── FSM Round ──

16. CheckFundsRound:
    → EOA (0.1) < auto_transfer_threshold (0.21)? YES
    → Safe (0.3) > 0? YES
    → transfer_amount = min(0.3, 2.0 - 0.1) = 0.3 xDAI (all of Safe's native balance)
    → TRANSFER → PrepareSafeToEOATransferRound → TransactionSettlementAbci
    → On-chain state: EOA ≈ 0.4 xDAI, Safe ≈ 0 xDAI

── Middleware Polls ──

17. get_funds_status():
    → EOA = 0.4 > 0.001 threshold → deficit = 0
    → Safe = 0 < 1.0 threshold → deficit = 5.0 - 0 = 5.0 xDAI

18. Handler: _get_adjusted_funds_status()
    → wxDAI balance = 0.5. Adjusted Safe = 0 + 0.5 = 0.5 < 1.0 threshold.
    → Adjusted deficit = 5.0 - 0.5 = 4.5 xDAI

19. HTTP Response:
    {
      "gnosis": {
        "0xAgentEOA": {
          "0x0000...0000": { "balance": "400000000000000000", "deficit": "0", "decimals": 18 }
        },
        "0xSafeAddress": {
          "0x0000...0000": { "balance": "0", "deficit": "4500000000000000000", "decimals": 18 },
          "0xe91D...a97d": { "balance": "500000000000000000", "deficit": "0", "decimals": 18 }
        }
      }
    }

── Middleware → Frontend → User ──

20. Middleware (service.py):
    → Parses: Safe deficit = 4.5 xDAI on gnosis.
    → Passes to frontend via /api/v2/service/{id}/funding_requirements:

    {
      "agent_funding_requests": {
        "gnosis": {
          "0xSafeAddress": {
            "0x0000...0000": "4500000000000000000"
          }
        }
      }
    }

21. Frontend (olas-operate-app):
    → Shows alert: "Fund your Safe with 4.5 xDAI"
    → User sees the Safe address — this is "the wallet" they understand.

22. User:
    → Approves funding. Optionally adjusts the amount.

── Frontend → Middleware → On-Chain ──

23. Frontend:
    → POST /api/v2/service/{id}/fund
    → Body: { "gnosis": { "0xSafeAddress": { "0x0000...0000": "4500000000000000000" } } }

24. Middleware (operate/services/service.py):
    → Transfers 4.5 xDAI from MasterSafe → Agent Safe.
    → On-chain state: EOA ≈ 0.4, Safe ≈ 4.5 xDAI

── Next FSM Cycle ──

25. CheckFundsRound:
    → EOA = 0.4 > 0.21 auto_transfer_threshold → CASE 1 (OK)
    → No transfer needed. FinishedFundsTransferRound → main FSM.

    (If EOA had drained further to e.g. 0.15 xDAI, the round would
     auto-transfer from the newly funded Safe.)
```

### 5.6 Last-Resort Path: EOA Critically Low

**On-chain state:** EOA = 0.0005 xDAI (below safe_tx_value), Safe = 0 xDAI

```
── FSM Round ──

26. CheckFundsRound:
    → EOA (0.0005) < auto_transfer_threshold (0.21)? YES
    → Safe (0) > 0? NO
    → CASE 3: No transfer. FinishedFundsTransferRound.

── Middleware Polls ──

27. get_funds_status():
    → EOA = 0.0005 < 0.001 threshold → deficit = 0.01 - 0.0005 = 0.0095 xDAI
    → Safe = 0 < 1.0 threshold → deficit = 5.0 xDAI

28. HTTP Response:
    {
      "gnosis": {
        "0xAgentEOA": {
          "0x0000...0000": { "balance": "500000000000000", "deficit": "9500000000000000", "decimals": 18 }
        },
        "0xSafeAddress": {
          "0x0000...0000": { "balance": "0", "deficit": "5000000000000000000", "decimals": 18 }
        }
      }
    }

── Middleware → Frontend → User ──

29. Middleware:
    → Sees BOTH EOA deficit (0.0095 xDAI) and Safe deficit (5.0 xDAI).
    → Reports both to frontend.

30. Frontend:
    → Shows: "Fund your agent EOA with 0.0095 xDAI" AND "Fund your Safe with 5.0 xDAI"

31. User:
    → Approves both.

32. Middleware:
    → Transfers from MasterSafe → Agent EOA (0.0095 xDAI)
    → Transfers from MasterSafe → Agent Safe (5.0 xDAI)

── Next FSM Cycle ──

33. CheckFundsRound:
    → EOA now has gas. Safe now has funds.
    → If EOA < auto_transfer_threshold: auto-transfer from Safe.
    → Normal flow resumes.
```

### 5.7 Complete System Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Agent Process                                     │
│                                                                          │
│  ┌──────────────────────────────────┐  ┌──────────────────────────────┐ │
│  │  FundsManagerAbciApp (FSM)        │  │ FundsManagerBehaviour         │ │
│  │                                    │  │ (SimpleBehaviour)             │ │
│  │  Config: auto_transfer             │  │                               │ │
│  │    threshold: 0.21 xDAI            │  │ Config: fund_requirements     │ │
│  │    topup: 2.0 xDAI                 │  │   EOA threshold: 0.001 xDAI  │ │
│  │                                    │  │   EOA topup: 0.01 xDAI       │ │
│  │  Runs: start of each FSM cycle     │  │   Safe threshold: 1.0 xDAI   │ │
│  │  Does: Safe → EOA native transfer  │  │   Safe topup: 5.0 xDAI       │ │
│  │  Via: TransactionSettlementAbci    │  │                               │ │
│  │                                    │  │ Called by: HTTP handler        │ │
│  │  ┌──────────┐ ┌───────────────┐   │  │   on GET /funds-status        │ │
│  │  │CheckFunds│→│PrepareSafeTx  │   │  │                               │ │
│  │  │Round     │ │Round          │   │  │ Returns: { balances, deficits }│ │
│  │  └──────────┘ └───────┬───────┘   │  │                               │ │
│  │                       ▼            │  │ EOA deficit only when          │ │
│  │           ┌───────────────────┐    │  │ EOA < 0.001 xDAI (last resort)│ │
│  │           │TxSettlement ABCI  │    │  │                               │ │
│  │           └───────────────────┘    │  │ Safe deficit when              │ │
│  │                                    │  │ Safe < 1.0 xDAI (normal)      │ │
│  └──────────────────────────────────┘  └──────────────┬───────────────┘ │
│                                                        │                  │
│                                            GET /funds-status              │
│                                                        │                  │
└────────────────────────────────────────────────────────┼──────────────────┘
                                                         │
                                                         ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  olas-operate-middleware                                                    │
│                                                                            │
│  service.py:                                                               │
│    requests.get("http://127.0.0.1:8716/funds-status")                     │
│    Parses JSON → extracts "deficit" per address/token                      │
│    NO CODE CHANGES — reads response exactly as before                      │
│                                                                            │
│  funding_manager.py:                                                       │
│    Aggregates deficits → exposes via API                                   │
│                                                                            │
│  cli.py:                                                                   │
│    GET  /api/v2/service/{id}/funding_requirements → returns deficits       │
│    POST /api/v2/service/{id}/fund → transfers MasterSafe → Agent wallets   │
│                                                                            │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  olas-operate-app (Pearl Frontend)                                         │
│                                                                            │
│  Balance.ts:                                                               │
│    fetch(`/api/v2/service/${id}/funding_requirements`)                     │
│                                                                            │
│  BalancesAndRefillRequirementsProvider.tsx:                                │
│    Polls with exponential backoff                                          │
│    If deficits present → shows funding alert                               │
│    NO CODE CHANGES — displays whatever MW reports                          │
│                                                                            │
│  User sees:                                                                │
│    - Nothing (normal case — auto-transfer handled it)                      │
│    - "Fund your Safe with X" (Safe depleted)                               │
│    - "Fund your EOA with X" (last resort only)                             │
│                                                                            │
│  User approves → POST /api/v2/service/{id}/fund                           │
│    → MW transfers MasterSafe → Agent Safe (or EOA in last resort)          │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Scenario Comparisons

### 6.1 Scenario A: EOA Runs Low — Safe Has Native Funds

**This is the primary scenario the feature addresses.**

Example: Omenstrat on Gnosis. EOA has 0.1 xDAI. Safe has 3.0 xDAI.

#### Current Flow (Today)

```
Step  Actor                   Action
────  ─────                   ──────
1     Agent (funds_manager)   get_funds_status(): EOA = 0.1 < 0.21 threshold.
                              EOA deficit = 2.0 - 0.1 = 1.9 xDAI. Returns to handler.

2     Agent (handler)         Applies wxDAI adjustment. Returns response to MW.

3     Middleware               Sees EOA deficit = 1.9 xDAI. Reports to frontend.

4     User                    Sees: "Fund your agent with 1.9 xDAI"
                              Confused — wallet shows 3.0 xDAI in Safe.
                              Eventually approves.

5     Middleware               Transfers 1.9 xDAI from MasterSafe → Agent EOA.
```

#### New Flow (With Auto-Transfer)

```
Step  Actor                   Action
────  ─────                   ──────
1     Agent (FSM round)       CheckFundsRound: EOA = 0.1 < 0.21 auto_transfer_threshold.
                              Safe = 3.0 xDAI. transfer_amount = min(3.0, 2.0 - 0.1) = 1.9.
                              → PrepareSafeToEOATransferRound → TransactionSettlementAbci.
                              Safe transfers 1.9 xDAI → EOA. EOA = 2.0, Safe = 1.1.

2     Agent (funds_manager)   get_funds_status() (on MW poll):
                              EOA = 2.0 > safe_tx_value threshold → deficit = 0.
                              Safe = 1.1 > 1.0 threshold → deficit = 0.

3     Middleware               No deficits. Nothing to report.

4     User                    Sees nothing. Agent operating normally.
```

**Result:** No user action required.

### 6.2 Scenario B: EOA Runs Low — Safe Depleted After Transfer

Example: Omenstrat. EOA has 0.1 xDAI. Safe has 1.5 xDAI.

#### New Flow

```
Step  Actor                   Action
────  ─────                   ──────
1     Agent (FSM round)       transfer_amount = min(1.5, 1.9) = 1.5 xDAI.
                              Safe → EOA. EOA = 1.6, Safe = 0.

2     Agent (funds_manager)   get_funds_status():
                              EOA = 1.6 > safe_tx_value → deficit = 0.
                              Safe = 0 < 1.0 threshold → deficit = 5.0 - 0 = 5.0 xDAI.

3     Middleware               Reports Safe deficit = 5.0 xDAI.

4     User                    Sees: "Fund your Safe with 5.0 xDAI"
                              Approves → MW funds Safe from MasterSafe.

5     Agent (next cycle)      If EOA still low: auto-transfers from newly funded Safe.
```

**Result:** User funds the Safe (which they understand as "the wallet"), not the EOA.

### 6.3 Scenario C: EOA Runs Low — Safe Already Empty

Example: Polystrat. EOA has 1.0 POL. Safe has 0 native POL (but may have USDC).

#### New Flow

```
Step  Actor                   Action
────  ─────                   ──────
1     Agent (FSM round)       CheckFundsRound: EOA = 1.0 < 3.2 auto_transfer_threshold.
                              Safe native = 0. Cannot transfer. No action.

2     Agent (funds_manager)   get_funds_status():
                              EOA = 1.0 > safe_tx_value → deficit = 0.
                              Safe native = 0 < 10.0 threshold → deficit = 40.0 POL.

3     Agent (handler)         ⚠️ _get_adjusted_funds_status() folds USDC into POL.
                              If USDC covers the gap, Safe deficit may be reduced/eliminated.
                              (This is existing behaviour — see Section 10.3.)

4     Middleware               Reports Safe deficit (adjusted).

5     User                    Approves funding Safe.

6     Agent (next cycle)      Auto-transfers from funded Safe → EOA.
```

### 6.4 Scenario D: EOA Critically Low — Cannot Execute Safe Tx

Example: Agents Fun on Base. EOA has 0.000001 ETH (below safe_tx_value).

#### New Flow (Identical to Current)

```
Step  Actor                   Action
────  ─────                   ──────
1     Agent (FSM round)       CheckFundsRound: EOA = 0.000001 ETH.
                              Estimates gas → EOA cannot afford Safe tx.
                              No transfer. Proceeds to FinishedRound.

2     Agent (funds_manager)   get_funds_status():
                              EOA = 0.000001 < safe_tx_value threshold.
                              EOA deficit = topup - 0.000001 (reported to MW).

3     Middleware               Reports EOA deficit. User funds EOA directly.
```

**This is the existing flow — unchanged.** The last-resort path.

### 6.5 Scenario E: Safe ERC20 Deficit (e.g., USDC.e on Polygon)

**Unchanged.** Auto-transfer only handles native tokens.

```
Agent (funds_manager):  USDC.e deficit calculated normally → MW reports → user funds Safe.
```

### 6.6 Scenario F: Safe Native Low, EOA Fine

**Unchanged.** EOA above threshold → no auto-transfer. Safe below threshold → deficit reported as before.

### 6.7 Scenario Summary

| Scenario | Current: Who is asked? | New: Who is asked? | Change? |
|----------|----------------------|-------------------|---------|
| **A: EOA low, Safe has funds** | User funds EOA | Nobody (auto-transfer) | **Yes** |
| **B: EOA low, Safe depleted after transfer** | User funds EOA | User funds Safe | **Yes** |
| **C: EOA low, Safe empty** | User funds EOA | User funds Safe | **Yes** |
| **D: EOA critically low** | User funds EOA | User funds EOA (fallback) | No |
| **E: Safe ERC20 low** | User funds Safe | User funds Safe | No |
| **F: Safe native low, EOA fine** | User funds Safe | User funds Safe | No |
| **G: Both fine** | Nobody | Nobody | No |

---

## 7. Algorithm

### 7.1 FSM Round Decision Logic

Evaluated in `CheckFundsRound` at the start of each FSM cycle:

```
for each chain in auto_transfer config:

    eoa_balance  = Agent EOA native balance (via multicall or ledger API)
    safe_balance = Agent Safe native balance (raw, NOT adjusted for wxDAI/USDC)

    # From auto_transfer config (new)
    threshold    = auto_transfer_threshold for this chain
    topup        = auto_transfer_topup for this chain

    # ─── CASE 1: EOA is sufficiently funded ───
    if eoa_balance >= threshold:
        action = NONE
        → FinishedFundsTransferRound

    # ─── CASE 2: EOA is low, Safe has native funds ───
    elif safe_balance > 0:
        # Verify EOA can afford the Safe tx gas
        estimated_gas = estimate_safe_tx_gas(chain)
        if eoa_balance < estimated_gas:
            action = NONE  # degenerate case — can't execute Safe tx
            → FinishedFundsTransferRound
        else:
            transfer_amount = min(safe_balance, topup - eoa_balance)
            action = TRANSFER(chain, transfer_amount)
            → PrepareSafeToEOATransferRound

    # ─── CASE 3: EOA is low, Safe has no native funds ───
    else:
        action = NONE  # nothing to transfer, wait for user to fund Safe
        → FinishedFundsTransferRound
```

### 7.2 `/funds-status` Deficit Logic (Unchanged Code, New Config Values)

The existing `get_funds_status()` code is unchanged. Only the configured threshold values change:

```
# Existing code (no changes):
deficit = max(topup - balance, 0) if balance < threshold else 0

# What changes is the CONFIG:
# Before: threshold = 0.21 xDAI (operational level)
# After:  threshold = safe_tx_value (e.g., 0.001 xDAI)
#
# Effect: deficit is only non-zero when EOA < safe_tx_value
#         (i.e., when EOA literally cannot execute any transaction)
```

### 7.3 Transfer Amount

```python
transfer_amount = min(safe_balance, auto_transfer_topup - eoa_balance)
```

- Capped at `auto_transfer_topup - eoa_balance` — don't overshoot the target.
- Capped at `safe_balance` — can't transfer more than available.

### 7.4 FSM Round Flow

```
                          ┌──────────────────────┐
                          │    CheckFundsRound    │
                          │                       │
                          │  1. Read EOA balance   │
                          │  2. Read Safe balance  │
                          │  3. Evaluate Cases 1-3 │
                          │  4. Estimate gas if    │
                          │     Case 2 applies     │
                          └──────────┬─────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                 │
              Case 1 (OK)    Case 2 (transfer)   Case 3 (no safe funds /
                    │                │             EOA can't afford gas)
                    │                ▼                 │
                    │   ┌────────────────────────┐    │
                    │   │ PrepareSafeToEOA        │    │
                    │   │ TransferRound           │    │
                    │   │                         │    │
                    │   │ Build Safe execTx:      │    │
                    │   │   to: EOA address       │    │
                    │   │   value: transfer_amount│    │
                    │   │   data: 0x              │    │
                    │   │   operation: CALL        │    │
                    │   └───────────┬──────────────┘    │
                    │               │                   │
                    │               ▼                   │
                    │   ┌────────────────────────┐      │
                    │   │ TransactionSettlement   │      │
                    │   │ AbciApp                 │      │
                    │   │                         │      │
                    │   │ Standard OA flow:       │      │
                    │   │ sign → submit → confirm │      │
                    │   └───────────┬──────────────┘      │
                    │               │                     │
                    ▼               ▼                     ▼
              ┌─────────────────────────────────────────────┐
              │       FinishedFundsTransferRound             │
              │                                              │
              │  → Return to agent's main FSM                │
              └──────────────────────────────────────────────┘
```

---

## 8. Configuration

### 8.1 New Parameters: `auto_transfer` (Per Chain)

New configuration in the `funds_manager` skill, added to `Params` model:

```yaml
# skill.yaml / service.yaml override for valory/funds_manager
models:
  params:
    args:
      # Existing (unchanged code, reconfigured values):
      fund_requirements: { ... }    # EOA threshold lowered to safe_tx_value
      rpc_urls: { ... }
      safe_contract_addresses: { ... }

      # New:
      auto_transfer:
        gnosis:
          threshold: 210000000000000000     # 0.21 xDAI — triggers auto-transfer
          topup: 2000000000000000000        # 2.0 xDAI — target EOA balance
        polygon:
          threshold: 3200000000000000000    # 3.2 POL
          topup: 30000000000000000000       # 30.0 POL
        optimism:
          threshold: 100000000000000        # 0.0001 ETH
          topup: 200000000000000            # 0.0002 ETH
        base:
          threshold: 99000000000000         # ~0.000099 ETH
          topup: 325700000000000            # ~0.000326 ETH
```

These are the values that are **currently** configured as the EOA native threshold/topup in `fund_requirements`. They move here.

### 8.2 Reconfigured: `fund_requirements` EOA Native Threshold

The EOA native `threshold` in `fund_requirements` is **lowered to `safe_tx_value`** — the minimum balance needed to execute a Safe transaction. This ensures `/funds-status` only reports an EOA deficit as a last resort.

The EOA native `topup` in `fund_requirements` is also lowered — when MW does fund the EOA directly (last resort), it only needs to provide enough for the EOA to execute the Safe→EOA auto-transfer.

```yaml
# Before (current):
fund_requirements:
  gnosis:
    agent:
      "0x0000...0000":
        topup: 2000000000000000000       # 2.0 xDAI
        threshold: 210000000000000000    # 0.21 xDAI

# After (reconfigured):
fund_requirements:
  gnosis:
    agent:
      "0x0000...0000":
        topup: 10000000000000000         # 0.01 xDAI (enough for a few Safe txs)
        threshold: 1000000000000000      # 0.001 xDAI (safe_tx_value)
```

### 8.3 Updated `Params` Model

```python
class Params(Model):
    def __init__(self, *args, **kwargs):
        # Existing (unchanged)
        self.rpc_urls: Dict[str, str] = ...
        self.safe_contract_addresses: Dict[str, str] = ...
        self.fund_requirements: FundRequirements = ...

        # New (optional — defaults to empty dict for backward compat)
        self.auto_transfer: Dict[str, Dict[str, int]] = self._ensure_get_optional(
            "auto_transfer", kwargs, Dict[str, Any], default={}
        )
        # Structure: { chain_name: { "threshold": int, "topup": int } }
```

When `auto_transfer` is not configured, the FSM round is a no-op. Agents that don't configure it retain the current behaviour entirely.

### 8.4 Required Per-Agent Configuration Changes

#### Optimus (Optimism)

Safe native funding must be enabled (currently disabled):

```yaml
# fund_requirements: EOA threshold lowered, Safe enabled
fund_requirements:
  optimism:
    agent:
      "0x0000...0000":
        topup: 10000000000000           # 0.00001 ETH (enough for Safe tx + margin)
        threshold: 5000000000000        # 0.000005 ETH (safe_tx_value)
    safe:
      "0x0000...0000":
        topup: <TBD>                    # needs to be set (currently 0)
        threshold: <TBD>                # needs to be set (currently 0)

# auto_transfer: values that were previously EOA threshold/topup
auto_transfer:
  optimism:
    threshold: 100000000000000          # 0.0001 ETH
    topup: 200000000000000              # 0.0002 ETH
```

#### Omenstrat (Gnosis)

```yaml
fund_requirements:
  gnosis:
    agent:
      "0x0000...0000":
        topup: 10000000000000000        # 0.01 xDAI
        threshold: 1000000000000000     # 0.001 xDAI (safe_tx_value)
    safe:                               # unchanged
      "0x0000...0000":
        topup: 5000000000000000000
        threshold: 1000000000000000000
      "0xe91D...a97d":                  # wxDAI — unchanged
        topup: 0
        threshold: 0

auto_transfer:
  gnosis:
    threshold: 210000000000000000       # 0.21 xDAI (was EOA threshold)
    topup: 2000000000000000000          # 2.0 xDAI (was EOA topup)
```

#### Polystrat (Polygon)

```yaml
fund_requirements:
  polygon:
    agent:
      "0x0000...0000":
        topup: 100000000000000000       # 0.1 POL
        threshold: 10000000000000000    # 0.01 POL (safe_tx_value)
    safe:                               # unchanged
      "0x0000...0000":
        topup: 40000000000000000000
        threshold: 10000000000000000000
      "0x2791...4174":                  # USDC.e — unchanged
        topup: 65000000
        threshold: 16000000
      "0x3c49...3359":                  # native USDC — unchanged
        topup: 0
        threshold: 0

auto_transfer:
  polygon:
    threshold: 3200000000000000000      # 3.2 POL (was EOA threshold)
    topup: 30000000000000000000         # 30.0 POL (was EOA topup)
```

#### Agents Fun (Base)

```yaml
fund_requirements:
  base:
    agent:
      "0x0000...0000":
        topup: 10000000000000           # 0.00001 ETH
        threshold: 5000000000000        # 0.000005 ETH (safe_tx_value)
    safe:                               # unchanged
      "0x0000...0000":
        topup: 1628500000000000
        threshold: 99000000000000

auto_transfer:
  base:
    threshold: 99000000000000           # ~0.000099 ETH (was EOA threshold)
    topup: 325700000000000              # ~0.000326 ETH (was EOA topup)
```

---

## 9. Repo Structure

All new code lives in the `funds_manager` skill in this repo:

```
packages/valory/skills/funds_manager/
├── __init__.py              # existing — update PUBLIC_ID version
├── behaviours.py            # existing — UNCHANGED (SimpleBehaviour for get_funds_status)
├── models.py                # existing — add auto_transfer to Params
├── skill.yaml               # existing — register new components, add deps
│
├── rounds.py                # NEW — CheckFundsRound, PrepareSafeToEOATransferRound,
│                            #        FinishedFundsTransferRound
├── fsm_behaviours.py        # NEW — FSM behaviours tied to rounds
│                            #        (CheckFundsBehaviour, PrepareSafeToEOATransferBehaviour)
├── payloads.py              # NEW — round payloads
├── composition.py           # NEW — FundsManagerAbciApp definition + transition mapping
│
└── tests/
    ├── ...                  # existing tests — unchanged
    ├── test_rounds.py       # NEW — round logic tests
    ├── test_fsm.py          # NEW — FSM transition tests
    └── test_composition.py  # NEW — AbciApp composition tests
```

Each agent imports and composes the AbciApp:

```python
# In each agent's composition.py:
from packages.valory.skills.funds_manager.composition import FundsManagerAbciApp

# Add to the FSM chain, typically at the start of each cycle:
abci_app_transition_mapping: AbciAppTransitionMapping = {
    FundsManagerAbciApp: {
        FinishedFundsTransferRound: <AgentMainAbciApp>,  # proceed to main logic
    },
    <AgentMainAbciApp>: {
        ...
    },
    ...
}
```

---

## 10. Per-Agent Impact Analysis

### 10.1 Optimus (Optimism)

| Item | Detail |
|------|--------|
| **FSM change** | Add `FundsManagerAbciApp` to FSM composition. |
| **Config change** | Enable Safe native funding (currently topup=0). Add `auto_transfer` config. Lower EOA threshold to `safe_tx_value`. |
| **x402 interaction** | x402 swaps (ETH→USDC via LiFi) execute from the EOA in a **background thread** and consume EOA ETH (gas + swap value). This accelerates EOA drain, making auto-transfer more important. The FSM round tops up the EOA at the start of each cycle. **EOA nonce risk:** the x402 swap submits an EOA tx in a background thread while the FSM may also submit an EOA tx (the Safe `execTransaction` call). These use the same EOA nonce sequence. In practice, the FSM round runs at cycle start and the x402 fires on MW poll — they're unlikely to overlap, but a nonce collision is theoretically possible. |
| **x402 deficit injection** | When x402 swap fails, `_inject_x402_eth_deficit()` adds ETH deficit to the `/funds-status` response. With the new config (EOA threshold = safe_tx_value), this injection only matters when EOA is already critically low. In normal operation, EOA deficit = 0 and the injection sets it to the ETH needed for the swap — MW would then fund the EOA directly. |
| **Withdrawal mode** | Handler overrides the entire `/funds-status` response. Since we're not changing `get_funds_status()` code, this is unaffected. The FSM round may still auto-transfer during withdrawal — this is fine, it ensures the EOA has gas for withdrawal transactions. |

### 10.2 Omenstrat (Gnosis)

| Item | Detail |
|------|--------|
| **FSM change** | Add `FundsManagerAbciApp` to FSM composition. |
| **Config change** | Add `auto_transfer` config. Lower EOA threshold to `safe_tx_value`. |
| **wxDAI adjustment** | The FSM round only considers the Safe's **native xDAI** balance when deciding the transfer amount — not wxDAI. wxDAI cannot be auto-transferred as gas. The handler's wxDAI folding continues unchanged — it only affects the `/funds-status` response for the Safe's reported deficit. No conflict. |
| **x402 interaction** | Omenstrat shares the same handler code as Polystrat. When `use_x402=true`, it runs the same ETH→USDC swap via LiFi in a background thread. Same EOA nonce risk as Optimus. Same deficit injection on failure. |

### 10.3 Polystrat (Polygon) — KNOWN INTERACTION

| Item | Detail |
|------|--------|
| **FSM change** | Add `FundsManagerAbciApp` to FSM composition. |
| **Config change** | Add `auto_transfer` config. Lower EOA threshold to `safe_tx_value`. |
| **USDC→POL adjustment** | **Known interaction (existing behaviour, not a new conflict).** See below. |
| **x402 interaction** | Same as Omenstrat — shares handler code. ETH→USDC swap in background thread. Same EOA nonce risk. Same deficit injection on failure. |

**The USDC→POL adjustment interaction explained:**

When auto-transfer depletes the Safe's native POL, the Safe's native deficit should be reported to the user. However, the Polystrat handler's `_get_adjusted_funds_status()` converts USDC to POL-equivalent and folds it into the native balance. If the Safe holds enough USDC, this adjustment can eliminate the native POL deficit — the user is never prompted to add native POL.

This is **existing behaviour** that exists today (the handler has always done this). Auto-transfer doesn't change the handler code. But auto-transfer makes the interaction more visible: the Safe's native POL gets drained by auto-transfers, and if the handler keeps masking the deficit with USDC-equivalent, the Safe never gets refilled with native POL.

**Resolution options (future follow-up):**
- **Option A:** Modify the Polystrat handler to not apply USDC→POL adjustment to the Safe's native deficit. The USDC adjustment still applies for informational display.
- **Option B:** Move the adjustment logic from per-agent handlers into the `funds_manager` skill, making it aware of auto-transfer needs.

### 10.4 Agents Fun / meme-ooorr (Base)

| Item | Detail |
|------|--------|
| **FSM change** | Add `FundsManagerAbciApp` to FSM composition, **before** the existing `CheckFundsBehaviour`. |
| **Config change** | Add `auto_transfer` config. Lower EOA threshold to `safe_tx_value`. |
| **CheckFundsBehaviour interaction** | meme-ooorr has its own `CheckFundsBehaviour` that gates FSM transitions on `minimum_gas_balance` (0.00005 ETH). After `FundsManagerAbciApp` tops up the EOA, this check passes. Ensure `FundsManagerAbciApp` is composed **before** `CheckFundsBehaviour` in the FSM chain. |
| **x402 interaction** | Same background thread swap mechanism. **However, meme-ooorr does NOT inject ETH deficit into `/funds-status` on swap failure** — unlike Optimus and Trader. The swap failure is silent. This means if the swap keeps failing, the EOA drains from failed swap gas costs but MW is not informed to provide extra ETH. |

### 10.5 x402 EOA Nonce Risk (All Agents)

The x402 swap runs in a **background thread** (ThreadPoolExecutor) triggered by each `/funds-status` poll. The auto-transfer runs in the **FSM** at the start of each cycle. Both submit EOA transactions:

- **Auto-transfer:** EOA signs and submits `Safe.execTransaction()` — uses EOA nonce N.
- **x402 swap:** EOA signs and submits LiFi swap tx — uses EOA nonce N (or N+1).

If both attempt to submit simultaneously, one will fail due to nonce collision. In practice:
- The FSM round runs at cycle start (before MW polls).
- The x402 swap fires on MW poll (which happens asynchronously, any time).
- Overlap is unlikely but not impossible.

**Mitigation options (future follow-up):**
- Move x402 swap into the FSM round chain (after auto-transfer), serializing both EOA transactions.
- Add a shared lock in `shared_state` to prevent concurrent EOA tx submission.
- Accept the risk — failed tx is retried on the next cycle/poll.

---

## 11. Edge Cases

### 11.1 Auto-Transfer Depletes Safe Below Its Threshold

**Scenario:** Safe has 2 xDAI. EOA needs 1.9 xDAI. After transfer, Safe has 0.1 xDAI (below 1.0 threshold).

**Behaviour:** Expected and correct. On the next MW poll, `/funds-status` reports a Safe deficit (existing code, no changes). User funds the Safe. This is the intended flow — the user always funds the Safe, and the agent manages the EOA internally.

### 11.2 Transfer Fails (Gas Estimation, Revert, Timeout)

**Scenario:** The Safe→EOA transfer transaction fails for any reason.

**Behaviour:** `TransactionSettlementAbci` handles retries per its standard logic. If all retries are exhausted, `FinishedFundsTransferRound` completes and the FSM proceeds to the agent's main logic. On the next FSM cycle, `CheckFundsRound` re-evaluates and attempts again.

If the EOA drains below `safe_tx_value` before a successful transfer, the existing `/funds-status` deficit logic kicks in (Path 3 — MW funds EOA directly).

### 11.3 Agent Restart During Pending Transfer

**Scenario:** Agent restarts while a Safe→EOA transfer is in-flight.

**Behaviour:** On restart, the FSM starts fresh. `CheckFundsRound` reads current on-chain balances:
- If the transfer confirmed: balances reflect it, no further action.
- If the transfer failed/is still pending: the round re-evaluates and may attempt a new transfer.

### 11.4 Multiple Chains

**Scenario:** Agent operates on multiple chains (e.g., future Optimus on Optimism + Base + Mode).

**Behaviour:** `CheckFundsRound` evaluates each chain's `auto_transfer` config independently. If multiple chains need transfers, they are executed sequentially — one Safe tx per chain, each going through `TransactionSettlementAbci`. Each chain uses a different Safe contract and different RPC.

### 11.5 Safe Has Only Wrapped/ERC20 Tokens, No Native

**Scenario:** Omenstrat Safe has 0 xDAI but 5 wxDAI. EOA needs xDAI.

**Behaviour:** The FSM round only looks at native token balance. Safe native = 0, so Case 3 (no transfer). The Safe's deficit is reported via `/funds-status` (existing behaviour). The user funds the Safe with native xDAI.

Future enhancement: unwrap wxDAI→xDAI in the Safe before transferring. Out of scope for this spec.

### 11.6 Concurrent Safe Transactions

**Scenario:** Could the auto-transfer round conflict with other Safe transactions from the agent's FSM?

**Behaviour:** No. The FSM serializes all operations. `FundsManagerAbciApp` runs at the start of the cycle, before any other Safe transactions. When it's executing (including `TransactionSettlementAbci`), no other round can submit a Safe transaction. This is why we use an FSM round rather than doing it from the SimpleBehaviour.

### 11.7 Gas Price Spike

**Scenario:** Gas price spikes. EOA has enough balance for a normal Safe tx but not at the current price.

**Behaviour:** `CheckFundsRound` estimates gas at runtime before attempting the transfer. If the EOA can't afford it at current prices, the round skips the transfer (Case 2 gas check fails). On the next cycle, gas may have normalized. If the EOA keeps draining and hits `safe_tx_value`, Path 3 kicks in (MW funds EOA).

Mitigation: configure `auto_transfer_threshold` with a comfortable margin above typical Safe tx gas costs.

### 11.8 `/funds-status` Polled While Transfer Is In-Flight

**Scenario:** The FSM is executing a Safe→EOA transfer. MW polls `/funds-status` at the same time.

**Behaviour:** `get_funds_status()` reads current on-chain balances. Since the transfer hasn't confirmed yet, the EOA is still below `safe_tx_value` threshold — but that threshold is very low, so `get_funds_status()` may return deficit = 0 (if EOA > safe_tx_value) or a small deficit (if EOA < safe_tx_value).

This is benign. If MW sees a small EOA deficit, it may prompt the user — but this is the last-resort path and rare. The transfer will confirm shortly, resolving the situation. No special handling needed.

---

## 12. Implementation Plan

### Phase 1: Core Logic in `funds_manager` Skill (this repo)

**Changes to existing files:**

1. **`models.py`** — Add `auto_transfer` to `Params`:
   ```python
   self.auto_transfer: Dict[str, Dict[str, int]] = ...
   # { chain: { "threshold": int, "topup": int } }
   ```

2. **`skill.yaml`** — Register new rounds, behaviours, payloads. Add dependency on `TransactionSettlementAbci` and Safe contract.

**New files:**

3. **`rounds.py`** — FSM rounds:
   - `CheckFundsRound` — reads balances (reuses multicall logic or ledger API), evaluates auto-transfer cases, estimates gas.
   - `PrepareSafeToEOATransferRound` — builds Safe `execTransaction` payload.
   - `FinishedFundsTransferRound` — terminal round, returns to agent FSM.

4. **`fsm_behaviours.py`** — Behaviours tied to rounds:
   - `CheckFundsBehaviour` — calls multicall, submits payload to `CheckFundsRound`.
   - `PrepareSafeToEOATransferBehaviour` — builds tx, submits payload.

5. **`payloads.py`** — Payload classes for each round.

6. **`composition.py`** — `FundsManagerAbciApp` with transition mapping:
   ```python
   class FundsManagerAbciApp(AbciApp):
       transition_function = {
           CheckFundsRound: {
               Event.TRANSFER: PrepareSafeToEOATransferRound,
               Event.NO_TRANSFER: FinishedFundsTransferRound,
           },
           PrepareSafeToEOATransferRound: {
               Event.DONE: FinishedFundsTransferRound,
           },
           FinishedFundsTransferRound: {},
       }
   ```

### Phase 2: Per-Agent Integration (external repos)

Each agent repo:
1. Update `composition.py` to import and compose `FundsManagerAbciApp` at the start of their FSM cycle.
2. Update `service.yaml`:
   - Lower EOA native threshold/topup in `fund_requirements` to `safe_tx_value`.
   - Add `auto_transfer` config with the previous EOA threshold/topup values.
3. For Optimus: enable Safe native funding.
4. For meme-ooorr: ensure `FundsManagerAbciApp` runs before `CheckFundsBehaviour`.

### Phase 3: Testing

1. **Unit tests** — `CheckFundsRound` decision logic (all cases).
2. **Unit tests** — `PrepareSafeToEOATransferRound` payload building.
3. **Unit tests** — FSM transitions (transfer vs no-transfer paths).
4. **Unit tests** — `Params` model with `auto_transfer` config (valid/invalid).
5. **Integration tests** — full round-trip with mocked multicall and Safe tx.
6. **Per-agent tests** — FSM composition with `FundsManagerAbciApp` chained.

### Phase 4: Follow-up (Polystrat Handler Conflict)

Address the Polystrat USDC→POL handler adjustment interaction (Section 10.3). Either:
- Modify handler to not suppress native POL deficits.
- Or move adjustment logic into `funds_manager` skill.

---

## Appendix A: Safe Transaction for Native Transfer

A native token transfer from Safe → EOA is a Safe `execTransaction` call:

```
to:             Agent EOA address
value:          transfer_amount (in wei)
data:           0x (empty — simple value transfer)
operation:      0 (CALL)
safeTxGas:      0 (use all available)
baseGas:        0
gasPrice:       0 (no refund)
gasToken:       0x0000000000000000000000000000000000000000
refundReceiver: 0x0000000000000000000000000000000000000000
signatures:     agent's signature (1-of-1)
```

The EOA signs this Safe transaction hash and submits the `execTransaction` call as a regular Ethereum transaction. Gas is paid by the EOA from its native balance.

---

## Appendix B: Configuration Before vs After

### Omenstrat (Gnosis) — Full Example

**Before:**
```yaml
# fund_requirements
gnosis:
  agent:
    "0x0000...0000": { topup: 2000000000000000000, threshold: 210000000000000000 }   # 2.0 / 0.21 xDAI
  safe:
    "0x0000...0000": { topup: 5000000000000000000, threshold: 1000000000000000000 }  # 5.0 / 1.0 xDAI
    "0xe91D...a97d": { topup: 0, threshold: 0 }                                      # wxDAI tracking

# auto_transfer: (does not exist)
```

**After:**
```yaml
# fund_requirements — EOA threshold lowered to safe_tx_value
gnosis:
  agent:
    "0x0000...0000": { topup: 10000000000000000, threshold: 1000000000000000 }       # 0.01 / 0.001 xDAI
  safe:
    "0x0000...0000": { topup: 5000000000000000000, threshold: 1000000000000000000 }  # 5.0 / 1.0 xDAI (unchanged)
    "0xe91D...a97d": { topup: 0, threshold: 0 }                                      # wxDAI (unchanged)

# auto_transfer — values that were previously EOA threshold/topup
gnosis: { threshold: 210000000000000000, topup: 2000000000000000000 }                # 0.21 / 2.0 xDAI
```

**Effect:**
- `/funds-status` only reports EOA deficit when EOA < 0.001 xDAI (last resort).
- FSM round auto-transfers when EOA < 0.21 xDAI, targeting 2.0 xDAI.
- Safe config is completely unchanged.
