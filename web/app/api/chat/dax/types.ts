export type ModelTableForPrompt = {
  name: string;
  columns: string[];
  measures: string[];
};

export type DaxModeRequest = {
  prompt: string;
  model?: string;
  pbixPath?: string;
  pbixContext?: string;
  mcpContext?: Partial<SemanticModelContext> | null;
};

export type SemanticModelContext = {
  source: "mcp" | "mock";
  tables: string[];
  columns: Record<string, string[]>;
  measures: string[];
  relationships: string[];
  modelTables?: ModelTableForPrompt[];
};

export type ReducedTable = {
  name: string;
  columns: string[];
  numericColumns: string[];
  booleanColumns: string[];
  isDateTable: boolean;
};

export type ReducedDaxContext = {
  source: "mcp" | "mock";
  tables: ReducedTable[];
  tableMap: Record<string, ReducedTable>;
  dateTables: string[];
  factTables: string[];
  relationships: string[];
  measures: string[];
  allColumnRefs: Set<string>;
};

export type ParsedIntent = {
  kind: "ratio" | "count" | "ytd" | "yoy" | "rolling" | "unknown";
  metric: string;
  domain: "hr" | "finance" | "generic";
};

// Simplified — no longer needed for template rendering,
// but kept for future use (e.g. fallback if LLM is unavailable)
export type MetricPlan =
  | {
      status: "ready";
      strategy: "numeric_ratio" | "boolean_ratio";
      measureName: string;
      numeratorRef?: string;
      denominatorRef?: string;
      booleanRef?: string;
      denominatorTable?: string;
      domain: "hr" | "finance" | "generic";
      notes: string[];
    }
  | {
      status: "insufficient_context";
      reason: string;
    };

export type DaxValidationResult = {
  ok: boolean;
  issues: string[];
  refs: string[];
};