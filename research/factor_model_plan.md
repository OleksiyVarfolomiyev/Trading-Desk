# Factor risk model for the desk: scope (plan only)

Goal: a Barra/Axioma-style cross-sectional risk model so the desk sees ex-ante risk by factor, specific risk, and what each trade does to both. No trading code is built from this; every factor must pass the desk's significance rule before it is shown.

## What exists (checked 2026-11-18)

| Piece | What it does | Limit for this plan |
|---|---|---|
| `research/factor_model.py` | Time-series regressions with Newey-West t, `fama_macbeth`, `sort_spread`, half-split helpers | Reusable building blocks (HAC, FM, split) |
| `desk/book_factors.py` | Regresses each book's actual daily returns on BTC, alt-vs-BTC, BTC momentum; stocks on SPY, QQQ-SPY, IWM-SPY, SMH-SPY, with split-sample alpha | Time-series, few hand-picked factors, no asset-level exposures, no specific risk |
| `research/data/factor_returns.csv`, `factor_premia*.csv` | Crypto factor series (BTC excess, momentum, vol, funding, alt-BTC, size, turnover) and FM premia per 1 sd characteristic | About 5 factors, 10 assets on average per day in FM (thin cross-section) |
| `research/data/daily_prices_usd.csv`, `daily_returns.csv`, `market_data.csv` | About 1,840 days, 35 coins; market cap and volume per coin | Universe far below 40-100 names; survivorship unknown |
| `research/data/french/` | Fama-French 5 factor, momentum, reversal, 12 industries (US daily) | Usable as stock factor returns directly |
| `desk/live_risk.py`, `desk/stress.py` | Live book betas to benchmarks, ex-ante row, scenario losses | Betas only; the model would replace/extend the ex-ante row |
| `desk/market_caps.py`, `desk/hl/*`, Yahoo via `live_risk.yahoo_stock_closes` | Caps, HL candles, stock closes | No fundamentals, no corporate actions feed |

No cross-sectional (Barra-style) model exists yet. Note: the desk's notes say the `research_factor_model` file names are only the time-series layer.

## References (methodology)

- toraniko (0xfdf), the model the tweet starts from: a characteristic factor model "in the same vein as Barra and Axioma": market, sector (GICS 1, 0/1 dummies) and style (value, momentum, size) factors; daily cross-sectional regression `estimate_factor_returns` with winsorisation and optional style residualisation; universe is top N by market cap per date. Repo: https://github.com/0xfdf/toraniko (README does not state weights or constraints; read the source before copying). Paleologo's pointer to the implementation: https://x.com/__paleologo/status/1847621448724041810
- Paleologo, *Advanced Portfolio Management* and his Barra/Axioma handbook-style material: the standard recipe is: exposures standardised (cap-weighted mean 0, equal-weighted sd 1), sqrt(cap)-weighted WLS with a sector-sum-zero constraint, EWMA factor covariance with Newey-West and eigenvalue adjustment, specific risk from residuals with Bayesian shrinkage to a size bucket, and a volatility regime adjustment. I could not fetch the handbooks; these steps are from general knowledge of those methodologies and need checking against them before building.

## Two models, not one

Crypto and stocks share almost nothing in data or factors, so build two models and combine them only at the covariance level (one extra "crypto" block, then the desk-wide matrix).

### A. Crypto perps model (HL, 24/7 daily)

Universe: HL perps with at least 90 days of history and liquidity (30d volume above a floor), roughly 60-120 names. Today's file covers 35; extend by backfilling HL candles (`desk/hl`) for the rest, including delisted coins to avoid survivorship.

Factors (all hypotheses until tested):
- Market (BTC) as the intercept-like factor, or the cap-weighted universe return.
- Style exposures, standardised per day: size (log cap), momentum (30d and 90d skipping last 3d), short-term reversal (5d), volatility (30d realised), liquidity (log dollar volume), funding (trailing 7d mean), beta to BTC (60d), and turnover/volume trend.
- "Sector" blocks: L1, L2, DeFi, AI, meme, exchange token. Needs a hand-kept mapping file (`research/data/asset_groups.csv` already exists; check coverage).

### B. Stocks model (IB book plus HIP-3 stocks)

The stock book is small, so a full Russell model is overkill and the data is missing (no fundamentals). Stage it:
1. Start with the ETF-factor version (SPY, QQQ-SPY, IWM-SPY, SMH-SPY, plus Fama-French momentum and 12 industries from `research/data/french`), estimated by time-series regression per holding with shrinkage. This is what `book_factors` does for the book, but per holding.
2. Only if the stock book grows past roughly 30 names: add a cross-sectional model with GICS sector, size, momentum, value, which needs a fundamentals vendor (flag as a decision, cost).

## Estimation method

