import os
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime, timezone, timedelta
import logging
import io

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Budapest XVII. ker. Sáránd utca közelítő koordinátái (kiindulópont a távolsághoz)
ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198
ORIGIN_LABEL = "Budapest XVII. ker. Sáránd utca"

PROCESSED_URLS_FILE = "processed_auctions.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)


# ------------------- Persistent storage for sent auctions -------------------
def load_processed_urls():
    """Betölti a már elküldött árverési URL-eket a fájlból."""
    if not os.path.exists(PROCESSED_URLS_FILE):
        return set()
    with open(PROCESSED_URLS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_processed_urls(processed_set):
    """Mentés előtt felülírja a fájlt a jelenlegi halmaz tartalmával."""
    with open(PROCESSED_URLS_FILE, "w", encoding="utf-8") as f:
        for url in processed_set:
            f.write(url + "\n")


# ------------------- Segédfüggvények (változatlan) -------------------
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
    candidates = []
    candidates.append(address)
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
    zip_match = re.match(r"(\d{4})", address)
    if zip_match:
        zip_candidate = zip_match.group(1) + ", Magyarország"
        if zip_candidate not in candidates:
            candidates.append(zip_candidate)
    seen = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def geocode_address(address):
    import time
    candidates = simplify_address(address)
    headers = {"User-Agent": "NAV-EAF-Scraper/1.0"}
    for candidate in candidates:
        try:
            params = {
                "q": candidate,
                "format": "json",
                "limit": 1,
                "countrycodes": "hu",
            }
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                logger.info(f"Geocode OK: '{candidate}' (eredeti: '{address}') → ({lat}, {lon})")
                return lat, lon
            else:
                logger.info(f"Geocode: nincs találat erre: '{candidate}', következő próba...")
            time.sleep(1.1)
        except Exception as e:
            logger.error(f"Geocode hiba ('{candidate}'): {e}")
            time.sleep(1.1)
    logger.warning(f"Geocode: minden próba sikertelen: '{address}'")
    return None


def get_drive_distance(dest_address):
    coords = geocode_address(dest_address)
    if not coords:
        return None
    dest_lat, dest_lon = coords
    try:
        url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}"
            f"?overview=false"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            km = round(route["distance"] / 1000, 1)
            minutes = round(route["duration"] / 60)
            return km, minutes
        else:
            logger.warning(f"OSRM: nem sikerült útvonalat számítani: {data.get('code')}")
            return None
    except Exception as e:
        logger.error(f"OSRM hiba: {e}")
        return None


def scrape_main_image(url, soup):
    BASE = "https://arveres.nav.gov.hu/"
    try:
        for img_tag in soup.find_all("img", fullurl=True):
            fullurl = img_tag.get("fullurl", "").strip()
            if not fullurl:
                continue
            if fullurl.startswith("http"):
                image_url = fullurl
            elif fullurl.startswith("/"):
                image_url = "https://arveres.nav.gov.hu" + fullurl
            else:
                image_url = BASE + fullurl
            logger.info(f"Kép URL (fullurl attribútum): {image_url}")
            return image_url
        logger.warning("Nem található fullurl attribútumú kép az oldalon.")
        return None
    except Exception as e:
        logger.error(f"Kép scrape hiba: {e}")
        return None


