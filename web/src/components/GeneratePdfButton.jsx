"use client";

import { useState } from "react";
import toast from "react-hot-toast";

export default function GeneratePdfButton({ payload, disabled }) {
  const [isGenerating, setIsGenerating] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [abortController, setAbortController] = useState(null);

  const buttonDisabled = disabled || isGenerating;

  const handleGenerate = async () => {
    if (buttonDisabled) return;

    const flask = (process.env.NEXT_PUBLIC_FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
    const controller = new AbortController();
    try {
      setIsGenerating(true);
      setAbortController(controller);
      setStatusMessage("Enriching documentation with AI... (this takes ~3 minutes)");

      const trimHugeStrings = (value, key = "") => {
        // Never truncate Power Query M expressions — required for source type detection.
        if (key === "power_query" || key === "Expression" || key === "expression") {
          return value;
        }
        if (typeof value === "string") return value.length > 500 ? value.slice(0, 500) : value;
        if (Array.isArray(value)) return value.map((v) => trimHugeStrings(v, key));
        if (value && typeof value === "object") {
          return Object.fromEntries(
            Object.entries(value).map(([k, v]) => [k, trimHugeStrings(v, k)])
          );
        }
        return value;
      };

      const payloadForPdf = trimHugeStrings({
        filename: String(payload?.filename || "documentation.pbix"),
        sources: payload?.sources || [],
        tables: payload?.tables || [],
        columns: payload?.columns || {},
        relationships: payload?.relationships || [],
        measures: payload?.measures || [],
        calculated_columns: payload?.calculated_columns || [],
        rls: payload?.rls || [],
        parameters: payload?.parameters || [],
        schema: payload?.schema || [],
        statistics: payload?.statistics || [],
        power_query: payload?.power_query || [],
        table_roles: payload?.table_roles || [],
      });

      const response = await fetch(`${flask}/documentation/generate-pdf`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payloadForPdf),
        signal: controller.signal,
      });

      if (!response.ok) {
        let serverMessage = `Failed to generate PDF (${response.status})`;
        try {
          const body = await response.json();
          serverMessage = body?.error || serverMessage;
        } catch {
          // Ignore JSON parse errors and keep default message.
        }
        throw new Error(serverMessage);
      }
      if (!response.body) {
        throw new Error("Streaming response is not available in this browser.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let receivedComplete = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() || "";

        for (const chunk of chunks) {
          const line = chunk
            .split("\n")
            .map((item) => item.trim())
            .find((item) => item.startsWith("data: "));
          if (!line) continue;

          let event;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }

          if (event.type === "progress") {
            setStatusMessage(event.message || "Generating...");
          } else if (event.type === "step_done") {
            setStatusMessage("Generating PDF...");
          } else if (event.type === "complete") {
            receivedComplete = true;
            setStatusMessage("Downloading generated PDF...");

            const b64 = String(event.pdf_base64 || "");
            const binary = window.atob(b64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
            const blob = new Blob([bytes], { type: "application/pdf" });

            const fallbackName = `${String(payload?.filename || "documentation").replace(/\.pbix$/i, "")}_documentation.pdf`;
            const filename = event.filename || fallbackName;
            const blobUrl = window.URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = blobUrl;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.URL.revokeObjectURL(blobUrl);

            toast.success("AI-enriched PDF generated and downloaded.");
            setStatusMessage("Done.");
          } else if (event.type === "error") {
            throw new Error(event.message || "Generation failed.");
          }
        }
      }

      if (!receivedComplete) {
        throw new Error("Generation stream ended before a PDF was produced.");
      }
    } catch (error) {
      const message =
        error?.name === "AbortError"
          ? "PDF generation was cancelled or timed out. Please try again."
          : error instanceof Error
            ? error.message
            : "Unexpected error while generating PDF.";
      toast.error(message);
      setStatusMessage(message);
    } finally {
      setAbortController(null);
      setIsGenerating(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 8 }}>
      <button
        type="button"
        onClick={handleGenerate}
        disabled={buttonDisabled}
        style={{
          alignSelf: "flex-end",
          border: "none",
          borderRadius: 10,
          padding: "10px 16px",
          fontSize: 13,
          fontWeight: 700,
          cursor: buttonDisabled ? "not-allowed" : "pointer",
          background: buttonDisabled ? "var(--pbi-border)" : "#4361ee",
          color: buttonDisabled ? "var(--pbi-muted)" : "#ffffff",
          transition: "all 120ms ease",
        }}
      >
        {isGenerating ? "Generating PDF..." : "Generate Full AI-Enriched PDF"}
      </button>

      {isGenerating ? (
        <button
          type="button"
          onClick={() => abortController?.abort()}
          style={{
            alignSelf: "flex-end",
            border: "1px solid var(--pbi-border)",
            borderRadius: 10,
            padding: "8px 14px",
            fontSize: 12,
            fontWeight: 700,
            cursor: "pointer",
            background: "var(--pbi-surface-alt)",
            color: "var(--pbi-muted)",
          }}
        >
          Cancel generation
        </button>
      ) : null}

      {isGenerating && (
        <div className="card" style={{ padding: 12, background: "var(--pbi-surface-alt)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span
              style={{
                width: 14,
                height: 14,
                borderRadius: "50%",
                border: "2px solid #cbd5e1",
                borderTopColor: "#4361ee",
                animation: "spin 1s linear infinite",
                display: "inline-block",
              }}
            />
            <div>
              <div style={{ fontWeight: 700 }}>Generating AI-enriched PDF</div>
              <div style={{ color: "var(--pbi-muted)", fontSize: 13 }}>{statusMessage || "Working..."}</div>
              <div style={{ color: "var(--pbi-muted)", fontSize: 12, marginTop: 2 }}>Estimated time remaining: about 3 minutes.</div>
            </div>
          </div>
          <style>{`@keyframes spin { from { transform: rotate(0deg);} to { transform: rotate(360deg);} }`}</style>
        </div>
      )}

      {!isGenerating && statusMessage ? (
        <div className="muted" style={{ textAlign: "right" }}>
          {statusMessage}
        </div>
      ) : null}
    </div>
  );
}
