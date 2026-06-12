import os
import json
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime, timezone
import logging
import copy
import io

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198
ORIGIN_LABEL = "Budapest XVII. ker. Sáránd utca"
SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

# =================== Színes Logolás Beállítása ===================
class ColorFormatter(logging.Formatter):
    def format(self, record):
        record = copy.copy(record)
        msg = str(record.msg)
        if "❌" in msg:
            record.msg = f"\033[91m{msg}\033[0m"  # Piros
        elif "✅" in msg:
            record.msg = f"\033[92m{msg}\033[0m"  # Zöld
        elif "⚠️" in msg:
            record.msg = f"\033[93m{msg}\033[0m"  # Sárga
        elif "Feldolgozva:" in msg:
            record.msg = f"\033[96m{msg}\033[0m"  # Cián
        return super().format(record)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setFormatter(ColorFormatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%y-%m-%d %H:%M:%S"))
logger.addHandler(ch)
logger.propagate = False

bot = Bot(token=BOT_TOKEN)

# =================== Látott URL-ek kezelése ===================
def load_seen_urls() -> set:
    if not os.path.exists(SEEN_URLS_FILE):
        return set()
    try:
        with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_urls", []))
    except Exception as e:
        logger.error(f"⚠️ Látott URL-ek betöltési hiba: {e}")
        return set()

def save_seen_urls(seen: set):
    try:
        with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_urls": sorted(seen)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"⚠️ Látott URL-ek mentési hiba: {e}")

def filter_new_auctions(auctions: list, seen: set) -> list:
    new = []
    for a in auctions:
        cim = a.get("cim", "Ismeretlen tétel")
        if a.get("url") and a["url"] not in seen:
            logger.info(f"✅ Értesítés küldése (új licit): {cim}")
            new.append(a)
        else:
            logger.info(f"❌ Nem ment át (szűrő - már látott): {cim}")
    return new

def group_auctions_by_category(auctions: list) -> dict:
    groups = {}
    for a in auctions:
        cat = a.get("kategoria_reszletes") or a.get("kategoria") or "Egyéb"
        cat_key = cat.split(" - ")[0].strip() if " - " in cat else cat
        groups.setdefault(cat_key, []).append(a)
    return dict(sorted(groups.items(), key=lambda x: -len(x[1])))

# =================== Segédfüggvények ===================
def clean_text(text):
    return " ".join(text.split()) if text else ""

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
    import re
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

    seen = []
    for c in candidates:
        if c.strip() and c.strip() not in seen:
            seen.append(c.strip())
    return seen

def geocode_address(address):
    import time
    candidates = simplify_address(address)
    headers = {"User-Agent": "NAV-EAF-Scraper/1.0"}

    for candidate in candidates:
        try:
            params = {"q": candidate, "format": "json", "limit": 1, "countrycodes": "hu"}
            resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=10)
            if resp.status_code == 200 and resp.json():
                return float(resp.json()[0]["lat"]), float(resp.json()[0]["lon"])
            time.sleep(1.1)
        except Exception:
            time.sleep(1.1)
    return None

def get_drive_distance(dest_address):
    coords = geocode_address(dest_address)
    if not coords:
        return None
    dest_lat, dest_lon = coords
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}?overview=false"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            return round(route["distance"] / 1000, 1), round(route["duration"] / 60)
    except Exception:
        pass
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
        pass
    return None

