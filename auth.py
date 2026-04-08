import requests
import bcrypt
import logging
import os
from datetime import datetime, timezone, timedelta

from config import AUTH_API_URL, SALT_URL

log = logging.getLogger(__name__)


def _is_token_valid(token: str) -> bool:
    """Controlla se il JWT è ancora valido decodificando il payload (senza librerie esterne)."""
    try:
        import base64, json
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return True
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        remaining = exp_dt - datetime.now(timezone.utc)
        if remaining > timedelta(hours=1):
            log.info(f"JWT valido — scade tra {remaining.days} giorni")
            return True
        else:
            log.info("JWT scaduto o in scadenza, sarà rinnovato")
            return False
    except Exception as e:
        log.warning(f"Impossibile verificare JWT: {e}")
        return False


def get_salt(email: str) -> str:
    resp = requests.get(SALT_URL.format(email=email), timeout=10)
    resp.raise_for_status()
    return resp.json()["salt"]


def hash_password(password: str, salt: str) -> str:
    return bcrypt.hashpw(password.encode(), salt.encode()).decode()


def authenticate(email: str, password: str, aud: str = "sorare-tg-bot") -> str:
    """
    Login Sorare con supporto 2FA e cache JWT.

    Priorità:
    1. Env var SORARE_JWT (impostata manualmente dopo il primo login)
    2. Login vero con email/password + OTP se richiesto
    """

    # 1. Prova JWT cached da env var
    cached_jwt = os.environ.get("SORARE_JWT", "").strip()
    if cached_jwt and _is_token_valid(cached_jwt):
        log.info("✅ Usando SORARE_JWT da env var — nessun login necessario")
        return cached_jwt

    # 2. Login vero
    log.info(f"🔐 Autenticazione per {email}...")
    salt = get_salt(email)
    hashed_pw = hash_password(password, salt)

    mutation = """
    mutation SignInMutation($input: signInInput!) {
      signIn(input: $input) {
        currentUser { slug }
        jwtToken(aud: "%(aud)s") { token expiredAt }
        otpSessionChallenge
        errors { message }
      }
    }
    """ % {"aud": aud}

    resp = requests.post(
        AUTH_API_URL,
        json={"query": mutation, "variables": {"input": {"email": email, "password": hashed_pw}}},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"Errore GraphQL: {data['errors']}")

    sign_in = data["data"]["signIn"]
    otp_challenge = sign_in.get("otpSessionChallenge")

    errors = sign_in.get("errors", [])
    non_2fa = [e for e in errors if e.get("message") != "2fa_missing"]
    if non_2fa and not otp_challenge:
        raise RuntimeError(f"Errore login: {non_2fa}")

    if otp_challenge:
        otp_code = _get_otp_code()

        mutation_otp = """
        mutation SignInMutation($input: signInInput!) {
          signIn(input: $input) {
            currentUser { slug }
            jwtToken(aud: "%(aud)s") { token expiredAt }
            errors { message }
          }
        }
        """ % {"aud": aud}

        resp2 = requests.post(
            AUTH_API_URL,
            json={"query": mutation_otp, "variables": {"input": {
                "otpSessionChallenge": otp_challenge,
                "otpAttempt": otp_code,
            }}},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        data2 = resp2.json()
        sign_in2 = data2["data"]["signIn"]

        if sign_in2.get("errors"):
            raise RuntimeError(f"OTP errato: {sign_in2['errors']}")

        token      = sign_in2["jwtToken"]["token"]
        expired_at = sign_in2["jwtToken"]["expiredAt"]
        slug       = sign_in2["currentUser"]["slug"]
    else:
        token      = sign_in["jwtToken"]["token"]
        expired_at = sign_in["jwtToken"]["expiredAt"]
        slug       = sign_in["currentUser"]["slug"]

    log.info(f"✅ Autenticato come: {slug} — JWT valido fino a {expired_at}")

    # Stampa il JWT nei log così puoi copiarlo e impostarlo come env var SORARE_JWT
    print("\n" + "="*60)
    print("🔑 COPIA QUESTO JWT E IMPOSTALO COME ENV VAR 'SORARE_JWT':")
    print(f"\nSORALE_JWT={token}\n")
    print(f"Scadenza: {expired_at}")
    print("="*60 + "\n")

    return token


def _get_otp_code() -> str:
    otp = os.environ.get("SORARE_OTP", "").strip()
    if otp:
        log.info("OTP letto da env var SORARE_OTP")
        return otp
    return input("Codice OTP (6 cifre): ").strip()