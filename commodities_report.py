"""
Daily Commodities Report
-------------------------
Fetches prices for a configurable list of commodities, builds a tiered
HTML email with small trend charts for the headline movers, and sends
it via SMTP.

Data sources:
  - EIA API (free, official US gov't data): WTI, Brent, Natural Gas,
    daily resolution, plus weekly crude oil inventory data.
  - Alpha Vantage (free tier): Copper, Aluminum, Wheat, Corn.
    IMPORTANT: these four endpoints only support Monthly/Quarterly/Annual
    resolution on Alpha Vantage — there is no daily data available for
    them at any tier. The "change" shown for these is month-over-month,
    not day-over-day, and is labeled as such in the email.
  - Metals.Dev (free tier, no credit card): Gold, Silver, Platinum,
    Palladium. Alpha Vantage does not offer precious metals data on the
    free tier (or possibly any tier) — its forex endpoint only covers
    real currency pairs, not metals, despite XAU/XAG/XPT/XPD looking
    like currency codes. Metals.Dev's timeseries endpoint conveniently
    returns all four metals for a date range in a single API call.

Note: Alpha Vantage doesn't cover Soybeans or Steel at all. Those stay
"N/A" — plug in a paid source (Nasdaq Data Link / Trading Economics) if
you want them.

Environment variables required (see README.md for how to get each):
  EIA_API_KEY            - free, from https://www.eia.gov/opendata/register.php
  ALPHAVANTAGE_API_KEY   - free, from https://www.alphavantage.co/support/#api-key
  METALS_API_KEY         - free, from https://metals.dev/ (sign up, no credit card needed)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD  - your email provider's SMTP creds
  REPORT_TO_EMAIL        - where the report gets sent
  REPORT_FROM_EMAIL      - the "from" address (often same as SMTP_USER)
"""

import os
import io
import smtplib
import requests
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
AV_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")
METALS_API_KEY = os.environ.get("METALS_API_KEY", "")

# Alpha Vantage free tier allows only 5 requests/minute.
AV_THROTTLE_SECONDS = 13

# ---------------------------------------------------------------------------
# Commodity configuration
# tier 1 = headline movers, shown in full WITH a trend chart
# tier 2 = compact table, no chart, commentary only on moves > 2%
# resolution: "daily" or "monthly" — controls the label shown in the email
# ---------------------------------------------------------------------------
COMMODITIES = [
    {"name": "WTI Crude",     "tier": 1, "source": "eia",       "series_id": "PET.RWTC.D", "resolution": "daily"},
    {"name": "Brent Crude",   "tier": 1, "source": "eia",       "series_id": "PET.RBRTE.D", "resolution": "daily"},
    {"name": "Gold",          "tier": 1, "source": "metals_dev", "resolution": "daily"},
    {"name": "Copper",        "tier": 1, "source": "av_commod", "function": "COPPER", "resolution": "monthly"},
    {"name": "Natural Gas",   "tier": 2, "source": "eia",       "series_id": "NG.RNGWHHD.D", "resolution": "daily"},
    {"name": "Silver",        "tier": 2, "source": "metals_dev", "resolution": "daily"},
    {"name": "Platinum",      "tier": 2, "source": "metals_dev", "resolution": "daily"},
    {"name": "Palladium",     "tier": 2, "source": "metals_dev", "resolution": "daily"},
    {"name": "Aluminum",      "tier": 2, "source": "av_commod", "function": "ALUMINUM", "resolution": "monthly"},
    {"name": "Steel",         "tier": 2, "source": "none"},
    {"name": "Corn",          "tier": 2, "source": "av_commod", "function": "CORN", "resolution": "monthly"},
    {"name": "Wheat",         "tier": 2, "source": "av_commod", "function": "WHEAT", "resolution": "monthly"},
    {"name": "Soybeans",      "tier": 2, "source": "none"},
]

