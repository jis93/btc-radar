"""Radar BTC — signaux d'achat « sur le moment » (exécuté toutes les 2 h).

6 signaux observables en temps réel (ou à J-1 / J-7 quand précisé) + 1
avertissement. Écrit signals.json et envoie un email UNIQUEMENT quand un
signal passe de OFF à ON (anti-spam : pas de nouvelle alerte pour le même
signal avant 24 h).

Sources choisies pour fonctionner depuis les runners GitHub (US) : Binance
renvoie 451 aux IP américaines, donc OKX / Coinbase / Hyperliquid.
Stdlib uniquement.
"""
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import http_json, http_text, yahoo_daily, re  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "signals.json")
NOW = datetime.now(timezone.utc)

# ------------------------------------------------------------- seuils
FUNDING_PURGE = -0.01      # % par 8 h (OKX) — funding nettement négatif
OI_PURGE_PCT = -8.0        # variation des positions ouvertes sur 1 jour
PRICE_PURGE_PCT = -6.0     # alternative au funding : chute du prix 24 h
STH_MVRV_MAX = 0.80        # prix / prix de revient des détenteurs récents
SOX_SHOCK = -3.0           # séance des semis jugée « mauvaise nouvelle »
NVDA_SHOCK = -5.0
CANDLE_RANGE = 6.0         # amplitude min de la bougie (% de l'ouverture)
CANDLE_WICK = 0.5          # mèche basse ≥ 50 % de l'amplitude
CANDLE_VOL = 1.8           # volume ≥ 1,8 × moyenne 30 j
CB_PREMIUM = 0.08          # prime Coinbase en %
ETF_STREAK = 5             # jours de sorties consécutifs avant 1re entrée
REALERT_H = 24