def parse_nav_eaf_details(url, html_text=None):
    if html_text is None:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(url, timeout=30, headers=headers)
            resp.raise_for_status()
            resp.encoding = "ISO-8859-2"
            html_text = resp.text
        except Exception as e:
            logger.error(f"Hiba a NAV EAF oldal betöltésekor: {url} - {e}")
            return None

    soup = BeautifulSoup(html_text, "html.parser")
    data = {"url": url}

    # ---- Árverés alapadatok táblázat ----
    alapadatok_table = None
    for div in soup.find_all("div", class_="FrissPortlet"):
        header = div.find("div", class_="HeaderTitle")
        if header and "Árverés alapadatok" in header.get_text():
            alapadatok_table = div.find("table", class_="DownloadAppsList")
            break

    if alapadatok_table:
        rows = alapadatok_table.find_all("tr", class_="Bg2")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                key = clean_text(cells[0].get_text())
                value = clean_text(cells[1].get_text())
                if "Árverés megnevezése" in key:
                    data["kategoria"] = value
                elif "Végrehajtási ügyszám" in key:
                    data["ugyintezesi_szam"] = value
                elif "Árverés kategória" in key:
                    data["kategoria_reszletes"] = value
                elif "Árverés sorszáma" in key:
                    data["sorszam"] = value
                elif "Árverés meghirdetése" in key:
                    data["meghirdetes"] = value
                elif "Árverés kezdete" in key:
                    data["kezdet"] = value
                elif "Árverés befejezése" in key:
                    data["befejezes"] = value
                elif "Ügyintéző telefon" in key:
                    data["telefon"] = value
                elif "Az árverezett tétel megtekinthető, hely" in key:
                    data["megtekintes_hely"] = value
                elif "Az árverezett tétel megtekinthető, idő" in key:
                    data["megtekintes_ido"] = value

    # ---- Árverezett tétel adatok táblázat ----
    tetel_table = None
    for div in soup.find_all("div", class_="FrissPortlet"):
        header = div.find("div", class_="HeaderTitle")
        if header and "Árverezett tétel adatok" in header.get_text():
            tetel_table = div.find("table", class_="DownloadAppsList")
            break

    if tetel_table:
        rows = tetel_table.find_all("tr", class_="Bg2")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 2:
                key = clean_text(cells[0].get_text())
                value = clean_text(cells[1].get_text())
                if "Tétel megnevezése" in key:
                    data["tetel_megnevezes"] = value
                elif "Becsérték" in key:
                    data["becsertek"] = value
                elif "Minimál ajánlat" in key:
                    data["minimal_ajanlat"] = value
                elif "Egyszerre árverezett tétel darabszám" in key:
                    data["darabszam"] = value
                elif "Állapot" in key:
                    data["allapot"] = value
                elif "Egyéb infó" in key:
                    data["egyeb_info"] = value

    data["image_url"] = scrape_main_image(url, soup)

    megtekintes_hely = data.get("megtekintes_hely", "")
    if megtekintes_hely:
        result = get_drive_distance(megtekintes_hely)
        if result:
            km, minutes = result
            data["tavolsag"] = f"{km} km ({minutes} perc autóval)"
        else:
            data["tavolsag"] = "Nem sikerült kiszámítani"
    else:
        data["tavolsag"] = "N/A"

    if "tetel_megnevezes" in data:
        data["cim"] = data["tetel_megnevezes"]
    elif "kategoria_reszletes" in data:
        data["cim"] = data["kategoria_reszletes"]
    else:
        data["cim"] = "Ismeretlen tétel"

    data["jelenlegi_ar"] = data.get("becsertek", "N/A")
    return data


def extract_html_from_message(msg):
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/html" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
            elif content_type == "message/rfc822":
                inner_payload = part.get_payload()
                if isinstance(inner_payload, list):
                    for inner_msg in inner_payload:
                        result = extract_html_from_message(inner_msg)
                        if result:
                            return result
                elif isinstance(inner_payload, bytes):
                    inner_msg = email.message_from_bytes(inner_payload)
                    result = extract_html_from_message(inner_msg)
                    if result:
                        return result
    else:
        if msg.get_content_type() == "text/html":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="ignore")
    return None


