
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "cosmereg.db"

SPEC = SimpleNamespace(
    slug="cosmereg",
    fmt="html",
    engine="rpa",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "holder"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
)
