# Sorare Telegram Deal Bot

Manda notifiche Telegram quando trova carte Sorare con sconto >= 15% sul floor.

## Setup locale (test)

```bash
pip install -r requirements.txt

# 1. Trova il tuo chat_id
TELEGRAM_TOKEN=xxx python get_chat_id.py

# 2. Avvia il bot
SORARE_EMAIL=tua@email.com \
SORARE_PASSWORD=tuapassword \
TELEGRAM_TOKEN=xxx \
TELEGRAM_CHAT_ID=xxx \
SORARE_OTP=123456 \
python bot.py
```

> `SORARE_OTP` serve solo al primo avvio se hai il 2FA attivo.
> Dopo il primo login puoi rimuoverla (il JWT dura 30 giorni).

## Deploy su Railway

1. Crea repo GitHub, carica questi file
2. Vai su [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Seleziona il repo
4. Vai su **Variables** e aggiungi:

| Variabile | Valore |
|---|---|
| `SORARE_EMAIL` | tua email Sorare |
| `SORARE_PASSWORD` | tua password Sorare |
| `TELEGRAM_TOKEN` | token dal @BotFather |
| `TELEGRAM_CHAT_ID` | il tuo chat id |
| `SORARE_OTP` | codice OTP (solo primo deploy, poi rimuovi) |

5. Railway usa il `Procfile` e avvia `python bot.py` automaticamente

## Soglie configurabili (config.py)

```python
FLOOR_DISCOUNT_PCT = 15   # % minimo di sconto per notificare
RARITIES = {"limited", "rare", "super_rare", "unique"}
```

## Aggiungere MLB e NBA

In `config.py`:
```python
SPORTS = ["football", "baseball", "nba"]
```
(il supporto multi-sport è già predisposto nel codice)

## Come funziona il 2FA su Railway

Il 2FA è il punto dolente dei deploy automatici perché il codice OTP
cambia ogni 30 secondi. La soluzione:

1. Al primo deploy aggiungi `SORARE_OTP` nelle env vars Railway
2. Railway avvia il bot, si autentica, ottiene il JWT (valido 30 giorni)
3. Rimuovi `SORARE_OTP` dalle env vars (non serve più per 30 giorni)
4. Tra 30 giorni ripeti con un nuovo OTP

In futuro possiamo salvare il JWT su un file/database per evitare
di ri-autenticarsi ad ogni restart.
