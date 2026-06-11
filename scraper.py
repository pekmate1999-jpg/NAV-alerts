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
# Secrets-ből vagy környezeti változókból
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
NAV_SENDER = "-eaf@nav.gov.hu"  # vagy os.environ.get("NAV_SENDER")

# Logolás beállítása
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Telegram bot inicializálása
bot = Bot(token=BOT_TOKEN)

def clean_text(text):
    """Tisztítja a szöveget: whitespace-ek kezelése"""
    if not text:
        return ""
    return " ".join(text.split())

def extract_mbvk_links(html_content):
    """Kinyeri az összes MBVK oldalra mutató linket az e-mail HTML tartalmából."""
    soup = BeautifulSoup(html_content, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "mbvk.hu" in href and ("/ingatlan" in href or "/targy" in href or "/licit" in href):
            # Abszolút URL készítése (ha relatív)
            if href.startswith("/"):
                href = "https://www.mbvk.hu" + href
            links.append(href)
    return list(set(links))  # egyedi linkek

def parse_mbvk_details(url):
    """Letölti az MBVK részletes oldalt és kinyeri a fontos mezőket."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Hiba az MBVK oldal betöltésekor: {url} - {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"url": url}

    # Különböző szelektorok próbálgatása (az általad küldött kép alapján)
    # 1. Cím / elhelyezkedés
    # Gyakori class-ok: .location, .address, .cim, .title
    addr_elem = soup.find(class_=re.compile(r"cim|address|location", re.I))
    if not addr_elem:
        addr_elem = soup.find("div", string=re.compile(r"Cím|Elhelyezkedés", re.I))
    if addr_elem:
        data["cim"] = clean_text(addr_elem.get_text(strip=True))
    else:
        data["cim"] = "Nem található"

    # 2. Jelenlegi ár / szakasz árak
    # A képen: "Jelenlegi ár: 250 000 Ft", "Szakasz árak: 450 000 / 350 000 / 250 000"
    # Keresünk olyan elemet, ami tartalmazza az "ár" szót
    price_elem = soup.find(string=re.compile(r"Jelenlegi ár", re.I))
    if price_elem:
        parent = price_elem.parent
        data["jelenlegi_ar"] = clean_text(parent.get_text(strip=True))
    else:
        # Alternatíva: .price, .current-price
        price_elem2 = soup.find(class_=re.compile(r"price", re.I))
        data["jelenlegi_ar"] = clean_text(price_elem2.get_text(strip=True)) if price_elem2 else "N/A"

    # Szakasz árak
    stages_elem = soup.find(string=re.compile(r"Szakasz árak", re.I))
    if stages_elem:
        parent = stages_elem.parent
        data["szakasz_arak"] = clean_text(parent.get_text(strip=True))
    else:
        data["szakasz_arak"] = "N/A"

    # 3. Ft/m²
    unit_elem = soup.find(string=re.compile(r"Ft/m²", re.I))
    if unit_elem:
        parent = unit_elem.parent
        data["negyzetmeter_ar"] = clean_text(parent.get_text(strip=True))
    else:
        data["negyzetmeter_ar"] = "N/A"

    # 4. Telekméret / alapterület
    size_elem = soup.find(string=re.compile(r"Telekméret|Telek m[ée]rete|Alapterület", re.I))
    if size_elem:
        parent = size_elem.parent
        data["meret"] = clean_text(parent.get_text(strip=True))
    else:
        data["meret"] = "N/A"

    # 5. Árverés vége (határidő)
    deadline_elem = soup.find(string=re.compile(r"Árverés vége|Befejezés|Határidő", re.I))
    if deadline_elem:
        parent = deadline_elem.parent
        data["hatarido"] = clean_text(parent.get_text(strip=True))
    else:
        data["hatarido"] = "N/A"

    # 6. Licitek száma
    bids_elem = soup.find(string=re.compile(r"Licitek száma", re.I))
    if bids_elem:
        parent = bids_elem.parent
        data["licitek"] = clean_text(parent.get_text(strip=True))
    else:
        data["licitek"] = "N/A"

    # 7. Státusz (pl. 92%, 3. szakasz)
    status_elem = soup.find(string=re.compile(r"Státusz|Szakasz", re.I))
    if status_elem:
        parent = status_elem.parent
        data["statusz"] = clean_text(parent.get_text(strip=True))
    else:
        data["statusz"] = "N/A"

    # 8. Leírás (ha van)
    desc_elem = soup.find(class_=re.compile(r"description|leírás", re.I))
    if not desc_elem:
        desc_elem = soup.find("div", string=re.compile(r"Leírás", re.I))
    if desc_elem:
        data["leiras"] = clean_text(desc_elem.get_text(strip=True)[:300])  # rövidítve
    else:
        data["leiras"] = ""

    return data

def get_emails_since(since_date):
    """IMAP segítségével lekéri a NAV-tól érkezett, olvasatlan e-maileket since_date óta."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")
        # Keresési feltétel: feladó = NAV_SENDER, érkezett a since_date után, és olvasatlan
        search_criteria = f'(FROM "{NAV_SENDER}" SINCE "{since_date.strftime("%d-%b-%Y")}" UNSEEN)'
        status, messages = mail.search(None, search_criteria)
        if status != "OK":
            logger.error("Nem sikerült lekérdezni az e-maileket.")
            return []

        email_ids = messages[0].split()
        logger.info(f"{len(email_ids)} új email található a NAV-tól {since_date} óta.")
        result = []
        for eid in email_ids:
            status, msg_data = mail.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            # HTML tartalom kinyerése
            html_body = None
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disposition = str(part.get("Content-Disposition"))
                    if content_type == "text/html" and "attachment" not in content_disposition:
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
    """Összefoglaló üzenet küldése Telegramra."""
    if not auctions:
        message = "📭 Nincs új árverés a NAV napi értesítőjében."
    else:
        message = f"🏠 *Új NAV árverési értesítők* – {datetime.now().strftime('%Y-%m-%d')}\n\n"
        for idx, auction in enumerate(auctions, 1):
            message += f"{idx}. *{auction.get('cim', 'Cím nélkül')}*\n"
            message += f"   💰 Jelenlegi ár: {auction.get('jelenlegi_ar', 'N/A')}\n"
            message += f"   📏 Méret: {auction.get('meret', 'N/A')}\n"
            message += f"   ⏰ Árverés vége: {auction.get('hatarido', 'N/A')}\n"
            message += f"   🎲 Licitek: {auction.get('licitek', 'N/A')}\n"
            message += f"   🔗 [Részletek]({auction['url']})\n\n"
    asyncio.run(bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown", disable_web_page_preview=False))

def main():
    # Az utolsó 24 órában érkezett levelek (reggel 8-kor futva az előző napi 8-tól)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    # Mivel a Gmail SINCE a helyi idő szerint értelmezi? Biztos, ami biztos, 1 napnál kicsit többet adunk.
    # Használjuk a mai nap 00:00-t? Inkább legyen 30 óra, hogy ne maradjon ki.
    since = since - timedelta(hours=6)  # ráhagyás
    logger.info(f"Keresés {since.strftime('%Y-%m-%d %H:%M')} óta érkezett e-mailekre.")

    emails_html = get_emails_since(since)
    if not emails_html:
        send_telegram_summary([])
        return

    all_auctions = []
    for html in emails_html:
        links = extract_mbvk_links(html)
        logger.info(f"Talált MBVK linkek: {links}")
        for link in links:
            details = parse_mbvk_details(link)
            if details:
                all_auctions.append(details)

    # Duplikációk szűrése URL alapján
    unique_auctions = {a["url"]: a for a in all_auctions}.values()
    send_telegram_summary(list(unique_auctions))

if __name__ == "__main__":
    main()
