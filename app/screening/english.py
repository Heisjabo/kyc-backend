"""Watchlist text in English.

The watchlists behind yente come from many countries, so a listed person's
positions, notes or sanction reasons can arrive in Korean, Russian, French or
any other language, often next to an English version of the same text. Staff
read these in English: names stay as listed, everything else is shown in
English.

english() picks the English values when there are any. When there are none, it
translates the others with Google Cloud Translation, using the service account
that already holds the Drive key (the Cloud Translation API has to be enabled
on its project). If that is not possible the original is kept, marked as not
translated, so nothing is hidden.
"""

import logging
import re
import threading
import time
import unicodedata

import httpx

logger = logging.getLogger(__name__)

_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
_SCOPES = ["https://www.googleapis.com/auth/cloud-translation"]

# Words that make a sentence English rather than another Latin-script language.
_ENGLISH_WORDS = {
    "the", "of", "and", "for", "in", "to", "is", "was", "with", "by", "as", "on", "has", "who",
    "which", "from", "his", "her", "their", "been", "an", "at", "member", "leader", "minister",
    "president", "director", "chairman", "head", "former", "general", "deputy", "commander",
    "official", "company", "state", "national", "senior", "chief", "secretary",
}
# Words common in other Latin-script languages and rare in English.
_OTHER_WORDS = {
    "de", "del", "la", "le", "les", "des", "du", "el", "los", "las", "et", "y", "und", "der", "die",
    "das", "il", "di", "da", "dos", "para", "pour", "est", "une", "con", "por", "au",
}
_WORD = re.compile(r"[A-Za-z]+")

_lock = threading.Lock()
_cache: dict[str, str | None] = {}
_credentials = None
_unavailable_until = 0.0


def _latin_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters)


def looks_english(text: str) -> bool:
    if _latin_share(text) < 0.9:
        return False
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return True
    english_words = sum(w in _ENGLISH_WORDS for w in words)
    other_words = sum(w in _OTHER_WORDS for w in words)
    if other_words > english_words:
        return False
    if english_words:
        return True
    # A short label with no telling word ("Supreme Leader", "UN 1718") reads the
    # same in English, provided it is plain ASCII.
    return len(words) <= 4 and text.isascii()


def _token() -> str | None:
    """An access token for the Translation API, from the Drive service account."""
    global _credentials
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import service_account

    from app.storage.drive import _service_account_info

    info = _service_account_info()
    if info is None:
        return None
    if _credentials is None:
        _credentials = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    if not _credentials.valid:
        _credentials.refresh(GoogleRequest())
    return _credentials.token


def _translate(texts: list[str]) -> dict[str, str | None]:
    """Each text in English, or None for the ones that could not be translated."""
    global _unavailable_until
    with _lock:
        todo = [t for t in dict.fromkeys(texts) if t not in _cache]
        if not todo or time.monotonic() < _unavailable_until:
            return {t: _cache.get(t) for t in texts}
    try:
        token = _token()
        if token is None:
            raise RuntimeError("no Google service account is configured")
        response = httpx.post(
            _TRANSLATE_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"q": todo, "target": "en", "format": "text"},
            timeout=10,
        )
        response.raise_for_status()
        translated = [item["translatedText"] for item in response.json()["data"]["translations"]]
        with _lock:
            _cache.update(zip(todo, translated))
    except Exception as exc:  # noqa: BLE001, the original is shown instead
        logger.warning("Watchlist text could not be translated: %s", exc)
        with _lock:
            # Do not try again on every request while the API is unreachable.
            _unavailable_until = time.monotonic() + 300
    with _lock:
        return {t: _cache.get(t) for t in texts}


def english(values: list[str]) -> list[dict]:
    """The values to show, each as {"text", "original", "translated", "english"}.

    English values when there are some; otherwise every value translated, or
    kept as it was, with english False, when translation is not possible.
    """
    values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    values = list(dict.fromkeys(values))
    if not values:
        return []
    chosen = [v for v in values if looks_english(v)]
    if chosen:
        return [{"text": v, "original": None, "translated": False, "english": True} for v in chosen]
    found = _translate(values)
    out, seen = [], set()
    for v in values:
        text = found.get(v)
        shown = text if text else v
        if shown in seen:
            continue
        seen.add(shown)
        out.append({"text": shown, "original": v if text else None, "translated": bool(text), "english": bool(text)})
    return out


def english_only(values: list[str]) -> list[str]:
    """Only the values already in English, without translating. For lists."""
    return [v for v in dict.fromkeys(values) if isinstance(v, str) and v.strip() and looks_english(v)]
