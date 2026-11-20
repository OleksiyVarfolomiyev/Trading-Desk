# Screener strategies: windows, fade, timing

Panel: the 40 most liquid main-DEX perps, 2023-11-15 to 2026-11-19 (1082 days). Each entry shorts the 5 weakest (or most stretched) names for the hold, return vs BTC, less a 9 bps round trip. Non-overlapping entries; validated means mean > 0 with t >= 2 overall and in both halves.

## (a) Relative weakness windows
| signal | hold | n | mean per trade | t | t 1st half | t 2nd half | validated |
|---|---|---|---|---|---|---|---|
| weak 1d | 1d | 508 | -0.12% | -0.8 | -0.1 | -1.2 | no |
| weak 3d | 1d | 508 | -0.09% | -0.5 | -0.1 | -0.8 | no |
| weak 7d | 1d | 508 | +0.05% | 0.4 | 0.5 | -0.1 | no |
| weak 14d | 1d | 508 | +0.09% | 0.6 | 0.3 | 0.7 | no |
| weak 30d | 1d | 508 | -0.06% | -0.4 | -0.1 | -0.5 | no |
| weak 60d | 1d | 507 | -0.05% | -0.3 | 0.1 | -0.7 | no |
| weak 30d, bouncing 3d | 1d | 499 | -0.15% | -1.1 | -0.0 | -1.8 | no |
| weak 30d, bouncing 3d > 5% | 1d | 411 | -0.00% | -0.0 | 0.0 | -0.0 | no |
| weak 1d | 3d | 169 | +0.30% | 0.8 | 2.0 | -0.9 | no |
| weak 3d | 3d | 169 | +0.50% | 1.1 | 1.2 | 0.3 | no |
| weak 7d | 3d | 169 | +0.51% | 1.2 | 1.3 | 0.4 | no |
| weak 14d | 3d | 169 | +0.40% | 1.1 | 1.1 | 0.4 | no |
| weak 30d | 3d | 169 | +0.13% | 0.3 | 0.4 | -0.1 | no |
| weak 60d | 3d | 168 | +0.20% | 0.4 | 0.5 | 0.1 | no |
| weak 30d, bouncing 3d | 3d | 166 | -0.45% | -1.1 | -0.6 | -1.1 | no |
| weak 30d, bouncing 3d > 5% | 3d | 139 | -0.96% | -1.7 | -1.0 | -1.5 | no |
| weak 1d | 7d | 72 | +0.32% | 0.3 | 0.1 | 0.4 | no |
| weak 3d | 7d | 72 | +0.21% | 0.2 | -0.1 | 0.4 | no |
| weak 7d | 7d | 72 | +0.70% | 0.7 | 0.9 | 0.1 | no |
| weak 14d | 7d | 72 | +0.46% | 0.5 | 0.7 | -0.0 | no |
| weak 30d | 7d | 72 | +0.17% | 0.2 | 0.1 | 0.2 | no |
| weak 60d | 7d | 72 | +0.27% | 0.2 | 0.2 | 0.1 | no |
| weak 30d, bouncing 3d | 7d | 71 | -0.54% | -0.6 | 0.2 | -1.0 | no |
| weak 30d, bouncing 3d > 5% | 7d | 59 | +0.65% | 0.5 | 1.2 | -0.4 | no |
| weak 1d | 14d | 36 | +1.59% | 1.0 | 0.3 | 1.3 | no |
| weak 3d | 14d | 36 | +2.68% | 1.8 | 0.5 | 2.7 | no |
| weak 7d | 14d | 36 | +0.46% | 0.3 | -0.3 | 1.5 | no |
| weak 14d | 14d | 36 | +1.11% | 0.6 | -0.1 | 1.5 | no |
| weak 30d | 14d | 36 | +1.56% | 1.0 | 0.7 | 0.7 | no |
| weak 60d | 14d | 36 | +0.12% | 0.1 | 0.3 | -0.3 | no |
| weak 30d, bouncing 3d | 14d | 35 | -1.62% | -0.6 | -0.4 | -0.5 | no |
| weak 30d, bouncing 3d > 5% | 14d | 28 | -0.77% | -0.3 | 0.3 | -0.7 | no |

