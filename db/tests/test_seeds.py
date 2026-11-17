"""make seed-risk y make load-country-risk: idempotentes, sobre un volumen nuevo."""

import hashlib
import re
import uuid
from datetime import date

from conftest import ADMIN, ROOT

CSV = ROOT / "db/seed/country_risk.csv"


def clearing_id_from_phase_1_seed() -> str:
    """Lo mismo que hace el script, pero calculado aquí de forma independiente."""
    sql = (ROOT / "db/seed/01_core_seed.sql").read_text()
    key = re.search(r"md5\('(verabank-seed:clearing[^']*)'\)::uuid", sql).group(1)
    return str(uuid.UUID(hashlib.md5(key.encode()).hexdigest()))


def exempt_rows(db):
    with db.connect("risk_svc") as conn:
        return conn.execute("SELECT account_id::text, reason FROM risk.aml_exempt_accounts ORDER BY 1").fetchall()


def country_rows(db):
    with db.connect("risk_svc") as conn:
        return conn.execute("SELECT country, level, source, list_date, valid_from, valid_to FROM risk.country_risk ORDER BY 1, 5").fetchall()


def test_seed_risk_exempts_clearing_and_is_idempotent(fresh_db):
    fresh_db.script("seed-risk.sh")
    first = exempt_rows(fresh_db)
    out = fresh_db.script("seed-risk.sh")
    assert exempt_rows(fresh_db) == first
    assert [r[0] for r in first] == [clearing_id_from_phase_1_seed()]
    assert clearing_id_from_phase_1_seed() in out.stdout
    with fresh_db.connect(ADMIN) as conn:  # el mismo valor que calcula la semilla de core
        assert conn.execute("SELECT md5('verabank-seed:clearing')::uuid::text").fetchone() == (first[0][0],)


def test_load_country_risk_is_idempotent_and_valid_from_is_the_list_date(fresh_db):
    fresh_db.script("load-country-risk.sh")
    first = country_rows(fresh_db)
    out = fresh_db.script("load-country-risk.sh")
    assert country_rows(fresh_db) == first
    assert "3 HIGH, 18 WATCH" in out.stdout
    assert len(first) == 21
    assert all(r[4] == r[3] and r[5] is None for r in first)  # valid_from = list_date y vigente


def test_a_newer_list_closes_the_previous_one(fresh_db, tmp_path):
    new_date = date(2026, 12, 15)
    lines = CSV.read_text().splitlines()
    rows = [line.replace("2026-10-30", new_date.isoformat()) for line in lines[1:] if not line.startswith("AO,")]
    rows = [line.replace("KE,WATCH", "KE,HIGH") for line in rows]  # Kenia sube de nivel; Angola sale de la lista
    rows.append(f"PE,WATCH,https://example.invalid/new-list,{new_date.isoformat()}")  # un país nuevo
    newer = tmp_path / "country_risk_newer.csv"
    newer.write_text("\n".join([lines[0], *rows]) + "\n")

    fresh_db.script("load-country-risk.sh", str(newer))
    by_country: dict[str, list] = {}
    for row in country_rows(fresh_db):
        by_country.setdefault(row[0], []).append(row)

    (angola,) = by_country["AO"]  # cerrada, sin fila nueva
    assert angola[4] == date(2026, 10, 30) and angola[5] == new_date
    kenya_old, kenya_new = by_country["KE"]  # una vigencia empieza donde acaba la anterior
    assert (kenya_old[1], kenya_old[4], kenya_old[5]) == ("WATCH", date(2026, 10, 30), new_date)
    assert (kenya_new[1], kenya_new[4], kenya_new[5]) == ("HIGH", new_date, None)
    assert [(r[1], r[5]) for r in by_country["PE"]] == [("WATCH", None)]
    open_rows = [r for rows_ in by_country.values() for r in rows_ if r[5] is None]
    assert len(open_rows) == 21  # 21 antes, menos Angola, más Perú, y Kenia sigue una vez
    assert {r[3] for r in open_rows} == {new_date} and {r[4] for r in open_rows} == {new_date}

    snapshot = country_rows(fresh_db)
    fresh_db.script("load-country-risk.sh", str(newer))
    assert country_rows(fresh_db) == snapshot  # repetirla no cambia nada


def test_an_older_list_than_the_current_one_is_refused(fresh_db):
    before = country_rows(fresh_db)
    result = fresh_db.script("load-country-risk.sh", check=False)  # el CSV del repositorio es de 2026-10-30
    assert result.returncode != 0 and "más antigua" in result.stderr
    assert country_rows(fresh_db) == before


def test_a_csv_with_mixed_dates_or_bad_codes_is_refused(fresh_db, tmp_path):
    header = CSV.read_text().splitlines()[0]
    for name, body in {
        "mixed.csv": "AO,WATCH,https://x.invalid/a,2026-12-20\nBO,WATCH,https://x.invalid/a,2026-12-21",
        "alpha3.csv": "ANG,WATCH,https://x.invalid/a,2026-12-20",
        "level.csv": "AO,MEDIUM,https://x.invalid/a,2026-12-20",
        "empty.csv": "",
    }.items():
        path = tmp_path / name
        path.write_text(f"{header}\n{body}\n")
        before = country_rows(fresh_db)
        assert fresh_db.script("load-country-risk.sh", str(path), check=False).returncode != 0, name
        assert country_rows(fresh_db) == before, name


def test_a_list_can_be_applied_retroactively_to_the_simulated_period(provisioned_db):
    """VALID_FROM: la lista de octubre de 2026 rige desde enero (el periodo que simula amlgen); es una opción explícita."""
    def clean():
        with provisioned_db.connect(ADMIN) as conn:
            conn.execute("DELETE FROM risk.country_risk")

    clean()
    try:
        # Sin la opción rige desde su fecha; con ella se adelanta, sin duplicar filas.
        provisioned_db.script("load-country-risk.sh")
        assert {r[4] for r in country_rows(provisioned_db)} == {date(2026, 10, 30)}
        out = provisioned_db.script("load-country-risk.sh", VALID_FROM="2026-01-01")
        rows = country_rows(provisioned_db)
        assert len(rows) == 21 and "retroactivamente al periodo simulado" in out.stderr
        assert {(r[3], r[4], r[5]) for r in rows} == {(date(2026, 10, 30), date(2026, 1, 1), None)}
        snapshot = rows
        provisioned_db.script("load-country-risk.sh", VALID_FROM="2026-01-01")
        assert country_rows(provisioned_db) == snapshot  # idempotente

        # Sobre una base vacía también carga con la vigencia retroactiva.
        clean()
        provisioned_db.script("load-country-risk.sh", VALID_FROM="2026-01-01")
        assert {r[4] for r in country_rows(provisioned_db)} == {date(2026, 1, 1)}

        # Posterior a la lista o mal escrita: se rechaza y no cambia nada.
        for bad in ("2026-11-01", "enero"):
            result = provisioned_db.script("load-country-risk.sh", check=False, VALID_FROM=bad)
            assert result.returncode != 0, bad
        assert {r[4] for r in country_rows(provisioned_db)} == {date(2026, 1, 1)}
    finally:
        clean()
