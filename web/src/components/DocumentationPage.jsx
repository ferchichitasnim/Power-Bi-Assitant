"use client";

import { useState, useCallback } from "react";
import { Check, Copy, FileText } from "lucide-react";
import Header from "./Header";
import EmptyFileState from "./shared/EmptyFileState";
import GeneratePdfButton from "./GeneratePdfButton";
import { usePBIX } from "../context/PBIXContext";
import { enrichSourceLabels } from "../utils/sourceDetection";

/* ------------------------------------------------------------------ */
/*  Copy button — reusable, shows checkmark briefly after copy        */
/* ------------------------------------------------------------------ */
function CopyButton({ text, label = "Copy", style = {} }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={copied ? "Copied!" : label}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "5px 10px",
        borderRadius: 8,
        border: "1px solid var(--pbi-border)",
        background: copied ? "rgba(0,178,148,0.10)" : "var(--pbi-surface-alt)",
        color: copied ? "var(--pbi-success, #00b294)" : "var(--pbi-muted)",
        cursor: "pointer",
        fontSize: 12,
        fontWeight: 600,
        transition: "all 150ms ease",
        flexShrink: 0,
        ...style,
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
      {copied ? "Copied!" : label}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*  Section header with title + copy button                           */
/* ------------------------------------------------------------------ */
function SectionHeader({ title, copyText }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 10,
      }}
    >
      <div style={{ fontWeight: 700 }}>{title}</div>
      {copyText && <CopyButton text={copyText} label="Copy" />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Stat card                                                         */
/* ------------------------------------------------------------------ */
function StatCard({ label, value }) {
  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 800 }}>{value}</div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  List card                                                         */
