import os

# ── Sorare credentials (metti su Railway come env vars) ─────────────────────
SORARE_EMAIL    = os.environ.get("SORARE_EMAIL", "francyfuccio20@gmail.com")
SORARE_PASSWORD = os.environ.get("SORARE_PASSWORD", "SorAre2002!")

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8600186752:AAHBZW1FxUtRQzJcfyad6g2ICd_kHRRkbxM")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1244252369" )
SORARE_OTP = os.environ.get("SORARE_OTP", "")


# ── Filtri di notifica ───────────────────────────────────────────────────────
FLOOR_DISCOUNT_PCT = 15     # % minimo di sconto per notificare
MIN_FLOOR_EUR      = 0.1       # floor minimo in euro (ignora carte troppo economiche)
RARITIES = {                 # rarità da monitorare
    "limited",
    "rare",
    "super_rare",
    "unique",
}
SPORTS = ["football"]        # aggiungi "baseball", "nba" quando vuoi

# ── WebSocket / API ──────────────────────────────────────────────────────────
WS_URL       = "wss://ws.sorare.com/cable"
API_URL = "https://api.sorare.com/graphql"
AUTH_API_URL = "https://api.sorare.com/graphql"
SALT_URL     = "https://api.sorare.com/api/v1/users/{email}"

# ── Comportamento ────────────────────────────────────────────────────────────
FLOOR_CACHE_TTL_SECONDS = 600   # cache floor per 10 minuti
RECONNECT_DELAY_SECONDS = 5