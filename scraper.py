import os
import json
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime, timezone, timedelta
import logging
import io
import asyncio

# ------------------- Konfiguráció (ÚJ SECRETS) -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")

# Áthangolva a Real Estate (MBVK) botra és csatornára
BOT_TOKEN = os.environ.get("REALESTATE_BOT_TOKEN")
CHAT_ID = os.environ.get("REALESTATE_CHAT_ID")

# Budapest XVII. ker. Sáránd utca közelítő koordinátái (kiindulópont a távolsághoz)
ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198

SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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
        logger.error(f"Látott URL-ek betöltési hiba: {e}")
        return set()


def save_seen_urls(seen: set):
    try:
        with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_urls": sorted(seen)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Látott URL-ek mentési hiba: {e}")


def filter_new_auctions(auctions: list, seen: set) -> list:
    return [a for a in auctions if a.get("url") and a["url"] not in seen]


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
                results = resp.json()
                return float(results[0]["lat"]), float(results[0]["lon"])
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
        url = f"http://router.project-osrm.org/route/v1/driving/{dest_lon},{dest_lat};{ORIGIN_LON},{ORIGIN_LAT}?overview=false"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("routes"):
                route = data["routes"][0]
                return round(route["distance"] / 1000, 1), round(route["duration"] / 60)
    except Exception:
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


def download_image(image_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(image_url, timeout=20, headers=headers)
        if "image" in resp.headers.get("Content-Type", ""):
            return resp.content
    except Exception:
        return None


def parse_nav_eaf_details(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=30, headers=headers)
        resp.encoding = "ISO-8859-2"
        html_text = resp.text
    except Exception:
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    data = {"url": url}

    # Táblázatok feldolgozása
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
                        elif "Árverés kategória" in key: data["kategoria_reszletes"] = value
                        elif "Árverés kezdete" in key: data["kezdet"] = value
                        elif "Árverés befejezése" in key: data["befejezes"] = value
                        elif "Az árverezett tétel megtekinthető, hely" in key: data["megtekintes_hely"] = value
                        elif "Tétel megnevezése" in key: data["tetel_megnevezes"] = value
                        elif "Becsérték" in key: data["becsertek"] = value
                        elif "Minimál ajánlat" in key: data["minimal_ajanlat"] = value
                        elif "Állapot" in key: data["allapot"] = value
                        elif "Egyéb infó" in key: data["egyeb_info"] = value

    data["image_url"] = scrape_main_image(url, soup)

    megtekintes_hely = data.get("megtekintes_hely", "")
    if megtekintes_hely:
        result = get_drive_distance(megtekintes_hely)
        data["tavolsag"] = f"{result[0]} km ({result[1]} perc)" if result else "Nem sikerült kiszámítani"
    else:
        data["tavolsag"] = "N/A"

    data["cim"] = data.get("tetel_megnevezes") or data.get("kategoria_reszletes") or "Ismeretlen ingatlan"
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
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        search_criteria = f'(UNSEEN SINCE "{since_date.strftime("%d-%b-%Y")}")'
        status, messages = mail.search(None, search_criteria)
        if status != "OK" or not messages[0]:
            return []

        result = []
        for eid in messages[0].split():
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK": continue
            msg = email.message_from_bytes(msg_data[0][1])
            
            subject_parts = decode_header(msg.get("Subject", ""))
            subject_str = "".join([p.decode(e or "utf-8", errors="ignore") if isinstance(p, bytes) else p for p, e in subject_parts])
            
            if "Elektronikus Árverés" in subject_str or "Elektronikus Arveres" in subject_str:
                html_body = extract_html_from_message(msg)
                if html_body:
                    result.append(html_body)
            mail.store(eid, "+FLAGS", "\\Seen")
        
        mail.close()
        mail.logout()
        return result
    except Exception as e:
        logger.error(f"IMAP hiba: {e}")
        return []


# =================== IDEIGLENES MBVK-STÍLUSÚ PAPSÁG ===================

async def send_auction_message(a: dict):
    # Az MBVK botokhoz hasonló, letisztultabb struktúra
    caption = f"🏠 <b>{a.get('cim', 'Ismeretlen ingatlan')}</b>\n\n"
    
    caption += f"💰 <b>Becsérték:</b> {a.get('becsertek', 'N/A')}\n"
    caption += f"📉 <b>Minimálár:</b> {a.get('minimal_ajanlat', 'N/A')}\n\n"
    
    caption += f"📅 <b>Kezdet:</b> {a.get('kezdet', 'N/A')}\n"
    caption += f"⏳ <b>Vége:</b> {a.get('befejezes', 'N/A')}\n\n"
    
    caption += f"📍 <b>Helyszín:</b> {a.get('megtekintes_hely', 'N/A')}\n"
    caption += f"🚗 <b>Távolság (XVII. ker):</b> {a.get('tavolsag', 'N/A')}\n\n"
    
    if a.get("egyeb_info"):
        caption += f"📝 <b>Leírás:</b> <i>{a['egyeb_info'][:300]}...</i>\n\n"
        
    caption += f"🔗 <a href='{a['url']}'>Árverési adatlap megnyitása</a>"

    image_bytes = download_image(a.get("image_url")) if a.get("image_url") else None
    try:
        if image_bytes:
            await bot.send_photo(chat_id=CHAT_ID, photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=CHAT_ID, text=caption, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Hiba az üzenetküldésnél: {e}")


async def send_telegram_messages(auctions: list):
    if not auctions:
        return
    for a in auctions:
        await send_auction_message(a)


# =================== Fő logika ===================

def main():
    since = datetime.now(timezone.utc) - timedelta(days=1)
    seen_urls = load_seen_urls()
    
    emails_html = get_emails_since(since)
    if not emails_html:
        return

    all_auctions = []
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        for link in links:
            details = parse_nav_eaf_details(link)
            if details:
                all_auctions.append(details)

    unique = list({a["url"]: a for a in all_auctions}.values())
    
    # KIZÁRÓLAG INGATLANOK MEGTARTÁSA
    real_estate_auctions = [
        a for a in unique 
        if "ingatlan" in a.get("kategoria", "").lower() 
        or "ingatlan" in a.get("kategoria_reszletes", "").lower()
    ]
    
    new_auctions = filter_new_auctions(real_estate_auctions, seen_urls)
    
    asyncio.run(send_telegram_messages(new_auctions))

    for a in new_auctions:
        seen_urls.add(a["url"])
    save_seen_urls(seen_urls)


if __name__ == "__main__":
    main()