## (b) Fade: stretched and up vs BTC
| signal | hold | n | mean per trade | t | t 1st half | t 2nd half | validated |
|---|---|---|---|---|---|---|---|
| fade: up >5% vs BTC in 3d, stretched | 1d | 344 | -0.33% | -1.3 | -1.1 | -0.7 | no |
| fade: up >10% vs BTC in 3d, stretched | 1d | 235 | -0.05% | -0.2 | -0.1 | -0.1 | no |
| fade: up >15% vs BTC in 3d, stretched | 1d | 147 | -0.32% | -0.7 | -0.4 | -0.6 | no |
| fade: up >20% vs BTC in 3d, stretched | 1d | 90 | -0.10% | -0.2 | -0.4 | 0.1 | no |
| fade: up >5% vs BTC in 3d, stretched | 3d | 116 | -1.15% | -1.4 | -0.9 | -1.2 | no |
| fade: up >10% vs BTC in 3d, stretched | 3d | 78 | +0.61% | 0.6 | 0.9 | 0.1 | no |
| fade: up >15% vs BTC in 3d, stretched | 3d | 50 | -0.16% | -0.1 | 1.0 | -0.6 | no |
| fade: up >20% vs BTC in 3d, stretched | 3d | 28 | +1.33% | 0.7 | 1.4 | -0.1 | no |
| fade: up >5% vs BTC in 3d, stretched | 7d | 45 | -0.28% | -0.2 | 0.2 | -0.4 | no |
| fade: up >10% vs BTC in 3d, stretched | 7d | 33 | +1.53% | 0.8 | 0.3 | 0.8 | no |
| fade: up >15% vs BTC in 3d, stretched | 7d | 20 | +3.53% | 1.2 | 0.8 | 0.9 | no |
| fade: up >20% vs BTC in 3d, stretched | 7d | 12 | +7.68% | 4.1 |  |  | no |
| fade: up >5% vs BTC in 3d, stretched | 14d | 23 | -4.19% | -1.4 | -0.5 | -1.6 | no |
| fade: up >10% vs BTC in 3d, stretched | 14d | 17 | -4.12% | -1.0 | -0.1 | -1.3 | no |
| fade: up >15% vs BTC in 3d, stretched | 14d | 10 | -1.07% | -0.2 |  |  | no |
| fade: up >20% vs BTC in 3d, stretched | 14d | 6 |  |  |  |  | no |

## (c) Timing of the desk's own short entries (entry day minus placebo days of the same coin, a short's gain)
| entry | hold | n | mean per date | t | t 1st half | t 2nd half | validated |
|---|---|---|---|---|---|---|---|
| entry day vs placebo | 1d | 27 | +0.40% | 0.5 | -0.0 | 0.7 | no |
| entry day vs placebo | 3d | 10 | +3.75% | 1.0 |  |  | no |
| entry day vs placebo | 7d | 5 |  |  |  |  | no |

## Reading

- **Windows:** no window of relative weakness (1, 3, 7, 14, 30 or 60 days) validates as a short signal on the liquid forty, net of costs: the best
  is the 3-day window held 14 days (t about 1.8), and the desk's 30-day rule is near zero (t 0.2 to 0.3). "Weak over 30 days and bouncing over 3"
  is not a better entry, it is worse (negative, t down to -1.7). So the screener's ranking is labelled as the desk's record, not a validated signal.
- **Fade:** stretched and up against BTC in 3 days, shorted for 1 to 14 days, does not validate at any threshold (5 to 20%), the same finding
  as the earlier pump-and-dump tests.
- **Timing:** each day averages all available coin-level differences, and test dates are spaced at least one holding period apart.
  The comparison covers closed trades with available candles. Nearby placebo windows can overlap and share market moves, so the
  reported t-statistics are descriptive. These results do not establish an entry timing rule.
- **The attribution's Timing row:** it is built from the deviation of the day's net weight from the window's average, times the benchmark return, plus
  fills marked to the close. That is partly net exposure against the benchmark, so it is not a clean measure of entry timing. The placebo test above
  holds the coin fixed and moves the entry day.
