# Suggestions review

Generated from desk/suggestions.py (log: data/suggestions.jsonl, append only). **344 suggestions logged**, **23 scoreable on price**, 21 scored so far (followed: 18). Verdict: **too few suggestions scored to say anything**.

## What is covered

The transcripts cover **2026-11-02 19:52 UTC to 2026-11-20 20:20 UTC** (six session files in three Claude project folders: 2 Crypto-Portfolio-Analytics sessions,
the old Dropbox trading-desk session, and 3 sessions in the current folder). The repo started on **2026-10-16**, so **2026-10-16 to 2026-11-02 is not covered by any transcript**:
whatever I advised in those two weeks is not in this log. Other sources used for the record, only where a recommendation of mine is clear: the memory files (dated by their
modified time; 5 rows), research notes dated by git (4 rows) and git history (only to check whether a suggestion was followed by a matching commit, never to invent one).
data/feedback.jsonl holds your clicks, not my advice, so it is not a source.

A suggestion here is any advice I gave in an exchange (a view on what to do: how to size, open/close/hold/avoid, which tool or rule, what to test next, whether a design or number is right, why something should not be done), asked for or unprompted. Extracted from the assistant messages by sentence (first-person views, 'I'd', 'I recommend', 'my read', 'not worth', 'better to', 'don't ...'), then classified by rule and reviewed on the price calls; recall is not perfect.

## Counts

| Kind | n | Asked | Unprompted |
|---|---|---|---|
| info | 146 | 57 | 70 |
| design | 43 | 20 | 23 |
| process | 41 | 14 | 27 |
| hold | 29 | 14 | 9 |
| test | 24 | 13 | 11 |
| size | 19 | 6 | 12 |
| rule | 19 | 7 | 12 |
| trade | 12 | 10 | 0 |
| avoid | 6 | 4 | 0 |
| reduce | 5 | 3 | 0 |

By trigger: asked 148, unprompted 164, unknown 32 (rows logged before the field). By fit: strategy 65, additional 176, process 100, unknown 3. By month: 2026-11 344.

## Hit rate (price moved the suggested way)

| Horizon | n | Hit rate | Mean move | z vs 50% |
|---|---|---|---|---|
| so far (interim) | 21 | 62% | 3.9% | 1.1 |
| 1d | 17 | 47% | n/a | -0.2 |
| 3d | 16 | 69% | 1.3% | 1.5 |
| 7d | 5 | 40% | -1.1% | -0.4 |

## Suggested vs the user's own entries

| Group | n | Hit 1d (n) | Hit 3d (n) | Hit 7d (n) | Hit so far (n) | Mean so far |
|---|---|---|---|---|---|---|
| Suggested trades, followed | 9 | 50% (8) | 75% (8) | 100% (1) | 100% (9) | 14.1% |
| Suggested trades, not followed | 2 | 0% (2) | 0% (2) | 0% (2) | 0% (2) | -12.9% |
| All directional calls, followed | 18 | 50% (14) | 77% (13) | 67% (3) | 72% (18) | 6.3% |
| All directional calls, not followed | 3 | 33% (3) | 33% (3) | 0% (2) | 0% (3) | -10.3% |
| Your own entries, never suggested | 30 | 56% (27) | 80% (25) | 72% (18) | 67% (30) | 7% |

All of these sit in one regime (a falling crypto market with the desk net short), so the calls and the entries are not independent: a high hit rate for shorts here says the regime, not skill. Directional calls measured: trade, avoid, reduce. An avoid is followed when nothing was opened.

## By fit (strategy vs additional)

| Fit | n scored | Hit 3d (n) | Hit so far (n) |
|---|---|---|---|
| strategy | 15 | 64% (11) | 67% (15) |
| additional | 6 | 80% (5) | 50% (6) |

## Followed trades by the label the user chose

| Label | n | Hit 3d (n) | Hit so far (n) |
|---|---|---|---|
| directional | 7 | 67% (6) | 100% (7) |
| unknown | 2 | 100% (2) | 100% (2) |

## The rest: was it done?

For suggestions that are not price calls the checkable follow-up is a commit in the next three days that shares content words with the advice (it was built or changed). It shows it was acted on, not that it was right; whether the outcome was what I said is checkable only for the price calls above.

| Kind | n | With a matching commit |
|---|---|---|
| design | 43 | 16 (37%) |
| process | 41 | 11 (27%) |
| hold | 29 | 5 (17%) |
| test | 24 | 8 (33%) |
| size | 19 | 2 (11%) |
| rule | 19 | 5 (26%) |
| trade | 1 | 0 (0%) |
| avoid | 1 | 0 (0%) |

## The scored calls

