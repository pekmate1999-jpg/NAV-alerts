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

# --- Konfiguráció ---
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

ORIGIN_LAT, ORIGIN_LON = 47.4344, 19.2198
SEEN_URLS_FILE = os.path.join(os.path.dirname(__file__), "seen_urls.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# A bot inicializálása
bot = Bot(token=BOT_TOKEN)

# --- Segédfüggvények (változatlan logikával) ---
def load_seen_urls() -> set:
    if not os.path.exists(SEEN_URLS_FILE): return set()
    try:
        with open(SEEN_URLS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("seen_urls", []))
    except: return set()

def save_seen_urls(seen: set):
    with open(SEEN_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_urls": sorted(seen)}, f, ensure_ascii=False, indent=2)

def extract_nav_eaf_links(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    links = {a["href"] for a in soup.find_all("a", href=True) if "arveres.nav.gov.hu" in a["href"] and ("auctionId" in a["href"] or "item=auctionSummary" in a["href"])}
    return ["https://arveres.nav.gov.hu" + l if l.startswith("/") else l.replace("nav.gov.hu//", "nav.gov.hu/") for l in links]

def get_emails_since(since_date):
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(EMAIL, PASSWORD)
    mail.select("inbox")
    status, messages = mail.search(None, f'(SINCE "{since_date.strftime("%d-%b-%Y")}")')
    results = []
    if status == "OK" and messages[0]:
        for eid in messages[0].split():
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            # Itt lehetne szűrést tenni, ha csak az olvasatlan kell (UNSEEN helyett)
            results.append(msg)
            mail.store(eid, "+FLAGS", "\\Seen")
    mail.logout()
    return results

# --- Aszinkron Telegram függvények ---
async def send_telegram_messages(auctions: list):
    if not auctions:
        await bot.send_message(chat_id=CHAT_ID, text="📭 Nincs új tétel.")
        return

    # Csoportosítás... (maradhat a korábbi logikád)
    await bot.send_message(chat_id=CHAT_ID, text=f"🔔 <b>{len(auctions)} új tétel érkezett.</b>", parse_mode="HTML")
    
    for a in auctions:
        caption = f"🏛️ <b>{a.get('cim', 'N/A')}</b>\n🔗 <a href='{a['url']}'>Részletek</a>"
        try:
            # Itt a legfontosabb: AWAIT használata
            await bot.send_message(chat_id=CHAT_ID, text=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Hiba: {e}")

async def main():
    logger.info("Indítás...")
    seen_urls = load_seen_urls()
    
    # E-mailek feldolgozása
    emails = get_emails_since(datetime.now(timezone.utc) - timedelta(days=1))
    
    all_auctions = []
    for msg in emails:
        # html kinyerése és linkek gyűjtése...
        # ... parse_nav_eaf_details hívása ...
        pass
    
    new_auctions = [a for a in all_auctions if a["url"] not in seen_urls]
    
    # Telegram küldés
    await send_telegram_messages(new_auctions)
    
    # Mentés
    for a in new_auctions: seen_urls.add(a["url"])
    save_seen_urls(seen_urls)
    logger.info("Kész.")

if __name__ == "__main__":
    # Az aszinkron futtatás belépési pontja
    asyncio.run(main())
