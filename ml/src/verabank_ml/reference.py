"""Implementación de referencia, lenta y por filas, de las features (solo para pruebas).

Está escrita aparte de `features.py` y sin compartir lógica con ella: para cada fila recorre todas las
demás y aplica la definición tal cual (`step` estrictamente menor; ventanas [s-24, s-1] y [s-1, s-1]).
"""

import math


def reference_features(
    rows: list[dict], feature_set: str, types: list[str], categories: list[str]
) -> list[dict]:
    """Una entrada por fila de un tipo permitido, con `row_id` (posición en `rows`) y sus features."""
    kept = [dict(row, row_id=i) for i, row in enumerate(rows) if row["type"] in types]
    out = []
    for row in kept:
        s, orig, dest, amount = row["step"], row["nameOrig"], row["nameDest"], row["amount"]
        before = [r for r in kept if r["step"] < s]
        day = [r for r in before if r["step"] >= s - 24]
        hour = [r for r in before if r["step"] >= s - 1]
        to_dest = [r for r in day if r["nameDest"] == dest]
        from_src_day = [r for r in day if r["nameOrig"] == orig]
        dest_history = [r["step"] for r in before if r["nameDest"] == dest]
        hod = (s - 1) % 24
        feats = {"row_id": row["row_id"], "amount": amount, "log_amount": math.log(1 + amount)}
        for category in categories:
            feats[f"type_{category}"] = 1.0 if row["type"] == category else 0.0
        feats["hour_sin"] = math.sin(2 * math.pi * hod / 24)
        feats["hour_cos"] = math.cos(2 * math.pi * hod / 24)
        feats["dst_in_count_24h"] = len(to_dest)
        feats["dst_in_sum_24h"] = sum(r["amount"] for r in to_dest)
        feats["dst_distinct_src_24h"] = len({r["nameOrig"] for r in to_dest})
        feats["dst_seen_before"] = 1.0 if dest_history else 0.0
        feats["dst_age_hours"] = s - min(dest_history) if dest_history else 0
        feats["src_out_count_1h"] = len([r for r in hour if r["nameOrig"] == orig])
        feats["src_out_count_24h"] = len(from_src_day)
        feats["src_out_sum_24h"] = sum(r["amount"] for r in from_src_day)
        feats["is_new_beneficiary"] = (
            0.0 if any(r["nameOrig"] == orig and r["nameDest"] == dest for r in before) else 1.0
        )
        if feature_set == "B":
            balance = row["oldbalanceOrg"]
            feats["src_balance_before"] = balance
            feats["amount_to_balance"] = amount / balance if balance > 0 else 0.0
        out.append(feats)
    return out
