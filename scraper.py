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

def clean_text(text):
    return " ".join(text.split()) if text else ""


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        # Elsőként fullurl attribútumú képek
        for img_tag in soup.find_all("img"):
            fullurl = img_tag.get("fullurl", "").strip()
            if fullurl:
                return fullurl if fullurl.startswith("http") else BASE + "/" + fullurl.lstrip("/")
        # Fallback: képgaléria bármely img src
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

# Mezők, amelyeket az "Árverés alapadatok" és "Árverezett tétel adatok" táblákból kiolvasunk.
# Kulcs: a táblában szereplő szöveg (részben), Érték: data dict kulcsa
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

    # --- Megye kinyerése a szövegből (fallback) ---
    megye_match = re.search(
        r'([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ\-]+)\s+(?:Vár)?megye',
        html_text
    )
    if megye_match:
        data["megye"] = megye_match.group(1).strip() + " vármegye"

    # --- Fő táblák feldolgozása ---
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
            # Fallback: bármely tábla a divben
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

            # Speciális eset: "Bejegyzések a tulajdoni lapon" – hosszabb szöveg
            if not matched and "Bejegyzések a tulajdoni lapon" in key:
                data["tulajdoni_lap_bejegyzesek"] = value

            if not matched and "Egyéb megjegyzések" in key:
                data["egyeb_megjegyzesek"] = value

    # --- Megye egységesítése (táblából vagy regex-ből) ---
    if "megye_tabla" in data and data["megye_tabla"]:
        raw = data["megye_tabla"]
        # NAV-on néha csak a vármegye neve szerepel „Szolnok" formában
        if "vármegye" not in raw.lower() and "megye" not in raw.lower():
            data["megye"] = raw + " vármegye"
        else:
            data["megye"] = raw
    # Ha a regex talált valamit és a tábla nem, hagyjuk a regex eredményt

    # --- Teljes cím összeállítása ---
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

    # --- Kép ---
    data["image_url"] = scrape_main_image(soup)

    # --- Geocódolás és távolság (ingatlanoknál a tényleges cím alapján) ---
    geocode_input = data.get("teljes_cim") or data.get("megtekintes_hely", "")

    # ingatlanoknál NE szűrjük ki az "ingatlan címén" értéket – ilyenkor a teljes_cim-et használjuk
    if geocode_input and geocode_input.lower() not in ("ingatlan címén", ""):
        coords = geocode_address(geocode_input)
        if coords:
            lat, lon = coords
            data["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            result = get_drive_distance(coords)
            data["tavolsag"] = (
                f"{result[0]} km ({result[1]} perc autóval)" if result
                else "Nem sikerült kiszámítani"
            )
        else:
            data["tavolsag"] = "N/A"
    else:
        # "ingatlan címén" esetén a teljes_cim alapján próbálunk geocódolni
        if data.get("teljes_cim"):
            coords = geocode_address(data["teljes_cim"])
            if coords:
                lat, lon = coords
                data["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
                result = get_drive_distance(coords)
                data["tavolsag"] = (
                    f"{result[0]} km ({result[1]} perc autóval)" if result
                    else "Nem sikerült kiszámítani"
                )
            else:
                data["tavolsag"] = "N/A"
        else:
            data["tavolsag"] = "N/A"

    # --- Megjelenítési cím / tétel neve ---
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
            return []

        msg_ids = messages[0].split()
        logger.info(f"Talált olvasatlan e-mailek száma összesen: {len(msg_ids)}")

        result = []
        for idx, eid in enumerate(msg_ids, 1):
            logger.info(f" -> [{idx}/{len(msg_ids)}] Olvasatlan e-mail letöltése (ID: {eid.decode()})...")
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject_parts = decode_header(msg.get("Subject", ""))
            subject_str = "".join([
                p.decode(e or "utf-8", errors="ignore") if isinstance(p, bytes) else p
                for p, e in subject_parts
            ])

            if "Elektronikus Árverés" in subject_str or "Elektronikus Arveres" in subject_str:
                logger.info(f"    * Találat: Árverési levél! Tárgy: {subject_str}")
                html_body = extract_html_from_message(msg)
                if html_body:
                    result.append(html_body)

            mail.store(eid, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()
        return result
    except Exception as e:
        logger.error(f"IMAP hiba történt: {e}")
        return []


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
            "disable_web_page_preview": False,
        }
        resp = requests.post(url, data=data, timeout=20)
        if resp.status_code == 200:
            logger.info("Sikeresen kiküldve sima szövegként.")
        else:
            logger.error(f"Telegram küldési hiba: {resp.text}")
    except Exception as e:
        logger.error(f"Nem sikerült kommunikálni a Telegram API-val: {e}")


def build_ingatlan_message(a: dict) -> str:
    """MBVK-stílusú Telegram üzenet ingatlan árverésekhez."""

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
    ugyintezesi_szam = escape_html(a.get("ugyintezesi_szam") or "")

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
        # Az előleg szövege hosszú lehet, csak az összeget emeljük ki
        eloleg_match = re.search(r"(\d[\d\s]*(?:HUF|Ft))", a.get("arveres_eloleg", ""))
        if eloleg_match:
            lines.append(f"💳 <b>Árverési előleg:</b> {escape_html(eloleg_match.group(1))}")

    lines.append("")
    lines.append("⚖️ <b>4. Jogi és Árverési Státusz</b>")
    if ugyintezesi_szam:
        lines.append(f"📁 <b>Ügyszám:</b> {ugyintezesi_szam}")
    lines.append(f"▶️ <b>Árverés kezdete:</b> {kezdet}")
    lines.append(f"🏁 <b>Árverés vége:</b> {befejezes}")
    if megtekintes_ido:
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
    """Ingóság árverési Telegram üzenet."""

    arveres_nev = escape_html(a.get("kategoria_reszletes") or a.get("kategoria") or "Árverés")
    tetel_nev = escape_html(a.get("cim") or "Ismeretlen tétel")
    becsertek = escape_html(a.get("becsertek") or "N/A")
    minimal_ajanlat = escape_html(a.get("minimal_ajanlat") or "N/A")
    kezdet = escape_html(a.get("kezdet") or "N/A")
    befejezes = escape_html(a.get("befejezes") or "N/A")
    allapot = escape_html(a.get("allapot") or "")
    darabszam = escape_html(a.get("darabszam") or "")
    megye = escape_html(a.get("megye") or "")
    tavolsag = a.get("tavolsag", "")
    ugyintezesi_szam = escape_html(a.get("ugyintezesi_szam") or "")
    megtekintes_ido = escape_html(a.get("megtekintes_ido") or "")

    varos = a.get("varos", "").strip()
    utca = a.get("utca", "").strip()
    hazszam = a.get("hazszam", "").strip()
    teljes_cim = escape_html(
        a.get("teljes_cim") or a.get("megtekintes_hely") or "Ismeretlen helyszín"
    )

    egyeb_info = a.get("egyeb_info") or ""
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
        "",
        "📅 <b>4. Időpontok és Árverési Státusz</b>",
    ])
    if ugyintezesi_szam:
        lines.append(f"📁 <b>Ügyszám:</b> {ugyintezesi_szam}")
    lines.append(f"▶️ <b>Kezdés:</b> {kezdet}")
    lines.append(f"🏁 <b>Befejezés:</b> {befejezes}")
    if megtekintes_ido:
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

    emails_html = get_emails_since(since)
    if not emails_html:
        logger.info("Nincs új, olvasatlan feldolgozandó e-mail.")
        return

    all_auctions = []
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        for link in links:
            if link not in seen_urls:
                details = parse_nav_eaf_details(link)
                if details:
                    all_auctions.append(details)
            else:
                logger.info(f"Már feldolgozott link kihagyása: {link}")

    unique_auctions = list({a["url"]: a for a in all_auctions}.values())
    logger.info(f"Összes új feldolgozandó tétel száma: {len(unique_auctions)}")

    for a in unique_auctions:
        kategoria_szoveg = (
            (a.get("kategoria") or "") + " " + (a.get("kategoria_reszletes") or "")
        ).lower()
        is_real_estate = "ingatlan" in kategoria_szoveg

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

    save_seen_urls(seen_urls)
    logger.info("=== SCRAPER SIKERESEN LEFUTOTT ===")


if __name__ == "__main__":
    main()
