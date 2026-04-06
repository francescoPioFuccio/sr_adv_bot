import json, logging, time, threading, queue, requests, websocket

from config import (
    SORARE_EMAIL, SORARE_PASSWORD,
    WS_URL, API_URL,
    FLOOR_DISCOUNT_PCT, MIN_FLOOR_EUR,
    RECONNECT_DELAY_SECONDS,
)
from auth import authenticate
from telegram_bot import notify_deal, notify_startup, notify_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sorare_bot")

AUD = "sorare-tg-bot"

SUBSCRIPTION = """
subscription OnNewOffer {
  anyCardWasUpdated(
    events: [offer_event_opened]
    rarities: [limited]
    sports: [FOOTBALL]
  ) {
    eventType
    card {
      slug
      serialNumber
      rarityTyped
      seasonYear
      inSeasonEligible
      sport
      anyPlayer {
        slug
        displayName
        activeClub { name }
      }
      liveSingleSaleOffer {
        receiverSide { amounts { eurCents usdCents gbpCents referenceCurrency } }
        sender { ... on User { slug } }
      }
    }
  }
}
"""

FLOOR_QUERY = """
query GetFloor($slug: String!) {
  anyPlayer(slug: $slug) {
    inSeason: lowestPriceAnyCard(inSeason: true, rarity: limited) {
      slug
      liveSingleSaleOffer {
        receiverSide {
          amounts { eurCents usdCents gbpCents referenceCurrency }
        }
      }
    }
    classic: lowestPriceAnyCard(inSeason: false, rarity: limited) {
      slug
      liveSingleSaleOffer {
        receiverSide {
          amounts { eurCents usdCents gbpCents referenceCurrency }
        }
      }
    }
  }
}
"""

# Tassi di cambio (aggiornati ogni 24h)
_fx_rates = {"USD": 1.08, "GBP": 0.86, "EUR": 1.0}
_fx_lock  = threading.Lock()


def _update_fx_rates():
    while True:
        try:
            resp = requests.get(
                "https://api.frankfurter.app/latest?from=EUR&to=USD,GBP",
                timeout=10,
            )
            rates = resp.json().get("rates", {})
            with _fx_lock:
                _fx_rates["USD"] = rates.get("USD", _fx_rates["USD"])
                _fx_rates["GBP"] = rates.get("GBP", _fx_rates["GBP"])
            log.info(f"[FX] 1 EUR = {_fx_rates['USD']:.4f} USD = {_fx_rates['GBP']:.4f} GBP")
        except Exception as e:
            log.warning(f"[FX] Errore: {e}")
        time.sleep(86400)


def to_eur(amounts: dict):
    if amounts.get("eurCents"):
        return amounts["eurCents"] / 100
    ref = (amounts.get("referenceCurrency") or "").upper()
    with _fx_lock:
        if ref == "USD" and amounts.get("usdCents"):
            return (amounts["usdCents"] / 100) / _fx_rates["USD"]
        if ref == "GBP" and amounts.get("gbpCents"):
            return (amounts["gbpCents"] / 100) / _fx_rates["GBP"]
    return None


