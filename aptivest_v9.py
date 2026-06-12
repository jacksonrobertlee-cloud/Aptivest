import re
import streamlit as st
import math
import random
import os
import yfinance as yf
from datetime import date

# =========================================================
# CONFIG
# =========================================================

APP_NAME     = "Aptivest"
APP_SUBTITLE = "Value Investing Trainer"

st.set_page_config(page_title=f"{APP_NAME} — {APP_SUBTITLE}", layout="wide")

# =========================================================
# CONSTANTS
# =========================================================

EPS_HI, EPS_LO   = 10.0, 5.0
REV_HI, REV_LO   = 10.0, 5.0
ROIC_HI, ROIC_LO = 15.0, 10.0
GROWTH_CAP        = 15.0
DISCOUNT_RATE     = 0.15
GROWTH_RATE_CAP   = 25.0

STRONG_MOAT_MIN   = 7
MODERATE_MOAT_MIN = 4

DISC_GREAT_BUY = +15
DISC_GOOD_BUY  = +5
DISC_FAIR_BUY  = -5
DISC_OVERPAY   = -20

SIM_BEAT_BASE    = 0.03
SIM_BEAT_SCORE   = 0.005
SIM_BEAT_MOVE    = 1.08
SIM_MISS_MOVE    = 0.91
SIM_NOISE_SCALE  = 0.025
SIM_VALUE_PULL   = 0.008
SIM_TRADING_DAYS = 252
SIM_PRICE_FLOOR  = 0.01

MISSION_DEFS = [
    (1, "Analyze 2 Stocks", 2, 20),
    (2, "Buy Below MOS",    1, 30),
    (3, "Avoid Overpaying", 1, 25),
]

XP_BASE   = 100
XP_FACTOR = 1.4

# =========================================================
# HELPERS
# =========================================================

def init_missions() -> list[dict]:
    return [
        {"id": i, "title": t, "progress": 0, "goal": g, "reward": r, "done": False}
        for i, t, g, r in MISSION_DEFS
    ]

def add_xp(amount: int, reason: str = "") -> None:
    st.session_state.xp += amount
    if reason:
        st.toast(f"+{amount} XP — {reason}", icon="⭐")

def update_discipline(delta: int) -> None:
    st.session_state.discipline = max(0, min(100, st.session_state.discipline + delta))

def xp_to_level(xp: int) -> tuple[int, int, int]:
    level, threshold = 1, XP_BASE
    while xp >= threshold:
        xp       -= threshold
        level    += 1
        threshold = int(threshold * XP_FACTOR)
    return level, xp, threshold

def update_mission(m_id: int) -> None:
    for m in st.session_state.missions:
        if m["id"] == m_id and not m["done"]:
            m["progress"] = min(m["progress"] + 1, m["goal"])
            if m["progress"] >= m["goal"]:
                m["done"] = True
                add_xp(m["reward"], f"Mission: {m['title']}")
                st.success(f"✅ Mission Complete: {m['title']}")

