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


def sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


def test_report_names_the_served_and_the_tested_version_without_contradiction():
    """Si el modelo servido no es el evaluado en test, el informe nombra ambos en las dos menciones."""
    import json

    served = json.loads((REPO_ROOT / "ml/artifacts/thresholds.json").read_text())["model_version"]
    tested = json.loads((REPO_ROOT / "ml/reports/test_evaluation.json").read_text())["model_version"]
    text = sections(REPORT.read_text())
    served_section = text["Modelo servido"]
    test_section = next(v for k, v in text.items() if k.startswith("Métricas en test"))
    assert served in served_section
    if served != tested:
        assert tested in served_section and "model_sha256" in served_section
        assert tested in test_section and served in test_section and "model_sha256" in test_section
        adr = (REPO_ROOT / "docs/adr/ADR-11.md").read_text()
        fam = served.split("/")[0]
        for version in (served.split("/")[1], tested.split("/")[1]):
            assert f"v{version}" in adr, version
        assert "reentrenamiento determinista" in adr and "model_sha256" in adr
        assert "corresponde a v" + tested.split("/")[1] in adr and fam in adr
