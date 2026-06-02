"use client";

import { Copy, Download } from "lucide-react";
import ReactMarkdown from "react-markdown";
import toast from "react-hot-toast";
import SectionCard from "./SectionCard";

function extractSection(md, headingPattern) {
  const pattern = new RegExp(
    `#{1,6}\\s*(?:${headingPattern})\\s*([\\s\\S]*?)(?=\\n\\s*#{1,6}\\s+|$)`,
    "i"
  );
  const match = md.match(pattern);
  return match ? match[1].trim() : "";
}

export default function StoryOutput({ content, loading }) {
  const parsed = {
    overview: extractSection(content, "Overview|Executive Summary"),
    insights: extractSection(content, "Key Insights?|Insights?"),
    risks: extractSection(content, "Risks?(?:\\s*(?:or|and)\\s*Data\\s*Quality\\s*Concerns?)?|Data\\s*Quality\\s*Concerns?"),
    actions: extractSection(content, "Recommended Actions?|Actions?|Next Steps?"),
  };
  const hasAnySection = Object.values(parsed).some(Boolean);
  const sections = hasAnySection ? parsed : { ...parsed, overview: (content || "").trim() };

  const copyText = async () => {
    await navigator.clipboard.writeText(content || "");
    toast.success("Story copied");
  };

  const download = (ext) => {
    const blob = new Blob([content || ""], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `pbix-story.${ext}`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Story Output</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="button" style={{ background: "var(--pbi-surface-alt)", color: "var(--pbi-text)" }} onClick={copyText} disabled={!content}>
            <Copy size={14} />
          </button>
          <button
            className="button"
            style={{ background: "var(--pbi-surface-alt)", color: "var(--pbi-text)" }}
            onClick={() => download("md")}
            disabled={!content}
          >
            <Download size={14} /> .md
          </button>
          <button
            className="button"
            style={{ background: "var(--pbi-surface-alt)", color: "var(--pbi-text)" }}
            onClick={() => download("txt")}
            disabled={!content}
          >
            <Download size={14} /> .txt
          </button>
        </div>
      </div>

      {loading && !content && (
        <div className="card" style={{ padding: 12, background: "var(--pbi-surface-alt)" }}>
          <div className="muted">Generating story with Ollama…</div>
          <div style={{ height: 6, marginTop: 8, background: "#dfe7f4", borderRadius: 999 }} />
        </div>
      )}

      {!loading && !content && (
        <p className="muted" style={{ margin: 0, fontSize: 14 }}>
          Generated narrative will appear here. If nothing shows after clicking Generate, check the error message above
          (often Ollama memory or connection).
        </p>
      )}

      {content && (
        <div className="section-grid">
          <SectionCard title="Overview" icon="🧭" color="var(--pbi-purple)">
            <ReactMarkdown>{sections.overview || "No content."}</ReactMarkdown>
          </SectionCard>
          <SectionCard title="Key Insights" icon="💡" color="var(--pbi-yellow)">
            <ReactMarkdown>{sections.insights || "No content."}</ReactMarkdown>
          </SectionCard>
          <SectionCard title="Risks" icon="⚠️" color="#E74C3C">
            <ReactMarkdown>{sections.risks || "No content."}</ReactMarkdown>
          </SectionCard>
          <SectionCard title="Recommended Actions" icon="✅" color="var(--pbi-success)">
            <ReactMarkdown>{sections.actions || "No content."}</ReactMarkdown>
          </SectionCard>
        </div>
      )}

      {loading && (
        <div style={{ marginTop: 10, fontSize: 13 }} className="muted">
          Streaming... <span style={{ animation: "blink 1s infinite" }}>|</span>
        </div>
      )}
    </div>
  );
}
