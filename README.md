# Department 3 — Base DeFi intelligence as an MCP server

Three JSON feeds from Base mainnet, sold per call over
[x402](https://x402.org), or from a **$50 prepaid credit pack** by card:

| Feed | Price |
| --- | --- |
| Base block + three WETH/USDC pools | **0.005 USDC** |
| USDC transfers of 25,000 USD or more | **0.010 USDC** |
| Cross-DEX spreads after fees and gas | **0.015 USDC** |

This repo is a drop-in MCP client for all three. It is a single Python file with
no dependencies, so it runs on whatever `python3` you already have.

```text
https://x402-dep3-edge.702digital66.workers.dev
```

## What you get back

One JSON envelope: the feed, the settlement receipt, and the ledger record. Each
route returns only its own dataset — a 0.005 call never contains the arbitrage
or whale data.

```json
{
  "status": "OK",
  "resource": "/telemetry",
  "latency_ms": 298.5,
  "telemetry": {
    "status": "ok",
    "chain": "Base Mainnet",
    "chain_id": 8453,
    "observed_at": "2026-11-16T10:44:33.774621+00:00",
    "block": {
      "number": 58177488,
      "hash": "0x0033cabee89c28dcbf7a3a88364555beeed8df8417d58352ca84bde092d6778b",
      "timestamp": 1794825873,
      "base_fee_gwei": 0.005,
      "gas_used": 6434811,
      "gas_limit": 40000000,
      "gas_utilization": 0.16087,
      "transaction_count": 42
    },
    "pools": [
      {
        "label": "USDC/WETH 0.05%",
        "address": "0xd0b53D9277642d899DF5C87A3966A349A798F224",
        "price_token1_per_token0": 1.804480152e-21,
        "tick": -201340,
        "sqrt_price_x96": "3365546833852245184194082"
      }
    ]
  },
  "receipt": {
    "rail": "stripe_prepaid_credit",
    "settlement": "CREDIT_VERIFIED_AT_ORIGIN",
    "amount_usdc": 5000,
    "asset": "USDC",
    "network": "eip155:8453",
    "verified": true
  },
  "ledger": {
    "department_record_id": "cbe70546b28911f95258bf75f6734836",
    "global_record_id": "accd48f7df0b7921a66e664ea1927397",
    "linked": true
  }
}
```

`telemetry` is the feed. `receipt` is what you paid with and how it was verified.
`ledger` is the hash-linked audit record, so a buyer can prove what was served.

Everything is raw chain data: no estimates, no aggregation, no model in the path.

## Install

Nothing to install. Clone it, then check the connection:

```sh
git clone https://github.com/Doja66/dep3-telemetry-mcp.git
cd dep3-telemetry-mcp
python3 dep3_mcp_server.py --demo
```

`--demo` reads the public discovery file and prints the live price, pay-to
address and checkout link. It costs nothing.

## Connect your agent

### Claude Code — one command

```sh
claude mcp add dep3-telemetry -- python3 /absolute/path/to/dep3_mcp_server.py
```

PowerShell:

```powershell
claude mcp add dep3-telemetry -- python "$PWD\dep3_mcp_server.py"
```

### Cursor / Claude Code project file

Copy `mcp.json` to `.cursor/mcp.json` (Cursor) or `.mcp.json` (Claude Code), and
replace the `args` path with the absolute path to `dep3_mcp_server.py`.

### OpenCode

Copy `opencode.json` to your project root and fix the `command` path. OpenCode V2
reads servers from `mcp.servers`.

### Any other MCP client

It is a standard stdio server: run `python3 dep3_mcp_server.py` and speak
newline-delimited JSON-RPC 2.0 on stdin/stdout. No HTTP, no ports, no OAuth.

## Paying

The server stays free to install. Calls are metered at the edge.

### Rail A — x402, per call

1. Call `get_telemetry` with no credentials. It returns HTTP 402 with the x402 v2
   PaymentRequired object, both in the `PAYMENT-REQUIRED` header (base64 JSON)
   and in the body. It names the amount, the USDC contract on Base, and `payTo`.
2. Sign an EIP-3009 `transferWithAuthorization` for that amount and send it as
   `PAYMENT-SIGNATURE: <base64 JSON>`. A standard x402 client does this on its
   own; the challenge declares `extra.assetTransferMethod: "eip3009"`.
3. The edge verifies the authorization through a facilitator, serves the data,
   and only then settles on Base. Settlement happens after the call succeeds, so
   a failed call is never charged. The facilitator pays the gas.

Manual fallback, for a wallet that would rather send the transfer itself:
transfer 5000 USDC units (6 decimals) on Base to `payTo`, then call again with
the transaction hash as `receipt`, or set `DEP3_RECEIPT`. That path is verified
on-chain before it serves — status `0x1`, the `Transfer` log from the USDC
contract, matching recipient and amount — and a hash can be spent once.

### Rail B — prepaid credit, by card

1. Buy the pack: **10,000 calls for $50.00** at
   <https://buy.stripe.com/9B6eVc4610pE5SYgcLgYU03>. Same $0.005 per call, paid
   once instead of 10,000 times.
2. Stripe redirects to `/credits/claim`, which returns a credit key `ck_...`.
3. Set `DEP3_CREDIT_KEY=ck_...` in the server's environment.

Each call debits 0.005 USD from the balance. If the origin is unavailable, the
debit is refunded automatically, so a failed call is not a lost call. Pass
`idempotency_key` on retries so one call is not charged twice.

## Tools

| Tool | Cost | What it does |
| --- | --- | --- |
| `get_base_telemetry` | 0.005 USD | Block + three WETH/USDC pools. Takes `fields`. |
| `get_base_whale_flow` | 0.010 USD | USDC transfers ≥ 25,000 USD. Takes `min_usd`. |
| `get_base_arb_spreads` | 0.015 USD | Spreads after both fees and gas. Takes `min_net_bps`. |
| `get_payment_options` | free | Prices, pay-to address, network, 402 challenges, checkout link. |
| `get_balance` | free | Remaining credit and calls for the configured key. |

`get_telemetry` is still accepted as an alias for `get_base_telemetry`, so an
older config keeps working, but it is not advertised in `tools/list`.

## Configuration

| Variable | Meaning |
| --- | --- |
| `DEP3_CREDIT_KEY` | Prepaid key `ck_...` for the card rail. |
| `DEP3_RECEIPT` | x402 transaction hash for the crypto rail. |
| `DEP3_BASE_URL` | Override the service host (mirrors, staging). |

Keys live in the client's environment, never in the repo. `.env` is gitignored.

## Verify without spending

```sh
python3 -m unittest test_protocol.py
```

The tests stub the HTTP layer and check the protocol surface: `initialize`,
`tools/list`, `tools/call`, unknown methods, and the no-id notification rule.

## Status and limits

- One region. Uptime is best-effort, with automatic refunds on the credit rail
  when upstream is unreachable.
- On-chain settlement is Base mainnet (`eip155:8453`), USDC
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
- No account, no signup, no cookies, no tracking.

## Licence

MIT. See `LICENSE`.
