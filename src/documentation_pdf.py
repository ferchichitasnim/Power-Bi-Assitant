#!/usr/bin/env python3
"""Power BI documentation PDF: data preparation, LLM enrichment, ReportLab rendering."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

from documentation_reportlab import SECTION_BUILDERS, build_reportlab_pdf

DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
LLM_TIMEOUT_SEC = 360
LLM_TEMPERATURE = 0.3


def _pdf_ollama_model() -> str:
    """Model for documentation PDF enrichment (DOC_PDF_OLLAMA_MODEL overrides default)."""
    explicit = str(os.environ.get("DOC_PDF_OLLAMA_MODEL") or "").strip()
    if explicit:
        return explicit
    return DEFAULT_MODEL


CARDINALITY_MAP = {0: "None", 1: "One", 2: "Many"}
DIRECTION_MAP = {1: "Single", 2: "Both", 3: "Automatic"}

MEASURE_DOMAIN_RULES: list[tuple[str, list[str]]] = [
    (
        "Performance Commerciale",
        ["vente", "sale", "revenue", "ca ", "aov", "chiffre", "order", "commande", "vr_", "cmv"],
    ),
    (
        "Marges et Rentabilité",
        ["marge", "margin", "revenu", "cost", "cout", "coût", "benef", "bénéf", "mbm", "profit"],
    ),
    (
        "Efficacité Opérationnelle",
        ["effic", "etd", "elc", "livraison", "transport", "stock", "backorder", "tb_", "ctv", "tv_"],
    ),
    (
        "Pipeline et Prospection",
        ["prospect", "pipeline", "cotation", "ptv", "rc_", "devis", "conversion"],
    ),
]

TOC_CATALOG = [
    ("overview", "Vue d'Ensemble du Modèle", "Schéma, KPIs et résumé exécutif"),
    ("sources", "Sources de Données", "Connecteurs, serveurs et bases de données"),
    ("tables", "Tables et Schéma", "Structure des tables, colonnes et types de données"),
    ("relationships", "Relations", "Liens entre tables, cardinalité et filtrage croisé"),
    ("measures", "Mesures DAX", "Formules de calcul et logique métier"),
    ("calculated_columns", "Colonnes Calculées DAX", "Colonnes dérivées et transformations"),
    ("rls", "Sécurité (RLS)", "Row-Level Security et rôles d'accès"),
    ("audit", "Audit et Recommandations", "Forces, risques et axes d'amélioration"),
]


def _is_internal_table(name: str) -> bool:
    lower = str(name or "").strip().lower().lstrip(" '\"`[$")
    return "localdatetable_" in lower or "datetabletemplate_" in lower


def _to_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return val
    if val is None:
        return []
    if hasattr(val, "tolist"):
        try:
            return val.tolist()
        except Exception:
            pass
    if hasattr(val, "to_dict"):
        try:
            return val.to_dict(orient="records")
        except Exception:
            pass
    if hasattr(val, "__iter__") and not isinstance(val, (str, dict)):
        try:
            return list(val)
        except Exception:
            pass
    return []


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, default)).strip()
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


def _trim_string(value: Any, max_chars: int = 220) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _safe_json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, indent=2)


def _readable_enum(value: Any, mapping: dict[int, str], default: str = "—") -> str:
    if value is None or value == "":
        return default
    try:
        return mapping.get(int(value), str(value))
    except (ValueError, TypeError):
        return str(value)


def _table_name(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("Name", "name", "TableName", "tableName", "table"):
            if item.get(key):
                return str(item[key]).strip()
        return ""
    return str(item or "").strip()


def _call_ollama_generate(prompt: str, model: str, base_url: str) -> str:
    url = f"{base_url.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": LLM_TEMPERATURE, "num_predict": 4000},
    }
    timeout_sec = _env_int("DOC_PDF_OLLAMA_TIMEOUT_SEC", LLM_TIMEOUT_SEC)
    response = requests.post(url, json=payload, timeout=timeout_sec)
    if response.status_code >= 400:
        raise RuntimeError(f"Ollama generation failed ({response.status_code}): {response.text[:500]}")
    text = str(response.json().get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return text


def _parse_llm_json(raw: str) -> dict[str, Any]:
    value = str(raw or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```\s*$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", value)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _rel_table_names(row: dict[str, Any]) -> tuple[str, str]:
    fr = ""
    to = ""
    for k in ("FromTableName", "fromTableName", "from_table", "from", "From"):
        if row.get(k):
            fr = str(row[k]).split("[")[0].strip()
            break
    if not fr and row.get("from"):
        fr = str(row["from"]).split("[")[0].strip()
    for k in ("ToTableName", "toTableName", "to_table", "to", "To"):
        if row.get(k):
            to = str(row[k]).split("[")[0].strip()
            break
    if not to and row.get("to"):
        to = str(row["to"]).split("[")[0].strip()
    return fr, to


def classify_business_tables(
    business_tables: list[str],
    relationships: list[dict[str, Any]],
    table_roles: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Classify tables as fact or dimension. dim_* is always a dimension; fact_* is always a fact."""
    role_map: dict[str, str] = {}
    if isinstance(table_roles, list):
        for item in table_roles:
            if isinstance(item, dict):
                tname = str(item.get("table") or "").strip()
                role = str(item.get("role") or "").strip().lower()
                if tname and role in ("fact", "dimension"):
                    role_map[tname] = role

    from_tables: set[str] = set()
    to_tables: set[str] = set()
    for row in relationships:
        if not isinstance(row, dict):
            continue
        fr, to = _rel_table_names(row)
        if fr and not _is_internal_table(fr):
            from_tables.add(fr)
        if to and not _is_internal_table(to):
            to_tables.add(to)

    facts: list[str] = []
    dims: list[str] = []
    for table in business_tables:
        if table in role_map:
            (facts if role_map[table] == "fact" else dims).append(table)
            continue
        lower = table.lower()
        if lower.startswith(("dim", "d_")) or "date" in lower or "calendar" in lower:
            dims.append(table)
        elif lower.startswith(("fact", "f_")):
            facts.append(table)
        else:
            is_from = table in from_tables
            is_to = table in to_tables
            if is_from and not is_to:
                facts.append(table)
            else:
                dims.append(table)

    for table in business_tables:
        if table not in facts and table not in dims:
            dims.append(table)
    return sorted(facts), sorted(dims)


