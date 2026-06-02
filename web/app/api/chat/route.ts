import { createOpenAI } from "@ai-sdk/openai";
import type { CoreMessage } from "ai";
import { streamText } from "ai";
import { runMcpFirstDaxPipeline } from "./dax/dax_pipeline";

const STORY_RULES = `You are a senior Power BI analytics storyteller.
Create a concise narrative for a business stakeholder using ONLY the provided context.
Output format rules:
1) Use exactly these markdown headings:
## Overview
## Key Insights
## Risks or Data Quality Concerns
## Recommended Actions
2) Under each heading use 3-6 bullet points.
3) Be concrete with table/column names from the context.
4) Do not invent metrics or percentages not implied by the context.`;

function truncateText(input: string, maxChars: number) {
  if (!input || maxChars <= 0 || input.length <= maxChars) return input;
  return `${input.slice(0, maxChars)}\n\n[... truncated by server for faster response ...]`;
}

function timeoutSignal(ms: number): AbortSignal | undefined {
  if (!Number.isFinite(ms) || ms <= 0) return undefined;
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(ms);
  }
  return undefined;
}

export async function POST(req: Request) {
  let body: {
    messages?: { role: string; content: string }[];
    prompt?: string;
    pbixPath?: string;
    context?: Record<string, unknown>;
    pbixContext?: string;
    mcpContext?: {
      tables?: string[];
      columns?: Record<string, string[]>;
      measures?: string[];
      relationships?: string[];
      modelTables?: { name: string; columns: string[]; measures: string[] }[];
    };
    daxContext?: string;
    mode?: string;
    model?: string;
  };
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const pbixPath = (body.pbixPath || "").trim();
  const mode = (body.mode || "story").trim().toLowerCase();
  const model = (body.model || process.env.OLLAMA_MODEL || "llama3.2:3b").trim();
  let messages = (body.messages || []) as CoreMessage[];
  const prompt = typeof body.prompt === "string" ? body.prompt.trim() : "";
  if (messages.length === 0 && prompt) {
    messages = [{ role: "user", content: prompt }];
  }
  const pbixContext = typeof body.pbixContext === "string" ? body.pbixContext.trim() : "";
  const ollamaBase = (process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434").replace(/\/$/, "");
  const ollama = createOpenAI({
    baseURL: `${ollamaBase}/v1`,
    apiKey: "ollama",
  });

  if (mode === "dax") {
    if (messages.length === 0 && !prompt) {
      return new Response(JSON.stringify({ error: "prompt is required for dax mode" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    const userPrompt = prompt || (typeof messages[0]?.content === "string" ? messages[0].content : "");
    const pipeline = await runMcpFirstDaxPipeline({
      prompt: userPrompt,
      model,
      pbixPath,
      pbixContext,
      mcpContext: body.mcpContext || null,
    });
    if (!pipeline.ok) {
      return new Response(JSON.stringify({ error: pipeline.error }), {
        status: pipeline.status,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(pipeline.payload, { status: 200, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }

  let context = body.context;
  if (!context) {
    if (!pbixPath) {
      return new Response(JSON.stringify({ error: "pbixPath is required" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
    const flaskUrl = (process.env.FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
    const ctxRes = await fetch(`${flaskUrl}/api/pbix/context?pbix_path=${encodeURIComponent(pbixPath)}`);
    const data = (await ctxRes.json()) as {
      ok?: boolean;
      error?: string;
      context?: Record<string, unknown>;
    };
    if (!data.ok || !data.context) {
      return new Response(JSON.stringify({ error: data.error || "Failed to load PBIX context" }), {
        status: ctxRes.ok ? 400 : ctxRes.status,
        headers: { "Content-Type": "application/json" },
      });
    }
    context = data.context;
  }

  if (messages.length === 0) {
    return new Response(
      JSON.stringify({
        error: "Missing prompt or messages. useCompletion sends `prompt`; useChat sends `messages`.",
      }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  }

  const storyMaxContextChars = Number.parseInt(process.env.STORY_MAX_CONTEXT_CHARS || "12000", 10);
  const contextJson = truncateText(JSON.stringify(context), Number.isNaN(storyMaxContextChars) ? 12000 : storyMaxContextChars);
  const system = `${STORY_RULES}\n\nContext JSON:\n${contextJson}`;

  try {
    const storyTimeoutMs = Number.parseInt(process.env.STORY_OLLAMA_TIMEOUT_MS || "120000", 10);
    const result = await streamText({
      model: ollama(model),
      system,
      messages,
      temperature: 0.2,
      maxTokens: 500,
      abortSignal: timeoutSignal(Number.isNaN(storyTimeoutMs) ? 120000 : storyTimeoutMs),
    });
    return result.toTextStreamResponse();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return new Response(
      JSON.stringify({
        error: `Story generation failed. ${message}. Check that Ollama is running and the selected model is available.`,
      }),
      { status: 504, headers: { "Content-Type": "application/json" } }
    );
  }
}