1. Returns: daily, winsorised at 1%/99% by day; crypto uses close-to-close UTC.
2. Exposures: z-scored each day with cap-weighted mean and equal-weight sd; missing values filled with 0 after z-scoring; lag exposures one day so none uses the same day's return.
3. Factor returns: daily cross-sectional WLS, weight = sqrt(market cap), sector block constrained to sum to zero (cap-weighted) so market and sectors are identified. Keep residuals as specific returns.
4. Factor covariance: EWMA (half-life about 90 days for variances, longer for correlations), Newey-West lag adjustment, eigenvalue shrinkage (Ledoit-Wolf as the simple baseline), volatility regime scaling from cross-sectional dispersion.
5. Specific risk: EWMA of squared residuals (half-life about 60 days), shrunk toward the cross-sectional median by size bucket; floor to avoid near-zero risk for new coins.
6. Portfolio risk: `sigma^2 = x' F x + sum(w_i^2 s_i^2)`, with `x = B'w`. Output: total vol, share of variance by factor and specific, marginal contribution per position, and the change from a proposed trade.
7. Short history: the 35-coin file gives roughly 5 years but fewer than 15 names in some early periods. Estimate factor returns only on days with at least 20 names, and report the effective cross-section.

## Validation (desk rules: t >= 2 in both halves, or dropped)

- Each style factor: mean factor return with Newey-West t, split into halves; a factor is kept only if its premium or its variance-explained contribution is significant in both halves. Failed factors are removed, never shown as tendencies.
- Model quality: out-of-sample R-squared of cross-sectional returns (factor fit from day t-1 exposures); share of variance explained versus a market-only model, tested with a paired t on daily squared errors.
- Risk forecast: for each book and for a set of random long/short portfolios, compare predicted ex-ante vol (rolling) to realised vol over the next 20 days; bias statistic (z-score of returns divided by predicted vol) should have standard deviation near 1 (target 0.8 to 1.2) in both halves.
- Compare against what the desk already has (`live_risk` ex-ante row, `book_factors` R-squared); adopt only if it forecasts better out of sample.
- Test the actual books' history: realised book vol versus the model's predicted vol at each past day, using the daily holdings in `hl_book_exposure_weekly.csv` (weekly, so interpolate or restrict to weekly checks).

## Plugging in

- Python: `desk/factor_risk.py` (model, pure functions) and `research/factor_risk_study.py` (build and validation, writes `research/data/factor_risk_*.csv`). Daily refresh by the existing data job, not by the trading daemon.
- Dashboard: one tile, attention-first: ex-ante vol and the top factor/position contributions, with an alert when a factor's share of variance jumps or a book's predicted vol breaches its realised range. Mirror in `desk/app_static/app.js` and `web/live.js` per the "improvements across the desk" rule; simple on the first screen, details on hover.
- Daemon: read-only use first (fit-entry check: "this trade adds X% to the market factor"), extending `desk/stress.py fit_entry`. No auto-trading or sizing from the model until it passes the forecast test for a full out-of-sample quarter.
- Notebooks: a minimal-code cell in `portfolio_analytics.ipynb` showing the factor decomposition.

## Phases

1. Data: backfill HL candles/cap/volume to 60-120 names including delisted; sector map; check coverage. (3-4 days)
2. Crypto cross-section: exposures, WLS, factor returns, significance table; drop failed factors. (3-4 days)
3. Covariance and specific risk; risk forecast tests on random portfolios and the books. (3-4 days)
4. Dashboard tile and entry-impact check in the daemon (read-only). (2-3 days)
5. Stocks ETF-factor layer per holding, then desk-wide covariance. (2-3 days)
6. Optional: equity cross-sectional model with fundamentals, only if the stock book grows or a data vendor is approved.

## Decisions needed from the user

- Approve backfilling more HL history (API calls; the relay has been rate limited, so batch slowly).
- Fundamentals vendor for stocks: yes or no (the model stays ETF-factor based without one).
- Whether HIP-3 equity perps count as stocks or crypto for factor purposes.

## Risks

- Crypto cross-sections are small and fat-tailed; WLS with 20-100 names gives noisy factor returns, so expect few factors to pass t >= 2.
- Specific risk for new listings has no history; shrinkage is essential.
- A model that explains risk well is not alpha; do not turn factor premia into signals without the desk's own tests.

## Phase 1 result (2026-11-18): built, validated, not adopted

`desk/factor_risk.py` and `research/factor_risk_study.py`, on the 104 cached HL daily candles (638 days, about 84 names a day). The candles carry no volume and cap history exists for 35 coins only, so size and the regression weights use today's cap (static, not point-in-time); there is no sector map, so no sector blocks.

