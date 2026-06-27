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

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    logging.warning("rapidfuzz nem elérhető! Futtasd: pip install rapidfuzz")

socket.setdefaulttimeout(45)

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

REAL_ESTATE_BOT_TOKEN = os.environ.get("REAL_ESTATE_BOT_TOKEN")
REAL_ESTATE_CHAT_ID = os.environ.get("REAL_ESTATE_CHAT_ID")

ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198

SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")
GEOCODE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "geocode_cache.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ------------------- SZŰRŐK (BÁRMIKOR MÓDOSÍTHATÓ) -------------------
CSAK_1_1_TULAJDON = True
MAX_INGOSAG_BECSERTEK = 2000000
MAX_MNV_INGATLAN_KIKIALTAS = 2000000   # MNV ingatlan kikiáltási ár limit
MAX_MNV_INGOSAG_KIKIALTAS = 2000000    # MNV ingóság kikiáltási ár limit

# Maximális távolság Budapesttől km-ben. None = kikapcsolva
MAX_TAVOLSAG_KM = None

# Kulcsszó szűrők (NAV ingóságnál). INGOSAG_WHITELIST üres = nincs whitelist szűrés.
INGOSAG_WHITELIST: list[str] = []
INGOSAG_BLACKLIST: list[str] = ["alkatrész", "sérült", "törött"]

# MNV EAR: ingatlan kategóriák (ami NEM szerepel itt, ingóságnak számít)
MNV_INGATLAN_KATEGORIAK: list[str] = [
    "ingatlan", "lakás", "ház", "telek", "garázs", "üzlet", "épület",
    "iroda", "tanya", "föld", "nyaraló", "pince", "műhely", "csarnok",
]

FUZZY_THRESHOLD = 70

# Ennyi nap után törlődnek a seen_urls bejegyzések
SEEN_URLS_EXPIRY_DAYS = 90

NAV_SENDER_DOMAINS = ["nav.gov.hu", "mnv.hu"]


# =================== Seen URLs – timestamp-alapú, lejáratos ===================

def load_seen_urls() -> dict:
    """
    Visszaad egy {url: iso_timestamp} dict-et.
    Régi lista-formátumú seen_urls.json-t automatikusan migrál.
    """
    if not os.path.exists(SEEN_URLS_FILE):
        return {}
    try:
        with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("seen_urls", {})
        if isinstance(raw, list):
            logger.info("seen_urls.json régi lista-formátum – migrálás timestamp-es dict-re.")
            now = datetime.now(timezone.utc).isoformat()
            return {url: now for url in raw}
        return raw
    except Exception as e:
        logger.error(f"Látott URL-ek betöltési hiba: {e}")
        return {}


def save_seen_urls(seen: dict):
    try:
        with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_urls": seen}, f, ensure_ascii=False, indent=2)
        logger.info(f"Látott URL-ek elmentve ({len(seen)} db).")
    except Exception as e:
        logger.error(f"Látott URL-ek mentési hiba: {e}")


def mark_seen(seen: dict, url: str, save: bool = True):
    """Hozzáad egy URL-t a seen dict-hez és opcionálisan azonnal menti."""
    seen[url] = datetime.now(timezone.utc).isoformat()
    if save:
        save_seen_urls(seen)


