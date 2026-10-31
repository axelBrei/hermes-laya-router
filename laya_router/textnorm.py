"""Normalización de texto y extracción de parámetros (temperatura, modo HVAC)."""
import re
import unicodedata

# El lookahead (?![\w.,]*\d) descarta decimales y números largos como "24.5" o "124".
_TEMP_RE = re.compile(r"(?<![\w.,])(\d{2})(?![\w.,]*\d)(?:\s*(?:°|º|grados))?(?!\w)")

_HVAC_LEXICON = (
    ("cool", ("frio", "fresco", "refriger")),
    ("heat", ("calor", "calef", "calent")),
    ("fan_only", ("ventil",)),
    ("auto", ("autom",)),
)


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", normalize(text)) if len(t) >= 3]


def lexicon_match(text: str, lexicon: list[str]) -> bool:
    entries = [normalize(e) for e in lexicon]
    entries = [e for e in entries if e]
    return any(tok.startswith(e) for tok in tokens(text) for e in entries)


def extract_text(user_message) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = [
            p["text"]
            for p in user_message
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def extract_temperature(text: str) -> int | None:
    for m in _TEMP_RE.finditer(normalize(text)):
        value = int(m.group(1))
        if 16 <= value <= 30:
            return value
    return None


def extract_hvac_mode(text: str) -> str | None:
    toks = tokens(text)
    for mode, prefixes in _HVAC_LEXICON:
        if any(t.startswith(p) for t in toks for p in prefixes):
            return mode
    return None
