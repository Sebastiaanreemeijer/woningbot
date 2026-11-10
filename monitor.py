"""
Woningbot v2 - stuurt een melding naar je telefoon/laptop (via ntfy) zodra er
een nieuwe woning verschijnt op een zoekpagina van een verhuursite.

Je hoeft in dit bestand niets aan te passen.
- De zoekpagina's staan in sites.txt
- Je ntfy-kanaal staat als 'secret' NTFY_TOPIC in GitHub

Ophalen gaat in twee stappen:
1. Snel: doet zich voor als een gewone Chrome-browser (curl_cffi)
2. Lukt dat niet: opent de pagina in een echte (onzichtbare) Chrome-browser
   die ook JavaScript uitvoert (Playwright)
"""

import json
import os
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = "seen.json"
SITES_FILE = "sites.txt"
MAX_MELDINGEN_PER_KEER = 8    # meer nieuwe woningen tegelijk? dan 1 samenvatting
FOUTEN_VOOR_WAARSCHUWING = 3  # na zoveel mislukte pogingen op rij: 1 waarschuwing

# Hoe een link naar een woning eruitziet, per website
PATRONEN = {
    "pararius.nl": r"/(appartement|huis|studio|kamer|woning)-te-huur/[^/]+/[0-9a-f]{8}(/|$)",
    "huurwoningen.nl": r"/huren/[^/]+/[0-9a-f]{8}(/|$)",
    "huurwoningen.com": r"/huren/[^/]+/[0-9a-f]{8}(/|$)",
    "funda.nl": r"/detail/huur/.+/\d{6,}",
    "kamernet.nl": r"/huren/[^/]+/[^/]+/[a-z]+-\d{5,}",
    "directwonen.nl": r"/huurwoningen-huren/[^/]+/[^/]+/[a-z-]+-\d{5,}",
}
# Voor andere sites (bijv. een lokale makelaar): links met een woning-ID erin
ALGEMEEN = r"/[^?#]*(\d{5,}|[0-9a-f]{8})"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLOKKADE_TEKSTEN = ("just a moment", "attention required", "access denied",
                    "verify you are human", "captcha", "cf-chl")

_browser = None
_pw = None


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def domein(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def schoon(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))


# ---------- ophalen ----------

