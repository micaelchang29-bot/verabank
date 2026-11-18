"""Consulta cada tarjeta de la colección VeraBank por ``POST /api/card/:id/query`` y muestra filas y las 3 primeras.

Uso: scripts/bi-up.sh no lo llama; ``make bi-check`` (lee .env). No imprime credenciales.
"""

from __future__ import annotations

import os
import sys

import setup_metabase as sm


def main() -> int:
    url = os.environ.get("METABASE_URL", f"http://localhost:{os.environ.get('METABASE_PORT', '3000')}")
    mb = sm.Metabase(url)
    try:
        mb.login(os.environ["METABASE_ADMIN_EMAIL"], os.environ["METABASE_ADMIN_PASSWORD"])
        collection = next(c["id"] for c in mb._items(mb.call("GET", "/api/collection"))
                          if c.get("name") == sm.COLLECTION_NAME and not c.get("archived"))
        cards = sorted((c for c in mb._items(mb.call("GET", "/api/card", params={"f": "all"}))
                        if c.get("collection_id") == collection and not c.get("archived")), key=lambda c: c["id"])
        failed = 0
        for card in cards:
            result = mb.call("POST", f"/api/card/{card['id']}/query")
            if result.get("status") != "completed":
                failed += 1
                print(f"[{card['id']}] {card['name']}: ERROR {result.get('error')}")
                continue
            data = result["data"]
            cols = [c["name"] for c in data["cols"]]
            print(f"[{card['id']}] {card['name']}: {result['row_count']} filas; columnas {cols}")
            for row in data["rows"][:3]:
                print(f"    {row}")
        return 1 if failed else 0
    finally:
        mb.close()


if __name__ == "__main__":
    sys.exit(main())