def detect_schema_type(
    business_tables: list[str],
    relationships: list[dict[str, Any]],
    fact_tables: list[str],
    dimension_tables: list[str],
) -> dict[str, Any]:
    if not relationships and not business_tables:
        return {
            "schema_type": "Inconnu",
            "fact_tables": fact_tables,
            "dimension_tables": dimension_tables,
            "explanation": "Aucune relation détectée.",
        }

    dim_set = set(dimension_tables)
    fact_set = set(fact_tables)
    dim_to_dim = 0
    for row in relationships:
        if not isinstance(row, dict):
            continue
        fr, to = _rel_table_names(row)
        if fr in dim_set and to in dim_set:
            dim_to_dim += 1

    if len(fact_tables) > 1:
        schema_type = "Constellation"
        explanation = (
            f"Constellation : {len(fact_tables)} tables de faits "
            f"({', '.join(fact_tables[:4])}) partageant "
            f"{len(dimension_tables)} dimension(s) communes."
        )
    elif dim_to_dim > 0:
        schema_type = "Snowflake"
        explanation = f"Schéma en flocon : {dim_to_dim} lien(s) entre dimensions " "(sous-dimensions en chaîne)."
    elif len(fact_tables) == 1:
        schema_type = "Étoile"
        explanation = f"Schéma en étoile : {fact_tables[0]} (faits) reliée à " f"{len(dimension_tables)} dimension(s)."
    else:
        schema_type = "Inconnu"
        explanation = "Structure relationnelle non classifiable automatiquement."

    return {
        "schema_type": schema_type,
        "fact_tables": fact_tables,
        "dimension_tables": dimension_tables,
        "explanation": explanation,
    }


