"use client";

import { useCallback, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { FileSpreadsheet, Loader2, Upload, X } from "lucide-react";
import { usePBIX } from "../../context/PBIXContext";

export default function GlobalFileBar() {
  const {
    fileName,
    fileSize,
    pbixContext,
    isLoading,
    error,
    uploadProgress,
    isOpeningDesktop,
    uploadFile,
    openInPowerBIDesktop,
    clearFile,
    setError,
  } = usePBIX();

  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);

  const hasFile = Boolean(pbixContext);
  const showError = Boolean(error);

  const onPick = () => inputRef.current?.click();

  const onFiles = useCallback(
    (list) => {
      const f = list?.[0];
      if (f) uploadFile(f);
    },
    [uploadFile]
  );

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    onFiles(e.dataTransfer?.files);
  };

  const onDragOver = (e) => {
    e.preventDefault();
    setDragging(true);
  };

  const onDragLeave = () => setDragging(false);

  return (
    <motion.div
      layout
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      style={{
        position: "relative",
        width: "100%",
        minHeight: 56,
        background: "var(--pbi-surface)",
        borderBottom: showError ? "1px solid rgba(231, 76, 60, 0.6)" : "1px solid var(--pbi-border)",
        boxShadow: dragging ? "0 0 0 2px var(--pbi-yellow), 0 0 24px rgba(242, 200, 17, 0.25)" : "none",
        transition: "box-shadow 150ms ease, border-color 150ms ease",
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pbix"
        style={{ display: "none" }}
        onChange={(e) => {
          onFiles(e.target.files);
          e.target.value = "";
        }}
      />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          padding: "0 16px",
          minHeight: 56,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <FileSpreadsheet size={22} color="var(--pbi-yellow)" style={{ flexShrink: 0 }} />
          <span className="muted" style={{ fontSize: 13, flexShrink: 0 }}>
            Active File:
          </span>
          <AnimatePresence mode="wait">
            {hasFile ? (
              <motion.span
                key="name"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                style={{ fontWeight: 700, fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              >
                {fileName}
              </motion.span>
            ) : (
              <motion.span
                key="none"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="muted"
                style={{ fontStyle: "italic", fontSize: 14 }}
              >
                No file loaded
              </motion.span>
            )}
          </AnimatePresence>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          {!hasFile && !isLoading && (
            <motion.button
              type="button"
              onClick={onPick}
              initial={{ opacity: 0.9 }}
              whileHover={{ scale: 1.01 }}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "10px 16px",
                borderRadius: 10,
                border: "1px dashed #9cb2f2",
                background: dragging ? "#eef3ff" : "transparent",
                color: "var(--pbi-text)",
                cursor: "pointer",
                fontWeight: 600,
                fontSize: 13,
              }}
            >
              <Upload size={18} color="var(--pbi-yellow)" />
              {dragging ? "Drop to load" : "Drop or select a .pbix file"}
            </motion.button>
          )}

          {hasFile && !isLoading && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8 }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 999,
                    background: "var(--pbi-success)",
                    boxShadow: "0 0 8px rgba(0, 178, 148, 0.6)",
                  }}
                />
                <span className="muted" style={{ fontSize: 12 }}>
                  {fileSize}
                </span>
                <button
                  type="button"
                  className="input"
                  style={{ padding: "6px 12px", fontSize: 12, cursor: "pointer", fontWeight: 600 }}
                  onClick={onPick}
                >
                  Change File
                </button>
                <button
                  type="button"
                  aria-label="Clear file"
                  onClick={() => {
                    clearFile();
                    setError(null);
                  }}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: 36,
                    height: 36,
                    borderRadius: 8,
                    border: "1px solid var(--pbi-border)",
                    background: "var(--pbi-surface-alt)",
                    color: "var(--pbi-muted)",
                    cursor: "pointer",
                  }}
                >
                  <X size={18} />
                </button>
              </div>
              <button
                type="button"
                className="button"
                onClick={openInPowerBIDesktop}
                disabled={isOpeningDesktop}
                style={{ padding: "8px 12px", fontSize: 12, height: "auto", minWidth: 220 }}
              >
                {isOpeningDesktop ? "Opening Power BI Desktop..." : "Open this PBIX in Power BI Desktop"}
              </button>
            </motion.div>
          )}

          {isLoading && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--pbi-muted)", fontSize: 13 }}>
              <Loader2 size={18} className="dax-spin" />
              Extracting model context…
            </div>
          )}
        </div>
      </div>

      {showError && (
        <div
          style={{
            padding: "8px 16px 10px",
            borderTop: "1px solid rgba(231, 76, 60, 0.4)",
            background: "rgba(231, 76, 60, 0.08)",
            color: "var(--pbi-danger)",
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <span>{error}</span>
          <button
            type="button"
            className="button"
            style={{ padding: "6px 12px", fontSize: 12, height: "auto" }}
            onClick={() => {
              setError(null);
              onPick();
            }}
          >
            Retry
          </button>
        </div>
      )}

      <AnimatePresence>
        {isLoading && (
          <motion.div
            initial={{ scaleX: 0 }}
            animate={{ scaleX: uploadProgress / 100 }}
            exit={{ opacity: 0 }}
            style={{
              position: "absolute",
              bottom: 0,
              left: 0,
              right: 0,
              height: 3,
              transformOrigin: "left",
              background: "linear-gradient(90deg, var(--pbi-yellow), var(--pbi-success))",
            }}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}
