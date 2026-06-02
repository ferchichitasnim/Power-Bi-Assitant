/**
 * Connector detection from Power Query M — mirrors documentation_pdf.detect_source_type.
 */

function normalizeMCode(text) {
  let value = String(text || "");
  value = value.replace(/\\n/g, " ").replace(/\\t/g, " ").replace(/\\r/g, " ");
  value = value.replace(/\\\./g, ".");
  value = value.replace(/\\\\/g, "\\");
  return value;
}

function collectMExpressions(powerQuery) {
  const expressions = [];
  for (const row of powerQuery || []) {
    if (!row || typeof row !== "object") continue;
    for (const key of ["Expression", "expression", "Formula", "M", "Query"]) {
      const val = row[key];
      if (val != null && String(val).trim() && String(val).toLowerCase() !== "nan") {
        expressions.push(String(val));
        break;
      }
    }
  }
  return expressions;
}

/** @returns {string} Connector label (PostgreSQL, SQL Server, …) or "Inconnu" */
export function detectSourceType(mExpressions) {
  const allMCode = normalizeMCode(
    (mExpressions || []).filter((e) => String(e).trim()).join(" ")
  );
  if (!allMCode.trim()) return "Inconnu";

  const codeUpper = allMCode.replace(/ /g, "");
  const checks = [
    ["PostgreSQL.Database", "PostgreSQL"],
    ["PostgreSQL\\.Database", "PostgreSQL"],
    ["Sql.Database", "SQL Server"],
    ["Sql.Databases", "SQL Server"],
    ["Oracle.Database", "Oracle"],
    ["MySQL.Database", "MySQL"],
    ["Odbc.DataSource", "ODBC"],
    ["OData.Feed", "OData"],
    ["Excel.Workbook", "Excel"],
    ["Csv.Document", "CSV"],
    ["SharePoint.", "SharePoint"],
    ["GoogleBigQuery", "BigQuery"],
    ["Snowflake.", "Snowflake"],
    ["AmazonRedshift", "Redshift"],
  ];
  for (const [needle, label] of checks) {
    if (codeUpper.includes(needle) || allMCode.includes(needle)) return label;
  }
  return "Inconnu";
}

export function formatSourceLabel(label, connectorType) {
  const text = String(label || "").trim();
  const ctype = String(connectorType || "").trim();
  if (!text) return "—";
  if (!ctype || ctype === "Inconnu") return text;
  if (text.startsWith(`${ctype} — `) || text.startsWith(`${ctype} - `)) return text;
  return `${ctype} — ${text}`;
}

/** @param {string[]} sources @param {object[]} powerQuery */
export function enrichSourceLabels(sources, powerQuery) {
  const connector = detectSourceType(collectMExpressions(powerQuery));
  return (sources || []).map((s) => formatSourceLabel(s, connector));
}
