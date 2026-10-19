from pathlib import Path

from .models import Run

STATIC = Path(__file__).parent / "static"
MARKER = '<script id="parquetscope-artifact" type="application/json">null</script>'


def viewer_html(run: Run | None = None) -> str:
    template = (STATIC / "offline.html").read_text()
    if run:
        # Prevent script termination in an artifact's SQL/column names.
        payload = run.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
        return template.replace(MARKER, MARKER.replace(">null<", f">{payload}<"), 1)
    return template
