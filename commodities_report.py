"""
Daily Commodities Report
-------------------------
Fetches prices for a configurable list of commodities, builds a tiered
HTML email (headline movers in full, secondary commodities compact,
inventory data when available), and sends it via SMTP.

Data sources:
  - EIA API (free, official US gov't data): WTI, Brent, Natural Gas,
    plus weekly crude oil inventory data.
  - Alpha Vantage (free tier): Copper, Aluminum, Wheat, Corn.
  - Alpha Vantage FX endpoint (free tier): Gold (XAU), Silver (XAG),
    Platinum (XPT), Palladium (XPD) — precious metals are quoted as
    currency pairs against USD.

Note: Alpha Vantage's free commodities endpoint doesn't cover Soybeans
or Steel. Those are left as "N/A" with a comment showing where to plug
in a paid source (Nasdaq Data Link / Trading Economics) if you want them.

Environment variables required (see README.md for how to get each):
  EIA_API_KEY            - free, from https://www.eia.gov/opendata/register.php
  ALPHAVANTAGE_API_KEY    - free, from https://www.alphavantage.co/support/#api-key
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD  - your email provider's SMTP creds
  REPORT_TO_EMAIL         - where the report gets sent
  REPORT_FROM_EMAIL       - the "from" address (often same as SMTP_USER)
"""

import os
import smtplib
import requests
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Set up logging for GitHub Actions & local debugging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
AV_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")

# ---------------------------------------------------------------------------
# Commodity configuration
# tier 1 = always shown in full with a "why it moved" line if it's a big move
# tier 2 = compact table, only gets commentary if the move is > 2%
# ---------------------------------------------------------------------------
COMMODITIES = [
    {"name": "WTI Crude",     "tier": 1, "source": "eia",       "series_id": "PET.RWTC.D"},
    {"name": "Brent Crude",   "tier": 1, "source": "eia",       "series_id": "PET.RBRTE.D"},
    {"name": "Gold",          "tier": 1, "source": "av_fx",     "symbol": "XAU"},
    {"name": "Copper",        "tier": 1, "source": "av_commod", "function": "COPPER"},
    {"name": "Natural Gas",   "tier": 2, "source": "eia",       "series_id": "NG.RNGWHHD.D"},
    {"name": "Silver",        "tier": 2, "source": "av_fx",     "symbol": "XAG"},
    {"name": "Platinum",      "tier": 2, "source": "av_fx",     "symbol": "XPT"},
    {"name": "Palladium",     "tier": 2, "source": "av_fx",     "symbol": "XPD"},
    {"name": "Aluminum",      "tier": 2, "source": "av_commod", "function": "ALUMINUM"},
    {"name": "Steel",         "tier": 2, "source": "none"},   # no free API source; add manually if needed
    {"name": "Corn",          "tier": 2, "source": "av_commod", "function": "CORN"},
    {"name": "Wheat",         "tier": 2, "source": "av_commod", "function": "WHEAT"},
    {"name": "Soybeans",      "tier": 2, "source": "none"},   # no free API source; add manually if needed
]

BIG_MOVE_THRESHOLD_TIER1 = 1.0   # % change that triggers a "why it moved" note for tier 1
BIG_MOVE_THRESHOLD_TIER2 = 2.0   # % change that triggers commentary for tier 2