- **Factors (t >= 2 in both halves):** none kept. Market, momentum, volatility, beta and size are indistinguishable from zero; reversal is negative (t -2.1 first half, -0.8 second) and fails the second half. Risk factors need no premium to be useful, so the real test is the forecast.
- **Risk forecast (60 random long/short and 30 long-only portfolios, 20-day horizon, QLIKE vs the portfolio's trailing 60-day vol, which is what the live ex-ante row is):** the full model's bias statistic is 0.87/0.97 (long/short) and 0.93/0.97 (long only) in the two halves, against 0.96/1.08 and 0.93/1.14 for trailing vol. Its QLIKE gain is not significant in both halves: long/short -0.02 (t -1.3) then +0.07 (t 2.0); long only +0.11 (t 2.9) then +0.14 (t 1.3). The market-only model is worse for long-only portfolios.
- **Verdict:** a modest, unestablished edge over trailing vol. No dashboard tile, no daemon use. Revisit after the history backfill and point-in-time caps.

## Results, longer data (2026-11-18)

Backfill: `research/backfill_hl_history.py` pulled daily candles with volume for all 235 main-DEX perps (delisted included) back to 2023-11, then the HIP-3 equity perps (stocks, kept out of the crypto model) and hourly funding for the 40 most liquid perps; slow and resumable. The study now uses that panel: 526 estimation days, about 178 names a day (before: 638 days, about 84 names, only 35 with cap history).

Size and the regression weights are the trailing 30-day median dollar volume, known the day before (point-in-time liquidity proxy; point-in-time market cap is not available for most coins, CoinGecko history covers about a year).

- **Factors:** none passes t >= 2 with the same sign in both halves. Volatility is negative in both halves (t -1.1, -1.7; -1.9 overall), Beta turns positive in the second half (t 2.1), Reversal and Size are zero. Nothing to show.
- **Risk forecast** (60 long/short and 30 long-only random portfolios, 20-day horizon, vs trailing 60-day vol): the QLIKE gain is positive in both halves for every case but not significant in both. Full model: long/short +0.042 (t 1.9) and +0.047 (t 1.7); long only +0.045 (t 1.3) and +0.164 (t 1.1). The market-only model does as well (long/short +0.033, t 1.8, and +0.039, t 3.0). Bias stays near 1 (full model 0.97 and 0.96 long/short, 0.84 and 0.95 long only; trailing vol 1.05 and 1.04, 0.92 and 1.16).
- **Verdict:** the style factors add nothing beyond a market factor; the forecast edge over trailing vol is consistent in sign but not established. Model stays off the dashboard and daemon. Next: sectors for the 235 coins (the one missing block) and the stock model on the HIP-3 history.

## Sectors, stock model and equity perps (2026-11-18)

**Crypto sector blocks.** `desk/crypto_sectors.csv` maps the 233 panel coins to 9 groups (L1 49, DeFi 48, Memes 48, AI and data 23, L2 and scaling 22, Gaming and NFT 17, Other 15, Store of value and payments 11); it is a hand-made first pass for review, not a vendor taxonomy. `desk/factor_risk.estimate(..., sectors=)` adds a 0/1 column per sector (at least 4 names that day) with the cap-weighted sum of sector slopes forced to zero, so they sit beside the market column. No sector premium is kept: Gaming and NFT is negative over the whole sample (t -2.8) but only t -1.4 in the first half; the rest are |t| < 2 in at least one half.
Risk forecast (same random portfolios and 20-day horizon, QLIKE gain over trailing 60-day vol, t in the first and second half):
- Long/short: Full model +0.042 (1.9) / +0.048 (1.8); Full + sectors +0.038 (1.4) / +0.046 (1.5); Market only +0.033 (1.8) / +0.041 (3.0); **Market + sectors +0.043 (2.3) / +0.050 (4.0)**.
- Long only: Full +0.045 (1.3) / +0.164 (1.1); Full + sectors +0.034 (1.0) / +0.164 (1.1); Market only +0.076 (2.0) / +0.165 (1.3); Market + sectors +0.050 (1.7) / +0.182 (1.3).
- Reading: sector blocks help the lean model and hurt the full one (the style columns add noise). Market + sectors is the only variant with t >= 2 in both halves, for long/short portfolios; long-only misses in both halves. It was not tested directly against Market only, and the sector map was chosen before looking, but the whole thing is still one sample of 526 days. Verdict: promising, not yet adopted; no tile.

**Stock model** (`desk/stock_risk.py`, `research/stock_risk_study.py`): each stock's exposures to the Fama-French factors (market, size, value, profitability, investment, momentum) by trailing 250-day regression, EWMA factor covariance, specific variance from the residuals. 63 listed stocks from the Yahoo cache, 442 days (2024-12-24 to 2026-09-30; the French files stop there).
- Factor premia: none kept (|t| < 2 everywhere; RMW is -1.5).
- Risk forecast against trailing 60-day vol, QLIKE gain (t first / second half): FF6 long/short -0.161 (-1.8) / -0.183 (-3.0); long only -0.043 (-0.7) / -0.095 (-3.0). Market only and FF3 are the same within noise. The factor models forecast too much risk (bias 0.83 to 0.94 against 1.03 for trailing vol) and lose significantly in the second half.
- Verdict: **fails**. For a 63-stock sample with a short window, the free factors add nothing over each portfolio's own trailing vol. Not adopted; no tile. Things not tried (to avoid fitting the sample): shorter covariance half-lives, shrunk exposures, industry factors.

**Equity perps cross-section** (the crypto model's regression on HIP-3 perps; Mom, Vol, Beta, Size, equal-weighted perp basket as the market): the backfill was still running (77 perps with at least 120 days, all on the xyz dex so far), 317 days, about 47 names a day. None kept: Momentum t 2.2 in the first half but -1.2 in the second; the rest |t| < 2 in both. With under a year of history this cannot settle anything; rerun when the panel has more names and time.
