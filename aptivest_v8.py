import re
import streamlit as st
import math
import random
import requests
import os
from datetime import date

# =========================================================
# CONFIG
# =========================================================

APP_NAME      = "Aptivest"
APP_SUBTITLE  = "Value Investing Trainer"
SECRET_KEY    = "RULE1_API_KEY"   # key name in secrets.toml / environment

st.set_page_config(page_title=f"{APP_NAME} — {APP_SUBTITLE}", layout="wide")

# ── Resolve API key once at startup ──────────────────────────────────────────
# Tries every access pattern Streamlit supports before falling back to env var.
def _load_api_key() -> str:
    # 1. Direct attribute access  (most common)
    try:
        return st.secrets[SECRET_KEY]
    except Exception:
        pass
    # 2. Nested under [default] section
    try:
        return st.secrets["default"][SECRET_KEY]
    except Exception:
        pass
    # 3. .get() style
    try:
        v = st.secrets.get(SECRET_KEY)
        if v:
            return v
    except Exception:
        pass
    # 4. Environment variable fallback
    return os.getenv(SECRET_KEY, "")

API_KEY = _load_api_key()

# =========================================================
# CONSTANTS
# =========================================================

# Scoring thresholds
EPS_HI, EPS_LO   = 10.0, 5.0
REV_HI, REV_LO   = 10.0, 5.0
ROIC_HI, ROIC_LO = 15.0, 10.0
GROWTH_CAP        = 15.0   # default max growth rate in sticker calc (sidebar-editable)
DISCOUNT_RATE     = 0.15   # default desired annual return (sidebar-editable)
GROWTH_RATE_CAP   = 25.0   # hard cap for CAGR output

# Moat / verdict thresholds
STRONG_MOAT_MIN   = 7
MODERATE_MOAT_MIN = 4

# Discipline rewards/penalties
DISC_GREAT_BUY    = +15
DISC_GOOD_BUY     = +5
DISC_FAIR_BUY     = -5
DISC_OVERPAY      = -20

# Simulator
SIM_BEAT_BASE     = 0.03
SIM_BEAT_SCORE    = 0.005
SIM_BEAT_MOVE     = 1.08
SIM_MISS_MOVE     = 0.91
SIM_NOISE_SCALE   = 0.025
SIM_VALUE_PULL    = 0.008
SIM_TRADING_DAYS  = 252
SIM_PRICE_FLOOR   = 0.01   # prevent negative / zero prices

# Mission definitions  (id, title, goal, reward_xp)
MISSION_DEFS = [
    (1, "Analyze 2 Stocks",  2, 20),
    (2, "Buy Below MOS",     1, 30),
    (3, "Avoid Overpaying",  1, 25),
]

# XP level curve
XP_BASE   = 100
XP_FACTOR = 1.4