def _columns_by_table(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Build column lists per table. model.schema (TableName/ColumnName) is the primary source."""
    out: dict[str, list[str]] = {}

    for row in _to_list(payload.get("schema")):
        if not isinstance(row, dict):
            continue
        tname = str(row.get("TableName") or row.get("tableName") or "").strip()
        cname = str(row.get("ColumnName") or row.get("columnName") or "").strip()
        if tname and cname and not _is_internal_table(tname):
            out.setdefault(tname, []).append(cname)

    for key in ("statistics",):
        for row in _to_list(payload.get(key)):
            if not isinstance(row, dict):
                continue
            tname = str(row.get("TableName") or row.get("tableName") or "").strip()
            cname = str(row.get("ColumnName") or row.get("columnName") or "").strip()
            if tname and cname and not _is_internal_table(tname):
                out.setdefault(tname, []).append(cname)

    columns_payload = payload.get("columns")
    if isinstance(columns_payload, dict):
        for table, cols in columns_payload.items():
            if _is_internal_table(str(table)):
                continue
            if isinstance(cols, list):
                for c in cols:
                    if isinstance(c, dict):
                        out.setdefault(str(table), []).append(str(c.get("Name") or c.get("name") or c.get("ColumnName") or ""))
                    else:
                        out.setdefault(str(table), []).append(str(c))

    for table_item in _to_list(payload.get("tables")):
        if not isinstance(table_item, dict):
            continue
        tname = _table_name(table_item)
        if not tname:
            continue
        raw_cols = table_item.get("columns") or table_item.get("Columns") or []
        if isinstance(raw_cols, list):
            for c in raw_cols:
                if isinstance(c, dict):
                    out.setdefault(tname, []).append(str(c.get("Name") or c.get("name") or ""))
                else:
                    out.setdefault(tname, []).append(str(c))

    for t in list(out.keys()):
        out[t] = list(dict.fromkeys([c for c in out[t] if c]))
    return out


def _format_main_columns(col_names: list[str], max_cols: int = 8) -> str:
    if not col_names:
        return "—"
    priority: list[str] = []
    rest: list[str] = []
    for c in col_names:
        cl = c.lower()
        if any(k in cl for k in ("id", "name", "date", "amount", "total", "margin", "state", "ref", "user")):
            priority.append(c)
        else:
            rest.append(c)
    ordered = priority + [c for c in rest if c not in priority]
    shown = ordered[:max_cols]
    text = ", ".join(shown)
    if len(col_names) > max_cols:
        text += " ..."
    return text


def _format_cardinality(row: dict[str, Any]) -> str:
    card = row.get("cardinality") or row.get("Cardinality")
    if isinstance(card, str) and card.strip():
        parts = [p.strip() for p in card.split(":")]
        if len(parts) == 2:

            def _side(label: str) -> str:
                u = label.upper()
                if u in ("MANY", "M"):
                    return "M"
                if u in ("ONE", "1"):
                    return "1"
                return label[:1].upper()

            return f"{_side(parts[0])}:{_side(parts[1])}"
        return card.strip()

    from_card = row.get("FromCardinality")
    to_card = row.get("ToCardinality")
    if from_card is not None or to_card is not None:
        left = _readable_enum(from_card, CARDINALITY_MAP, "?")[0]
        right = _readable_enum(to_card, CARDINALITY_MAP, "?")[0]
        return f"{left}:{right}"

    legacy = row.get("Cardinality")
    if legacy is not None:
        label = _readable_enum(legacy, CARDINALITY_MAP, "?")
        return label[0] + ":1" if label != "?" else "—"
    return "—"


def _is_mm_relationship(card_label: str) -> bool:
    c = str(card_label or "").upper().replace(" ", "")
    return c in ("M:M", "MM", "MANY:MANY", "MANY:MANY") or "M:M" in c


def _simplify_dax(expression: str) -> str:
    expr = re.sub(r"\s+", " ", str(expression or "").strip())
    if not expr:
        return "—"
    if len(expr) <= 80:
        return expr
    funcs = re.findall(r"\b([A-Z][A-Z0-9_]*)\s*\(", expr)
    seen: list[str] = []
    for fn in funcs:
        if fn not in seen and fn not in ("IF", "RETURN", "VAR"):
            seen.append(fn)
        if len(seen) >= 4:
            break
    if seen:
        return " + ".join(seen[:4])
    return _trim_string(expr, 80)


def _guess_measure_domain(name: str) -> str:
    lower = str(name or "").lower()
    for domain, keywords in MEASURE_DOMAIN_RULES:
        if any(k in lower for k in keywords):
            return domain
    return "Autres Mesures"


def _normalize_m_code(text: str) -> str:
    """Normalize M code for connector string matching (escaped newlines, backslashes)."""
    value = str(text or "")
    value = value.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    value = value.replace("\\.", ".")
    value = value.replace("\\\\", "\\")
    return value


def _power_query_to_records(power_query_data: Any) -> list[dict[str, Any]]:
    """Accept list[dict], DataFrame-like, or dict and return power_query records."""
    if power_query_data is None:
        return []

    if hasattr(power_query_data, "to_dict"):
        try:
            records = power_query_data.to_dict(orient="records")
            if isinstance(records, list):
                return [r for r in records if isinstance(r, dict)]
        except Exception:
            pass

    if isinstance(power_query_data, dict):
        if "Expression" in power_query_data or "expression" in power_query_data:
            return [power_query_data]
        return []

    rows = _to_list(power_query_data)
    out: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"Expression": item})
    return out


def _collect_m_expressions(
    power_query_data: Any,
    sources_raw: list[Any] | None = None,
) -> list[str]:
    """Collect M code strings from model.power_query Expression column."""
    expressions: list[str] = []
    for row in _power_query_to_records(power_query_data):
        for key in ("Expression", "expression", "Formula", "M", "Query"):
            val = row.get(key)
            if val is not None and str(val).strip() and str(val).lower() not in ("nan", "none"):
                expressions.append(str(val))
                break

    for item in sources_raw or []:
        text = str(item or "").strip()
        if text and (".Database" in text or "Database(" in text or "let " in text.lower()):
            expressions.append(text)

    return expressions


def detect_source_type(m_expressions: list[str]) -> str:
    """Detect data source type using simple substring checks (robust vs regex/escaping)."""
    all_m_code = _normalize_m_code(" ".join(str(e) for e in m_expressions if str(e).strip()))
    logger.info("DEBUG M expressions (first 300 chars): %s", all_m_code[:300])
    print(f"DEBUG M expressions: {all_m_code[:200]}")

    if not all_m_code.strip():
        return "Inconnu"

    code_upper = all_m_code.replace(" ", "")
    checks = [
        ("PostgreSQL.Database", "PostgreSQL"),
        ("PostgreSQL\\.Database", "PostgreSQL"),
        ("Sql.Database", "SQL Server"),
        ("Sql.Databases", "SQL Server"),
        ("Oracle.Database", "Oracle"),
        ("MySQL.Database", "MySQL"),
        ("Odbc.DataSource", "ODBC"),
        ("OData.Feed", "OData"),
        ("Excel.Workbook", "Excel"),
        ("Csv.Document", "CSV"),
        ("SharePoint.", "SharePoint"),
        ("GoogleBigQuery", "BigQuery"),
        ("Snowflake.", "Snowflake"),
        ("AmazonRedshift", "Redshift"),
    ]
    for needle, label in checks:
        if needle in code_upper or needle in all_m_code:
            logger.info("[pdf-source] matched connector substring: %s => %s", needle, label)
            return label

    return "Inconnu"


def format_source_label(label: str, connector_type: str) -> str:
    """Prefix a source endpoint label with connector type (e.g. PostgreSQL — host (database: db))."""
    text = str(label or "").strip()
    ctype = str(connector_type or "").strip()
    if not text:
        return "—"
    if not ctype or ctype == "Inconnu":
        return text
    for sep in (" — ", " - "):
        if text.startswith(f"{ctype}{sep}"):
            return text
    return f"{ctype} — {text}"


def enrich_source_labels(sources: list[str], power_query_data: Any) -> list[str]:
    """Add connector type prefix to source labels using Power Query M detection."""
    connector = detect_source_type(_collect_m_expressions(power_query_data))
    return [format_source_label(s, connector) for s in sources]


def _extract_server_database(all_m_code: str, source_type: str) -> tuple[str, str]:
    """Extract server and database from M connector call."""
    normalized = _normalize_m_code(all_m_code)
    generic = re.search(
        r'\.Database\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']',
        normalized,
        flags=re.IGNORECASE,
    )
    if generic:
        return generic.group(1).strip(), generic.group(2).strip()
    return "—", "—"


def _extract_source_rows_from_power_query(
    power_query_data: Any,
    sources_raw: list[Any] | None = None,
) -> list[list[str]]:
    """Parse M expressions for connector type, server, and database."""
    print(f"DEBUG _extract_source_rows_from_power_query called, pq type={type(power_query_data).__name__}")
    records = _power_query_to_records(power_query_data)
    logger.info("[pdf-source] power_query records count: %d", len(records))

    expressions = _collect_m_expressions(power_query_data, sources_raw)
    if expressions:
        for idx, expr in enumerate(expressions[:3]):
            logger.info(
                "[pdf-source] M expression[%d] (len=%d): %s",
                idx,
                len(expr),
                expr[:800] + ("..." if len(expr) > 800 else ""),
            )
    else:
        logger.warning(
            "[pdf-source] No M expressions found (records=%d, raw_type=%s)",
            len(records),
            type(power_query_data).__name__,
        )

    source_type = detect_source_type(expressions)
    logger.info("[pdf-source] detect_source_type => %s", source_type)

    if source_type == "Inconnu":
        return []

    all_m_code = _normalize_m_code(" ".join(expressions))
    server, database = _extract_server_database(all_m_code, source_type)
    logger.info("[pdf-source] server=%s database=%s", server, database)
    return [[source_type, server, database, "Import", "Actif"]]


def _parse_source_label(source: str) -> list[str]:
    """Return [Type, Server, Database, Mode, Status] from a source string (fallback)."""
    raw = str(source or "").strip()
    if not raw:
        return ["Inconnu", "—", "—", "Import", "Actif"]

    lower = raw.lower()
    if "postgres" in lower:
        stype = "PostgreSQL"
    elif "oracle" in lower:
        stype = "Oracle"
    elif "mysql" in lower:
        stype = "MySQL"
    elif "snowflake" in lower:
        stype = "Snowflake"
    elif "sql server" in lower or re.search(r"\bsql\b", lower):
        stype = "SQL Server"
    elif raw.startswith("http"):
        stype = "Web / API"
    elif any(ext in lower for ext in (".xlsx", ".csv", ".parquet")):
        stype = "Fichier"
    elif "sharepoint" in lower:
        stype = "SharePoint"
    else:
        detected = detect_source_type([raw])
        stype = detected if detected != "Inconnu" else "Inconnu"

    server = "—"
    database = "—"
    mode = "Import"

    db_match = re.search(r"\(database:\s*([^)]+)\)", raw, flags=re.IGNORECASE)
    if db_match:
        database = db_match.group(1).strip()
        server = re.sub(r"\s*\(database:.*\)", "", raw, flags=re.IGNORECASE).strip() or "—"
    elif " / " in raw:
        parts = [p.strip() for p in raw.split(" / ", 1)]
        server, database = parts[0], parts[1] if len(parts) > 1 else "—"
    elif "—" in raw:
        parts = [p.strip() for p in raw.split("—", 1)]
        server, database = parts[0], parts[1] if len(parts) > 1 else "—"
    else:
        server = raw

    return [stype, server, database, mode, "Actif"]


def _normalize_relationship_row(row: dict[str, Any]) -> dict[str, Any] | None:
    fr_table = str(row.get("FromTableName") or row.get("fromTable") or "").strip()
    fr_col = str(row.get("FromColumnName") or row.get("fromColumn") or "").strip()
    to_table = str(row.get("ToTableName") or row.get("toTable") or "").strip()
    to_col = str(row.get("ToColumnName") or row.get("toColumn") or "").strip()

    if not fr_table and row.get("from"):
        m = re.match(r"([^[]+)\[([^\]]+)\]", str(row["from"]))
        if m:
            fr_table, fr_col = m.group(1).strip(), m.group(2).strip()
    if not to_table and row.get("to"):
        m = re.match(r"([^[]+)\[([^\]]+)\]", str(row["to"]))
        if m:
            to_table, to_col = m.group(1).strip(), m.group(2).strip()

    if not (fr_table and to_table):
        return None
    if _is_internal_table(fr_table) or _is_internal_table(to_table):
        return None

    card = _format_cardinality(row)
    direction = row.get("direction") or row.get("CrossFilteringBehavior")
    if isinstance(direction, (int, float)):
        direction = _readable_enum(direction, DIRECTION_MAP)
    direction = str(direction or "Single")

    active = row.get("active")
    if active is None:
        active = row.get("IsActive", True)
    active_mark = "✓" if bool(active) else "✗"

    remark_parts: list[str] = []
    if _is_mm_relationship(card):
        remark_parts.append("⚠ M:M")
    if str(direction).lower() == "both":
        remark_parts.append("Filtrage bidirectionnel")
    remark = " + ".join(remark_parts) if remark_parts else "—"

    return {
        "from": f"{fr_table}[{fr_col}]" if fr_col else fr_table,
        "to": f"{to_table}[{to_col}]" if to_col else to_table,
        "cardinality": card,
        "direction": direction,
        "active": active_mark,
        "remark": remark,
        "is_mm": _is_mm_relationship(card),
        "is_both": str(direction).lower() == "both",
    }


def _rls_row_from_dict(item: dict[str, Any]) -> dict[str, str] | None:
    role = str(
        item.get("role") or item.get("Role") or item.get("RoleName") or item.get("name") or item.get("Name") or ""
    ).strip()
    table = str(
        item.get("table")
        or item.get("Table")
        or item.get("TableName")
        or item.get("EntityName")
        or item.get("filtered_table")
        or ""
    ).strip()
    expr = str(
        item.get("expression")
        or item.get("Expression")
        or item.get("filter")
        or item.get("FilterExpression")
        or item.get("Filter")
        or item.get("DaxFilter")
        or item.get("dax")
        or item.get("MemberExpression")
        or ""
    ).strip()
    desc = str(item.get("description") or item.get("Description") or "").strip()
    if not (role or table or expr):
        return None
    if not desc:
        desc = "Filtre de sécurité au niveau des lignes"
    return {
        "role": role or "—",
        "table": table or "—",
        "expression": expr or "—",
        "description": desc,
    }


def _normalize_rls_rows(rls_raw: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    if isinstance(rls_raw, dict):
        if isinstance(rls_raw.get("details"), list):
            for detail in rls_raw["details"]:
                if isinstance(detail, dict) and isinstance(detail.get("entries"), list):
                    for entry in detail["entries"]:
                        if isinstance(entry, dict):
                            parsed = _rls_row_from_dict(entry)
                            if parsed:
                                rows.append(parsed)
                elif isinstance(detail, dict):
                    parsed = _rls_row_from_dict(detail)
                    if parsed:
                        rows.append(parsed)
        parsed_top = _rls_row_from_dict(rls_raw)
        if parsed_top:
            rows.append(parsed_top)

    for item in _to_list(rls_raw):
        if isinstance(item, dict):
            if isinstance(item.get("entries"), list):
                for entry in item["entries"]:
                    if isinstance(entry, dict):
                        parsed = _rls_row_from_dict(entry)
                        if parsed:
                            rows.append(parsed)
                continue
            if isinstance(item.get("details"), list):
                rows.extend(_normalize_rls_rows(item))
                continue
            parsed = _rls_row_from_dict(item)
            if parsed:
                rows.append(parsed)
        elif isinstance(item, str):
            text = item.strip()
            if not text or re.match(r"^[a-z_]+:\s*\d+\s+entr", text, flags=re.IGNORECASE):
                continue
            rows.append(
                {
                    "role": "—",
                    "table": "—",
                    "expression": "—",
                    "description": text,
                }
            )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["role"], row["table"], row["expression"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def prepare_pdf_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Build structured context from raw PBIX documentation payload."""
    filename = str(payload.get("filename") or "PowerBI_Documentation.pbix")
    tables_raw = _to_list(payload.get("tables"))
    business_tables = sorted({_table_name(t) for t in tables_raw if _table_name(t) and not _is_internal_table(_table_name(t))})

    rels_raw = _to_list(payload.get("relationships"))
    business_rels: list[dict[str, Any]] = []
    for row in rels_raw:
        if isinstance(row, dict):
            norm = _normalize_relationship_row(row)
            if norm:
                business_rels.append(norm)

    measures_raw = _to_list(payload.get("measures"))
    measures: list[dict[str, Any]] = []
    for m in measures_raw:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("Name") or m.get("MeasureName") or "").strip()
        if not name:
            continue
        table = str(m.get("table") or m.get("TableName") or "").strip()
        formula = str(
            m.get("formula") or m.get("Expression") or m.get("expression") or m.get("MeasureExpression") or ""
        ).strip()
        measures.append(
            {
                "name": name,
                "table": table,
                "formula": formula,
                "domain": _guess_measure_domain(name),
                "dax_logic": _simplify_dax(formula),
            }
        )

    calc_cols_raw = _to_list(payload.get("calculated_columns"))
    calc_cols: list[dict[str, Any]] = []
    for c in calc_cols_raw:
        if not isinstance(c, dict):
            continue
        table = str(c.get("table") or c.get("TableName") or "").strip()
        name = str(c.get("name") or c.get("ColumnName") or c.get("Name") or "").strip()
        formula = str(c.get("formula") or c.get("Expression") or c.get("expression") or "").strip()
        if table and name and not _is_internal_table(table):
            calc_cols.append(
                {
                    "table": table,
                    "name": name,
                    "formula": formula,
                    "expression_short": _simplify_dax(formula),
                }
            )

    power_query_data = payload.get("power_query")
    if not power_query_data:
        doc = payload.get("documentation") if isinstance(payload.get("documentation"), dict) else {}
        power_query_data = doc.get("power_query")
    sources_raw = _to_list(payload.get("sources"))
    logger.info(
        "[pdf-source] prepare_pdf_context power_query present=%s",
        bool(_power_query_to_records(power_query_data)),
    )
    source_rows = _extract_source_rows_from_power_query(power_query_data, sources_raw)
    if not source_rows:
        for s in sources_raw:
            row = _parse_source_label(str(s))
            if row[0] != "Inconnu":
                source_rows.append(row)
        if not source_rows:
            source_rows = [_parse_source_label(str(s)) for s in sources_raw if str(s).strip()]

    columns_map = _columns_by_table(payload)
    table_roles = _to_list(payload.get("table_roles"))
    fact_names, dim_names = classify_business_tables(business_tables, rels_raw, table_roles)
    schema_info = detect_schema_type(business_tables, business_rels, fact_names, dim_names)

    mm_count = sum(1 for r in business_rels if r.get("is_mm"))

    return {
        "filename": filename,
        "generated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
        "model_name": _pdf_ollama_model(),
        "data_source_label": _format_data_source_label(source_rows, sources_raw),
        "kpis": {
            "tables": len(business_tables),
            "measures": len(measures),
            "calculated_columns": len(calc_cols),
            "relationships": len(business_rels),
        },
        "business_tables": business_tables,
        "columns_map": columns_map,
        "schema_info": schema_info,
        "fact_names": fact_names,
        "dim_names": dim_names,
        "relationships": business_rels,
        "measures": measures,
        "calculated_columns": calc_cols,
        "source_rows": source_rows,
        "sources_raw": sources_raw,
        "rls_rows": _normalize_rls_rows(payload.get("rls")),
        "mm_count": mm_count,
        "parameters": _to_list(payload.get("parameters")),
    }


