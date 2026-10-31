"""
Woningbot - stuurt een melding naar je telefoon (via ntfy) zodra er een
nieuwe woning verschijnt op een zoekpagina van een verhuursite.

Je hoeft in dit bestand niets aan te passen.
- De zoekpagina's staan in sites.txt
- Je ntfy-kanaal staat als 'secret' NTFY_TOPIC in GitHub
"""

import json
import os
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = "seen.json"
SITES_FILE = "sites.txt"
MAX_MELDINGEN_PER_KEER = 8   # meer nieuwe woningen tegelijk? dan 1 samenvatting
FOUTEN_VOOR_WAARSCHUWING = 3  # na zoveel mislukte pogingen op rij: 1 waarschuwing

# Hoe een link naar een woning eruitziet, per website
PATRONEN = {
    "pararius.nl": r"/(appartement|huis|studio|kamer|woning)-te-huur/[^/]+/[0-9a-f]{8}(/|$)",
    "huurwoningen.nl": r"/huren/[^/]+/[0-9a-f]{8}(/|$)",
    "huurwoningen.com": r"/huren/[^/]+/[0-9a-f]{8}(/|$)",
    "funda.nl": r"/detail/huur/.+/\d{6,}",
    "kamernet.nl": r"/huren/[^/]+/[^/]+/[a-z]+-\d{5,}",
}
# Voor andere sites (bijv. een lokale makelaar): links met een woning-ID erin
ALGEMEEN = r"/[^?#]*(\d{5,}|[0-9a-f]{8})"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}


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


def haal_op(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def woning_links(zoek_url, html):
    d = domein(zoek_url)
    patroon = next((p for site, p in PATRONEN.items() if d.endswith(site)), ALGEMEEN)
    parser = LinkParser()
    parser.feed(html)
    gevonden = []
    for href in parser.links:
        volledig = urljoin(zoek_url, href)
        if domein(volledig) != d:
            continue
        if re.search(patroon, urlparse(volledig).path):
            s = schoon(volledig)
            if s != schoon(zoek_url) and s not in gevonden:
                gevonden.append(s)
    return gevonden


def omschrijving(url):
    """Maakt van de link een leesbare tekst, bijv. 'zwolle · vijfhoek'."""
    delen = [p for p in urlparse(url).path.split("/") if p]
    woorden = [
        p.replace("-", " ")
        for p in delen
        if not re.fullmatch(r"[0-9a-f]{8}|\d+|huren|detail|huur", p)
        and "te-huur" not in p
    ]
    return " · ".join(woorden[-2:]) or url


def meld(titel, tekst, link=None):
    print(f"[melding] {titel} | {tekst} | {link or ''}")
    if not TOPIC:
        return
    data = {"topic": TOPIC, "title": titel, "message": tekst, "tags": ["house"]}
    if link:
        data["click"] = link
        data["actions"] = [{"action": "view", "label": "Bekijk woning", "url": link}]
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
        return [
            r.strip() for r in f
            if r.strip().startswith("http") and not r.strip().startswith("#")
        ]


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
            links = woning_links(zoek_url, haal_op(zoek_url))
            if not links:
                raise ValueError("geen woningen op de pagina gevonden")
        except Exception as e:
            fouten[zoek_url] = fouten.get(zoek_url, 0) + 1
            print(f"Probleem met {d}: {e}")
            if fouten[zoek_url] == FOUTEN_VOOR_WAARSCHUWING:
                meld(
                    f"Woningbot kan {d} niet lezen",
                    "Deze site blokkeert de bot of is veranderd. "
                    "Haal hem eventueel weg uit sites.txt.",
                    zoek_url,
                )
            continue

        if fouten.get(zoek_url, 0) >= FOUTEN_VOOR_WAARSCHUWING:
            meld(f"Woningbot werkt weer voor {d}", "De site is weer bereikbaar.")
        fouten[zoek_url] = 0

        if zoek_url not in gezien:
            gezien[zoek_url] = links
            meld(
                f"Woningbot actief op {d}",
                f"{len(links)} woningen staan er nu. Je krijgt vanaf nu alleen nieuwe.",
                zoek_url,
            )
            continue

        bekend = set(gezien[zoek_url])
        nieuw = [l for l in links if l not in bekend]
        if len(nieuw) > MAX_MELDINGEN_PER_KEER:
            meld(f"{len(nieuw)} nieuwe woningen op {d}", "Tik om de zoekpagina te openen.", zoek_url)
        else:
            for l in nieuw:
                meld(f"Nieuwe woning op {d}", omschrijving(l), l)

        gezien[zoek_url] = (nieuw + gezien[zoek_url])[:500]

    # sites die uit sites.txt zijn gehaald, vergeten
    for oud in list(gezien):
        if oud not in sites:
            gezien.pop(oud)
            fouten.pop(oud, None)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(staat, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
