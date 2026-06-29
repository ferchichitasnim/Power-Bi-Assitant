import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

_FOCUS_STOP_WORDS = frozenset(
    {
        "per",
        "for",
        "the",
        "and",
        "by",
        "or",
        "vs",
        "par",
        "pour",
        "les",
        "des",
        "une",
        "with",
        "from",
        "into",
        "this",
        "that",
        "are",
        "was",
        "were",
        "has",
        "have",
        "not",
        "aux",
        "sur",
        "dans",
    }
)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def normalize_tables(tables_obj: Any) -> list[str]:
    if tables_obj is None:
        return []
    if hasattr(tables_obj, "tolist"):
        return [str(v) for v in tables_obj.tolist()]
    return [str(v) for v in tables_obj]


def _focus_word_variants(word: str) -> set[str]:
    variants = {word}
    if len(word) > 4 and word.endswith("s"):
        variants.add(word[:-1])
    if len(word) > 5 and word.endswith("es"):
        variants.add(word[:-2])
    if len(word) > 4 and word.endswith("ing"):
        variants.add(word[:-3])
    return variants


def _tokenize_context_text(context_text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", context_text.lower()))


def focus_matches_context(context_text: str, focus: str) -> bool:
    """
    Return True when the focus phrase plausibly relates to PBIX metadata text.

    Uses stop-word filtering, simple stemming (sales -> sale), and token overlap
    so report phrases like "Sales per commercial" match tables such as fact_sale2.
    """
    focus = (focus or "").strip()
    if not focus:
        return True

    context_text = (context_text or "").strip()
    if not context_text:
        return False

    focus_lower = focus.lower()
    context_lower = context_text.lower()
    focus_norm = re.sub(r"\s+", " ", focus_lower).strip()
    if focus_norm and focus_norm in context_lower:
        return True

    focus_words = [w for w in re.findall(r"[a-z0-9]+", focus_lower) if len(w) >= 3 and w not in _FOCUS_STOP_WORDS]
    if not focus_words:
        return True

    context_tokens = _tokenize_context_text(context_text)
    matched = 0
    for word in focus_words:
        if any(variant in context_lower for variant in _focus_word_variants(word)):
            matched += 1
            continue
        prefix = word[:4] if len(word) >= 4 else word
        if any(token.startswith(prefix) or prefix in token for token in context_tokens):
            matched += 1

    required = 1 if len(focus_words) == 1 else max(1, (len(focus_words) + 1) // 2)
    return matched >= required


def normalize_statistics(stats_obj: Any) -> list[dict[str, Any]]:
    if stats_obj is None:
        return []
    if hasattr(stats_obj, "to_dict"):
        return stats_obj.to_dict(orient="records")
    if isinstance(stats_obj, list):
        return stats_obj
    return []


def compact_story_context_for_prompt(context: dict[str, Any]) -> dict[str, Any]:
    """Smaller payload for Ollama — keeps names the model needs without huge column lists."""
    if not isinstance(context, dict):
        return {}
    tables = [str(t) for t in (context.get("tables") or []) if str(t).strip()][:40]
    measures = [str(m) for m in (context.get("measures") or []) if str(m).strip()][:80]
    column_names = [str(c) for c in (context.get("column_names") or []) if str(c).strip()][:60]
    top_size = context.get("top_size_columns") or []
    top_card = context.get("top_cardinality_columns") or []
    if not column_names and isinstance(top_size, list):
        for row in top_size[:30]:
            if isinstance(row, dict):
                col = str(row.get("column") or "").strip()
                if col:
                    column_names.append(col)
    return {
        "file_name": context.get("file_name"),
        "table_count": context.get("table_count") or len(tables),
        "tables": tables,
        "measures": measures,
        "column_names": column_names[:60],
        "top_size_columns": top_size[:8] if isinstance(top_size, list) else [],
        "top_cardinality_columns": top_card[:8] if isinstance(top_card, list) else [],
    }


def build_story_context(
    file_path: str,
    tables_obj: Any,
    statistics_obj: Any,
    measures_obj: Any = None,
) -> dict[str, Any]:
    tables = normalize_tables(tables_obj)
    stats_rows = normalize_statistics(statistics_obj)
    measures: list[str] = []
    if measures_obj is not None:
        if isinstance(measures_obj, list):
            measures = [str(m).strip() for m in measures_obj if str(m).strip()]
        elif hasattr(measures_obj, "tolist"):
            measures = [str(m).strip() for m in measures_obj.tolist() if str(m).strip()]

    column_names: list[str] = []
    seen_columns: set[str] = set()
    for row in stats_rows:
        if not isinstance(row, dict):
            continue
        column = str(row.get("ColumnName") or "").strip()
        if not column or column in seen_columns:
            continue
        seen_columns.add(column)
        column_names.append(column)

    total_size = sum(_safe_int(r.get("DataSize")) for r in stats_rows)
    total_dictionary = sum(_safe_int(r.get("Dictionary")) for r in stats_rows)
    total_hash_index = sum(_safe_int(r.get("HashIndex")) for r in stats_rows)

    top_size = sorted(stats_rows, key=lambda r: _safe_int(r.get("DataSize")), reverse=True)[:15]
    top_cardinality = sorted(stats_rows, key=lambda r: _safe_int(r.get("Cardinality")), reverse=True)[:15]

    def reduce_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "table": row.get("TableName"),
            "column": row.get("ColumnName"),
            "cardinality": _safe_int(row.get("Cardinality")),
            "data_size": _safe_int(row.get("DataSize")),
        }

    return {
        "file_name": os.path.basename(file_path),
        "table_count": len(tables),
        "tables": tables,
        "column_count": len(stats_rows),
        "measures": measures[:300],
        "column_names": column_names[:500],
        "total_data_size": total_size,
        "total_dictionary": total_dictionary,
        "total_hash_index": total_hash_index,
        "top_size_columns": [reduce_row(r) for r in top_size],
        "top_cardinality_columns": [reduce_row(r) for r in top_cardinality],
    }


def _story_prompt(context: dict[str, Any]) -> str:
    compact = json.dumps(context, ensure_ascii=True)
    return (
        "You are a senior Power BI analytics storyteller. "
        "Create a concise but specific narrative for a business stakeholder using this model context.\n\n"
        "Output format rules:\n"
        "1) Use exactly these markdown headings:\n"
        "## Overview\n## Key Insights\n## Risks or Data Quality Concerns\n## Recommended Actions\n"
        "2) Under each heading use 3-6 bullet points.\n"
        "3) Be concrete with names of tables/columns when possible.\n"
        "4) No generic fluff.\n\n"
        f"Context JSON:\n{compact}"
    )


def generate_story_with_ollama(
    context: dict[str, Any],
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: int = 300,
) -> str:
    ollama_model = model or os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
    ollama_base = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
    url = f"{ollama_base}/v1/chat/completions"

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": "You write sharp Power BI narratives from metadata and statistics."},
            {"role": "user", "content": _story_prompt(context)},
        ],
        "temperature": 0.2,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Ollama at {ollama_base}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid JSON response from Ollama.") from exc

    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Unexpected response format from Ollama.") from exc


def split_story_sections(story_text: str) -> dict[str, str]:
    headings = [
        "Overview",
        "Key Insights",
        "Risks or Data Quality Concerns",
        "Recommended Actions",
    ]
    sections = {heading: "" for heading in headings}

    for idx, heading in enumerate(headings):
        pattern = rf"##\s*{re.escape(heading)}\s*(.*?)"
        if idx + 1 < len(headings):
            next_heading = headings[idx + 1]
            pattern += rf"(?=##\s*{re.escape(next_heading)}|\Z)"
        match = re.search(pattern, story_text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            sections[heading] = match.group(1).strip()

    return sections