def _format_data_source_label(source_rows: list[list[str]], sources_raw: list[Any]) -> str:
    if source_rows:
        row = source_rows[0]
        stype, server, database = row[0], row[1], row[2]
        if server != "—" and database != "—":
            return f"{stype} — {server} ({database})"
        if server != "—":
            return f"{stype} — {server}"
        return stype
    if sources_raw:
        return _trim_string(str(sources_raw[0]), 120)
    return "Non détectée dans les métadonnées PBIX"


def _llm_redescribe_item(
    item_type: str,
    name: str,
    columns: list[str],
    formula: str,
    model: str,
    base_url: str,
) -> str:
    """Single-item LLM call for duplicate disambiguation only."""
    prompt = (
        f"Décris en UNE phrase métier unique en français ce {item_type} Power BI.\n"
        f"Nom: {name}\n"
        f"Colonnes: {', '.join(columns) if columns else 'non fourni'}\n"
        f"Expression DAX: {formula or 'non fourni'}\n"
        "Réponds uniquement par la phrase, sans guillemets. Sois spécifique, pas générique."
    )
    try:
        return _trim_string(_call_ollama_generate(prompt, model, base_url), 160)
    except Exception:
        return ""


def enrich_documentation_json(ctx: dict[str, Any]) -> dict[str, Any]:
    """Deterministic descriptions first; LLM only for audit/narrative and duplicate fixes."""
    deterministic = build_deterministic_enrichment(ctx)
    ctx["enrichment"] = deterministic
    merged: dict[str, Any] = dict(deterministic)

    model = _pdf_ollama_model()
    base_url = str(os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)).strip() or DEFAULT_OLLAMA_BASE_URL
    try:
        raw = _call_ollama_generate(_build_llm_audit_prompt(ctx), model, base_url)
        llm_audit = _parse_llm_json(raw)
        for key, value in llm_audit.items():
            if key not in ("table_descriptions", "measure_descriptions", "calc_column_descriptions"):
                merged[key] = value
    except Exception as exc:
        merged["_audit_error"] = str(exc)

    # Re-check duplicate measure descriptions; optional LLM fix for stragglers
    measure_descs = merged.get("measure_descriptions") or {}
    biz: dict[str, str] = {n: (v.get("business_desc") if isinstance(v, dict) else str(v)) for n, v in measure_descs.items()}
    inv: dict[str, list[str]] = {}
    for n, d in biz.items():
        if d:
            inv.setdefault(d, []).append(n)
    for desc, names in inv.items():
        if len(names) < 2:
            continue
        for name in names:
            m = next((x for x in ctx.get("measures") or [] if x["name"] == name), {})
            better = _llm_redescribe_item(
                "indicateur DAX",
                name,
                [],
                m.get("formula", ""),
                model,
                base_url,
            )
            if better:
                measure_descs[name]["business_desc"] = better

    merged["measure_descriptions"] = measure_descs
    return merged


