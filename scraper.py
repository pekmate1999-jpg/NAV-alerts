import os
import imaplib
import email
import re
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from datetime import datetime, timezone, timedelta
import logging

# ------------------- Konfiguráció -------------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

NAV_SENDER_DEFAULT = "-eaf@nav.gov.hu"
NAV_SUBJECT_KEYWORDS = ["Elektronikus Árverés - Hírlevél", "Fwd: Elektronikus Árverés - Hírlevél"]

TEST_MODE = os.environ.get("TEST_MODE", "").lower() == "true"
TEST_HTML_FILE = os.environ.get("TEST_HTML_FILE", "NAV Elektronikus Árverési Felület.html")

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
            links.append(href)
    return list(set(links))

def parse_nav_eaf_details(url, html_text=None):
    if html_text is None:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(url, timeout=15, headers=headers)
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

    status_div = soup.find("div", class_="Title")
    if status_div:
        status_text = clean_text(status_div.get_text())
        if "nem lehet licitálni" in status_text:
            data["statusz"] = "Még nem lehet licitálni"
        else:
            data["statusz"] = status_text[:100]

    if "tetel_megnevezes" in data:
        data["cim"] = data["tetel_megnevezes"]
    elif "kategoria_reszletes" in data:
        data["cim"] = data["kategoria_reszletes"]
    else:
        data["cim"] = "Ismeretlen tétel"

    data["jelenlegi_ar"] = data.get("becsertek", "N/A")
    return data

def get_emails_since(since_date):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")

        email_ids = []
        # Feladó alapján
        search_criteria_sender = f'(FROM "{NAV_SENDER_DEFAULT}" SINCE "{since_date.strftime("%d-%b-%Y")}")'
        status, messages = mail.search(None, search_criteria_sender)
        if status == "OK" and messages[0]:
            email_ids.extend(messages[0].split())
            logger.info(f"Feladó alapján {len(messages[0].split())} e-mail található.")

        # Tárgy kulcsszavak alapján
        for keyword in NAV_SUBJECT_KEYWORDS:
            search_criteria_subj = f'(SUBJECT "{keyword}" SINCE "{since_date.strftime("%d-%b-%Y")}")'
            status, messages = mail.search(None, search_criteria_subj)
            if status == "OK" and messages[0]:
                new_ids = messages[0].split()
                email_ids.extend(new_ids)
                logger.info(f"Tárgy '{keyword}' alapján {len(new_ids)} e-mail található.")

        email_ids = list(set(email_ids))
        logger.info(f"Összesen {len(email_ids)} egyedi e-mail található {since_date} óta.")

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
            mail.store(eid, "+FLAGS", "\\Seen")
        mail.close()
        mail.logout()
        return result
    except Exception as e:
        logger.exception(f"IMAP hiba: {e}")
        return []

def send_telegram_summary(auctions):
    if not auctions:
        message = "📭 Nincs új NAV EAF árverési értesítő az elmúlt 24 órában."
    else:
        message = f"🏛️ *NAV EAF – Új árverési értesítők* ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n"
        for idx, a in enumerate(auctions, 1):
            message += f"{idx}. *{a.get('cim', 'Cím nélkül')}*\n"
            message += f"   🏷️ Kategória: {a.get('kategoria_reszletes', 'N/A')}\n"
            message += f"   💰 Ár (becsérték): {a.get('jelenlegi_ar', 'N/A')}\n"
            message += f"   📦 Tétel: {a.get('tetel_megnevezes', 'N/A')}\n"
            message += f"   📅 Kezdés: {a.get('kezdet', 'N/A')}\n"
            message += f"   ⏰ Befejezés: {a.get('befejezes', 'N/A')}\n"
            message += f"   📍 Megtekintés: {a.get('megtekintes_hely', 'N/A')}\n"
            if a.get('egyeb_info'):
                message += f"   📝 Infó: {a.get('egyeb_info')[:80]}\n"
            message += f"   🔗 [Részletek]({a['url']})\n\n"
    bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown", disable_web_page_preview=False)

def test_with_local_file(file_path):
    """Teszt mód: helyi HTML fájlból olvas (ISO-8859-2 kódolással)."""
    logger.info(f"Teszt mód: helyi fájl beolvasása: {file_path}")
    if not os.path.exists(file_path):
        logger.error(f"A fájl nem található: {file_path}")
        return
    # A HTML fájl ISO-8859-2 kódolású, ezt kell használni
    with open(file_path, "r", encoding="iso-8859-2") as f:
        html = f.read()
    # A tesztben a fájl maga a részletes oldal (nem e-mail), ezért közvetlenül meghívjuk a parsert
    details = parse_nav_eaf_details("file://" + os.path.abspath(file_path), html_text=html)
    if details:
        send_telegram_summary([details])
    else:
        logger.error("Nem sikerült kinyerni az adatokat a helyi fájlból.")

def main():
    if TEST_MODE:
        test_with_local_file(TEST_HTML_FILE)
        return

    since = datetime.now(timezone.utc) - timedelta(days=1)
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
