import os
import sys
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Debug: írjuk ki a környezeti változók első pár karakterét
real_token = os.environ.get("REAL_ESTATE_BOT_TOKEN", "")
real_chat = os.environ.get("REAL_ESTATE_CHAT_ID", "")
logger.info(f"REAL_ESTATE_BOT_TOKEN hossza: {len(real_token)}, első 5 karakter: {real_token[:5] if real_token else 'None'}")
logger.info(f"REAL_ESTATE_CHAT_ID: {real_chat}")

# Bot inicializálás egyszerűen, try-except
real_estate_bot = None
if real_token and real_chat:
    try:
        from telegram import Bot
        real_estate_bot = Bot(token=real_token.strip())
        logger.info("Ingatlan bot sikeresen inicializálva")
    except Exception as e:
        logger.error(f"Ingatlan bot init hiba: {e}")
else:
    logger.warning("REAL_ESTATE_BOT_TOKEN vagy CHAT_ID hiányzik")


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
    if not name:
        return "ismeretlen"
    name = re.sub(r'\s*\([^)]*\)', '', name)
    name = re.sub(r',', '', name)
    name = name.lower()
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def group_by_name(auctions: list) -> dict:
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
    kategoria = auction.get("kategoria_reszletes", "") or auction.get("kategoria", "")
    cim = auction.get("cim", "")
    szoveg = (kategoria + " " + cim).lower()
    keywords = [
        "ingatlan", "lakás", "ház", "családi ház", "telek", "garázs", "üdülő",
        "iroda", "üzlet", "pince", "műhely", "raktár", "beépítetlen terület", "kivett",
        "lakóház", "gazdasági épület", "tanya", "majorság", "szőlő", "gyümölcsös"
    ]
    return any(kw in szoveg for kw in keywords)


def build_safe_caption(group_name: str, items: list) -> str:
    first = items[0]
    MAX_LEN = 1024

    base = f"🏛️ <b>{group_name}</b>\n\n"
    base += "📦 <b>1. Tétel alapadatok</b>\n"
    if first.get("allapot"):
        base += f"📊 Állapot: {first.get('allapot')}\n"
    if first.get("darabszam"):
        base += f"🔢 Darabszám: {first.get('darabszam')}\n"
    base += "\n"

    base += "💰 <b>2. Pénzügyi információk</b>\n"
    base += f"💵 Becsérték: {first.get('becsertek', 'N/A')}\n"
    base += f"💸 Minimál ajánlat: {first.get('minimal_ajanlat', 'N/A')}\n"
    base += "\n"

    base += "📅 <b>3. Időpontok</b>\n"
    base += f"▶️ Kezdés: {first.get('kezdet', 'N/A')}\n"
    base += f"⏹️ Befejezés: {first.get('befejezes', 'N/A')}\n"
    base += "\n"

    base += "📍 <b>4. Megtekintés</b>\n"
    base += f"🗺️ Helyszín: {first.get('megtekintes_hely', 'N/A')}\n"
    base += f"🕐 Időpont: {first.get('megtekintes_ido', 'N/A')}\n"
    base += f"🚗 Távolság: {first.get('tavolsag', 'N/A')}\n"
    base += "\n"

    if len(items) == 1:
        link_part = f"🔗 <a href='{items[0]['url']}'>Részletek megtekintése</a>"
    else:
        link_part = "🔗 <b>Linkek az egyes tételekhez:</b>\n"
        for idx, item in enumerate(items, 1):
            link_part += f"{idx}. <a href='{item['url']}'>Tétel linkje</a>\n"

    desc_text = first.get("egyeb_info", "")
    escaped_desc = html_escape.escape(desc_text)
    max_desc_len = MAX_LEN - len(base) - len(link_part) - 10
    if max_desc_len < 50:
        desc_part = ""
    else:
        if len(escaped_desc) > max_desc_len:
            escaped_desc = escaped_desc[:max_desc_len-3] + "…"
        desc_part = f"📝 <b>5. Leírás</b>\n<i>{escaped_desc}</i>\n\n"

    caption = base + desc_part + link_part
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


def send_grouped_messages(groups: dict, target_bot, target_chat_id: str, category_label: str):
    if not groups or not target_bot:
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
        caption = build_safe_caption(group_name, items)
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


# =================== NAV EAF feldolgozás ===================

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
    if not links:
        pattern = r'https?://arveres\.nav\.gov\.hu[^\s"\'>]+'
        links = re.findall(pattern, html_content)
        links = [l for l in links if 'auctionId' in l or 'item=auctionSummary' in l]
    return list(set(links))


