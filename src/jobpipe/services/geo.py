"""Country names and ISO3 codes -> ISO 3166-1 alpha-2, for the countries postings commonly name.
Unknown stays None: no guessing from city names."""
from __future__ import annotations

_C = {
    "KZ": ("kazakhstan", "kaz"), "AM": ("armenia", "arm"), "KG": ("kyrgyzstan", "kgz"), "RU": ("russia", "rus"),
    "BY": ("belarus", "blr"), "GE": ("georgia", "geo"), "RS": ("serbia", "srb"), "UZ": ("uzbekistan", "uzb"),
    "AE": ("united arab emirates", "are"), "CY": ("cyprus", "cyp"), "SG": ("singapore", "sgp"),
    "JP": ("japan", "jpn"), "HK": ("hong kong", "hkg"), "CN": ("china", "chn"), "IN": ("india", "ind"),
    "GB": ("united kingdom", "gbr"), "IE": ("ireland", "irl"), "NL": ("netherlands", "nld"),
    "DE": ("germany", "deu"), "CH": ("switzerland", "che"), "FR": ("france", "fra"), "ES": ("spain", "esp"),
    "PT": ("portugal", "prt"), "IT": ("italy", "ita"), "AT": ("austria", "aut"), "BE": ("belgium", "bel"),
    "LU": ("luxembourg", "lux"), "DK": ("denmark", "dnk"), "SE": ("sweden", "swe"), "NO": ("norway", "nor"),
    "FI": ("finland", "fin"), "PL": ("poland", "pol"), "CZ": ("czechia", "cze"), "RO": ("romania", "rou"),
    "BG": ("bulgaria", "bgr"), "GR": ("greece", "grc"), "HU": ("hungary", "hun"), "EE": ("estonia", "est"),
    "LT": ("lithuania", "ltu"), "LV": ("latvia", "lva"), "MT": ("malta", "mlt"), "IL": ("israel", "isr"),
    "TR": ("turkey", "tur"), "US": ("united states", "usa"), "CA": ("canada", "can"), "AU": ("australia", "aus"),
    "NZ": ("new zealand", "nzl"), "BR": ("brazil", "bra"), "MX": ("mexico", "mex"), "AR": ("argentina", "arg"),
}
_ALIASES = {"uk": "GB", "england": "GB", "usa": "US", "u.s.": "US", "united states of america": "US",
            "uae": "AE", "czech republic": "CZ", "the netherlands": "NL", "holland": "NL", "türkiye": "TR",
            "казахстан": "KZ", "армения": "AM", "кыргызстан": "KG", "киргизия": "KG", "россия": "RU",
            "беларусь": "BY", "грузия": "GE", "сербия": "RS", "оаэ": "AE", "кипр": "CY", "германия": "DE",
            "нидерланды": "NL", "великобритания": "GB"}
_BY_NAME = {name: iso2 for iso2, (name, _) in _C.items()} | _ALIASES
_BY_ISO3 = {iso3: iso2 for iso2, (_, iso3) in _C.items()}


def to_iso2(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if len(v) == 2 and v.upper() in _C:
        return v.upper()
    if len(v) == 3 and v in _BY_ISO3:
        return _BY_ISO3[v]
    return _BY_NAME.get(v)


def country_from_location(location: str | None) -> str | None:
    """The last comma-separated part, if it names a country: 'Amsterdam, Netherlands' -> 'NL'."""
    if not location:
        return None
    return to_iso2(location.split(",")[-1])
