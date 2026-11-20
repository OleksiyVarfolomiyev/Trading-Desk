# Neutral market-making tests on c248

Two neutral two-sided ladders, $11 a level, 5 levels each way, quoting only while the coin ranges (5-day efficiency under 0.4).

| Ladder | Started | Spacing | Fills so far | Result so far |
|---|---|---|---|---|
| ETH | 2026-11-16 17:04 | 0.1 ATR (58 bps) | 72 (11-20) | net about -$0.05 on $800 traded; 5m markout -6.3 bps (t -1.4); no edge |
| NEAR | 2026-11-20 00:52 | 0.05 ATR (56 bps), the default | 0 | just started |

**Why NEAR (candidates measured 2026-11-20, quoted spread median of 3 book reads):**

| Coin | Spread bps | Depth ±10bp | 24h volume | ATR bps | 5d efficiency | Funding APR | Verdict |
|---|---|---|---|---|---|---|---|
| NEAR | 2.5 | $335K | $56M | 885 | 0.27 | -0.2% | **chosen**: widest spread with real depth and volume, ranging |
| kPEPE | 2.7 | $165K | $15M | 1063 | 0.76 | -1.9% | trending, thin volume |
| INJ | 2.0 | $36K | $3M | 971 | 0.21 | 2.8% | too thin |
| LINK | 1.1 | $113K | $5M | 609 | 0.11 | 3.4% | low volume |
| ENA | 1.0 | $90K | $10M | 1098 | 0.35 | 11% | open long on 07f2 (directional position) |
| AAVE | 1.0 | $126K | $8M | 768 | 0.18 | -1.0% | spread near ETH's |
| ETH (reference) | 0.57 | $25M | $830M | 572 | 0.07 | 3.4% | running |
| HYPE | 0.15 | $204K | $608M | 739 | 0.27 | 8.7% | tighter than ETH |
| ZEC | 0.11 | $241K | $227M | 1112 | 0.15 | 11% | tighter than ETH |

NEAR's quoted spread (2.5 bps) is 4.4x ETH's (0.57 bps); the ladder earns its rung spacing, not the quoted spread, so a wider ATR and spacing matter more.
The 3.5-day replay (research/neutral_mm_replay.py) cannot separate coins: 4 to 54 fills each (NEAR at the default spacing: 36-54 fills, -0.6 to -1.2 per $1K, ETH at 0.1 ATR: 16-22 fills, +0.7 to +0.8). Not significant either way.

**Review rule:** at 200 fills each (ladder.json `verdict_fills: 200`, was 100), the net edge per $1K traded after fees must reach t>=2; otherwise the test ends. The daemon's own verdict still stops a ladder whose round trips net of fees are negative at that point (not in a trend regime).
**Restarts:** each daemon restart interrupts the ladders; the log shows 75 ETH resumes since 2026-11-15, so batch code changes.
