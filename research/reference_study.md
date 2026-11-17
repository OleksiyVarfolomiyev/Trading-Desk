# Reference products: what to learn from them

The desk studies IB, Robinhood, DefiLlama and Hyperliquid because their choices are shaped by feedback from millions
of users. `desk/reference_watch.py` checks them daily (the daemon raises changes as one dashboard alert); this file holds
what was learned and what the desk took from it. Last reviewed 2026-11-17.

## DefiLlama: data the desk can pull (verified by calling each endpoint on 2026-11-17)
Free, no key: protocols and TVL (`api.llama.fi/protocols`, 9,059 protocols), chains (`/v2/chains`), stablecoins
(`stablecoins.llama.fi`, 452 assets), yields (`yields.llama.fi/pools`, 15,298 pools), DEX volumes (`/overview/dexs`),
fees and revenue (`/overview/fees`), perp open interest (`/overview/open-interest`), hacks (`/hacks`, 1,359), prices
(`coins.llama.fi`). Paid (HTTP 402): derivatives volume, bridges, raises, token emissions/unlocks.
Candidate signals to test (hypotheses, t >= 2 in both halves before any use): stablecoin supply growth as crypto risk
appetite; a protocol's fees against its token's move (fade tokens whose price ran ahead of fees); open-interest build-up
by venue; a hack in a protocol as a short trigger for its token.

## Robinhood (Legend, Cortex; sources below)
Shipped: Cortex builds indicators and market scanners from a sentence and watches for matches in near real time; ladders
for equities and crypto; short selling (among the most requested); social trading by invitation; live earnings
experience. Complaints in reviews: outages at busy moments, support, account restrictions; the interface itself draws
few complaints ("make trading simple" still holds).
Taken for the desk: plain-language scanners (the screener could take a sentence); the dashboard stays simple first,
depth on demand; reliability at busy moments matters more than features.

## Interactive Brokers (TWS, IBKR Desktop, PortfolioAnalyst)
PortfolioAnalyst: free unified reporting and analytics (the model for the desk's Report); Ask IBKR: questions about the
portfolio in plain English; tax-lot choice when closing; investment themes for discovery; windows that dock, link and
hide (the desk's tiles); hide balances with a tap (the desk's privacy click).

## Hyperliquid
The docs publish an index of every page (`llms.txt`, 145 entries): a new page there is the first sign of a new product
(HIP-3 DEXes, outcome markets). The Python SDK's releases show API changes before they break the desk's parsers.
Ten HIP-3 DEXes are live (xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io; desk/source_watch.py follows them).

## Limits
Robinhood's newsroom renders in the browser, so the script sees few headlines there; DefiLlama's own docs page blocks
scripts (its API docs answer). Review claims come from aggregators and blogs, weaker than the companies' own notes.

Sources: [Robinhood Cortex](https://robinhood.com/us/en/support/articles/cortex/),
[Robinhood AI agents, perps, earnings](https://pulse2.com/robinhood-unveils-ai-agents-24-7-stock-trading-perpetual-futures-and-earnings-contracts/),
[Robinhood Social](https://www.financemagnates.com/forex/brokers/robinhood-launches-social-trading-platform-to-compete-with-reddit-communities/),
[2026 app review analysis](https://unstar.app/blog/stock-trading-app-reviews-robinhood-webull-fidelity-etrade-2026),
[Robinhood outage June 2026](https://gvwire.com/2026/06/12/robinhood-down-for-thousands-of-users-downdetector-shows/),
[NEWs @ IBKR Vol. 27](https://www.interactivebrokers.com/en/general/communiques/2025/2025-vol27.php),
[IBKR Advisor Portal and PortfolioAnalyst](https://fxnewsgroup.com/forex-news/retail-forex/interactive-brokers-introduces-significant-enhancements-to-its-web-based-advisor-portal/),
[Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs).