BIG_MOVE_THRESHOLD_TIER1 = 1.0
BIG_MOVE_THRESHOLD_TIER2 = 2.0
MIN_DATA_QUALITY = 0.5


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def validate_environment():
    required_vars = [
        "EIA_API_KEY", "ALPHAVANTAGE_API_KEY", "METALS_API_KEY",
        "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
        "REPORT_TO_EMAIL", "REPORT_FROM_EMAIL",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info("Environment validation passed.")


def fetch_with_retry(url, params, max_retries=3, backoff=2):
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait_time = backoff ** attempt
                logger.warning(f"Rate limited (429). Attempt {attempt + 1}/{max_retries}, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            resp.raise_for_status()

            # Alpha Vantage returns HTTP 200 even when rate-limited.
            try:
                body = resp.json()
            except ValueError:
                return resp  # not JSON (shouldn't happen for these APIs)
            if isinstance(body, dict) and ("Information" in body or "Note" in body):
                msg = body.get("Information") or body.get("Note")
                if attempt == max_retries - 1:
                    raise RuntimeError(f"Alpha Vantage rate limit: {msg}")
                logger.warning(f"Alpha Vantage rate-limited, waiting {AV_THROTTLE_SECONDS}s before retry...")
                time.sleep(AV_THROTTLE_SECONDS)
                continue

            return resp
        except (requests.RequestException, requests.Timeout) as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed after {max_retries} attempts: {e}")
                raise
            wait_time = backoff ** attempt
            logger.warning(f"Attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
            time.sleep(wait_time)
    raise RuntimeError(f"Exhausted retries for {url}.")


# ---------------------------------------------------------------------------
# Data fetchers — each returns {"price", "change", "pct_change", "history": [(date, price), ...]}
# "history" is ascending by date and used to draw the trend chart (tier 1 only).
# ---------------------------------------------------------------------------
def fetch_eia_series(series_id):
    url = "https://api.eia.gov/v2/seriesid/" + series_id
    params = {"api_key": EIA_API_KEY}
    resp = fetch_with_retry(url, params=params)
    data = resp.json()["response"]["data"]  # newest first
    latest = float(data[0]["value"])
    previous = float(data[1]["value"])
    change = latest - previous
    pct_change = (change / previous) * 100 if previous else 0
    history = [(d["period"], float(d["value"])) for d in reversed(data[:14])]
    return {"price": latest, "change": change, "pct_change": pct_change, "history": history}


def fetch_av_commodity(function):
    """Copper/Aluminum/Wheat/Corn — Alpha Vantage only supports these at
    monthly/quarterly/annual resolution, so request monthly explicitly."""
    url = "https://www.alphavantage.co/query"
    params = {"function": function, "interval": "monthly", "apikey": AV_API_KEY}
    resp = fetch_with_retry(url, params=params)
    js = resp.json().get("data", [])
    if not js or len(js) < 2:
        raise ValueError(f"Insufficient data returned for {function}")
    latest = float(js[0]["value"])
    previous = float(js[1]["value"])
    change = latest - previous
    pct_change = (change / previous) * 100 if previous else 0
    history = [(d["date"], float(d["value"])) for d in reversed(js[:12])]
    return {"price": latest, "change": change, "pct_change": pct_change, "history": history}


def fetch_metals_dev_batch():
    """One call to Metals.Dev's timeseries endpoint returns Gold, Silver,
    Platinum, and Palladium together for the whole date range — far more
    efficient than fetching each metal separately."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    start = today - timedelta(days=13)  # 14-day window, within their 30-day max
    url = "https://api.metals.dev/v1/timeseries"
    params = {
        "api_key": METALS_API_KEY,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
    }
    resp = fetch_with_retry(url, params=params)
    body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error_message", "Metals.Dev request failed"))

    rates = body["rates"]  # {date_str: {"metals": {"gold": .., "silver": .., ...}, ...}}
    sorted_dates = sorted(rates.keys())

    metal_keys = {"gold": "Gold", "silver": "Silver", "platinum": "Platinum", "palladium": "Palladium"}
    out = {}
    for metal_key, display_name in metal_keys.items():
        history = []
        for date_str in sorted_dates:
            price = rates[date_str].get("metals", {}).get(metal_key)
            if price is not None:
                history.append((date_str, float(price)))
        if len(history) < 2:
            logger.warning(f"Not enough Metals.Dev history for {display_name}")
            continue
        latest = history[-1][1]
        previous = history[-2][1]
        change = latest - previous
        pct_change = (change / previous) * 100 if previous else 0
        out[display_name] = {"price": latest, "change": change, "pct_change": pct_change, "history": history}
    return out


def fetch_eia_crude_inventory():
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
        else:
            return None
    except Exception as e:
        logger.warning(f"Failed to fetch {commodity['name']}: {e}")
        return None


# ---------------------------------------------------------------------------
# Charting — small sparkline PNGs, embedded as proper CID email attachments.
# (Base64 data-URI images in <img src="data:..."> are stripped or not
# rendered by Gmail and many other clients — CID attachments are the
# reliable, standard way to embed images in HTML email.)
# ---------------------------------------------------------------------------
def build_sparkline_png(history):
    """history: list of (label, value) ascending. Returns raw PNG bytes or None."""
    if not history or len(history) < 2:
        return None
    values = [v for _, v in history]
    color = "#1a7f37" if values[-1] >= values[0] else "#c0342c"

    fig, ax = plt.subplots(figsize=(2.6, 0.7), dpi=120)
    ax.plot(range(len(values)), values, color=color, linewidth=1.8)
    ax.fill_between(range(len(values)), values, min(values), color=color, alpha=0.08)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------
def fmt_price(v):
    if v is None or v < 0:
        return "N/A"
    return f"${v:,.2f}"


def fmt_change(change, pct, resolution="daily"):
    sign = "+" if change >= 0 else ""
    color = "#1a7f37" if change >= 0 else "#c0342c"
    arrow = "▲" if change >= 0 else "▼"
    label = " (mo/mo)" if resolution == "monthly" else ""
    return f'<span style="color:{color};">{arrow} {sign}{change:,.2f} ({sign}{pct:.1f}%){label}</span>'


def build_html(results, inventory, data_quality):
    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).strftime("%A, %B %d, %Y")
    chart_images = {}  # cid -> png bytes, returned alongside the HTML

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
            tier1_rows += f'<tr><td style="padding:8px;">{c["name"]}</td><td colspan="3" style="padding:8px;color:#888;">data unavailable</td></tr>'
            continue
        png_bytes = build_sparkline_png(r.get("history", []))
        chart_cell = ""
        if png_bytes:
            cid = f"chart_{c['name'].lower().replace(' ', '_')}"
            chart_images[cid] = png_bytes
            chart_cell = f'<img src="cid:{cid}" width="90" height="24" alt="trend" />'
        tier1_rows += (
            f'<tr>'
            f'<td style="padding:8px;font-weight:600;">{c["name"]}</td>'
            f'<td style="padding:8px;">{fmt_price(r["price"])}</td>'
            f'<td style="padding:8px;">{fmt_change(r["change"], r["pct_change"], c.get("resolution", "daily"))}</td>'
            f'<td style="padding:8px;">{chart_cell}</td>'
            f'</tr>'
        )
        if abs(r["pct_change"]) >= BIG_MOVE_THRESHOLD_TIER1:
            period_word = "this month" if c.get("resolution") == "monthly" else "today"
            movers_notes += f'<li><b>{c["name"]}</b> moved {r["pct_change"]:+.1f}% {period_word} — check news for the driver.</li>'

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
            f'<td style="padding:6px 8px;">{fmt_change(r["change"], r["pct_change"], c.get("resolution", "daily"))}</td>'
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
    <body style="font-family: -apple-system, Arial, sans-serif; color:#1a1a1a; max-width:640px; margin:0 auto;">
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
            Automated report. Data from EIA, Alpha Vantage, and Metals-API. Not investment advice.
            Copper/Aluminum/Corn/Wheat changes are month-over-month (no daily data available from source).
        </p>
    </body>
    </html>
    """
    return html, chart_images


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_email(html_body, chart_images=None, dry_run=False):
    if dry_run:
        logger.info("DRY RUN MODE: Email preview (not sending):")
        logger.info(html_body)
        logger.info(f"Would attach {len(chart_images or {})} chart image(s): {list((chart_images or {}).keys())}")
        return

    # multipart/related wraps the alternative body + inline images, so mail
    # clients that support CID references (Gmail included) render the charts.
    msg = MIMEMultipart("related")
    msg["Subject"] = f"Commodities Report — {datetime.now().strftime('%b %d, %Y')}"
    msg["From"] = os.environ["REPORT_FROM_EMAIL"]
    msg["To"] = os.environ["REPORT_TO_EMAIL"]

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(html_body, "html"))
    msg.attach(alt_part)

    for cid, png_bytes in (chart_images or {}).items():
        img = MIMEImage(png_bytes, "png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(img)

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

    # Precious metals: one batched call covers all four (Gold, Silver, Platinum, Palladium)
    try:
        results.update(fetch_metals_dev_batch())
    except Exception as e:
        logger.warning(f"Metals.Dev batch fetch failed: {e}")

    av_calls_made = 0
    for c in COMMODITIES:
        if c["source"] == "metals_dev":
            continue  # already handled above
        if c["source"] == "av_commod":
            if av_calls_made > 0:
                time.sleep(AV_THROTTLE_SECONDS)
            av_calls_made += 1
        r = fetch_commodity(c)
        if r:
            results[c["name"]] = r

    total_commodities = len([c for c in COMMODITIES if c["source"] != "none"])
    successful_fetches = len(results)
    data_quality = successful_fetches / total_commodities if total_commodities > 0 else 0
    logger.info(f"Data quality: {successful_fetches}/{total_commodities} commodities ({data_quality*100:.0f}%)")

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

    html, chart_images = build_html(results, inventory, data_quality)
    send_email(html, chart_images=chart_images, dry_run=dry_run)


if __name__ == "__main__":
    main()
