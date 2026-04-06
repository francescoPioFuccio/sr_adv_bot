import requests
import bcrypt
import logging

from config import AUTH_API_URL, SALT_URL

log = logging.getLogger(__name__)


def get_salt(email: str) -> str:
    resp = requests.get(SALT_URL.format(email=email), timeout=10)
    resp.raise_for_status()
    return resp.json()["salt"]


def hash_password(password: str, salt: str) -> str:
    return bcrypt.hashpw(password.encode(), salt.encode()).decode()


def authenticate(email: str, password: str, aud: str = "sorare-tg-bot") -> str:
    """Login Sorare con supporto 2FA. Restituisce JWT token."""
    log.info(f"Autenticazione per {email}...")
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
        # Su server non possiamo chiedere input interattivo — 
        # il token OTP va passato come env var SORARE_OTP al primo avvio,
        # oppure usiamo il flow senza 2FA se l'account è già whitelistato per IP.
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

        token = sign_in2["jwtToken"]["token"]
        slug  = sign_in2["currentUser"]["slug"]
    else:
        token = sign_in["jwtToken"]["token"]
        slug  = sign_in["currentUser"]["slug"]

    log.info(f"Autenticato come: {slug}")
    return token


def _get_otp_code() -> str:
    """
    Recupera il codice OTP:
    - Se c'è la env var SORARE_OTP la usa (utile per il primo deploy)
    - Altrimenti chiede input da console (utile in locale)
    """
    import os
    otp = os.environ.get("SORARE_OTP", "").strip()
    if otp:
        log.info("OTP letto da env var SORARE_OTP")
        return otp
    # fallback interattivo (solo in locale)
    return input("Codice OTP (6 cifre): ").strip()
