import os
import json
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import logging
import re
import socket
import time
import urllib.parse

# Globális időtúllépés beállítása (45 másodperc)
socket.setdefaulttimeout(45)

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")

# Alap bot az INGÓSÁGOKNAK
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Bot az INGATLANOKNAK
REAL_ESTATE_BOT_TOKEN = os.environ.get("REAL_ESTATE_BOT_TOKEN")
REAL_ESTATE_CHAT_ID = os.environ.get("REAL_ESTATE_CHAT_ID")

# Távolságszámítási kiindulópont koordinátái (Budapest)
ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198

SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ------------------- SZŰRŐK (BÁRMIKOR MÓDOSÍTHATÓ) -------------------
# Ingatlan szűrő: True esetén CSAK az 1/1 tulajdoni hányaddal rendelkező ingatlanok mennek át.
# Ha szeretnéd a többit is látni, állítsd False-ra.
CSAK_1_1_TULAJDON = True  

# Ingóság szűrő: Csak a megadott HUF érték ALATTI becsértékű ingóságokról küld értesítést.
# Ha ki szeretnéd kapcsolni ezt a korlátot, állítsd None-ra (pl. MAX_INGOSAG_BECSERTEK = None).
MAX_INGOSAG_BECSERTEK = 2000000

# MNV EAR szűrő: Csak a megadott HUF érték ALATTI kikiáltási árú ingatlanokról küld értesítést.
# A hírlevélből közvetlenül, scraping nélkül kerülnek kinyerésre.
MAX_MNV_KIKIALTAS = 2000000

# ------------------- FELADÓ SZŰRŐK -------------------
# Csak ezen domain-ekről érkező, VAGY ezeket tartalmazó forwarded levelek kerülnek feldolgozásra.
NAV_SENDER_DOMAINS = ["nav.gov.hu", "mnv.hu"]


# =================== Látott URL-ek kezelése ===================

def load_seen_urls() -> set:
    if not os.path.exists(SEEN_URLS_FILE):
        return set()
    try:
        with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_urls", []))
    except Exception as e:
        logger.error(f"Látott URL-ek betöltési hiba: {e}")
        return set()


def save_seen_urls(seen: set):
    try:
        with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_urls": sorted(seen)}, f, ensure_ascii=False, indent=2)
        logger.info(f"Látott URL-ek elmentve ({len(seen)} db).")
    except Exception as e:
        logger.error(f"Látott URL-ek mentési hiba: {e}")


# =================== Segédfüggvények ===================

def generate_gcal_url(title, date_str, location="", details=""):
    """
    Készít egy Google Calendar URL-t a megadott adatokból.
    Átváltja UTC-vé és ctz paramétert ad hozzá, hogy mobilon/appban se legyen egész napos hiba.
    """
    if not date_str or date_str == "N/A":
        return None

    try:
        import zoneinfo
        bp_tz = zoneinfo.ZoneInfo("Europe/Budapest")
    except Exception:
        bp_tz = None

    try:
        # Keresünk minden dátum-idő formátumot a szövegben (YYYY-MM-DD HH:MM)
        matches = re.findall(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})', date_str)
        
        if not matches:
            return None

        # Kezdési időpont
        start_dt = datetime.strptime(matches[0], "%Y-%m-%d %H:%M")
        
        # Ha van befejezési időpont (pl. megtekintésnél)
        if len(matches) >= 2:
            end_dt = datetime.strptime(matches[1], "%Y-%m-%d %H:%M")
        else:
            # Ha nincs befejezés, alapértelmezetten 1 órás eseményt csinálunk
            end_dt = start_dt + timedelta(hours=1)

        # Ha elérhető a zoneinfo, átváltjuk UTC-re (a mobil appok szigorúan ezt kérik)
        if bp_tz:
            start_dt = start_dt.replace(tzinfo=bp_tz)
            end_dt = end_dt.replace(tzinfo=bp_tz)
            start_utc = start_dt.astimezone(timezone.utc)
            end_utc = end_dt.astimezone(timezone.utc)
            start_str = start_utc.strftime("%Y%m%dT%H%M%SZ")
            end_str = end_utc.strftime("%Y%m%dT%H%M%SZ")
        else:
            start_str = start_dt.strftime("%Y%m%dT%H%M%SZ")
            end_str = end_dt.strftime("%Y%m%dT%H%M%SZ")

        # URL összeállítása
        url = f"https://calendar.google.com/calendar/render?action=TEMPLATE"
        url += f"&text={urllib.parse.quote(title)}"
        url += f"&dates={start_str}/{end_str}"
        url += f"&ctz=Europe/Budapest"
        
        if location and location != "N/A":
            url += f"&location={urllib.parse.quote(location)}"
        if details:
            url += f"&details={urllib.parse.quote(details)}"
            
        return url
    except Exception as e:
        logger.warning(f"Nem sikerült a naptár link generálása: {e}")
        return None


def clean_text(text):
    return " ".join(text.split()) if text else ""


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&").replace("<", "<").replace(">", ">")


def parse_price_to_int(price_str):
    """Szöveges árból (pl. '2 500 000 HUF') tiszta egészet csinál az összehasonlításhoz."""
    if not price_str:
        return 0
    cleaned = re.sub(r'[^\d]', '', price_str.replace('\xa0', ''))
    return int(cleaned) if cleaned else 0


def calculate_darabar(price_str, db_str):
    """Kiszámolja a darabárat, ha a darabszám meg van adva és > 1."""
    if not price_str or not db_str:
        return None
    try:
        p_clean = price_str.replace(" ", "").replace("\xa0", "")
        p_match = re.search(r"(\d+)", p_clean)
        d_clean = db_str.replace(" ", "").replace("\xa0", "")
        d_match = re.search(r"(\d+)", d_clean)
        
        if p_match and d_match:
            price = int(p_match.group(1))
            db = int(d_match.group(1))
            if db > 1:
                darabar = round(price / db)
                return f"{darabar:,} HUF/db".replace(",", " ")
    except Exception as e:
        logger.warning(f"Nem sikerült a darabárat kiszámolni: {e}")
    return None


