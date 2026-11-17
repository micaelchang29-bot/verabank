"""risk.aml_rules: semillas v1 sin FAN_IN, una sola versión activa por regla y parámetros inmutables."""

import psycopg
import pytest


def rules(db):
    with db.connect("risk_svc") as conn:
        return {
            (code, version): (params, active)
            for code, version, params, active in conn.execute(
                "SELECT rule_code, version, params, active FROM risk.aml_rules")
        }


def test_seed_is_exactly_the_three_rules_of_the_design_without_fan_in(provisioned_db):
    assert rules(provisioned_db) == {
        ("THRESHOLD", 1): ({"threshold": "10000.00"}, True),
        ("STRUCTURING", 1): (
            {"threshold": "10000.00", "min_ratio": 0.70, "max_ratio": 1.00, "min_count": 3,
             "window_hours": 24, "min_total": "10000.00"}, True),
        ("COUNTRY_RISK", 1): ({"high_min_amount": "1000.00", "watch_min_amount": "5000.00"}, True),
    }  # fmt: skip


def test_amounts_keep_their_decimal_text_in_the_seed(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        text = conn.execute("SELECT params::text FROM risk.aml_rules WHERE rule_code = 'STRUCTURING'").fetchone()[0]
    assert '"min_ratio": 0.70' in text and '"max_ratio": 1.00' in text and '"min_total": "10000.00"' in text


def test_only_one_active_version_per_rule(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO risk.aml_rules (rule_code, version, params, active) "
                         "VALUES ('THRESHOLD', 2, '{\"threshold\": \"9000.00\"}', true)")
        try:
            # inactiva sí; y el relevo correcto es desactivar la anterior y activar la nueva
            conn.execute("INSERT INTO risk.aml_rules (rule_code, version, params, active) "
                         "VALUES ('TEST_RULE', 1, '{}', true), ('TEST_RULE', 2, '{}', false), ('TEST_RULE', 3, '{}', false)")
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute("UPDATE risk.aml_rules SET active = true WHERE rule_code = 'TEST_RULE' AND version = 2")
            with conn.transaction():
                conn.execute("UPDATE risk.aml_rules SET active = false WHERE rule_code = 'TEST_RULE' AND version = 1")
                conn.execute("UPDATE risk.aml_rules SET active = true WHERE rule_code = 'TEST_RULE' AND version = 2")
            assert conn.execute("SELECT count(*) FROM risk.aml_rules WHERE rule_code = 'TEST_RULE' AND active").fetchone() == (1,)
        finally:
            conn.execute("DELETE FROM risk.aml_rules WHERE rule_code = 'TEST_RULE'")


def test_a_rule_version_cannot_be_edited_in_place(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        for change in ("params = '{\"threshold\": \"1.00\"}'", "version = 9", "valid_from = now()"):
            with pytest.raises(psycopg.errors.RaiseException, match="inserta una versión nueva"):
                conn.execute(f"UPDATE risk.aml_rules SET {change} WHERE rule_code = 'THRESHOLD'")
    assert rules(provisioned_db)[("THRESHOLD", 1)] == ({"threshold": "10000.00"}, True)


def test_params_must_be_a_json_object(provisioned_db):
    with provisioned_db.connect("risk_svc") as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO risk.aml_rules (rule_code, version, params, active) VALUES ('X', 1, '[]', false)")
