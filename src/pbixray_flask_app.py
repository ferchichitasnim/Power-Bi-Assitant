#!/usr/bin/env python3
import asyncio
import ast
import base64
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)
if not logger.handlers and not logging.root.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
_dax_log = os.environ.get("DAX_LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, _dax_log, logging.INFO))
_ollama_singleflight_lock = threading.Semaphore(1)


def _log_flush(msg: str, *args: object) -> None:
    """Flush streams so logs appear while another thread is blocked on Ollama."""
    logger.info(msg, *args)
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.flush()
        except Exception:
            pass


def _truncate_dax_text(text: str, max_len: int, label: str, req_id: str) -> str:
    """max_len <= 0 means no limit."""
    if max_len <= 0 or len(text) <= max_len:
        return text
    logger.warning(
        "[dax] req_id=%s truncating %s %d -> %d chars (0 = unlimited for that field)",
        req_id,
        label,
        len(text),
        max_len,
    )
    return text[:max_len] + "\n\n[... truncated by server; set DAX_MAX_* env to 0 or raise limit ...]"


def _friendly_ollama_error(exc: Exception, base_url: str, model: str | None = None) -> str:
    """Return a short, actionable message for common Ollama connection failures."""
    original = str(exc)
    lowered = original.lower()
    model_hint = f" and pull model `{model}`" if model else ""

    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return f"Cannot reach Ollama at {base_url}. Start Ollama with `ollama serve`" f"{model_hint}, then retry."
        if isinstance(reason, socket.timeout):
            return f"Ollama request timed out at {base_url}. Try again or use a smaller model."

    if "connection refused" in lowered or "winerror 10061" in lowered:
        return f"Cannot reach Ollama at {base_url}. Start Ollama with `ollama serve`" f"{model_hint}, then retry."
    if "timed out" in lowered:
        return f"Ollama request timed out at {base_url}. Try again or use a smaller model."

    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip().startswith("{") else {}
            detail = str(parsed.get("error") or raw).strip()
        except Exception:
            detail = ""
        if detail:
            lowered_detail = detail.lower()
            if "unable to allocate" in lowered_detail or "out of memory" in lowered_detail:
                return (
                    f"Ollama ran out of memory loading model `{model or 'selected model'}`. "
                    "Close other apps, restart Ollama (`ollama serve`), try a smaller model "
                    "(e.g. llama3.2:1b), or reduce story context size."
                )
            return f"Ollama error ({exc.code}): {detail[:400]}"

    return f"Ollama request failed at {base_url}: {original}"


from flask import (
    Flask,
    Response,
    after_this_request,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
)
from flask_cors import CORS
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from pbixray import PBIXRay
from pbix_patcher import PBIXPatcher, PBIXPatcherError
from pbix_upload_store import cleanup_old_uploads, get_upload, register_upload, upload_dir
from pbi_desktop_connector import inject_measures_into_pbi_desktop
from documentation_pdf import (
    assemble_reportlab_document,
    build_deterministic_enrichment,
    enrich_documentation_json,
    enrich_source_labels,
    prepare_pdf_context,
)
from documentation_reportlab import build_reportlab_pdf
from storytelling.ollama_story import (
    build_story_context,
    compact_story_context_for_prompt,
    focus_matches_context,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB request limit
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:3001",
                "http://localhost:3001",
                "http://127.0.0.1:3002",
                "http://localhost:3002",
            ],
            "expose_headers": ["X-Patch-Method", "Content-Disposition"],
        },
        r"/documentation/*": {
            "origins": [
                "http://127.0.0.1:3000",
                "http://localhost:3000",
                "http://127.0.0.1:3001",
                "http://localhost:3001",
                "http://127.0.0.1:3002",
                "http://localhost:3002",
            ]
        },
    },
)

_DOC_CONTEXT_CACHE: dict[str, dict[str, Any]] = {}
_DOC_CONTEXT_LAST_KEY = "__last__"
_DOC_CACHE_MAX_ITEMS = 5


# ---------------------------------------------------------------------------
# Enum maps: PBIXRay returns integers for Cardinality / CrossFilteringBehavior.
# We map them to readable labels for the Documentation panel.
# ---------------------------------------------------------------------------
CARDINALITY_MAP = {0: "None", 1: "One", 2: "Many"}
DIRECTION_MAP = {1: "Single", 2: "Both", 3: "Automatic"}


def _readable_enum(value: Any, mapping: dict[int, str], default: str = "Not available") -> str:
    """Convert an int / str / None into a human label using mapping."""
    if value is None or value == "":
        return default
    try:
        return mapping.get(int(value), str(value))
    except (ValueError, TypeError):
        return str(value)


def normalize_tables(tables_obj: Any) -> list[str]:
    if tables_obj is None:
        return []
    if hasattr(tables_obj, "tolist"):
        values = tables_obj.tolist()
        return [str(v) for v in values]
    return [str(v) for v in tables_obj]


def normalize_statistics(stats_obj: Any) -> list[dict[str, Any]]:
    if stats_obj is None:
        return []
    if hasattr(stats_obj, "to_dict"):
        return stats_obj.to_dict(orient="records")
    if isinstance(stats_obj, list):
        return stats_obj
    return []


