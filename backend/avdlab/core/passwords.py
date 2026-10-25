"""Password policy, hashing (argon2id), temporary passwords, TOTP + recovery-code helpers (v0.8.0).

Everything here is a pure function (or a thin wrapper around argon2/pyotp) so it can be unit-tested without a DB.
Auth-related settings live in `auth_settings` (env `AVDLAB_*`) next to the policy that uses them.
"""
from __future__ import annotations

import re
import secrets
import string

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AVDLAB_", env_file=".env", extra="ignore")

    password_min_length: int = 12  # AVDLAB_PASSWORD_MIN_LENGTH (never below 8)
    login_max_failures: int = 5  # AVDLAB_LOGIN_MAX_FAILURES consecutive bad passwords → lockout
    login_lockout_minutes: int = 15  # AVDLAB_LOGIN_LOCKOUT_MINUTES
    mfa_issuer: str = "avdlab"  # AVDLAB_MFA_ISSUER: label shown in the authenticator app

    @field_validator("password_min_length")
    @classmethod
    def _floor(cls, v: int) -> int:
        return max(8, v)


auth_settings = AuthSettings()

DEFAULT_ADMIN_PASSWORD = "change-me"

# ------------------------------------------------------------------ deny-list (common passwords, lower-cased)
COMMON_PASSWORDS: frozenset[str] = frozenset("""
123456 password 12345678 qwerty 123456789 12345 1234 111111 1234567 dragon 123123 baseball abc123 football monkey
letmein 696969 shadow master 666666 qwertyuiop 123321 mustang 1234567890 michael 654321 superman 1qaz2wsx 7777777
121212 000000 qazwsx 123qwe killer trustno1 jordan jennifer zxcvbnm asdfgh hunter buster soccer harley batman
andrew tigger sunshine iloveyou 2000 charlie robert thomas hockey ranger daniel starwars klaster 112233 george
computer michelle jessica pepper 1111 zxcvbn 555555 11111111 131313 freedom 777777 pass maggie 159753 aaaaaa
ginger princess joshua cheese amanda summer love ashley nicole chelsea biteme matthew access yankees 987654321
dallas austin thunder taylor matrix minecraft william corvette hello martin heather secret merlin diamond 1234qwer
gfhjkm hammer silver 222222 88888888 anthony justin test bailey q1w2e3r4t5 patrick internet scooter orange 11111
golfer cookie richard samantha bigdog guitar jackson whatever mickey chicken sparky snoopy maverick phoenix camaro
peanut morgan welcome falcon cowboy ferrari samsung andrea smokey steelers joseph mercedes dakota arsenal eagles
melissa boomer booboo spider nascar monster tigers yellow xxxxxx 123123123 gateway marina diablo bulldog qwer1234
compaq purple hardcore banana junior hannah 123654 porsche lakers iceman money cowboys 987654 london tennis 999999
ncc1701 coffee scooby 0000 miller boston q1w2e3r4 fuckoff brandon yamaha chester mother forever johnny edward 333333
oliver redsox player nikita knight fender barney midnight please brandy chicago badboy iwantu slayer rangers charles
angel flower bigdaddy rabbit wizard bigdick jasper enter rachel chris 7777 changeme change-me password1 password123
password1234 passw0rd p@ssw0rd p@ssword admin admin123 administrator root toor letmein123 welcome1 welcome123
qwerty123 qwerty1234 iloveyou1 abc12345 abcd1234 football1 monkey123 dragon123 sunshine1 princess1 azerty
azerty123 arista arista123 avdlab avdlab123 cisco cisco123 juniper network networking changemenow default
""".split())

_CLASSES = (
    ("lowercase letter", re.compile(r"[a-z]")),
    ("uppercase letter", re.compile(r"[A-Z]")),
    ("digit", re.compile(r"[0-9]")),
    ("symbol", re.compile(r"[^A-Za-z0-9]")),
)


def min_length() -> int:
    return max(8, auth_settings.password_min_length)


def password_violations(password: str, username: str = "", *, minimum: int | None = None) -> list[str]:
    """Human-readable policy violations; an empty list means the password is acceptable."""
    out: list[str] = []
    n = max(8, minimum) if minimum is not None else min_length()
    if len(password) < n:
        out.append(f"must be at least {n} characters long")
    present = [name for name, rx in _CLASSES if rx.search(password)]
    if len(present) < 3:
        missing = [name for name, _ in _CLASSES if name not in present]
        out.append(f"must contain at least 3 of: lowercase, uppercase, digit, symbol (missing: {', '.join(missing)})")
    user = username.strip().lower()
    if user and len(user) >= 3 and user in password.lower():
        out.append("must not contain the username")
    if password.lower() in COMMON_PASSWORDS or password.lower().rstrip("!.1") in COMMON_PASSWORDS:
        out.append("is too common (found in a list of frequently used passwords)")
    if re.search(r"(.)\1{3,}", password):
        out.append("must not contain more than 3 identical characters in a row")
    return out


def policy_description() -> dict:
    """What the frontend shows next to the password field."""
    return {"min_length": min_length(), "classes_required": 3, "max_repeat": 3}


# ------------------------------------------------------------------ hashing
_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
# recovery codes are 50+ bits of randomness → a cheap argon2id profile is plenty and keeps 10× verification fast
_code_hasher = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# a constant dummy hash so unknown-user logins spend the same time as wrong-password logins
_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))
    verify_password(_DUMMY_HASH, password)


# ------------------------------------------------------------------ temporary passwords
_TEMP_ALPHABET = string.ascii_letters + string.digits + "-_.!@#%+="


def generate_temp_password(length: int = 20, username: str = "") -> str:
    """Random password that always satisfies the policy (all 4 classes, no long runs)."""
    while True:
        pw = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))
        if not password_violations(pw, username, minimum=min(length, 12)) and all(rx.search(pw) for _, rx in _CLASSES):
            return pw


# ------------------------------------------------------------------ TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=auth_settings.mfa_issuer)


def normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code or "").lower()


def verify_totp(secret: str, code: str) -> bool:
    c = normalize_code(code)
    if not (len(c) == 6 and c.isdigit()):
        return False
    return pyotp.TOTP(secret).verify(c, valid_window=1)  # 30 s step, ±1 step clock drift


def qr_svg(data: str) -> str:
    import io

    import qrcode
    from qrcode.image.svg import SvgPathImage

    img = qrcode.make(data, image_factory=SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# ------------------------------------------------------------------ recovery codes
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i


def generate_recovery_codes(n: int = 10) -> list[str]:
    """`xxxxx-xxxxx` codes (10 chars from a 31-symbol alphabet ≈ 49.5 bits each)."""
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        out.append(f"{raw[:5]}-{raw[5:]}")
    return out


def hash_recovery_code(code: str) -> str:
    return _code_hasher.hash(normalize_code(code))


def match_recovery_code(hashes: list[str], code: str) -> int | None:
    """Index of the hash that matches `code` (caller removes it → single use), else None."""
    c = normalize_code(code)
    if len(c) != 10:
        return None
    for i, h in enumerate(hashes):
        try:
            if _code_hasher.verify(h, c):
                return i
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            continue
    return None
