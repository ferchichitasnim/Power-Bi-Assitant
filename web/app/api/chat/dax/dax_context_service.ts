// dax_context_service.ts — unchanged
import { ReducedDaxContext, ReducedTable, SemanticModelContext } from "./types";

function isDateTableName(name: string) {
  return /\b(date|calendar|time|dimdate)\b/i.test(name);
}

function isBooleanColumnName(name: string) {
  return /^(is|has)[A-Z_]|^(is|has)\b|flag|active|enabled|newhire|new_hire|isnewhire/i.test(name);
}

function isNumericColumnName(name: string) {
  return /\b(amount|total|count|qty|quantity|sales|revenue|cost|price|hire|hiring|active|headcount|rate)\b/i.test(name);
}

function quoteRef(table: string, column: string) {
  return `'${table}'[${column}]`;
}

function bareColumnRef(table: string, column: string) {
  return `${table.toLowerCase()}[${column.toLowerCase()}]`;
}

export function buildReducedDaxContext(model: SemanticModelContext): ReducedDaxContext {
  const tables: ReducedTable[] = [];
  const tableMap: Record<string, ReducedTable> = {};
  const allColumnRefs = new Set<string>();

  const tableNames =
    model.modelTables && model.modelTables.length > 0
      ? model.modelTables.map((t) => t.name)
      : model.tables;

  for (const table of tableNames) {
    const fromModelTable = model.modelTables?.find((t) => t.name === table);
    const cols = fromModelTable?.columns?.length
      ? fromModelTable.columns
      : model.columns[table] || [];
    const reduced: ReducedTable = {
      name: table,
      columns: cols,
      numericColumns: cols.filter(isNumericColumnName),
      booleanColumns: cols.filter(isBooleanColumnName),
      isDateTable: isDateTableName(table),
    };
    tables.push(reduced);
    tableMap[table.toLowerCase()] = reduced;
    for (const col of cols) {
      allColumnRefs.add(quoteRef(table, col).toLowerCase());
      allColumnRefs.add(bareColumnRef(table, col));
    }
  }

  const factTables = tables
    .filter((t) => !t.isDateTable)
    .sort((a, b) => b.numericColumns.length - a.numericColumns.length)
    .map((t) => t.name);
  const dateTables = tables.filter((t) => t.isDateTable).map((t) => t.name);

  return {
    source: model.source,
    tables,
    tableMap,
    dateTables,
    factTables,
    relationships: model.relationships,
    measures: model.measures,
    allColumnRefs,
  };
}