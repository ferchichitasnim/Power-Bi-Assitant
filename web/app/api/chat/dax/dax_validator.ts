import { DaxValidationResult, ParsedIntent, ReducedDaxContext } from "./types";

function log(stage: string, data: unknown) {
  console.log(`\n[DAX Validator][${stage}]`, JSON.stringify(data, null, 2));
}

function hasBalancedParentheses(s: string) {
  let depth = 0;
  for (const ch of s) {
    if (ch === "(") depth += 1;
    if (ch === ")") depth -= 1;
    if (depth < 0) return false;
  }
  return depth === 0;
}

export function validateDax(dax: string, intent: ParsedIntent, ctx: ReducedDaxContext): DaxValidationResult {
  const raw = (dax || "").trim();
  const issues: string[] = [];

  log("input", { dax: raw, intent, measures: ctx.measures });

  if (!raw) {
    issues.push("Empty DAX formula.");
    return { ok: false, issues, refs: [] };
  }

  if (!raw.includes("=")) issues.push("Missing measure assignment.");
  if (/[A-Za-z0-9 _]+\s*=\s*[A-Za-z0-9 _]+\s*=/i.test(raw)) issues.push("Repeated assignment detected.");
  if (/SUMX\s*\(\s*[^,()]+\s*,\s*1\s*\)/i.test(raw)) issues.push("Useless SUMX(Table,1) detected.");
  if (!hasBalancedParentheses(raw)) issues.push("Unbalanced parentheses.");
  if (/\bVAR\b/i.test(raw) && !/\bRETURN\b/i.test(raw)) issues.push("Broken VAR/RETURN block.");

  // Build a set of known measure names (lowercase) for reference checking
  // Flask returns measures as "Employee[New Hires]" — extract bare names too
  const knownMeasures = new Set<string>();
  for (const m of ctx.measures) {
    knownMeasures.add(m.toLowerCase());
    // Extract bare name from "Table[MeasureName]" format
    const match = m.match(/^[^[]*\[([^\]]+)\]$/);
    if (match) {
      knownMeasures.add(match[1].trim().toLowerCase());
    }
  }

  // Check column references: 'Table'[Column]
  const refs = Array.from(raw.matchAll(/'([^']+)'\[([^\]]+)\]/g));
  for (const m of refs) {
    const tableName = m[1].trim();
    const colName = m[2].trim();
    const ref = `'${tableName.toLowerCase()}'[${colName.toLowerCase()}]`;
    const bareRef = `${tableName.toLowerCase()}[${colName.toLowerCase()}]`;

    if (!ctx.allColumnRefs.has(ref) && !ctx.allColumnRefs.has(bareRef)) {
      const measureOnTable = knownMeasures.has(`${tableName.toLowerCase()}[${colName.toLowerCase()}]`.toLowerCase());
      if (!knownMeasures.has(colName.toLowerCase()) && !measureOnTable) {
        issues.push(`Unknown field reference: '${tableName}'[${colName}].`);
        log("unknown_ref", { ref, knownColumns: Array.from(ctx.allColumnRefs).slice(0, 10), knownMeasures: ctx.measures });
      }
    }
  }

  // Check unquoted column references: Table[Column]
  const bareTableRefs = Array.from(raw.matchAll(/(?<!')\b([A-Za-z_][\w]*)\[([^\]]+)\]/g));
  for (const m of bareTableRefs) {
    const tableName = m[1].trim();
    const colName = m[2].trim();
    const bareRef = `${tableName.toLowerCase()}[${colName.toLowerCase()}]`;
    const quotedRef = `'${tableName.toLowerCase()}'[${colName.toLowerCase()}]`;

    if (!ctx.allColumnRefs.has(bareRef) && !ctx.allColumnRefs.has(quotedRef)) {
      if (!knownMeasures.has(colName.toLowerCase())) {
        issues.push(`Unknown field reference: ${tableName}[${colName}].`);
        log("unknown_bare_table_ref", { bareRef, knownMeasures: ctx.measures });
      }
    }
  }

  // Check bare measure references: [MeasureName] (without table prefix)
  const bareMeasureRefs = Array.from(raw.matchAll(/(?<!'[^']*)\[([^\]]+)\]/g));
  for (const m of bareMeasureRefs) {
    const name = m[1].trim();
    // Skip if it's part of a 'Table'[Column] reference (already checked above)
    const fullMatch = m[0];
    const idx = m.index || 0;
    const before = raw.slice(Math.max(0, idx - 30), idx);
    if (/'[^']*$/.test(before)) continue; // Part of 'Table'[Col]

    if (!knownMeasures.has(name.toLowerCase())) {
      // Check if it's a column in any table
      const isColumn = ctx.allColumnRefs.has(name.toLowerCase());
      if (!isColumn) {
        // Only warn, don't block — the LLM might be defining a new measure
        log("unknown_bare_ref", { name, knownMeasures: ctx.measures });
      }
    }
  }

  // Domain-specific checks
  if (intent.domain === "hr") {
    const dateNames = ctx.dateTables.map((x) => x.toLowerCase());
    if (dateNames.some((dt) => new RegExp(`COUNTROWS\\s*\\(\\s*'\\s*${dt.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*'\\s*\\)`, "i").test(raw))) {
      issues.push("Date table used as denominator in HR metric.");
    }
  }

  log("result", { ok: issues.length === 0, issues });

  return {
    ok: issues.length === 0,
    issues,
    refs: refs.map((m) => `'${m[1]}'[${m[2]}]`),
  };
}