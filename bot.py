import json, logging, time, threading, queue, requests, websocket
from datetime import datetime, timezone, timedelta
import os # <--- Aggiunto per leggere la porta di Render
from flask import Flask # <--- Nuova dipendenza per il web server

from config import (
    SORARE_EMAIL, SORARE_PASSWORD,
    WS_URL, API_URL,
    FLOOR_DISCOUNT_PCT, MIN_FLOOR_EUR, MIN_PRICE_EUR,
    RECONNECT_DELAY_SECONDS,
    SPORTS, RARITIES,
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

# --- AGGIUNTA: FINTO SERVER WEB PER RENDER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    # Questo è l'endpoint che UptimeRobot chiamerà
    return "Bot is alive!", 200

def run_web_server():
    # Render assegna una porta tramite variabile d'ambiente PORT
    port = int(os.environ.get("PORT", 8080))
    # Avviamo Flask in modalità silenziosa
    app.run(host='0.0.0.0', port=port)
# --------------------------------------------

# Mapping da config SPORTS -> enum GraphQL
SPORT_ENUM = {
    "football": "FOOTBALL",
    "nba": "NBA",
    "baseball": "BASEBALL",
}

# Rarities config -> enum GraphQL (già lowercase nel config)
RARITY_ENUM = {
    "limited": "limited",
    "rare": "rare",
    "super_rare": "super_rare",
    "unique": "unique",
}


def _build_subscription() -> str:
    sports_gql = ", ".join(SPORT_ENUM[s] for s in SPORTS if s in SPORT_ENUM)
    rarities_gql = ", ".join(RARITY_ENUM[r] for r in RARITIES if r in RARITY_ENUM)
    return f"""
subscription OnNewOffer {{
  anyCardWasUpdated(
    events: [offer_event_opened]
    rarities: [{rarities_gql}]
    sports: [{sports_gql}]
  ) {{
    eventType
    card {{
      slug
      serialNumber
      rarityTyped
      seasonYear
      inSeasonEligible
      sport
      anyPlayer {{
        slug
        displayName
        activeClub {{ name }}
      }}
      liveSingleSaleOffer {{
        createdAt
        receiverSide {{ amounts {{ eurCents usdCents gbpCents referenceCurrency }} }}
        sender {{ ... on User {{ slug }} }}
      }}
    }}
  }}
}}
"""


LIVE_LISTINGS_QUERY = """
query GetPlayerLiveListings($playerSlug: String!, $sport: Sport!, $last: Int = 100) {
  tokens {
    liveSingleSaleOffers(
      playerSlug: $playerSlug
      sport: $sport
      last: $last
    ) {
      nodes {
        id
        status
        senderSide {
          anyCards {
            slug
            serialNumber
            rarityTyped
            seasonYear
            inSeasonEligible
            grade
            power
          }
        }
        receiverSide {
          amounts {
            eurCents
            usdCents
            gbpCents
            wei
          }
        }
      }
    }
  }
}
"""

# Tassi di cambio (aggiornati periodicamente)
# USD e GBP ogni 24h, ETH ogni ora (più volatile)
_fx_rates = {"USD": 1.08, "GBP": 0.86, "EUR": 1.0, "ETH": 1800.0}
_fx_lock = threading.Lock()


def _update_fx_rates():
    """Aggiorna tassi di cambio: USD/GBP ogni 24h, ETH ogni ora."""
    last_eth_update = 0

    while True:
        try:
            # Aggiorna USD e GBP (ogni 24h)
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
            log.warning(f"[FX] Errore aggiornamento USD/GBP: {e}")

        # Aggiorna ETH ogni ora (più volatile)
        current_time = time.time()
        if current_time - last_eth_update >= 3600:  # 1 ora
            try:
                resp_eth = requests.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=eur",
                    timeout=10,
                )
                eth_price = resp_eth.json().get("ethereum", {}).get("eur")
                if eth_price:
                    with _fx_lock:
                        _fx_rates["ETH"] = eth_price
                    log.info(f"[FX] 1 ETH = €{_fx_rates['ETH']:.2f}")
                    last_eth_update = current_time
            except Exception as e:
                log.warning(f"[FX] Errore aggiornamento ETH: {e}")

        time.sleep(3600)  # Check ogni ora (ETH sarà aggiornato, USD/GBP solo ogni 24h)


def to_eur(amounts: dict) -> float | None:
    """Converte importi in EUR. Supporta: eurCents, usdCents, gbpCents, wei."""
    if amounts.get("eurCents"):
        return amounts["eurCents"] / 100
    with _fx_lock:
        if amounts.get("usdCents"):
            return (amounts["usdCents"] / 100) / _fx_rates["USD"]
        if amounts.get("gbpCents"):
            return (amounts["gbpCents"] / 100) / _fx_rates["GBP"]
        if amounts.get("wei"):
            eth = int(amounts["wei"]) / 1e18  # wei → ETH
            return eth * _fx_rates["ETH"]  # ETH → EUR
    return None