def get_floor(player_slug: str, in_season: bool, new_card_slug: str, jwt: str):
    try:
        resp = requests.post(
            API_URL,
            json={"query": FLOOR_QUERY, "variables": {"slug": player_slug}},
            headers={
                "Authorization": f"Bearer {jwt}",
                "JWT-AUD": AUD,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("data") or not data["data"].get("anyPlayer"):
            log.warning(f"[FLOOR] Risposta inattesa: {str(data)[:150]}")
            return None

        player_data = data["data"]["anyPlayer"]
        floor_card = player_data.get("inSeason" if in_season else "classic")

        if not floor_card:
            log.info(f"[FLOOR] Nessuna carta attiva per {player_slug}")
            return None

        if floor_card.get("slug") == new_card_slug:
            log.info("[FLOOR] La carta listata è già il floor — skip")
            return None

        amounts = (
            (floor_card.get("liveSingleSaleOffer") or {})
            .get("receiverSide", {})
            .get("amounts") or {}
        )
        return to_eur(amounts)

    except Exception as e:
        log.error(f"[FLOOR] Errore: {e}", exc_info=True)
        return None


card_queue = queue.Queue(maxsize=50)
API_CALL_INTERVAL = 0.4


def card_url(slug: str, sport: str = "football") -> str:
    base = {
        "football": "https://sorare.com/football/cards",
        "baseball": "https://sorare.com/mlb/cards",
        "nba":      "https://sorare.com/nba/cards",
    }.get(sport, "https://sorare.com/football/cards")
    return f"{base}/{slug}"


def process_offer(event_data: dict, jwt: str):
    card        = event_data.get("card", {})
    card_slug   = card.get("slug", "")
    serial      = card.get("serialNumber", "?")
    rarity      = (card.get("rarityTyped") or "").lower()
    sport       = (card.get("sport") or "football").lower()
    in_season   = card.get("inSeasonEligible") or False
    player      = card.get("anyPlayer", {})
    player_name = player.get("displayName", "?")
    player_slug = player.get("slug", "")
    club        = (player.get("activeClub") or {}).get("name", "?")

    live_offer    = card.get("liveSingleSaleOffer") or {}
    price_amounts = (live_offer.get("receiverSide") or {}).get("amounts") or {}
    seller_slug   = (live_offer.get("sender") or {}).get("slug", "unknown")

    price_eur = to_eur(price_amounts)
    if not price_eur or price_eur <= 0:
        log.info("  → skip: prezzo non disponibile")
        return

    floor_eur = get_floor(player_slug, in_season, card_slug, jwt)
    if not floor_eur:
        log.info("  → skip: floor non disponibile")
        return

    if floor_eur < MIN_FLOOR_EUR:
        log.info(f"  → skip: floor €{floor_eur:.2f} < minimo €{MIN_FLOOR_EUR}")
        return

    discount_pct = (1 - price_eur / floor_eur) * 100
    tipo = "IS" if in_season else "Classic"
    log.info(f"  [{tipo}] €{price_eur:.2f} | floor €{floor_eur:.2f} | sconto {discount_pct:.1f}%")

    if discount_pct < FLOOR_DISCOUNT_PCT:
        log.info(f"  → skip: {discount_pct:.1f}% < soglia {FLOOR_DISCOUNT_PCT}%")
        return

    log.info("  🔥 AFFARE! Invio notifica...")
    notify_deal(
        player_name=player_name,
        rarity=rarity,
        serial=str(serial),
        club=club,
        price_eur=price_eur,
        floor_eur=floor_eur,
        discount_pct=discount_pct,
        seller_slug=seller_slug,
        card_url=card_url(card_slug, sport),
        sport=sport,
    )


def queue_worker(jwt: str):
    while True:
        event_data = card_queue.get()
        try:
            process_offer(event_data, jwt)
        except Exception as e:
            log.error(f"Worker error: {e}", exc_info=True)
        finally:
            card_queue.task_done()
            time.sleep(API_CALL_INTERVAL)


class SorareBot:
    def __init__(self, jwt: str):
        self.jwt     = jwt
        self.ws      = None
        self.running = False

    def on_open(self, ws):
        log.info("WebSocket connesso")
        ws.send(json.dumps({
            "command":    "subscribe",
            "identifier": json.dumps({"channel": "GraphqlChannel"}),
        }))
        time.sleep(1)
        ws.send(json.dumps({
            "command":    "message",
            "identifier": json.dumps({"channel": "GraphqlChannel"}),
            "data": json.dumps({
                "query":         SUBSCRIPTION,
                "variables":     {},
                "operationName": "OnNewOffer",
                "action":        "execute",
            }),
        }))
        log.info("Subscription attiva — in ascolto per offerte...")
        notify_startup()

    def on_message(self, ws, message):
        try:
            data     = json.loads(message)
            msg_type = data.get("type", "")
            if msg_type == "ping":
                return
            if msg_type in ("welcome", "confirm_subscription"):
                log.info(f"[WS] {msg_type}")
                return

            msg   = data.get("message", {})
            event = msg.get("result", msg).get("data", {}).get("anyCardWasUpdated")
            if not event or not event.get("card"):
                return

            player_name = event["card"].get("anyPlayer", {}).get("displayName", "?")
            serial      = event["card"].get("serialNumber", "?")
            log.info(f"[EVENTO] {player_name} | limited #{serial}")

            try:
                card_queue.put_nowait(event)
            except queue.Full:
                log.warning(f"  → coda piena, scartato: {player_name}")

        except Exception as e:
            log.error(f"on_message error: {e}")

    def on_error(self, ws, error):
        log.error(f"WebSocket errore: {error}")

    def on_close(self, ws, code, msg):
        log.warning(f"WebSocket chiuso ({code}). Riconnessione tra {RECONNECT_DELAY_SECONDS}s...")
        if self.running:
            time.sleep(RECONNECT_DELAY_SECONDS)
            self.start()

    def start(self):
        self.running = True
        self.ws = websocket.WebSocketApp(
            WS_URL,
            header={
                "Authorization": f"Bearer {self.jwt}",
                "Origin": "https://sorare.com",
            },
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            subprotocols=["actioncable-v1-json"],
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


def main():
    if not SORARE_EMAIL or not SORARE_PASSWORD:
        raise RuntimeError("Imposta SORARE_EMAIL e SORARE_PASSWORD come env vars")

    jwt = authenticate(SORARE_EMAIL, SORARE_PASSWORD, aud=AUD)

    threading.Thread(target=_update_fx_rates, daemon=True).start()
    log.info("[FX] Thread tassi avviato")

    threading.Thread(target=queue_worker, args=(jwt,), daemon=True).start()
    log.info("[WORKER] Thread coda avviato")

    bot = SorareBot(jwt)
    try:
        bot.start()
    except KeyboardInterrupt:
        log.info("Interruzione manuale")
        bot.stop()


if __name__ == "__main__":
    main()