"""
pbix_patcher.py — Patch DAX measures into Power BI Desktop (.pbix) files.

Workflow:
    1. Extract the .pbix (ZIP archive)
    2. Parse the DataModelSchema (JSON tabular model definition)
    3. Add / update / remove DAX measures
    4. Repackage into a new .pbix file

Usage:
    from pbix_patcher import PBIXPatcher

    patcher = PBIXPatcher("Sales_Report.pbix")
    patcher.extract()

    # Inspect existing model
    tables = patcher.list_tables()
    measures = patcher.list_measures()

    # Add a new measure
    patcher.add_measure(
        table_name="Sales",
        measure_name="Total Revenue",
        dax_expression="SUM(Sales[Amount])",
        format_string="$#,##0.00",
        description="Sum of all sales amounts",
    )

    # Update an existing measure
    patcher.update_measure(
        table_name="Sales",
        measure_name="Total Revenue",
        new_dax_expression="SUMX(Sales, Sales[Qty] * Sales[Price])",
    )

    # Remove a measure
    patcher.remove_measure(table_name="Sales", measure_name="Old Metric")

    # Repackage
    output_path = patcher.repackage("Sales_Report_patched.pbix")
"""

import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional


class PBIXPatcherError(Exception):
    """Base exception for PBIX patching errors."""
    pass