def get_all_listings(player_slug: str, sport: str, jwt: str) -> list[dict]:
    """
    Ritorna tutti i listing live del giocatore per un dato sport.
    sport deve essere il valore enum GraphQL: FOOTBALL, NBA, BASEBALL
    Usa una cache TTL=10min per evitare 429.
    """
    cache_key = (player_slug, sport)
    now = time.time()

    with _listings_cache_lock:
        cached = _listings_cache.get(cache_key)
        if cached and (now - cached["ts"]) < LISTINGS_CACHE_TTL:
            age = int(now - cached["ts"])
            log.info(f"[LISTINGS] {player_slug} ({sport}): {len(cached['listings'])} listing da cache (eta {age}s)")
            return cached["listings"]

    # Rate limiter globale: rispetta il limite 60 req/min di Sorare
    global _api_last_call_ts
    with _api_rate_lock:
        now_rl = time.time()
        elapsed = now_rl - _api_last_call_ts
        if elapsed < API_MIN_INTERVAL:
            time.sleep(API_MIN_INTERVAL - elapsed)
        _api_last_call_ts = time.time()

    try:
        resp = requests.post(
            API_URL,
            json={
                "query": LIVE_LISTINGS_QUERY,
                "variables": {
                    "playerSlug": player_slug,
                    "sport": sport,
                    "last": 100,
                },
            },
            headers={
                "Authorization": f"Bearer {jwt}",
                "JWT-AUD": AUD,
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "?")
            log.warning(f"[LISTINGS] ⚠️ 429 RATE LIMIT per {player_slug} ({sport}) — Retry-After: {retry_after}s")
            return []

        if not resp.ok:
            log.error(f"[LISTINGS] ❌ HTTP {resp.status_code} per {player_slug} ({sport}): {resp.text[:200]}")
            return []

        data = resp.json()

        # Errori GraphQL espliciti (es. 429 wrappato nel body)
        if "errors" in data:
            log.error(f"[LISTINGS] ❌ Errore GraphQL per {player_slug} ({sport}): {data['errors']}")
            return []

        nodes = (
            data.get("data", {})
            .get("tokens", {})
            .get("liveSingleSaleOffers", {})
            .get("nodes", [])
        )

        listings = []
        for node in nodes:
            amounts = (node.get("receiverSide") or {}).get("amounts") or {}
            price_eur = to_eur(amounts)
            if not price_eur:
                continue
            cards = (node.get("senderSide") or {}).get("anyCards") or []
            for card in cards:
                listings.append({
                    "slug": card.get("slug", ""),
                    "price_eur": price_eur,
                    "rarity": card.get("rarityTyped", ""),
                    "in_season": card.get("inSeasonEligible", False),
                    "season": card.get("seasonYear", 0),
                    "serial": card.get("serialNumber", 0),
                    "grade": card.get("grade", 0),
                    "power": card.get("power", "1.000"),
                })

        log.info(f"[LISTINGS] {player_slug} ({sport}): {len(listings)} listing trovati -- salvati in cache")
        with _listings_cache_lock:
            _listings_cache[cache_key] = {"ts": time.time(), "listings": listings}
        return listings

    except Exception as e:
        log.error(f"[LISTINGS] ❌ Eccezione per {player_slug} ({sport}): {e}", exc_info=True)
        return []


def compute_floor(
        listings: list[dict],
        new_card_slug: str,
        rarity: str,
        in_season: bool,
) -> float | None:
    if in_season:
        # IS: floor solo tra le altre IS
        others = [
            l for l in listings
            if l["slug"] != new_card_slug
               and l["rarity"] == rarity
               and l["in_season"] == True
        ]
        if not others:
            log.info(f"[FLOOR] Nessun altro listing IS rarity={rarity} oltre alla carta nuova")
            return None
        floor = min(l["price_eur"] for l in others)
        log.info(f"[FLOOR] Floor IS calcolato su {len(others)} listing: €{floor:.2f}")
        return floor
    else:
        # Classic: floor = min(floor_classic, floor_IS) perché le IS sono intercambiabili
        classic_others = [
            l for l in listings
            if l["slug"] != new_card_slug
               and l["rarity"] == rarity
               and l["in_season"] == False
        ]
        is_others = [
            l for l in listings
            if l["rarity"] == rarity
               and l["in_season"] == True
        ]

        floors = []
        if classic_others:
            floor_classic = min(l["price_eur"] for l in classic_others)
            floors.append(floor_classic)
            log.info(f"[FLOOR] Floor Classic su {len(classic_others)} listing: €{floor_classic:.2f}")
        if is_others:
            floor_is = min(l["price_eur"] for l in is_others)
            floors.append(floor_is)
            log.info(f"[FLOOR] Floor IS (riferimento) su {len(is_others)} listing: €{floor_is:.2f}")

        if not floors:
            log.info(f"[FLOOR] Nessun listing rarity={rarity} oltre alla carta nuova")
            return None

        floor = min(floors)
        log.info(f"[FLOOR] Floor effettivo Classic (min tra Classic e IS): €{floor:.2f}")
        return floor


card_queue = queue.Queue(maxsize=200)
API_CALL_INTERVAL = 0.4
NUM_WORKERS = 2
HEARTBEAT_TIMEOUT_SECONDS = 90

# Deduplicazione: set degli slug già in coda
_queued_slugs: set[str] = set()
_queued_slugs_lock = threading.Lock()

# Cache listings: key = (player_slug, sport) -> {"ts": float, "listings": list}
_listings_cache: dict[tuple, dict] = {}
_listings_cache_lock = threading.Lock()
LISTINGS_CACHE_TTL = 600  # 10 minuti

# Rate limiter: max 1 chiamata API al secondo (limite JWT Sorare: 60/min)
_api_rate_lock = threading.Lock()
_api_last_call_ts: float = 0.0
API_MIN_INTERVAL = 1.1  # secondi tra una chiamata e l'altra (60/min con margine)


def card_url(slug: str, sport: str = "FOOTBALL") -> str:
    base = {
        "FOOTBALL": "https://sorare.com/football/cards",
        "BASEBALL": "https://sorare.com/mlb/cards",
        "NBA": "https://sorare.com/nba/cards",
    }.get(sport.upper(), "https://sorare.com/football/cards")
    return f"{base}/{slug}"


def process_offer(event_data: dict, jwt: str):
    card = event_data.get("card", {})
    card_slug = card.get("slug", "")
    serial = card.get("serialNumber", "?")
    rarity = (card.get("rarityTyped") or "").lower()
    sport = (card.get("sport") or "FOOTBALL").upper()  # FOOTBALL, NBA, BASEBALL
    in_season = card.get("inSeasonEligible") or False
    player = card.get("anyPlayer", {})
    player_name = player.get("displayName", "?")
    player_slug = player.get("slug", "")
    club = (player.get("activeClub") or {}).get("name", "?")

    live_offer = card.get("liveSingleSaleOffer") or {}
    price_amounts = (live_offer.get("receiverSide") or {}).get("amounts") or {}
    seller_slug = (live_offer.get("sender") or {}).get("slug", "unknown")

    price_eur = to_eur(price_amounts)
    if not price_eur or price_eur <= 0:
        log.info("  → skip: prezzo non disponibile")
        return

    if price_eur < MIN_PRICE_EUR:
        log.info(f"  → skip: prezzo €{price_eur:.2f} < minimo €{MIN_PRICE_EUR}")
        return

    # Listing filtrati per sport (passa enum GraphQL direttamente)
    all_listings = get_all_listings(player_slug, sport, jwt)
    if not all_listings:
        log.info("  → skip: nessun listing trovato")
        return

    floor_eur = compute_floor(all_listings, card_slug, rarity, in_season)
    if not floor_eur:
        log.info("  → skip: floor non calcolabile (unica carta listata per questo tipo)")
        return

    if floor_eur < MIN_FLOOR_EUR:
        log.info(f"  → skip: floor €{floor_eur:.2f} < minimo €{MIN_FLOOR_EUR}")
        return

    discount_pct = (1 - price_eur / floor_eur) * 100
    tipo = "IS" if in_season else "Classic"
    log.info(f"  [{sport}][{tipo}] €{price_eur:.2f} | floor €{floor_eur:.2f} | sconto {discount_pct:.1f}%")

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
        sport=sport.lower(),
    )


