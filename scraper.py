import os
import imaplib
import email
from email.header import decode_header
import re
import requests
from bs4 import BeautifulSoup
from telegram import Bot
import asyncio
from datetime import datetime, timezone, timedelta
import logging

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
NAV_SENDER = "-eaf@nav.gov.hu"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)

def clean_text(text):
    return " ".join(text.split()) if text else ""

def extract_nav_eaf_links(html_content):
    """Kinyeri a NAV EAF oldalakra mutató linkeket az e-mail HTML tartalmából."""
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # NAV EAF domain lehet: eaf.nav.gov.hu, vagy nav.gov.hu/eaf, esetleg link.nav.gov.hu
        if "eaf.nav.gov.hu" in href or "/eaf/" in href or "nav.gov.hu" in href and "arveres" in href:
            if href.startswith("/"):
                href = "https://eaf.nav.gov.hu" + href
            links.append(href)
    return list(set(links))

def parse_nav_eaf_details(url):
    """
    Letölti a NAV EAF részletes oldalt és megpróbálja kinyerni a fontos adatokat.
    Ez a függvény várja a pontos HTML struktúrát – a jelenlegi verzió általános.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Hiba a NAV EAF oldal betöltésekor: {url} - {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"url": url}

    # Általános keresés: olyan elemek, amelyek szövege tartalmazza ezeket a kulcsszavakat
    # A pontos szelektorok a valós HTML alapján lesznek cserélve.
    # Példa mezők (a NAV EAF tipikusan használ ilyen címkéket):
    # "Jelenlegi ár:", "Árverés vége:", "Licitek száma:", "Állapot:", "Megnevezés:", "Elhelyezkedés:", stb.

    fields = {
        "jelenlegi_ar": ["Jelenlegi ár", "Aktuális ár", "Kezdő ár"],
        "hatarido": ["Árverés vége", "Befejezés", "Határidő", "Lejárat"],
        "licitek": ["Licitek száma", "Ajánlatok száma"],
        "statusz": ["Állapot", "Státusz", "Szakasz"],
        "cim": ["Cím", "Elhelyezkedés", "Helyszín"],
        "meret": ["Telek méret", "Alapterület", "Terület", "m²"]
    }

    for key, keywords in fields.items():
        found = None
        for kw in keywords:
            elem = soup.find(string=re.compile(kw, re.I))
            if elem:
                # Megpróbáljuk a szülő elemből kiolvasni a teljes szöveget
                parent = elem.parent
                full_text = clean_text(parent.get_text(strip=True))
                # Levágjuk a kulcsszót, a maradék az érték
                # Egyszerűen kivesszük a kulcsszót és a kettőspontot
                value = re.sub(rf'^{re.escape(kw)}[\s:]*', '', full_text, flags=re.I)
                found = value
                break
        data[key] = found if found else "N/A"

    # Külön a leírás (hosszabb szöveg)
    desc_elem = soup.find(class_=re.compile(r"description|leírás|reszletek", re.I))
    if not desc_elem:
        desc_elem = soup.find("div", string=re.compile(r"Leírás|Részletek", re.I))
    if desc_elem:
        data["leiras"] = clean_text(desc_elem.get_text(strip=True)[:300])
    else:
        data["leiras"] = ""

    # Ha a cím továbbra is hiányzik, próbáljuk meg a <title> vagy h1 alapján
    if data["cim"] == "N/A":
        title = soup.find("title")
        if title:
            data["cim"] = clean_text(title.get_text(strip=True))

    return data

def get_emails_since(since_date):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")
        search_criteria = f'(FROM "{NAV_SENDER}" SINCE "{since_date.strftime("%d-%b-%Y")}" UNSEEN)'
        status, messages = mail.search(None, search_criteria)
        if status != "OK":
            logger.error("IMAP keresés sikertelen.")
            return []
        email_ids = messages[0].split()
        logger.info(f"{len(email_ids)} új NAV e-mail (olvasatlan) {since_date} óta.")
        result = []
        for eid in email_ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            html_body = None
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html" and "attachment" not in str(part.get("Content-Disposition")):
                        html_body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                if msg.get_content_type() == "text/html":
                    html_body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            if html_body:
                result.append(html_body)
            # Opcionális: megjelöljük olvasottként
            # mail.store(eid, "+FLAGS", "\\Seen")
        mail.close()
        mail.logout()
        return result
    except Exception as e:
        logger.exception(f"IMAP hiba: {e}")
        return []

def send_telegram_summary(auctions):
    if not auctions:
        message = "📭 Nincs új NAV EAF árverési értesítő."
    else:
        message = f"🏛️ *NAV EAF – Új árverési értesítők* ({datetime.now().strftime('%Y-%m-%d')})\n\n"
        for idx, a in enumerate(auctions, 1):
            message += f"{idx}. *{a.get('cim', 'Cím nélkül')}*\n"
            message += f"   💰 Ár: {a.get('jelenlegi_ar', 'N/A')}\n"
            message += f"   📏 Méret: {a.get('meret', 'N/A')}\n"
            message += f"   ⏰ Vége: {a.get('hatarido', 'N/A')}\n"
            message += f"   🎲 Licit: {a.get('licitek', 'N/A')}\n"
            message += f"   📊 Státusz: {a.get('statusz', 'N/A')}\n"
            message += f"   🔗 [Részletek]({a['url']})\n\n"
    asyncio.run(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown", disable_web_page_preview=False))

def main():
    # Az előző napi reggel 8 óta (UTC-2 körüli ráhagyással)
    since = datetime.now(timezone.utc) - timedelta(days=1, hours=6)
    logger.info(f"Keresés kezdete: {since.strftime('%Y-%m-%d %H:%M')} UTC")
    emails_html = get_emails_since(since)
    if not emails_html:
        send_telegram_summary([])
        return

    all_auctions = []
    for html in emails_html:
        links = extract_nav_eaf_links(html)
        logger.info(f"Talált NAV EAF linkek: {links}")
        for link in links:
            details = parse_nav_eaf_details(link)
            if details:
                all_auctions.append(details)

    unique = {a["url"]: a for a in all_auctions}.values()
    send_telegram_summary(list(unique))

if __name__ == "__main__":
    main()