# API
API_TIMEOUT = 15   # seconds — FMP can be slow

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
    """Returns (level, xp_into_level, xp_needed_for_next)."""
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
    """Reset all game state to defaults."""
    for key in [
        "xp", "discipline", "watchlist", "live_prices",
        "portfolio", "streak", "missions", "last_day",
        "mission3_awarded", "todays_buys",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    st.rerun()

# =========================================================
# SESSION STATE INIT
# =========================================================

defaults: dict = {
    "xp":               0,
    "discipline":       100,
    "watchlist":        {},   # ticker → analysis snapshot (frozen at fetch time)
    "live_prices":      {},   # ticker → current simulated price
    "portfolio":        {},
    "streak":           1,
    "missions":         init_missions(),
    "last_day":         str(date.today()),
    "mission3_awarded": False,
    "todays_buys":      [],   # tickers bought today — for Mission 3 tracking
    # Sidebar-editable valuation assumptions
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
# API & SCORING FUNCTIONS
# =========================================================

def _safe_get(url: str) -> list | dict | None:
    """GET with timeout and HTTP error handling. Returns parsed JSON or None."""
    try:
        res = requests.get(url, timeout=API_TIMEOUT)
        res.raise_for_status()
        return res.json()
    except requests.exceptions.Timeout:
        st.warning(f"⏱ Request timed out ({url.split('?')[0].split('/')[-1]}).")
    except requests.exceptions.HTTPError as e:
        st.warning(f"HTTP {e.response.status_code} from API.")
    except Exception as e:
        st.warning(f"Network error: {e}")
    return None


def validate_ticker(ticker: str) -> str | None:
    """
    Return cleaned ticker or None with a warning.
    Accepts letters, dots, and hyphens up to 10 chars (covers BRK.B, RDS-A, etc).
    """
    cleaned = ticker.strip().upper()
    if not cleaned:
        st.warning("Enter a ticker symbol.")
        return None
    if not re.match(r'^[A-Z.\-]{1,10}$', cleaned):
        st.warning(
            f"'{cleaned}' doesn't look like a valid ticker. "
            "Use letters, dots, or hyphens (e.g. AAPL, BRK.B, RDS-A)."
        )
        return None
    return cleaned


def get_financials(ticker: str) -> dict | None:
    """
    Fetch profile, income statement, and ratios from FMP.
    Profile failure is fatal; income/ratio failures are non-fatal (partial data).
    """
    if not API_KEY:
        st.error(
            f"No API key found. Add `{SECRET_KEY}` to `.streamlit/secrets.toml` "
            "or as an environment variable."
        )
        return None
    api_key = API_KEY

    base         = "https://financialmodelingprep.com/api/v3"
    profile_data = _safe_get(f"{base}/profile/{ticker}?apikey={api_key}")

    if not isinstance(profile_data, list) or not profile_data:
        st.error(f"No profile data for '{ticker}'. Check the ticker symbol and try again.")
        return None

    profile     = profile_data[0]
    income_data = _safe_get(f"{base}/income-statement/{ticker}?limit=5&apikey={api_key}")
    ratios_data = _safe_get(f"{base}/ratios/{ticker}?limit=5&apikey={api_key}")

    eps_values = revenue_values = roic_values = []

    if isinstance(income_data, list):
        # FMP returns newest-first; preserve that order explicitly
        eps_values     = [d["eps"]     for d in income_data if d.get("eps")     is not None]
        revenue_values = [d["revenue"] for d in income_data if d.get("revenue") is not None]
    else:
        st.info(f"Income statement unavailable for {ticker} — EPS/revenue metrics will be zero.")

    if isinstance(ratios_data, list):
        roic_values = [
            d["returnOnCapitalEmployed"]
            for d in ratios_data
            if d.get("returnOnCapitalEmployed") is not None
        ]
    else:
        st.info(f"Ratios unavailable for {ticker} — ROIC will show as 0.")

    partial = not eps_values or not revenue_values or not roic_values

    return {
        "price":        profile.get("price", 0.0),
        "company_name": profile.get("companyName", ticker),
        "eps":          eps_values,
        "revenue":      revenue_values,
        "roic":         roic_values,
        "partial":      partial,
    }


def calc_cagr(values: list[float]) -> float:
    """
    Annualised CAGR from a list ordered **newest-first** (FMP default).
    Returns the actual value including negative growth — negative CAGR is real signal.
    Clamps at [-100, GROWTH_RATE_CAP] to avoid wild outliers.
    """
    try:
        # Keep only positive values (can't compute CAGR through zero/negative base)
        positives = [v for v in values if v and v > 0]
        if len(positives) < 2:
            return 0.0
        newest = positives[0]   # most recent
        oldest = positives[-1]  # furthest back
        if oldest <= 0:
            return 0.0
        n    = len(positives) - 1
        cagr = ((newest / oldest) ** (1 / n) - 1) * 100
        return round(max(-100.0, min(cagr, GROWTH_RATE_CAP)), 2)
    except Exception:
        return 0.0


def score_big_five(financials: dict) -> dict:
    """Score EPS growth, revenue growth, and ROIC (0–9). Returns dict with score + metrics."""
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

    score = pts(eps_growth, EPS_HI, EPS_LO) + pts(rev_growth, REV_HI, REV_LO) + pts(roic, ROIC_HI, ROIC_LO)
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
    """
    Rule-based sticker price using EPS compounding.
    Uses user-configured growth cap and discount rate from session state.
    Returns (0.0, 0.0) for non-positive EPS.

    PE note: 2× growth rate is an approximation. At 15% growth the PE = 30,
    which is aggressive. Users can lower the growth cap in the sidebar to
    produce more conservative estimates.
    """
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
hcol1.metric("⭐ XP",          st.session_state.xp)
hcol2.metric("🏆 Level",       level)
hcol3.metric("🛡️ Discipline",  f"{st.session_state.discipline}/100")
hcol4.metric("🔥 Streak",      f"{st.session_state.streak} days")

st.progress(
    xp_in_level / xp_needed,
    text=f"Level {level} — {xp_in_level}/{xp_needed} XP to next level"
)

# =========================================================
# MISSIONS
# =========================================================

st.subheader("🎯 Daily Missions")
for m in st.session_state.missions:
    icon   = "✅" if m["done"] else "⏳"
    filled = int((m["progress"] / m["goal"]) * 10)
    bar    = "█" * filled + "░" * (10 - filled)
    st.write(
        f"{icon} **{m['title']}** [{bar}] {m['progress']}/{m['goal']} "
        f"— reward: {m['reward']} XP"
    )

# =========================================================
# TABS
# =========================================================

tab1, tab2, tab3, tab4 = st.tabs(
    ["🔍 Analyze", "👁️ Watchlist", "💼 Portfolio", "📈 Simulator"]
)

# ===========================
# TAB 1 — ANALYZE
# ===========================

with tab1:
    st.markdown(
        "Enter a ticker to fetch real financials, score the Big Five health metrics, "
        "and compute a rule-based sticker price and margin of safety."
    )
    ticker_input = st.text_input("Ticker Symbol", "AAPL", max_chars=10).upper().strip()

    if st.button("Fetch & Analyze", key="btn_analyze"):
        clean = validate_ticker(ticker_input)
        if clean:
            with st.spinner(f"Fetching financials for {clean}…"):
                financials = get_financials(clean)

            if financials:
                price = financials["price"]
                if not price:
                    st.error("Price not available for this ticker. Try again later.")
                else:
                    bf           = score_big_five(financials)
                    latest_eps   = financials["eps"][0] if financials["eps"] else 0.0
                    sticker, mos = calculate_sticker(latest_eps, bf["eps_growth"])
                    moat         = moat_label(bf["score"])
                    verdict      = verdict_label(bf["score"])

                    if financials.get("partial"):
                        st.warning(
                            "⚠️ Partial data — one or more financial endpoints returned no data. "
                            "Scores below may be understated."
                        )

                    if sticker == 0.0:
                        st.warning(
                            "⚠️ EPS is zero or negative — a sticker price cannot be calculated. "
                            "This company may be pre-profit and is difficult to value with this method."
                        )

                    # Store analysis snapshot; price is frozen at fetch time
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
                    # Live price starts at analysis price; simulator drifts it from here
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
                            f"⚠️ Negative EPS growth ({bf['eps_growth']:.1f}%) detected. "
                            "The sticker price above uses 0% growth as a floor — treat it as optimistic."
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
        st.markdown(
            "Click **Buy** to add a position. "
            "Discipline rewards and penalties apply based on price vs. sticker."
        )
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
                    st.error(f"Cannot buy {t} — no valid sticker price (negative or missing EPS).")
                else:
                    is_wonderful = s.get("score", 0) >= STRONG_MOAT_MIN
                    buy_p        = live_price

                    if buy_p <= s["mos"] and is_wonderful:
                        update_discipline(DISC_GREAT_BUY)
                        update_mission(2)
                        st.session_state.portfolio[t] = {
                            "buy_price":    buy_p,
                            "sticker":      s["sticker"],
                            "date_bought":  str(date.today()),
                            "score":        s.get("score"),
                            "company_name": s.get("company_name", t),
                        }
                        st.session_state.todays_buys.append(t)
                        st.success(
                            f"Masterful buy! Wonderful company {t} below MOS. "
                            f"+{DISC_GREAT_BUY} Discipline"
                        )
                    elif buy_p <= s["mos"]:
                        update_discipline(DISC_GOOD_BUY)
                        update_mission(2)
                        st.session_state.portfolio[t] = {
                            "buy_price":    buy_p,
                            "sticker":      s["sticker"],
                            "date_bought":  str(date.today()),
                            "score":        s.get("score"),
                            "company_name": s.get("company_name", t),
                        }
                        st.session_state.todays_buys.append(t)
                        st.success(f"Bought {t} below MOS. +{DISC_GOOD_BUY} Discipline")
                    elif buy_p <= s["sticker"]:
                        update_discipline(DISC_FAIR_BUY)
                        st.session_state.portfolio[t] = {
                            "buy_price":    buy_p,
                            "sticker":      s["sticker"],
                            "date_bought":  str(date.today()),
                            "score":        s.get("score"),
                            "company_name": s.get("company_name", t),
                        }
                        st.session_state.todays_buys.append(t)
                        st.warning(
                            f"Bought {t} at fair value — no safety margin. "
                            f"{DISC_FAIR_BUY} Discipline"
                        )
                    else:
                        update_discipline(DISC_OVERPAY)
                        st.session_state.portfolio[t] = {
                            "buy_price":    buy_p,
                            "sticker":      s["sticker"],
                            "date_bought":  str(date.today()),
                            "score":        s.get("score"),
                            "company_name": s.get("company_name", t),
                        }
                        st.session_state.todays_buys.append(t)
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
        "Advance by one trading day. Company quality score influences whether events "
        "are beats, misses, or ordinary daily drift."
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
                cur            = st.session_state.live_prices.get(t, s["analysis_price"])

                # Guard against zero/near-zero price before division
                if cur <= 0:
                    cur = max(SIM_PRICE_FLOOR, s["analysis_price"])

                if roll < beat_threshold:
                    new_price = cur * SIM_BEAT_MOVE
                    events.append(("success", f"📈 {t} earnings beat! +{int((SIM_BEAT_MOVE-1)*100)}%"))
                elif roll > miss_threshold:
                    new_price = cur * SIM_MISS_MOVE
                    events.append(("error", f"📉 {t} earnings miss. -{int((1-SIM_MISS_MOVE)*100)}%"))
                else:
                    growth_drift = (s.get("eps_growth", 0) / 100) / SIM_TRADING_DAYS
                    value_pull   = (
                        (s["sticker"] - cur) / cur * SIM_VALUE_PULL
                        if s["sticker"] and cur > 0 else 0
                    )
                    noise     = (random.random() - 0.5) * SIM_NOISE_SCALE
                    new_price = cur * (1 + growth_drift + value_pull + noise)

                # Enforce price floor
                st.session_state.live_prices[t] = round(max(SIM_PRICE_FLOOR, new_price), 2)

            for kind, msg in events:
                if kind == "success":
                    st.success(msg)
                else:
                    st.error(msg)

            # Mission 3: award once if *today's* buys are all at or below sticker
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
            score_str = f"Score: {s.get('score', '?')}/9"
            st.write(
                f"{zone} **{t}**: ${live:.2f}  |  "
                f"MOS ${s['mos']:.2f}  |  Sticker ${s['sticker']:.2f}  |  {score_str}"
            )

# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:
    st.header("⚙️ Setup")

    if not API_KEY:
        st.warning("No API key detected.")
        st.markdown(
            f"Add to `.streamlit/secrets.toml`:\n"
            f"```toml\n{SECRET_KEY} = 'your_key_here'\n```\n"
            f"Or set the environment variable `{SECRET_KEY}`."
        )
    else:
        st.success("API key loaded ✓")

    st.divider()

    with st.expander("📐 Valuation Assumptions", expanded=False):
        st.session_state.growth_cap = st.slider(
            "Max growth rate cap (%)", 5.0, 25.0,
            float(st.session_state.growth_cap), 0.5,
            help=(
                "Caps the EPS CAGR used in the sticker price calculation. "
                "Lower this for more conservative estimates (e.g. 10–12%)."
            )
        )
        st.session_state.discount_rate = st.slider(
            "Desired annual return", 0.08, 0.25,
            float(st.session_state.discount_rate), 0.01,
            format="%.2f",
            help="Rate used to discount the 10-year future price back to today (0.15 = 15%)."
        )
        st.caption(
            "Changes take effect on your **next** analysis. "
            "Re-analyze a ticker to recalculate with new assumptions."
        )

    st.divider()

    with st.expander("📖 How It Works", expanded=True):
        st.markdown("**Value investing principles**")
        st.markdown("- Buy *wonderful* companies")
        st.markdown("- At a 50% margin of safety")
        st.markdown("- Hold long term")
        st.divider()
        st.markdown("**Big Five scoring (0–9)**")
        st.markdown(
            f"- EPS growth CAGR: >{int(EPS_HI)}% = 3pts, "
            f">{int(EPS_LO)}% = 2pts, >0% = 1pt"
        )
        st.markdown("- Revenue growth CAGR: same scale")
        st.markdown(
            f"- Avg ROIC: >{int(ROIC_HI)}% = 3pts, "
            f">{int(ROIC_LO)}% = 2pts, >0% = 1pt"
        )

    st.divider()

    st.markdown("**Reset**")
    if st.button("🔄 Reset All Progress", key="btn_reset"):
        reset_all()