def queue_worker(jwt: str):
    while True:
        event_data = card_queue.get()
        player_slug = (event_data.get("card", {}).get("anyPlayer") or {}).get("slug", "")
        try:
            process_offer(event_data, jwt)
        except Exception as e:
            log.error(f"Worker error: {e}", exc_info=True)
        finally:
            with _queued_slugs_lock:
                _queued_slugs.discard(player_slug)
            card_queue.task_done()
            time.sleep(API_CALL_INTERVAL)


class SorareBot:
    def __init__(self, jwt: str):
        self.jwt = jwt
        self.ws = None
        self.running = False
        self.subscription = _build_subscription()
        self.last_message_time = time.time()

    def heartbeat_watchdog(self):
        while True:
            time.sleep(30)

            silence = time.time() - self.last_message_time

            if silence > HEARTBEAT_TIMEOUT_SECONDS:
                log.warning(f"[WATCHDOG] FREEZE DETECTED ({int(silence)}s) — HARD RESTART")

                try:
                    if self.ws:
                        self.ws.keep_running = False
                        self.ws.close()
                except:
                    pass

                # 🔥 IMPORTANTISSIMO: esci dal thread
                return

    def on_open(self, ws):
        log.info("WebSocket connesso")
        self.last_message_time = time.time()
        ws.send(json.dumps({
            "command": "subscribe",
            "identifier": json.dumps({"channel": "GraphqlChannel"}),
        }))
        time.sleep(1)
        ws.send(json.dumps({
            "command": "message",
            "identifier": json.dumps({"channel": "GraphqlChannel"}),
            "data": json.dumps({
                "query": self.subscription,
                "variables": {},
                "operationName": "OnNewOffer",
                "action": "execute",
            }),
        }))
        sports_str = ", ".join(s.upper() for s in SPORTS)
        log.info(f"Subscription attiva — sport: [{sports_str}]")
        notify_startup()

    def on_message(self, ws, message):
        try:
            self.last_message_time = time.time()
            data = json.loads(message)
            msg_type = data.get("type", "")
            if msg_type == "ping":
                return
            if msg_type in ("welcome", "confirm_subscription"):
                log.info(f"[WS] {msg_type}")
                return

            msg = data.get("message", {})
            event = msg.get("result", msg).get("data", {}).get("anyCardWasUpdated")
            if not event or not event.get("card"):
                return

            # Filtro createdAt: scarta offerte più vecchie di 5 minuti
            live_offer = event["card"].get("liveSingleSaleOffer") or {}
            created_at_str = live_offer.get("createdAt")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    age = datetime.now(timezone.utc) - created_at
                    if age > timedelta(minutes=5):
                        log.info(f"  → skip: offerta vecchia {int(age.total_seconds()//60)}min")
                        return
                except Exception:
                    pass  # se non parsabile, lascia passare

            # Filtro prezzo PRIMA di mettere in coda — evita sprechi di coda e API
            live_offer_pre = event["card"].get("liveSingleSaleOffer") or {}
            price_amounts_pre = (live_offer_pre.get("receiverSide") or {}).get("amounts") or {}
            price_eur_pre = to_eur(price_amounts_pre)
            if not price_eur_pre or price_eur_pre < MIN_PRICE_EUR:
                return

            player_name = event["card"].get("anyPlayer", {}).get("displayName", "?")
            player_slug = event["card"].get("anyPlayer", {}).get("slug", "")
            serial = event["card"].get("serialNumber", "?")
            sport = event["card"].get("sport", "?")
            log.info(f"[EVENTO] {player_name} | {sport} | #{serial}")

            with _queued_slugs_lock:
                if player_slug in _queued_slugs:
                    log.info(f"  → skip: {player_name} già in coda")
                    return
                _queued_slugs.add(player_slug)

            try:
                card_queue.put_nowait(event)
            except queue.Full:
                with _queued_slugs_lock:
                    _queued_slugs.discard(player_slug)
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
        while True:
            self.running = True
            self.last_message_time = time.time()

            threading.Thread(target=self.heartbeat_watchdog, daemon=True).start()

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

            log.info("🔌 Connessione WS...")
            self.ws.run_forever(ping_interval=30, ping_timeout=10)

            log.warning(f"🔁 WS terminato — retry tra {RECONNECT_DELAY_SECONDS}s")
            time.sleep(RECONNECT_DELAY_SECONDS)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


def main():
    if not SORARE_EMAIL or not SORARE_PASSWORD:
        raise RuntimeError("Imposta SORARE_EMAIL e SORARE_PASSWORD come env vars")

    jwt = authenticate(SORARE_EMAIL, SORARE_PASSWORD, aud=AUD)

    # 1. Avvia il finto server web per Render in un thread separato
    threading.Thread(target=run_web_server, daemon=True).start()
    log.info("[WEB] Finto server avviato per tenere vivo il deploy")

    # 2. Avvia i thread del bot
    threading.Thread(target=_update_fx_rates, daemon=True).start()
    log.info("[FX] Thread tassi avviato")

    for i in range(NUM_WORKERS):
        threading.Thread(target=queue_worker, args=(jwt,), daemon=True).start()
    log.info(f"[WORKER] {NUM_WORKERS} worker paralleli avviati")

    bot = SorareBot(jwt)
    try:
        bot.start()
    except KeyboardInterrupt:
        log.info("Interruzione manuale")
        bot.stop()


if __name__ == "__main__":
    main()