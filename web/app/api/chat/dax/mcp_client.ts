import { DaxModeRequest, ModelTableForPrompt, SemanticModelContext } from "./types";

// ─── Console logging helper ───────────────────────────────────
function log(stage: string, data: unknown) {
  console.log(`\n[MCP Client][${stage}]`, JSON.stringify(data, null, 2));
}

function modelTablesFromPayload(raw: Partial<SemanticModelContext>): ModelTableForPrompt[] {
  if (!Array.isArray(raw.modelTables) || raw.modelTables.length === 0) return [];
  return raw.modelTables
    .map((t) => ({
      name: String(t?.name || "").trim(),
      columns: Array.isArray(t?.columns) ? t.columns.map((c) => String(c).trim()).filter(Boolean) : [],
      measures: Array.isArray(t?.measures) ? t.measures.map((m) => String(m).trim()).filter(Boolean) : [],
    }))
    .filter((t) => t.name);
}

function normalizeContext(raw: Partial<SemanticModelContext>, source: "mcp" | "mock"): SemanticModelContext {
  const modelTables = modelTablesFromPayload(raw);

  let tables = Array.isArray(raw.tables) ? raw.tables.map((x) => String(x).trim()).filter(Boolean) : [];
  const columnsObj = raw.columns && typeof raw.columns === "object" ? raw.columns : {};
  let columns: Record<string, string[]> = {};
  for (const [table, cols] of Object.entries(columnsObj || {})) {
    if (!Array.isArray(cols)) continue;
    const clean = cols.map((c) => String(c).trim()).filter(Boolean);
    if (clean.length) columns[String(table).trim()] = clean;
  }
  let measures = Array.isArray(raw.measures) ? raw.measures.map((x) => String(x).trim()).filter(Boolean) : [];
  const relationships = Array.isArray(raw.relationships)
    ? raw.relationships.map((x) => String(x).trim()).filter(Boolean)
    : [];

  if (modelTables.length > 0) {
    if (!tables.length) tables = modelTables.map((t) => t.name);
    for (const t of modelTables) {
      if (t.columns.length && !columns[t.name]?.length) {
        columns[t.name] = t.columns;
      }
    }
    if (!measures.length) {
      measures = modelTables.flatMap((t) =>
        t.measures.map((m) => (t.name ? `${t.name}[${m}]` : m))
      );
    }
  }

  log("normalizeContext", {
    source,
    tables,
    columnCount: Object.keys(columns).length,
    measures,
    relationships,
    modelTableCount: modelTables.length,
  });
  return { source, tables, columns, measures, relationships, modelTables };
}

export async function getMcpSemanticModelContext(input: DaxModeRequest): Promise<SemanticModelContext> {
  // ── 1. Try the context passed directly in the request payload ──
  if (input.mcpContext) {
    log("source:payload", "Trying mcpContext from request body");
    const fromPayload = normalizeContext(input.mcpContext, "mcp");
    if (fromPayload.tables.length > 0 || (fromPayload.modelTables?.length ?? 0) > 0) {
      log("source:payload", "SUCCESS — using mcpContext from request body");
      return fromPayload;
    }
    log("source:payload", "EMPTY — mcpContext had no tables, trying Flask fallback");
  }

  // ── 2. Try fetching from Flask backend (PBIXRay MCP) ──
  if (input.pbixPath?.trim()) {
    const flaskUrl = (process.env.FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
    const url = `${flaskUrl}/api/pbix/context?pbix_path=${encodeURIComponent(input.pbixPath.trim())}`;
    log("source:flask", `Fetching from ${url}`);

    try {
      const res = await fetch(url);
      if (res.ok) {
        const data = (await res.json()) as {
          ok?: boolean;
          tables?: string[];
          columns?: Record<string, string[]>;
          measures?: string[];
          relationships?: string[];
        };
        log("source:flask:response", {
          ok: data.ok,
          tables: data.tables,
          columnTables: data.columns ? Object.keys(data.columns) : [],
          measures: data.measures,
        });

        if (data.ok) {
          const normalized = normalizeContext(
            {
              tables: data.tables,
              columns: data.columns,
              measures: data.measures,
              relationships: data.relationships,
            },
            "mcp"
          );
          if (normalized.tables.length > 0) {
            log("source:flask", "SUCCESS — using Flask/MCP context");
            return normalized;
          }
        }
      } else {
        log("source:flask:error", { status: res.status, statusText: res.statusText });
      }
    } catch (err) {
      log("source:flask:error", { message: err instanceof Error ? err.message : String(err) });
    }
  }

  // ── 3. NO MORE MOCK FALLBACK ──
  // The old code had a buildMockContext() that invented fake columns like "isNewHire".
  // This caused the pipeline to generate DAX referencing columns that don't exist.
  // Instead, we throw a clear error so the user knows MCP is not connected.
  const errorMsg = [
    "Could not retrieve semantic model context.",
    input.pbixPath ? `Flask backend at ${process.env.FLASK_URL || "http://127.0.0.1:5052"} did not return valid data.` : "No pbixPath provided.",
    "Make sure: (1) Flask backend is running, (2) PBIXRay MCP server is connected, (3) the PBIX file path is correct.",
  ].join(" ");

  log("source:FAILED", errorMsg);
  throw new Error(errorMsg);
}