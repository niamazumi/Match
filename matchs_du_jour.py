#!/usr/bin/env python3
"""
Matchs du jour = matchs proposés par WinnerBet + statistiques SoccerSTATS.com
"""

import difflib
import os
import re
import smtplib
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from html.parser import HTMLParser

# ====================== RÉGLAGES (modifiables) ======================
SEUIL_PLUS_1_5 = 0.75   # conditions 1, 2 et 3
SEUIL_BTTS = 0.75       # "les deux équipes marquent" : taux minimum
MIN_MATCHS = 5          # nombre minimal de matchs joués pour fiabiliser les taux
JOUR = 1                # 1 = aujourd'hui, 2 = demain (numérotation SoccerSTATS)
URL_WINNER = "https://winner.bet/fr/sports"   # à corriger si l'adresse diffère

# Envoi automatique (laissez vide ce que vous n'utilisez pas)
EMAIL_ENVOI = os.environ.get("EMAIL_ENVOI", "")
EMAIL_MOT_DE_PASSE = os.environ.get("EMAIL_MOT_DE_PASSE", "")
EMAIL_DESTINATAIRE = os.environ.get("EMAIL_DESTINATAIRE", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PAGE_URL = os.environ.get("PAGE_URL", "")   # adresse de votre page web (GitHub Pages)

# Noms différents entre SoccerSTATS et WinnerBet : "nom SoccerSTATS": "nom WinnerBet"
ALIAS = {
    "Paris SG": "Paris Saint-Germain",
    "Man Utd": "Manchester United",
    "Man City": "Manchester City",
    "Inter": "Internazionale",
}
# ====================================================================

URL_DOM_EXT = f"https://www.soccerstats.com/matches.asp?matchday={JOUR}"
URL_TOUS = f"https://www.soccerstats.com/matches.asp?matchday={100 + JOUR}"
DOSSIER = os.path.dirname(os.path.abspath(__file__))


# ---------------------------- SoccerSTATS ----------------------------
class LecteurTableaux(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lignes, self.cellule, self.dans_cellule = [[]], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.lignes.append([])
        elif tag in ("td", "th"):
            self.dans_cellule, self.cellule = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.dans_cellule:
            self.lignes[-1].append(" ".join("".join(self.cellule).split()))
            self.dans_cellule = False

    def handle_data(self, data):
        if self.dans_cellule:
            self.cellule.append(data)


def telecharger(url):
    """Ouvre la page avec un vrai navigateur (le site refuse les programmes simples)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=True)
        ctx = nav.new_context(
            locale="en-US", viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"))
        page = ctx.new_page()
        rep = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        html = page.content()
        statut = rep.status if rep else 0
        nav.close()
    if statut >= 400:
        raise RuntimeError(f"SoccerSTATS a refusé l'accès (code {statut}) : {url}")
    return html


def pct(texte):
    m = re.fullmatch(r"(\d+)%", texte.strip())
    return int(m.group(1)) / 100 if m else None


def extraire_equipes(html):
    p = LecteurTableaux()
    p.feed(html)
    equipes, ligue = [], ""
    for cellules in p.lignes:
        m = re.search(r"(\S.*?)\s+stats\s+Matches played", " ".join(cellules))
        if m:
            ligue = m.group(1).strip()
        for i, c in enumerate(cellules):
            if c in ("home", "away"):
                stats = cellules[i + 1:]
                if len(stats) < 11 or not stats[0].isdigit():
                    break
                avant = " ".join(cellules[:i])
                heure = re.search(r"\d\d:\d\d", avant)
                nom = re.sub(r"\d\d:\d\d", "", avant).replace("stats", "").strip()
                equipes.append({
                    "ligue": ligue, "nom": nom, "role": c,
                    "heure": heure.group(0) if heure else "",
                    "gp": int(stats[0]), "bts": pct(stats[4]), "p15": pct(stats[8]),
                })
                break
    return equipes


def paires(equipes):
    return [(equipes[i], equipes[i + 1]) for i in range(len(equipes) - 1)
            if equipes[i]["role"] == "home" and equipes[i + 1]["role"] == "away"]


def candidats_soccerstats():
    dom_ext = paires(extraire_equipes(telecharger(URL_DOM_EXT)))
    tous = {(h["nom"], a["nom"]): (h, a)
            for h, a in paires(extraire_equipes(telecharger(URL_TOUS)))}
    retenus = []
    for h, a in dom_ext:
        if (h["nom"], a["nom"]) not in tous:
            continue
        ht, at = tous[(h["nom"], a["nom"])]
        v = [h["p15"], a["p15"], ht["p15"], at["p15"], ht["bts"], at["bts"]]
        if None in v or min(h["gp"], a["gp"], ht["gp"], at["gp"]) < MIN_MATCHS:
            continue
        if (h["p15"] >= SEUIL_PLUS_1_5 and a["p15"] >= SEUIL_PLUS_1_5
                and ht["p15"] >= SEUIL_PLUS_1_5 and at["p15"] >= SEUIL_PLUS_1_5
                and ht["bts"] >= SEUIL_BTTS and at["bts"] >= SEUIL_BTTS):
            retenus.append({
                "ligue": h["ligue"], "heure": h["heure"],
                "domicile": h["nom"], "exterieur": a["nom"],
                "c1": h["p15"], "c2": a["p15"], "c3h": ht["p15"], "c3a": at["p15"],
                "btts_h": ht["bts"], "btts_a": at["bts"],
            })
    return retenus


# ----------------------------- WinnerBet -----------------------------
def lire_winner():
    """Ouvre WinnerBet, fait défiler la liste et renvoie toutes les lignes de texte vues."""
    from playwright.sync_api import sync_playwright
    lignes, vues = [], set()
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=True)
        ctx = nav.new_context(
            locale="fr-FR", viewport={"width": 412, "height": 915},
            user_agent=("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"))
        page = ctx.new_page()
        page.goto(URL_WINNER, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        sans_nouveau = 0
        for _ in range(150):
            texte = page.inner_text("body")
            nouveau = False
            for l in texte.splitlines():
                l = l.strip()
                if l and l not in vues:
                    vues.add(l)
                    lignes.append(l)
                    nouveau = True
            sans_nouveau = 0 if nouveau else sans_nouveau + 1
            if sans_nouveau >= 6:
                break
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(700)
        nav.close()
    with open(os.path.join(DOSSIER, "winner_page.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lignes))
    return lignes


STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ud", "sv", "fk", "de", "la", "el", "ii"}


def norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def jetons(s):
    return [t for t in norm(s).split() if t not in STOP]


def ressemble(nom, ligne):
    nom = ALIAS.get(nom, nom)
    n, l = norm(nom), norm(ligne)
    if len(l) < 4 or not n:
        return False
    if n in l or l in n:
        return True
    if difflib.SequenceMatcher(None, n, l).ratio() >= 0.85:
        return True
    sig = [t for t in jetons(nom) if len(t) >= 4]
    ltok = jetons(ligne)
    return bool(sig) and all(any(x == t or x.startswith(t) or t.startswith(x) and len(x) >= 4
                                 for x in ltok) for t in sig)


def existe_chez_winner(domicile, exterieur, lignes):
    ih = [i for i, l in enumerate(lignes) if ressemble(domicile, l)]
    ia = [i for i, l in enumerate(lignes) if ressemble(exterieur, l)]
    return any(abs(i - j) <= 8 for i in ih for j in ia)


# ------------------------------ Sortie ------------------------------
def p(x):
    return f"{x * 100:.0f} %"


def page_html(resultats, jour):
    lignes = "".join(
        f"<tr><td>{escape(r['ligue'])}</td><td>{r['heure']}</td>"
        f"<td><b>{escape(r['domicile'])}</b> - <b>{escape(r['exterieur'])}</b></td>"
        f"<td>{p(r['c1'])}</td><td>{p(r['c2'])}</td>"
        f"<td>{p(r['c3h'])} / {p(r['c3a'])}</td>"
        f"<td>{p(r['btts_h'])} / {p(r['btts_a'])}</td></tr>"
        for r in resultats
    ) or "<tr><td colspan='7'>Aucun match ne remplit toutes les conditions aujourd'hui.</td></tr>"
    return f"""<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="robots" content="noindex"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Matchs du jour {jour}</title>
<style>body{{font-family:sans-serif;margin:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;font-size:14px}}th{{background:#eee}}</style>
<h2>Matchs retenus (disponibles chez WinnerBet) - {jour}</h2>
<table><tr><th>Ligue</th><th>Heure (site)</th><th>Match</th><th>Dom. +1,5</th><th>Ext. +1,5</th>
<th>Tous matchs +1,5 (dom / ext)</th><th>Les deux marquent (dom / ext)</th></tr>{lignes}</table>
<p>Statistiques SoccerSTATS.com. Ce ne sont pas des garanties de gain. Jouez avec modération.</p></html>"""


def message_texte(resultats, jour):
    lien = f"\n\nPage web : {PAGE_URL}" if PAGE_URL else ""
    if not resultats:
        return f"Matchs du {jour} : aucun match ne remplit toutes les conditions." + lien
    l = [f"Matchs retenus du {jour} (disponibles chez WinnerBet) :"]
    for r in resultats:
        l.append(f"- {r['heure']} {r['domicile']} - {r['exterieur']} ({r['ligue']})")
    return "\n".join(l) + lien


def envoyer(texte, jour, page, chemin):
    if EMAIL_ENVOI and EMAIL_MOT_DE_PASSE:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"Matchs du jour {jour}"
            msg["From"] = EMAIL_ENVOI
            msg["To"] = EMAIL_DESTINATAIRE or EMAIL_ENVOI
            msg.set_content(texte)
            msg.add_alternative(page, subtype="html")  # lisible directement dans Chrome / Gmail
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(EMAIL_ENVOI, EMAIL_MOT_DE_PASSE)
                s.send_message(msg)
            print("E-mail envoyé.")
        except Exception as e:
            print("Échec de l'e-mail :", e)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": texte}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data, timeout=30)
            print("Message Telegram envoyé.")
        except Exception as e:
            print("Échec Telegram :", e)


def lancer():
    jour = (datetime.utcnow() + timedelta(hours=1)).date().isoformat()  # date de Kinshasa (UTC+1)
    print("1/3 Lecture des matchs WinnerBet...")
    lignes_winner = lire_winner()
    print(f"    {len(lignes_winner)} lignes lues (copie dans winner_page.txt)")
    if len(lignes_winner) < 30:
        print("    ATTENTION : très peu de texte lu, WinnerBet bloque peut-être la lecture.")
    print("2/3 Statistiques SoccerSTATS...")
    candidats = candidats_soccerstats()
    print(f"    {len(candidats)} match(s) respectent les conditions statistiques")
    print("3/3 Vérification : le match existe chez WinnerBet ?")
    resultats = [r for r in candidats
                 if existe_chez_winner(r["domicile"], r["exterieur"], lignes_winner)]
    resultats.sort(key=lambda r: r["heure"])
    texte = message_texte(resultats, jour)
    print("\n" + texte)
    chemin = os.path.join(DOSSIER, "matchs_du_jour.html")
    page = page_html(resultats, jour)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(page)
    envoyer(texte, jour, page, chemin)
    if not os.environ.get("GITHUB_ACTIONS"):
        webbrowser.open("file://" + chemin)


def main():
    if "--winner-seulement" in sys.argv:
        l = lire_winner()
        print(f"{len(l)} lignes lues. Ouvrez winner_page.txt pour vérifier qu'on y voit les matchs.")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--quotidien":
        h, m = map(int, sys.argv[2].split(":"))
        while True:
            now = datetime.now()
            prochain = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if prochain <= now:
                prochain += timedelta(days=1)
            print(f"Prochain lancement automatique : {prochain:%d/%m %H:%M}")
            time.sleep((prochain - datetime.now()).total_seconds())
            try:
                lancer()
            except Exception as e:
                print("Erreur :", e)
    else:
        lancer()


if __name__ == "__main__":
    main()