def load_prev():
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print(f"[warn] {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------- collecte
def get_spot():
    cb = float(http_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    usdt = float(http_json("https://api.exchange.coinbase.com/products/USDT-USD/ticker")["price"])
    okx = http_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")["data"][0]
    okx_usd = float(okx["last"]) * usdt
    chg24 = (float(okx["last"]) / float(okx["open24h"]) - 1) * 100
    return {"price": round(cb), "chg24": round(chg24, 2),
            "cb_premium": round((cb / okx_usd - 1) * 100, 3)}


def get_funding():
    d = http_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP")
    return round(float(d["data"][0]["fundingRate"]) * 100, 4)


def get_oi_change():
    """Positions ouvertes OKX (USD), variation du dernier jour complet + jour en cours."""
    rows = http_json("https://www.okx.com/api/v5/rubik/stat/contracts/"
                     "open-interest-volume?ccy=BTC&period=1D")["data"]
    oi = [float(r[1]) for r in rows]          # du plus récent au plus ancien
    return round((oi[0] / oi[1] - 1) * 100, 1)


def get_realized():
    """bitcoin-data.com (gratuit = valeurs à J-7)."""
    r = http_json("https://bitcoin-data.com/v1/realized-price/last")
    s = http_json("https://bitcoin-data.com/v1/sth-realized-price/last")
    return {"realized": round(float(r["realizedPrice"])),
            "sth_realized": round(float(s["sthRealizedPrice"])),
            "asof": r.get("d")}


def get_candle():
    """Dernière bougie journalière COMPLÈTE Coinbase : [t, low, high, open, close, vol]."""
    c = http_json("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400")
    t, lo, hi, op, cl, vol = c[1]
    avg = sum(x[5] for x in c[2:32]) / len(c[2:32])
    rng = hi - lo
    return {"date": datetime.fromtimestamp(t, timezone.utc).date().isoformat(),
            "range_pct": round(rng / op * 100, 1),
            "wick": round((min(op, cl) - lo) / rng, 2) if rng else 0,
            "close_pos": round((cl - lo) / rng, 2) if rng else 0,
            "vol_x": round(vol / avg, 2), "low": round(lo), "close": round(cl)}


def day_change(symbol):
    """(date de la dernière séance, variation %) — séance en cours incluse."""
    s = yahoo_daily(symbol, rng="5d")
    k = sorted(s)
    return k[-1], round((s[k[-1]] / s[k[-2]] - 1) * 100, 2)


def btc_change_on(day):
    """Variation de BTC sur la même journée UTC que la séance actions."""
    s = yahoo_daily("BTC-USD", rng="1mo")
    k = sorted(s)
    if day not in s or k.index(day) == 0:
        return None
    i = k.index(day)
    return round((s[k[i]] / s[k[i - 1]] - 1) * 100, 2)


def get_etf():
    html = http_text("https://farside.co.uk/btc/")
    daily = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(cells) >= 2 and re.match(r"\d{1,2} \w{3} \d{4}", cells[0]):
            raw = cells[-1].replace(",", "").replace("(", "-").replace(")", "")
            try:
                daily.append((cells[0], float(raw)))
            except ValueError:
                pass
    return daily


# ------------------------------------------------------------- signaux
def compute(prev):
    spot = safe(get_spot) or {}
    price = spot.get("price") or prev.get("price")
    funding = safe(get_funding)
    oi = safe(get_oi_change)
    real = safe(get_realized) or prev.get("realized_cache") or {}
    candle = safe(get_candle)
    sox = safe(day_change, "^SOX")
    nvda = safe(day_change, "NVDA")
    etf = safe(get_etf)
    sig = {}

    def put(key, label, on, detail, how, lag="temps réel"):
        sig[key] = {"label": label, "on": bool(on), "detail": detail,
                    "rule": how, "lag": lag}

    # 1. Prix sous le prix réalisé (MVRV < 1)
    rp = real.get("realized")
    put("realized", "Prix sous le prix réalisé (MVRV < 1)",
        price and rp and price < rp,
        f"Prix {price:,} $ vs prix réalisé {rp:,} $ (MVRV {price / rp:.2f})" if price and rp else "Donnée indisponible",
        "Tout le marché est en perte en moyenne — signature des bottoms de cycle",
        lag=f"prix réalisé au {real.get('asof', '?')} (J-7, gratuit)")

    # 2. Détenteurs récents en perte profonde
    sth = real.get("sth_realized")
    put("sth", f"Détenteurs récents en perte > {round((1 - STH_MVRV_MAX) * 100)} %",
        price and sth and price / sth < STH_MVRV_MAX,
        f"Prix / revient des < 5 mois ({sth:,} $) = {price / sth:.2f}" if price and sth else "Donnée indisponible",
        f"Ratio < {STH_MVRV_MAX} : ventes paniques des acheteurs récents",
        lag=f"revient au {real.get('asof', '?')} (J-7)")

    # 3. Journée de purge du levier
    chg = spot.get("chg24")
    purge = oi is not None and oi <= OI_PURGE_PCT and (
        (funding is not None and funding <= FUNDING_PURGE) or (chg is not None and chg <= PRICE_PURGE_PCT))
    put("purge", "Journée de purge du levier", purge,
        f"Positions ouvertes {oi:+.1f} % / 1 j · funding {funding:+.4f} % · prix {chg:+.1f} % / 24 h"
        if None not in (oi, funding, chg) else "Donnée indisponible",
        f"Positions ouvertes ≤ {OI_PURGE_PCT:.0f} % ET (funding ≤ {FUNDING_PURGE} % ou prix ≤ {PRICE_PURGE_PCT:.0f} %) — ventes forcées")

    # 4. Résilience aux mauvaises nouvelles
    day, sox = sox if sox else (None, None)
    nvda = nvda[1] if nvda else None
    btc_day = safe(btc_change_on, day) if day else None
    shock = (sox is not None and sox <= SOX_SHOCK) or (nvda is not None and nvda <= NVDA_SHOCK)
    put("resilience", "BTC résiste à une mauvaise séance tech",
        shock and btc_day is not None and btc_day >= 0,
        f"Séance du {day} : semis {sox:+.1f} % · NVDA {nvda:+.1f} % · BTC même jour {btc_day:+.1f} %"
        if None not in (sox, nvda, btc_day) else "Donnée indisponible",
        f"Semis ≤ {SOX_SHOCK:.0f} % ou NVDA ≤ {NVDA_SHOCK:.0f} % sur la séance, et BTC ≥ 0 le même jour — vendeurs épuisés")

    # 5. Bougie de capitulation (dernière journée complète)
    cap = candle and candle["range_pct"] >= CANDLE_RANGE and candle["wick"] >= CANDLE_WICK \
        and candle["close_pos"] >= 0.5 and candle["vol_x"] >= CANDLE_VOL
    put("candle", "Bougie de capitulation", cap,
        (f"{candle['date']} : amplitude {candle['range_pct']} %, mèche basse {int(candle['wick'] * 100)} %, "
         f"volume ×{candle['vol_x']} (plus-bas {candle['low']:,} $ → clôture {candle['close']:,} $)")
        if candle else "Donnée indisponible",
        f"Amplitude ≥ {CANDLE_RANGE:.0f} %, mèche basse ≥ {int(CANDLE_WICK * 100)} %, clôture dans la moitié haute, volume ≥ ×{CANDLE_VOL}",
        lag="clôture journalière (UTC)")

    # 6. Retour des acheteurs au comptant
    prem = spot.get("cb_premium")
    etf_first = False
    etf_txt = ""
    if etf and len(etf) > ETF_STREAK:
        last_d, last_v = etf[-1]
        streak = 0
        for _, v in reversed(etf[:-1]):
            if v < 0:
                streak += 1
            else:
                break
        etf_first = last_v > 0 and streak >= ETF_STREAK
        etf_txt = f" · ETF {last_d} : {last_v:+.0f} M$ (après {streak} j de sorties)"
    put("spot", "Retour des acheteurs au comptant",
        (prem is not None and prem >= CB_PREMIUM) or etf_first,
        (f"Prime Coinbase {prem:+.3f} %" if prem is not None else "Prime indisponible") + etf_txt,
        f"Prime Coinbase ≥ +{CB_PREMIUM} % ou 1re entrée ETF après ≥ {ETF_STREAK} j de sorties",
        lag="prime : temps réel · ETF : J-1")

    # Avertissement : hausse portée par le levier
    warn = oi is not None and oi >= 5 and funding is not None and funding >= 0.01 and chg is not None and chg >= 3
    warning = {"label": "Hausse portée par le levier (ne pas courir après)", "on": bool(warn),
               "detail": f"Positions ouvertes {oi:+.1f} % · funding {funding:+.4f} % · prix {chg:+.1f} %"
               if None not in (oi, funding, chg) else "Donnée indisponible",
               "rule": "Positions ouvertes ≥ +5 %, funding ≥ +0,01 %, prix ≥ +3 % sur 24 h"}
    return price, sig, warning, real


# ------------------------------------------------------------- email
def send_alert(new, sig, warning, price):
    addr, pwd = os.environ.get("MAIL_ADDRESS"), os.environ.get("MAIL_APP_PASSWORD")
    if not addr or not pwd:
        print("MAIL_ADDRESS / MAIL_APP_PASSWORD absents — alerte non envoyée")
        return
    on = [k for k, v in sig.items() if v["on"]]
    lines = [f"BTC {price:,} $ — {len(on)}/6 signaux actifs", ""]
    lines.append("NOUVEAU :")
    for k in new:
        lines += [f"  🟢 {sig[k]['label']}", f"     {sig[k]['detail']}", f"     ({sig[k]['rule']})"]
    lines += ["", "TOUS LES SIGNAUX :"]
    for v in sig.values():
        lines.append(f"  {'🟢' if v['on'] else '⚪'} {v['label']} — {v['detail']}")
    lines.append(f"  {'⚠️' if warning['on'] else '·'} {warning['label']} — {warning['detail']}")
    lines += ["", "Page : https://jis93.github.io/btc-radar/",
              "Un signal isolé ment : la convergence compte. Heuristique de veille, pas un conseil."]
    msg = MIMEText("\n".join(lines), "plain", "utf-8")
    msg["Subject"] = f"🟢 Signal BTC — {', '.join(sig[k]['label'] for k in new)} ({price:,} $)"
    msg["From"] = msg["To"] = addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pwd)
        s.send_message(msg)
    print(f"alerte envoyée : {new}")


def main():
    prev = load_prev()
    price, sig, warning, real = compute(prev)
    last_alert = dict(prev.get("last_alert", {}))
    prev_on = {k for k, v in prev.get("signals", {}).items() if v.get("on")}
    new = []
    for k, v in sig.items():
        if v["on"] and k not in prev_on:
            t = last_alert.get(k)
            if not t or NOW - datetime.fromisoformat(t) > timedelta(hours=REALERT_H):
                new.append(k)
    log = prev.get("log", [])
    for k in new:
        last_alert[k] = NOW.isoformat(timespec="minutes")
        log.append({"t": last_alert[k], "key": k, "label": sig[k]["label"],
                    "price": price, "detail": sig[k]["detail"]})
    out = {"updated_at": NOW.isoformat(timespec="minutes"), "price": price,
           "signals": sig, "warning": warning, "active": sum(v["on"] for v in sig.values()),
           "last_alert": last_alert, "log": log[-50:],
           "realized_cache": real if real.get("realized") else prev.get("realized_cache")}
    with open(PATH, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{out['active']}/6 actifs | nouveaux: {new or '-'} | " +
          " ".join(f"{k}:{'ON' if v['on'] else 'off'}" for k, v in sig.items()))
    if new:
        try:
            send_alert(new, sig, warning, price)
        except Exception as e:
            print(f"[warn] email : {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