def _col_blob(table: str, columns: list[str]) -> str:
    return f"{table} {' '.join(columns)}".lower()


def get_table_description(table_name: str, column_names: list[str]) -> str | None:
    """Generate description based on table name first (most reliable)."""
    name_lower = table_name.lower()

    if "invoice" in name_lower or "factur" in name_lower:
        return "Factures clients et suivi de paiement"
    if "sale" in name_lower or "vente" in name_lower:
        return "Commandes de vente, marges et suivi livraison"
    if "employee" in name_lower or "employe" in name_lower:
        return "Employés / commerciaux"
    if "partner" in name_lower or "client" in name_lower:
        return "Clients / partenaires"
    if "currency" in name_lower or "devise" in name_lower:
        return "Devises et taux de change"
    if "product" in name_lower or "produit" in name_lower:
        return "Produits, prix unitaires et coûts"
    if "prospec" in name_lower:
        return "Prospects, stades pipeline et objectifs"
    if "purchase" in name_lower or "achat" in name_lower:
        return "Bons de commande fournisseurs"
    if "stock" in name_lower or "quant" in name_lower:
        return "Stock — quantités et dates d'entrée"
    if "supplier" in name_lower or "fournisseur" in name_lower:
        return "Données fournisseurs, coûts et références"
    return None


def _describe_table_from_columns_fallback(table: str, columns: list[str]) -> str:
    """Secondary signal from columns when table name is ambiguous."""
    t = table.lower()
    blob = _col_blob(table, columns)
    n = len(columns)

    if "currency" in t or ("rate" in blob and "currency" in blob):
        return "Devises et taux de change"
    if t.startswith("fact"):
        return f"Table de faits — transactions {table}"
    if t.startswith("dim"):
        return f"Dimension — attributs de référence ({table})"
    return f"Table {table} — {n} colonnes ({', '.join(columns[:4])}{'...' if n > 4 else ''})"


def describe_table_from_columns(table: str, columns: list[str]) -> str:
    """Table name first, then column-based fallback."""
    by_name = get_table_description(table, columns)
    if by_name:
        return by_name
    return _describe_table_from_columns_fallback(table, columns)


def describe_measure_dax_logic(name: str, formula: str) -> str:
    expr = re.sub(r"\s+", " ", formula or "").strip()
    if not expr:
        return "—"
    if len(expr) <= 100:
        return expr
    funcs = re.findall(r"\b([A-Z][A-Z0-9_]*)\s*\(", expr)
    uniq = [f for f in dict.fromkeys(funcs) if f not in ("IF", "VAR", "RETURN")][:5]
    return " + ".join(uniq) if uniq else _trim_string(expr, 100)


