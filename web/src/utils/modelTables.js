/**
 * Build per-table schema (columns + measures) from PBIX upload payload.
 */

function isInternalPowerBITableName(name) {
  const v = String(name || "").toLowerCase();
  return v.includes("localdatatable_") || v.includes("datetabletemplate_");
}

/**
 * @param {string[]} measuresFlat - e.g. ["fact_invoice2[Revenu]", "Revenu"]
 * @returns {Record<string, string[]>}
 */
function measuresByTableFromFlat(measuresFlat) {
  /** @type {Record<string, string[]>} */
  const out = {};
  for (const raw of measuresFlat || []) {
    const m = String(raw || "").trim();
    if (!m) continue;
    const bracket = m.match(/^([^[]+)\[([^\]]+)\]$/);
    if (bracket) {
      const table = bracket[1].trim();
      const name = bracket[2].trim();
      if (!table || !name) continue;
      if (!out[table]) out[table] = [];
      if (!out[table].includes(name)) out[table].push(name);
      continue;
    }
    if (!out.__bare__) out.__bare__ = [];
    if (!out.__bare__.includes(m)) out.__bare__.push(m);
  }
  delete out.__bare__;
  return out;
}

/**
 * @param {object} data - Upload API response (or sanitized subset)
 * @returns {{ name: string, columns: string[], measures: string[] }[]}
 */
export function buildModelTablesFromPayload(data) {
  const tableNames = (data?.tables || [])
    .map((t) => String(t).trim())
    .filter((t) => t && !isInternalPowerBITableName(t));

  const columnsObj = data?.columns && typeof data.columns === "object" ? { ...data.columns } : {};
  for (const row of [...(data?.schema || []), ...(data?.stats_preview || [])]) {
    if (!row || typeof row !== "object") continue;
    const table = String(row.TableName || row.table || "").trim();
    const column = String(row.ColumnName || row.column || "").trim();
    if (!table || !column || isInternalPowerBITableName(table)) continue;
    if (!columnsObj[table]) columnsObj[table] = [];
    if (!columnsObj[table].includes(column)) columnsObj[table].push(column);
  }
  const safeColumns = Object.fromEntries(
    Object.entries(columnsObj).filter(([tableName]) => !isInternalPowerBITableName(tableName))
  );

  /** @type {Record<string, string[]>} */
  const measuresByTable = {};

  const measureDocs = data?.documentation?.dax_calculations?.measures;
  if (Array.isArray(measureDocs) && measureDocs.length > 0) {
    for (const doc of measureDocs) {
      if (!doc || typeof doc !== "object") continue;
      const table = String(doc.table || doc.TableName || "").trim();
      const name = String(doc.name || doc.Name || "").trim();
      if (!table || !name || isInternalPowerBITableName(table)) continue;
      if (!measuresByTable[table]) measuresByTable[table] = [];
      if (!measuresByTable[table].includes(name)) measuresByTable[table].push(name);
    }
  }

  if (!Object.keys(measuresByTable).length) {
    Object.assign(measuresByTable, measuresByTableFromFlat(data?.measures || []));
  }

  const allTableNames = new Set([...tableNames, ...Object.keys(safeColumns), ...Object.keys(measuresByTable)]);

  return [...allTableNames]
    .filter((name) => !isInternalPowerBITableName(name))
    .sort((a, b) => a.localeCompare(b))
    .map((name) => ({
      name,
      columns: [...new Set((safeColumns[name] || []).map((c) => String(c).trim()).filter(Boolean))].sort(
        (a, b) => a.localeCompare(b)
      ),
      measures: [...new Set((measuresByTable[name] || []).map((m) => String(m).trim()).filter(Boolean))].sort(
        (a, b) => a.localeCompare(b)
      ),
    }));
}

/**
 * Plain-text DATA MODEL block for LLM prompts (works well with small models).
 *
 * @param {{ name: string, columns?: string[], measures?: string[] }[]} tables
 * @returns {string}
 */
export function formatModelForPrompt(tables) {
  if (!tables || tables.length === 0) return "";

  return tables
    .map((t) => {
      const lines = [`Table: ${t.name}`];
      if (t.columns && t.columns.length > 0) {
        lines.push(`  Columns: ${t.columns.join(", ")}`);
      }
      if (t.measures && t.measures.length > 0) {
        lines.push(`  Existing Measures: ${t.measures.join(", ")}`);
      }
      return lines.join("\n");
    })
    .join("\n\n");
}
