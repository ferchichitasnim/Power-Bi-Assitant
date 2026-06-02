import { buildReducedDaxContext } from "./dax_context_service";
import { getMcpSemanticModelContext } from "./mcp_client";
import { validateDax } from "./dax_validator";
import { parseDaxIntent } from "./intent_parser";
import { DaxModeRequest, ReducedDaxContext, ParsedIntent } from "./types";
import { createOpenAI } from "@ai-sdk/openai";
import { generateText } from "ai";

// ─── Console logging helper ───────────────────────────────────
function log(stage: string, data: unknown) {
  console.log(`\n[DAX Pipeline][${stage}]`, JSON.stringify(data, null, 2));
}

// ─── Build the system prompt with REAL schema context ─────────
function buildDaxSystemPrompt(ctx: ReducedDaxContext, intent: ParsedIntent): string {
  // List all tables with their columns
  const schemaLines: string[] = [];
  for (const table of ctx.tables) {
    const cols = table.columns.map((c) => `    - ${c}`).join("\n");
    schemaLines.push(`  Table: '${table.name}'\n    Columns:\n${cols}`);
  }

  // List existing measures (critical — the LLM must know what already exists)
  // Flask returns "Employee[New Hires]" format — show as "[New Hires] (on Employee table)"
  let measuresSection: string;
  if (ctx.measures.length > 0) {
    const measureLines = ctx.measures.map((m) => {
      const match = m.match(/^([^[]*)\[([^\]]+)\]$/);
      if (match) {
        const table = match[1].trim();
        const name = match[2].trim();
        return table ? `  - [${name}] (on '${table}' table)` : `  - [${name}]`;
      }
      return `  - [${m}]`;
    });
    measuresSection = `\nExisting measures (reference these with [MeasureName] — do NOT recalculate them):\n${measureLines.join("\n")}`;
  } else {
    measuresSection = "\nNo existing measures found.";
  }

  // List relationships
  const relsSection =
    ctx.relationships.length > 0
      ? `\nRelationships:\n${ctx.relationships.map((r) => `  - ${r}`).join("\n")}`
      : "";

  return `You are a DAX formula expert for Microsoft Power BI.

## Your task
Generate a valid DAX measure based on the user's request and the semantic model below.

## Semantic model schema
${schemaLines.join("\n\n")}
${measuresSection}
${relsSection}

## CRITICAL RULES
1. ONLY reference tables and columns that exist in the schema above. NEVER invent tables or columns.
2. If a measure already exists (listed above), reference it with [MeasureName] instead of recalculating.
3. Use DIVIDE() instead of "/" to handle divide-by-zero safely.
4. Use proper DAX syntax: table names in single quotes, column names in square brackets.
5. The output must be ONLY the DAX formula. No markdown, no explanation, no code fences.
6. Format: MeasureName = <expression>
7. If the user's request CANNOT be answered with the tables and columns in this schema, respond with EXACTLY: ERROR: The requested metric cannot be calculated from the available data model. Available tables: [list the table names]. Do NOT invent columns or tables that are not listed above.

## Example output format
Hiring Rate =
DIVIDE(
    [New Hires],
    [Actives],
    0
)`;
}

