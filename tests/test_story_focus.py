import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from storytelling.ollama_story import focus_matches_context


def test_sales_per_commercial_matches_fact_sale_table():
    context = json.dumps(
        {
            "tables": ["fact_sale2", "dim_employee2"],
            "measures": ["Total Sales"],
            "column_names": ["amount", "employee_id"],
        }
    )
    assert focus_matches_context(context, "sales per commercial")


def test_unrelated_focus_rejected():
    context = json.dumps({"tables": ["dim_currency"], "measures": [], "column_names": ["code"]})
    assert not focus_matches_context(context, "employee turnover")


def test_empty_focus_allowed():
    assert focus_matches_context('{"tables":["fact_sale2"]}', "")
