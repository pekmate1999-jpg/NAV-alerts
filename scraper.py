import os
from telegram import Bot

token = os.environ.get("REAL_ESTATE_BOT_TOKEN")
chat_id = os.environ.get("REAL_ESTATE_CHAT_ID")

if token and chat_id:
    try:
        bot = Bot(token=token)
        me = bot.get_me()
        print(f"Bot név: {me.first_name}, username: {me.username}")
        # Próbáljunk üzenetet küldeni
        bot.send_message(chat_id=chat_id, text="Teszt üzenet az ingatlan botból")
        print("Sikeres teszt üzenet")
    except Exception as e:
        print(f"Hiba: {e}")
else:
    print("Hiányzó token vagy chat_id")