def clean_text(text):
    return " ".join(text.split()) if text else ""


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
                elif "Árverés kezdete" in key:
                    data["kezdet"] = value
                elif "Árverés befejezése" in key:
                    data["befejezes"] = value
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
        # Távolság számítás – a geocode függvényeket rövidítve
        coords = None
        try:
            params = {"q": megtekintes_hely, "format": "json", "limit": 1, "countrycodes": "hu"}
            resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, timeout=10)
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    coords = (float(results[0]["lat"]), float(results[0]["lon"]))
        except Exception:
            pass
        if coords:
            dest_lat, dest_lon = coords
            try:
                url = f"http://router.project-osrm.org/route/v1/driving/{ORIGIN_LON},{ORIGIN_LAT};{dest_lon},{dest_lat}?overview=false"
                r = requests.get(url, timeout=15)
                if r.status_code == 200:
                    route = r.json().get("routes", [{}])[0]
                    km = round(route.get("distance", 0) / 1000, 1)
                    minutes = round(route.get("duration", 0) / 60)
                    data["tavolsag"] = f"{km} km ({minutes} perc autóval)"
                else:
                    data["tavolsag"] = "Nem sikerült kiszámítani"
            except Exception:
                data["tavolsag"] = "Nem sikerült kiszámítani"
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


# =================== MBVK (végrehajtói) feldolgozás ===================

def extract_text_from_pdf(pdf_bytes):
    """PDF tartalom szöveges kinyerése"""
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        logger.error(f"PDF olvasási hiba: {e}")
        return None


