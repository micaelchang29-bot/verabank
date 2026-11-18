"""¿Hay una alerta de esa regla que incluya esa transferencia? Recorre GET /api/v1/aml/alerts con su cursor.

Uso: python3 scripts/find_alert.py <url-risk-stream> <archivo-de-cabecera-api-key> <regla> <transfer_id>
Sale con 0 si la encuentra y con 1 si no. La clave de API se lee de un archivo de cabeceras, no de la línea de comandos.
"""

import json
import sys
import urllib.parse
import urllib.request


def main() -> int:
    url, auth_file, rule, transfer_id = sys.argv[1:5]
    name, _, value = open(auth_file, encoding="utf-8").read().strip().partition(": ")
    cursor = None
    for _ in range(1000):
        query = {"rule_code": rule, "limit": "200", **({"cursor": cursor} if cursor else {})}
        req = urllib.request.Request(f"{url}/api/v1/aml/alerts?{urllib.parse.urlencode(query)}", headers={name: value})
        with urllib.request.urlopen(req, timeout=10) as r:
            page = json.load(r)
        if any(transfer_id in a.get("transfer_ids", []) for a in page["items"]):
            return 0
        cursor = page.get("next_cursor")
        if not cursor:
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
