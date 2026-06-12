import os
import json
import imaplib
import email
import asyncio
import io
import logging
import requests
from email.header import decode_header
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime, timezone, timedelta

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Budapest XVII. ker. Sáránd utca közelítő koordinátái (kiindulópont a távolsághoz)
ORIGIN_LAT = 47.4344
ORIGIN_LON = 19.2198
ORIGIN_LABEL = "Budapest XVII. ker. Sáránd utca"

# ------------------- Látott URL-ek tárolója -------------------
SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)


# =================== Látott URL-ek kezelése ===================

def load_seen_urls() -> set:
    """Betölti a már látott (kiküldött) URL-ek halmazát a JSON fájlból."""
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
    """Elmenti a látott URL-ek halmazát a JSON fájlba."""
    try:
        with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen_urls": sorted(list(seen))}, f, ensure_ascii=False, indent=2)
        logger.info(f"Látott URL-ek mentve: {len(seen)} db → {SEEN_URLS_FILE}")
    except Exception as e:
        logger.error(f"Látott URL-ek mentési hiba: {e}")


def filter_new_auctions(auctions: list, seen: set) -> list:
    """Csak azokat az árveréseket adja vissza, amelyek URL-je még nem szerepel a seen halmazban."""
    new = [a for a in auctions if a.get("url") and a["url"] not in seen]
    logger.info(f"Szűrés: {len(auctions)} árverésből {len(new)} új (még nem küldött).")
    return new


# =================== Csoportosítás ===================

def group_auctions_by_category(auctions: list) -> dict:
    """
    Az árveréseket kategória szerint csoportosítja.
    Visszaad egy rendezett dict-et: {kategória_cím: [árverés, ...]}
    """
    groups = {}
    for a in auctions:
        cat = (
            a.get("kategoria_reszletes")
            or a.get("kategoria")
            or "Egyéb"
        )
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
            params = {"q": candidate, "format": "json", "limit": 1, "countrycodes": "hu"}
            resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            results = resp.json()
            if results:
                lat = float(results[0]["lat"])
                lon = float(results[0]["lon"])
                logger.info(f"Geocode OK: '{candidate}' → ({lat}, {lon})")
                return lat, lon
            else:
                logger.info(f"Geocode: nincs találat: '{candidate}', következő próba...")
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
            f"{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}?overview=false"
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
            logger.info(f"Kép URL: {image_url}")
            return image_url
        logger.warning("Nem található kép.")
        return None
    except Exception as e:
        logger.error(f"Kép scrape hiba: {e}")
        return None


def download_image(image_url, session=None):
    try:
        req = session or requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = req.get(image_url, timeout=20, headers=headers)
        logger.info(f"Kép letöltés: {image_url} → HTTP {resp.status_code}, {len(resp.content)} byte")
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            logger.warning(f"Nem kép tartalom ({content_type}), session szükséges?")
            return None
        return resp.content
    except Exception as e:
        logger.error(f"Kép letöltési hiba: {image_url} - {e}")
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

        # MÓDOSÍTÁS: Beiktatva az UNSEEN feltétel, így csak az olvasatlan leveleket kéri le
        search_criteria = f'(UNSEEN SINCE "{since_date.strftime("%d-%b-%Y")}")'
        status, messages = mail.search(None, search_criteria)
        if status != "OK" or not messages[0]:
            logger.info("Nincs új olvasatlan e-mail a megadott időszakban.")
            return []

        email_ids = messages[0].split()
        logger.info(f"Összesen {len(email_ids)} olvasatlan e-mail érkezett {since_date} óta.")

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


# =================== ASZINKRON Telegram küldés ===================

async def send_group_header(category: str, count: int):
    """Kategória-fejléc üzenetet küld a csoport elé (Aszinkron)."""
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>{category}</b>  •  {count} tétel\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Fejléc küldési hiba: {e}")


