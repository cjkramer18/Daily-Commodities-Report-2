# Daily Commodities Report

Sends you a tiered HTML email every weekday morning: headline movers (WTI, Brent,
Gold, Copper) in full, secondary commodities in a compact table, and an inventory
watch section when EIA releases weekly stockpile data.

## 1. Get free API keys

- **EIA API key** (energy prices + inventory data): https://www.eia.gov/opendata/register.php — instant, free, no approval wait.
- **Alpha Vantage API key** (metals + ag): https://www.alphavantage.co/support/#api-key — instant, free tier is 25 requests/day, which is enough for this script (it makes ~9 calls per run).

## 2. Get SMTP credentials

Any of these work:
- **Gmail**: use an [App Password](https://myaccount.google.com/apppasswords) (not your regular password). Host: `smtp.gmail.com`, Port: `587`.
- **Resend** (https://resend.com) or **SendGrid** (https://sendgrid.com): both have free tiers and give you SMTP credentials in their dashboard — often more reliable for automated sending than a personal Gmail account.

## 3. Run it locally to test

```bash
pip install -r requirements.txt

export EIA_API_KEY="your_key"
export ALPHAVANTAGE_API_KEY="your_key"
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

## 4. Automate it with GitHub Actions (free, no server needed)

1. Push this folder to a new **private** GitHub repo.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add each
   of the environment variables above as a secret (same names).
3. The workflow in `.github/workflows/daily-report.yml` is set to run weekdays at
   7am ET. Adjust the `cron` line if you want a different time — GitHub Actions
   cron schedules are always in UTC.
4. To test it without waiting for the schedule: go to the **Actions** tab →
   "Daily Commodities Report" → **Run workflow**.

## Coverage notes

- Soybeans and Steel don't have a clean free-tier API source, so they're left as
  "n/a" placeholders in the script. If you want them, Nasdaq Data Link or
  Trading Economics both have paid endpoints that would slot into
  `fetch_commodity()` the same way the others do.
- Alpha Vantage's free tier is rate-limited (25 calls/day). This script uses
  about 9 per run, so running it more than twice a day will hit the limit.

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
