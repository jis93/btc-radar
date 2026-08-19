#!/usr/bin/env python3
"""Radar Bottom BTC — envoi du résumé quotidien par email (SMTP Gmail).

Secrets requis : MAIL_ADDRESS (expéditeur = destinataire),
MAIL_APP_PASSWORD (mot de passe d'application Google, 16 caractères).
Sans eux, le script sort en 0 sans rien faire (l'email est optionnel,
il ne doit jamais faire échouer le workflow).
"""
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_ICON = {"ok": "🟢", "warn": "🟠", "bad": "🔴"}
ORDER = ["etf_flows", "fed_pivot", "oil_channel", "cycle_clock",
         "miners", "leverage_purge", "sentiment"]
JUDGE_LABELS = {
    "ceasefire": "Cessez-le-feu accepté par l'Iran",
    "gulf_infra": "Infra énergétique majeure du Golfe touchée (7 j)",
    "fed_cut": "Baisse de taux Fed actée / quasi-certaine",
}


def build_body(d):
    p = d.get("prices", {})
    lines = []
    filled = round((d.get("score") or 0) / 5)
    lines.append(f"PROXIMITÉ DU BOTTOM : {d.get('score', '?')}/100")
    lines.append("[" + "█" * filled + "░" * (20 - filled) + "]")
    lines.append("")

    alerts = d.get("alerts") or []
    if alerts:
        lines.append("⚠️  ALERTES — À VÉRIFIER SOI-MÊME :")
        for a in alerts:
            lines.append(f"  • {a['label']}"
                         + (" [CHANGEMENT DÉTECTÉ]" if a.get("changed") else ""))
            if a.get("justification"):
                lines.append(f"    {a['justification']}")
            if a.get("source"):
                lines.append(f"    {a['source']}")
        lines.append("")

    lines.append("MARCHÉ :")
    btc = p.get("btc_usd")
    lines.append(f"  BTC {('$' + format(btc, ',')) if btc else '—'}"
                 f" ({p.get('btc_24h_pct', 0):+.1f} % / 24 h)"
                 f"  ·  Brent ${p.get('brent_usd', '—')}"
                 f" ({p.get('brent_14d_pct', 0):+.1f} % / 14 j)")
    lines.append(f"  Fear&Greed {p.get('fear_greed', '—')}"
                 f"  ·  Funding {p.get('funding_rate_pct', '—')} %"
                 f"  ·  Hashrate 30 j {p.get('hashrate_30d_pct', 0):+.1f} %"
                 f"  ·  ETF 15 j {p.get('etf_flows_15d_musd', 0):+.0f} M$")
    lines.append("")

    lines.append("INDICATEURS :")
    for k in ORDER:
        ind = d.get("indicators", {}).get(k)
        if ind:
            lines.append(f"  {STATE_ICON.get(ind['state'], '·')} "
                         f"{ind['label']} ({ind['weight']} %) — {ind['detail']}")
    lines.append("")

    lines.append("JUGE LLM (conservateur, OUI = confiance haute + source <7 j) :")
    for k, label in JUDGE_LABELS.items():
        v = d.get("judge", {}).get(k, {})
        ans = "OUI" if v.get("answer") == "YES" else "NON"
        just = v.get("justification", v.get("error", ""))
        lines.append(f"  [{ans}] {label} — {just}")
    lines.append("")
    lines.append("Page : https://jis93.github.io/btc-radar/")
    lines.append("Rappel : convergence des signaux > signal isolé. "
                 "Heuristique de veille, pas un conseil.")
    return "\n".join(lines)


def main():
    addr = os.environ.get("MAIL_ADDRESS")
    pwd = os.environ.get("MAIL_APP_PASSWORD")
    if not addr or not pwd:
        print("MAIL_ADDRESS / MAIL_APP_PASSWORD absents — email non envoyé")
        return
    with open(os.path.join(ROOT, "data.json")) as f:
        d = json.load(f)

    day = datetime.now().strftime("%d/%m")
    n_alerts = len(d.get("alerts") or [])
    subject = (f"🎯 Radar BTC {day} — {d.get('score', '?')}/100"
               + (f" — ⚠️ {n_alerts} alerte(s)" if n_alerts else ""))

    msg = MIMEText(build_body(d), "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pwd)
        s.send_message(msg)
    print(f"email envoyé à {addr}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # l'email ne doit jamais casser le workflow
        print(f"[warn] envoi email échoué : {type(e).__name__}: {e}",
              file=sys.stderr)
