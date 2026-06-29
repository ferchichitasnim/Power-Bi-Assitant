import { ReducedDaxContext } from "./types";

const FILTER_VALUE_MAP: Record<string, { column: string; value: string }> = {
  "confirmed|confirmé|confirmée|confirmées": { column: "state", value: "sale" },
  "paid|payé|payée|payées": { column: "payment_state", value: "paid" },
  "draft|brouillon": { column: "state", value: "draft" },
  "cancelled|annulé|annulée": { column: "state", value: "cancel" },
  "sent|envoyé|envoyée": { column: "state", value: "sent" },
  "overdue|en retard|impayé": { column: "payment_state", value: "not_paid" },
};

export function tryFilterFallback(prompt: string, ctx: ReducedDaxContext): string | null {
  const lower = prompt.toLowerCase();

  // Match a filter keyword
  let filterColumn = "";
  let filterValue = "";
  for (const [keywords, mapping] of Object.entries(FILTER_VALUE_MAP)) {
    if (new RegExp(`\\b(${keywords})\\b`, "i").test(lower)) {
      filterColumn = mapping.column;
      filterValue = mapping.value;
      break;
    }
  }
  if (!filterColumn) return null;

  // Find which table has this column
  let tableName = "";
  for (const table of ctx.tables) {
    if (table.columns.some(c => c.toLowerCase() === filterColumn.toLowerCase())) {
      tableName = table.name;
      break;
    }
  }
  if (!tableName) return null;

  // Determine aggregation type from the prompt
  const isCount = /\b(nombre|count|number|combien|how many)\b/i.test(lower);
  const measureName = prompt.replace(/[()[\]]/g, "").trim();

  if (isCount) {
    return `${measureName} =\nCOUNTROWS(\n    FILTER(\n        '${tableName}',\n        ${tableName}[${filterColumn}] = "${filterValue}"\n    )\n)`;
  }

  // For sum/revenue type measures
  const table = ctx.tableMap[tableName.toLowerCase()];
  const amountCol = table?.columns.find(c => /amount_total|totalprice/i.test(c)) || "amount_total";

  return `${measureName} =\nCALCULATE(\n    SUM(${tableName}[${amountCol}]),\n    ${tableName}[${filterColumn}] = "${filterValue}"\n)`;
}