| At | Kind | Side | Coin | Fit | Conf | Followed | Label | so far | 1d | 3d | 7d |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-08T04:36 | trade | short | BTC | additional | medium | yes |  | +14.5% | +1.8% | +2.5% | +14.9% |
| 2026-11-08T04:36 | avoid | long | BTC | strategy | medium | yes |  | +14.5% | +1.8% | +2.5% | +14.9% |
| 2026-11-10T14:36 | reduce | short | AVAX | additional | medium | yes |  | -14.1% | +4.7% | +2.0% | -13.5% |
| 2026-11-12T21:37 | trade | long | HYPE | additional | medium | no |  | -15.1% | -0.4% | -5.1% | -6.8% |
| 2026-11-13T03:37 | trade | short | VVV | strategy | medium | no |  | -10.8% | -3.7% | -11.9% | -14.9% |
| 2026-11-14T01:29 | reduce | short | ICP | strategy | medium | yes |  | -17.1% | -11.9% | +2.2% |  |
| 2026-11-14T01:44 | avoid | long | BERA | additional | high | yes |  | +38.4% | +26.0% | +30.8% |  |
| 2026-11-14T01:44 | reduce | short | BERA | strategy | low | yes |  | -38.4% | -26.0% | -30.8% |  |
| 2026-11-15T01:21 | avoid | short | GRAM | strategy | medium | no |  | -4.9% | +3.4% | +5.4% |  |
| 2026-11-15T01:54 | trade | short | ETH | additional | low | yes |  | +11.1% | -0.5% | +0.7% |  |
| 2026-11-16T15:39 | trade | short | PUMP | strategy | medium | yes | directional | +16.2% | +5.7% | +7.4% |  |
| 2026-11-16T15:39 | trade | short | ZRO | strategy | medium | yes | directional | +19.9% | +4.2% | +6.0% |  |
| 2026-11-16T15:39 | trade | short | ARB | strategy | medium | yes | directional | +15.9% | +2.3% | +7.9% |  |
| 2026-11-16T15:39 | trade | short | UNI | strategy | medium | yes | directional | +15.3% | -2.3% | +4.1% |  |
| 2026-11-16T15:49 | trade | short | TRUMP | strategy | medium | yes | directional | +11.1% | -1.4% | -1.6% |  |
| 2026-11-16T15:49 | trade | short | MELANIA | strategy | medium | yes | directional | +6.0% | -0.6% | -1.4% |  |
| 2026-11-17T21:01 | avoid | short | BNB | strategy | low | yes |  | -9.1% | -2.4% |  |  |
| 2026-11-19T22:41 | reduce | long | ENA | strategy | low | yes |  | +7.8% |  |  |  |
| 2026-11-19T22:41 | reduce | long | XMR | strategy | low | yes |  | +9.1% |  |  |  |
| 2026-11-20T01:49 | trade | short | xyz:SHAZ | strategy | medium | yes | directional | +17.2% |  |  |  |
| 2026-11-20T15:43 | avoid | long | mkts:BVIV | additional | low | yes |  | -5.2% |  |  |  |

## Voided (logged in error, left out of every count)

- 2026-11-05: The most you could short is 6,761 HEMI, about **$31**, with $3.37 of risk. (a statement of what fits, not a recommendation)

## Classification

- **fit = strategy** when the call applies a rule or thesis you set (the caps, the sizing rule, ladders take-profit-only, thesis review first, the short-crypto
  default). The four-shorts hold, the XMR/ENA reduces, the SHAZ call and the CVX size are strategy-fit. The SHAZ call is the ambiguous one: *which* of the four to open
  and at what size is my pick, but re-shorting the four was your decision and the size is the rule's, so it is classed strategy.
- **fit = additional** when it is my own idea beyond your strategy: the BVIV view (a new instrument, a carry-trade framing), the Fear & Greed signal test, the ETH ladder
  'let it run to 200 fills', the NEAR 'leave it', the meme-spike detector.
- **fit = process** for ops, UI and engineering advice ('I'd recommend yes' to a dashboard change): logged for the record, never scored.
- **kind**: trade/avoid/reduce are scored on price; hold and size are capacity or sizing views and cannot be scored on price (a hold that waits for room is not a price call);
  info is analysis or process.
- **confidence**: 'I'd recommend yes' = high, 'I'd' / 'My view' / 'My suggestion' = medium, 'probably' and 'if you want' and options offered without a recommendation = low.
- **followed**: a trade, an opening fill in that coin and direction within 3 days; a reduce, a closing fill on that side within 3 days; an avoid, no opening fill. ENA and
  XMR count as followed for the reduce because both longs were closed within a day (a full close, for a different reason: the longs were accidental), which is more than the
  trim that was suggested.
- **price moves** are measured from the hour of the suggestion in the suggested direction at 1, 3 and 7 days; 'so far' is the move to the latest price and is interim.

## The honest read

- **Coverage:** 344 suggestions in the 18 days the transcripts cover; nothing for 2026-10-16 to 2026-11-02, and the extraction is by sentence, so some advice buried in long lists or tables is missed.
- **Price calls:** 21 are scored; the 1-day, 3-day and 7-day hit rates are in the table with their n. Below 20 scored the desk's rule says too few to claim anything; the longer horizons have fewer calls than the 1-day.
- **Strategy-fit vs additional:** 15 strategy-fit and 6 additional calls are scored. Compare their hit rates only with their n beside them.
- **Followed vs not followed vs your own entries:** see the table; the groups share one market regime, so differences between them are not evidence of skill.
- **Most of what I advise is not a price call** (design, process, rules, tests): for those the record is whether it was done, not whether it was right.

