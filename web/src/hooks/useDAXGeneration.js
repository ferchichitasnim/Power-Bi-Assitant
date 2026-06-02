"use client";

import { useCallback, useRef, useState } from "react";

const DAX_DEBUG = process.env.NEXT_PUBLIC_DAX_DEBUG === "1";

function daxClientLog(phase, data) {
  const line = { phase, ...data, t: typeof performance !== "undefined" ? Math.round(performance.now()) : 0 };
  if (DAX_DEBUG) {
    console.debug("[dax:client]", line);
  }
}

export function parseDaxSections(full) {
  const text = full || "";

  // Match DAX code block — supports both heading styles
  const daxMatch = text.match(
    /##\s*DAX Measure\s*([\s\S]*?)(?=##\s*(?:Logic Explanation|How it works)\b|$)/i
  );

  // Match explanation — supports both "Logic Explanation" and "How it works"
  const logicMatch = text.match(
    /##\s*(?:Logic Explanation|How it works)\s*([\s\S]*?)(?=##\s*(?:Suggested Improvements|Suggestions?\s*(?:&|and)\s*Variants?)\b|$)/i
  );

  // Match suggestions — supports both "Suggested Improvements" and "Suggestions & Variants"
  const sugMatch = text.match(
    /##\s*(?:Suggested Improvements|Suggestions?\s*(?:&|and)\s*Variants?)\s*([\s\S]*?)(?=_Context source:|$)/i
  );

  let daxCode = (daxMatch?.[1] || "").trim();
  daxCode = daxCode.replace(/^```[\w]*\s*/i, "").replace(/```\s*$/i, "").trim();

  const explanation = (logicMatch?.[1] || "").trim();
  const suggestions = (sugMatch?.[1] || "").trim();

  if (!daxCode && !explanation && !suggestions && text.trim()) {
    const codeFence = text.match(/```(?:dax|sql)?\s*([\s\S]*?)```/i);
    if (codeFence?.[1]?.trim()) {
      return { daxCode: codeFence[1].trim(), explanation: text.trim(), suggestions: "" };
    }
    return { daxCode: text.trim(), explanation: "", suggestions: "" };
  }

  return { daxCode, explanation, suggestions };
}

function parseSSEDataLine(line) {
  return line;
}

export default function useDAXGeneration() {
  const [rawText, setRawText] = useState("");
  const [daxCode, setDaxCode] = useState("");
  const [explanation, setExplanation] = useState("");
  const [suggestions, setSuggestions] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const abortRef = useRef(null);

  const applyParsed = useCallback((full) => {
    const p = parseDaxSections(full);
    setDaxCode(p.daxCode);
    setExplanation(p.explanation);
    setSuggestions(p.suggestions);
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setRawText("");
    setDaxCode("");
    setExplanation("");
    setSuggestions("");
    setError(null);
    setIsLoading(false);
  }, []);

  const generate = useCallback(
    async ({ query, context = "", model = "llama3.2:3b", pbixContext = "", mcpContext = null, modelTables = null }) => {
      const q = (query || "").trim();
      if (!q) {
        setError(new Error("Enter a description first."));
        return null;
      }

      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const timeoutMs = Number.parseInt(process.env.NEXT_PUBLIC_DAX_CLIENT_TIMEOUT_MS || "90000", 10);
      const timeoutId = setTimeout(() => ctrl.abort("client-timeout"), Number.isNaN(timeoutMs) ? 90000 : timeoutMs);

      setRawText("");
      setDaxCode("");
      setExplanation("");
      setSuggestions("");
      setError(null);
      setIsLoading(true);

      const t0 = typeof performance !== "undefined" ? performance.now() : 0;
      let lastMark = t0;

      const mark = (phase, extra = {}) => {
        const now = typeof performance !== "undefined" ? performance.now() : 0;
        const deltaMs = Math.round(now - lastMark);
        const sinceStartMs = Math.round(now - t0);
        lastMark = now;
        console.info("[dax:client]", phase, { deltaMs, sinceStartMs, ...extra });
        daxClientLog(phase, { deltaMs, sinceStartMs, ...extra });
      };

      mark("1_fetch_start", { url: "/api/chat (mode=dax)" });

      let acc = "";
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: "dax",
            prompt: q,
            daxContext: context.trim(),
            model,
            pbixContext: typeof pbixContext === "string" ? pbixContext : "",
            mcpContext: modelTables?.length
              ? { ...(mcpContext || {}), modelTables }
              : mcpContext,
          }),
          signal: ctrl.signal,
        });

        mark("2_fetch_response_headers", { status: res.status, ok: res.ok });

        if (!res.ok) {
          const errText = await res.text();
          let message = errText || `HTTP ${res.status}`;
          try {
            const parsed = JSON.parse(errText);
            if (parsed?.error && typeof parsed.error === "string") {
              message = parsed.error;
            }
          } catch {
            // Keep raw text fallback.
          }
          throw new Error(message);
        }

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let chunkEvents = 0;

        let firstReadBytes = true;
        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            mark("7_reader_done", { chunkEvents, accChars: acc.length });
            break;
          }
          if (firstReadBytes && value?.byteLength) {
            firstReadBytes = false;
            mark("3_first_raw_bytes_from_api", { bytes: value.byteLength });
          }
          if (DAX_DEBUG && value?.byteLength) {
            daxClientLog("raw_chunk", { bytes: value.byteLength });
          }
          const textChunk = decoder.decode(value, { stream: true });
          if (textChunk) {
            chunkEvents += 1;
            if (chunkEvents === 1) {
              mark("5_first_text_chunk", { textLen: textChunk.length });
            }
            acc += parseSSEDataLine(textChunk);
            setRawText(acc);
            applyParsed(acc);
          }
        }

        if (!acc.trim()) {
          throw new Error("No DAX output was returned. Try again or switch to a smaller Ollama model.");
        }

        applyParsed(acc);
        const parsed = parseDaxSections(acc);
        mark("8_parse_complete", { totalChars: acc.length, chunkEvents });
        return { rawText: acc, ...parsed };
      } catch (e) {
        const reason = String(ctrl.signal?.reason || "");
        const errName = String(e?.name || "");
        const errMessage = String(e?.message || e || "");
        const isAbortLike =
          errName === "AbortError" ||
          errName === "TimeoutError" ||
          reason.includes("client-timeout") ||
          errMessage.includes("client-timeout") ||
          errMessage.includes("The operation was aborted");

        if (isAbortLike) {
          const timedOut =
            reason.includes("client-timeout") ||
            errName === "TimeoutError" ||
            errMessage.includes("client-timeout");

          if (timedOut) {
            // If we already rendered partial output, avoid masking it with a hard error.
            if (!acc.trim()) {
              setError(new Error("DAX generation timed out. Try a shorter prompt or a smaller Ollama model."));
            }
          } else {
            console.info("[dax:client]", "aborted");
          }
          return null;
        }
        console.info("[dax:client]", "error", { message: String(e) });
        setError(e instanceof Error ? e : new Error(String(e)));
        return null;
      } finally {
        clearTimeout(timeoutId);
        setIsLoading(false);
        abortRef.current = null;
      }
    },
    [applyParsed]
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return {
    rawText,
    daxCode,
    explanation,
    suggestions,
    isLoading,
    error,
    generate,
    reset,
    stop,
  };
}