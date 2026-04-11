import os

# ── Sorare credentials ────────────────────────────────────────────────────────
SORARE_EMAIL = os.environ.get("SORARE_EMAIL", "")
SORARE_PASSWORD = os.environ.get("SORARE_PASSWORD", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SORARE_OTP = os.environ.get("SORARE_OTP", "")

# ── Sport da monitorare ───────────────────────────────────────────────────────
# Valori validi: "football", "nba", "baseball"
# Mapping interno -> enum GraphQL: football=FOOTBALL, nba=NBA, baseball=BASEBALL
SPORTS = ["football","nba", "baseball"]

# ── Filtri di notifica ────────────────────────────────────────────────────────
FLOOR_DISCOUNT_PCT = 15
MIN_FLOOR_EUR = 0.1
MIN_PRICE_EUR=5
RARITIES = {
    "limited",
    #"rare",
    #"super_rare",
    #"unique",
}

# ── WebSocket / API ───────────────────────────────────────────────────────────
WS_URL = "wss://ws.sorare.com/cable"
API_URL = "https://api.sorare.com/graphql"
AUTH_API_URL = "https://api.sorare.com/graphql"
SALT_URL = "https://api.sorare.com/api/v1/users/{email}"

# ── Comportamento ─────────────────────────────────────────────────────────────
FLOOR_CACHE_TTL_SECONDS = 600
RECONNECT_DELAY_SECONDS = 5