def _columns_by_table(stats_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in stats_rows:
        t = str(row.get("TableName") or "").strip()
        c = str(row.get("ColumnName") or "").strip()
        if not t or not c:
            continue
        out.setdefault(t, []).append(c)
    for t in out:
        out[t] = sorted(set(out[t]))
    return out


def _table_roles(
    tables: list[str],
    relationships: list[str],
    relationships_rows: Any = None,
) -> list[dict[str, str]]:
    """
    Determine whether each table is a dimension or fact.

    Strategy (in priority order):
    1. Naming convention: dim*, d_* = dimension; fact*, f_* = fact; *date*, *calendar* = dimension
    2. Relationship direction: a table that only appears on the "To" side (the "one" side /
       lookup side) of relationships is a dimension; tables on the "From" side (many side)
       are facts. This fixes the bug where Gender, AgeGroup, Ethnicity etc. were labeled
       "fact" because the old heuristic only checked the "from" side.
    3. Fallback: if no relationships mention the table, default to "dimension".
    """
    # Build sets of tables that appear on each side of relationships.
    from_tables: set[str] = set()
    to_tables: set[str] = set()

    if isinstance(relationships_rows, list):
        for row in relationships_rows:
            if not isinstance(row, dict):
                continue
            ft = str(row.get("FromTableName") or "").strip()
            tt = str(row.get("ToTableName") or "").strip()
            if ft:
                from_tables.add(ft)
            if tt:
                to_tables.add(tt)
    else:
        # Fallback: parse the text-based relationship lines "Table[col] → Table[col]"
        for line in relationships:
            parts = line.split("→")
            if len(parts) == 2:
                left = parts[0].strip().split("[")[0].strip()
                right = parts[1].strip().split("[")[0].strip()
                if left:
                    from_tables.add(left)
                if right:
                    to_tables.add(right)

    roles: list[dict[str, str]] = []
    for table in tables:
        lower = table.lower()
        # 1. Naming convention (highest priority)
        if lower.startswith(("dim", "d_")):
            role = "dimension"
        elif lower.startswith(("fact", "f_")):
            role = "fact"
        elif "date" in lower or "calendar" in lower:
            role = "dimension"
        else:
            # 2. Relationship-based heuristic
            is_from = table in from_tables  # appears as the "many" / FK side
            is_to = table in to_tables  # appears as the "one" / lookup side

            if is_to and not is_from:
                # Only on the lookup side → dimension (e.g. Gender, AgeGroup, Ethnicity)
                role = "dimension"
            elif is_from and not is_to:
                # Only on the FK side → fact
                role = "fact"
            elif is_from and is_to:
                # Appears on both sides → likely a bridge or snowflake dim; lean fact
                role = "fact"
            else:
                # No relationships mention this table → default dimension
                role = "dimension"

        roles.append({"table": table, "role": role})
    return roles


def _key_columns(schema_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    key_like: list[dict[str, str]] = []
    for row in schema_rows:
        table = str(row.get("TableName") or "").strip()
        column = str(row.get("ColumnName") or "").strip()
        dtype = str(row.get("PandasDataType") or row.get("DataType") or "").strip()
        lower = column.lower()
        if not (table and column):
            continue
        if lower.endswith("id") or "key" in lower or "date" in lower or lower in {"id", "pk", "fk"}:
            key_like.append({"table": table, "column": column, "data_type": dtype})
    return key_like


def _build_raw_context(
    file_name: str,
    tables: list[str],
    columns: dict[str, list[str]],
    measures: list[str],
    relationships: list[str],
    story_context: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append(f"File: {file_name}")
    lines.append(f"Tables: {', '.join(tables)}")
    if columns:
        lines.append("Columns by table:")
        for t in sorted(columns.keys()):
            cols = columns[t]
            parts = [f"{t}[{c}]" for c in cols[:80]]
            extra = ""
            if len(cols) > 80:
                extra = f" ... (+{len(cols) - 80} more columns)"
            lines.append(f"  {t}: {', '.join(parts)}{extra}")
    if measures:
        lines.append("Measures: " + ", ".join(measures[:200]))
        if len(measures) > 200:
            lines.append(f"  ... (+{len(measures) - 200} more measures)")
    if relationships:
        lines.append("Relationships:")
        for rel in relationships[:100]:
            lines.append(f"  {rel}")
    lines.append("")
    lines.append("Story / stats summary (JSON):")
    lines.append(json.dumps(story_context, ensure_ascii=True))
    return "\n".join(lines)


def summarize(stats_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_size = 0
    total_dictionary = 0
    total_hash_index = 0

    for row in stats_rows:
        total_size += int(row.get("DataSize", 0) or 0)
        total_dictionary += int(row.get("Dictionary", 0) or 0)
        total_hash_index += int(row.get("HashIndex", 0) or 0)

    top_size = sorted(stats_rows, key=lambda r: int(r.get("DataSize", 0) or 0), reverse=True)[:15]
    top_cardinality = sorted(stats_rows, key=lambda r: int(r.get("Cardinality", 0) or 0), reverse=True)[:15]

    return {
        "total_columns": len(stats_rows),
        "total_data_size": total_size,
        "total_dictionary": total_dictionary,
        "total_hash_index": total_hash_index,
        "top_size": top_size,
        "top_cardinality": top_cardinality,
    }


def _mcp_text(result: Any) -> str:
    if result is None:
        return ""
    content = getattr(result, "content", None) or []
    texts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n".join(texts).strip()


def _mcp_parse_json(text: str, fallback: Any) -> Any:
    if not text:
        return fallback
    stripped = text.strip()
    if stripped.lower().startswith("error:"):
        raise RuntimeError(stripped)
    try:
        return json.loads(stripped)
    except Exception:
        return fallback


def _coerce_tables(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, tuple):
        return [str(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("Index(") and "[" in s and "]" in s:
            inner = s[s.find("[") : s.rfind("]") + 1]
            try:
                parsed = ast.literal_eval(inner)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except Exception:
                pass
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = ast.literal_eval(s)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except Exception:
                pass
    return []


def _is_internal_powerbi_table(table_name: str) -> bool:
    """
    Filter internal auto-generated Power BI helper tables from user-facing docs.
    These are not real business model tables and should not be counted/displayed.
    """
    name = str(table_name or "").strip()
    if not name:
        return False
    lower = name.lower().lstrip(" '\"`[$")
    return "localdatetable_" in lower or "datetabletemplate_" in lower


def _filter_table_names(tables: list[str]) -> list[str]:
    return [t for t in tables if not _is_internal_powerbi_table(t)]


def _filter_rows_by_table_name(
    rows: Any,
    table_keys: tuple[str, ...],
    *,
    drop_none: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        should_skip = False
        for key in table_keys:
            table_name = str(row.get(key) or "").strip()
            # Skip internal Power BI tables
            if table_name and _is_internal_powerbi_table(table_name):
                should_skip = True
                break
            # Skip rows where table name is None/empty (PBIXRay stores None
            # when the target is an internal auto-generated table)
            if drop_none and (not table_name or table_name.lower() == "none"):
                should_skip = True
                break
        if not should_skip:
            out.append(row)
    return out


def _mcp_measure_names(measures_rows: Any) -> list[str]:
    if not isinstance(measures_rows, list):
        return []
    out: list[str] = []
    for row in measures_rows:
        if not isinstance(row, dict):
            continue
        table_name = str(row.get("TableName") or "").strip()
        measure_name = str(row.get("Name") or "").strip()
        if table_name and measure_name:
            out.append(f"{table_name}[{measure_name}]")
        elif measure_name:
            out.append(measure_name)
    return sorted(set(out))


def _mcp_relationship_lines(relationships_rows: Any) -> list[str]:
    """Build short readable text lines for the raw context (used by Ollama prompts)."""
    if not isinstance(relationships_rows, list):
        return []
    lines: list[str] = []
    for row in relationships_rows:
        if not isinstance(row, dict):
            continue
        from_table = str(row.get("FromTableName") or "").strip()
        from_column = str(row.get("FromColumnName") or "").strip()
        to_table = str(row.get("ToTableName") or "").strip()
        to_column = str(row.get("ToColumnName") or "").strip()
        if not (from_table and from_column and to_table and to_column):
            continue
        is_active = bool(row.get("IsActive", True))
        suffix = "" if is_active else " (inactive)"
        lines.append(f"{from_table}[{from_column}] → {to_table}[{to_column}]{suffix}")
    return lines


def _extract_sources_from_rows(power_query_rows: Any, metadata_rows: Any) -> list[str]:
    """Heuristic extraction of data-source hints from Power Query (M) text.

    PBIXRay exposes each query's M ``Expression`` as a string. We do not execute M;
    we scan for common connector function calls (Sql.Database, Web.Contents, etc.)
    and optional model metadata rows whose names look like connection fields.

    Dynamic M (variables built outside string literals) may not match; results are
    best-effort labels for documentation, not a full lineage engine.
    """
    sources: set[str] = set()
    if isinstance(power_query_rows, list):
        for row in power_query_rows:
            if not isinstance(row, dict):
                continue
            expr = str(row.get("Expression") or "")
            if not expr:
                continue
            # Regexes over literal strings inside M. Order: specific multi-arg first.
            patterns = [
                # SQL / cloud SQL
                r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                r'Sql\.Databases\(\s*"([^"]+)"',
                r'AzureSQL\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                r'AnalysisServices\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                # Other databases
                r'Oracle\.Database\(\s*"([^"]+)"',
                r'Snowflake\.Databases\(\s*"([^"]+)"',
                r'PostgreSQL\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                r'MySQL\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                r'Db2\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                r'Teradata\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"',
                # Files & folders
                r'Excel\.Workbook\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Csv\.Document\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Parquet\.Document\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Json\.Document\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Xml\.Tables\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Access\.Database\(\s*File\.Contents\(\s*"([^"]+)"',
                r'Folder\.Files\(\s*"([^"]+)"',
                r'Folder\.Contents\(\s*"([^"]+)"',
                # Web & APIs
                r'Web\.Contents\(\s*"([^"]+)"',
                r'Json\.Document\(\s*Web\.Contents\(\s*"([^"]+)"',
                r'Xml\.Document\(\s*Web\.Contents\(\s*"([^"]+)"',
                r'OData\.Feed\(\s*"([^"]+)"',
                # SharePoint
                r'SharePoint\.Files\(\s*"([^"]+)"',
                r'SharePoint\.Contents\(\s*"([^"]+)"',
                r'SharePoint\.Tables\(\s*"([^"]+)"',
                # Generic ODBC / OleDb (connection string or DSN in first string arg)
                r'Odbc\.DataSource\(\s*"([^"]+)"',
                r'OleDb\.DataSource\(\s*"([^"]+)"',
            ]
            for pattern in patterns:
                for match in re.findall(pattern, expr, flags=re.IGNORECASE):
                    if isinstance(match, tuple):
                        value = " / ".join(str(v).strip() for v in match if str(v).strip())
                    else:
                        value = str(match).strip()
                    if value:
                        sources.add(value)
    if isinstance(metadata_rows, list):
        for row in metadata_rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").lower()
            value = str(row.get("Value") or "").strip()
            if value and any(k in name for k in ("source", "server", "database", "provider", "connection")):
                sources.add(value)
    # Post-process: make raw connection strings more readable
    cleaned: set[str] = set()
    for s in sources:
        # "." is SQL Server shorthand for localhost
        s = re.sub(r"^\.\s*/\s*", "localhost / ", s)
        # "(local)" is another SQL Server localhost alias
        s = re.sub(r"^\(local\)\s*/\s*", "localhost / ", s, flags=re.IGNORECASE)
        # Format "server / database" into a cleaner label
        parts = [p.strip() for p in s.split(" / ")]
        if len(parts) == 2:
            server, database = parts
            s = f"{server} (database: {database})"
        cleaned.add(s)
    return enrich_source_labels(sorted(cleaned), power_query_rows)


def _dax_measure_docs_from_rows(measures_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(measures_rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in measures_rows:
        if not isinstance(row, dict):
            continue
        table = str(row.get("TableName") or "").strip()
        name = str(row.get("Name") or "").strip()
        formula = str(row.get("Expression") or "").strip()
        if not name:
            continue
        measure_ref = f"{table}[{name}]" if table else name
        dependencies: list[str] = []
        for dep in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\[([^\]]+)\]", formula):
            dependencies.append(f"{dep[0]}[{dep[1]}]")
        out.append(
            {
                "name": name,
                "table": table,
                "reference": measure_ref,
                "formula": formula,
                "business_meaning": "Business meaning is not explicitly stored in PBIX metadata.",
                "dependencies": sorted(set(dependencies)),
            }
        )
    return out


def _dax_column_docs_from_rows(dax_columns_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(dax_columns_rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in dax_columns_rows:
        if not isinstance(row, dict):
            continue
        table = str(row.get("TableName") or "").strip()
        name = str(row.get("ColumnName") or row.get("Name") or "").strip()
        formula = str(row.get("Expression") or "").strip()
        if not (table and name):
            continue
        out.append({"table": table, "name": name, "reference": f"{table}[{name}]", "formula": formula})
    return out


def _m_parameters_docs_from_rows(m_parameters_rows: Any) -> list[dict[str, Any]]:
    if not isinstance(m_parameters_rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in m_parameters_rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        current = row.get("CurrentValue")
        required = row.get("IsRequired")
        typ = row.get("Type")
        desc = row.get("Description")
        out.append(
            {
                "name": name,
                "current_value": "" if current is None else str(current),
                "type": "" if typ is None else str(typ),
                "is_required": bool(required) if required is not None else False,
                "description": "" if desc is None else str(desc),
            }
        )
    return out


def _relationship_details_from_rows(relationships_rows: Any) -> list[dict[str, Any]]:
    """
    Build human-readable relationship records for the Documentation UI.

    PBIXRay returns Cardinality / CrossFilteringBehavior as integer codes,
    so we map them to labels. We handle both the newer FromCardinality /
    ToCardinality split and the legacy single Cardinality column.
    """
    if not isinstance(relationships_rows, list):
        return []

    details: list[dict[str, Any]] = []
    for row in relationships_rows:
        if not isinstance(row, dict):
            continue

        from_card = row.get("FromCardinality")
        to_card = row.get("ToCardinality")
        legacy_card = row.get("Cardinality")

        if from_card is not None or to_card is not None:
            cardinality = (
                f"{_readable_enum(from_card, CARDINALITY_MAP, '?')}" f":{_readable_enum(to_card, CARDINALITY_MAP, '?')}"
            )
        elif legacy_card is not None:
            cardinality = _readable_enum(legacy_card, CARDINALITY_MAP)
        else:
            cardinality = "Not available"

        direction = _readable_enum(row.get("CrossFilteringBehavior"), DIRECTION_MAP)

        details.append(
            {
                "from": f"{row.get('FromTableName')}[{row.get('FromColumnName')}]",
                "to": f"{row.get('ToTableName')}[{row.get('ToColumnName')}]",
                "cardinality": cardinality,
                "direction": direction,
                "active": bool(row.get("IsActive", True)),
            }
        )
    return details


def _extract_rls_from_pbixray_model(model: Any) -> dict[str, Any]:
    """Extract RLS roles from PBIXRay model (same candidate attrs as MCP server)."""
    details: list[dict[str, Any]] = []
    has_rls = False
    candidate_attrs = (
        "roles",
        "rls",
        "role_permissions",
        "table_permissions",
        "dax_rls",
        "row_level_security",
        "tmschema_role_memberships",
    )
    for attr in candidate_attrs:
        try:
            value = getattr(model, attr, None)
            if value is None:
                continue
            if hasattr(value, "to_dict"):
                rows = value.to_dict(orient="records")
                if rows:
                    has_rls = True
                    details.append({"source": attr, "count": len(rows), "entries": rows[:50]})
                continue
            if isinstance(value, (list, tuple)) and len(value) > 0:
                has_rls = True
                details.append({"source": attr, "count": len(value), "entries": list(value)[:50]})
                continue
            if isinstance(value, dict) and len(value) > 0:
                has_rls = True
                details.append({"source": attr, "count": len(value), "entries": value})
        except Exception:
            continue
    return {"has_rls": has_rls, "details": details}


def _normalize_rls_from_mcp(rls_data: Any) -> dict[str, Any]:
    """
    Convert the MCP get_rls_roles response into a UI-friendly shape.
    Input:  { "has_rls": bool, "details": [ {source, count, entries}, ... ] }
    Output: { "has_rls": bool, "details": ["source: N entries", ...] }
    """
    if not isinstance(rls_data, dict):
        return {"has_rls": False, "details": []}

    raw_details = rls_data.get("details", []) or []
    detail_strings: list[str] = []
    for d in raw_details:
        if isinstance(d, dict):
            src = d.get("source", "unknown")
            count = d.get("count", 0)
            detail_strings.append(f"{src}: {count} entries")
        else:
            detail_strings.append(str(d))

    return {
        "has_rls": bool(rls_data.get("has_rls", False)),
        "details": detail_strings,
    }


def _documentation_payload_from_mcp(
    *,
    tables: list[str],
    relationships: list[str],
    schema_rows: list[dict[str, Any]],
    metadata_rows: Any,
    power_query_rows: Any,
    measures_rows: Any,
    dax_columns_rows: Any,
    m_parameters_rows: Any,
    relationships_rows: Any,
    rls_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources = _extract_sources_from_rows(power_query_rows, metadata_rows)
    measures = _dax_measure_docs_from_rows(measures_rows)
    dax_columns = _dax_column_docs_from_rows(dax_columns_rows)
    m_parameters = _m_parameters_docs_from_rows(m_parameters_rows)
    relationship_details = _relationship_details_from_rows(relationships_rows)
    table_roles = _table_roles(tables, relationships, relationships_rows)
    key_columns = _key_columns(schema_rows)

    # RLS sourced from dedicated MCP tool (no longer hardcoded).
    rls_raw = rls_data if isinstance(rls_data, dict) else {"has_rls": False, "details": []}
    rls = _normalize_rls_from_mcp(rls_raw)

    return {
        "report_sources": {"sources": sources},
        "report_model": {"tables": tables, "relationships": relationship_details},
        "dax_calculations": {"calculated_columns": dax_columns, "measures": measures},
        "security_and_parameters": {"rls": rls, "rls_raw": rls_raw, "parameters": m_parameters},
        "model_schema": schema_rows,
        "power_query": power_query_rows if isinstance(power_query_rows, list) else [],
        "data_sources": {
            "source_systems": sources,
            "tables_used": tables,
            "refresh_frequency": "Not available in extracted PBIX metadata.",
        },
        "data_model": {
            "table_roles": table_roles,
            "relationships": relationship_details,
            "key_columns": key_columns,
        },
        "measures": measures,
        "kpis_metrics_definitions": [],
        "report_pages_visuals": [],
        "filters_slicers": {
            "global_filters": [],
            "page_level_filters": [],
            "default_states": "Not available in extracted PBIX metadata.",
        },
        "refresh_performance": {
            "refresh_schedule": "Not available in extracted PBIX metadata.",
            "data_volume": {"table_count": len(tables), "relationship_count": len(relationships)},
            "known_performance_issues": [],
            "optimization_notes": [],
        },
        "governance_compliance": {
            "dataset_certification_status": "Not available in extracted PBIX metadata.",
            "sensitivity_label": "Not available in extracted PBIX metadata.",
            "data_ownership": "Not available in extracted PBIX metadata.",
            "access_rules": "Not available in extracted PBIX metadata.",
        },
    }


async def _extract_context_via_mcp(resolved: str) -> dict[str, Any]:
    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pbixray_server.py")
    params = StdioServerParameters(command=sys.executable, args=[server_path], env=None)
    init_timeout = float(os.environ.get("MCP_INIT_TIMEOUT_SEC", "20"))
    total_timeout = float(os.environ.get("MCP_TOTAL_EXTRACT_TIMEOUT_SEC", "90"))

    async def _call_tool_text(
        session: ClientSession,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout_sec: float,
        optional: bool = False,
    ) -> str:
        args = arguments or {}
        logger.info("[mcp] call start tool=%s timeout=%.1fs optional=%s", tool_name, timeout_sec, optional)
        try:
            result = await asyncio.wait_for(session.call_tool(tool_name, args), timeout=timeout_sec)
            text = _mcp_text(result)
            logger.info("[mcp] call done tool=%s text_chars=%d", tool_name, len(text))
            return text
        except asyncio.TimeoutError:
            msg = f"MCP tool timed out: {tool_name} after {timeout_sec:.1f}s"
            if optional:
                logger.warning("[mcp] %s (continuing with fallback)", msg)
                return ""
            logger.error("[mcp] %s", msg)
            raise RuntimeError(msg)
        except Exception as exc:
            if optional:
                logger.warning("[mcp] tool failed but optional tool=%s error=%s", tool_name, exc)
                return ""
            logger.error("[mcp] tool failed tool=%s error=%s", tool_name, exc)
            raise

    required_timeout = float(os.environ.get("MCP_TOOL_TIMEOUT_SEC", "45"))
    optional_timeout = float(os.environ.get("MCP_OPTIONAL_TOOL_TIMEOUT_SEC", "20"))

    async def _run_mcp_pipeline() -> dict[str, Any]:
        logger.info("[mcp] opening stdio client server_path=%s", server_path)
        async with stdio_client(params) as streams:
            logger.info("[mcp] stdio client connected")
            async with ClientSession(streams[0], streams[1]) as session:
                logger.info("[mcp] session initialize start timeout=%.1fs", init_timeout)
                try:
                    await asyncio.wait_for(session.initialize(), timeout=init_timeout)
                except asyncio.TimeoutError:
                    msg = f"MCP session initialize timed out after {init_timeout:.1f}s"
                    logger.error("[mcp] %s", msg)
                    raise RuntimeError(msg)
                logger.info("[mcp] session initialize done")

                load_text = await _call_tool_text(
                    session,
                    "load_pbix_file",
                    {"file_path": resolved},
                    timeout_sec=required_timeout,
                    optional=False,
                )
                if load_text.lower().startswith("error:"):
                    raise RuntimeError(load_text)

                # All model introspection goes through MCP tools.
                # NOTE: The MCP server (pbixray_server.py) now filters internal
                # tables at the source, so the data returned here is already clean.
                # We still apply _filter_* as a safety net.
                tables_text = await _call_tool_text(session, "get_tables", {}, timeout_sec=required_timeout, optional=False)
                stats_text = await _call_tool_text(session, "get_statistics", {}, timeout_sec=required_timeout, optional=False)
                measures_text = await _call_tool_text(
                    session, "get_dax_measures", {}, timeout_sec=required_timeout, optional=False
                )
                dax_columns_text = await _call_tool_text(
                    session, "get_dax_columns", {}, timeout_sec=required_timeout, optional=False
                )
                schema_text = await _call_tool_text(session, "get_schema", {}, timeout_sec=required_timeout, optional=False)
                relationships_text = await _call_tool_text(
                    session, "get_relationships", {}, timeout_sec=required_timeout, optional=False
                )
                metadata_text = await _call_tool_text(session, "get_metadata", {}, timeout_sec=optional_timeout, optional=True)
                power_query_text = await _call_tool_text(
                    session, "get_power_query", {}, timeout_sec=optional_timeout, optional=True
                )
                m_parameters_text = await _call_tool_text(
                    session, "get_m_parameters", {}, timeout_sec=optional_timeout, optional=True
                )
                model_summary_text = await _call_tool_text(
                    session, "get_model_summary", {}, timeout_sec=optional_timeout, optional=True
                )
                # Dedicated MCP tool for Row-Level Security roles.
                rls_text = await _call_tool_text(session, "get_rls_roles", {}, timeout_sec=optional_timeout, optional=True)

                tables_data = _mcp_parse_json(tables_text, [])
                stats_rows = _mcp_parse_json(stats_text, [])
                measures_rows = _mcp_parse_json(measures_text, [])
                dax_columns_rows = _mcp_parse_json(dax_columns_text, [])
                schema_rows = _mcp_parse_json(schema_text, [])
                relationships_rows = _mcp_parse_json(relationships_text, [])
                metadata_data = _mcp_parse_json(metadata_text, {})
                power_query_rows = _mcp_parse_json(power_query_text, [])
                m_parameters_rows = _mcp_parse_json(m_parameters_text, [])
                model_summary = _mcp_parse_json(model_summary_text, {})
                rls_data = _mcp_parse_json(rls_text, {"has_rls": False, "details": []})

                tables = _coerce_tables(tables_data)
                if not tables and isinstance(model_summary, dict):
                    tables = _coerce_tables(model_summary.get("tables", []))
                if not isinstance(stats_rows, list):
                    stats_rows = []
                if not isinstance(schema_rows, list):
                    schema_rows = []
                if not tables and stats_rows:
                    tables = sorted(
                        {
                            str(row.get("TableName")).strip()
                            for row in stats_rows
                            if isinstance(row, dict) and str(row.get("TableName") or "").strip()
                        }
                    )

                # Safety-net filtering (MCP server already filters, but belt-and-suspenders)
                tables = _filter_table_names(tables)
                stats_rows = _filter_rows_by_table_name(stats_rows, ("TableName",))
                schema_rows = _filter_rows_by_table_name(schema_rows, ("TableName",))
                relationships_rows = _filter_rows_by_table_name(
                    relationships_rows, ("FromTableName", "ToTableName"), drop_none=True
                )
                measures_rows = _filter_rows_by_table_name(measures_rows, ("TableName",))
                dax_columns_rows = _filter_rows_by_table_name(dax_columns_rows, ("TableName",))
                power_query_rows = _filter_rows_by_table_name(power_query_rows, ("TableName",))

                measures = _mcp_measure_names(measures_rows)
                relationships = _mcp_relationship_lines(relationships_rows)
                story_context = build_story_context(resolved, tables, stats_rows, measures)

                metadata_rows: list[dict[str, Any]] = []
                if isinstance(metadata_data, dict):
                    metadata_rows = [{"Name": k, "Value": v} for k, v in metadata_data.items()]

                sources = _extract_sources_from_rows(power_query_rows, metadata_rows)

                # ---- Debug logs ----
                try:
                    logger.info(
                        "[mcp] tables=%d stats=%d measures=%d dax_cols=%d rels=%d "
                        "meta_keys=%d pq=%d m_params=%d rls_has=%s rls_details=%d",
                        len(tables),
                        len(stats_rows) if isinstance(stats_rows, list) else -1,
                        len(measures_rows) if isinstance(measures_rows, list) else -1,
                        len(dax_columns_rows) if isinstance(dax_columns_rows, list) else -1,
                        len(relationships_rows) if isinstance(relationships_rows, list) else -1,
                        len(metadata_data) if isinstance(metadata_data, dict) else -1,
                        len(power_query_rows) if isinstance(power_query_rows, list) else -1,
                        len(m_parameters_rows) if isinstance(m_parameters_rows, list) else -1,
                        rls_data.get("has_rls") if isinstance(rls_data, dict) else "?",
                        len(rls_data.get("details", [])) if isinstance(rls_data, dict) else -1,
                    )
                    if isinstance(relationships_rows, list) and relationships_rows:
                        logger.info(
                            "[mcp] relationship sample keys=%s",
                            list(relationships_rows[0].keys()) if isinstance(relationships_rows[0], dict) else "n/a",
                        )
                except Exception:
                    pass
                # --------------------

                documentation = _documentation_payload_from_mcp(
                    tables=tables,
                    relationships=relationships,
                    schema_rows=schema_rows,
                    metadata_rows=metadata_rows,
                    power_query_rows=power_query_rows,
                    measures_rows=measures_rows,
                    dax_columns_rows=dax_columns_rows,
                    m_parameters_rows=m_parameters_rows,
                    relationships_rows=relationships_rows,
                    rls_data=rls_data,
                )

                return {
                    "tables": tables,
                    "stats_rows": stats_rows,
                    "story_context": story_context,
                    "measures": measures,
                    "relationships": relationships,
                    "sources": sources,
                    "documentation": documentation,
                }

    logger.info("[mcp] extraction pipeline start timeout=%.1fs file=%s", total_timeout, resolved)
    try:
        payload = await asyncio.wait_for(_run_mcp_pipeline(), timeout=total_timeout)
        logger.info("[mcp] extraction pipeline done")
        return payload
    except asyncio.TimeoutError:
        msg = f"MCP extraction timed out after {total_timeout:.1f}s"
        logger.error("[mcp] %s", msg)
        raise RuntimeError(msg)


def _extract_context_via_mcp_sync(resolved: str) -> dict[str, Any]:
    return asyncio.run(_extract_context_via_mcp(resolved))


def _safe_df_records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict(orient="records")
            return rows if isinstance(rows, list) else []
        except Exception:
            return []
    return []


def _extract_context_direct(resolved: str) -> dict[str, Any]:
    """
    Fallback extractor when MCP handshake/initialize is unavailable.
    Uses PBIXRay directly in-process to avoid MCP transport issues.
    """
    logger.warning("[direct] falling back to in-process PBIXRay extraction for file=%s", resolved)
    model = PBIXRay(resolved)

    tables = _filter_table_names(normalize_tables(getattr(model, "tables", [])))
    stats_rows = _filter_rows_by_table_name(_safe_df_records(getattr(model, "statistics", None)), ("TableName",))
    schema_rows = _filter_rows_by_table_name(_safe_df_records(getattr(model, "schema", None)), ("TableName",))
    measures_rows = _filter_rows_by_table_name(_safe_df_records(getattr(model, "dax_measures", None)), ("TableName",))
    dax_columns_rows = _filter_rows_by_table_name(_safe_df_records(getattr(model, "dax_columns", None)), ("TableName",))
    relationships_rows = _filter_rows_by_table_name(
        _safe_df_records(getattr(model, "relationships", None)),
        ("FromTableName", "ToTableName"),
        drop_none=True,
    )
    power_query_rows = _filter_rows_by_table_name(
        _safe_df_records(getattr(model, "power_query", None)),
        ("TableName",),
    )
    m_parameters_rows = _safe_df_records(getattr(model, "m_parameters", None))

    metadata_rows = _safe_df_records(getattr(model, "metadata", None))
    metadata_data: dict[str, Any] = {}
    for row in metadata_rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        metadata_data[name] = row.get("Value")

    measures = _mcp_measure_names(measures_rows)
    relationships = _mcp_relationship_lines(relationships_rows)
    story_context = build_story_context(resolved, tables, stats_rows, measures)
    sources = _extract_sources_from_rows(power_query_rows, metadata_rows)

    rls_data = _extract_rls_from_pbixray_model(model)
    documentation = _documentation_payload_from_mcp(
        tables=tables,
        relationships=relationships,
        schema_rows=schema_rows,
        metadata_rows=metadata_rows,
        power_query_rows=power_query_rows,
        measures_rows=measures_rows,
        dax_columns_rows=dax_columns_rows,
        m_parameters_rows=m_parameters_rows,
        relationships_rows=relationships_rows,
        rls_data=rls_data,
    )

    logger.info(
        "[direct] tables=%d stats=%d measures=%d dax_cols=%d rels=%d meta_keys=%d pq=%d m_params=%d",
        len(tables),
        len(stats_rows),
        len(measures_rows),
        len(dax_columns_rows),
        len(relationships_rows),
        len(metadata_data),
        len(power_query_rows),
        len(m_parameters_rows),
    )

    return {
        "tables": tables,
        "stats_rows": stats_rows,
        "story_context": story_context,
        "measures": measures,
        "relationships": relationships,
        "sources": sources,
        "documentation": documentation,
        "context_source": "direct",
    }


def _launch_power_bi_desktop(target_path: Path) -> tuple[bool, str]:
    pbidesktop_exe = os.environ.get("POWERBI_DESKTOP_EXE", "").strip()
    candidates: list[list[str]] = []

    if pbidesktop_exe:
        candidates.append([pbidesktop_exe, str(target_path)])
    candidates.append(["cmd", "/c", "start", "", str(target_path)])
    candidates.append(["powershell", "-NoProfile", "-Command", f'Start-Process -FilePath "{str(target_path)}"'])

    last_error = ""
    for cmd in candidates:
        try:
            subprocess.Popen(cmd)
            return True, "Power BI launch command started."
        except Exception as exc:
            last_error = str(exc)
            continue
    return False, last_error or "Unable to launch Power BI Desktop."


def extract_pbix_payload(resolved: str) -> dict[str, Any]:
    context_source = "mcp"
    try:
        mcp_payload = _extract_context_via_mcp_sync(resolved)
        context_source = "mcp"
        if not isinstance(mcp_payload, dict):
            raise RuntimeError("MCP extraction returned an invalid payload")
    except Exception as exc:
        logger.warning("[extract] MCP path failed, switching to direct PBIXRay: %s", exc)
        mcp_payload = _extract_context_direct(resolved)
        context_source = str(mcp_payload.get("context_source") or "direct")
    tables = mcp_payload["tables"]
    stats_rows = mcp_payload["stats_rows"]
    story_context = mcp_payload["story_context"]
    schema_rows = _filter_rows_by_table_name(
        _ensure_list((mcp_payload.get("documentation") or {}).get("model_schema")),
        ("TableName",),
    )
    if not schema_rows:
        schema_rows = _filter_rows_by_table_name(_ensure_list(stats_rows), ("TableName",))
    columns = _columns_by_table(schema_rows if schema_rows else stats_rows)
    measures = mcp_payload["measures"]
    relationships = mcp_payload["relationships"]
    sources = mcp_payload.get("sources", [])
    documentation = mcp_payload.get("documentation", {})
    power_query_rows = _ensure_list(documentation.get("power_query"))
    summary = summarize(stats_rows)

    raw_context = _build_raw_context(
        os.path.basename(resolved),
        tables,
        columns,
        measures,
        relationships,
        story_context,
    )
    return {
        "ok": True,
        "pbix_path": resolved,
        "file_name": os.path.basename(resolved),
        "tables": tables,
        "summary": summary,
        "stats_preview": stats_rows[:100],
        "context": story_context,
        "columns": columns,
        "schema": schema_rows,
        "measures": measures,
        "relationships": relationships,
        "sources": sources,
        "power_query": power_query_rows,
        "documentation": documentation,
        "rawContext": raw_context,
        "contextSource": context_source,
        "contextError": "",
    }


def _ensure_list(value: Any) -> list:
    """Convert any value to a plain Python list safely."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        try:
            return value.tolist()
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (str, dict)):
        try:
            return list(value)
        except (TypeError, ValueError):
            pass
    return []


def _build_pdf_context_from_extracted(payload: dict[str, Any]) -> dict[str, Any]:
    documentation = payload.get("documentation") or {}
    report_sources = documentation.get("report_sources") or {}
    report_model = documentation.get("report_model") or {}
    data_model = documentation.get("data_model") or {}
    dax_calculations = documentation.get("dax_calculations") or {}
    security_and_parameters = documentation.get("security_and_parameters") or {}
    security_rls = security_and_parameters.get("rls")
    rls_raw = security_and_parameters.get("rls_raw")

    stats_rows = _ensure_list(payload.get("stats_preview") or payload.get("statistics") or [])
    schema_rows = _ensure_list(documentation.get("model_schema") or payload.get("schema"))
    columns = payload.get("columns") if isinstance(payload.get("columns"), dict) else {}
    if not columns and stats_rows:
        columns = _columns_by_table(stats_rows)

    return {
        "filename": str(payload.get("uploaded_name") or payload.get("file_name") or "PowerBI_Documentation.pbix"),
        "sources": _ensure_list(report_sources.get("sources") or payload.get("sources")),
        "tables": _ensure_list(report_model.get("tables") or payload.get("tables")),
        "relationships": _ensure_list(report_model.get("relationships") or payload.get("relationships")),
        "measures": _ensure_list(dax_calculations.get("measures")),
        "calculated_columns": _ensure_list(dax_calculations.get("calculated_columns")),
        "rls": rls_raw if rls_raw else security_rls,
        "parameters": _ensure_list(security_and_parameters.get("parameters")),
        "schema": schema_rows,
        "statistics": stats_rows,
        "columns": columns,
        "power_query": _ensure_list(payload.get("power_query") or documentation.get("power_query")),
        "table_roles": _ensure_list(data_model.get("table_roles")),
    }


def _cache_doc_context(payload: dict[str, Any], uploaded_filename: str) -> None:
    context = _build_pdf_context_from_extracted(payload)
    key = uploaded_filename.strip().lower()
    if key:
        _DOC_CONTEXT_CACHE[key] = context
    _DOC_CONTEXT_CACHE[_DOC_CONTEXT_LAST_KEY] = context

    # Avoid unbounded growth.
    keys = [k for k in _DOC_CONTEXT_CACHE.keys() if k != _DOC_CONTEXT_LAST_KEY]
    if len(keys) > _DOC_CACHE_MAX_ITEMS:
        for old_key in keys[:-_DOC_CACHE_MAX_ITEMS]:
            _DOC_CONTEXT_CACHE.pop(old_key, None)


@app.get("/")
def index():
    default_path = os.path.abspath("Employee Hiring and History.pbix")
    return render_template("dashboard.html", default_path=default_path)


@app.get("/storytelling")
def storytelling_get():
    ui = os.environ.get("STORY_UI_URL", "http://127.0.0.1:3000").strip()
    if ui:
        return redirect(ui, code=302)
    return redirect("/", code=302)


@app.get("/api/pbix/context")
def api_pbix_context():
    pbix_path = (request.args.get("pbix_path") or "").strip()
    if not pbix_path:
        return jsonify({"ok": False, "error": "pbix_path is required"}), 400

    resolved = os.path.expanduser(pbix_path)
    if not os.path.exists(resolved):
        return jsonify({"ok": False, "error": f"PBIX file not found: {resolved}"}), 404

    try:
        return jsonify(extract_pbix_payload(resolved))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/pbix/upload")
def api_pbix_upload():
    logger.info("[upload] request received content_length=%s", request.content_length)
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "Missing file field."}), 400

    filename = upload.filename or ""
    logger.info("[upload] processing filename=%s", filename)
    if not filename.lower().endswith(".pbix"):
        return jsonify({"ok": False, "error": "Only .pbix files are accepted."}), 400

    cleanup_old_uploads()
    pbix_id = str(uuid.uuid4())
    dest = upload_dir() / f"{pbix_id}.pbix"
    try:
        upload.save(str(dest))
        logger.info("[upload] saved pbix id=%s path=%s", pbix_id, dest)
        logger.info("[upload] starting extract_pbix_payload")
        payload = extract_pbix_payload(str(dest))
        logger.info("[upload] extract_pbix_payload completed ok=%s", payload.get("ok"))
        payload["uploaded_name"] = filename
        payload["pbix_id"] = pbix_id
        payload["pbix_path"] = str(dest)
        register_upload(pbix_id, dest, filename)
        _cache_doc_context(payload, filename)
        return jsonify(payload)
    except Exception as exc:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({"ok": False, "error": str(exc)}), 500


def _normalize_patch_measures(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("measures must be a non-empty list")
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        table_name = str(item.get("table_name") or "").strip()
        measure_name = str(item.get("measure_name") or "").strip()
        dax_expression = str(item.get("dax_expression") or "").strip()
        if not table_name or not measure_name or not dax_expression:
            raise ValueError("Each measure requires table_name, measure_name, and dax_expression.")
        out.append(
            {
                "table_name": table_name,
                "measure_name": measure_name,
                "dax_expression": dax_expression,
                "format_string": str(item.get("format_string") or ""),
                "description": str(item.get("description") or ""),
                "display_folder": str(item.get("display_folder") or ""),
            }
        )
    if not out:
        raise ValueError("measures must contain at least one valid measure object")
    return out


def _patch_measures_tabular_script_response(
    pbix_path: str,
    measures: list[dict[str, str]],
    original_filename: str,
) -> Response:
    """Tabular Editor script fallback — does not require DataModelSchema / extract()."""
    patcher = PBIXPatcher(pbix_path)
    stem = Path(original_filename).stem or "model"
    script_path = Path(tempfile.gettempdir()) / f"{stem}_measures.csx"
    try:
        script = patcher.generate_tabular_editor_script(
            [
                {
                    "table_name": m["table_name"],
                    "measure_name": m["measure_name"],
                    "dax_expression": m["dax_expression"],
                    "format_string": m.get("format_string", ""),
                    "description": m.get("description", ""),
                }
                for m in measures
            ]
        )
        script_path.write_text(script, encoding="utf-8")
    finally:
        patcher.cleanup()

    @after_this_request
    def _remove_script_file(response):  # noqa: ANN001
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
        return response

    response = send_file(
        str(script_path),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"{stem}_measures.csx",
    )
    response.headers["X-Patch-Method"] = "tabular-editor-script"
    return response


@app.post("/api/pbix/inject-measures")
def api_pbix_inject_measures():
    """Inject DAX measures directly into a running Power BI Desktop instance."""
    body = request.get_json(silent=True) or {}
    measures = body.get("measures") or []

    if not measures:
        return jsonify({"ok": False, "error": "No measures provided"}), 400

    try:
        measures = _normalize_patch_measures(measures)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    result = inject_measures_into_pbi_desktop(measures)

    if result.get("ok"):
        return jsonify(result), 200
    return jsonify(result), 422


@app.post("/api/pbix/patch-measures")
def api_pbix_patch_measures():
    body = request.get_json(silent=True) or {}
    pbix_id = str(body.get("pbix_id") or "").strip()
    if not pbix_id:
        return jsonify({"ok": False, "error": "pbix_id is required"}), 400

    try:
        measures = _normalize_patch_measures(body.get("measures"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    meta = get_upload(pbix_id)
    if not meta:
        return jsonify({"ok": False, "error": "PBIX file not found or expired. Re-upload the file."}), 404

    pbix_path = Path(meta["file_path"])
    if not pbix_path.exists():
        return jsonify({"ok": False, "error": "PBIX file no longer on server. Re-upload the file."}), 404

    original_filename = str(meta.get("original_filename") or pbix_path.name)
    stem = Path(original_filename).stem or "model"
    pbix_path_str = str(pbix_path)

    patcher = PBIXPatcher(pbix_path_str)
    try:
        patcher.extract()

        batch_measures = [{**m, "overwrite": True} for m in measures]
        patcher.batch_add_measures(batch_measures)
        output_path = patcher.repackage(str(upload_dir() / f"{pbix_id}_patched.pbix"))

        @after_this_request
        def _remove_patched_file(response):  # noqa: ANN001
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
            return response

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=f"{stem}_patched.pbix",
            mimetype="application/octet-stream",
        )
        response.headers["X-Patch-Method"] = "direct"
        return response

    except Exception as exc:
        error_msg = str(exc)
        logger.info(
            "[patch] Direct patching failed pbix_id=%s: %s — falling back to Tabular Editor script",
            pbix_id,
            error_msg,
        )
        try:
            return _patch_measures_tabular_script_response(pbix_path_str, measures, original_filename)
        except Exception as fallback_err:
            logger.exception("[patch] script fallback failed pbix_id=%s", pbix_id)
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Both patching and script generation failed: {fallback_err}",
                    }
                ),
                500,
            )
    finally:
        try:
            patcher.cleanup()
        except Exception:
            pass


@app.post("/api/pbix/open-uploaded")
def api_pbix_open_uploaded():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "Missing file field."}), 400

    filename = (upload.filename or "").strip()
    if not filename.lower().endswith(".pbix"):
        return jsonify({"ok": False, "error": "Only .pbix files are accepted."}), 400

    safe_name = os.path.basename(filename) or "uploaded.pbix"
    stamp = int(time.time())
    target_path = Path(tempfile.gettempdir()) / f"pbix_story_open_{stamp}_{safe_name}"

    try:
        upload.save(str(target_path))
        opened, launch_message = _launch_power_bi_desktop(target_path)
        status = 200 if opened else 500
        return (
            jsonify(
                {
                    "ok": opened,
                    "opened": opened,
                    "file_name": safe_name,
                    "pbix_path": str(target_path),
                    "launch_message": launch_message,
                }
            ),
            status,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to open uploaded PBIX: {exc}"}), 500


@app.post("/documentation/generate-pdf")
def api_documentation_generate_pdf():
    body = request.get_json(silent=True) or {}
    filename = str(body.get("filename") or "").strip()
    context_payload: dict[str, Any] = {}
    cache_key = filename.lower() if filename else ""

    cached_payload: dict[str, Any] = {}
    if cache_key and cache_key in _DOC_CONTEXT_CACHE:
        cached_payload = dict(_DOC_CONTEXT_CACHE[cache_key])
    elif _DOC_CONTEXT_LAST_KEY in _DOC_CONTEXT_CACHE:
        cached_payload = dict(_DOC_CONTEXT_CACHE[_DOC_CONTEXT_LAST_KEY])

    if any(
        key in body
        for key in (
            "sources",
            "tables",
            "relationships",
            "measures",
            "calculated_columns",
            "rls",
            "parameters",
            "schema",
            "power_query",
            "columns",
            "table_roles",
        )
    ):
        body_power_query = _ensure_list(body.get("power_query"))
        context_payload = {
            "filename": filename or "PowerBI_Documentation.pbix",
            "sources": body.get("sources") or [],
            "tables": body.get("tables") or [],
            "relationships": body.get("relationships") or [],
            "measures": body.get("measures") or [],
            "calculated_columns": body.get("calculated_columns") or [],
            "rls": body.get("rls") or [],
            "parameters": body.get("parameters") or [],
            "schema": body.get("schema") or [],
            "power_query": body_power_query or _ensure_list(cached_payload.get("power_query")),
            "columns": body.get("columns") or {},
            "table_roles": body.get("table_roles") or [],
        }
    elif cached_payload:
        context_payload = cached_payload

    if not context_payload:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No cached documentation context found. Upload a .pbix file first before generating the PDF.",
                }
            ),
            400,
        )

    if filename:
        context_payload["filename"] = filename

    def stream() -> Iterator[str]:
        try:
            full_context = dict(context_payload)

            yield f"data: {json.dumps({'type': 'progress', 'step': 'prepare', 'message': 'Préparation des données du modèle...'})}\n\n"
            ctx = prepare_pdf_context(full_context)
            yield f"data: {json.dumps({'type': 'step_done', 'step': 'prepare'})}\n\n"

            yield f"data: {json.dumps({'type': 'progress', 'step': 'enriching', 'message': 'Descriptions métier et analyse IA...'})}\n\n"
            ctx["enrichment"] = build_deterministic_enrichment(ctx)
            llm = enrich_documentation_json(ctx)
            yield f"data: {json.dumps({'type': 'step_done', 'step': 'enriching'})}\n\n"

            yield f"data: {json.dumps({'type': 'progress', 'step': 'pdf', 'message': 'Génération du PDF ReportLab...'})}\n\n"
            doc = assemble_reportlab_document(ctx, llm)
            pdf_bytes = build_reportlab_pdf(doc)
            output_name = f"{os.path.splitext(os.path.basename(str(ctx.get('filename') or 'doc.pbix')))[0]}_documentation.pdf"

            pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
            yield f"data: {json.dumps({'type': 'complete', 'pdf_base64': pdf_b64, 'filename': output_name})}\n\n"
        except requests.exceptions.Timeout:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Ollama timed out. Make sure Ollama is running and the model is loaded.'})}\n\n"
        except Exception as exc:
            logger.error("[documentation-pdf] SSE generation failed: %s\n%s", exc, traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': f'PDF generation failed: {exc}'})}\n\n"

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


DAX_SYSTEM_PROMPT = """You are an expert Power BI DAX developer.
Given a natural language request, respond with exactly 3 sections:

## DAX Measure
Write a complete, production-ready DAX measure.
Use proper formatting with line breaks and indentation.
Include a meaningful measure name.

## Logic Explanation
Explain clearly how the DAX works:
- What each function does
- How filter context is handled
- Why you chose this approach

## Suggested Improvements
Provide 3-5 concrete suggestions:
- Performance optimizations
- Edge case handling
- Alternative approaches
- Related measures they might need

Be concise but thorough. Format code blocks with backticks.Every section MUST have at least 3 bullet points. No exceptions.
"""


def iter_ollama_chat_stream(
    model: str,
    system: str,
    user: str,
    *,
    request_id: str = "",
    temperature: float | None = None,
) -> Iterator[str]:
    rid = f" req_id={request_id}" if request_id else ""
    lock_timeout_sec = float(os.environ.get("OLLAMA_SINGLEFLIGHT_WAIT_SEC", "1.0"))
    got_lock = _ollama_singleflight_lock.acquire(timeout=lock_timeout_sec)
    if not got_lock:
        raise RuntimeError(
            "Ollama is busy with another generation request. "
            "Wait for the current run to finish or click Stop, then try again."
        )

    base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    url = f"{base}/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
    }
    if temperature is not None:
        payload["options"] = {"temperature": float(temperature)}
    num_ctx_raw = os.environ.get("DAX_OLLAMA_NUM_CTX", "").strip()
    if num_ctx_raw:
        try:
            payload.setdefault("options", {})
            payload["options"]["num_ctx"] = int(num_ctx_raw)
            _log_flush("[dax]%s ollama options num_ctx=%s", rid, num_ctx_raw)
        except ValueError:
            logger.warning("[dax]%s invalid DAX_OLLAMA_NUM_CTX=%r ignored", rid, num_ctx_raw)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    _log_flush(
        "[dax]%s ollama POST %s model=%s payload_bytes=%d system_chars=%d user_chars=%d",
        rid,
        url,
        model,
        len(data),
        len(system),
        len(user),
    )
    _log_flush(
        "[dax]%s blocking on urllib.urlopen() — try: `ollama ps`, `ollama run %s` to warm.",
        rid,
        model,
    )
    t_connect_start = time.perf_counter()
    hb_sec = float(os.environ.get("DAX_URLOPEN_HEARTBEAT_SEC", "3"))
    timeout_sec = float(os.environ.get("DAX_OLLAMA_READ_TIMEOUT_SEC", os.environ.get("DAX_OLLAMA_TIMEOUT_SEC", "600")))
    stop_hb = threading.Event()

    def _urlopen_heartbeat() -> None:
        while not stop_hb.wait(hb_sec):
            elapsed = time.perf_counter() - t_connect_start
            _log_flush(
                "[dax]%s still inside urlopen after %.1fs — check `ollama ps` / GPU.",
                rid,
                elapsed,
            )

    try:
        hb_thread = threading.Thread(target=_urlopen_heartbeat, name="dax-urlopen-hb", daemon=True)
        hb_thread.start()
        try:
            resp = urllib.request.urlopen(req, timeout=timeout_sec)
        except Exception as exc:
            logger.error("[dax]%s urlopen failed after %.1f ms: %s", rid, (time.perf_counter() - t_connect_start) * 1000, exc)
            raise RuntimeError(_friendly_ollama_error(exc, base, model=model)) from exc
        finally:
            stop_hb.set()
        connect_ms = (time.perf_counter() - t_connect_start) * 1000
        status = getattr(resp, "status", None)
        logger.info("[dax]%s ollama ready status=%s connect_ms=%.1f", rid, status, connect_ms)

        line_num = 0
        json_ok = 0
        json_bad = 0
        empty_lines = 0
        skipped_no_content = 0
        content_yields = 0
        total_chars = 0
        last_heartbeat = time.perf_counter()
        slow_readline_log_ms = float(os.environ.get("DAX_SLOW_READLINE_MS", "3000"))

        with resp:
            while True:
                t_block = time.perf_counter()
                raw = resp.readline()
                block_ms = (time.perf_counter() - t_block) * 1000
                line_num += 1

                if line_num == 1:
                    logger.info("[dax]%s first readline: blocked_ms=%.1f bytes=%d", rid, block_ms, len(raw))
                elif block_ms >= slow_readline_log_ms:
                    logger.warning("[dax]%s slow readline line=%d blocked_ms=%.1f", rid, line_num, block_ms)

                if not raw:
                    logger.info(
                        "[dax]%s readline eof after %d lines yields=%d chars=%d", rid, line_num, content_yields, total_chars
                    )
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    empty_lines += 1
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    json_bad += 1
                    continue

                json_ok += 1

                if obj.get("done"):
                    logger.info(
                        "[dax]%s ollama done=true lines=%d yields=%d chars=%d", rid, line_num, content_yields, total_chars
                    )
                    break

                msg = obj.get("message") or {}
                piece = msg.get("content") or ""
                if not piece:
                    piece = obj.get("response") or ""
                if not piece:
                    skipped_no_content += 1
                    continue

                content_yields += 1
                total_chars += len(piece)
                if content_yields == 1:
                    logger.info("[dax]%s first token delta chars=%d", rid, len(piece))

                now = time.perf_counter()
                if now - last_heartbeat >= 5.0:
                    logger.info("[dax]%s heartbeat lines=%d yields=%d chars=%d", rid, line_num, content_yields, total_chars)
                    last_heartbeat = now

                yield piece

        logger.info("[dax]%s stream finished lines=%d yields=%d chars=%d", rid, line_num, content_yields, total_chars)
    finally:
        _ollama_singleflight_lock.release()


@app.post("/api/dax/generate")
def api_dax_generate():
    req_id = str(uuid.uuid4())[:8]
    t0 = time.perf_counter()
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    context = (body.get("context") or "").strip()
    pbix_context = (body.get("pbix_context") or "").strip()
    model = (body.get("model") or os.environ.get("OLLAMA_MODEL", "llama3.2:3b")).strip()
    logger.info(
        "[dax] req_id=%s begin query_len=%d context_len=%d pbix_context_len=%d model=%s",
        req_id,
        len(query),
        len(context),
        len(pbix_context),
        model,
    )
    if not query:
        return jsonify({"ok": False, "error": "query is required"}), 400

    max_user_ctx = int(os.environ.get("DAX_MAX_USER_CONTEXT_CHARS", "4000"))
    max_pbix_ctx = int(os.environ.get("DAX_MAX_PBIX_CONTEXT_CHARS", "8000"))
    if context:
        context = _truncate_dax_text(context, max_user_ctx, "user_context (textarea)", req_id)
    if pbix_context:
        pbix_context = _truncate_dax_text(pbix_context, max_pbix_ctx, "pbix_context", req_id)

    user_content = f"Natural language request:\n{query}\n"
    if context:
        user_content += f"\nOptional table/column context (user notes):\n{context}\n"

    system_prompt = DAX_SYSTEM_PROMPT
    if pbix_context:
        system_prompt += f"""

You have access to the user's actual Power BI data model:
{pbix_context}

Use the EXACT table names and column names from this model
in your generated DAX. Do not invent table or column names.
Reference real relationships when using RELATED or USERELATIONSHIP.
"""

    def generate() -> Iterator[str]:
        yield f"data: {json.dumps({'type': 'start', 'req_id': req_id})}\n\n"
        sse_chunks = 0
        try:
            for piece in iter_ollama_chat_stream(model, system_prompt, user_content, request_id=req_id):
                sse_chunks += 1
                yield f"data: {json.dumps({'type': 'chunk', 'text': piece})}\n\n"
        except Exception as exc:
            logger.error("[dax] req_id=%s stream error: %s\n%s", req_id, exc, traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc), 'req_id': req_id})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'req_id': req_id})}\n\n"
        logger.info("[dax] req_id=%s sse complete elapsed_ms=%.1f", req_id, (time.perf_counter() - t0) * 1000)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


STORY_RULES = """You are a senior Power BI analytics storyteller.
You will receive extracted metadata from a .pbix file. Your job is to produce a concise, grounded narrative for a business stakeholder.

ANALYSIS APPROACH:
- Identify fact tables vs dimension/lookup tables by row count patterns
- Flag high-cardinality columns relative to table size
- Note tables with no relationships or orphan columns
- Look for naming inconsistencies suggesting modeling issues
- Infer the business domain from table and column names

ANALYSIS DEPTH:
- For each large table (highest row counts), describe its likely role (fact vs dimension), its key columns, and any quality signals
- For each relationship detected, note whether it follows standard star-schema patterns or something unusual
- For each column with notably high cardinality, assess whether this is expected or a potential grain issue
- If measures or calculated columns are present, briefly describe what each appears to compute and which tables it depends on

OUTPUT FORMAT (strict):
Use exactly these markdown headings:
# Overview
# Key Insights
# Risks or Data Quality Concerns
# Recommended Actions

RULES:
- 3-6 bullet points per section
- Every bullet must cite at least one table or column name from context
- Do not invent metrics, percentages, or row counts not present in context
- If uncertain about something, say "likely" or "suggests" — do not state as fact
- Recommended Actions must reference which insight or risk they address
- Be specific enough that a developer could act on each bullet without asking followup questions

SECTION PURPOSES:
- Overview: business domain, model scale, structural summary
- Key Insights: non-obvious patterns a stakeholder should know
- Risks: concrete data quality or modeling problems found in context
- Recommended Actions: specific next steps tied to identified risks/insights"""


@app.post("/api/story/generate")
def api_story_generate():
    req_id = str(uuid.uuid4())[:8]
    t0 = time.perf_counter()
    body = request.get_json(silent=True) or {}

    context = body.get("context")
    model = (body.get("model") or os.environ.get("OLLAMA_MODEL", "llama3.2:3b")).strip()
    focus = (body.get("focus") or "").strip()
    logger.info("[story] req_id=%s begin model=%s focus_len=%d", req_id, model, len(focus))

    if context is None:
        return jsonify({"ok": False, "error": "context is required"}), 400

    focus_check_text = ""
    if isinstance(context, str):
        context_text = context.strip()
        focus_check_text = context_text
    else:
        if isinstance(context, dict):
            focus_check_text = json.dumps(context, ensure_ascii=True)
            context = compact_story_context_for_prompt(context)
        try:
            context_text = json.dumps(context, ensure_ascii=True)
        except Exception:
            context_text = str(context)
        context_text = context_text.strip()

    if not context_text:
        return jsonify({"ok": False, "error": "context must not be empty"}), 400

    max_ctx_chars = int(os.environ.get("STORY_MAX_CONTEXT_CHARS", "6000"))
    if max_ctx_chars > 0 and len(context_text) > max_ctx_chars:
        logger.warning(
            "[story] req_id=%s truncating context %d -> %d chars",
            req_id,
            len(context_text),
            max_ctx_chars,
        )
        context_text = context_text[:max_ctx_chars] + "\n\n[... truncated by server ...]"

    # Validate focus against full client context (before compacting for Ollama).
    if focus and not focus_matches_context(focus_check_text or context_text, focus):
        return (
            jsonify(
                {
                    "ok": False,
                    "error_type": "invalid_focus",
                    "error": (
                        f"The focus area '{focus}' is not related to this Power BI model. "
                        "Try using table names, column names, or business concepts from your data."
                    ),
                }
            ),
            400,
        )

    story_temp = float(os.environ.get("STORY_OLLAMA_TEMPERATURE", "0.2"))
    system_prompt = STORY_RULES
    if focus:
        user_content = (
            f"Power BI model metadata (JSON):\n{context_text}\n\n"
            f"Analyze this model with emphasis on: {focus}. "
            "At least half of Key Insights and Recommended Actions should relate to that focus. "
            "Still cover overall model health in Overview and Risks."
        )
    else:
        user_content = (
            f"Power BI model metadata (JSON):\n{context_text}\n\n"
            "Identify the most important structural patterns, data quality risks, and actionable "
            "recommendations. Write for a business stakeholder who has not seen this model before."
        )

    def generate() -> Iterator[str]:
        yield f"data: {json.dumps({'type': 'start', 'req_id': req_id})}\n\n"
        try:
            for piece in iter_ollama_chat_stream(
                model,
                system_prompt,
                user_content,
                request_id=req_id,
                temperature=story_temp,
            ):
                yield f"data: {json.dumps({'type': 'chunk', 'text': piece})}\n\n"
        except Exception as exc:
            logger.error("[story] req_id=%s stream error: %s\n%s", req_id, exc, traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc), 'req_id': req_id})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'req_id': req_id})}\n\n"
        logger.info("[story] req_id=%s sse complete elapsed_ms=%.1f", req_id, (time.perf_counter() - t0) * 1000)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/ollama/models")
def api_ollama_models():
    base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    url = f"{base}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            body = response.read().decode("utf-8")
        return app.response_class(body, status=200, mimetype="application/json")
    except Exception as exc:
        return jsonify({"ok": False, "error": _friendly_ollama_error(exc, base)}), 503


@app.post("/analyze")
def analyze():
    pbix_path = (request.form.get("pbix_path") or "").strip()
    if not pbix_path:
        return render_template("dashboard.html", error="PBIX path is required.", default_path="")

    resolved = os.path.expanduser(pbix_path)
    if not os.path.exists(resolved):
        return render_template("dashboard.html", error=f"PBIX file not found: {resolved}", default_path=pbix_path)

    try:
        payload = extract_pbix_payload(resolved)
        tables = payload.get("tables", [])
        stats_rows = payload.get("stats_preview", [])
        summary = payload.get("summary", {})
        return render_template(
            "dashboard.html",
            default_path=resolved,
            file_name=os.path.basename(resolved),
            tables=tables,
            stats_rows=stats_rows[:100],
            summary=summary,
            pbix_path=resolved,
        )
    except Exception as exc:
        return render_template("dashboard.html", error=f"Failed to analyze PBIX: {exc}", default_path=pbix_path)


if __name__ == "__main__":
    port = int(os.environ.get("PBIX_DASHBOARD_PORT", "5052"))
    app.run(host="127.0.0.1", port=port, debug=True)
