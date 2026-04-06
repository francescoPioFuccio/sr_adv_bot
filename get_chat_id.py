"""
Esegui questo script UNA VOLTA per trovare il tuo Telegram chat_id.

1. Vai su @BotFather, crea il bot, copia il token
2. Manda un qualsiasi messaggio al tuo bot su Telegram
3. Esegui: TELEGRAM_TOKEN=xxx python get_chat_id.py
"""
import os, requests

token = os.environ.get("TELEGRAM_TOKEN") or input("Token bot Telegram: ").strip()
resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates").json()

updates = resp.get("result", [])
if not updates:
    print("Nessun messaggio trovato. Manda prima un messaggio al tuo bot su Telegram, poi riesegui.")
else:
    for u in updates:
        msg = u.get("message", {})
        chat = msg.get("chat", {})
        print(f"Chat ID: {chat.get('id')}  |  Nome: {chat.get('first_name')} {chat.get('last_name','')}")