/* ------------------------------------------------------------------ */
function ListCard({ title, items, emptyText }) {
  const visibleItems = (items || []).filter((item) => !isInternalPowerBITableName(String(item)));
  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ fontWeight: 700, marginBottom: 10, fontSize: 15 }}>{title}</div>
      {visibleItems.length === 0 ? (
        <div className="muted">{emptyText}</div>
      ) : (
        <div
          style={{
            maxHeight: 320,
            overflow: "auto",
            border: "1px solid var(--pbi-border)",
            borderRadius: 10,
            background: "var(--pbi-surface-alt)",
            padding: 10,
          }}
        >
          <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 8 }}>
            {visibleItems.map((item, idx) => (
              <li key={`${idx}-${item}`} style={{ lineHeight: 1.35 }}>
                {item}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Helper: build plain-text for a single section                     */
/* ------------------------------------------------------------------ */
function buildSectionText(title, items) {
  const visibleItems = (items || []).filter((i) => !isInternalPowerBITableName(String(i)));
  if (visibleItems.length === 0) return `${title}\n  (empty)`;
  return `${title}\n${visibleItems.map((i) => `  • ${i}`).join("\n")}`;
}

function isInternalPowerBITableName(name) {
  const v = String(name || "").toLowerCase();
  return v.includes("localdatatable_") || v.includes("datetabletemplate_");
}

function filterInternalPowerBITables(items) {
  return (items || []).filter((name) => !isInternalPowerBITableName(name));
}

/* ------------------------------------------------------------------ */
/*  Helper: build full documentation as plain text                    */
/* ------------------------------------------------------------------ */
function buildFullDocText({
  docTables,
  modelRelationships,
  tableRoles,
  keyColumns,
  daxColumns,
  measureDocs,
  rls,
  parameters,
  sourceList,
}) {
  const lines = [];

  lines.push("=== REPORT DATA SOURCES ===");
  const srcList = sourceList || [];
  if (srcList.length) srcList.forEach((s) => lines.push(`  • ${s}`));
  else lines.push("  No sources detected.");

  lines.push("");
  lines.push("=== REPORT MODEL ===");
  lines.push(`Tables (${docTables.length}):`);
  docTables.forEach((t) => lines.push(`  • ${t}`));
  lines.push("");
  lines.push(`Relationships (${modelRelationships.length}):`);
  modelRelationships.forEach((r) => {
    const tag = r.active === false ? " [INACTIVE]" : "";
    lines.push(`  • ${r.from} → ${r.to} | cardinality: ${r.cardinality} | direction: ${r.direction}${tag}`);
  });

  lines.push("");
  lines.push("=== TABLE ROLES ===");
  if (tableRoles.length) tableRoles.forEach((t) => lines.push(`  • ${t.table} — ${t.role}`));
  else lines.push("  No roles detected.");

  lines.push("");
  lines.push("=== KEY COLUMNS ===");
  if (keyColumns.length) keyColumns.forEach((k) => lines.push(`  • ${k.table}[${k.column}]${k.data_type ? ` (${k.data_type})` : ""}`));
  else lines.push("  No key columns detected.");

  lines.push("");
  lines.push(`=== DAX CALCULATED COLUMNS (${daxColumns.length}) ===`);
  if (daxColumns.length) daxColumns.forEach((c) => lines.push(`  • ${c.reference}${c.formula ? ` = ${c.formula}` : ""}`));
  else lines.push("  No DAX calculated columns.");

  lines.push("");
  lines.push(`=== DAX MEASURES (${measureDocs.length}) ===`);
  if (measureDocs.length) measureDocs.forEach((m) => lines.push(`  • ${m.reference}${m.formula ? ` = ${m.formula}` : ""}`));
  else lines.push("  No DAX measures.");

  const hasRls = Boolean(rls?.has_rls) || (Array.isArray(rls?.details) && rls.details.length > 0);
  if (hasRls) {
    lines.push("");
    lines.push("=== RLS ===");
    lines.push(`  RLS Present: ${rls.has_rls ? "Yes" : "Not detected"}`);
    if (rls.details && rls.details.length) rls.details.forEach((d) => lines.push(`  • ${d}`));
  }

  const paramList = parameters || [];
  if (paramList.length) {
    lines.push("");
    lines.push("=== PARAMETERS ===");
    paramList.forEach((p) =>
      lines.push(
        `  • ${p.name}${p.type ? ` (${p.type})` : ""}${p.current_value ? ` = ${p.current_value}` : ""}${
          p.is_required ? " [Required]" : ""
        }`
      )
    );
  }

  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */
export default function DocumentationPage() {
  const { pbixContext, fileName } = usePBIX();

  if (!pbixContext) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <Header
          title="Documentation"
          subtitle="Explore model details extracted from the current PBIX file."
          icon={<FileText size={24} color="var(--pbi-primary)" />}
        />
        <EmptyFileState message="Upload a .pbix file using the bar above to generate documentation." />
      </div>
    );
  }

  const tables = pbixContext.tables || [];
  const relationships = pbixContext.relationships || [];
  const sources = pbixContext.sources || [];
  const documentation = pbixContext.documentation || {};

  const reportSources = documentation.report_sources || {};
  const reportModel = documentation.report_model || {};
  const daxCalculations = documentation.dax_calculations || {};
  const securityAndParameters = documentation.security_and_parameters || {};
  const dataModel = documentation.data_model || {};

  const rawDocTables = reportModel.tables && reportModel.tables.length ? reportModel.tables : tables;
  const docTables = filterInternalPowerBITables(rawDocTables);
  const modelRelationships = reportModel.relationships || [];
  const daxColumns = daxCalculations.calculated_columns || [];
  const measureDocs = daxCalculations.measures || [];
  const rls = securityAndParameters.rls || { has_rls: false, details: [] };
  const parameters = securityAndParameters.parameters || [];
  const tableRoles = dataModel.table_roles || [];
  const keyColumns = dataModel.key_columns || [];

  const powerQuery = documentation.power_query || pbixContext.power_query || [];
  const rawSrcItems =
    reportSources.sources && reportSources.sources.length ? reportSources.sources : sources;
  const srcItems = enrichSourceLabels(rawSrcItems, powerQuery);
  const relItems = modelRelationships.map((r) => {
    const tag = r.active === false ? " [INACTIVE]" : "";
    return `${r.from} → ${r.to} | cardinality: ${r.cardinality} | direction: ${r.direction}${tag}`;
  });
  const roleItems = tableRoles.map((t) => `${t.table} — ${t.role}`);
  const keyItems = keyColumns.map((k) => `${k.table}[${k.column}]${k.data_type ? ` (${k.data_type})` : ""}`);
  const daxColItems = daxColumns.map((c) => `${c.reference}${c.formula ? ` = ${c.formula}` : ""}`);
  const measureItems = measureDocs.map((m) => `${m.reference}${m.formula ? ` = ${m.formula}` : ""}`);
  const paramItems = parameters.map(
    (p) =>
      `${p.name}${p.type ? ` (${p.type})` : ""}${p.current_value ? ` = ${p.current_value}` : ""}${
        p.is_required ? " [Required]" : ""
      }`
  );
  const hasRlsContent = Boolean(rls.has_rls) || (Array.isArray(rls.details) && rls.details.length > 0);
  const hasParametersContent = paramItems.length > 0;
  const showSecuritySection = hasRlsContent || hasParametersContent;
  const securitySectionTitle =
    hasRlsContent && hasParametersContent
      ? "RLS and Parameters"
      : hasRlsContent
        ? "RLS"
        : "Parameters";
  const securityCopyText = [
    hasRlsContent &&
      `RLS\n  RLS Present: ${rls.has_rls ? "Yes" : "Not detected"}` +
        (rls.details?.length ? "\n" + rls.details.map((d) => `  • ${d}`).join("\n") : ""),
    hasParametersContent && buildSectionText("Parameters", paramItems),
  ]
    .filter(Boolean)
    .join("\n\n");

  const fullDocText = buildFullDocText({
    docTables,
    modelRelationships,
    tableRoles,
    keyColumns,
    daxColumns,
    measureDocs,
    rls,
    parameters,
    sourceList: srcItems,
  });

  const documentationPdfPayload = {
    filename: fileName || "PowerBI_Report.pbix",
    sources: srcItems,
    tables: docTables,
    columns: pbixContext.columns || {},
    schema: documentation.model_schema || [],
    power_query: documentation.power_query || pbixContext.power_query || [],
    relationships: modelRelationships,
    measures: measureDocs,
    calculated_columns: daxColumns,
    rls: securityAndParameters.rls_raw || rls,
    table_roles: tableRoles,
    parameters,
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Header
        title="Documentation"
        subtitle="Structured report metadata including sources, model entities, DAX artifacts, and security settings."
        icon={<FileText size={24} color="var(--pbi-primary)" />}
      />

      {/* Copy + Generate */}
      <div style={{ display: "grid", gap: 10, justifyItems: "end" }}>
        <CopyButton
          text={fullDocText}
          label="Copy All Documentation"
          style={{ padding: "8px 16px", fontSize: 13 }}
        />
        <GeneratePdfButton payload={documentationPdfPayload} disabled={!pbixContext} />
      </div>

      <div style={{ display: "grid", gap: 16 }}>
        {/* ---------------- Report Data Sources ---------------- */}
        <div className="card" style={{ padding: 16 }}>
          <SectionHeader
            title="Report Data Sources"
            copyText={buildSectionText("Report Data Sources", srcItems)}
          />
          <ListCard title="Sources" items={srcItems} emptyText="No sources detected." />
        </div>

        {/* ---------------- Report Model ---------------- */}
        <div className="card" style={{ padding: 16 }}>
          <SectionHeader
            title="Report Model (Tables and Relationships)"
            copyText={buildSectionText("Tables", docTables) + "\n\n" + buildSectionText("Relationships", relItems)}
          />
          <div className="section-grid">
            <StatCard label="Number of Tables" value={docTables.length} />
            <StatCard label="Number of Relationships" value={modelRelationships.length || relationships.length} />
          </div>
          <div className="section-grid" style={{ marginTop: 12 }}>
            <ListCard title="Tables" items={docTables} emptyText="No tables detected." />
            <ListCard
              title="Relationships"
              items={relItems}
              emptyText={
                relationships.length
                  ? "Relationships detected without structured details."
                  : "No relationships detected."
              }
            />
          </div>
        </div>

        {/* ---------------- Table Roles & Key Columns ---------------- */}
        <div className="card" style={{ padding: 16 }}>
          <SectionHeader
            title="Table Roles and Key Columns"
            copyText={buildSectionText("Table Roles", roleItems) + "\n\n" + buildSectionText("Key Columns", keyItems)}
          />
          <div className="section-grid">
            <ListCard title="Table Roles (dimension / fact)" items={roleItems} emptyText="No roles detected." />
            <ListCard title="Key Columns" items={keyItems} emptyText="No key columns detected." />
          </div>
        </div>

        {/* ---------------- DAX ---------------- */}
        <div className="card" style={{ padding: 16 }}>
          <SectionHeader
            title="DAX Calculated Columns and Measures"
            copyText={buildSectionText("DAX Calculated Columns", daxColItems) + "\n\n" + buildSectionText("DAX Measures", measureItems)}
          />
          <div className="section-grid">
            <StatCard label="DAX Columns" value={daxColumns.length} />
            <StatCard label="DAX Measures" value={measureDocs.length} />
          </div>
          <div className="section-grid" style={{ marginTop: 12 }}>
            <ListCard title="DAX Calculated Columns" items={daxColItems} emptyText="No DAX calculated columns." />
            <ListCard title="DAX Measures" items={measureItems} emptyText="No DAX measures." />
          </div>
        </div>

        {/* ---------------- RLS & Parameters ---------------- */}
        {showSecuritySection && (
          <div className="card" style={{ padding: 16 }}>
            <SectionHeader title={securitySectionTitle} copyText={securityCopyText} />
            <div className={hasRlsContent && hasParametersContent ? "section-grid" : undefined}>
              {hasRlsContent && (
                <div className="card" style={{ padding: 12, background: "var(--pbi-surface-alt)" }}>
                  <div style={{ fontWeight: 700, marginBottom: 6 }}>RLS</div>
                  <div className="muted">RLS Present: {rls.has_rls ? "Yes" : "Not detected"}</div>
                  {rls.details && rls.details.length > 0 && (
                    <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
                      {rls.details.map((d, idx) => (
                        <li key={`${idx}-${d}`}>{d}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {hasParametersContent && <ListCard title="Parameters" items={paramItems} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}