async def send_auction_message(idx: int, a: dict):
    """Egy árverési tételt küld el Telegram üzenetként képpel együtt (Aszinkron)."""
    caption = f"🏛️ <b>{a.get('cim', 'Cím nélkül')}</b>\n\n"

    caption += "📦 <b>1. Tétel alapadatok</b>\n"
    caption += f"🏷️ Kategória: {a.get('kategoria_reszletes', 'N/A')}\n"
    caption += f"📊 Állapot: {a.get('allapot', 'N/A')}\n"
    if a.get("darabszam"):
        caption += f"🔢 Darabszám: {a.get('darabszam')}\n"
    caption += "\n"

    caption += "💰 <b>2. Pénzügyi információk</b>\n"
    caption += f"💵 Becsérték: {a.get('becsertek', 'N/A')}\n"
    caption += f"💸 Minimál ajánlat: {a.get('minimal_ajanlat', 'N/A')}\n"
    caption += "\n"

    caption += "📅 <b>3. Időpontok</b>\n"
    caption += f"▶️ Kezdés: {a.get('kezdet', 'N/A')}\n"
    caption += f"⏹️ Befejezés: {a.get('befejezes', 'N/A')}\n"
    caption += "\n"

    caption += "📍 <b>4. Megtekintés</b>\n"
    caption += f"🗺️ Helyszín: {a.get('megtekintes_hely', 'N/A')}\n"
    caption += f"🕐 Időpont: {a.get('megtekintes_ido', 'N/A')}\n"
    caption += f"🚗 Távolság: {a.get('tavolsag', 'N/A')}\n"
    caption += "\n"

    if a.get("egyeb_info"):
        caption += "📝 <b>5. Leírás</b>\n"
        caption += f"<i>{a['egyeb_info'][:250]}</i>\n\n"

    caption += f"🔗 <a href='{a['url']}'>Részletek megtekintése</a>"

    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    image_url = a.get("image_url")
    image_bytes = download_image(image_url) if image_url else None
    sent = False

    if image_bytes:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML")
            sent = True
        except Exception as e:
            logger.warning(f"Kép küldés (bytes) sikertelen: {e}")

    if not sent and image_url:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=image_url, caption=caption, parse_mode="HTML")
            sent = True
        except Exception as e:
            logger.warning(f"Kép küldés (URL) sikertelen: {e}")

    if not sent:
        try:
            await bot.send_message(chat_id=CHAT_ID, text=caption, parse_mode="HTML", disable_web_page_preview=False)
        except Exception as e:
            logger.error(f"Telegram szöveges küldés is sikertelen ({a.get('cim', '?')}): {e}")


async def send_telegram_messages(auctions: list):
    """
    Az árveréseket kategória szerint csoportosítva küldi el Telegramra (Aszinkron).
    """
    if not auctions:
        await bot.send_message(
            chat_id=CHAT_ID,
            text="📭 Nincs új NAV EAF árverési értesítő (minden már ismert).",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    groups = group_auctions_by_category(auctions)
    total = sum(len(v) for v in groups.values())
    cat_count = len(groups)

    summary = (
        f"🔔 <b>Új NAV EAF árverések</b>\n"
        f"📊 Összesen: <b>{total} új tétel</b> / <b>{cat_count} kategória</b>\n"
        f"🕐 {datetime.now().strftime('%Y.%m.%d %H:%M')}"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=summary, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Összefoglaló fejléc küldési hiba: {e}")

    for category, items in groups.items():
        await send_group_header(category, len(items))
        for idx, a in enumerate(items, 1):
            logger.info(f"Küldés [{category}] {idx}/{len(items)}: {a.get('cim', '?')}")
            await send_auction_message(idx, a)

    logger.info(f"Telegram küldés kész: {total} tétel, {cat_count} kategória.")


# =================== Fő logika ===================

async def main():
    since = datetime.now(timezone.utc) - timedelta(days=1)
    logger.info(f"=== NAV EAF Scraper v1.00 indítás: {datetime.now().strftime('%Y.%m.%d %H:%M')} ===")
    logger.info(f"E-mail keresés kezdete: {since.strftime('%Y-%m-%d %H:%M')} UTC")

    # 1. Látott URL-ek betöltése
    seen_urls = load_seen_urls()
    logger.info(f"Már ismert URL-ek száma: {len(seen_urls)}")

    # 2. E-mailek lekérése és linkek kinyerése
    emails_html = get_emails_since(since)
    if not emails_html:
        logger.info("Nem érkezett új olvasatlan NAV e-mail az elmúlt 24 órában.")
        await send_telegram_messages([])
        return

    all_auctions = []
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        logger.info(f"Talált NAV EAF linkek: {links}")
        for link in links:
            details = parse_nav_eaf_details(link)
            if details:
                all_auctions.append(details)

    # 3. URL-alapú deduplikáció az aktuális futáson belül
    unique = list({a["url"]: a for a in all_auctions}.values())
    logger.info(f"Egyedi árverések (aktuális futás): {len(unique)} db")

    # 4. Szűrés: csak azok, amelyekről még NEM küldtünk értesítőt
    new_auctions = filter_new_auctions(unique, seen_urls)

    # 5. Telegram üzenetek küldése csoportosítva
    await send_telegram_messages(new_auctions)

    # 6. Látott URL-ek frissítése és mentése
    for a in new_auctions:
        seen_urls.add(a["url"])
    save_seen_urls(seen_urls)

    logger.info("=== Futás befejezve ===")


if __name__ == "__main__":
    # Az aszinkron main hurok indítása
    asyncio.run(main())