class PBIXPatcher:
    """Extract, patch DAX measures, and repackage .pbix files."""

    # The file inside the .pbix ZIP that holds the tabular model definition
    MODEL_SCHEMA_FILENAME = "DataModelSchema"

    def __init__(self, pbix_path: str, work_dir: Optional[str] = None):
        """
        Args:
            pbix_path: Path to the input .pbix file.
            work_dir:  Temporary directory for extraction. Auto-created if None.
        """
        self.pbix_path = Path(pbix_path).resolve()
        if not self.pbix_path.exists():
            raise FileNotFoundError(f"PBIX file not found: {self.pbix_path}")
        if not self.pbix_path.suffix.lower() == ".pbix":
            raise PBIXPatcherError("File must have a .pbix extension")

        self.work_dir = Path(work_dir) if work_dir else self.pbix_path.parent / f".{self.pbix_path.stem}_extracted"
        self.model_schema: Optional[dict] = None
        self._extracted = False

    # ──────────────────────────────────────────────
    # Extraction
    # ──────────────────────────────────────────────

    def extract(self) -> dict:
        """
        Extract the .pbix archive and parse the DataModelSchema.
        Returns the parsed model schema dict.
        """
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.work_dir.mkdir(parents=True)

        # .pbix is a ZIP file
        try:
            with zipfile.ZipFile(self.pbix_path, "r") as zf:
                zf.extractall(self.work_dir)
        except zipfile.BadZipFile:
            raise PBIXPatcherError(
                f"Cannot open '{self.pbix_path.name}' as a ZIP archive. "
                "Ensure this is a valid .pbix file (not .pbit or corrupted)."
            )

        # Locate and parse the model schema
        schema_path = self.work_dir / self.MODEL_SCHEMA_FILENAME
        if not schema_path.exists():
            # Some .pbix versions may nest it differently
            candidates = list(self.work_dir.rglob("DataModelSchema"))
            if not candidates:
                raise PBIXPatcherError(
                    "DataModelSchema not found in .pbix archive. "
                    "This file may be a .pbit template or use an unsupported format."
                )
            schema_path = candidates[0]

        raw = schema_path.read_bytes()
        # Handle BOM (Power BI often writes UTF-16 LE with BOM)
        text = self._decode_schema(raw)
        self.model_schema = json.loads(text)
        self._extracted = True
        return self.model_schema

    @staticmethod
    def _decode_schema(raw: bytes) -> str:
        """Decode DataModelSchema bytes, handling various encodings Power BI uses."""
        for encoding in ("utf-16-le", "utf-16-be", "utf-8-sig", "utf-8"):
            try:
                text = raw.decode(encoding)
                # Quick sanity check — valid JSON should start with { after stripping
                stripped = text.strip().lstrip("\ufeff")
                if stripped.startswith("{"):
                    return stripped
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise PBIXPatcherError("Cannot decode DataModelSchema — unknown encoding")

    def _ensure_extracted(self):
        if not self._extracted or self.model_schema is None:
            raise PBIXPatcherError("Call .extract() before modifying the model.")

    # ──────────────────────────────────────────────
    # Inspection helpers
    # ──────────────────────────────────────────────

    def list_tables(self) -> list[dict]:
        """Return a summary of every table in the model."""
        self._ensure_extracted()
        tables = self.model_schema.get("model", {}).get("tables", [])
        return [
            {
                "name": t["name"],
                "columns": [c["name"] for c in t.get("columns", [])],
                "measures": [m["name"] for m in t.get("measures", [])],
            }
            for t in tables
        ]

    def list_measures(self, table_name: Optional[str] = None) -> list[dict]:
        """
        List all measures, optionally filtered by table.
        Returns list of {table, name, expression, description, formatString}.
        """
        self._ensure_extracted()
        results = []
        for table in self.model_schema["model"]["tables"]:
            if table_name and table["name"] != table_name:
                continue
            for m in table.get("measures", []):
                results.append({
                    "table": table["name"],
                    "name": m["name"],
                    "expression": m.get("expression", ""),
                    "description": m.get("description", ""),
                    "formatString": m.get("formatString", ""),
                })
        return results

    def get_model_context(self) -> dict:
        """
        Return a compact representation of the data model suitable for
        sending to an LLM as context for DAX generation.
        """
        self._ensure_extracted()
        tables = []
        for t in self.model_schema["model"]["tables"]:
            tables.append({
                "name": t["name"],
                "columns": [
                    {"name": c["name"], "dataType": c.get("dataType", "unknown")}
                    for c in t.get("columns", [])
                ],
                "measures": [
                    {"name": m["name"], "expression": m.get("expression", "")}
                    for m in t.get("measures", [])
                ],
                "relationships_hint": "see model.relationships",
            })
        relationships = []
        for r in self.model_schema.get("model", {}).get("relationships", []):
            relationships.append({
                "fromTable": r.get("fromTable"),
                "fromColumn": r.get("fromColumn"),
                "toTable": r.get("toTable"),
                "toColumn": r.get("toColumn"),
            })
        return {"tables": tables, "relationships": relationships}

    # ──────────────────────────────────────────────
    # DAX Patching — Add / Update / Remove
    # ──────────────────────────────────────────────

    def _find_table(self, table_name: str) -> dict:
        """Return the table dict or raise."""
        for t in self.model_schema["model"]["tables"]:
            if t["name"] == table_name:
                return t
        available = [t["name"] for t in self.model_schema["model"]["tables"]]
        raise PBIXPatcherError(
            f"Table '{table_name}' not found. Available tables: {available}"
        )

    def add_measure(
        self,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        format_string: str = "",
        description: str = "",
        display_folder: str = "",
        overwrite: bool = False,
    ) -> dict:
        """
        Add a DAX measure to a table. If the measure already exists and
        overwrite=True, it will be replaced; otherwise raises an error.

        Returns the measure dict that was added.
        """
        self._ensure_extracted()
        table = self._find_table(table_name)

        if "measures" not in table:
            table["measures"] = []

        # Check for duplicates
        for i, m in enumerate(table["measures"]):
            if m["name"] == measure_name:
                if overwrite:
                    table["measures"].pop(i)
                    break
                else:
                    raise PBIXPatcherError(
                        f"Measure '{measure_name}' already exists in table "
                        f"'{table_name}'. Use overwrite=True or update_measure()."
                    )

        measure = {
            "name": measure_name,
            "expression": self._normalize_dax(dax_expression),
        }
        if format_string:
            measure["formatString"] = format_string
        if description:
            measure["description"] = description
        if display_folder:
            measure["displayFolder"] = display_folder

        if not format_string:
            measure["annotations"] = [
                {"name": "PBI_FormatHint", "value": json.dumps({"isGeneralNumber": True})}
            ]

        table["measures"].append(measure)
        return measure

    def update_measure(
        self,
        table_name: str,
        measure_name: str,
        new_dax_expression: Optional[str] = None,
        new_format_string: Optional[str] = None,
        new_description: Optional[str] = None,
        new_display_folder: Optional[str] = None,
    ) -> dict:
        """Update an existing measure's expression and/or metadata."""
        self._ensure_extracted()
        table = self._find_table(table_name)

        for m in table.get("measures", []):
            if m["name"] == measure_name:
                if new_dax_expression is not None:
                    m["expression"] = self._normalize_dax(new_dax_expression)
                if new_format_string is not None:
                    m["formatString"] = new_format_string
                if new_description is not None:
                    m["description"] = new_description
                if new_display_folder is not None:
                    m["displayFolder"] = new_display_folder
                return m

        raise PBIXPatcherError(
            f"Measure '{measure_name}' not found in table '{table_name}'."
        )

    def remove_measure(self, table_name: str, measure_name: str) -> bool:
        """Remove a measure. Returns True if found and removed.

        Raises:
            PBIXPatcherError: If the measure is not found.
        """
        self._ensure_extracted()
        table = self._find_table(table_name)
        measures = table.get("measures", [])
        for i, m in enumerate(measures):
            if m["name"] == measure_name:
                measures.pop(i)
                return True
        raise PBIXPatcherError(
            f"Measure '{measure_name}' not found in table '{table_name}'."
        )

    def add_calculated_column(
        self,
        table_name: str,
        column_name: str,
        dax_expression: str,
        data_type: str = "string",
        format_string: str = "",
    ) -> dict:
        """Add a DAX calculated column to a table."""
        self._ensure_extracted()
        table = self._find_table(table_name)

        if "columns" not in table:
            table["columns"] = []

        for c in table["columns"]:
            if c["name"] == column_name:
                raise PBIXPatcherError(
                    f"Column '{column_name}' already exists in '{table_name}'."
                )

        column = {
            "name": column_name,
            "dataType": data_type,
            "expression": self._normalize_dax(dax_expression),
            "type": "calculated",
            "isDataTypeInferred": True,
        }
        if format_string:
            column["formatString"] = format_string

        table["columns"].append(column)
        return column

    def batch_add_measures(self, measures: list[dict]) -> list[dict]:
        """
        Add multiple measures at once.

        Each dict in the list should have:
            - table_name: str
            - measure_name: str
            - dax_expression: str
            - format_string: str (optional)
            - description: str (optional)
            - display_folder: str (optional)
            - overwrite: bool (optional, default False)
        """
        results = []
        for m in measures:
            result = self.add_measure(
                table_name=m["table_name"],
                measure_name=m["measure_name"],
                dax_expression=m["dax_expression"],
                format_string=m.get("format_string", ""),
                description=m.get("description", ""),
                display_folder=m.get("display_folder", ""),
                overwrite=m.get("overwrite", False),
            )
            results.append(result)
        return results

    # ──────────────────────────────────────────────
    # Repackaging
    # ──────────────────────────────────────────────

    def repackage(self, output_path: Optional[str] = None) -> Path:
        """
        Write the modified model back to the DataModelSchema file
        and repackage everything into a new .pbix archive.

        Args:
            output_path: Destination .pbix path. Defaults to '<original>_patched.pbix'.

        Returns:
            Path to the output .pbix file.
        """
        self._ensure_extracted()

        if output_path is None:
            output_path = self.pbix_path.parent / f"{self.pbix_path.stem}_patched.pbix"
        output_path = Path(output_path).resolve()

        # Write the updated DataModelSchema back to disk
        schema_path = self.work_dir / self.MODEL_SCHEMA_FILENAME
        if not schema_path.exists():
            candidates = list(self.work_dir.rglob("DataModelSchema"))
            schema_path = candidates[0] if candidates else self.work_dir / self.MODEL_SCHEMA_FILENAME

        schema_json = json.dumps(self.model_schema, ensure_ascii=False, indent=2)

        # Power BI Desktop typically expects UTF-16 LE with BOM
        encoded = b"\xff\xfe" + schema_json.encode("utf-16-le")
        schema_path.write_bytes(encoded)

        # Recreate the ZIP archive preserving the original structure
        # First, get the original file list and compression settings
        original_members = {}
        try:
            with zipfile.ZipFile(self.pbix_path, "r") as zf_orig:
                for info in zf_orig.infolist():
                    original_members[info.filename] = info
        except Exception:
            pass  # Fall back to default compression

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(self.work_dir):
                for filename in files:
                    file_path = Path(root) / filename
                    arcname = str(file_path.relative_to(self.work_dir))
                    # Normalize path separators for ZIP
                    arcname = arcname.replace(os.sep, "/")

                    # Use original compression method if available
                    compress_type = zipfile.ZIP_DEFLATED
                    if arcname in original_members:
                        compress_type = original_members[arcname].compress_type

                    zf.write(file_path, arcname, compress_type=compress_type)

        return output_path

    def cleanup(self):
        """Remove the temporary extraction directory."""
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)

    # ──────────────────────────────────────────────
    # Tabular Editor script generation (alternative)
    # ──────────────────────────────────────────────

    def generate_tabular_editor_script(
        self, measures: list[dict], output_path: Optional[str] = None
    ) -> str:
        """
        Generate a C# script for Tabular Editor that adds/updates measures.
        This is an alternative approach: instead of modifying the .pbix directly,
        the user can run this script in Tabular Editor while the .pbix is open.

        Args:
            measures: List of dicts with table_name, measure_name, dax_expression,
                      format_string (optional), description (optional).
            output_path: If given, write the script to this file.

        Returns:
            The C# script as a string.
        """
        lines = [
            "// Auto-generated Tabular Editor script",
            "// Run this in Tabular Editor while connected to your Power BI model",
            "// (Tabular Editor → File → Open → From DB → localhost:<port>)",
            "",
        ]

        for m in measures:
            table = m["table_name"].replace('"', '\\"')
            name = m["measure_name"].replace('"', '\\"')
            dax = m["dax_expression"].replace('"', '\\"').replace("\n", "\\n")
            fmt = m.get("format_string", "").replace('"', '\\"')
            desc = m.get("description", "").replace('"', '\\"')

            lines.append(f'// --- Measure: {m["measure_name"]} ---')
            lines.append("{")
            lines.append(f'    var tableName = "{table}";')
            lines.append(f'    var measureName = "{name}";')
            lines.append(f'    var daxExpression = "{dax}";')
            lines.append(f'    var table = Model.Tables[tableName];')
            lines.append(f"")
            lines.append(f"    Measure measure;")
            lines.append(f"    if (table.Measures.Contains(measureName)) {{")
            lines.append(f"        measure = table.Measures[measureName];")
            lines.append(f'        measure.Expression = daxExpression;')
            lines.append(f"    }} else {{")
            lines.append(f'        measure = table.AddMeasure(measureName, daxExpression);')
            lines.append(f"    }}")
            if fmt:
                lines.append(f'    measure.FormatString = "{fmt}";')
            if desc:
                lines.append(f'    measure.Description = "{desc}";')
            lines.append("}")
            lines.append("")

        script = "\n".join(lines)

        if output_path:
            Path(output_path).write_text(script, encoding="utf-8")

        return script

    # ──────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────

    @staticmethod
    def _normalize_dax(expression: str) -> str:
        """Clean up DAX expression whitespace while preserving line breaks."""
        # Remove leading/trailing whitespace from each line
        lines = [line.strip() for line in expression.strip().splitlines()]
        # Remove empty lines at start/end, but preserve internal blank lines
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def __enter__(self):
        self.extract()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def __repr__(self):
        status = "extracted" if self._extracted else "not extracted"
        return f"PBIXPatcher('{self.pbix_path.name}', {status})"