def describe_measure_from_name_and_dax(name: str, formula: str) -> str:
    """Deterministic measure business description."""
    n = name.lower()
    expr = (formula or "").upper()
    blob = f"{n} {expr}"

    if "mbm" in n or ("marge" in n and "moy" in n):
        return "Marge bénéficiaire moyenne"
    if "marge" in n or "margin" in n:
        return "Marge ou ratio de rentabilité"
    if "vente" in n and "mois" in n and ("actuel" in n or "actu" in n):
        return "Chiffre d'affaires du mois en cours"
    if "vente" in n and "mois" in n and ("precedent" in n or "préc" in n or "prec" in n):
        return "Chiffre d'affaires du mois précédent"
    if "cmv" in n or ("crois" in n and "vente" in n):
        return "Taux de croissance mensuelle des ventes"
    if "aov" in n or "goal" in n:
        return "Objectif de valeur moyenne des commandes"
    if "revenu" in n:
        return "Revenu net (CA moins coûts produits)"
    if "elc" in n or ("livraison" in n and "délai" not in n):
        return "Délai moyen de livraison en jours"
    if "efficien" in n or "etd" in n or "er_" in n:
        unit = "heures" if "HOUR" in expr else "jours"
        return f"Délai moyen ({unit}) — indicateur d'efficience opérationnelle"
    if "ctv" in n or ("cout" in n and "transport" in n):
        return "Ratio coût transport / chiffre d'affaires"
    if "cout" in n or "cost" in n or "totalcost" in n:
        return "Coût total ou somme des coûts produits"
    if "ptv" in n or ("prospect" in n and "vent" in n):
        return "Taux de conversion prospect vers vente / facture"
    if "rc_" in n or "ratio" in n and "cotation" in n:
        return "Ratio de cotation commerciale"
    if "tb_" in n or "backorder" in n:
        return "Pourcentage de commandes finalisées (backorders)"
    if "tv_" in n or "taux_vente" in n or "rotation" in n:
        return "Taux de rotation des stocks"
    if "vr_" in n or ("vent" in n and "repre" in n):
        return "Ventes moyennes par représentant commercial"
    if "ratio" in n or "taux" in n:
        if "DIVIDE" in expr and "COUNT" in expr:
            return "Taux ou ratio calculé (DIVIDE sur volumes)"
        return "Ratio ou taux métier dérivé du modèle"
    if "DIVIDE" in expr and "COUNT" in expr:
        return "Taux de conversion ou ratio entre deux volumes"
    if "SUMX" in expr and ("MONTH" in expr or "TODAY" in expr):
        return "Agrégat conditionnel sur la période courante"
    if columns_refs := re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\[", formula or ""):
        refs = ", ".join(dict.fromkeys(columns_refs)[:3])
        return f"Calcul agrégé sur {refs}"
    return f"Indicateur {name}"


def describe_calculated_column(table: str, column: str, formula: str) -> str:
    blob = f"{column} {formula}".lower()
    if "currency" in blob or "rate" in blob:
        if "if" in blob and "blank" in blob:
            return "Taux de change avec fallback à 1"
        return "Montant ou coût converti en devise de référence"
    if "margin" in blob or "marge" in blob:
        return "Marge en devise locale"
    if "total" in blob and "price" in blob:
        return "Montant total converti en devise de référence"
    if "cost" in blob:
        return "Coût produit ou fournisseur converti"
    if "ht" in blob:
        return "Montant HT converti en devise de référence"
    return f"Colonne dérivée — {column} sur {table}"


def _dedupe_descriptions(
    descriptions: dict[str, str],
    keys: list[str],
    columns_map: dict[str, list[str]],
) -> dict[str, str]:
    """Ensure each key has a unique description; disambiguate duplicates."""
    out = dict(descriptions)
    inv: dict[str, list[str]] = {}
    for key in keys:
        desc = out.get(key, "").strip()
        if desc:
            inv.setdefault(desc, []).append(key)

    for desc, dup_keys in inv.items():
        if len(dup_keys) < 2:
            continue
        for key in dup_keys:
            cols = columns_map.get(key, [])
            if "|" in key:
                table, col = key.split("|", 1)
                out[key] = f"{desc} ({table}[{col}])"
            else:
                hint = ", ".join(cols[:5]) if cols else key
                out[key] = f"{desc} — {hint}"

    return out


def build_deterministic_enrichment(ctx: dict[str, Any]) -> dict[str, Any]:
    """Build table/measure/column descriptions without relying on LLM."""
    columns_map = ctx.get("columns_map") or {}
    table_descriptions: dict[str, str] = {}
    all_tables = list(ctx.get("fact_names") or []) + list(ctx.get("dim_names") or [])

    for table in all_tables:
        table_descriptions[table] = describe_table_from_columns(table, columns_map.get(table, []))

    table_descriptions = _dedupe_descriptions(table_descriptions, all_tables, columns_map)

    measure_descriptions: dict[str, dict[str, str]] = {}
    for m in ctx.get("measures") or []:
        name = m["name"]
        formula = m.get("formula", "")
        measure_descriptions[name] = {
            "business_desc": describe_measure_from_name_and_dax(name, formula),
            "dax_logic": describe_measure_dax_logic(name, formula),
            "domain": m.get("domain") or _guess_measure_domain(name),
        }

    biz_descs = {n: v["business_desc"] for n, v in measure_descriptions.items()}
    fixed_biz = _dedupe_descriptions(biz_descs, list(biz_descs.keys()), {})
    for name, desc in fixed_biz.items():
        measure_descriptions[name]["business_desc"] = desc

    calc_descriptions: dict[str, str] = {}
    for c in ctx.get("calculated_columns") or []:
        key = f"{c['table']}|{c['name']}"
        calc_descriptions[key] = describe_calculated_column(c["table"], c["name"], c.get("formula", ""))

    calc_descriptions = _dedupe_descriptions(
        calc_descriptions,
        list(calc_descriptions.keys()),
        {k.split("|")[0]: columns_map.get(k.split("|")[0], []) for k in calc_descriptions},
    )

    return {
        "table_descriptions": table_descriptions,
        "measure_descriptions": measure_descriptions,
        "calc_column_descriptions": calc_descriptions,
    }


def _build_llm_audit_prompt(ctx: dict[str, Any]) -> str:
    """Smaller LLM prompt — audit and narrative only; descriptions are pre-built."""
    summary = {
        "filename": ctx["filename"],
        "schema": ctx["schema_info"],
        "kpis": ctx["kpis"],
        "fact_tables": ctx["fact_names"],
        "dimension_tables": ctx["dim_names"],
        "mm_count": ctx["mm_count"],
        "source": ctx.get("data_source_label"),
        "table_descriptions": ctx.get("enrichment", {}).get("table_descriptions", {}),
        "measure_count": len(ctx.get("measures") or []),
    }
    return f"""Expert Power BI. Renvoie UNIQUEMENT du JSON valide.

MODÈLE:
{_safe_json(summary)}

{{
  "overview": ["résumé exécutif paragraphe 1", "paragraphe 2"],
  "sources_intro": "phrase sur la source de données",
  "sources_caption": "note",
  "tables_intro": "phrase intro tables",
  "tables_caption": "note anomalies nommage si pertinent",
  "relationships_intro": "phrase",
  "relationships_warning": "texte si M:M sinon vide",
  "measures_intro": "phrase",
  "calculated_columns_intro": "phrase",
  "rls_intro": "texte si RLS",
  "rls_info": "propagation filtres RLS",
  "audit": {{
    "strengths": ["3 à 5 forces spécifiques"],
    "issues": [{{"severity": "Élevé|Moyen|Faible", "text": "problème"}}],
    "recommendations": [{{"num": "1", "title": "titre", "desc": "action"}}]
  }}
}}

Français. Spécifique à CE modèle. Pas de généralités."""


def _fallback_audit(ctx: dict[str, Any]) -> dict[str, Any]:
    schema = ctx["schema_info"]
    k = ctx["kpis"]
    mm = ctx["mm_count"]
    strengths = [
        f"Schéma {schema['schema_type'].lower()} avec séparation claire faits / dimensions",
        f"{k['measures']} mesures DAX couvrant ventes, marges et opérations",
        f"Source de données unique ({ctx.get('data_source_label', 'détectée')}) simplifiant la gouvernance",
    ]
    if ctx.get("calculated_columns"):
        strengths.append(f"{k['calculated_columns']} colonnes calculées pour conversions de devises et montants normalisés")
    if ctx.get("rls_rows"):
        strengths.append("RLS configuré pour le contrôle d'accès par utilisateur")

    issues: list[dict[str, str]] = []
    if mm > 0:
        issues.append(
            {
                "severity": "Élevé",
                "text": f"{mm} relation(s) Many-to-Many avec filtrage bidirectionnel — risque d'ambiguïté et de performances.",
            }
        )
    thin_dims = [t for t in ctx.get("dim_names", []) if len((ctx.get("columns_map") or {}).get(t, [])) <= 2]
    if thin_dims:
        issues.append(
            {
                "severity": "Moyen",
                "text": f"Dimensions peu enrichies ({', '.join(thin_dims[:3])}) — ajouter attributs métier (email, région).",
            }
        )
    if any("prospecct" in t for t in ctx.get("dim_names", [])):
        issues.append(
            {
                "severity": "Faible",
                "text": "Faute de frappe dans 'dim_prospecct' (double 'c') — impact sur la lisibilité.",
            }
        )

    recommendations = [
        {
            "num": "1",
            "title": "Résoudre les relations M:M",
            "desc": "Créer des tables pont pour éliminer les ambiguïtés de filtrage si des relations M:M existent.",
        },
        {
            "num": "2",
            "title": "Enrichir les dimensions clés",
            "desc": "Ajouter email, département et région sur employés/partenaires pour le RLS et l'analyse.",
        },
        {
            "num": "3",
            "title": "Standardiser le nommage",
            "desc": "Adopter une convention FR ou EN cohérente pour mesures et colonnes.",
        },
        {
            "num": "4",
            "title": "Table calendrier dédiée",
            "desc": "Remplacer les LocalDateTable auto-générées par une dim_date centralisée.",
        },
        {
            "num": "5",
            "title": "Documenter les mesures",
            "desc": "Renseigner le champ Description de chaque mesure dans Power BI Desktop.",
        },
    ]
    return {"strengths": strengths, "issues": issues, "recommendations": recommendations}


def _fallback_overview(ctx: dict[str, Any]) -> list[str]:
    k = ctx["kpis"]
    schema = ctx["schema_info"]
    return [
        (
            f"Ce document décrit le modèle Power BI <b>{ctx['filename']}</b>. "
            f"Il contient {k['tables']} tables métier, {k['measures']} mesures DAX, "
            f"{k['calculated_columns']} colonnes calculées et {k['relationships']} relations."
        ),
        (
            f"Le schéma détecté est de type <b>{schema['schema_type']}</b>. "
            f"{schema['explanation']} Les tables de dates auto-générées (LocalDateTable, DateTableTemplate) "
            "sont exclues de ce rapport."
        ),
    ]


def _merge_measure_groups(ctx: dict[str, Any], llm: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    desc_map = llm.get("measure_descriptions") if isinstance(llm.get("measure_descriptions"), dict) else {}
    if not desc_map and isinstance(ctx.get("enrichment"), dict):
        desc_map = ctx["enrichment"].get("measure_descriptions") or {}
    groups: dict[str, list[dict[str, str]]] = {}

    for measure in ctx["measures"]:
        name = measure["name"]
        llm_m = desc_map.get(name) if isinstance(desc_map.get(name), dict) else {}
        domain = str(llm_m.get("domain") or measure.get("domain") or "Autres Mesures")
        business_desc = str(llm_m.get("business_desc") or describe_measure_from_name_and_dax(name, measure.get("formula", "")))
        dax_logic = str(
            llm_m.get("dax_logic")
            or describe_measure_dax_logic(name, measure.get("formula", ""))
            or measure.get("dax_logic")
            or "—"
        )
        groups.setdefault(domain, []).append({"name": name, "business_desc": business_desc, "dax_logic": dax_logic})

    domain_order = [d[0] for d in MEASURE_DOMAIN_RULES] + ["Autres Mesures"]
    ordered: dict[str, list[dict[str, str]]] = {}
    for domain in domain_order:
        if domain in groups:
            ordered[domain] = groups.pop(domain)
    for domain, items in groups.items():
        ordered[domain] = items
    return ordered


def assemble_reportlab_document(ctx: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    """Merge prepared context + LLM enrichment into ReportLab document dict."""
    llm = llm or {}
    schema = ctx["schema_info"]
    k = ctx["kpis"]

    overview_raw = llm.get("overview") if isinstance(llm.get("overview"), list) else None
    if not overview_raw:
        overview_raw = _fallback_overview(ctx)
    overview_paragraphs = [str(p) for p in overview_raw if str(p).strip()]

    table_desc = llm.get("table_descriptions") if isinstance(llm.get("table_descriptions"), dict) else {}
    if not table_desc and isinstance(ctx.get("enrichment"), dict):
        table_desc = ctx["enrichment"].get("table_descriptions") or {}
    columns_map = ctx["columns_map"]

    fact_tables: list[list[str]] = []
    for name in ctx["fact_names"]:
        cols = columns_map.get(name, [])
        desc = str(table_desc.get(name) or describe_table_from_columns(name, cols))
        fact_tables.append([name, str(len(cols)), desc, _format_main_columns(cols)])

    dim_tables: list[list[str]] = []
    for name in ctx["dim_names"]:
        cols = columns_map.get(name, [])
        desc = str(table_desc.get(name) or describe_table_from_columns(name, cols))
        dim_tables.append([name, str(len(cols)), desc])

    rel_rows = [[r["from"], r["to"], r["cardinality"], r["direction"], r["active"], r["remark"]] for r in ctx["relationships"]]

    calc_desc = llm.get("calc_column_descriptions") if isinstance(llm.get("calc_column_descriptions"), dict) else {}
    if not calc_desc and isinstance(ctx.get("enrichment"), dict):
        calc_desc = ctx["enrichment"].get("calc_column_descriptions") or {}
    calc_rows: list[list[str]] = []
    for col in ctx["calculated_columns"]:
        key = f"{col['table']}|{col['name']}"
        desc = str(
            calc_desc.get(key)
            or calc_desc.get(col["name"])
            or describe_calculated_column(col["table"], col["name"], col.get("formula", ""))
        )
        calc_rows.append([col["table"], col["name"], col["expression_short"], desc])

    rls_rows = [[r["role"], r["table"], r["expression"], r["description"]] for r in ctx["rls_rows"]]

    audit = llm.get("audit") if isinstance(llm.get("audit"), dict) else {}
    fallback_audit = _fallback_audit(ctx)

    strengths = audit.get("strengths") if isinstance(audit.get("strengths"), list) else []
    if len(strengths) < 3:
        strengths = (strengths + fallback_audit["strengths"])[:5]

    issues_raw = audit.get("issues") if isinstance(audit.get("issues"), list) else []
    issues: list[tuple[str, str]] = []
    for item in issues_raw:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                issues.append((str(item.get("severity") or "Moyen"), text))
        elif isinstance(item, str) and item.strip():
            issues.append(("Moyen", item.strip()))
    if len(issues) < 3:
        for item in fallback_audit["issues"]:
            issues.append((item["severity"], item["text"]))
        issues = issues[:5]

    recs_raw = audit.get("recommendations") if isinstance(audit.get("recommendations"), list) else []
    recommendations: list[tuple[str, str, str]] = []
    for i, item in enumerate(recs_raw, 1):
        if isinstance(item, dict):
            recommendations.append(
                (
                    str(item.get("num") or i),
                    str(item.get("title") or "Recommandation"),
                    str(item.get("desc") or ""),
                )
            )
    if len(recommendations) < 3:
        for item in fallback_audit["recommendations"]:
            recommendations.append((item["num"], item["title"], item["desc"]))
        recommendations = recommendations[:5]

    # Section visibility
    section_flags = {
        "overview": bool(overview_paragraphs),
        "sources": bool(ctx["source_rows"]),
        "tables": bool(fact_tables or dim_tables),
        "relationships": bool(rel_rows),
        "measures": bool(ctx["measures"]),
        "calculated_columns": bool(calc_rows),
        "rls": bool(
            [
                r
                for r in ctx["rls_rows"]
                if any(str(r.get(k) or "").strip() not in ("", "—") for k in ("role", "table", "expression"))
            ]
        ),
        "audit": True,
    }

    visible_keys = [key for key, _title, _desc in TOC_CATALOG if section_flags.get(key)]
    toc_sections = []
    content_sections = []
    for idx, key in enumerate(visible_keys, 1):
        num = f"{idx:02d}"
        title_desc = next((t, d) for k, t, d in TOC_CATALOG if k == key)
        toc_sections.append({"num": num, "title": title_desc[0], "desc": title_desc[1]})

        data: dict[str, Any] = {"num": num}
        if key == "overview":
            data["paragraphs"] = overview_paragraphs
        elif key == "sources":
            data["intro"] = str(llm.get("sources_intro") or "")
            data["rows"] = ctx["source_rows"]
            data["caption"] = str(llm.get("sources_caption") or "")
        elif key == "tables":
            data["intro"] = str(
                llm.get("tables_intro")
                or (
                    f"Le modèle contient <b>{k['tables']} tables métier</b> "
                    f"({len(fact_tables)} faits, {len(dim_tables)} dimensions). "
                    f"Schéma {schema['schema_type']}."
                )
            )
            data["fact_tables"] = fact_tables
            data["dim_tables"] = dim_tables
            data["caption"] = str(llm.get("tables_caption") or "")
        elif key == "relationships":
            mm = ctx["mm_count"]
            data["intro"] = str(
                llm.get("relationships_intro")
                or (
                    f"Le modèle définit <b>{len(rel_rows)} relations</b> entre tables métier."
                    + (f" Attention : <b>{mm}</b> relation(s) Many-to-Many." if mm else "")
                )
            )
            data["rows"] = rel_rows
            warn = str(llm.get("relationships_warning") or "").strip()
            if not warn and mm > 0:
                warn = (
                    f"{mm} relation(s) Many-to-Many avec filtrage bidirectionnel peuvent provoquer "
                    "des ambiguïtés de filtrage et dégrader les performances. Envisagez des tables pont."
                )
            data["warning"] = warn
        elif key == "measures":
            data["intro"] = str(
                llm.get("measures_intro")
                or f"Le modèle contient <b>{k['measures']} mesures DAX</b> regroupées par domaine métier."
            )
            data["groups"] = _merge_measure_groups(ctx, llm)
        elif key == "calculated_columns":
            data["intro"] = str(
                llm.get("calculated_columns_intro")
                or f"Le modèle utilise <b>{k['calculated_columns']} colonnes calculées</b>."
            )
            data["rows"] = calc_rows
        elif key == "rls":
            data["intro"] = str(llm.get("rls_intro") or f"Le modèle contient <b>{len(rls_rows)} rôle(s) RLS</b>.")
            data["rows"] = rls_rows
            data["info"] = str(llm.get("rls_info") or "")
        elif key == "audit":
            data["strengths"] = [str(s) for s in strengths if str(s).strip()]
            data["issues"] = [(s, t) for s, t in issues if t]
            data["recommendations"] = recommendations

        content_sections.append({"builder": SECTION_BUILDERS[key], "data": data})

    return {
        "filename": ctx["filename"],
        "generated_at": ctx["generated_at"],
        "model_name": ctx["model_name"],
        "data_source_label": ctx["data_source_label"],
        "kpis": k,
        "toc_sections": toc_sections,
        "content_sections": content_sections,
    }


def generate_documentation_pdf_bytes(
    payload: dict[str, Any],
    progress_callback: Callable[[str, str], None] | None = None,
) -> tuple[bytes, str]:
    def _progress(step: str, message: str) -> None:
        if progress_callback:
            progress_callback(step, message)

    _progress("prepare", "Préparation des données du modèle...")
    ctx = prepare_pdf_context(payload)

    _progress("enriching", "Génération des descriptions et analyse IA...")
    ctx["enrichment"] = build_deterministic_enrichment(ctx)
    llm = enrich_documentation_json(ctx)
    for drop_key in ("_error", "_audit_error"):
        llm.pop(drop_key, None)

    _progress("pdf", "Génération du PDF ReportLab...")
    doc = assemble_reportlab_document(ctx, llm)
    pdf_bytes = build_reportlab_pdf(doc)

    output_name = f"{os.path.splitext(os.path.basename(ctx['filename']))[0]}_documentation.pdf"
    return pdf_bytes, output_name


def generate_documentation_pdf(payload: dict[str, Any]) -> tuple[str, str]:
    pdf_bytes, output_name = generate_documentation_pdf_bytes(payload)
    fd, pdf_path = tempfile.mkstemp(prefix="pbix_documentation_", suffix=".pdf")
    os.close(fd)
    with open(pdf_path, "wb") as out:
        out.write(pdf_bytes)
    return pdf_path, output_name