def remove_sablon_szoveg(text):
    """Eltávolítja a NAV-os sablonszövegeket a leírásból."""
    if not text:
        return ""
    
    sablonok = [
        "Az elárverezett vagyontárgyakért sem az adós, sem az adóhatóság jótállással nem tartozik. A megtekintés során a résztvevők által nem észlelt vagy fel nem ismerhető rejtett hibákért, a vagyontárgy esetlegesen előforduló vélt vagy valós hiányosságaiért az adóhatóság felelősséget nem vállal.",
        "Az elárverezett vagyontárgyakért sem az adós, sem az adóhatóság jótállással nem tartozik.",
        "A megtekintés során a résztvevők által nem észlelt vagy fel nem ismerhető rejtett hibákért, a vagyontárgy esetlegesen előforduló vélt vagy valós hiányosságaiért az adóhatóság felelősséget nem vállal."
    ]
    
    for s in sablonok:
        text = text.replace(s, "")
        
    return " ".join(text.split()).strip()


def extract_nav_eaf_links(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "arveres.nav.gov.hu" in href and ("auctionId" in href or "item=auctionSummary" in href):
            if href.startswith("/"):
                href = "https://arveres.nav.gov.hu" + href
            href = href.replace("nav.gov.hu//", "nav.gov.hu/")
            links.append(href)
    return list(set(links))


def simplify_address(address):
    """Fokozatosan egyszerűsített cím-változatok geocódoláshoz."""
    candidates = [address]
    cleaned = re.sub(r",?\s*\d+(/\d+)?\s*hrsz\.?", "", address, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(külterület|belterület|tanya)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().rstrip(",").strip()
    if cleaned and cleaned != address:
        candidates.append(cleaned)

    city_match = re.match(
        r"(\d{4}\s+[A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ][A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ\s\-]+?)"
        r"(?:\s*,|\s+\d|\s+külterület|\s+belterület|$)",
        cleaned or address
    )
    if city_match:
        city_only = city_match.group(1).strip()
        if city_only not in candidates:
            candidates.append(city_only)
    return [c.strip() for c in candidates if c.strip()]


def geocode_address(address):
    candidates = simplify_address(address)
    headers = {"User-Agent": "NAV-EAF-Scraper-V2/1.0"}
    for candidate in candidates:
        try:
            logger.info(f" -> Geokódolás megkísérlése ezzel: {candidate}")
            params = {"q": candidate, "format": "json", "limit": 1, "countrycodes": "hu"}
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params, headers=headers, timeout=10
            )
            if resp.status_code == 200 and resp.json():
                results = resp.json()
                return float(results[0]["lat"]), float(results[0]["lon"])
            time.sleep(1.1)
        except Exception as e:
            logger.warning(f"Geokódolási részhiba ({candidate}): {e}")
            time.sleep(1.1)
    return None


def get_drive_distance(coords):
    if not coords:
        return None
    dest_lat, dest_lon = coords
    try:
        url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}?overview=false"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                return round(route["distance"] / 1000, 1), round(route["duration"] / 60)
    except Exception as e:
        logger.warning(f"Távolságszámítási hiba: {e}")
    return None


def scrape_main_image(soup):
    """Első kép az oldalról (fullurl attribútum vagy képgaléria)."""
    BASE = "https://arveres.nav.gov.hu"
    try:
        for img_tag in soup.find_all("img"):
            fullurl = img_tag.get("fullurl", "").strip()
            if fullurl:
                return fullurl if fullurl.startswith("http") else BASE + "/" + fullurl.lstrip("/")
        galeria = soup.find("div", string=re.compile("Képgaléria", re.IGNORECASE))
        if galeria:
            parent = galeria.find_parent()
            if parent:
                img = parent.find("img", src=True)
                if img:
                    src = img["src"]
                    return src if src.startswith("http") else BASE + "/" + src.lstrip("/")
    except Exception:
        pass
    return None


# =================== NAV oldal feldolgozása ===================

FIELD_MAP = {
    "Árverés megnevezése":                      "kategoria",
    "Végrehajtási ügyszám":                     "ugyintezesi_szam",
    "Árverés kategória":                        "kategoria_reszletes",
    "Árverés sorszáma":                         "arveres_sorszam",
    "Árverés meghirdetése":                     "meghirdetes",
    "Árverés kezdete":                          "kezdet",
    "Árverés befejezése":                       "befejezes",
    "Az árverezett tétel megtekinthető, hely":  "megtekintes_hely",
    "Az árverezett tétel megtekinthető, idő":   "megtekintes_ido",
    # Ingatlan tétel adatok
    "Ingatlan megnevezése":                     "ingatlan_megnevezes",
    "Tétel megnevezése":                        "tetel_megnevezes",
    "Becsérték":                                "becsertek",
    "Árverési előleg":                          "arveres_eloleg",
    "Minimál ajánlat":                          "minimal_ajanlat",
    "Egyéb infó":                               "egyeb_info",
    "Van előárverezésre jogosult":              "eloarverezesre_jogosult",
    # Cím
    "Ország":                                   "orszag",
    "Megye":                                    "megye_tabla",
    "Cím irányítószám, város":                  "varos",
    "Cím utca":                                 "utca",
    "Házszám, emelet, ajtó":                    "hazszam",
    # Ingatlan jellemzők
    "Tulajdoni hányad":                         "tulajdoni_hanyad",
    "Helyrajzi szám":                           "helyrajzi_szam",
    "Terület":                                  "terulet",
    "3.a Megközelíthetősége":                   "megkozelithetoseg",
    "5. Külön engedély nélkül beépíthető":      "beepitheto",
    "7. Talajának minősége":                    "talaj_minoseg",
    "8. Növényzete":                            "novenyzet",
    "9. Kerítése":                              "kerites",
    "Kerítés anyaga":                           "kerites_anyaga",
    # Ingóság jellemzők
    "Állapot":                                  "allapot",
    "Egyszerre árverezett tétel darabszám":     "darabszam",
}


