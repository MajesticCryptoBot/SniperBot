# Bot fee — sell-side 1% (configurable), charged in SOL

The bot collects a fee on **sell trades** — the industry-standard model
(Maestro, BONKbot and Trojan all charge ~1% of sell volume). The fee is
**not** taken by the bot in a second transaction: it is charged **atomically
inside the Jupiter swap** using Jupiter's Referral Program, so there is no
failure mode where a trade succeeds but the fee is missed.

- Fee mint: Jupiter picks it by its priority list — **SOL first** — so on a
  token → SOL sell the fee is charged in SOL. Buys are never charged.
- Range: 50–255 bps (Jupiter's hard limit). Default `BOT_FEE_BPS=100` = 1%.
- Jupiter keeps 20% of the charged fee (this replaces Jupiter's own platform
  fee — there is no extra cost).
- The user sees the fee **before confirming**: the sell preview quotes the
  swap with the fee active, so "Indicative SOL out" is already the net amount,
  and a `💠 Bot fee: 1.00%` line is shown.

## One-time enrollment (you, the owner)

1. Go to **https://referral.jup.ag/** and connect your **fee wallet**.
2. Create a **Referral account** (this is an on-chain account owned by your
   fee wallet).
3. On the same dashboard create a **referral token account for WSOL**
   (`So11111111111111111111111111111111111111112`). **This step is mandatory**
   — if the referral token account for the fee mint does not exist, Jupiter
   executes the swap **without your fee, silently**.
4. Optionally create token accounts for USDC/USDT too, in case Jupiter ever
   picks a stablecoin as fee mint.

## Configuration (.env)

```
JUP_REFERRAL_ACCOUNT=<referral account address from step 2>
BOT_FEE_BPS=100          # 1% — allowed range 50-255; 0 disables the fee
```

Both are read at boot; invalid values stop the bot with a clear ConfigError.
Without `JUP_REFERRAL_ACCOUNT` the bot runs exactly as before (no fee).

## Admin command

`/admin_fees` (admins only) shows:

- fee status, percentage and referral account address;
- totals from the audit ledger (per fee mint, SOL value where known);
- the balances currently waiting inside the referral account (live on-chain
  lookup), and claim instructions.

Fees accumulate **inside the Jupiter referral account**. Claim them at
https://referral.jup.ag/ with the fee wallet that owns the account — that is
the official, safe route; no private key of the fee wallet is ever needed by
the bot itself.

## Audit ledger

Every executed sell that Jupiter charged a fee for gets a row in the
`fee_ledger` table (created automatically by `python migrate.py`):

| column | meaning |
| --- | --- |
| trade_id | the trades row it belongs to |
| token_mint | the token that was sold |
| fee_mint | the mint the fee was charged in |
| fee_bps | fee rate that was active |
| fee_amount_raw | best-known fee amount in fee-mint raw units (executed totals minus net taker amount, falling back to the quote estimate) |
| fee_value_sol | SOL value of the fee when fee_mint is WSOL |
| volume_sol | net SOL proceeds of the sell |

Tests: `tests/test_bot_fee.py` covers config validation, request params,
fee math, the ledger and the full sell/buy flows.

## Why did my enrollment transaction fail? (funding — read this first)

Creating the Referral account and its WSOL referral token account are real
on-chain transactions, and the dashboard simulates them before sending. An
**empty fee wallet** therefore fails the simulation with an insufficient
funds error — that does not mean the feature is broken, only that the wallet
has no SOL yet. The one-time cost is small:

- ~0.0016 SOL rent for the Referral account
- ~0.002 SOL rent for the WSOL referral token account
- small network fees for the two transactions

**~0.01 SOL in the fee wallet covers the whole enrollment.** Fund the fee
wallet, then repeat the two steps: create the Referral account, then create
its WSOL referral token account. Only after BOTH exist, put the referral
account address into `JUP_REFERRAL_ACCOUNT` in `.env` and restart the bot.

Until then, leave `JUP_REFERRAL_ACCOUNT=` empty: every swap runs fee-free
exactly as before, and `/admin_fees` will show the disabled state with these
same instructions.