def download_image(image_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(image_url, timeout=20, headers=headers)
        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", ""):
            return resp.content
    except Exception:
        pass
    return None

def parse_nav_eaf_details(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, timeout=30, headers=headers)
        resp.encoding = "ISO-8859-2"
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.error(f"⚠️ Hiba az oldal betöltésekor: {url} - {e}")
        return None

    data = {"url": url}
    
    for div in soup.find_all("div", class_="FrissPortlet"):
        header = div.find("div", class_="HeaderTitle")
        if not header: continue
        
        table = div.find("table", class_="DownloadAppsList")
        if not table: continue

        if "Árverés alapadatok" in header.get_text():
            for row in table.find_all("tr", class_="Bg2"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    key, value = clean_text(cells[0].get_text()), clean_text(cells[1].get_text())
                    if "Árverés megnevezése" in key: data["kategoria"] = value
                    elif "Árverés kategória" in key: data["kategoria_reszletes"] = value
                    elif "Árverés kezdete" in key: data["kezdet"] = value
                    elif "Árverés befejezése" in key: data["befejezes"] = value
                    elif "megtekinthető, hely" in key: data["megtekintes_hely"] = value
                    elif "megtekinthető, idő" in key: data["megtekintes_ido"] = value

        elif "Árverezett tétel adatok" in header.get_text():
            for row in table.find_all("tr", class_="Bg2"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    key, value = clean_text(cells[0].get_text()), clean_text(cells[1].get_text())
                    if "Tétel megnevezése" in key: data["tetel_megnevezes"] = value
                    elif "Becsérték" in key: data["becsertek"] = value
                    elif "Minimál ajánlat" in key: data["minimal_ajanlat"] = value
                    elif "darabszám" in key: data["darabszam"] = value
                    elif "Állapot" in key: data["allapot"] = value
                    elif "Egyéb infó" in key: data["egyeb_info"] = value

    data["image_url"] = scrape_main_image(url, soup)

    megtekintes_hely = data.get("megtekintes_hely", "")
    if megtekintes_hely:
        dist = get_drive_distance(megtekintes_hely)
        data["tavolsag"] = f"{dist[0]} km ({dist[1]} perc autóval)" if dist else "Nem sikerült kiszámítani"
    else:
        data["tavolsag"] = "N/A"

    data["cim"] = data.get("tetel_megnevezes") or data.get("kategoria_reszletes") or "Ismeretlen tétel"
    
    # Képernyőfotóhoz hasonló logolás
    logger.info(f"Feldolgozva: {data['cim']} | Ár: {data.get('becsertek', 'N/A')}")
    
    return data

def extract_html_from_message(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
    elif msg.get_content_type() == "text/html":
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return None

def get_unread_emails():
    """Csak az OLVASATLAN e-maileket kéri le, majd olvasottnak jelöli őket."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, 'UNSEEN')
        if status != "OK" or not messages[0]:
            logger.info("⚠️ Nincs új, olvasatlan e-mail.")
            return []

        email_ids = messages[0].split()
        result = []

        for eid in email_ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK": continue
            
            msg = email.message_from_bytes(msg_data[0][1])
            subject_str = "".join([part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part for part, enc in decode_header(msg.get("Subject", ""))])
            subject_lower = subject_str.lower()
            from_lower = msg.get("From", "").lower()

            # Szigorított szűrés: NAV levél, de NEM MNV (mert az MNV is használhatja az 'elektronikus árverés' szót)
            is_nav = ("nav" in from_lower or "elektronikus árverés" in subject_lower or "elektronikus arveres" in subject_lower)
            if "mnv" in subject_lower:
                is_nav = False

            if is_nav:
                html_body = extract_html_from_message(msg)
                if html_body: result.append(html_body)
            
            # Olvasottnak jelölés
            mail.store(eid, "+FLAGS", "\\Seen")

        mail.logout()
        return result

    except Exception as e:
        logger.error(f"⚠️ IMAP hiba: {e}")
        return []

# =================== Telegram küldés ===================
def send_group_header(category: str, count: int):
    bot.send_message(chat_id=CHAT_ID, text=f"📂 <b>{category}</b>  •  {count} tétel", parse_mode="HTML")

def send_auction_message(a: dict):
    caption = f"🏛️ <b>{a.get('cim', 'Cím nélkül')}</b>\n\n"
    caption += f"📦 <b>Kategória:</b> {a.get('kategoria_reszletes', 'N/A')}\n"
    caption += f"💵 <b>Becsérték:</b> {a.get('becsertek', 'N/A')} (Min: {a.get('minimal_ajanlat', 'N/A')})\n"
    caption += f"📅 <b>Idő:</b> {a.get('kezdet', 'N/A')} - {a.get('befejezes', 'N/A')}\n"
    caption += f"📍 <b>Helyszín:</b> {a.get('megtekintes_hely', 'N/A')}\n"
    caption += f"🚗 <b>Távolság:</b> {a.get('tavolsag', 'N/A')}\n\n"
    caption += f"🔗 <a href='{a['url']}'>Részletek megtekintése</a>"

    image_bytes = download_image(a.get("image_url")) if a.get("image_url") else None

    try:
        if image_bytes:
            bot.send_photo(chat_id=CHAT_ID, photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML")
        else:
            bot.send_message(chat_id=CHAT_ID, text=caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"⚠️ Telegram küldési hiba: {e}")

def send_telegram_messages(auctions: list):
    if not auctions:
        return
    groups = group_auctions_by_category(auctions)
    
    bot.send_message(
        chat_id=CHAT_ID, 
        text=f"🔔 <b>Új NAV EAF árverések</b>\nÖsszesen: <b>{sum(len(v) for v in groups.values())} tétel</b>", 
        parse_mode="HTML"
    )

    for category, items in groups.items():
        send_group_header(category, len(items))
        for a in items:
            send_auction_message(a)

# =================== Fő logika ===================
def main():
    logger.info("=== NAV EAF Scraper Indul ===")
    seen_urls = load_seen_urls()

    emails_html = get_unread_emails()
    if not emails_html:
        return

    all_auctions = []
    for html in emails_html:
        for link in extract_nav_eaf_links(html):
            details = parse_nav_eaf_details(link)
            if details:
                all_auctions.append(details)

    unique = list({a["url"]: a for a in all_auctions}.values())
    new_auctions = filter_new_auctions(unique, seen_urls)

    send_telegram_messages(new_auctions)

    for a in new_auctions:
        seen_urls.add(a["url"])
    save_seen_urls(seen_urls)

if __name__ == "__main__":
    main()
