from laya_router.textnorm import (
    normalize,
    tokens,
    lexicon_match,
    extract_text,
    extract_temperature,
    extract_hvac_mode,
)


def test_normalize_strips_accents():
    assert normalize("Prendé la LUZ de la Oficina") == "prende la luz de la oficina"


def test_lexicon_match_prefix():
    assert lexicon_match("apagame todo", ["apag"]) is True
    assert lexicon_match("como estas", ["apag", "luz"]) is False


def test_lexicon_match_accented_entry():
    assert lexicon_match("hace frio", ["frío"]) is True


def test_extract_text_multimodal_and_empty():
    assert extract_text([{"type": "text", "text": "hola"}, {"type": "image_url", "image_url": {}}]) == "hola"
    assert extract_text("") == "" and extract_text(None) == ""


def test_extract_temperature():
    assert extract_temperature("poné el aire en 24 grados") == 24
    assert extract_temperature("poné el aire a 35") is None
    assert extract_temperature("prendé la luz 2") is None
    assert extract_temperature("poné 124") is None
    assert extract_temperature("aire a 22°") == 22


def test_extract_hvac_mode():
    assert extract_hvac_mode("poné el aire en frío") == "cool"
    assert extract_hvac_mode("calefacción en el living") == "heat"
    assert extract_hvac_mode("prendé el aire") is None


def test_extract_temperature_boundaries():
    assert extract_temperature("aire en 16") == 16
    assert extract_temperature("aire en 30") == 30
    assert extract_temperature("aire en 15") is None
    assert extract_temperature("aire en 31") is None
    assert extract_temperature("aire en 24.5") is None


def test_tokens():
    assert tokens("Prendé la luz 2 de a") == ["prende", "luz"]
