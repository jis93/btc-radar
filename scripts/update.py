#!/usr/bin/env python3
"""Radar Bottom BTC — collecte quotidienne des indicateurs + juge LLM.

Stdlib uniquement (urllib). Chaque fetcher est indépendant : un échec
n'écrase jamais la dernière valeur connue (data.json est rechargé et
fusionné). Le juge LLM (Groq compound-mini, recherche web intégrée)
est conservateur : OUI seulement si confiance haute + source < 7 jours,
sinon état inchangé.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "data.json")

UA = {"User-Agent": "Mozilla/5.0 (btc-radar; +https://github.com/jis93/btc-radar)"}

CYCLE_TOP = date(2025, 10, 6)          # ATH $126,296
CYCLE_BOTTOM_DAYS = (363, 384)         # fenêtre historique sur 3 cycles


def http_json(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_text(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def load_previous():
    try:
        with open(DATA_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- fetchers

def fetch_btc(out):
    d = http_json("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=bitcoin&vs_currencies=usd&include_market_cap=true"
                  "&include_24hr_change=true")
    b = d["bitcoin"]
    out["btc_usd"] = round(b["usd"])
    out["btc_24h_pct"] = round(b.get("usd_24h_change") or 0, 2)
    out["btc_mcap_usd"] = b.get("usd_market_cap")


def yahoo_series(symbol, rng="3mo"):
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                  f"{symbol}?range={rng}&interval=1d")
    res = d["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
    return closes


def fetch_markets(out):
    brent = yahoo_series("BZ=F")
    out["brent_usd"] = round(brent[-1], 2)
    if len(brent) >= 11:
        out["brent_14d_pct"] = round((brent[-1] / brent[-11] - 1) * 100, 1)
    spx = yahoo_series("^GSPC")
    out["spx"] = round(spx[-1])
    out["spx_drawdown_pct"] = round((spx[-1] / max(spx) - 1) * 100, 1)
    nvda = yahoo_series("NVDA")
    out["nvda_drawdown_pct"] = round((nvda[-1] / max(nvda) - 1) * 100, 1)


def fetch_fng(out):
    d = http_json("https://api.alternative.me/fng/?limit=1")
    out["fear_greed"] = int(d["data"][0]["value"])


def fetch_funding(out):
    d = http_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
    out["funding_rate_pct"] = round(float(d["lastFundingRate"]) * 100, 4)


def fetch_hashrate(out):
    d = http_json("https://mempool.space/api/v1/mining/hashrate/3m")
    hs = d.get("hashrates") or []
    if len(hs) >= 30:
        cur = sum(h["avgHashrate"] for h in hs[-7:]) / 7
        old = sum(h["avgHashrate"] for h in hs[-37:-30]) / 7
        out["hashrate_30d_pct"] = round((cur / old - 1) * 100, 1)


def fetch_mvrv(out):
    from datetime import timedelta
    start = (date.today() - timedelta(days=6)).isoformat()
    d = http_json("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
                  "?assets=btc&metrics=CapMrktCurUSD,CapRealUSD&frequency=1d"
                  f"&start_time={start}")
    row = d["data"][-1]
    out["mvrv"] = round(float(row["CapMrktCurUSD"]) / float(row["CapRealUSD"]), 2)


def fetch_etf_flows(out):
    """Farside — best effort. Somme des 15 derniers totaux quotidiens (M$)."""
    html = http_text("https://farside.co.uk/btc/")
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.S)
    daily = []
    for tr in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(cells) >= 2 and re.match(r"\d{1,2} \w{3} \d{4}", cells[0]):
            raw = cells[-1].replace(",", "").replace("(", "-").replace(")", "")
            try:
                daily.append(float(raw))
            except ValueError:
                pass
    if daily:
        out["etf_last_day_musd"] = daily[-1]
        out["etf_flows_15d_musd"] = round(sum(daily[-15:]), 1)


# ---------------------------------------------------------------- juge LLM

JUDGE_QUESTIONS = {
    "ceasefire": (
        "Iran-USA war 2026: has Iran ACCEPTED (not merely received or discussed) "
        "a ceasefire or peace agreement with the United States that is currently "
        "in force? A proposal, mediation offer, or rumor does NOT count."
    ),
    "gulf_infra": (
        "In the last 7 days, has a MAJOR OPERATIONAL Gulf energy facility been "
        "hit and materially damaged: Ras Tanura, Abqaiq, Ruwais, Ras Laffan "
        "LNG, or Kharg Island export terminals? Attacks on tankers, military "
        "bases, or already-shut facilities do NOT count."
    ),
    "fed_cut": (
        "Has the US Federal Reserve CUT its policy rate at its most recent 2026 "
        "FOMC meeting, or officially signaled a cut at the next meeting as "
        "near-certain? Market speculation alone does NOT count."
    ),
}

JUDGE_SYSTEM = (
    "You are a conservative fact-checker for wartime financial monitoring. "
    "Search the web before answering. Beware of recirculated old news: check "
    "the DATE of every source; events from early 2026 (e.g. the March South "
    "Pars strike) recirculate as if fresh. Answer ONLY with JSON: "
    '{"answer":"YES"|"NO","confidence":"high"|"low",'
    '"justification":"one short sentence","source_url":"...",'
    '"source_date":"YYYY-MM-DD"}. '
    "Rule: answer YES only if confidence is high AND the supporting source is "
    "dated within the last 7 days. When in doubt, answer NO."
)


def ask_groq(question, api_key):
    body = json.dumps({
        "model": "groq/compound-mini",
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json", **UA},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    verdict = json.loads(m.group(0))
    assert verdict.get("answer") in ("YES", "NO")
    return verdict


SENTIMENT_SYSTEM = (
    "You are a contrarian market-psychology analyst. You cannot browse X or "
    "Reddit directly, so instead search the web for RECENT (last 7 days) "
    "dated coverage that MEASURES or DESCRIBES crypto retail sentiment: "
    "Fear & Greed index commentary, Santiment/social-volume analyses, "
    "articles about 'crypto sentiment', funding-rate and long/short "
    "positioning commentary, Google-Trends pieces, exchange-flow mood "
    "reports. Synthesize what they say about the retail crowd's mood. "
    "Classification guide: euphoria = greed/FOMO everywhere; capitulation = "
    "forced selling, despair stories; apathy = 'Bitcoin is dead', nobody "
    "talks about it. STRICT DATE RULE: evidence must be dated within the "
    "last 7 days; otherwise answer neutral with low confidence. "
    "Answer ONLY with JSON: "
    '{"sentiment":"euphoria"|"optimism"|"neutral"|"fear"|"capitulation"|"apathy",'
    '"evidence":"one sentence citing the coverage you found",'
    '"evidence_date":"YYYY-MM-DD","confidence":"high"|"low"}'
)

SENTIMENT_QUESTION = (
    "What is the prevailing retail sentiment about Bitcoin on social networks "
    "right now (this week)? Classify it."
)

SENTIMENT_LEVELS = ("euphoria", "optimism", "neutral", "fear",
                    "capitulation", "apathy")


def ask_sentiment(api_key):
    # compound (complet) : meilleure recherche web que mini pour ce scan
    body = json.dumps({
        "model": "groq/compound",
        "messages": [
            {"role": "system", "content": SENTIMENT_SYSTEM},
            {"role": "user", "content": SENTIMENT_QUESTION},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json", **UA},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    verdict = json.loads(m.group(0))
    assert verdict.get("sentiment") in SENTIMENT_LEVELS
    return verdict


def ask_groq_retry(fn, api_key, tries=3):
    import time
    for i in range(tries):
        try:
            return fn(api_key)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(20 * (i + 1))   # rate limit Groq : backoff 20 s / 40 s


def run_judge(prev_judge):
    import time
    api_key = os.environ.get("GROQ_API_KEY")
    judge = dict(prev_judge or {})
    if not api_key:
        judge["note"] = "GROQ_API_KEY absent — jugements inchangés"
        return judge
    for key, q in JUDGE_QUESTIONS.items():
        try:
            time.sleep(5)   # espacement entre questions (rate limit)
            v = ask_groq_retry(lambda k: ask_groq(q, k), api_key)
            # conservateur : un YES à confiance basse est rétrogradé en NO
            if v["answer"] == "YES" and v.get("confidence") != "high":
                v["answer"] = "NO"
                v["justification"] = "(YES basse confiance rétrogradé) " + \
                    v.get("justification", "")
            prev_answer = (prev_judge or {}).get(key, {}).get("answer", "NO")
            v["changed"] = v["answer"] != prev_answer
            judge[key] = v
        except Exception as e:  # échec → état inchangé
            old = (prev_judge or {}).get(key, {"answer": "NO"})
            old["error"] = f"{type(e).__name__}"
            judge[key] = old
    try:
        time.sleep(5)
        judge["sentiment"] = ask_groq_retry(ask_sentiment, api_key)
    except Exception as e:
        old = (prev_judge or {}).get("sentiment", {"sentiment": "neutral"})
        old["error"] = f"{type(e).__name__}"
        judge["sentiment"] = old
    judge["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return judge


# ---------------------------------------------------------------- score

def compute(prices, judge, prev):
    ind = {}
    alerts = []

    def state_of(score):
        return "ok" if score >= 75 else ("warn" if score >= 35 else "bad")

    # 1. Flux ETF (25 %)
    flows = prices.get("etf_flows_15d_musd")
    if flows is None:
        s = prev.get("indicators", {}).get("etf_flows", {}).get("score", 0)
        detail = "Farside indisponible — dernier état conservé"
    elif flows > 500:
        s, detail = 100, f"Entrées nettes 15 j : +{flows:.0f} M$"
    elif flows > -500:
        s, detail = 50, f"Flux 15 j proches de zéro : {flows:+.0f} M$"
    else:
        s, detail = 0, f"Sorties nettes 15 j : {flows:+.0f} M$"
    ind["etf_flows"] = {"label": "Flux nets des ETF spot", "weight": 20,
                        "score": s, "state": state_of(s), "detail": detail,
                        "flip": "Vert : 2-3 semaines d'entrées nettes cumulées"}

    # 2. Pivot Fed (25 %)
    fed = judge.get("fed_cut", {})
    if fed.get("answer") == "YES":
        s, detail = 100, "Baisse actée/quasi-certaine — " + fed.get("justification", "")
    else:
        s, detail = 50, "Taux tenus ; CPI en baisse → attentes pour les FOMC de sept/oct/déc"
    ind["fed_pivot"] = {"label": "Pivot Fed", "weight": 20, "score": s,
                        "state": state_of(s), "detail": detail,
                        "flip": "Vert : première baisse effective (juge LLM + FedWatch)"}

    # 3. Canal pétrole → inflation (15 %)
    ceasefire = judge.get("ceasefire", {}).get("answer") == "YES"
    b14 = prices.get("brent_14d_pct")
    if ceasefire:
        s, detail = 100, "Cessez-le-feu accepté — canal inflation refermé"
    elif b14 is None:
        s, detail = prev.get("indicators", {}).get("oil_channel", {}).get("score", 25), \
            "Brent indisponible — dernier état conservé"
    elif b14 > 5:
        s, detail = 0, f"Brent {prices.get('brent_usd')} $ ({b14:+.1f} % / 14 j) — pression inflation"
    elif b14 > -2:
        s, detail = 50, f"Brent {prices.get('brent_usd')} $ ({b14:+.1f} % / 14 j) — stabilisé"
    else:
        s, detail = 100, f"Brent {prices.get('brent_usd')} $ ({b14:+.1f} % / 14 j) — détente"
    ind["oil_channel"] = {"label": "Canal pétrole → inflation", "weight": 15,
                          "score": s, "state": state_of(s), "detail": detail,
                          "flip": "Vert : Brent stable/en baisse plusieurs semaines ou désescalade actée"}

    # 4. Horloge du cycle (15 %)
    days = (date.today() - CYCLE_TOP).days
    mid = sum(CYCLE_BOTTOM_DAYS) / 2
    s = min(100, round(days / mid * 100))
    in_window = CYCLE_BOTTOM_DAYS[0] <= days <= CYCLE_BOTTOM_DAYS[1]
    detail = f"J+{days} depuis le top (fenêtre 363-384 j : " + \
        ("EN COURS" if in_window else f"dans {CYCLE_BOTTOM_DAYS[0]-days} j" if days < CYCLE_BOTTOM_DAYS[0] else "dépassée") + ")"
    ind["cycle_clock"] = {"label": "Horloge mécanique du cycle", "weight": 15,
                          "score": s, "state": state_of(s), "detail": detail,
                          "flip": "Vert : entrée dans la fenêtre du 4-17 octobre 2026"}

    # 5. Capitulation minière (10 %)
    h30 = prices.get("hashrate_30d_pct")
    if h30 is None:
        s, detail = 50, "Hashrate indisponible — état neutre conservé"
    elif h30 < -8:
        s, detail = 0, f"Hashrate {h30:+.1f} % / 30 j — capitulation active"
    elif h30 < 2:
        s, detail = 50, f"Hashrate {h30:+.1f} % / 30 j — purge en cours de stabilisation"
    else:
        s, detail = 100, f"Hashrate {h30:+.1f} % / 30 j — reprise, capitulation derrière"
    ind["miners"] = {"label": "Capitulation des mineurs", "weight": 10,
                     "score": s, "state": state_of(s), "detail": detail,
                     "flip": "Vert : hashrate qui remonte après la purge"}

    # 6. Purge finale du levier (10 %)
    fng = prices.get("fear_greed")
    nvda = prices.get("nvda_drawdown_pct") or 0
    fund = prices.get("funding_rate_pct")
    if fng is not None and fng <= 15 and (fund or 0) < 0:
        s, detail = 100, f"Peur extrême (F&G {fng}) + funding négatif — purge en cours/réalisée"
    elif (fng is not None and fng <= 25) or nvda < -20:
        s, detail = 50, f"Stress partiel (F&G {fng}, NVDA {nvda:+.1f} % vs plus haut 3 m)"
    else:
        s, detail = 0, f"Pas de flush global (F&G {fng}, NVDA {nvda:+.1f} %, S&P {prices.get('spx_drawdown_pct', 0):+.1f} %)"
    ind["leverage_purge"] = {"label": "Purge finale du levier", "weight": 10,
                             "score": s, "state": state_of(s), "detail": detail,
                             "flip": "Vert : liquidation majeure réalisée et absorbée"}

    # 7. Sentiment réseaux (10 %) — contrarien : euphorie = loin du bottom,
    #    apathie ("BTC est mort") = signature classique du creux final
    SENTIMENT_SCORES = {"euphoria": 0, "optimism": 25, "neutral": 40,
                        "fear": 60, "capitulation": 90, "apathy": 100}
    sen = judge.get("sentiment", {})
    level = sen.get("sentiment", "neutral")
    stale = False
    ed = sen.get("evidence_date")
    if ed:
        try:
            from datetime import timedelta
            stale = (date.today() - date.fromisoformat(ed)) > timedelta(days=7)
        except ValueError:
            stale = True
    # conservateur : confiance basse ou evidence périmée → neutre
    if sen.get("confidence") != "high" or stale:
        level = "neutral"
    s = SENTIMENT_SCORES.get(level, 40)
    labels_fr = {"euphoria": "euphorie", "optimism": "optimisme",
                 "neutral": "neutre", "fear": "peur",
                 "capitulation": "capitulation", "apathy": "apathie"}
    detail = f"Réseaux : {labels_fr.get(level, level)}" + \
        (f" — {sen['evidence']}" if sen.get("evidence") else "") + \
        (" (échec du dernier scan, état conservé)" if sen.get("error") else "")
    ind["sentiment"] = {"label": "Sentiment réseaux (contrarien)", "weight": 10,
                        "score": s, "state": state_of(s), "detail": detail,
                        "flip": "Vert : capitulation ou apathie généralisée ('BTC est mort')"}

    score = round(sum(i["score"] * i["weight"] for i in ind.values()) / 100)

    # alertes
    for key, label in (("ceasefire", "Cessez-le-feu Iran accepté"),
                       ("gulf_infra", "Infra énergétique majeure du Golfe touchée"),
                       ("fed_cut", "Baisse de taux Fed")):
        v = judge.get(key, {})
        if v.get("answer") == "YES":
            alerts.append({"key": key, "label": label,
                           "changed": bool(v.get("changed")),
                           "justification": v.get("justification", ""),
                           "source": v.get("source_url", "")})
    if nvda < -15:
        alerts.append({"key": "nvda", "label": f"NVDA {nvda:+.1f} % vs plus haut — contagion IA possible",
                       "changed": False, "justification": "", "source": ""})
    return ind, score, alerts


def main():
    prev = load_previous()
    prices = dict(prev.get("prices", {}))
    for fn in (fetch_btc, fetch_markets, fetch_fng, fetch_funding,
               fetch_hashrate, fetch_etf_flows):
        try:
            fn(prices)
        except Exception as e:
            print(f"[warn] {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr)

    judge = run_judge(prev.get("judge"))
    indicators, score, alerts = compute(prices, judge, prev)

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "prices": prices,
        "judge": judge,
        "indicators": indicators,
        "score": score,
        "alerts": alerts,
    }
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"score={score} | " + " ".join(f"{k}:{v['score']}" for k, v in indicators.items()))


if __name__ == "__main__":
    main()
