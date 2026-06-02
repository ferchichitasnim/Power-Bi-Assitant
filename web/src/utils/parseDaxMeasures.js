/**
 * Parse generated DAX text into structured measure objects for /api/pbix/patch-measures.
 */

function stripCodeFences(text) {
  return String(text || "")
    .replace(/^```[\w]*\s*/im, "")
    .replace(/```\s*$/im, "")
    .trim();
}

/**
 * Infer table name from Table[Column] or 'Table Name'[Column] references in DAX.
 */
export function inferTableFromDax(daxExpression, tableNames = []) {
  const expr = String(daxExpression || "");
  const tables = (tableNames || []).filter(Boolean);
  if (!tables.length) return "";

  const quoted = [...expr.matchAll(/'([^']+)'\s*\[/g)].map((m) => m[1]);
  for (const name of quoted) {
    const hit = tables.find((t) => t === name || t.toLowerCase() === name.toLowerCase());
    if (hit) return hit;
  }

  const bare = [...expr.matchAll(/\b([A-Za-z_][\w]*)\s*\[/g)].map((m) => m[1]);
  for (const name of bare) {
    const hit = tables.find((t) => t === name || t.toLowerCase() === name.toLowerCase());
    if (hit) return hit;
  }

  return tables[0];
}

/**
 * @param {string} daxText - Raw DAX from the LLM (may include `Name = expression`)
 * @param {string} defaultTableName - Fallback table when not inferable from DAX
 * @param {string[]} tableNames - Model tables from upload metadata
 * @returns {{ table_name: string, measure_name: string, dax_expression: string, format_string: string, description: string }[]}
 */
export function parseDaxMeasures(daxText, defaultTableName = "", tableNames = []) {
  const text = stripCodeFences(daxText);
  if (!text) return [];

  const defaultTable =
    defaultTableName || (Array.isArray(tableNames) && tableNames.length ? tableNames[0] : "");

  const blocks = text.split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
  const chunks = blocks.length > 1 ? blocks : [text];

  const measures = [];

  for (const chunk of chunks) {
    const cleaned = stripCodeFences(chunk);
    const eqIdx = cleaned.indexOf("=");
    if (eqIdx > 0) {
      const measure_name = cleaned.slice(0, eqIdx).trim();
      let dax_expression = cleaned.slice(eqIdx + 1).trim();
      dax_expression = stripCodeFences(dax_expression);
      if (measure_name && dax_expression) {
        const table_name = inferTableFromDax(dax_expression, tableNames) || defaultTable;
        measures.push({
          table_name,
          measure_name,
          dax_expression,
          format_string: "",
          description: "",
        });
      }
    } else if (cleaned) {
      const table_name = inferTableFromDax(cleaned, tableNames) || defaultTable;
      measures.push({
        table_name,
        measure_name: "New Measure",
        dax_expression: cleaned,
        format_string: "",
        description: "",
      });
    }
  }

  return measures;
}

/**
 * @param {string | null} contentDisposition
 * @returns {string}
 */
export function filenameFromContentDisposition(contentDisposition) {
  if (!contentDisposition) return "download";
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      return utf8[1];
    }
  }
  const quoted = /filename="([^"]+)"/i.exec(contentDisposition);
  if (quoted?.[1]) return quoted[1];
  const plain = /filename=([^;\s]+)/i.exec(contentDisposition);
  return plain?.[1] || "download";
}
