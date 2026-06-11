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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)


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
    """
    Fokozatosan egyszerűsíti a címet a geocodinghoz.
    Visszaad egy listát a próbálandó változatokból (legspecifikusabbtól a legsimábbig).
    """
    import re
    candidates = []

    # 1. Eredeti cím
    candidates.append(address)

    # 2. Levágjuk a hrsz-t, helyrajzi számot és a 'külterület' / 'belterület' szót
    cleaned = re.sub(r",?\s*\d+(/\d+)?\s*hrsz\.?", "", address, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(külterület|belterület|tanya)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip().rstrip(",").strip()
    if cleaned and cleaned != address:
        candidates.append(cleaned)

    # 3. Csak irányítószám + városnév (minden utáni részt levágjuk az első vessző után)
    # pl. "2475 Kápolnásnyék" a "2475 Kápolnásnyék külterület, 0172/16 hrsz"-ből
    city_match = re.match(r"(\d{4}\s+[A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ][A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ\s\-]+?)(?:\s*,|\s+\d|\s+külterület|\s+belterület|$)", cleaned or address)
    if city_match:
        city_only = city_match.group(1).strip()
        if city_only not in candidates:
            candidates.append(city_only)

    # 4. Csak az irányítószám + "Magyarország"
    zip_match = re.match(r"(\d{4})", address)
    if zip_match:
        zip_candidate = zip_match.group(1) + ", Magyarország"
        if zip_candidate not in candidates:
            candidates.append(zip_candidate)

    # Üres stringek és duplikátumok eltávolítása, sorrend megtartásával
    seen = []
    for c in candidates:
        c = c.strip()
        if c and c not in seen:
            seen.append(c)
    return seen


def geocode_address(address):
    """
    Cím geocodolása Nominatim API-val (OpenStreetMap).
    Fokozatosan egyszerűsített lekérdezésekkel próbálkozik, ha az eredeti nem talál semmit.
    Visszaad (lat, lon) tuple-t vagy None-t hiba esetén.
    """
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
        except Exception as e:
            logger.error(f"Geocode hiba ('{candidate}'): {e}")

    logger.warning(f"Geocode: minden próba sikertelen: '{address}'")
    return None


def get_drive_distance(dest_address):
    """
    Autós távolság és menetidő OSRM API-val.
    Visszaad egy (távolság_km, percek) tuple-t, vagy None-t hiba esetén.
    """
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
    """
    Kinyeri a Képgaléria szekció főképének URL-jét.

    A NAV EAF oldalon a nagy kép JavaScript-tel töltődik be a #defaultPicture
    div-be, ezért BeautifulSoup nem látja. Ehelyett a thumbnail <img> tagek
    'fullurl' attribútumát olvassuk ki – ez tartalmazza a teljes méretű kép
    relatív URL-jét (pl. pictures/9/e/700876.jpg), ami session nélkül is
    elérhető.
    """
    BASE = "https://arveres.nav.gov.hu/"

    try:
        # Keressük az összes img taget, aminek van fullurl attribútuma
        for img_tag in soup.find_all("img", fullurl=True):
            fullurl = img_tag.get("fullurl", "").strip()
            if not fullurl:
                continue
            # Abszolút URL építése
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


def download_image(image_url, session=None):
    """
    Letölti a képet és visszaadja bytes-ként, vagy None-t hiba esetén.
    Opcionálisan átvehet egy requests.Session-t (pl. bejelentkezett session).
    """
    try:
        req = session or requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = req.get(image_url, timeout=20, headers=headers)
        logger.info(f"Kép letöltés: {image_url} → HTTP {resp.status_code}, {len(resp.content)} byte")
        resp.raise_for_status()
        # Ellenőrzés: valóban kép-e (nem login redirect HTML)
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            logger.warning(f"Kép letöltés: nem kép tartalom ({content_type}), valószínűleg session szükséges.")
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

    # ---- Kép scrape ----
    data["image_url"] = scrape_main_image(url, soup)

    # ---- Távolság számítás ----
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

    # ---- Cím és ár összefoglalók ----
    if "tetel_megnevezes" in data:
        data["cim"] = data["tetel_megnevezes"]
    elif "kategoria_reszletes" in data:
        data["cim"] = data["kategoria_reszletes"]
    else:
        data["cim"] = "Ismeretlen tétel"

    data["jelenlegi_ar"] = data.get("becsertek", "N/A")
    return data


def extract_html_from_message(msg):
    """
    Rekurzívan kinyeri a HTML tartalmat egy e-mail üzenetből,
    beleértve a továbbított (message/rfc822) mellékleteket is.
    """
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
    """
    Lekéri az összes e-mailt a megadott dátum óta,
    majd kliens oldalon szűri a NAV és továbbított e-maileket.
    """
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


def send_auction_message(idx, a):
    """
    Egy árverési tételt küld el Telegram üzenetként képpel együtt.
    """
    caption = (
        f"<b>🏛️ {a.get('cim', 'Cím nélkül')}</b>\n\n"
        f"🏷️ <b>Kategória:</b> {a.get('kategoria_reszletes', 'N/A')}\n"
        f"💰 <b>Becsérték:</b> {a.get('becsertek', 'N/A')}\n"
        f"💸 <b>Minimál ajánlat:</b> {a.get('minimal_ajanlat', 'N/A')}\n"
        f"📦 <b>Állapot:</b> {a.get('allapot', 'N/A')}\n"
        f"📅 <b>Kezdés:</b> {a.get('kezdet', 'N/A')}\n"
        f"⏰ <b>Befejezés:</b> {a.get('befejezes', 'N/A')}\n"
        f"📍 <b>Megtekintés:</b> {a.get('megtekintes_hely', 'N/A')}\n"
        f"🕐 <b>Megtekintési idő:</b> {a.get('megtekintes_ido', 'N/A')}\n"
        f"🚗 <b>Távolság:</b> {a.get('tavolsag', 'N/A')}\n"
    )
    if a.get("egyeb_info"):
        caption += f"📝 <b>Infó:</b> {a['egyeb_info'][:200]}\n"
    caption += f"\n🔗 <a href='{a['url']}'>Részletek megtekintése</a>"

    # Telegram caption limit: 1024 karakter
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    image_url = a.get("image_url")
    image_bytes = download_image(image_url) if image_url else None

    sent = False

    # 1. próba: letöltött kép bytes-ként
    if image_bytes:
        try:
            bot.send_photo(
                chat_id=CHAT_ID,
                photo=io.BytesIO(image_bytes),
                caption=caption,
                parse_mode="HTML",
            )
            sent = True
        except Exception as e:
            logger.warning(f"Kép küldés (bytes) sikertelen: {e}")

    # 2. próba: kép URL-ként átadva (Telegram tölti le)
    if not sent and image_url:
        try:
            bot.send_photo(
                chat_id=CHAT_ID,
                photo=image_url,
                caption=caption,
                parse_mode="HTML",
            )
            sent = True
        except Exception as e:
            logger.warning(f"Kép küldés (URL) sikertelen: {e}")

    # 3. fallback: szöveges üzenet kép nélkül
    if not sent:
        try:
            bot.send_message(
                chat_id=CHAT_ID,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        except Exception as e:
            logger.error(f"Telegram szöveges küldés is sikertelen ({a.get('cim', '?')}): {e}")


def send_telegram_messages(auctions):
    """
    Minden árverést külön Telegram üzenetben küld el.
    Ha nincs találat, egyetlen értesítő üzenetet küld.
    """
    if not auctions:
        bot.send_message(
            chat_id=CHAT_ID,
            text="📭 Nincs új NAV EAF árverési értesítő az elmúlt 24 órában.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    logger.info(f"Összesen {len(auctions)} árverés küldése Telegramra.")
    for idx, a in enumerate(auctions, 1):
        logger.info(f"Küldés {idx}/{len(auctions)}: {a.get('cim', '?')}")
        send_auction_message(idx, a)


def main():
    since = datetime.now(timezone.utc) - timedelta(days=1)
    logger.info(f"Keresés kezdete: {since.strftime('%Y-%m-%d %H:%M')} UTC")

    emails_html = get_emails_since(since)
    if not emails_html:
        send_telegram_messages([])
        return

    all_auctions = []
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        logger.info(f"Talált NAV EAF linkek: {links}")
        for link in links:
            details = parse_nav_eaf_details(link)
            if details:
                all_auctions.append(details)

    unique = list({a["url"]: a for a in all_auctions}.values())
    send_telegram_messages(unique)


if __name__ == "__main__":
    main()
