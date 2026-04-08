import requests
import logging
from typing import Optional

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ID dei topic Telegram per ogni sport
SPORT_THREAD_ID = {
    "football": 87,
    "nba":      3,
    "baseball": 4,
}


def send_message(
    text: str,
    parse_mode: str = "HTML",
    thread_id: Optional[int] = None
) -> bool:
    """Manda un messaggio al gruppo/topic. Ritorna True se ok."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configurato, skip notifica")
        return False

    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        resp = requests.post(
            f"{TG_API}/sendMessage",
            json=payload,
            timeout=10,
        )

        if not resp.ok:
            log.error(f"Telegram error: {resp.text}")
            return False

        return True

    except Exception as e:
        log.error(f"Telegram exception: {e}")
        return False


def notify_deal(
    player_name: str,
    rarity: str,
    serial: str,
    club: str,
    price_eur: float,
    floor_eur: Optional[float],
    discount_pct: float,
    seller_slug: str,
    card_url: str,
    sport: str = "football",
    thread_id: Optional[int] = None,
) -> bool:
    sport_emoji = {"football": "⚽", "baseball": "⚾", "nba": "🏀"}.get(sport, "🃏")
    rarity_emoji = {
        "limited":    "🟡",
        "rare":       "🔴",
        "super_rare": "🔵",
        "unique":     "🟣",
    }.get(rarity.lower(), "⚪")

    floor_str = f"€{floor_eur:.2f}" if floor_eur else "N/D"

    text = (
        f"{sport_emoji} <b>AFFARE SU SORARE</b>\n\n"
        f"{rarity_emoji} <b>{player_name}</b> — {rarity.upper()} #{serial}\n"
        f"🏟 {club}\n\n"
        f"💰 Prezzo: <b>€{price_eur:.2f}</b>\n"
        f"📊 Floor: {floor_str}\n"
        f"🔥 Sconto: <b>-{discount_pct:.1f}%</b>\n\n"
        f"👤 Seller: <code>{seller_slug}</code>\n"
        f"🔗 <a href=\"{card_url}\">Apri su Sorare</a>"
    )

    if thread_id is None:
        thread_id = SPORT_THREAD_ID.get(sport)

    return send_message(text, thread_id=thread_id)


def notify_startup() -> None:
    """Manda il messaggio di avvio in tutti i topic sport."""
    for sport, thread_id in SPORT_THREAD_ID.items():
        sport_emoji = {"football": "⚽", "baseball": "⚾", "nba": "🏀"}.get(sport, "🃏")
        send_message(
            f"{sport_emoji} <b>Sorare Bot avviato</b> — in ascolto per affari...",
            thread_id=thread_id,
        )


def notify_error(msg: str) -> None:
    send_message(f"⚠️ <b>Errore bot</b>: {msg}")


def get_my_chat_id() -> Optional[int]:
    """Utility per trovare il proprio chat_id."""
    try:
        resp = requests.get(f"{TG_API}/getUpdates", timeout=10)
        updates = resp.json().get("result", [])
        if updates:
            return updates[-1]["message"]["chat"]["id"]
    except Exception as e:
        log.error(f"get_my_chat_id error: {e}")
    return None