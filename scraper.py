import os
import json
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import logging
import io
import re
import socket

# Globális időtúllépés beállítása (45 másodperc)
socket.setdefaulttimeout(45)

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")

# Alap bot az INGÓSÁGOKNAK
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Új bot az INGATLANOKNAK
REAL_ESTATE_BOT_TOKEN = os.environ.get("REAL_ESTATE_BOT_TOKEN")
REAL_ESTATE_CHAT_ID = os.environ.get("REAL_ESTATE_CHAT_ID")

# Távolságszámítási kiindulópont koordinátái
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


# =================== Segédfüggvények & Térkép ===================

def clean_text(text):
    return " ".join(text.split()) if text else ""


def escape_html(text):
    if not text:
        return ""
    # Szigorúbb csere, hogy a Telegram parser semmiképp se akadjon meg rajta
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
    candidates = [address]
    cleaned = re.sub(r",?\s*\d+(/\d+)?\s*hrsz\.?", "", address, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(külterület|belterület|tanya)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().rstrip(",").strip()
    if cleaned and cleaned != address:
        candidates.append(cleaned)

    city_match = re.match(r"(\d{4}\s+[A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ][A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ\s\-]+?)(?:\s*,|\s+\d|\s+külterület|\s+belterület|$)", cleaned or address)
    if city_match:
        city_only = city_match.group(1).strip()
        if city_only not in candidates:
            candidates.append(city_only)
    return [c.strip() for c in candidates if c.strip()]


def geocode_address(address):
    import time
    candidates = simplify_address(address)
    headers = {"User-Agent": "NAV-EAF-Scraper-V2/1.0"}
    for candidate in candidates:
        try:
            logger.info(f" -> Geokódolás megkísérlése ezzel: {candidate}")
            params = {"q": candidate, "format": "json", "limit": 1, "countrycodes": "hu"}
            resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=10)
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
        url = f"http://router.project-osrm.org/route/v1/driving/{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}?overview=false"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                return round(route["distance"] / 1000, 1), round(route["duration"] / 60)
    except Exception as e:
        logger.warning(f"Távolságszámítási hiba: {e}")
    return None


def scrape_main_image(url, soup):
    BASE = "https://arveres.nav.gov.hu/"
    try:
        for img_tag in soup.find_all("img", fullurl=True):
            fullurl = img_tag.get("fullurl", "").strip()
            if not fullurl:
                continue
            return fullurl if fullurl.startswith("http") else BASE + fullurl.lstrip("/")
    except Exception:
        return None


