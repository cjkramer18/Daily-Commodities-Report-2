[README.md](https://github.com/user-attachments/files/32210731/README.md)
# Daily Commodities Report

Sends you a tiered HTML email every weekday morning: headline movers (WTI, Brent,
Gold, Copper) in full, secondary commodities in a compact table, and an inventory
watch section when EIA releases weekly stockpile data.

## 1. Get free API keys

- **EIA API key** (energy prices + inventory data): https://www.eia.gov/opendata/register.php — instant, free, no approval wait.
- **Alpha Vantage API key** (metals + ag): https://www.alphavantage.co/support/#api-key — instant, free tier is 25 requests/day, which is enough for this script (it makes ~9 calls per run).

## 2. Get a free Metals.Dev key (for Gold/Silver/Platinum/Palladium)

Sign up free at https://metals.dev/ — Alpha Vantage does not offer precious
metals data on any tier despite XAU/XAG/XPT/XPD looking like currency codes,
so this is a separate provider just for those four. Metals.Dev's free plan
genuinely requires no credit card and includes about 100 requests/month.
The script makes just 1 call per run (a single timeseries request covers all
four metals plus 14 days of history), so weekday runs use roughly 22-25
calls/month — comfortably within the free tier.

## 3. Get SMTP credentials

Any of these work:
- **Gmail**: use an [App Password](https://myaccount.google.com/apppasswords) (not your regular password). Host: `smtp.gmail.com`, Port: `587`.
- **Resend** (https://resend.com) or **SendGrid** (https://sendgrid.com): both have free tiers and give you SMTP credentials in their dashboard — often more reliable for automated sending than a personal Gmail account.

## 4. Run it locally to test

```bash
pip install -r requirements.txt

export EIA_API_KEY="your_key"
export ALPHAVANTAGE_API_KEY="your_key"
export METALS_API_KEY="your_key"
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="you@gmail.com"
export SMTP_PASSWORD="your_app_password"
export REPORT_TO_EMAIL="you@gmail.com"
export REPORT_FROM_EMAIL="you@gmail.com"

python commodities_report.py
```

Check your inbox. If something's missing, check the console — the script logs a
warning per commodity that fails to fetch instead of crashing the whole report.

## 5. Automate it with GitHub Actions (free, no server needed)

1. Push this folder to a new **private** GitHub repo.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add each
   of the environment variables above (including `METALS_API_KEY`) as a secret
   (same names).
3. The workflow in `.github/workflows/daily-report.yml` is set to run weekdays at
   7am ET. Adjust the `cron` line if you want a different time — GitHub Actions
   cron schedules are always in UTC.
4. To test it without waiting for the schedule: go to the **Actions** tab →
   "Daily Commodities Report" → **Run workflow**.

## 6. Enable the interactive dashboard (GitHub Pages)

The workflow now also generates `docs/data/price_history.json`, a persisted
price-history archive, and `docs/index.html`, a dashboard page with selectable
timeframes (1M/3M/6M/YTD/1Y/5Y) and moving averages (200/50/20-day SMA, 10-day
EMA). To publish it:

1. **Important: GitHub Pages requires a public repo on the free plan.**
   Private-repo Pages hosting needs GitHub Pro/Team/Enterprise. Your API keys
   and email credentials stay safe either way — GitHub Actions secrets are
   encrypted and never exposed in the repo or logs regardless of visibility —
   but the *code* and any data files become publicly viewable if you switch.
   If that's fine: **Settings → General → Danger Zone → Change visibility →
   Public**. If not, either keep the dashboard unpublished (the email report
   still works fully without it) or upgrade your GitHub plan.
2. **Settings → Pages → Source: Deploy from a branch → Branch: `main`,
   folder: `/docs` → Save.**
3. GitHub will publish it at `https://<your-username>.github.io/<repo-name>/`
   within a minute or two. The daily email links to this URL automatically at
   the bottom of the report.
4. The first run **bootstraps** roughly a year of Gold/Silver/Platinum/Palladium
   history (about 13 API calls, one-time). WTI/Brent/Natural Gas get full
   history immediately since EIA has no historical-range limit. The 200-day
   moving average for the metals won't have enough data to display until
   about 7 months of daily runs have accumulated — the dashboard shows a note
   explaining this until then.

## Coverage notes

- Soybeans and Steel don't have a clean free-tier API source, so they're left as
  "n/a" placeholders in the script. If you want them, Nasdaq Data Link or
  Trading Economics both have paid endpoints that would slot into
  `fetch_commodity()` the same way the others do.
- Alpha Vantage's free tier is rate-limited (25 calls/day). This script uses
  about 9 per run, so running it more than twice a day will hit the limit.
- Copper doesn't get dashboard charting for now — Alpha Vantage only offers
  monthly resolution for it, which doesn't fit the daily-history dashboard.

---

## Sites worth bookmarking alongside the email

For quick manual checks, deeper charts, or context when something moves a lot:

- **Trading Economics — Commodities** (https://tradingeconomics.com/commodities) — clean live quotes across all categories, plus forecasts and historical charts. Good single-page overview.
- **Yahoo Finance — Commodities** (https://finance.yahoo.com/markets/commodities/) — real-time-ish prices, % change, volume, and daily charts; easiest for a fast gut-check.
- **Barchart — Major Commodities** (https://www.barchart.com/futures/major-commodities) — deeper futures data: highs/lows, most active contracts, Commitment of Traders reports if you get into positioning data.
- **Investing.com — Commodities** (https://www.investing.com/commodities) — broad coverage with news and technical analysis attached to each commodity page.
- **EIA — U.S. Energy Information Administration** (https://www.eia.gov) — the official source for energy data and inventory reports; worth checking directly on Wednesdays when weekly petroleum data drops.
- **IndexMundi — Commodity Prices** (https://www.indexmundi.com/commodities/) — best for long historical context (data back to 1980) if you want to see whether today's move is actually notable.
- **Bloomberg — Commodities** (https://www.bloomberg.com/markets/commodities) — best for the "why" behind moves; more editorial/news context than raw data.