def get_emails_since(since_date):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        search_criteria = f'(SINCE "{since_date.strftime("%d-%b-%Y")}")'
        status, messages = mail.search(None, search_criteria)
        if status != "OK" or not messages[0]:
            logger.info("Nincs e-mail a megadott időszakban.")
            return []

        email_ids = messages[0].split()
        logger.info(f"Összesen {len(email_ids)} e-mail érkezett {since_date} óta.")

        result = []
        for eid in email_ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            from_ = msg.get("From", "")
            subject_parts = decode_header(msg.get("Subject", ""))
            subject_str = ""
            for part, enc in subject_parts:
                if isinstance(part, bytes):
                    part = part.decode(enc or "utf-8", errors="ignore")
                subject_str += part

            is_nav = (
                any(sender in from_ for sender in ["-eaf@nav.gov.hu", "eaf@nav.gov.hu"])
                or "Elektronikus Árverés" in subject_str
                or "Elektronikus Arveres" in subject_str
            )

            if not is_nav:
                continue

            logger.info(f"NAV e-mail: {subject_str} | Feladó: {from_}")

            html_body = extract_html_from_message(msg)
            if html_body:
                result.append(html_body)
            else:
                logger.warning(f"Nincs HTML tartalom a NAV e-mailben: {subject_str}")

            mail.store(eid, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()
        return result

    except Exception as e:
        logger.exception(f"IMAP hiba: {e}")
        return []


# ------------------- Telegram üzenetek csoportosítva -------------------
def send_grouped_telegram_message(auctions):
    """
    Egyetlen Telegram üzenetben elküldi az összes, egy e‑mailből származó új tételt.
    auctions: list of dict (a parse_nav_eaf_details által visszaadott adatok)
    """
    count = len(auctions)
    text = f"🏛️ <b>Új NAV EAF árverések ({count} tétel)</b>\n\n"
    for idx, a in enumerate(auctions, 1):
        text += f"{idx}. <b>{a.get('cim', 'Cím nélkül')}</b>\n"
        text += f"💰 Becsérték: {a.get('becsertek', 'N/A')}\n"
        text += f"📍 Helyszín: {a.get('megtekintes_hely', 'N/A')}\n"
        text += f"🚗 Távolság: {a.get('tavolsag', 'N/A')}\n"
        text += f"🔗 <a href='{a['url']}'>Részletek</a>\n\n"
        if len(text) > 4000:
            text = text[:3950] + "\n... (a további tételek nem fértek el)"
            break

    try:
        bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Hiba a csoportos üzenet küldésekor: {e}")


def send_telegram_messages(groups):
    """
    groups: list of list of dict (minden belső lista egy e‑mail összes új tételét tartalmazza)
    """
    if not groups:
        bot.send_message(
            chat_id=CHAT_ID,
            text="📭 Nincs új NAV EAF árverési értesítő az elmúlt 24 órában.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    logger.info(f"Összesen {len(groups)} e‑mail csoport küldése Telegramra.")
    for idx, group in enumerate(groups, 1):
        logger.info(f"Csoport {idx}/{len(groups)} – {len(group)} tétel")
        send_grouped_telegram_message(group)


# ------------------- Főprogram -------------------
def main():
    since = datetime.now(timezone.utc) - timedelta(days=1)
    logger.info(f"Keresés kezdete: {since.strftime('%Y-%m-%d %H:%M')} UTC")

    # Betöltjük a már elküldött tételek URL-jeit
    processed_urls = load_processed_urls()
    logger.info(f"Már feldolgozott tételek száma: {len(processed_urls)}")

    emails_html = get_emails_since(since)
    if not emails_html:
        send_telegram_messages([])
        save_processed_urls(processed_urls)
        return

    # Csoportosítjuk az egyes e‑mailekben érkező új tételeket
    groups = []  # list of lists (minden belső lista az adott e‑mail új tételeit tartalmazza)
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        logger.info(f"Talált NAV EAF linkek ebben az e‑mailben: {links}")
        auctions_in_this_email = []
        for link in links:
            if link in processed_urls:
                logger.info(f"Link már feldolgozva korábban, kihagyás: {link}")
                continue
            details = parse_nav_eaf_details(link)
            if details:
                processed_urls.add(link)
                auctions_in_this_email.append(details)
        if auctions_in_this_email:
            groups.append(auctions_in_this_email)

    # Üzenetek küldése (csoportosítva)
    send_telegram_messages(groups)

    # A frissített processed set-et elmentjük
    save_processed_urls(processed_urls)
    logger.info(f"Mentés után feldolgozott tételek száma: {len(processed_urls)}")


if __name__ == "__main__":
    main()
