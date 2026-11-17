"""El informe del modelo es Markdown válido: cada tabla tiene las mismas columnas en todas sus filas."""

import re

from verabank_ml.features import REPO_ROOT

REPORT = REPO_ROOT / "ml" / "reports" / "model-report.md"


def cells(line: str) -> int:
    """Celdas de una fila de tabla; la barra escapada (`\\|`) no separa celdas."""
    return len(re.split(r"(?<!\\)\|", line.strip())[1:-1])


def test_every_table_row_has_the_same_number_of_cells_as_its_header():
    lines = REPORT.read_text().splitlines()
    tables = 0
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|(-+\|)+", lines[i + 1]):
            width = cells(lines[i])
            assert cells(lines[i + 1]) == width, lines[i]
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                assert cells(lines[j]) == width, lines[j]
                j += 1
            tables += 1
            i = j
        else:
            i += 1
    assert tables >= 8
