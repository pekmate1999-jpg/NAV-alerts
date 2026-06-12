import os
import json
import imaplib
import email
from email.header import decode_header
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime
import logging
import io
import html as html_escape
import re

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
real_estate_bot = Bot(token=REAL_ESTATE_BOT_TOKEN) if REAL_ESTATE_BOT_TOKEN and REAL_ESTATE_CHAT_ID else None


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
        logger.info(f"Látott URL-ek mentve: {len(seen)} db")
    except Exception as e:
        logger.error(f"Látott URL-ek mentési hiba: {e}")


def filter_new_auctions(auctions: list, seen: set) -> list:
    new = [a for a in auctions if a.get("url") and a["url"] not in seen]
    logger.info(f"Szűrés: {len(auctions)} árverésből {len(new)} új.")
    return new


# =================== Normalizálás és csoportosítás ===================

def normalize_name(name: str) -> str:
    """Eltávolítja a zárójeles részeket, vesszőket, kisbetűsít, törli a felesleges szóközöket."""
    if not name:
        return "ismeretlen"
    name = re.sub(r'\s*\([^)]*\)', '', name)  # zárójelben lévő rész
    name = re.sub(r',', '', name)             # vesszők
    name = name.lower()
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def group_by_name(auctions: list) -> dict:
    """Csoportosítás normalizált név alapján. A csoport címe az első tétel eredeti neve."""
    groups = {}
    for a in auctions:
        raw_name = a.get("cim", "Ismeretlen tétel")
        norm_name = normalize_name(raw_name)
        if norm_name not in groups:
            groups[norm_name] = (raw_name, [])
        groups[norm_name][1].append(a)
    result = {}
    for _, (orig, items) in groups.items():
        result[orig] = items
    return result


def is_real_estate(auction: dict) -> bool:
    """Ingatlan felismerése kategória és tétel név alapján."""
    kategoria = auction.get("kategoria_reszletes", "") or auction.get("kategoria", "")
    cim = auction.get("cim", "")
    szoveg = (kategoria + " " + cim).lower()
    keywords = [
        "ingatlan", "lakás", "ház", "családi ház", "telek", "garázs", "üdülő",
        "iroda", "üzlet", "pince", "műhely", "raktár", "beépítetlen terület", "kivett",
        "lakóház", "gazdasági épület", "tanya", "majorság", "szőlő", "gyümölcsös"
    ]
    return any(kw in szoveg for kw in keywords)


def build_combined_message(group_name: str, items: list) -> str:
    """Egy csoport üzenetének összeállítása (kategória nélkül, HTML escape-el)."""
    first = items[0]
    caption = f"🏛️ <b>{group_name}</b>\n\n"

    # 1. Alapadatok
    caption += "📦 <b>1. Tétel alapadatok</b>\n"
    if first.get("allapot"):
        caption += f"📊 Állapot: {first.get('allapot')}\n"
    if first.get("darabszam"):
        caption += f"🔢 Darabszám: {first.get('darabszam')}\n"
    caption += "\n"

    # 2. Pénzügyi információk
    caption += "💰 <b>2. Pénzügyi információk</b>\n"
    caption += f"💵 Becsérték: {first.get('becsertek', 'N/A')}\n"
    caption += f"💸 Minimál ajánlat: {first.get('minimal_ajanlat', 'N/A')}\n"
    caption += "\n"

    # 3. Időpontok
    caption += "📅 <b>3. Időpontok</b>\n"
    caption += f"▶️ Kezdés: {first.get('kezdet', 'N/A')}\n"
    caption += f"⏹️ Befejezés: {first.get('befejezes', 'N/A')}\n"
    caption += "\n"

    # 4. Megtekintés
    caption += "📍 <b>4. Megtekintés</b>\n"
    caption += f"🗺️ Helyszín: {first.get('megtekintes_hely', 'N/A')}\n"
    caption += f"🕐 Időpont: {first.get('megtekintes_ido', 'N/A')}\n"
    caption += f"🚗 Távolság: {first.get('tavolsag', 'N/A')}\n"
    caption += "\n"

    # 5. Leírás – HTML escape
    if first.get("egyeb_info"):
        escaped_desc = html_escape.escape(first['egyeb_info'][:250])
        caption += "📝 <b>5. Leírás</b>\n"
        caption += f"<i>{escaped_desc}</i>\n\n"

    # Linkek
    if len(items) == 1:
        caption += f"🔗 <a href='{items[0]['url']}'>Részletek megtekintése</a>"
    else:
        caption += "🔗 <b>Linkek az egyes tételekhez:</b>\n"
        for idx, item in enumerate(items, 1):
            caption += f"{idx}. <a href='{item['url']}'>Tétel linkje</a>\n"

    if len(caption) > 1024:
        caption = caption[:1020] + "…"
    return caption


