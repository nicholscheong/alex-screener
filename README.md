# Alex — US Equity Screener

A locally-run screening dashboard for US equities. It covers most of the metrics you would
find in a broker screener (price/momentum, valuation, financial quality, technicals), refreshes
quotes on a timer, and can rank the universe by chart-pattern similarity to an image you upload.

- **Universe:** all S&P 500 constituents (503 names).
- **Data source:** Yahoo Finance (free public endpoints; intraday quotes may be delayed ~15 min).
- **Dependencies:** Python 3 only. No third-party packages.

## Running it

Double-click **`Start Alex.bat`**. Your browser opens `http://127.0.0.1:8787` automatically.
Close the console window to stop the server.

Manual start: open a terminal in this folder and run `python server.py`.
(`PORT` / `HOST` are read from env vars. Set `ALEX_TOKEN="a-secret"` to lock it
down — the first visit must then be `<url>/?key=<token>`, which drops a cookie.)

**Cloud deployment:** see [`DEPLOY.md`](DEPLOY.md) — `Dockerfile`, `fly.toml` and
`render.yaml` are included; the repo is already `git init`-ed.

## Layout

The interface is a single full-width table — no sidebar. Everything is driven from the header
and the **Filters** bar directly beneath it.

### Adding metrics (columns)

The default table shows only Ticker / Name / Price / Chg %. In the header, type a keyword into
**"+ Add a metric"** (`P/E`, `RSI`, `ROE`, `ATR`, `dividend yield`, `moving average`, `Bollinger`…).
Click a match to add it as a column; click again to remove it. The dropdown also has
**+ Market / Momentum · Valuation · Financials · Technicals** to add a whole group at once.

**Period-tunable technicals.** RSI, MA, BIAS, ATR % and KDJ-J each take a period. Search the
name and a row with a **period box** appears — type any number (2–400) and hit *+ Add* or Enter.
Add as many as you want; each is its own column (`RSI(9)`, `RSI(21)`, `MA30` …) that you can
sort, range-filter and export. Already-added ones show as removable chips on the row. Shortcut:
type `rsi 9` in the search box and press Enter.

**MACD condition (DIF / DEA).** Search `MACD` (or `DIF`, `DEA`) and a row appears with a
**relationship dropdown**: `DIF > DEA`, `DIF < DEA`, `DIF crosses above DEA (golden cross)`,
`DIF crosses below DEA (death cross)`. Pick one, hit *+ Add*, and it becomes a screening
condition shown as a chip in the Filters bar (standard 12/26/9). Add more than one to stack them.
`MACD hist` and `DIF` are also available as plain columns if you just want the numbers.

BOLL %b is a fixed column at its standard setting.

### Range filters

Every metric column gets a **min / max** pair in the Filters bar. Fill either box to screen by range:

- Percentage metrics take plain numbers — `5` means 5% (turnover, range, chg %, ROE, margins,
  dividend yield, BIAS, ATR %, …).
- Market cap / dollar volume / volume accept `10B`, `500M`, `1.5T`, `20K`.
- Fill just the min, just the max, or both. Blank = no bound.
- The table shows only names that satisfy **every** active condition. A name with a missing
  value for a filtered metric is excluded by that filter.

### Sector and signal filters

To the left of the Filters bar:

- **Sector** — multi-select over the 11 GICS sectors.
- **Signals** — MA bull alignment, crossed above MA20 / MA50, RSI overbought / oversold,
  52-week high / low, volume surge (RVOL > 2), price above MA200. (MACD DIF/DEA conditions
  have their own picker — search `MACD`.)

Checking any of these adds it as a filter.

### Pattern Search (upload an image or draw)

Header button **"◧ Pattern Search"** opens a dialog with two inputs:

- **Upload image** — a screenshot from any charting tool, a line chart, or a phone screenshot.
  The app finds the darkest / most saturated line in the picture and extracts it into a curve
  (the dialog draws the detected curve back for you to confirm). Works on dark or light
  backgrounds and colored lines; gridlines and axis labels are ignored.