def reset_all() -> None:
    for key in [
        "xp", "discipline", "watchlist", "live_prices",
        "portfolio", "streak", "missions", "last_day",
        "mission3_awarded", "todays_buys",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()

# =========================================================
# SESSION STATE
# =========================================================

defaults: dict = {
    "xp":               0,
    "discipline":       100,
    "watchlist":        {},
    "live_prices":      {},
    "portfolio":        {},
    "streak":           1,
    "missions":         init_missions(),
    "last_day":         str(date.today()),
    "mission3_awarded": False,
    "todays_buys":      [],
    "growth_cap":       GROWTH_CAP,
    "discount_rate":    DISCOUNT_RATE,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# =========================================================
# DAILY RESET
# =========================================================

today = str(date.today())
if st.session_state.last_day != today:
    incomplete = [m for m in st.session_state.missions if not m["done"]]
    if incomplete:
        st.session_state.streak = 0
        st.warning("❌ Streak lost — you didn't complete all missions yesterday.")
    else:
        st.session_state.streak += 1
        st.info("🌅 New day — missions reset. Streak continues!")
    st.session_state.missions         = init_missions()
    st.session_state.last_day         = today
    st.session_state.mission3_awarded = False
    st.session_state.todays_buys      = []

# =========================================================
# DATA LAYER — yfinance (no API key required)
# =========================================================

def validate_ticker(ticker: str) -> str | None:
    cleaned = ticker.strip().upper()
    if not cleaned:
        st.warning("Enter a ticker symbol.")
        return None
    if not re.match(r'^[A-Z.\-]{1,10}$', cleaned):
        st.warning(
            f"'{cleaned}' doesn't look like a valid ticker. "
            "Use letters, dots, or hyphens (e.g. AAPL, BRK.B)."
        )
        return None
    return cleaned


def get_financials(ticker: str) -> dict | None:
    """
    Pull financials from Yahoo Finance via yfinance.
    No API key required. Returns unified dict or None on failure.
    """
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info or {}
    except Exception as e:
        st.error(f"Could not fetch data for '{ticker}': {e}")
        return None

    # Current price — try multiple fields yfinance uses
    price = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
        or 0.0
    )
    company_name = info.get("longName") or info.get("shortName") or ticker

    if not price:
        st.error(
            f"No price data found for '{ticker}'. "
            "Double-check the symbol (e.g. BRK-B not BRK.B on Yahoo)."
        )
        return None

    # ── Income statement (annual, newest first) ──────────────────────────────
    eps_values     = []
    revenue_values = []
    try:
        financials = stock.financials          # columns = dates, newest left
        if financials is not None and not financials.empty:
            # Revenue
            for row_name in ["Total Revenue", "Revenue"]:
                if row_name in financials.index:
                    revenue_values = [
                        float(v) for v in financials.loc[row_name].values
                        if v is not None and str(v) != "nan"
                    ]
                    break
            # Net income as EPS proxy when EPS not directly available
            for row_name in ["Basic EPS", "Diluted EPS"]:
                if row_name in financials.index:
                    eps_values = [
                        float(v) for v in financials.loc[row_name].values
                        if v is not None and str(v) != "nan"
                    ]
                    break
    except Exception:
        st.info(f"Income statement unavailable for {ticker} — some metrics will be estimated.")

    # EPS fallback: use trailingEps / forwardEps from info
    if not eps_values:
        fallback_eps = info.get("trailingEps") or info.get("forwardEps")
        if fallback_eps:
            eps_values = [float(fallback_eps)]

    # ── ROIC proxy via returnOnEquity from info ───────────────────────────────
    roic_values = []
    try:
        roe = info.get("returnOnEquity")
        if roe is not None:
            roic_values = [float(roe)]   # single value; good enough for scoring
    except Exception:
        pass

    partial = not eps_values or not revenue_values or not roic_values

    return {
        "price":        float(price),
        "company_name": company_name,
        "eps":          eps_values,
        "revenue":      revenue_values,
        "roic":         roic_values,
        "partial":      partial,
    }

# =========================================================
# SCORING FUNCTIONS
# =========================================================

def calc_cagr(values: list[float]) -> float:
    """CAGR from newest-first list. Allows negative output (real signal)."""
    try:
        positives = [v for v in values if v and v > 0]
        if len(positives) < 2:
            return 0.0
        newest, oldest = positives[0], positives[-1]
        if oldest <= 0:
            return 0.0
        n    = len(positives) - 1
        cagr = ((newest / oldest) ** (1 / n) - 1) * 100
        return round(max(-100.0, min(cagr, GROWTH_RATE_CAP)), 2)
    except Exception:
        return 0.0


def score_big_five(financials: dict) -> dict:
    eps_growth = calc_cagr(financials["eps"])
    rev_growth = calc_cagr(financials["revenue"])
    roic       = (
        round(sum(financials["roic"]) / len(financials["roic"]) * 100, 2)
        if financials["roic"] else 0.0
    )

    def pts(val, hi, lo) -> int:
        if val > hi:  return 3
        if val > lo:  return 2
        if val > 0:   return 1
        return 0

    score = (
        pts(eps_growth, EPS_HI, EPS_LO)
        + pts(rev_growth, REV_HI, REV_LO)
        + pts(roic, ROIC_HI, ROIC_LO)
    )
    return {"score": score, "eps_growth": eps_growth, "rev_growth": rev_growth, "roic": roic}


def moat_label(score: int) -> str:
    if score >= STRONG_MOAT_MIN:   return "Strong"
    if score >= MODERATE_MOAT_MIN: return "Moderate"
    return "Weak"


def verdict_label(score: int) -> str:
    if score >= STRONG_MOAT_MIN:   return "✅ Wonderful Company"
    if score >= MODERATE_MOAT_MIN: return "⚠️ Average Company"
    return "❌ Avoid"


def calculate_sticker(eps: float, growth_pct: float) -> tuple[float, float]:
    if eps <= 0:
        return 0.0, 0.0
    g            = max(0.0, min(growth_pct, st.session_state.growth_cap))
    pe           = max(10.0, 2 * g)
    future_eps   = eps * math.pow(1 + g / 100, 10)
    future_price = future_eps * pe
    sticker      = future_price / math.pow(1 + st.session_state.discount_rate, 10)
    mos          = sticker / 2
    return round(sticker, 2), round(mos, 2)

# =========================================================
# HEADER
# =========================================================

st.title(f"🎯 {APP_NAME} — {APP_SUBTITLE}")

level, xp_in_level, xp_needed = xp_to_level(st.session_state.xp)
hcol1, hcol2, hcol3, hcol4 = st.columns(4)
hcol1.metric("⭐ XP",         st.session_state.xp)
hcol2.metric("🏆 Level",      level)
hcol3.metric("🛡️ Discipline", f"{st.session_state.discipline}/100")
hcol4.metric("🔥 Streak",     f"{st.session_state.streak} days")
st.progress(xp_in_level / xp_needed, text=f"Level {level} — {xp_in_level}/{xp_needed} XP to next level")

# =========================================================
# MISSIONS
# =========================================================

st.subheader("🎯 Daily Missions")
for m in st.session_state.missions:
    icon   = "✅" if m["done"] else "⏳"
    filled = int((m["progress"] / m["goal"]) * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    st.write(f"{icon} **{m['title']}** [{bar}] {m['progress']}/{m['goal']} — reward: {m['reward']} XP")

# =========================================================
# TABS
# =========================================================

tab1, tab2, tab3, tab4 = st.tabs(["🔍 Analyze", "👁️ Watchlist", "💼 Portfolio", "📈 Simulator"])

# ===========================
# TAB 1 — ANALYZE
# ===========================

with tab1:
    st.markdown(
        "Enter a ticker to fetch real financials from Yahoo Finance (no API key needed), "
        "score the Big Five health metrics, and compute a rule-based sticker price."
    )
    ticker_input = st.text_input("Ticker Symbol", "AAPL", max_chars=10).upper().strip()

    if st.button("Fetch & Analyze", key="btn_analyze"):
        clean = validate_ticker(ticker_input)
        if clean:
            with st.spinner(f"Fetching data for {clean} from Yahoo Finance…"):
                financials = get_financials(clean)

            if financials:
                price        = financials["price"]
                bf           = score_big_five(financials)
                latest_eps   = financials["eps"][0] if financials["eps"] else 0.0
                sticker, mos = calculate_sticker(latest_eps, bf["eps_growth"])
                moat         = moat_label(bf["score"])
                verdict      = verdict_label(bf["score"])

                if financials.get("partial"):
                    st.warning(
                        "⚠️ Partial data — one or more metrics could not be retrieved. "
                        "Scores may be understated."
                    )
                if sticker == 0.0:
                    st.warning(
                        "⚠️ EPS is zero or negative — sticker price cannot be calculated. "
                        "This company may be pre-profit."
                    )

                st.session_state.watchlist[clean] = {
                    "analysis_price": price,
                    "sticker":        sticker,
                    "mos":            mos,
                    "score":          bf["score"],
                    "moat":           moat,
                    "verdict":        verdict,
                    "eps_growth":     bf["eps_growth"],
                    "rev_growth":     bf["rev_growth"],
                    "roic":           bf["roic"],
                    "company_name":   financials["company_name"],
                }
                st.session_state.live_prices[clean] = price

                add_xp(10, f"Analyzed {clean}")
                update_mission(1)

                st.success(f"**{clean}** — {financials['company_name']}")
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("Current Price",    f"${price:.2f}")
                mc2.metric("Sticker Price",    f"${sticker:.2f}" if sticker else "N/A")
                mc3.metric("Margin of Safety", f"${mos:.2f}"     if mos     else "N/A")
                mc4.metric("Verdict",           verdict)

                with st.expander("📊 Big Five Health Metrics", expanded=True):
                    bc1, bc2, bc3, bc4 = st.columns(4)
                    bc1.metric("EPS Growth (CAGR)",     f"{bf['eps_growth']:.1f}%")
                    bc2.metric("Revenue Growth (CAGR)", f"{bf['rev_growth']:.1f}%")
                    bc3.metric("Avg ROIC",              f"{bf['roic']:.1f}%")
                    bc4.metric("Moat",                  f"{moat} ({bf['score']}/9)")

                if bf["eps_growth"] < 0:
                    st.warning(
                        f"⚠️ Negative EPS growth ({bf['eps_growth']:.1f}%) — "
                        "sticker price uses 0% as a floor; treat it as optimistic."
                    )

                if sticker > 0:
                    if price <= mos:
                        st.success("✅ **BUY ZONE** — price is below margin of safety")
                    elif price <= sticker:
                        st.info("🟡 **FAIR VALUE** — below sticker but above MOS")
                    else:
                        st.warning("❌ **WAIT** — price exceeds sticker price")

# ===========================
# TAB 2 — WATCHLIST
# ===========================

with tab2:
    if not st.session_state.watchlist:
        st.info("No stocks on your watchlist yet. Analyze some stocks in the Analyze tab.")
    else:
        st.markdown("Click **Buy** to add a position. Discipline rewards apply based on price vs. sticker.")
        for t, s in list(st.session_state.watchlist.items()):
            live_price = st.session_state.live_prices.get(t, s["analysis_price"])
            wc1, wc2, wc3, wc4, wc5, wc6 = st.columns([1.5, 1.2, 1.2, 1.2, 1.5, 0.8])
            wc1.write(f"**{t}** — {s.get('company_name', '')}")
            wc2.write(f"Live: ${live_price:.2f}")
            wc3.write(f"Sticker: ${s['sticker']:.2f}" if s["sticker"] else "Sticker: N/A")
            wc4.write(f"MOS: ${s['mos']:.2f}"         if s["mos"]     else "MOS: N/A")
            wc5.write(s.get("verdict", "—"))

            if wc6.button("Buy", key=f"buy_{t}"):
                if t in st.session_state.portfolio:
                    st.warning(f"Already holding {t}.")
                elif s["sticker"] == 0:
                    st.error(f"Cannot buy {t} — no valid sticker price.")
                else:
                    is_wonderful = s.get("score", 0) >= STRONG_MOAT_MIN
                    buy_p = live_price
                    entry = {
                        "buy_price":    buy_p,
                        "sticker":      s["sticker"],
                        "date_bought":  str(date.today()),
                        "score":        s.get("score"),
                        "company_name": s.get("company_name", t),
                    }
                    st.session_state.portfolio[t] = entry
                    st.session_state.todays_buys.append(t)

                    if buy_p <= s["mos"] and is_wonderful:
                        update_discipline(DISC_GREAT_BUY)
                        update_mission(2)
                        st.success(f"Masterful buy! Wonderful company {t} below MOS. +{DISC_GREAT_BUY} Discipline")
                    elif buy_p <= s["mos"]:
                        update_discipline(DISC_GOOD_BUY)
                        update_mission(2)
                        st.success(f"Bought {t} below MOS. +{DISC_GOOD_BUY} Discipline")
                    elif buy_p <= s["sticker"]:
                        update_discipline(DISC_FAIR_BUY)
                        st.warning(f"Bought {t} at fair value — no safety margin. {DISC_FAIR_BUY} Discipline")
                    else:
                        update_discipline(DISC_OVERPAY)
                        st.error(f"Overpaid for {t}! {DISC_OVERPAY} Discipline")

# ===========================
# TAB 3 — PORTFOLIO
# ===========================

with tab3:
    if not st.session_state.portfolio:
        st.info("No positions yet. Buy stocks from the Watchlist tab.")
    else:
        total_pl = 0.0
        for t, pos in st.session_state.portfolio.items():
            current  = st.session_state.live_prices.get(t, pos["buy_price"])
            pl       = current - pos["buy_price"]
            pl_pct   = (pl / pos["buy_price"]) * 100 if pos["buy_price"] else 0.0
            total_pl += pl
            pc1, pc2, pc3, pc4, pc5 = st.columns([1, 1, 1, 1, 1.5])
            pc1.write(f"**{t}**")
            pc2.write(f"Buy: ${pos['buy_price']:.2f}")
            pc3.write(f"Now: ${current:.2f}")
            color = "🟢" if pl >= 0 else "🔴"
            pc4.write(f"{color} P/L: ${pl:+.2f} ({pl_pct:+.1f}%)")
            pc5.caption(
                f"{pos.get('company_name', '')} · "
                f"Bought {pos.get('date_bought', '—')} · "
                f"Score {pos.get('score', '?')}/9"
            )
        st.divider()
        st.metric("Total Unrealised P/L", f"${total_pl:+.2f}")

# ===========================
# TAB 4 — SIMULATOR
# ===========================

with tab4:
    st.markdown(
        "Advance by one trading day. Company quality score influences "
        "whether events are beats, misses, or ordinary drift."
    )
    if not st.session_state.watchlist:
        st.info("Add stocks to your watchlist first.")
    else:
        if st.button("⏩ Next Day", key="btn_next_day"):
            events = []
            for t in list(st.session_state.watchlist.keys()):
                s     = st.session_state.watchlist[t]
                score = s.get("score", 4)
                roll  = random.random()

                beat_threshold = SIM_BEAT_BASE + score * SIM_BEAT_SCORE
                miss_threshold = 1.0 - (SIM_BEAT_BASE + (9 - score) * SIM_BEAT_SCORE)
                cur            = max(SIM_PRICE_FLOOR, st.session_state.live_prices.get(t, s["analysis_price"]))

                if roll < beat_threshold:
                    new_price = cur * SIM_BEAT_MOVE
                    events.append(("success", f"📈 {t} earnings beat! +{int((SIM_BEAT_MOVE-1)*100)}%"))
                elif roll > miss_threshold:
                    new_price = cur * SIM_MISS_MOVE
                    events.append(("error", f"📉 {t} earnings miss. -{int((1-SIM_MISS_MOVE)*100)}%"))
                else:
                    growth_drift = (s.get("eps_growth", 0) / 100) / SIM_TRADING_DAYS
                    value_pull   = (s["sticker"] - cur) / cur * SIM_VALUE_PULL if s["sticker"] else 0
                    noise        = (random.random() - 0.5) * SIM_NOISE_SCALE
                    new_price    = cur * (1 + growth_drift + value_pull + noise)

                st.session_state.live_prices[t] = round(max(SIM_PRICE_FLOOR, new_price), 2)

            for kind, msg in events:
                if kind == "success": st.success(msg)
                else:                 st.error(msg)

            if not st.session_state.mission3_awarded and st.session_state.todays_buys:
                overpaid_today = any(
                    st.session_state.portfolio.get(t, {}).get("buy_price", float("inf"))
                    > st.session_state.watchlist.get(t, {}).get("sticker", float("inf"))
                    for t in st.session_state.todays_buys
                )
                if not overpaid_today:
                    update_mission(3)
                    st.session_state.mission3_awarded = True

        st.subheader("Current Prices")
        for t, s in st.session_state.watchlist.items():
            live = st.session_state.live_prices.get(t, s["analysis_price"])
            if s["sticker"] > 0:
                zone = "✅" if live < s["mos"] else ("🟡" if live < s["sticker"] else "❌")
            else:
                zone = "⚪"
            st.write(
                f"{zone} **{t}**: ${live:.2f}  |  "
                f"MOS ${s['mos']:.2f}  |  Sticker ${s['sticker']:.2f}  |  "
                f"Score {s.get('score','?')}/9"
            )

# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:
    st.header("⚙️ Setup")
    st.success("✓ No API key required — powered by Yahoo Finance")

    st.divider()
    with st.expander("📐 Valuation Assumptions", expanded=False):
        st.session_state.growth_cap = st.slider(
            "Max growth rate cap (%)", 5.0, 25.0,
            float(st.session_state.growth_cap), 0.5,
            help="Caps EPS CAGR in sticker price calc. Lower = more conservative."
        )
        st.session_state.discount_rate = st.slider(
            "Desired annual return", 0.08, 0.25,
            float(st.session_state.discount_rate), 0.01,
            format="%.2f",
            help="Discount rate for 10-year future value (0.15 = 15%)."
        )
        st.caption("Changes apply on your **next** analysis.")

    st.divider()
    with st.expander("📖 How It Works", expanded=True):
        st.markdown("**Value investing principles**")
        st.markdown("- Buy *wonderful* companies")
        st.markdown("- At a 50% margin of safety")
        st.markdown("- Hold long term")
        st.divider()
        st.markdown("**Big Five scoring (0–9)**")
        st.markdown(f"- EPS growth CAGR: >{int(EPS_HI)}% = 3pts, >{int(EPS_LO)}% = 2pts, >0% = 1pt")
        st.markdown("- Revenue growth CAGR: same scale")
        st.markdown(f"- Avg ROIC: >{int(ROIC_HI)}% = 3pts, >{int(ROIC_LO)}% = 2pts, >0% = 1pt")

    st.divider()
    st.markdown("**Reset**")
    if st.button("🔄 Reset All Progress", key="btn_reset"):
        reset_all()