def download_image(image_url: str) -> bytes | None:
    if not image_url:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(image_url, timeout=20, headers=headers)
        resp.raise_for_status()
        if "image" not in resp.headers.get("Content-Type", ""):
            return None
        return resp.content
    except Exception as e:
        logger.error(f"Kép letöltési hiba: {image_url} - {e}")
        return None


def send_grouped_messages(groups: dict, target_bot: Bot, target_chat_id: str, category_label: str):
    if not groups:
        return
    total_items = sum(len(v) for v in groups.values())
    total_groups = len(groups)
    summary = (
        f"🔔 <b>Új NAV EAF árverések ({category_label})</b>\n"
        f"📊 Összesen: <b>{total_items} új tétel</b> / <b>{total_groups} csoport</b>\n"
        f"🕐 {datetime.now().strftime('%Y.%m.%d %H:%M')}"
    )
    try:
        target_bot.send_message(chat_id=target_chat_id, text=summary, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Összefoglaló fejléc hiba ({category_label}): {e}")

    for group_name, items in groups.items():
        caption = build_combined_message(group_name, items)
        first_item = items[0]
        image_url = first_item.get("image_url")
        image_bytes = download_image(image_url) if image_url else None
        sent = False

        if image_bytes:
            try:
                target_bot.send_photo(chat_id=target_chat_id, photo=io.BytesIO(image_bytes), caption=caption, parse_mode="HTML")
                sent = True
                logger.info(f"Csoport kész: {group_name} ({len(items)} tétel) - képpel")
            except Exception as e:
                logger.warning(f"Képküldés sikertelen: {e}")
        if not sent and image_url:
            try:
                target_bot.send_photo(chat_id=target_chat_id, photo=image_url, caption=caption, parse_mode="HTML")
                sent = True
                logger.info(f"Csoport kész: {group_name} ({len(items)} tétel) - URL képpel")
            except Exception as e:
                logger.warning(f"Képküldés URL-ről sikertelen: {e}")
        if not sent:
            try:
                target_bot.send_message(chat_id=target_chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=False)
                logger.info(f"Csoport kész: {group_name} ({len(items)} tétel) - szövegesen")
            except Exception as e:
                logger.error(f"Szöveges küldés sikertelen ({group_name}): {e}")


# =================== E-mail és adatgyűjtés ===================

def clean_text(text):
    return " ".join(text.split()) if text else ""


def extract_links_from_text(text: str) -> list:
    """Kinyeri a NAV EAF linkeket sima szöveges tartalomból (reguláris kifejezéssel)."""
    pattern = r'https?://arveres\.nav\.gov\.hu[^\s"\'>]+'
    links = re.findall(pattern, text)
    # Szűrés: csak azok, amelyek tartalmazzák az auctionId vagy item=auctionSummary paramétert
    filtered = []
    for link in links:
        if 'auctionId' in link or 'item=auctionSummary' in link:
            filtered.append(link)
    return list(set(filtered))


def extract_nav_eaf_links(html_content):
    """Először HTML-ből próbál linkeket kinyerni, ha nincs, akkor szövegesen."""
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "arveres.nav.gov.hu" in href and ("auctionId" in href or "item=auctionSummary" in href):
            if href.startswith("/"):
                href = "https://arveres.nav.gov.hu" + href
            href = href.replace("nav.gov.hu//", "nav.gov.hu/")
            links.append(href)
    if not links:
        # Ha nem találtunk HTML linkeket, próbáljuk a szöveges kinyerést
        links = extract_links_from_text(html_content)
        if links:
            logger.info(f"Szöveges kinyeréssel talált linkek: {links}")
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
                return lat, lon
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
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            km = round(route["distance"] / 1000, 1)
            minutes = round(route["duration"] / 60)
            return km, minutes
        else:
            return None
    except Exception:
        return None


def scrape_main_image(url, soup):
    BASE = "https://arveres.nav.gov.hu/"
    try:
        for img_tag in soup.find_all("img", fullurl=True):
            fullurl = img_tag.get("fullurl", "").strip()
            if not fullurl:
                continue
            if fullurl.startswith("http"):
                return fullurl
            elif fullurl.startswith("/"):
                return "https://arveres.nav.gov.hu" + fullurl
            else:
                return BASE + fullurl
        return None
    except Exception:
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

    # Alapadatok tábla
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

    # Tétel adatok tábla
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

    return data


def extract_html_from_message(msg):
    """Rekurzívan kinyeri a HTML tartalmat az e-mailből."""
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


def get_unread_nav_emails():
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, '(UNSEEN)')
        if status != "OK" or not messages[0]:
            logger.info("Nincs olvasatlan e-mail.")
            return []

        email_ids = messages[0].split()
        logger.info(f"Összesen {len(email_ids)} olvasatlan e-mail.")

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
                mail.store(eid, "+FLAGS", "\\Seen")
                continue

            logger.info(f"NAV e-mail: {subject_str}")
            html_body = extract_html_from_message(msg)
            if not html_body:
                # Ha nincs HTML, akkor a teljes nyers szöveget használjuk (plain text)
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                html_body = payload.decode(charset, errors="ignore")
                                break
                else:
                    if msg.get_content_type() == "text/plain":
                        payload = msg.get_payload(decode=True)
                        if payload:
                            charset = msg.get_content_charset() or "utf-8"
                            html_body = payload.decode(charset, errors="ignore")

            if html_body:
                result.append(html_body)
            else:
                logger.warning(f"Nincs tartalom a NAV e-mailben: {subject_str}")

            mail.store(eid, "+FLAGS", "\\Seen")

        mail.close()
        mail.logout()
        return result

    except Exception as e:
        logger.exception(f"IMAP hiba: {e}")
        return []