# Minimum data quality threshold: abort send if < this % of commodities have data
MIN_DATA_QUALITY = 0.5  # 50%


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def validate_environment():
    """Ensure all required environment variables are set."""
    required_vars = [
        "EIA_API_KEY",
        "ALPHAVANTAGE_API_KEY",
        "SMTP_HOST",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "REPORT_TO_EMAIL",
        "REPORT_FROM_EMAIL"
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info("Environment validation passed.")


def fetch_with_retry(url, params, max_retries=3, backoff=2):
    """
    Fetch with exponential backoff for transient failures.
    Raises exception only after all retries exhausted.
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)

            # Handle rate limiting gracefully
            if resp.status_code == 429:
                wait_time = backoff ** attempt
                logger.warning(f"Rate limited (429). Attempt {attempt + 1}/{max_retries}, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue

            resp.raise_for_status()
            return resp
        except (requests.RequestException, requests.Timeout) as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed after {max_retries} attempts: {e}")
                raise
            wait_time = backoff ** attempt
            logger.warning(f"Attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
            time.sleep(wait_time)
    # All retries were 429s with no exception raised — treat as failure.
    raise RuntimeError(f"Exhausted retries for {url} (rate limited).")


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------
def fetch_eia_series(series_id):
    """Pull the two most recent daily values from EIA and compute % change."""
    url = "https://api.eia.gov/v2/seriesid/" + series_id
    params = {"api_key": EIA_API_KEY}
    resp = fetch_with_retry(url, params=params)
    data = resp.json()["response"]["data"]
    # EIA returns newest first
    latest = float(data[0]["value"])
    previous = float(data[1]["value"])
    change = latest - previous
    pct_change = (change / previous) * 100 if previous else 0
    return {"price": latest, "change": change, "pct_change": pct_change}


def fetch_av_commodity(function):
    """Alpha Vantage commodities endpoint (monthly-resolution for some, daily for others)."""
    url = "https://www.alphavantage.co/query"
    params = {"function": function, "interval": "daily", "apikey": AV_API_KEY}
    resp = fetch_with_retry(url, params=params)
    js = resp.json().get("data", [])
    if not js or len(js) < 2:
        raise ValueError(f"Insufficient data returned for {function}")
    latest = float(js[0]["value"])
    previous = float(js[1]["value"])
    change = latest - previous
    pct_change = (change / previous) * 100 if previous else 0
    return {"price": latest, "change": change, "pct_change": pct_change}


def fetch_av_fx(symbol):
    """Precious metals via Alpha Vantage FX_DAILY (quoted as e.g. XAU/USD)."""
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_DAILY",
        "from_symbol": symbol,
        "to_symbol": "USD",
        "apikey": AV_API_KEY,
    }
    resp = fetch_with_retry(url, params=params)
    series = resp.json().get("Time Series FX (Daily)", {})
    if not series:
        raise ValueError(f"No time series data for {symbol}")
    dates = sorted(series.keys(), reverse=True)
    if len(dates) < 2:
        raise ValueError(f"Insufficient historical data for {symbol}")
    latest = float(series[dates[0]]["4. close"])
    previous = float(series[dates[1]]["4. close"])
    change = latest - previous
    pct_change = (change / previous) * 100 if previous else 0
    return {"price": latest, "change": change, "pct_change": pct_change}


def fetch_eia_crude_inventory():
    """Weekly EIA crude oil stockpile change (only meaningful on release days, Wed)."""
    url = "https://api.eia.gov/v2/seriesid/PET.WCRSTUS1.W"
    params = {"api_key": EIA_API_KEY}
    resp = fetch_with_retry(url, params=params)
    data = resp.json()["response"]["data"]
    latest = float(data[0]["value"])
    previous = float(data[1]["value"])
    change = latest - previous
    period = data[0]["period"]
    return {"period": period, "level": latest, "change": change}


def fetch_commodity(commodity):
    source = commodity["source"]
    try:
        if source == "eia":
            return fetch_eia_series(commodity["series_id"])
        elif source == "av_commod":
            return fetch_av_commodity(commodity["function"])
        elif source == "av_fx":
            return fetch_av_fx(commodity["symbol"])
        else:
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch {commodity['name']}: {e}")
        return None


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------
def fmt_price(v):
    """Format price with validation for edge cases."""
    if v is None or v < 0:
        return "N/A"
    return f"${v:,.2f}"


def fmt_change(change, pct):
    sign = "+" if change >= 0 else ""
    color = "#1a7f37" if change >= 0 else "#c0342c"
    arrow = "▲" if change >= 0 else "▼"
    return f'<span style="color:{color};">{arrow} {sign}{change:,.2f} ({sign}{pct:.1f}%)</span>'


def build_html(results, inventory, data_quality):
    """Build HTML email with optional data quality warning."""
    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).strftime("%A, %B %d, %Y")

    # Data quality warning
    quality_warning = ""
    if data_quality < 1.0:
        warning_pct = int(data_quality * 100)
        quality_warning = f"""
        <div style="background-color:#fff3cd; border-left:4px solid #ffc107; padding:12px; margin-bottom:16px; font-size:13px;">
            <b>⚠ Data Quality Warning:</b> Only {warning_pct}% of commodities loaded successfully.
            Check that API keys are valid and rate limits haven't been exceeded.
        </div>
        """

    tier1_rows = ""
    movers_notes = ""
    for c in COMMODITIES:
        if c["tier"] != 1:
            continue
        r = results.get(c["name"])
        if not r:
            tier1_rows += f'<tr><td style="padding:8px;">{c["name"]}</td><td colspan="2" style="padding:8px;color:#888;">data unavailable</td></tr>'
            continue
        tier1_rows += (
            f'<tr>'
            f'<td style="padding:8px;font-weight:600;">{c["name"]}</td>'
            f'<td style="padding:8px;">{fmt_price(r["price"])}</td>'
            f'<td style="padding:8px;">{fmt_change(r["change"], r["pct_change"])}</td>'
            f'</tr>'
        )
        if abs(r["pct_change"]) >= BIG_MOVE_THRESHOLD_TIER1:
            movers_notes += f'<li><b>{c["name"]}</b> moved {r["pct_change"]:+.1f}% — check news for the driver.</li>'

    tier2_rows = ""
    for c in COMMODITIES:
        if c["tier"] != 2:
            continue
        r = results.get(c["name"])
        if not r:
            tier2_rows += f'<tr><td style="padding:6px 8px;">{c["name"]}</td><td colspan="2" style="padding:6px 8px;color:#888;">n/a</td></tr>'
            continue
        note = " ⚠" if abs(r["pct_change"]) >= BIG_MOVE_THRESHOLD_TIER2 else ""
        tier2_rows += (
            f'<tr>'
            f'<td style="padding:6px 8px;">{c["name"]}{note}</td>'
            f'<td style="padding:6px 8px;">{fmt_price(r["price"])}</td>'
            f'<td style="padding:6px 8px;">{fmt_change(r["change"], r["pct_change"])}</td>'
            f'</tr>'
        )

    inventory_section = ""
    if inventory:
        sign = "+" if inventory["change"] >= 0 else ""
        inventory_section = f"""
        <h3 style="margin-top:24px;">Inventory Watch</h3>
        <p style="font-size:14px;">EIA crude oil stocks ({inventory['period']}):
        {inventory['level']:,.1f}M bbl ({sign}{inventory['change']:,.1f}M vs prior week)</p>
        """

    movers_section = ""
    if movers_notes:
        movers_section = f"""
        <h3 style="margin-top:24px;">Notable Moves</h3>
        <ul style="font-size:14px;">{movers_notes}</ul>
        """

    html = f"""
    <html>
    <body style="font-family: -apple-system, Arial, sans-serif; color:#1a1a1a; max-width:600px; margin:0 auto;">
        <h2 style="margin-bottom:0;">Commodities Report</h2>
        <p style="color:#666; margin-top:4px;">{today}</p>

        {quality_warning}

        <h3>Headline Movers</h3>
        <table style="width:100%; border-collapse:collapse; font-size:15px;">
            {tier1_rows}
        </table>

        {movers_section}

        <h3 style="margin-top:24px;">Also Watching</h3>
        <table style="width:100%; border-collapse:collapse; font-size:13px; color:#333;">
            {tier2_rows}
        </table>

        {inventory_section}

        <p style="font-size:11px; color:#999; margin-top:32px;">
            Automated report. Data from EIA and Alpha Vantage. Not investment advice.
        </p>
    </body>
    </html>
    """
    return html


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_email(html_body, dry_run=False):
    """Send the email, or preview it in dry-run mode."""
    if dry_run:
        logger.info("DRY RUN MODE: Email preview (not sending):")
        logger.info(html_body)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Commodities Report — {datetime.now().strftime('%b %d, %Y')}"
    msg["From"] = os.environ["REPORT_FROM_EMAIL"]
    msg["To"] = os.environ["REPORT_TO_EMAIL"]
    msg.attach(MIMEText(html_body, "html"))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", 587))
    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.send_message(msg)
        logger.info(f"Email sent successfully to {os.environ['REPORT_TO_EMAIL']}")
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error while sending email: {e}")
        raise


def main(dry_run=False):
    validate_environment()

    results = {}
    for c in COMMODITIES:
        r = fetch_commodity(c)
        if r:
            results[c["name"]] = r

    # Calculate data quality
    total_commodities = len([c for c in COMMODITIES if c["source"] != "none"])
    successful_fetches = len(results)
    data_quality = successful_fetches / total_commodities if total_commodities > 0 else 0

    logger.info(f"Data quality: {successful_fetches}/{total_commodities} commodities ({data_quality*100:.0f}%)")

    # Abort if data quality is too low. IMPORTANT: this raises rather than
    # returning, so the GitHub Actions job fails loudly (red X) instead of
    # showing a false green checkmark while silently sending nothing.
    if data_quality < MIN_DATA_QUALITY:
        raise RuntimeError(
            f"Data quality {data_quality*100:.0f}% below minimum {MIN_DATA_QUALITY*100:.0f}%. "
            "Aborting email send to avoid sending a mostly-empty report. "
            "Check API keys and rate limits."
        )

    inventory = None
    try:
        inventory = fetch_eia_crude_inventory()
        logger.info("Inventory data fetched successfully")
    except Exception as e:
        logger.warning(f"Inventory fetch failed (non-fatal): {e}")

    html = build_html(results, inventory, data_quality)
    send_email(html, dry_run=dry_run)


if __name__ == "__main__":
    main()