def clean_expired_seen_urls(seen: dict) -> dict:
    """Törli a SEEN_URLS_EXPIRY_DAYS napnál régebbi bejegyzéseket."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_URLS_EXPIRY_DAYS)
    cleaned = {}
    removed = 0
    for url, ts in seen.items():
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                cleaned[url] = ts
            else:
                removed += 1
        except Exception:
            cleaned[url] = ts  # ha nem értelmezhető a dátum, megtartjuk
    if removed:
        logger.info(f"Lejárt seen_urls bejegyzések törölve: {removed} db")
    return cleaned


# =================== Geocoding cache ===================

def load_geocode_cache() -> dict:
    if not os.path.exists(GEOCODE_CACHE_FILE):
        return {}
    try:
        with open(GEOCODE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Geocoding cache betöltési hiba: {e}")
        return {}


def save_geocode_cache(cache: dict):
    try:
        with open(GEOCODE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Geocoding cache mentési hiba: {e}")


# =================== Retry-képes HTTP hívás ===================

def fetch_with_retry(url: str, retries: int = 3, backoff: int = 2, **kwargs) -> requests.Response:
    """requests.get wrapper exponenciális visszalépéssel."""
    last_exc = None
    for attempt in range(retries):
        try:
            return requests.get(url, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                wait = backoff ** attempt
                logger.warning(f"HTTP hiba ({attempt + 1}/{retries}): {e} – újrapróbálás {wait}s múlva")
                time.sleep(wait)
    raise last_exc


# =================== Segédfüggvények ===================

def generate_gcal_url(title, date_str, location="", details=""):
    if not date_str or date_str == "N/A":
        return None

    try:
        import zoneinfo
        bp_tz = zoneinfo.ZoneInfo("Europe/Budapest")
    except Exception:
        bp_tz = None

    try:
        matches = re.findall(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})', date_str)
        if not matches:
            return None

        start_dt = datetime.strptime(matches[0], "%Y-%m-%d %H:%M")
        end_dt = (
            datetime.strptime(matches[1], "%Y-%m-%d %H:%M")
            if len(matches) >= 2
            else start_dt + timedelta(hours=1)
        )

        if bp_tz:
            start_dt = start_dt.replace(tzinfo=bp_tz).astimezone(timezone.utc)
            end_dt = end_dt.replace(tzinfo=bp_tz).astimezone(timezone.utc)

        start_str = start_dt.strftime("%Y%m%dT%H%M%SZ")
        end_str = end_dt.strftime("%Y%m%dT%H%M%SZ")

        url = "https://calendar.google.com/calendar/render?action=TEMPLATE"
        url += f"&text={urllib.parse.quote(title)}"
        url += f"&dates={start_str}/{end_str}"
        url += "&ctz=Europe/Budapest"
        if location and location != "N/A":
            url += f"&location={urllib.parse.quote(location)}"
        if details:
            # A details-ben lévő URL-ek & karaktereit %26-ra cseréljük,
            # hogy a GCal ne vágja el az auctionId-t és más paramétereket.
            safe_details = re.sub(
                r'(https?://\S+)',
                lambda m: m.group(0).replace('&', '%26'),
                details
            )
            url += f"&details={urllib.parse.quote(safe_details)}"
        return url
    except Exception as e:
        logger.warning(f"Nem sikerült a naptár link generálása: {e}")
        return None


def clean_text(text):
    return " ".join(text.split()) if text else ""


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_price_to_int(price_str):
    if not price_str:
        return 0
    cleaned = re.sub(r'[^\d]', '', price_str.replace('\xa0', ''))
    return int(cleaned) if cleaned else 0


def extract_km_from_tavolsag(tavolsag_str: str) -> float | None:
    """Kinyeri a km értéket a tavolsag stringből, pl. '42.3 km (31 perc autóval)' → 42.3"""
    if not tavolsag_str:
        return None
    m = re.search(r'([\d.]+)\s*km', tavolsag_str)
    return float(m.group(1)) if m else None


def check_keywords(name: str) -> tuple[bool, str | None]:
    """
    Ellenőrzi a whitelist/blacklist szűrőket egy ingóság nevén.
    Visszaad: (átment-e, kihagyás oka vagy None)
    """
    name_lower = name.lower()
    if INGOSAG_WHITELIST:
        if not any(w.lower() in name_lower for w in INGOSAG_WHITELIST):
            return False, "nem szerepel a whitelist-en"
    for b in INGOSAG_BLACKLIST:
        if b.lower() in name_lower:
            return False, f"blacklist ({b})"
    return True, None


def calculate_darabar(price_str, db_str):
    if not price_str or not db_str:
        return None
    try:
        p_match = re.search(r"(\d+)", price_str.replace(" ", "").replace("\xa0", ""))
        d_match = re.search(r"(\d+)", db_str.replace(" ", "").replace("\xa0", ""))
        if p_match and d_match:
            price = int(p_match.group(1))
            db = int(d_match.group(1))
            if db > 1:
                return f"{round(price / db):,} HUF/db".replace(",", " ")
    except Exception as e:
        logger.warning(f"Nem sikerült a darabárat kiszámolni: {e}")
    return None


def remove_sablon_szoveg(text):
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


def _extract_megye_from_nominatim(address_block: dict) -> str | None:
    """Kinyeri a megye nevet a Nominatim addressdetails blokkból."""
    city = (
        address_block.get("city")
        or address_block.get("town")
        or address_block.get("municipality")
        or ""
    )
    county = address_block.get("county", "")
    if "Budapest" in city:
        return "Budapest"
    if county:
        return county  # pl. "Tolna vármegye"
    return None


def geocode_address(address):
    """
    Geokódol egy magyar címet.
    Visszaad: ((lat, lon), megye_str) vagy None ha nem sikerül.
    Cache: {"coords": [lat, lon], "megye": "..."} dict.
    Régi [lat, lon] lista bejegyzések backward-compatible módon kezelve.
    """
    cache = load_geocode_cache()
    candidates = simplify_address(address)

    for candidate in candidates:
        if candidate in cache:
            entry = cache[candidate]
            logger.info(f" -> Geokódolás cache-ből: {candidate}")
            if isinstance(entry, list):
                return tuple(entry), None
            return tuple(entry["coords"]), entry.get("megye")

    headers = {"User-Agent": "NAV-EAF-Scraper-V2/1.0"}
    for candidate in candidates:
        try:
            logger.info(f" -> Geokódolás megkísérlése ezzel: {candidate}")
            params = {
                "q": candidate,
                "format": "json",
                "limit": 1,
                "countrycodes": "hu",
                "addressdetails": 1,
            }
            resp = fetch_with_retry(
                "https://nominatim.openstreetmap.org/search",
                params=params, headers=headers, timeout=10
            )
            if resp.status_code == 200 and resp.json():
                result = resp.json()[0]
                coords = (float(result["lat"]), float(result["lon"]))
                megye = _extract_megye_from_nominatim(result.get("address", {}))
                cache[candidate] = {"coords": list(coords), "megye": megye}
                save_geocode_cache(cache)
                return coords, megye
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
        resp = fetch_with_retry(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                return round(route["distance"] / 1000, 1), round(route["duration"] / 60)
    except Exception as e:
        logger.warning(f"Távolságszámítási hiba: {e}")
    return None


def scrape_main_image(soup):
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
    "Ingatlan megnevezése":                     "ingatlan_megnevezes",
    "Tétel megnevezése":                        "tetel_megnevezes",
    "Becsérték":                                "becsertek",
    "Árverési előleg":                          "arveres_eloleg",
    "Minimál ajánlat":                          "minimal_ajanlat",
    "Egyéb infó":                               "egyeb_info",
    "Van előárverezésre jogosult":              "eloarverezesre_jogosult",
    "Ország":                                   "orszag",
    "Megye":                                    "megye_tabla",
    "Cím irányítószám, város":                  "varos",
    "Cím utca":                                 "utca",
    "Házszám, emelet, ajtó":                    "hazszam",
    "Tulajdoni hányad":                         "tulajdoni_hanyad",
    "Helyrajzi szám":                           "helyrajzi_szam",
    "Terület":                                  "terulet",
    "3.a Megközelíthetősége":                   "megkozelithetoseg",
    "5. Külön engedély nélkül beépíthető":      "beepitheto",
    "7. Talajának minősége":                    "talaj_minoseg",
    "8. Növényzete":                            "novenyzet",
    "9. Kerítése":                              "kerites",
    "Kerítés anyaga":                           "kerites_anyaga",
    "Állapot":                                  "allapot",
    "Egyszerre árverezett tétel darabszám":     "darabszam",
}


def parse_nav_eaf_details(url):
    logger.info(f"NAV oldal letöltése: {url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = fetch_with_retry(url, timeout=20, headers=headers)
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

        table = div.find("table", class_="DownloadAppsList") or div.find("table")
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
        geo_result = geocode_address(geocode_input)
        if geo_result:
            coords, geo_megye = geo_result
            lat, lon = coords
            data["maps_url"] = f"http://maps.google.com/?q={lat},{lon}"
            if not data.get("megye") and geo_megye:
                data["megye"] = geo_megye
            result = get_drive_distance(coords)
            data["tavolsag"] = (
                f"{result[0]} km ({result[1]} perc autóval)" if result
                else "Nem sikerült kiszámítani"
            )
        else:
            data["tavolsag"] = "N/A"
    else:
        if data.get("teljes_cim"):
            geo_result = geocode_address(data["teljes_cim"])
            if geo_result:
                coords, geo_megye = geo_result
                lat, lon = coords
                data["maps_url"] = f"http://maps.google.com/?q={lat},{lon}"
                if not data.get("megye") and geo_megye:
                    data["megye"] = geo_megye
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
    from_header = msg.get("From", "").lower()
    subj_l = subject_str.lower()
    html_l = (html_body or "").lower()

    mnv_signals = [
        any(d in from_header for d in ["mnv.hu"]),
        "mnv ear" in subj_l,
        "heti hírlevél" in subj_l,
        "heti hirlevél" in subj_l,
        "meghirdetett árverésekről" in subj_l,
        "no-reply-ear@mnv.hu" in html_l,
        "ear.mnv.hu" in html_l,
        ("mnv" in html_l and "ear" in html_l and "hírlevél" in html_l),
    ]
    if any(mnv_signals):
        return "mnv_ear"

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


# =================== MNV EAR hírlevél feldolgozása ===================

def is_mnv_ingatlan(alkategoria: str) -> bool:
    """True ha az MNV alkategória ingatlan, False ha ingóság."""
    a = alkategoria.lower()
    return any(k in a for k in MNV_INGATLAN_KATEGORIAK)


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
    if "tetel_nev_azonosito" not in auction_data or "kikialtas_ar" not in auction_data:
        return None
    raw_nev = auction_data.get("tetel_nev_azonosito", "")
    id_match = re.search(r'\[(\d+/\d+)\]', raw_nev)
    auction_data["mnv_id"] = id_match.group(1) if id_match else None
    cim_match = re.match(r'^(.*?)(?:\s*[\[\(])', raw_nev)
    auction_data["cim_rovid"] = cim_match.group(1).strip() if cim_match else raw_nev
    return auction_data


def parse_mnv_ear_auctions(html_content: str) -> list:
    soup = BeautifulSoup(html_content, "html.parser")
    raw_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    all_labels = {label for label, _ in MNV_LABEL_FIELD}

    def is_label(line: str) -> bool:
        return any(lbl in line for lbl in all_labels)

    def next_value(lines: list, idx: int) -> str:
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

            if field == "alkategoria" and current:
                result = _finalize_mnv_auction(current)
                if result:
                    auctions.append(result)
                current = {}

            after_label = line[line.index(label) + len(label):].strip().lstrip(":").strip()
            if after_label:
                current[field] = after_label
            else:
                val = next_value(lines, i)
                if val:
                    current[field] = val
                    i += 1
            break

        i += 1

    if current:
        result = _finalize_mnv_auction(current)
        if result:
            auctions.append(result)

    logger.info(f"MNV EAR: {len(auctions)} tétel kinyerve az e-mailből.")
    return auctions


def build_mnv_ear_message(a: dict) -> str:
    mnv_ingatlan = is_mnv_ingatlan(a.get("alkategoria", ""))
    alkategoria = escape_html(a.get("alkategoria") or ("Ingatlan" if mnv_ingatlan else "Ingóság"))
    tetel = escape_html(a.get("tetel_nev_azonosito") or "Ismeretlen")
    ar = escape_html(a.get("kikialtas_ar") or "N/A")
    meghirdetes = escape_html(a.get("meghirdetes") or "N/A")
    hatarido = escape_html(a.get("biztosítek_hatarido") or "")
    kezdet = escape_html(a.get("kezdet") or "N/A")
    befejezes = escape_html(a.get("befejezes") or "N/A")

    mnv_id = a.get("mnv_id")
    auction_link = "#"
    if mnv_id:
        match = re.match(r'(\d+)/', mnv_id)
        if match:
            auction_id = match.group(1)
            auction_link = f"https://e-arveres.mnv.hu//index-ingosag.html?.actionId=action.auction.AuctionSummaryAction&auctionId={auction_id}&FRAME_SKIP_DEJAVU=1"

    reszletek = f"Kikiáltási ár: {a.get('kikialtas_ar', 'N/A')} | MNV EAR árverés"
    kezdet_url = generate_gcal_url(f"MNV Árverés Kezdete: {a.get('cim_rovid', '')}", kezdet, "", reszletek)
    befejezes_url = generate_gcal_url(f"MNV Árverés Vége: {a.get('cim_rovid', '')}", befejezes, "", reszletek)
    hatarido_url = generate_gcal_url(f"MNV Biztosíték határideje: {a.get('cim_rovid', '')}", hatarido, "", reszletek) if hatarido else None

    tavolsag = a.get("tavolsag", "")
    megye = escape_html(a.get("megye") or "")

    header = "🏛 <b>MNV EAR INGATLAN TALÁLAT</b>" if mnv_ingatlan else "🔔 <b>MNV EAR INGÓSÁG TALÁLAT</b>"

    lines = [
        header,
        f"📋 <b>{alkategoria}</b>",
        "",
        "🌍 <b>1. Elhelyezkedés és Alapadatok</b>",
        f"🏷 <b>Tétel:</b> {tetel}",
    ]

    if megye:
        lines.append(f"🏛 <b>Megye:</b> {megye}")
    if tavolsag and tavolsag not in ("N/A", "Nem sikerült kiszámítani"):
        lines.append(f"🚗 <b>Budapest-távolság:</b> {tavolsag}")

    lines.extend([
        "",
        "💰 <b>2. Pénzügyi Információk</b>",
        f"💵 <b>Kikiáltási ár:</b> {ar}",
        "",
        "📅 <b>3. Időpontok</b>",
        f"📢 <b>Meghirdetve:</b> {meghirdetes}",
    ])

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

    lines.append("")
    if a.get("maps_url"):
        lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")
    lines.append(f"🔗 <a href='{auction_link}'>Megnyitás az MNV EAR rendszerben</a>")

    return "\n".join(lines)


# =================== Telegram üzenetküldés ===================

def send_via_requests(caption, image_url, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id:
        logger.error("Hiba: Hiányzó Telegram token vagy chat ID!")
        return

    if image_url and len(caption) <= 1024:
        try:
            img_resp = fetch_with_retry(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if img_resp.status_code == 200 and "image" in img_resp.headers.get("Content-Type", ""):
                url = f"https://api.telegram.org/bot{target_bot_token}/sendPhoto"
                files = {"photo": ("image.jpg", img_resp.content, "image/jpeg")}
                data = {"chat_id": target_chat_id, "caption": caption, "parse_mode": "HTML"}
                resp = requests.post(url, files=files, data=data, timeout=20)
                if resp.status_code == 200:
                    logger.info("Sikeresen kiküldve képpel együtt.")
                    return
                logger.warning(f"Képes küldés sikertelen: {resp.text}")
        except Exception as e:
            logger.warning(f"Nem sikerült a képet küldeni: {e}")

    elif image_url and len(caption) > 1024:
        try:
            img_resp = fetch_with_retry(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if img_resp.status_code == 200 and "image" in img_resp.headers.get("Content-Type", ""):
                url = f"https://api.telegram.org/bot{target_bot_token}/sendPhoto"
                files = {"photo": ("image.jpg", img_resp.content, "image/jpeg")}
                data = {"chat_id": target_chat_id, "parse_mode": "HTML"}
                resp = requests.post(url, files=files, data=data, timeout=20)
                if resp.status_code == 200:
                    logger.info("Kép sikeresen kiküldve (külön üzenetben).")
                else:
                    logger.warning(f"Kép küldése sikertelen: {resp.text}")
        except Exception as e:
            logger.warning(f"Nem sikerült a képet küldeni: {e}")

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
            logger.info("Szöveg sikeresen kiküldve.")
        else:
            logger.error(f"Telegram szöveg küldési hiba: {resp.text}")
    except Exception as e:
        logger.error(f"Nem sikerült kommunikálni a Telegram API-val: {e}")


def send_summary(stats: dict):
    """Napi összefoglaló küldése az ingóság bot csatornájára."""
    if not BOT_TOKEN or not CHAT_ID:
        return

    total_sent = (
        stats["elkuld_ingosag"] + stats["elkuld_ingatlan"]
        + stats["elkuld_mnv_ingatlan"] + stats["elkuld_mnv_ingosag"]
    )

    lines = [
        "📊 <b>Napi összefoglaló – Scraper V2.4</b>",
        "",
        f"📧 Feldolgozott e-mailek: {stats['emailek']}",
        f"🔍 Talált NAV tételek: {stats['nav_talalt']}",
        f"🔍 Talált MNV tételek: {stats['mnv_talalt']}",
        f"🚫 Szűrt tételek: {stats['szurt']}",
        f"✅ NAV ingóság: {stats['elkuld_ingosag']}",
        f"✅ NAV ingatlan: {stats['elkuld_ingatlan']}",
        f"✅ MNV ingatlan: {stats['elkuld_mnv_ingatlan']}",
        f"✅ MNV ingóság: {stats['elkuld_mnv_ingosag']}",
        f"<b>📨 Összesen elküldve: {total_sent}</b>",
    ]

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        logger.info("Napi összefoglaló elküldve.")
    except Exception as e:
        logger.warning(f"Összefoglaló küldési hiba: {e}")


# =================== Üzenetépítő függvények ===================

def build_ingatlan_message(a: dict, include_link: bool = True) -> str:
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

    egyeb_info = a.get("egyeb_info") or a.get("egyeb_megjegyzesek") or ""
    egyeb_info = remove_sablon_szoveg(egyeb_info)
    if len(egyeb_info) > 500:
        egyeb_info = egyeb_info[:500] + "…"
    egyeb_info = escape_html(egyeb_info)

    lines = [
        "🏠 <b>NAV INGATLAN TALÁLAT</b>",
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

    if include_link:
        lines.append("")
        lines.append(f"🔗 <a href='{a.get('url', '')}'>Részletek a NAV oldalon</a>")
        if a.get("maps_url"):
            lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")

    return "\n".join(lines)


def build_ingosag_message(a: dict, include_link: bool = True) -> str:
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

    darabar = calculate_darabar(minimal_ajanlat_raw, darabszam_raw)

    egyeb_info = a.get("egyeb_info") or ""
    egyeb_info = remove_sablon_szoveg(egyeb_info)
    if len(egyeb_info) > 400:
        egyeb_info = egyeb_info[:400] + "…"
    egyeb_info = escape_html(egyeb_info)

    lines = [
        "🔔 <b>NAV INGÓSÁG TALÁLAT</b>",
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

    if darabar:
        lines.append(f"💲 <b>Minimum darabár:</b> {darabar}")

    lines.extend([
        "",
        "📅 <b>4. Időpontok és Árverési Státusz</b>"
    ])

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

    if include_link:
        lines.extend(["", f"🔗 <a href='{a.get('url', '')}'>Részletek a NAV oldalon</a>"])
        if a.get("maps_url"):
            lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")

    return "\n".join(lines)


# =================== Fuzzy csoportosítás ===================

def normalize_for_fuzzy(name: str) -> str:
    if not name:
        return ""
    lower = name.lower().strip()
    cleaned = re.sub(
        r'\b\d+\s*(ml|g|kg|db|cm|mm|m|l|cl|dl|ft|huf|%|")\b',
        '',
        lower,
        flags=re.IGNORECASE
    )
    cleaned = re.sub(r'\b\d+\b', '', cleaned)
    cleaned = re.sub(r'[,.\-_/\\]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def group_auctions_by_similarity(auctions: list, threshold: int = FUZZY_THRESHOLD) -> dict:
    if not auctions:
        return {}

    normalized_names = []
    for a in auctions:
        raw = (
            a.get("cim")
            or a.get("ingatlan_megnevezes")
            or a.get("tetel_megnevezes")
            or a.get("kategoria_reszletes")
            or ""
        )
        normalized_names.append(normalize_for_fuzzy(raw))

    grouped = {}
    used = set()

    for i, a in enumerate(auctions):
        if i in used:
            continue

        group = [a]
        used.add(i)

        for j, b in enumerate(auctions):
            if j in used:
                continue

            if RAPIDFUZZ_AVAILABLE:
                score = fuzz.token_sort_ratio(normalized_names[i], normalized_names[j])
            else:
                score = 100 if normalized_names[i] == normalized_names[j] else 0

            if score >= threshold:
                logger.info(
                    f"  [FUZZY] Csoport: '{normalized_names[i]}' <-> '{normalized_names[j]}' "
                    f"({score}% hasonlóság) → összevonva"
                )
                group.append(b)
                used.add(j)

        key = normalized_names[i] or auctions[i]["url"]
        grouped[key] = group

    logger.info(f"Fuzzy csoportosítás eredménye: {len(auctions)} tételből {len(grouped)} csoport")
    return grouped


# =================== Fő logika ===================

def main():
    logger.info("=== SCRAPER V2.4 INDÍTÁSA ===")
    since = datetime.now(timezone.utc) - timedelta(days=1)

    seen_urls = load_seen_urls()
    seen_urls = clean_expired_seen_urls(seen_urls)

    stats = {
        "emailek": 0,
        "nav_talalt": 0,
        "mnv_talalt": 0,
        "szurt": 0,
        "elkuld_ingosag": 0,
        "elkuld_ingatlan": 0,
        "elkuld_mnv_ingatlan": 0,
        "elkuld_mnv_ingosag": 0,
    }

    nav_eaf_htmls, mnv_ear_htmls = get_emails_since(since)
    stats["emailek"] = len(nav_eaf_htmls) + len(mnv_ear_htmls)

    if not nav_eaf_htmls and not mnv_ear_htmls:
        logger.info("Nincs új, olvasatlan NAV/MNV e-mail feldolgozásra.")
        send_summary(stats)
        return

    # =====================================================================
    # 1. NAV EAF feldolgozás – scraping + fuzzy csoportosítás
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
    stats["nav_talalt"] = len(unique_auctions)
    logger.info(f"Összes új NAV EAF feldolgozandó tétel: {len(unique_auctions)}")

    # Szűrés
    filtered_auctions = []
    for a in unique_auctions:
        kategoria_szoveg = (
            (a.get("kategoria") or "") + " " + (a.get("kategoria_reszletes") or "")
        ).lower()
        is_real_estate = "ingatlan" in kategoria_szoveg

        if is_real_estate:
            if CSAK_1_1_TULAJDON:
                tulajdon = a.get("tulajdoni_hanyad", "")
                if "1/1" not in tulajdon:
                    logger.info(f"-> [SZŰRŐ] Ingatlan kihagyva (Nem 1/1 tulajdon): {a.get('teljes_cim')} ({tulajdon})")
                    stats["szurt"] += 1
                    continue
        else:
            # Becsérték szűrő
            if MAX_INGOSAG_BECSERTEK is not None:
                becsertek_int = parse_price_to_int(a.get("becsertek", ""))
                if becsertek_int > MAX_INGOSAG_BECSERTEK:
                    logger.info(f"-> [SZŰRŐ] Ingóság kihagyva (Becsérték > {MAX_INGOSAG_BECSERTEK} HUF): {a.get('cim')} ({a.get('becsertek')})")
                    stats["szurt"] += 1
                    continue

            # Kulcsszó szűrő
            name = a.get("cim") or ""
            ok, reason = check_keywords(name)
            if not ok:
                logger.info(f"-> [SZŰRŐ] Ingóság kihagyva ({reason}): {name}")
                stats["szurt"] += 1
                continue

        # Távolság szűrő (ingatlan + ingóság egyaránt)
        if MAX_TAVOLSAG_KM is not None:
            km = extract_km_from_tavolsag(a.get("tavolsag", ""))
            if km is not None and km > MAX_TAVOLSAG_KM:
                logger.info(f"-> [SZŰRŐ] Kihagyva (távolság: {km} km > {MAX_TAVOLSAG_KM} km): {a.get('cim')}")
                stats["szurt"] += 1
                continue

        filtered_auctions.append(a)

    logger.info(f"Szűrés után feldolgozandó tételek: {len(filtered_auctions)}")

    # Fuzzy csoportosítás normalizált név alapján
    grouped = group_auctions_by_similarity(filtered_auctions, threshold=FUZZY_THRESHOLD)

    for group_key, auctions_list in grouped.items():
        base = auctions_list[0]
        kategoria_szoveg = (
            (base.get("kategoria") or "") + " " + (base.get("kategoria_reszletes") or "")
        ).lower()
        is_real_estate = "ingatlan" in kategoria_szoveg

        if is_real_estate:
            token = REAL_ESTATE_BOT_TOKEN
            chat_id = REAL_ESTATE_CHAT_ID
            caption = build_ingatlan_message(base, include_link=False)
        else:
            token = BOT_TOKEN
            chat_id = CHAT_ID
            caption = build_ingosag_message(base, include_link=False)

        if base.get("maps_url"):
            caption += f"\n🗺 <a href='{base.get('maps_url')}'>Google Térkép</a>"

        if len(auctions_list) == 1:
            caption += f"\n\n🔗 <a href='{base['url']}'>Részletek a NAV oldalon</a>"
        else:
            caption += "\n\n📌 <b>Az összes ilyen tétel linkjei:</b>"
            for idx, a in enumerate(auctions_list, 1):
                caption += f"\n{idx}. <a href='{a['url']}'>Megtekintés a NAV oldalon</a>"

        if token and chat_id:
            send_via_requests(caption, base.get("image_url"), token, chat_id)
            # Azonnali mentés minden sikeres küldés után
            for a in auctions_list:
                mark_seen(seen_urls, a["url"], save=True)
            if is_real_estate:
                stats["elkuld_ingatlan"] += 1
            else:
                stats["elkuld_ingosag"] += 1
        else:
            logger.error(f"Kihagyva! Hiányzó token vagy chat_id (Ingatlan volt? {is_real_estate})")

    # =====================================================================
    # 2. MNV EAR feldolgozás
    # =====================================================================
    all_mnv = []
    for html in mnv_ear_htmls:
        all_mnv.extend(parse_mnv_ear_auctions(html))

    stats["mnv_talalt"] = len(all_mnv)
    logger.info(f"Összes MNV EAR tétel az e-mail(ek)ből: {len(all_mnv)}")

    for a in all_mnv:
        mnv_id = a.get("mnv_id")
        mnv_key = f"mnv_ear_{mnv_id}" if mnv_id else None

        if mnv_key and mnv_key in seen_urls:
            logger.info(f"Már feldolgozott MNV tétel kihagyása: {a.get('tetel_nev_azonosito')}")
            continue

        kikialtas_int = parse_price_to_int(a.get("kikialtas_ar", ""))
        if kikialtas_int == 0:
            logger.warning(f"-> [MNV] Nem sikerült az árat értelmezni: {a.get('tetel_nev_azonosito')}")

        # Típus meghatározása az alkategoria alapján
        mnv_ingatlan = is_mnv_ingatlan(a.get("alkategoria", ""))

        # Típusfüggő ár szűrő
        ar_limit = MAX_MNV_INGATLAN_KIKIALTAS if mnv_ingatlan else MAX_MNV_INGOSAG_KIKIALTAS
        if ar_limit is not None and kikialtas_int >= ar_limit:
            logger.info(
                f"-> [SZŰRŐ] MNV {'ingatlan' if mnv_ingatlan else 'ingóság'} kihagyva "
                f"(Kikiáltási ár {kikialtas_int:,} Ft >= {ar_limit:,} Ft): "
                f"{a.get('tetel_nev_azonosito')}"
            )
            stats["szurt"] += 1
            continue

        # Routing: ingatlan → ingatlan csatorna, ingóság → ingóság csatorna
        if mnv_ingatlan:
            mnv_token = REAL_ESTATE_BOT_TOKEN
            mnv_chat_id = REAL_ESTATE_CHAT_ID
        else:
            mnv_token = BOT_TOKEN
            mnv_chat_id = CHAT_ID

        logger.info(
            f"-> [MNV ROUTING] {'Ingatlan' if mnv_ingatlan else 'Ingóság'} csatornára: "
            f"{a.get('tetel_nev_azonosito')} | {a.get('kikialtas_ar')}"
        )

        # Geokódolás a cim_rovid alapján (cache-elt, nem lassít sokat)
        mnv_address = a.get("cim_rovid", "").strip()
        if mnv_address:
            geo_result = geocode_address(mnv_address)
            if geo_result:
                coords, geo_megye = geo_result
                lat, lon = coords
                a["maps_url"] = f"http://maps.google.com/?q={lat},{lon}"
                a["megye"] = geo_megye or ""
                result = get_drive_distance(coords)
                a["tavolsag"] = (
                    f"{result[0]} km ({result[1]} perc autóval)" if result
                    else "Nem sikerült kiszámítani"
                )
                logger.info(f"   [MNV GEO] {mnv_address} → {a['tavolsag']} | {a['megye']}")
            else:
                a["tavolsag"] = "N/A"
                a["megye"] = ""
                logger.info(f"   [MNV GEO] Nem sikerült: {mnv_address}")

        if mnv_token and mnv_chat_id:
            msg = build_mnv_ear_message(a)
            send_via_requests(msg, None, mnv_token, mnv_chat_id)
            if mnv_key:
                mark_seen(seen_urls, mnv_key, save=True)
            if mnv_ingatlan:
                stats["elkuld_mnv_ingatlan"] += 1
            else:
                stats["elkuld_mnv_ingosag"] += 1
        else:
            logger.error(
                f"Kihagyva! Hiányzó token/chat_id "
                f"({'ingatlan' if mnv_ingatlan else 'ingóság'} csatorna)"
            )

    send_summary(stats)
    logger.info("=== SCRAPER V2.4 SIKERESEN LEFUTOTT ===")


if __name__ == "__main__":
    main()