def parse_nav_eaf_details(url):
    logger.info(f"NAV oldal letöltése: {url}")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=15, headers=headers)
        resp.encoding = "ISO-8859-2"
        html_text = resp.text
    except Exception as e:
        logger.error(f"Nem sikerült letölteni a NAV oldalt: {e}")
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    data = {"url": url}

    megye_match = re.search(r'([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ\-]+)\s+(?:Vár)?megye', html_text)
    if megye_match:
        data["megye"] = megye_match.group(1).strip() + " vármegye"

    for div in soup.find_all("div", class_="FrissPortlet"):
        header = div.find("div", class_="HeaderTitle")
        if not header:
            continue
        header_text = header.get_text()
        
        if "Árverés alapadatok" in header_text or "Árverezett tétel adatok" in header_text:
            table = div.find("table", class_="DownloadAppsList")
            if table:
                for row in table.find_all("tr", class_="Bg2"):
                    cells = row.find_all("td")
                    if len(cells) >= 2:
                        key = clean_text(cells[0].get_text())
                        value = clean_text(cells[1].get_text())
                        if "Árverés megnevezése" in key: data["kategoria"] = value
                        elif "Végrehajtási ügyszám" in key: data["ugyintezesi_szam"] = value
                        elif "Árverés kategória" in key: data["kategoria_reszletes"] = value
                        elif "Árverés kezdete" in key: data["kezdet"] = value
                        elif "Árverés befejezése" in key: data["befejezes"] = value
                        elif "Az árverezett tétel megtekinthető, hely" in key: data["megtekintes_hely"] = value
                        elif "Tétel megnevezése" in key: data["tetel_megnevezes"] = value
                        elif "Becsérték" in key: data["becsertek"] = value
                        elif "Minimál ajánlat" in key: data["minimal_ajanlat"] = value
                        elif "Egyszerre árverezett tétel darabszám" in key: data["darabszam"] = value
                        elif "Állapot" in key: data["allapot"] = value
                        elif "Egyéb infó" in key: data["egyeb_info"] = value
                        elif "Cím irányítószám, város" in key: data["Cím irányítószám, város"] = value
                        elif "Cím utca" in key: data["Cím utca"] = value
                        elif "Házszám, emelet, ajtó" in key: data["Házszám, emelet, ajtó"] = value
                        elif "Az árverezett tétel megtekinthető, idő" in key: data["megtekintes_ido"] = value
   

    data["image_url"] = scrape_main_image(url, soup)

    megtekintes_hely = data.get("megtekintes_hely", "")
    if megtekintes_hely and "ingatlan" not in megtekintes_hely.lower():
        coords = geocode_address(megtekintes_hely)
        if coords:
            lat, lon = coords
            data["maps_url"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            result = get_drive_distance(coords)
            data["tavolsag"] = f"{result[0]} km ({result[1]} perc autóval)" if result else "Nem sikerült kiszámítani"
        else:
            data["tavolsag"] = "N/A"
    else:
        data["tavolsag"] = "N/A"

    data["cim"] = data.get("tetel_megnevezes") or data.get("kategoria_reszletes") or "Ismeretlen tétel"
    return data


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
            if status != "OK": continue
            
            msg = email.message_from_bytes(msg_data[0][1])
            subject_parts = decode_header(msg.get("Subject", ""))
            subject_str = "".join([p.decode(e or "utf-8", errors="ignore") if isinstance(p, bytes) else p for p, e in subject_parts])
            
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


# =================== Üzenetküldés Dinamikus Bot Választással ===================

def send_via_requests(caption, image_url, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id:
        logger.error("Hiba: Hiányzó Telegram token vagy chat ID!")
        return

    if image_url:
        try:
            img_resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if img_resp.status_code == 200 and "image" in img_resp.headers.get("Content-Type", ""):
                url = f"https://api.telegram.org/bot{target_bot_token}/sendPhoto"
                files = {'photo': ('image.jpg', img_resp.content, 'image/jpeg')}
                data = {'chat_id': target_chat_id, 'caption': caption, 'parse_mode': 'HTML'}
                
                resp = requests.post(url, files=files, data=data, timeout=20)
                if resp.status_code == 200:
                    logger.info("Sikeresen kiküldve képpel együtt.")
                    return
                logger.warning(f"Képküldés API hiba: {resp.text}")
        except Exception as e:
            logger.warning(f"Nem sikerült a képet küldeni, megpróbáljuk sima szövegként: {e}")

    try:
        url = f"https://api.telegram.org/bot{target_bot_token}/sendMessage"
        data = {'chat_id': target_chat_id, 'text': caption, 'parse_mode': 'HTML', 'disable_web_page_preview': False}
        resp = requests.post(url, data=data, timeout=20)
        if resp.status_code == 200:
            logger.info("Sikeresen kiküldve sima szövegként.")
        else:
            logger.error(f"Telegram küldési hiba: {resp.text}")
    except Exception as e:
        logger.error(f"Nem sikerült kommunikálni a Telegram API-val: {e}")


def send_auction_message(a: dict, target_bot_token, target_chat_id, is_real_estate: bool):
    # 1. Előkészítés
    leiras_nyers = a.get("egyeb_info", "")
    if len(leiras_nyers) > 400:
        leiras_nyers = leiras_nyers[:400] + "…"

    leiras = escape_html(leiras_nyers)
    becsertek = escape_html(a.get('becsertek', '0 HUF'))
    minimal_ajanlat = escape_html(a.get('minimal_ajanlat', '0 HUF'))
    kezdet = escape_html(a.get('kezdet', 'N/A'))
    befejezes = escape_html(a.get('befejezes', 'N/A'))
    darabszam = escape_html(a.get('darabszam', ''))
    
    # Árverés neve a fejlécbe
    arveres_nev = escape_html(a.get('kategoria_reszletes', a.get('kategoria', 'Árverés')))

    # Cím és helyszín kezelése
    varos_resz = a.get('Cím irányítószám, város', a.get('város', '')).strip()
    utca_resz = a.get('Cím utca', a.get('utca', '')).strip()
    hazszam_resz = a.get('Házszám, emelet, ajtó', a.get('házszám', '')).strip()
    teljes_cim = f"{varos_resz}, {utca_resz} {hazszam_resz}".strip(", ").replace("  ", " ")
    if not teljes_cim or teljes_cim == ",":
        teljes_cim = a.get('megtekintes_hely', 'Ismeretlen helyszín')

    # A "Cím" mezőbe most a tétel neve kerül
    tetel_nev = escape_html(a.get('cim', 'Ismeretlen tétel'))

    megye = escape_html(a.get("megye", ""))
    tavolsag = a.get("tavolsag", "")
    allapot = escape_html(a.get('allapot', a.get('kategoria_reszletes', 'Egyéb')))

    # 2. Üzenet felépítése
    fejlec = f"🔔 <b>{arveres_nev}</b>"
    
    lines = [
        fejlec, "",
        "🌍 <b>1. Elhelyezkedés és Alapadatok</b>",
        f"📍 <b>Tétel:</b> {tetel_nev}"
    ]
    
    if megye: lines.append(f"🏛 <b>Megye:</b> {megye}")
    if tavolsag and "Nem sikerült" not in tavolsag and tavolsag != "N/A": 
        lines.append(f"🗺 <b>Budapest-távolság:</b> {tavolsag}")
    
    lines.extend([
        "",
        "🏠 <b>2. A Tétel Jellemzői</b>",
        f"🚪 <b>Állapot:</b> {allapot}"
    ])
    if darabszam: lines.append(f"🔢 <b>Darabszám:</b> {darabszam}")
    
    lines.extend([
        "",
        "💰 <b>3. Pénzügyi Információk</b>",
        f"💵 <b>Becsérték:</b> {becsertek}",
        f"📉 <b>Minimál ajánlat:</b> {minimal_ajanlat}",
        "",
        "📅 <b>4. Időpontok és Árverési Státusz</b>",
        f"▶️ <b>Kezdés:</b> {kezdet}",
        f"⬜ <b>Befejezés:</b> {befejezes}",
        f"📍 <b>Helyszín:</b> {escape_html(teljes_cim)}"
    ])
    
    if a.get("megtekintes_ido"):
        lines.append(f"🕒 <b>Megtekintés:</b> {escape_html(a.get('megtekintes_ido'))}")
    
    if leiras:
        lines.extend(["", f"📝 <b>Leírás:</b>", f"<i>{leiras}</i>"])
        
    lines.extend(["", f"🔗 <a href='{a.get('url', '')}'>Részletek a NAV oldalon</a>"])
    
    if a.get("maps_url"): 
        lines.append(f"🗺 <a href='{a.get('maps_url')}'>Google Térkép</a>")

    caption = "\n".join(lines)
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
        kategoria_szoveg = (a.get("kategoria", "") + " " + a.get("kategoria_reszletes", "")).lower()
        is_real_estate = "ingatlan" in kategoria_szoveg
        
        if is_real_estate:
            token = REAL_ESTATE_BOT_TOKEN
            chat_id = REAL_ESTATE_CHAT_ID
            logger.info(f"-> [INGATLAN ROUTING] Küldés az Ingatlan Botnak: {a.get('cim')}")
        else:
            token = BOT_TOKEN
            chat_id = CHAT_ID
            logger.info(f"-> [INGÓSÁG ROUTING] Küldés az Ingóság Botnak: {a.get('cim')}")
            
        if token and chat_id:
            send_auction_message(a, token, chat_id, is_real_estate=is_real_estate)
            seen_urls.add(a["url"])
        else:
            logger.error(f"Kihagyva! Hiányzó token vagy chat_id ehhez a típushoz (Ingatlan volt? {is_real_estate})")
        
    save_seen_urls(seen_urls)
    logger.info("=== SCRAPER SIKERESEN LEFUTOTT ===")


if __name__ == "__main__":
    main()