def parse_nav_eaf_details(url):
    logger.info(f"NAV oldal letöltése: {url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=20, headers=headers)
        resp.encoding = "ISO-8859-2"
        html_text = resp.text
    except Exception as e:
        logger.error(f"Nem sikerült letölteni a NAV oldalt: {e}")
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    data = {"url": url}

    megye_match = re.search(
        r'([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ\-]+)\s+(?:Vár)?megye',
        html_text
    )
    if megye_match:
        data["megye"] = megye_match.group(1).strip() + " vármegye"

    for div in soup.find_all("div", class_="FrissPortlet"):
        header = div.find("div", class_="HeaderTitle")
        if not header:
            continue
        header_text = header.get_text()

        relevant = (
            "Árverés alapadatok" in header_text
            or "Árverezett tétel adatok" in header_text
        )
        if not relevant:
            continue

        table = div.find("table", class_="DownloadAppsList")
        if not table:
            table = div.find("table")
        if not table:
            continue

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            key = clean_text(cells[0].get_text())
            value = clean_text(cells[1].get_text())
            if not key or not value:
                continue

            matched = False
            for field_key, dict_key in FIELD_MAP.items():
                if field_key in key:
                    data[dict_key] = value
                    matched = True
                    break

            if not matched and "Bejegyzések a tulajdoni lapon" in key:
                data["tulajdoni_lap_bejegyzesek"] = value

            if not matched and "Egyéb megjegyzések" in key:
                data["egyeb_megjegyzesek"] = value

    if "megye_tabla" in data and data["megye_tabla"]:
        raw = data["megye_tabla"]
        if "vármegye" not in raw.lower() and "megye" not in raw.lower():
            data["megye"] = raw + " vármegye"
        else:
            data["megye"] = raw

    varos = data.get("varos", "").strip()
    utca = data.get("utca", "").strip()
    hazszam = data.get("hazszam", "").strip()

    if varos:
        parts = [p for p in [varos, utca, hazszam] if p]
        data["teljes_cim"] = ", ".join(parts[:1]) + (
            " " + " ".join(parts[1:]) if len(parts) > 1 else ""
        )
    else:
        data["teljes_cim"] = data.get("megtekintes_hely", "")

    data["image_url"] = scrape_main_image(soup)

    geocode_input = data.get("teljes_cim") or data.get("megtekintes_hely", "")

    if geocode_input and geocode_input.lower() not in ("ingatlan címén", ""):
        coords = geocode_address(geocode_input)
        if coords:
            lat, lon = coords
            data["maps_url"] = f"http://maps.google.com/?q={lat},{lon}"
            result = get_drive_distance(coords)
            data["tavolsag"] = (
                f"{result[0]} km ({result[1]} perc autóval)" if result
                else "Nem sikerült kiszámítani"
            )
        else:
            data["tavolsag"] = "N/A"
    else:
        if data.get("teljes_cim"):
            coords = geocode_address(data["teljes_cim"])
            if coords:
                lat, lon = coords
                data["maps_url"] = f"http://maps.google.com/?q={lat},{lon}"
                result = get_drive_distance(coords)
                data["tavolsag"] = (
                    f"{result[0]} km ({result[1]} perc autóval)" if result
                    else "Nem sikerült kiszámítani"
                )
            else:
                data["tavolsag"] = "N/A"
        else:
            data["tavolsag"] = "N/A"

    data["cim"] = (
        data.get("ingatlan_megnevezes")
        or data.get("tetel_megnevezes")
        or data.get("kategoria_reszletes")
        or "Ismeretlen tétel"
    )

    logger.info(f"Feldolgozva: {data.get('teljes_cim')} | {data.get('cim')} | {data.get('becsertek')}")
    return data


# =================== Email feldolgozás ===================

def extract_html_from_message(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return None


def _decode_subject(msg) -> str:
    subject_parts = decode_header(msg.get("Subject", ""))
    return "".join([
        p.decode(e or "utf-8", errors="ignore") if isinstance(p, bytes) else p
        for p, e in subject_parts
    ])


def _classify_email(msg, subject_str: str, html_body: str) -> str:
    """
    Visszaadja a levél típusát:
      'nav_eaf' – NAV Elektronikus Árverés (közvetlenül vagy forwarded)
      'mnv_ear' – MNV EAR Heti hírlevél (közvetlenül vagy forwarded)
      'skip'    – nem releváns, kihagyandó
    Csak NAV/MNV domain-ről érkező vagy tőlük forwarded leveleket fogad el.
    """
    from_header = msg.get("From", "").lower()
    subj_l = subject_str.lower()
    html_l = (html_body or "").lower()

    # --- MNV EAR felismerés ---
    mnv_signals = [
        any(d in from_header for d in ["mnv.hu"]),
        "mnv ear" in subj_l,
        "heti hírlevél" in subj_l,
        "heti hirlevél" in subj_l,
        "meghirdetett árverésekről" in subj_l,
        "no-reply-ear@mnv.hu" in html_l,
        "ear.mnv.hu" in html_l,
        # forwarded esetén a törzsben szerepelhet a feladó
        ("mnv" in html_l and "ear" in html_l and "hírlevél" in html_l),
    ]
    if any(mnv_signals):
        return "mnv_ear"

    # --- NAV EAF felismerés ---
    nav_signals = [
        any(d in from_header for d in ["nav.gov.hu"]),
        "elektronikus árverés" in subj_l,
        "elektronikus arveres" in subj_l,
        "eaf@nav.gov.hu" in html_l,
        "arveres.nav.gov.hu" in html_l,
    ]
    if any(nav_signals):
        return "nav_eaf"

    return "skip"


def get_emails_since(since_date):
    """
    Visszaadja az olvasatlan NAV/MNV levelek HTML tartalmát két listában:
      (nav_eaf_htmls, mnv_ear_htmls)
    Csak NAV EAF vagy MNV EAR feladóktól (vagy tőlük forwarded) érkező leveleket dolgoz fel.
    """
    try:
        logger.info("Kapcsolódás a Gmail IMAP szerverhez...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        search_criteria = f'(UNSEEN SINCE "{since_date.strftime("%d-%b-%Y")}")'
        logger.info(f"Keresési feltétel küldése: {search_criteria}")

        status, messages = mail.search(None, search_criteria)

        if status != "OK" or not messages[0]:
            logger.info("Nem található ÚJ, OLVASATLAN levél a megadott dátum óta.")
            mail.close()
            mail.logout()
            return [], []

        msg_ids = messages[0].split()
        logger.info(f"Talált olvasatlan e-mailek száma összesen: {len(msg_ids)}")

        nav_eaf_htmls = []
        mnv_ear_htmls = []

        for idx, eid in enumerate(msg_ids, 1):
            logger.info(f" -> [{idx}/{len(msg_ids)}] E-mail letöltése (ID: {eid.decode()})...")
            # BODY.PEEK[]: letölti a levelet anélkül, hogy automatikusan olvasottnak jelölné
            status, msg_data = mail.fetch(eid, "(BODY.PEEK[])")
            if status != "OK":
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject_str = _decode_subject(msg)
            html_body = extract_html_from_message(msg)

            email_type = _classify_email(msg, subject_str, html_body)

            if email_type == "nav_eaf":
                logger.info(f"    * NAV EAF levél! Tárgy: {subject_str}")
                if html_body:
                    nav_eaf_htmls.append(html_body)
                mail.store(eid, "+FLAGS", "\\Seen")
            elif email_type == "mnv_ear":
                logger.info(f"    * MNV EAR hírlevél! Tárgy: {subject_str}")
                if html_body:
                    mnv_ear_htmls.append(html_body)
                mail.store(eid, "+FLAGS", "\\Seen")
            else:
                logger.info(f"    * Nem NAV/MNV levél, kihagyás. Feladó: {msg.get('From', '')} | Tárgy: {subject_str}")

        mail.close()
        mail.logout()
        return nav_eaf_htmls, mnv_ear_htmls
    except Exception as e:
        logger.error(f"IMAP hiba történt: {e}")
        return [], []


# =================== MNV EAR hírlevél feldolgozása (scraping nélkül) ===================

# A felismerni kívánt mezőcímkék és a hozzájuk tartozó dict kulcsok.
# FONTOS: sorrendben kell tartani – a sor-alapú parser erre támaszkodik.
MNV_LABEL_FIELD = [
    ("Árverés alkategória neve",                    "alkategoria"),
    ("Árverezett tétel megnevezése, azonosítója",   "tetel_nev_azonosito"),
    ("Kikiáltási ár",                               "kikialtas_ar"),
    ("Meghirdetési dátum és idő",                   "meghirdetes"),
    ("Biztosíték megfizetésének határideje",         "biztosítek_hatarido"),
    ("Kezdési dátum és idő",                        "kezdet"),
    ("Befejezési dátum és idő",                     "befejezes"),
]


def _finalize_mnv_auction(auction_data: dict) -> dict | None:
    """Egyedi azonosítót és rövid cím-verziót told bele a dict-be; None-t ad vissza ha hiányos."""
    if "tetel_nev_azonosito" not in auction_data or "kikialtas_ar" not in auction_data:
        return None
    raw_nev = auction_data.get("tetel_nev_azonosito", "")
    id_match = re.search(r'\[(\d+/\d+)\]', raw_nev)
    auction_data["mnv_id"] = id_match.group(1) if id_match else None
    cim_match = re.match(r'^(.*?)(?:\s*[\[\(])', raw_nev)
    auction_data["cim_rovid"] = cim_match.group(1).strip() if cim_match else raw_nev
    return auction_data


def parse_mnv_ear_auctions(html_content: str) -> list:
    """
    Kinyeri az MNV EAR hírlevél árverési tételeit HTML-tartalmából.

    A HTML-t BeautifulSoup-pal szöveggé alakítja, majd soronként dolgozza fel.
    Ez az eljárás forwarded Gmail-leveleknél is megbízhatóan működik,
    hiszen nem függ a HTML táblázat-struktúrájától.

    Logika:
      - Minden „Árverés alkategória neve" sort új árverési blokk kezdetének tekint.
      - Az egyes mezők értéke lehet ugyanazon a soron (pl. „Kikiáltási ár  64 770 000 Ft")
        VAGY a következő nem-üres, nem-mezőcímke soron.
      - Egy blokk lezárul, ha újabb „Árverés alkategória neve" sort talál.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    raw_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    all_labels = {label for label, _ in MNV_LABEL_FIELD}

    def is_label(line: str) -> bool:
        return any(lbl in line for lbl in all_labels)

    def next_value(lines: list, idx: int) -> str:
        """A következő nem-üres, nem-mezőcímke sort adja vissza értékként."""
        j = idx + 1
        while j < len(lines):
            if lines[j] and not is_label(lines[j]):
                return lines[j]
            break
        return ""

    auctions = []
    current: dict = {}
    i = 0

    while i < len(lines):
        line = lines[i]

        for label, field in MNV_LABEL_FIELD:
            if label not in line:
                continue

            # Új blokk indul – az előző mentése ha kész
            if field == "alkategoria" and current:
                result = _finalize_mnv_auction(current)
                if result:
                    auctions.append(result)
                current = {}

            # Érték kinyerése: ugyanazon a soron a label után, vagy a következő soron
            after_label = line[line.index(label) + len(label):].strip().lstrip(":").strip()
            if after_label:
                current[field] = after_label
            else:
                val = next_value(lines, i)
                if val:
                    current[field] = val
                    i += 1  # következő sort már feldolgoztuk
            break

        i += 1

    # Utolsó blokk mentése
    if current:
        result = _finalize_mnv_auction(current)
        if result:
            auctions.append(result)

    logger.info(f"MNV EAR: {len(auctions)} tétel kinyerve az e-mailből.")
    return auctions


def build_mnv_ear_message(a: dict) -> str:
    """Telegram értesítő MNV EAR hírlevélből kinyert ingatlanhoz."""
    alkategoria = escape_html(a.get("alkategoria") or "Ingatlan")
    tetel = escape_html(a.get("tetel_nev_azonosito") or "Ismeretlen")
    ar = escape_html(a.get("kikialtas_ar") or "N/A")
    meghirdetes = escape_html(a.get("meghirdetes") or "N/A")
    hatarido = escape_html(a.get("biztosítek_hatarido") or "")
    kezdet = escape_html(a.get("kezdet") or "N/A")
    befejezes = escape_html(a.get("befejezes") or "N/A")

    # MNV EAR dinamikus link előállítása az azonosítóból (javítva a valós formátumra)
    mnv_id = a.get("mnv_id")  # pl. "50002/260611"
    auction_link = "#"
    if mnv_id:
        match = re.match(r'(\d+)/', mnv_id)
        if match:
            auction_id = match.group(1)
            # Helyes link: https://e-arveres.mnv.hu//index-ingosag.html?.actionId=...&auctionId=...
            auction_link = f"https://e-arveres.mnv.hu//index-ingosag.html?.actionId=action.auction.AuctionSummaryAction&auctionId={auction_id}&FRAME_SKIP_DEJAVU=1"

    reszletek = f"Kikiáltási ár: {a.get('kikialtas_ar', 'N/A')} | MNV EAR árverés"
    kezdet_url = generate_gcal_url(f"MNV Árverés Kezdete: {a.get('cim_rovid', '')}", kezdet, "", reszletek)
    befejezes_url = generate_gcal_url(f"MNV Árverés Vége: {a.get('cim_rovid', '')}", befejezes, "", reszletek)
    hatarido_url = generate_gcal_url(f"MNV Biztosíték határideje: {a.get('cim_rovid', '')}", hatarido, "", reszletek) if hatarido else None

    lines = [
        "🏛 <b>MNV EAR INGATLAN TALÁLAT</b>",
        f"📋 <b>{alkategoria}</b>",
        "",
        "🌍 <b>1. Elhelyezkedés és Alapadatok</b>",
        f"🏷 <b>Tétel:</b> {tetel}",
        "",
        "💰 <b>2. Pénzügyi Információk</b>",
        f"💵 <b>Kikiáltási ár:</b> {ar}",
        "",
        "📅 <b>3. Időpontok</b>",
        f"📢 <b>Meghirdetve:</b> {meghirdetes}",
    ]

    if hatarido:
        if hatarido_url:
            lines.append(f"⏰ <b>Biztosíték határideje:</b> <a href='{hatarido_url}'>{hatarido}</a>")
        else:
            lines.append(f"⏰ <b>Biztosíték határideje:</b> {hatarido}")

    if kezdet_url:
        lines.append(f"▶️ <b>Kezdés:</b> <a href='{kezdet_url}'>{kezdet}</a>")
    else:
        lines.append(f"▶️ <b>Kezdés:</b> {kezdet}")

    if befejezes_url:
        lines.append(f"🏁 <b>Befejezés:</b> <a href='{befejezes_url}'>{befejezes}</a>")
    else:
        lines.append(f"🏁 <b>Befejezés:</b> {befejezes}")

    lines.extend([
        "",
        f"🔗 <a href='{auction_link}'>Megnyitás az MNV EAR rendszerben</a>",
    ])

    return "\n".join(lines)


# =================== Telegram üzenetküldés ===================

def send_via_requests(caption, image_url, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id:
        logger.error("Hiba: Hiányzó Telegram token vagy chat ID!")
        return

    if image_url:
        try:
            img_resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if img_resp.status_code == 200 and "image" in img_resp.headers.get("Content-Type", ""):
                url = f"https://api.telegram.org/bot{target_bot_token}/sendPhoto"
                files = {"photo": ("image.jpg", img_resp.content, "image/jpeg")}
                data = {"chat_id": target_chat_id, "caption": caption, "parse_mode": "HTML"}
                resp = requests.post(url, files=files, data=data, timeout=20)
                if resp.status_code == 200:
                    logger.info("Sikeresen kiküldve képpel együtt.")
                    return
                logger.warning(f"Képküldés API hiba: {resp.text}")
        except Exception as e:
            logger.warning(f"Nem sikerült a képet küldeni, megpróbáljuk sima szövegként: {e}")

    try:
        url = f"https://api.telegram.org/bot{target_bot_token}/sendMessage"
        data = {
            "chat_id": target_chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, data=data, timeout=20)
        if resp.status_code == 200:
            logger.info("Sikeresen kiküldve sima szövegként.")
        else:
            logger.error(f"Telegram küldési hiba: {resp.text}")
    except Exception as e:
        logger.error(f"Nem sikerült kommunikálni a Telegram API-val: {e}")


def build_ingatlan_message(a: dict) -> str:
    """MBVK-stílusú Telegram üzenet ingatlan árverésekhez (Ügyszám nélkül)."""

    arveres_nev = escape_html(a.get("kategoria_reszletes") or a.get("kategoria") or "Ingatlan árverés")
    ingatlan_nev = escape_html(a.get("ingatlan_megnevezes") or a.get("cim") or "Ismeretlen")
    teljes_cim = escape_html(a.get("teljes_cim") or "Ismeretlen cím")
    megye = escape_html(a.get("megye") or "")
    tavolsag = a.get("tavolsag", "")

    becsertek = escape_html(a.get("becsertek") or "N/A")
    minimal_ajanlat = escape_html(a.get("minimal_ajanlat") or "N/A")
    arveres_eloleg = escape_html(a.get("arveres_eloleg") or "")

    kezdet = escape_html(a.get("kezdet") or "N/A")
    befejezes = escape_html(a.get("befejezes") or "N/A")
    megtekintes_ido = escape_html(a.get("megtekintes_ido") or "")

    tulajdoni_hanyad = escape_html(a.get("tulajdoni_hanyad") or "")
    helyrajzi_szam = escape_html(a.get("helyrajzi_szam") or "")
    terulet = escape_html(a.get("terulet") or "")
    megkozelithetoseg = escape_html(a.get("megkozelithetoseg") or "")
    beepitheto = escape_html(a.get("beepitheto") or "")
    kerites = escape_html(a.get("kerites") or "")
    kerites_anyaga = escape_html(a.get("kerites_anyaga") or "")
    talaj = escape_html(a.get("talaj_minoseg") or "")
    novenyzet = escape_html(a.get("novenyzet") or "")

    bejegyzesek = escape_html(a.get("tulajdoni_lap_bejegyzesek") or "")
    
    # Sablonszöveg szűrése
    egyeb_info = a.get("egyeb_info") or a.get("egyeb_megjegyzesek") or ""
    egyeb_info = remove_sablon_szoveg(egyeb_info)
    if len(egyeb_info) > 500:
        egyeb_info = egyeb_info[:500] + "…"
    egyeb_info = escape_html(egyeb_info)

    lines = [
        f"🏠 <b>NAV INGATLAN TALÁLAT</b>",
        f"📋 <b>{arveres_nev}</b>",
        "",
        "🌍 <b>1. Elhelyezkedés és Alapadatok</b>",
        f"🏷 <b>Megnevezés/Cím:</b> {ingatlan_nev}",
        f"📍 <b>Cím:</b> {teljes_cim}",
    ]

    if megye:
        lines.append(f"🏛 <b>Megye:</b> {megye}")
    if tavolsag and tavolsag not in ("N/A", "Nem sikerült kiszámítani"):
        lines.append(f"🗺 <b>Budapest-távolság:</b> {tavolsag}")

    lines.append("")
    lines.append("🏗 <b>2. Az Ingatlan és a Telek Jellemzői</b>")

    if tulajdoni_hanyad:
        lines.append(f"📄 <b>Tulajdoni hányad:</b> {tulajdoni_hanyad}")
    if helyrajzi_szam:
        lines.append(f"🔢 <b>Helyrajzi szám:</b> {helyrajzi_szam}")
    if terulet:
        lines.append(f"📐 <b>Terület:</b> {terulet}")
    if megkozelithetoseg:
        lines.append(f"🛤 <b>Megközelíthetőség:</b> {megkozelithetoseg}")
    if beepitheto:
        lines.append(f"🏗 <b>Beépíthető:</b> {beepitheto}")
    if kerites:
        kerites_str = kerites
        if kerites_anyaga:
            kerites_str += f" ({kerites_anyaga})"
        lines.append(f"🚧 <b>Kerítés:</b> {kerites_str}")
    if talaj:
        lines.append(f"🌱 <b>Talaj:</b> {talaj}")
    if novenyzet:
        lines.append(f"🌿 <b>Növényzet:</b> {novenyzet}")

    lines.append("")
    lines.append("💰 <b>3. Pénzügyi Információk</b>")
    lines.append(f"💵 <b>Becsérték:</b> {becsertek}")
    lines.append(f"📉 <b>Minimál ajánlat:</b> {minimal_ajanlat}")
    if arveres_eloleg:
        eloleg_match = re.search(r"(\d[\d\s]*(?:HUF|Ft))", a.get("arveres_eloleg", ""))
        if eloleg_match:
            lines.append(f"💳 <b>Árverési előleg:</b> {escape_html(eloleg_match.group(1))}")

    lines.append("")
    lines.append("⚖️ <b>4. Jogi és Árverési Státusz</b>")

    # Google naptár linkek generálása
    tetel_cim = ingatlan_nev
    hely = teljes_cim
    reszletek = f"További infó: {a.get('url', '')}"

    kezdet_url = generate_gcal_url(f"NAV Árverés Kezdete: {tetel_cim}", kezdet, hely, reszletek)
    befejezes_url = generate_gcal_url(f"NAV Árverés Vége: {tetel_cim}", befejezes, hely, reszletek)

    if kezdet_url:
        lines.append(f"▶️ <b>Kezdés:</b> <a href='{kezdet_url}'>{kezdet}</a>")
    else:
        lines.append(f"▶️ <b>Árverés kezdete:</b> {kezdet}")

    if befejezes_url:
        lines.append(f"🏁 <b>Befejezés:</b> <a href='{befejezes_url}'>{befejezes}</a>")
    else:
        lines.append(f"🏁 <b>Árverés vége:</b> {befejezes}")

    if megtekintes_ido:
        megtekintes_url = generate_gcal_url(f"NAV Megtekintés: {tetel_cim}", megtekintes_ido, hely, reszletek)
        if megtekintes_url:
            lines.append(f"🕒 <b>Megtekintés:</b> <a href='{megtekintes_url}'>{megtekintes_ido}</a>")
        else:
            lines.append(f"🕒 <b>Megtekintés:</b> {megtekintes_ido}")

    if bejegyzesek:
        lines.append("")
        lines.append("📜 <b>Bejegyzések a tulajdoni lapon:</b>")
        lines.append(f"<i>{bejegyzesek}</i>")

    if egyeb_info:
        lines.append("")
        lines.append("📝 <b>Leírás:</b>")
        lines.append(f"<i>{egyeb_info}</i>")

    lines.append("")
    lines.append(f"🔗 <a href='{a.get('url', '')}'>Részletek a NAV oldalon</a>")
    if a.get("maps_url"):
        lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")

    return "\n".join(lines)


def build_ingosag_message(a: dict) -> str:
    """Ingóság árverési Telegram üzenet (Ügyszám nélkül, darabárral)."""

    arveres_nev = escape_html(a.get("kategoria_reszletes") or a.get("kategoria") or "Árverés")
    tetel_nev = escape_html(a.get("cim") or "Ismeretlen tétel")
    becsertek = escape_html(a.get("becsertek") or "N/A")
    
    minimal_ajanlat_raw = a.get("minimal_ajanlat") or "N/A"
    minimal_ajanlat = escape_html(minimal_ajanlat_raw)
    
    darabszam_raw = a.get("darabszam") or ""
    darabszam = escape_html(darabszam_raw)
    
    kezdet = escape_html(a.get("kezdet") or "N/A")
    befejezes = escape_html(a.get("befejezes") or "N/A")
    allapot = escape_html(a.get("allapot") or "")
    megye = escape_html(a.get("megye") or "")
    tavolsag = a.get("tavolsag", "")
    megtekintes_ido = escape_html(a.get("megtekintes_ido") or "")

    teljes_cim = escape_html(
        a.get("teljes_cim") or a.get("megtekintes_hely") or "Ismeretlen helyszín"
    )

    # Darabár kalkulálása a segédfüggvénnyel
    darabar = calculate_darabar(minimal_ajanlat_raw, darabszam_raw)

    # Sablonszöveg szűrése
    egyeb_info = a.get("egyeb_info") or ""
    egyeb_info = remove_sablon_szoveg(egyeb_info)
    if len(egyeb_info) > 400:
        egyeb_info = egyeb_info[:400] + "…"
    egyeb_info = escape_html(egyeb_info)

    lines = [
        f"🔔 <b>NAV INGÓSÁG TALÁLAT</b>",
        f"📋 <b>{arveres_nev}</b>",
        "",
        "🌍 <b>1. Elhelyezkedés és Alapadatok</b>",
        f"🏷 <b>Tétel:</b> {tetel_nev}",
        f"📍 <b>Helyszín:</b> {teljes_cim}",
    ]

    if megye:
        lines.append(f"🏛 <b>Megye:</b> {megye}")
    if tavolsag and tavolsag not in ("N/A", "Nem sikerült kiszámítani"):
        lines.append(f"🗺 <b>Budapest-távolság:</b> {tavolsag}")

    lines.append("")
    lines.append("📦 <b>2. A Tétel Jellemzői</b>")
    if allapot:
        lines.append(f"🚪 <b>Állapot:</b> {allapot}")
    if darabszam:
        lines.append(f"🔢 <b>Darabszám:</b> {darabszam}")

    lines.extend([
        "",
        "💰 <b>3. Pénzügyi Információk</b>",
        f"💵 <b>Becsérték:</b> {becsertek}",
        f"📉 <b>Minimál ajánlat:</b> {minimal_ajanlat}",
    ])
    
    # Ha van számolható darabár, megjelenítjük a minimál ajánlat alatt
    if darabar:
        lines.append(f"💲 <b>Minimum darabár:</b> {darabar}")

    lines.extend([
        "",
        "📅 <b>4. Időpontok és Árverési Státusz</b>"
    ])
    
    # Google naptár linkek generálása
    hely = teljes_cim
    reszletek = f"További infó: {a.get('url', '')}"

    kezdet_url = generate_gcal_url(f"NAV Árverés Kezdete: {tetel_nev}", kezdet, hely, reszletek)
    befejezes_url = generate_gcal_url(f"NAV Árverés Vége: {tetel_nev}", befejezes, hely, reszletek)

    if kezdet_url:
        lines.append(f"▶️ <b>Kezdés:</b> <a href='{kezdet_url}'>{kezdet}</a>")
    else:
        lines.append(f"▶️ <b>Kezdés:</b> {kezdet}")

    if befejezes_url:
        lines.append(f"🏁 <b>Befejezés:</b> <a href='{befejezes_url}'>{befejezes}</a>")
    else:
        lines.append(f"🏁 <b>Befejezés:</b> {befejezes}")

    if megtekintes_ido:
        megtekintes_url = generate_gcal_url(f"NAV Megtekintés: {tetel_nev}", megtekintes_ido, hely, reszletek)
        if megtekintes_url:
            lines.append(f"🕒 <b>Megtekintés:</b> <a href='{megtekintes_url}'>{megtekintes_ido}</a>")
        else:
            lines.append(f"🕒 <b>Megtekintés:</b> {megtekintes_ido}")

    if egyeb_info:
        lines.extend(["", "📝 <b>Leírás:</b>", f"<i>{egyeb_info}</i>"])

    lines.extend(["", f"🔗 <a href='{a.get('url', '')}'>Részletek a NAV oldalon</a>"])
    if a.get("maps_url"):
        lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")

    return "\n".join(lines)


def send_auction_message(a: dict, target_bot_token, target_chat_id, is_real_estate: bool):
    if is_real_estate:
        caption = build_ingatlan_message(a)
    else:
        caption = build_ingosag_message(a)
    send_via_requests(caption, a.get("image_url"), target_bot_token, target_chat_id)


# =================== Fő logika ===================

def main():
    logger.info("=== SCRAPER INDÍTÁSA ===")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    seen_urls = load_seen_urls()

    nav_eaf_htmls, mnv_ear_htmls = get_emails_since(since)

    if not nav_eaf_htmls and not mnv_ear_htmls:
        logger.info("Nincs új, olvasatlan NAV/MNV e-mail feldolgozásra.")
        return

    # =====================================================================
    # 1. NAV EAF feldolgozás – meglévő scraping logika
    # =====================================================================
    all_auctions = []
    for html in nav_eaf_htmls:
        links = extract_nav_eaf_links(html)
        for link in links:
            if link not in seen_urls:
                details = parse_nav_eaf_details(link)
                if details:
                    all_auctions.append(details)
            else:
                logger.info(f"Már feldolgozott NAV link kihagyása: {link}")

    unique_auctions = list({a["url"]: a for a in all_auctions}.values())
    logger.info(f"Összes új NAV EAF feldolgozandó tétel: {len(unique_auctions)}")

    for a in unique_auctions:
        kategoria_szoveg = (
            (a.get("kategoria") or "") + " " + (a.get("kategoria_reszletes") or "")
        ).lower()
        is_real_estate = "ingatlan" in kategoria_szoveg

        # ---- SZŰRÉSI LOGIKA ----
        if is_real_estate:
            if CSAK_1_1_TULAJDON:
                tulajdon = a.get("tulajdoni_hanyad", "")
                if "1/1" not in tulajdon:
                    logger.info(f"-> [SZŰRŐ] Ingatlan kihagyva (Nem 1/1 tulajdon): {a.get('teljes_cim')} ({tulajdon})")
                    continue
        else:
            if MAX_INGOSAG_BECSERTEK is not None:
                becsertek_int = parse_price_to_int(a.get("becsertek", ""))
                if becsertek_int > MAX_INGOSAG_BECSERTEK:
                    logger.info(f"-> [SZŰRŐ] Ingóság kihagyva (Becsérték > {MAX_INGOSAG_BECSERTEK} HUF): {a.get('cim')} ({a.get('becsertek')})")
                    continue
        # ------------------------

        if is_real_estate:
            token = REAL_ESTATE_BOT_TOKEN
            chat_id = REAL_ESTATE_CHAT_ID
            logger.info(f"-> [INGATLAN ROUTING] Küldés az Ingatlan Botnak: {a.get('teljes_cim')}")
        else:
            token = BOT_TOKEN
            chat_id = CHAT_ID
            logger.info(f"-> [INGÓSÁG ROUTING] Küldés az Ingóság Botnak: {a.get('cim')}")

        if token and chat_id:
            send_auction_message(a, token, chat_id, is_real_estate=is_real_estate)
            seen_urls.add(a["url"])
        else:
            logger.error(
                f"Kihagyva! Hiányzó token vagy chat_id (Ingatlan volt? {is_real_estate})"
            )

    # =====================================================================
    # 2. MNV EAR feldolgozás – közvetlenül a hírlevélből, scraping nélkül
    # =====================================================================
    all_mnv = []
    for html in mnv_ear_htmls:
        all_mnv.extend(parse_mnv_ear_auctions(html))

    logger.info(f"Összes MNV EAR tétel az e-mail(ek)ből: {len(all_mnv)}")

    for a in all_mnv:
        mnv_id = a.get("mnv_id")
        mnv_key = f"mnv_ear_{mnv_id}" if mnv_id else None

        # Duplikát ellenőrzés
        if mnv_key and mnv_key in seen_urls:
            logger.info(f"Már feldolgozott MNV tétel kihagyása: {a.get('tetel_nev_azonosito')}")
            continue

        # Kikiáltási ár szűrés – csak 2 M Ft alattiak
        kikialtas_int = parse_price_to_int(a.get("kikialtas_ar", ""))
        if kikialtas_int == 0:
            logger.warning(f"-> [MNV] Nem sikerült az árat értelmezni: {a.get('tetel_nev_azonosito')}")
        if kikialtas_int >= MAX_MNV_KIKIALTAS:
            logger.info(
                f"-> [SZŰRŐ] MNV tétel kihagyva (Kikiáltási ár {kikialtas_int:,} Ft >= {MAX_MNV_KIKIALTAS:,} Ft): "
                f"{a.get('tetel_nev_azonosito')}"
            )
            continue

        logger.info(
            f"-> [MNV ROUTING] Küldés az Ingatlan Botnak: "
            f"{a.get('tetel_nev_azonosito')} | {a.get('kikialtas_ar')}"
        )

        if REAL_ESTATE_BOT_TOKEN and REAL_ESTATE_CHAT_ID:
            msg = build_mnv_ear_message(a)
            send_via_requests(msg, None, REAL_ESTATE_BOT_TOKEN, REAL_ESTATE_CHAT_ID)
            if mnv_key:
                seen_urls.add(mnv_key)
        else:
            logger.error("Kihagyva! Hiányzó REAL_ESTATE_BOT_TOKEN vagy REAL_ESTATE_CHAT_ID")

    save_seen_urls(seen_urls)
    logger.info("=== SCRAPER SIKERESEN LEFUTOTT ===")


if __name__ == "__main__":
    main()