# =================== Fő logika ===================

def main():
    logger.info(f"=== NAV EAF Scraper v2.05 (teljes javítás) indítás: {datetime.now().strftime('%Y.%m.%d %H:%M')} ===")

    seen_urls = load_seen_urls()
    logger.info(f"Már ismert URL-ek száma: {len(seen_urls)}")

    emails_html = get_unread_nav_emails()
    if not emails_html:
        logger.info("Nem érkezett új NAV e-mail.")
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
    logger.info(f"Egyedi árverések (aktuális futás): {len(unique)} db")

    new_auctions = filter_new_auctions(unique, seen_urls)
    if not new_auctions:
        logger.info("Nincs új árverés a már látott URL-ekhez képest.")
        return

    # Szétválasztás ingatlan / ingóság
    real_estate = [a for a in new_auctions if is_real_estate(a)]
    other = [a for a in new_auctions if not is_real_estate(a)]
    logger.info(f"Ingatlan tételek: {len(real_estate)}, ingóságok: {len(other)}")

    # Ingóságok → fő bot
    if other:
        other_groups = group_by_name(other)
        send_grouped_messages(other_groups, bot, CHAT_ID, "ingóságok")

    # Ingatlanok → ingatlan bot (ha van)
    if real_estate:
        real_groups = group_by_name(real_estate)
        if real_estate_bot:
            send_grouped_messages(real_groups, real_estate_bot, REAL_ESTATE_CHAT_ID, "ingatlanok")
        else:
            logger.warning("Ingatlan bot nincs beállítva, ingatlanok a fő botba mennek.")
            send_grouped_messages(real_groups, bot, CHAT_ID, "ingatlanok")

    # Látott URL-ek frissítése
    for a in new_auctions:
        seen_urls.add(a["url"])
    save_seen_urls(seen_urls)

    logger.info("=== Futás befejezve ===")


if __name__ == "__main__":
    main()