// ─── Call the LLM to generate DAX ─────────────────────────────
async function callLlmForDax(
  systemPrompt: string,
  userPrompt: string,
  model: string
): Promise<{ dax: string; error?: string }> {
  const ollamaBase = (process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434").replace(/\/$/, "");

  log("LLM:config", { model, ollamaBase });
  log("LLM:system_prompt", systemPrompt);
  log("LLM:user_prompt", userPrompt);

  const ollama = createOpenAI({
    baseURL: `${ollamaBase}/v1`,
    apiKey: "ollama",
  });

  try {
    const result = await generateText({
      model: ollama(model),
      system: systemPrompt,
      prompt: userPrompt,
      temperature: 0.1, // Low temp for deterministic DAX
      maxTokens: 500,
    });

    const rawText = result.text.trim();
    log("LLM:raw_response", rawText);

    // Clean up: remove markdown code fences if the LLM adds them anyway
    let cleaned = rawText
      .replace(/^```(?:dax)?\s*/i, "")
      .replace(/\s*```$/i, "")
      .trim();

    // Fix dot notation -> bracket notation (e.g. 'Table.Column' -> Table[Column] or Table.Column -> Table[Column])
    cleaned = cleaned.replace(/'([^']+)\.([^']+)'/g, "$1[$2]");
    cleaned = cleaned.replace(/(\b[A-Za-z_]\w*)\.([A-Za-z_]\w*(?:\$)?)\b(?!\s*[.(])/g, "$1[$2]");

    return { dax: cleaned };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    log("LLM:error", message);
    return { dax: "", error: `LLM call failed: ${message}` };
  }
}

// ─── Build explanation from the generated DAX ─────────────────
function buildExplanation(dax: string, ctx: ReducedDaxContext): string {
  const lines: string[] = [];

  // Check what DAX functions are used
  if (/\bDIVIDE\b/i.test(dax)) lines.push("- Uses `DIVIDE()` for safe division (handles divide-by-zero).");
  if (/\bFILTER\b/i.test(dax)) lines.push("- Uses `FILTER()` to apply row-level conditions.");
  if (/\bCALCULATE\b/i.test(dax)) lines.push("- Uses `CALCULATE()` to modify filter context.");
  if (/\bCOUNTROWS\b/i.test(dax)) lines.push("- Uses `COUNTROWS()` to count rows in a table.");
  if (/\bSUM\b/i.test(dax)) lines.push("- Uses `SUM()` for aggregation.");
  if (/\bAVERAGE\b/i.test(dax)) lines.push("- Uses `AVERAGE()` for mean calculation.");
  if (/\bTOTALYTD\b/i.test(dax)) lines.push("- Uses `TOTALYTD()` for year-to-date accumulation.");
  if (/\bSAMEPERIODLASTYEAR\b/i.test(dax)) lines.push("- Uses `SAMEPERIODLASTYEAR()` for year-over-year comparison.");

  // Show which existing measures are referenced
  const measureRefs = ctx.measures.filter((m) => dax.includes(`[${m}]`));
  if (measureRefs.length > 0) {
    lines.push(`- References existing measures: ${measureRefs.map((m) => `\`[${m}]\``).join(", ")}.`);
  }

  if (lines.length === 0) lines.push("- Standard DAX measure.");

  lines.push("", "```dax", dax, "```");
  return lines.join("\n");
}

function buildSuggestions(dax: string, ctx: ReducedDaxContext): string {
  const suggestions: string[] = ["- Keep this as a base measure and reuse it in visuals."];

  if (ctx.dateTables.length > 0 && !/\bDate\b/i.test(dax)) {
    suggestions.push("- Consider adding a date filter variant for period-specific analysis.");
  }
  if (!/\bCALCULATE\b/i.test(dax)) {
    suggestions.push("- Wrap with `CALCULATE()` if you need to apply slicer-independent filters.");
  }
  return suggestions.join("\n");
}

// ─── Main pipeline ────────────────────────────────────────────
export async function runMcpFirstDaxPipeline(input: DaxModeRequest) {
  log("pipeline:start", {
    prompt: input.prompt,
    model: input.model,
    pbixPath: input.pbixPath,
    hasMcpContext: !!input.mcpContext,
    hasPbixContext: !!input.pbixContext,
  });

  // 1. Get semantic model context from MCP
  const modelContext = await getMcpSemanticModelContext(input);
  log("pipeline:model_context", {
    source: modelContext.source,
    tables: modelContext.tables,
    columns: modelContext.columns,
    measures: modelContext.measures,
  });

  // 2. Build reduced context
  const reduced = buildReducedDaxContext(modelContext);
  log("pipeline:reduced_context", {
    factTables: reduced.factTables,
    dateTables: reduced.dateTables,
    allColumnRefs: Array.from(reduced.allColumnRefs),
    measures: reduced.measures,
  });

  // 3. Parse intent (for validation hints, not for hardcoded logic)
  const intent = parseDaxIntent(input.prompt);
  log("pipeline:intent", intent);

  // 4. Build system prompt with schema context
  const systemPrompt = buildDaxSystemPrompt(reduced, intent);

  // 5. Call the LLM
  const model = input.model || process.env.OLLAMA_MODEL || "llama3.2:3b";
  const llmResult = await callLlmForDax(systemPrompt, input.prompt, model);

  if (llmResult.error || !llmResult.dax) {
    return {
      ok: false as const,
      status: 502,
      error: llmResult.error || "LLM returned empty response.",
    };
  }

  // 5b. Check if the LLM refused (it was told to respond with ERROR: if the request doesn't match the schema)
  if (llmResult.dax.startsWith("ERROR:")) {
    return {
      ok: false as const,
      status: 422,
      error: llmResult.dax,
    };
  }

  // 6. Validate the generated DAX against the schema
  const validation = validateDax(llmResult.dax, intent, reduced);
  log("pipeline:validation", validation);

  if (!validation.ok) {
    return {
      ok: false as const,
      status: 422,
      error: `Generated DAX references fields not in your data model: ${validation.issues.join(" | ")}`,
    };
  }

  // 7. Format the output
  const explanation = buildExplanation(llmResult.dax, reduced);
  const suggestions = buildSuggestions(llmResult.dax, reduced);

  const payload = `## DAX Measure
\`\`\`dax
${llmResult.dax}
\`\`\`

## How it works
${explanation}

## Suggestions & Variants
${suggestions}

_Context source: ${reduced.source.toUpperCase()}_`;

  log("pipeline:done", { daxLength: llmResult.dax.length, source: reduced.source });

  return {
    ok: true as const,
    status: 200,
    payload,
  };
}