- **Draw** — sketch the shape you want on the pad (cup-and-handle, double bottom, rounded
  bottom, breakout, …) by holding and dragging left to right.

Pick a **Lookback** (20 / 40 / 60 / 90 / 120 sessions, default 60) and click **Run match**.
A **Pattern sim** column (0–100) is added and the full universe is sorted by it, descending.
Matching is **shape only** — both curves are normalized (z-scored) and compared with
band-constrained dynamic time warping, so absolute return and scale do not matter.

- Combines with the range filters (e.g. "looks like this chart **and** P/E < 20").
- The thumbnail chip in the Filters bar has an × to clear it.
- Scoring uses daily history, so it needs the technicals load to finish (status bar shows
  `Technicals X%`). The query is saved in the browser and re-runs automatically next session.
- Matching runs entirely in the browser. Uploaded images are never sent to the server.

### Other controls

- **Sorting** — click any column header to sort by it; click again to reverse. Stacks with
  filters and Pattern Search.
- **Reset** — **Reset values** clears all filter values, sectors and signals but keeps the
  metric columns; **Clear all** returns to the default 4 columns. The status bar reads
  `X / 503  ·  N filters  ·  M pattern-scored`.
- **Ticker search** — top-right; filters rows by ticker or company name (stacks with the rest).
- **Detail panel** — click any row for a 180-session price chart, the full metric set, and every
  triggered signal (independent of which columns are shown).
- **Full chart** — click the mini chart in the detail panel to open a large candlestick chart
  with MA20 / MA60 overlays, a volume pane, selectable range (3M / 6M / 1Y / 2Y / All), and a
  TradingView-style crosshair: move the pointer to read that session's date, O/H/L/C, % change
  and volume. Esc or click outside to close (the detail panel stays open).
- **Header** — metric search, Pattern Search, ticker search, refresh interval (10s–60s / Off),
  ↻ refresh now, Export CSV (exports the currently visible columns and rows).
- All filters, selected metrics, sort order and the pattern query persist in the browser.

## Metric list

| Group | Metrics |
|---|---|
| Market / Momentum | Price, Chg %, daily range %, relative volume, turnover %, volume, dollar volume, 5/20/60-day return, YTD, 52-week change, distance off 52-week high / low, 40-day trend sparkline |
| Valuation | Market cap, P/E (TTM), forward P/E, P/B, P/S, PEG, dividend yield, EPS, payout ratio |
| Financial quality | ROE, ROA, gross margin, operating margin, net margin, revenue growth, earnings growth, debt/equity, current ratio, beta |
| Technicals | MACD histogram, DIF, BOLL %b, analyst rating, signal set |
| Technicals (period-tunable) | RSI(n), MA(n), BIAS(n), ATR(n) %, KDJ-J(n) — type any period |

## Notes and known limits

- Quotes refresh every 15 seconds (adjustable). Valuation / financial fields are cached for
  30 minutes. Technicals are computed from daily bars and recomputed against the live price.
- Technicals load in the background one name at a time — roughly 30–40 seconds on first open,
  then cached in the browser for faster subsequent loads. (The candlestick chart needs open
  prices, so the first launch after this update does one more full history refresh.)
- **Financial fields (ROE, margins, P/S, PEG, …) are fetched only for the top ~120 visible names
  by default.** As soon as you add any financial metric as a column or filter, the app backfills
  all 503 in the background (status bar shows `Financials X%`, ~1–2 minutes, then cached 30 min).
  So the match count for a fresh ROE filter grows as data arrives — this is expected.
- Free-source limits: intraday prices are delayed; a few recently-listed names have too little
  history and show "–" for some metrics (and are excluded by the corresponding filters).
- US equities / S&P 500 only. Broader coverage (full US market, other exchanges) is future work.
- Financial figures come from Yahoo and may differ slightly from other providers in definition.
- Pattern Search is an **approximate curve-shape match**, not chart-pattern recognition. Image
  extraction works best on a clean single main line; with several overlapping lines, heavy
  shading, or very dense candles the extracted curve can be off — the dialog preview is there
  to check it before you run the match.