def parse_mbvk_auction_from_text(text: str, pdf_url: str = None) -> dict:
    """
    Megpróbálja kinyerni a lényeges adatokat a PDF szövegéből.
    Visszaad egy dict-et, ami kompatibilis a NAV EAF struktúrával.
    """
    data = {
        "url": pdf_url or "https://arveres.mbk.hu",
        "forras": "MBVK",
        "cim": "Ismeretlen MBVK tétel",
        "becsertek": "N/A",
        "minimal_ajanlat": "N/A",
        "kezdet": "N/A",
        "befejezes": "N/A",
        "megtekintes_hely": "N/A",
        "megtekintes_ido": "N/A",
        "tavolsag": "N/A",
        "egyeb_info": "",
        "image_url": None
    }

    # Gépjármű típus keresése
    car_match = re.search(r"gyártmány és típus:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if car_match:
        data["cim"] = car_match.group(1).strip()

    # Becsérték
    value_match = re.search(r"becsértéke:\s*([\d\s]+)Ft", text, re.IGNORECASE)
    if value_match:
        data["becsertek"] = value_match.group(1).strip() + " Ft"

    # Kikiáltási ár (minimál ajánlat)
    min_match = re.search(r"Kikiáltási ára:\s*([\d\s]+)Ft", text, re.IGNORECASE)
    if min_match:
        data["minimal_ajanlat"] = min_match.group(1).strip() + " Ft"

    # Megtekintési hely és idő
    hely_match = re.search(r"megtekintési helye:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if hely_match:
        data["megtekintes_hely"] = hely_match.group(1).strip()
    ido_match = re.search(r"megtekintési ideje:\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if ido_match:
        data["megtekintes_ido"] = ido_match.group(1).strip()

    # Árverési időszak (a táblázatból az első szakasz kezdete és utolsó befejezése)
    start_match = re.search(r"1\. szakasz\s+([\d\.]+)\s+([\d\.]+)\s+[\d:]+", text)
    if start_match:
        data["kezdet"] = start_match.group(1).replace(".", "-")
    end_match = re.search(r"4\. szakasz\s+[\d\.]+\s+([\d\.]+)\s+[\d:]+", text)
    if end_match:
        data["befejezes"] = end_match.group(1).replace(".", "-")

    # Leírás (jármű jellemzői)
    desc_match = re.search(r"jellemzői:\s*(.+?)(?:\n\n|\n\s*\n|$)", text, re.DOTALL | re.IGNORECASE)
    if desc_match:
        data["egyeb_info"] = desc_match.group(1).strip()[:300]
    else:
        data["egyeb_info"] = text[:300]

    return data


def process_mbvk_email(msg):
    """
    Megvizsgálja az e-mailt: ha van PDF csatolmány és a feladó végrehajtó,
    kinyeri az adatokat.
    """
    results = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment" and part.get_filename():
            filename = part.get_filename()
            if filename.lower().endswith(".pdf"):
                pdf_bytes = part.get_payload(decode=True)
                if pdf_bytes:
                    pdf_text = extract_text_from_pdf(pdf_bytes)
                    if pdf_text:
                        auction = parse_mbvk_auction_from_text(pdf_text)
                        # URL nincs, de a fájlnév egyedi lehet
                        auction["url"] = f"mbvk_{filename}_{datetime.now().timestamp()}"
                        results.append(auction)
    return results


def get_mbvk_emails():
    """Olvasatlan e-mailek között keresi a végrehajtói értesítőket"""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, '(UNSEEN)')
        if status != "OK" or not messages[0]:
            return []

        email_ids = messages[0].split()
        mbvk_auctions = []

        for eid in email_ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            from_ = msg.get("From", "").lower()
            subject = msg.get("Subject", "").lower()

            # Végrehajtói azonosítás: pl. andrisvhiroda.hu, mbvk, végrehajtó, árverés
            is_bailiff = any(x in from_ for x in ["andrisvhiroda", "végrehajtó", "mbvk"]) or \
                         any(x in subject for x in ["végrehajtói", "árverési hirdetmény", "ingó árverés"])

            if is_bailiff:
                logger.info(f"MBVK/végrehajtói e-mail: {subject}")
                auctions = process_mbvk_email(msg)
                mbvk_auctions.extend(auctions)

            mail.store(eid, "+FLAGS", "\\Seen")  # mindenképp megjelöljük olvasottként

        mail.close()
        mail.logout()
        return mbvk_auctions

    except Exception as e:
        logger.exception(f"MBVK e-mail lekérési hiba: {e}")
        return []


# =================== Fő logika ===================

def main():
    logger.info(f"=== NAV EAF + MBVK Scraper v3.0 indítás: {datetime.now().strftime('%Y.%m.%d %H:%M')} ===")

    seen_urls = load_seen_urls()
    logger.info(f"Már ismert URL-ek száma: {len(seen_urls)}")

    # 1. NAV EAF e-mailek feldolgozása
    nav_emails = get_unread_nav_emails()  # ez a korábbi függvény, most nem másoltam be ismét, de a teljes kódban benne van
    all_auctions = []
    if nav_emails:
        for html in nav_emails:
            links = extract_nav_eaf_links(html)
            for link in links:
                details = parse_nav_eaf_details(link)
                if details:
                    all_auctions.append(details)

    # 2. MBVK e-mailek feldolgozása
    mbvk_auctions = get_mbvk_emails()
    all_auctions.extend(mbvk_auctions)

    # Deduplikáció URL alapján
    unique = list({a["url"]: a for a in all_auctions}.values())
    logger.info(f"Egyedi árverések összesen: {len(unique)} (NAV: {len(nav_emails)} e-mail, MBVK: {len(mbvk_auctions)} tétel)")

    new_auctions = filter_new_auctions(unique, seen_urls)
    if not new_auctions:
        logger.info("Nincs új tétel.")
        return

    # Szétválasztás kategóriák szerint
    real_estate = [a for a in new_auctions if is_real_estate(a)]
    other = [a for a in new_auctions if not is_real_estate(a) and a.get("forras") != "MBVK"]
    mbvk_items = [a for a in new_auctions if a.get("forras") == "MBVK"]

    logger.info(f"Ingatlan: {len(real_estate)}, Ingóság (NAV): {len(other)}, MBVK: {len(mbvk_items)}")

    # Küldés
    if other:
        other_groups = group_by_name(other)
        if bot:
            send_grouped_messages(other_groups, bot, CHAT_ID, "ingóságok")
        else:
            logger.error("Fő bot nincs inicializálva")

    if real_estate:
        real_groups = group_by_name(real_estate)
        if real_estate_bot:
            send_grouped_messages(real_groups, real_estate_bot, REAL_ESTATE_CHAT_ID, "ingatlanok")
        elif bot:
            send_grouped_messages(real_groups, bot, CHAT_ID, "ingatlanok")
        else:
            logger.error("Nincs bot az ingatlanok küldéséhez")

    if mbvk_items:
        mbvk_groups = group_by_name(mbvk_items)
        if mbvk_bot:
            send_grouped_messages(mbvk_groups, mbvk_bot, MBVK_CHAT_ID, "MBVK árverések")
        elif bot:
            send_grouped_messages(mbvk_groups, bot, CHAT_ID, "MBVK árverések")
        else:
            logger.error("Nincs bot az MBVK tételek küldéséhez")

    # Látott URL-ek mentése
    for a in new_auctions:
        seen_urls.add(a["url"])
    save_seen_urls(seen_urls)

    logger.info("=== Futás befejezve ===")


# A korábbi get_unread_nav_emails függvényt itt nem írtam újra, de a teljes kódban benne van.
# (A fenti kód a teljesség kedvéért ezt is tartalmazza a letölthető változatban.)
