"""db/seed/country_risk.csv: sin duplicados, solo códigos ISO 3166-1 alfa-2 y la lista del GAFI sin ambigüedades."""

import csv
from datetime import date

import pycountry
from conftest import ROOT

CSV = ROOT / "db/seed/country_risk.csv"
# Nombre del GAFI -> alfa-2. «Virgin Islands (UK)» es VG (Islas Vírgenes Británicas), no VI (las de EE. UU.).
HIGH = {"Iran": "IR", "Democratic People's Republic of Korea (DPRK)": "KP", "Myanmar": "MM"}
WATCH = {
    "Angola": "AO", "Bolivia": "BO", "Bosnia and Herzegovina": "BA", "Cameroon": "CM", "Haiti": "HT",
    "Iraq": "IQ", "Kenya": "KE", "Kuwait": "KW", "Lao People's Democratic Republic": "LA", "Lebanon": "LB",
    "Nepal": "NP", "Papua New Guinea": "PG", "South Sudan": "SS", "Syria": "SY", "Venezuela": "VE",
    "Vietnam": "VN", "Virgin Islands (UK)": "VG", "Yemen": "YE",
}  # fmt: skip


def rows():
    with CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_header_and_columns():
    assert CSV.read_text().splitlines()[0] == "country,level,source,list_date"
    assert all(set(r) == {"country", "level", "source", "list_date"} and None not in r.values() for r in rows())


def test_no_duplicates_and_every_code_is_a_real_alpha_2():
    countries = [r["country"] for r in rows()]
    assert len(countries) == len(set(countries))
    for code in countries:
        assert len(code) == 2 and code == code.upper()
        assert pycountry.countries.get(alpha_2=code) is not None, code


def test_the_two_fatf_lists_map_to_the_expected_codes():
    by_level = {"HIGH": set(), "WATCH": set()}
    for r in rows():
        by_level[r["level"]].add(r["country"])
    assert by_level["HIGH"] == set(HIGH.values()) and len(HIGH) == 3
    assert by_level["WATCH"] == set(WATCH.values()) and len(WATCH) == 18
    assert "VG" in by_level["WATCH"] and "VI" not in by_level["WATCH"]
    assert len(set(HIGH.values()) | set(WATCH.values())) == 21  # ningún país en las dos listas


def test_source_and_date_per_list():
    for r in rows():
        assert r["source"].startswith("https://www.fatf-gafi.org/en/publications/High-risk-and-other-monitored-jurisdictions/")
        assert r["source"].endswith(
            "call-for-action-october-2026.html" if r["level"] == "HIGH" else "increased-monitoring-october-2026.html")
        assert date.fromisoformat(r["list_date"]) == date(2026, 10, 30)
