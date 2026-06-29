"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Zap } from "lucide-react";
import ModelSelector from "../ModelSelector";
import EmptyFileState from "../shared/EmptyFileState";
import { usePBIX } from "../../context/PBIXContext";
import useOllamaModels from "../../hooks/useOllamaModels";
import useDAXGeneration from "../../hooks/useDAXGeneration";
import NaturalLanguageInput from "./NaturalLanguageInput";
import GenerateDAXButton from "./GenerateDAXButton";
import DAXOutputCard from "./DAXOutputCard";
import Header from "../Header";

export default function DAXGeneratorPage() {
  const { pbixContext } = usePBIX();
  const [query, setQuery] = useState("");
  const { models } = useOllamaModels();
  const [model, setModel] = useState("llama3.2:3b");

  const { daxCode, explanation, isLoading, error, generate, stop } = useDAXGeneration();

  const onGenerate = async () => {
    await generate({
      query,
      model,
      pbixContext: pbixContext?.rawContext || "",
      modelTables: pbixContext?.modelTables || [],
      mcpContext: {
        tables: pbixContext?.tables || [],
        columns: pbixContext?.columns || {},
        measures: pbixContext?.measures || [],
        relationships: pbixContext?.relationships || [],
        modelTables: pbixContext?.modelTables || [],
      },
    });
  };

  const hasModel = Boolean(pbixContext?.tables?.length);

  if (!hasModel) {
    return (
      <div style={{ display: "grid", gap: 18 }}>
        <Header
          title="DAX Generator"
          subtitle="Turn plain-language business requests into DAX measures with explanation."
          icon={<Zap size={24} color="var(--pbi-primary)" />}
        />
        <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} style={{ marginTop: 20 }}>
          <EmptyFileState message="Upload a .pbix file using the bar above so DAX can use your real tables and columns." />
        </motion.div>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 18 }}>
      <Header
        title="DAX Generator"
        subtitle="Describe what you want to calculate. The assistant will generate the measure and explain how it works."
        icon={<Zap size={24} color="var(--pbi-primary)" />}
      />

      <div
        style={{
          display: "grid",
          gap: 20,
          alignItems: "stretch",
        }}
        className="dax-page-grid"
      >
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          style={{ display: "grid", gap: 16, alignContent: "start" }}
        >
          <div className="card" style={{ padding: 16, display: "grid", gap: 14 }}>
            <ModelSelector models={models} selected={model} onSelect={setModel} />
            <NaturalLanguageInput value={query} onChange={setQuery} />

            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
              <GenerateDAXButton disabled={!query.trim()} loading={isLoading} onClick={onGenerate} />
              {isLoading && (
                <button
                  type="button"
                  className="button"
                  style={{
                    background: "var(--pbi-surface-alt)",
                    color: "var(--pbi-text)",
                  }}
                  onClick={stop}
                >
                  Stop
                </button>
              )}
            </div>
          </div>

          {error && (
            <div className="card" style={{ padding: 12, borderColor: "rgba(217, 83, 79, 0.5)", color: "var(--pbi-danger)" }}>
              {error.message}
            </div>
          )}
        </motion.div>

        <div style={{ minHeight: 0, display: "flex", flexDirection: "column" }}>
          <DAXOutputCard
            daxCode={daxCode}
            explanation={explanation}
            isLoading={isLoading}
            pbixId={pbixContext?.pbixId}
            tables={pbixContext?.tables}
          />
        </div>
      </div>
    </div>
  );
}