def haal_snel(url):
    from curl_cffi import requests as creq
    r = creq.get(url, impersonate="chrome", timeout=30,
                 headers={"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def haal_met_browser(url):
    global _browser, _pw
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
    ctx = _browser.new_context(user_agent=UA, locale="nl-NL",
                               timezone_id="Europe/Amsterdam",
                               viewport={"width": 1366, "height": 900})
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Beveiligingscheck ("Just a moment...")? Even wachten tot die klaar is.
        for _ in range(4):
            if not any(t in page.title().lower() for t in BLOKKADE_TEKSTEN):
                break
            page.wait_for_timeout(5000)
        page.wait_for_timeout(2000)
        html = page.content()
        status = resp.status if resp else 0
        if any(t in page.title().lower() for t in BLOKKADE_TEKSTEN):
            raise RuntimeError(f"beveiligingscheck van de site (HTTP {status})")
        return html
    finally:
        ctx.close()


def sluit_browser():
    global _browser, _pw
    try:
        if _browser:
            _browser.close()
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _browser = _pw = None


# ---------- woningen herkennen ----------

def woning_links(zoek_url, html):
    d = domein(zoek_url)
    patroon = next((p for site, p in PATRONEN.items() if d.endswith(site)), None)
    zoek_pad = urlparse(zoek_url).path.rstrip("/")
    parser = LinkParser()
    parser.feed(html)
    gevonden = []
    for href in parser.links:
        volledig = urljoin(zoek_url, href)
        # Woningen achter een betaalmuur: de echte link zit in 'returnUrl'
        terug = parse_qs(urlparse(volledig).query).get("returnUrl")
        if terug:
            volledig = urljoin(zoek_url, terug[0])
        if domein(volledig) != d:
            continue
        pad = urlparse(volledig).path
        if patroon:
            ok = re.search(patroon, pad)
        else:
            # onbekende site: link met een ID, of een pagina 'onder' de zoekpagina
            # (bijv. /aanbod -> /aanbod/mooie-woning-zwolle)
            dieper = zoek_pad and pad.startswith(zoek_pad + "/") and len(pad) > len(zoek_pad) + 3
            ok = dieper or re.search(ALGEMEEN, pad)
        if ok:
            s = schoon(volledig)
            if s != schoon(zoek_url) and s not in gevonden:
                gevonden.append(s)
    return gevonden


def haal_woningen(zoek_url):
    """Probeert eerst snel, daarna met een echte browser. Geeft (links, methode)."""
    redenen = []
    try:
        links = woning_links(zoek_url, haal_snel(zoek_url))
        if links:
            return links, "snel"
        redenen.append("snel: geen woningen gevonden")
    except Exception as e:
        redenen.append(f"snel: {e}")
    try:
        links = woning_links(zoek_url, haal_met_browser(zoek_url))
        if links:
            return links, "browser"
        redenen.append("browser: geen woningen gevonden")
    except Exception as e:
        redenen.append(f"browser: {str(e).splitlines()[0][:120]}")
    raise RuntimeError(" | ".join(redenen))


def omschrijving(url):
    """Maakt van de link een leesbare tekst, bijv. 'zwolle · vijfhoek'."""
    delen = [p for p in urlparse(url).path.split("/") if p]
    woorden = [
        p.replace("-", " ")
        for p in delen
        if not re.fullmatch(r"[0-9a-f]{8}|\d+|huren|detail|huur|aanbod", p)
        and "te-huur" not in p
    ]
    return " · ".join(woorden[-2:]) or url


# ---------- meldingen ----------

def meld(titel, tekst, link=None):
    print(f"[melding] {titel} | {tekst} | {link or ''}")
    if not TOPIC:
        return
    data = {"topic": TOPIC, "title": titel, "message": tekst, "tags": ["house"]}
    if link:
        data["click"] = link
        data["actions"] = [{"action": "view", "label": "Bekijk", "url": link}]
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("Melding versturen mislukt:", e)


def lees_sites():
    if not os.path.exists(SITES_FILE):
        return []
    with open(SITES_FILE, encoding="utf-8") as f:
        return [r.strip() for r in f
                if r.strip().startswith("http") and not r.strip().startswith("#")]


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            staat = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        staat = {}
    gezien = staat.setdefault("gezien", {})
    fouten = staat.setdefault("fouten", {})

    sites = lees_sites()
    if not sites:
        print("Geen zoekpagina's in sites.txt")
        return

    for zoek_url in sites:
        d = domein(zoek_url)
        try:
            links, methode = haal_woningen(zoek_url)
            print(f"OK {d}: {len(links)} woningen ({methode})")
        except Exception as e:
            fouten[zoek_url] = fouten.get(zoek_url, 0) + 1
            print(f"Probleem met {d}: {e}")
            if fouten[zoek_url] == FOUTEN_VOOR_WAARSCHUWING:
                meld(f"Woningbot kan {d} niet lezen",
                     "Deze site blokkeert de bot of is veranderd. "
                     "Haal hem eventueel weg uit sites.txt.", zoek_url)
            continue

        if fouten.get(zoek_url, 0) >= FOUTEN_VOOR_WAARSCHUWING and zoek_url in gezien:
            meld(f"Woningbot werkt weer voor {d}", "De site is weer bereikbaar.")
        fouten[zoek_url] = 0

        if zoek_url not in gezien:
            gezien[zoek_url] = links
            meld(f"Woningbot actief op {d}",
                 f"{len(links)} woningen staan er nu. Je krijgt vanaf nu alleen nieuwe.",
                 zoek_url)
            continue

        bekend = set(gezien[zoek_url])
        nieuw = [l for l in links if l not in bekend]
        if len(nieuw) > MAX_MELDINGEN_PER_KEER:
            meld(f"{len(nieuw)} nieuwe woningen op {d}", "Tik om de zoekpagina te openen.", zoek_url)
        else:
            for l in nieuw:
                meld(f"Nieuwe woning op {d}", omschrijving(l), l)
        gezien[zoek_url] = (nieuw + gezien[zoek_url])[:500]

    sluit_browser()

    # sites die uit sites.txt zijn gehaald, vergeten
    for oud in list(gezien):
        if oud not in sites:
            gezien.pop(oud)
    for oud in list(fouten):
        if oud not in sites:
            fouten.pop(oud)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(staat, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
