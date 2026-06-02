/**
 * Merge compact story stats with measures/columns so focus validation and the LLM
 * see report-relevant names (e.g. "Sales per commercial" ↔ fact_sale2, measures).
 */
export function buildStoryInputContext(pbixContext) {
  if (!pbixContext) return null;

  const base =
    pbixContext.storyContext && typeof pbixContext.storyContext === "object"
      ? { ...pbixContext.storyContext }
      : {};

  const tables = base.tables?.length ? base.tables : pbixContext.tables || [];
  const measures = [
    ...(Array.isArray(base.measures) ? base.measures : []),
    ...(Array.isArray(pbixContext.measures) ? pbixContext.measures : []),
  ].filter(Boolean);
  const uniqueMeasures = [...new Set(measures)];

  const columnNames = [];
  const seenColumns = new Set();
  for (const name of base.column_names || []) {
    const key = String(name || "").trim();
    if (!key || seenColumns.has(key)) continue;
    seenColumns.add(key);
    columnNames.push(key);
  }
  for (const cols of Object.values(pbixContext.columns || {})) {
    for (const col of cols || []) {
      const key = String(col || "").trim();
      if (!key || seenColumns.has(key)) continue;
      seenColumns.add(key);
      columnNames.push(key);
    }
  }

  return {
    ...base,
    tables,
    measures: uniqueMeasures.slice(0, 80),
    column_names: columnNames.slice(0, 60),
  };
}
