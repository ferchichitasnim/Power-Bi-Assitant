"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import toast from "react-hot-toast";
import { buildModelTablesFromPayload } from "../utils/modelTables";

function formatFileSize(bytes) {
  if (bytes == null || Number.isNaN(bytes) || bytes < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  const digits = i === 0 || n >= 10 ? 0 : 1;
  return `${n.toFixed(digits)} ${units[i]}`;
}

const PBIXContext = createContext(null);

function isInternalPowerBITableName(name) {
  const v = String(name || "").toLowerCase();
  return v.includes("localdatatable_") || v.includes("datetabletemplate_");
}

function sanitizePbixPayload(data) {
  const safeTables = (data?.tables || []).filter((t) => !isInternalPowerBITableName(t));
  const safeColumns = Object.fromEntries(
    Object.entries(data?.columns || {}).filter(([tableName]) => !isInternalPowerBITableName(tableName))
  );

  const documentation = data?.documentation || {};
  const reportModel = documentation.report_model || {};
  const dataModel = documentation.data_model || {};

  const safeReportTables = (reportModel.tables || []).filter((t) => !isInternalPowerBITableName(t));
  const safeTableRoles = (dataModel.table_roles || []).filter((r) => !isInternalPowerBITableName(r?.table));
  const safeKeyColumns = (dataModel.key_columns || []).filter((k) => !isInternalPowerBITableName(k?.table));
  const safeRelationships = (reportModel.relationships || []).filter(
    (r) => !isInternalPowerBITableName(String(r?.from || "").split("[")[0]) && !isInternalPowerBITableName(String(r?.to || "").split("[")[0])
  );

  return {
    pbixId: data?.pbix_id || null,
    tables: safeTables,
    columns: safeColumns,
    modelTables: buildModelTablesFromPayload({
      tables: safeTables,
      columns: safeColumns,
      measures: data?.measures || [],
      documentation: data?.documentation,
      schema: data?.schema,
      stats_preview: data?.stats_preview,
    }),
    measures: data?.measures || [],
    relationships: data?.relationships || [],
    sources: data?.sources || [],
    power_query: documentation.power_query || data?.power_query || [],
    documentation: {
      ...documentation,
      report_model: {
        ...reportModel,
        tables: safeReportTables,
        relationships: safeRelationships,
      },
      data_model: {
        ...dataModel,
        table_roles: safeTableRoles,
        key_columns: safeKeyColumns,
      },
    },
    rawContext: data?.rawContext || "",
    storyContext: data?.context || null,
  };
}

export function PBIXProvider({ children }) {
  const [file, setFile] = useState(null);
  const [fileName, setFileName] = useState("");
  const [fileSize, setFileSize] = useState("");
  const [pbixContext, setPbixContext] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadVersion, setUploadVersion] = useState(0);
  const [isOpeningDesktop, setIsOpeningDesktop] = useState(false);

  const clearFile = useCallback(() => {
    setFile(null);
    setFileName("");
    setFileSize("");
    setPbixContext(null);
    setError(null);
    setUploadProgress(0);
    setIsLoading(false);
  }, []);

  const uploadFile = useCallback((f) => {
    if (!f) return;
    if (!f.name?.toLowerCase().endsWith(".pbix")) {
      setError("Only .pbix files are supported.");
      return;
    }
    setError(null);
    setIsLoading(true);
    setUploadProgress(5);
    setFile(f);
    setFileName(f.name);
    setFileSize(formatFileSize(f.size));

    const flask = (process.env.NEXT_PUBLIC_FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
    const xhr = new XMLHttpRequest();
    const uploadTimeoutMs = Number(process.env.NEXT_PUBLIC_PBIX_UPLOAD_TIMEOUT_MS || 45000);
    xhr.open("POST", `${flask}/api/pbix/upload`);
    xhr.timeout = Number.isFinite(uploadTimeoutMs) && uploadTimeoutMs > 0 ? uploadTimeoutMs : 45000;

    xhr.upload.onprogress = (evt) => {
      if (evt.lengthComputable) {
        setUploadProgress(Math.max(10, Math.round((evt.loaded / evt.total) * 100)));
      }
    };

    xhr.onload = () => {
      setIsLoading(false);
      setUploadProgress(100);
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.ok) {
            setPbixContext(sanitizePbixPayload(data));
            setFileName(data.uploaded_name || data.file_name || f.name);
            setUploadVersion((v) => v + 1);
            toast.success("Model loaded");
          } else {
            setError(data.error || "Upload failed");
            setPbixContext(null);
          }
        } catch {
          setError("Invalid response from server");
          setPbixContext(null);
        }
      } else {
        let serverError = "";
        try {
          const parsed = JSON.parse(xhr.responseText || "{}");
          serverError = parsed?.error ? String(parsed.error) : "";
        } catch {
          serverError = "";
        }
        setError(serverError || `Upload failed (${xhr.status})`);
        setPbixContext(null);
      }
    };

    xhr.onerror = () => {
      setIsLoading(false);
      setError("Network error during upload");
      setPbixContext(null);
    };

    xhr.ontimeout = () => {
      setIsLoading(false);
      setUploadProgress(0);
      setError(
        "Model extraction timed out. The backend did not respond in time. Please retry; if it keeps happening, check Flask logs for the stuck MCP tool."
      );
      setPbixContext(null);
    };

    const form = new FormData();
    form.append("file", f);
    xhr.send(form);
  }, []);

  const openInPowerBIDesktop = useCallback(() => {
    if (!file) {
      setError("Upload a .pbix file first.");
      return;
    }

    setIsOpeningDesktop(true);
    setError(null);

    const flask = (process.env.NEXT_PUBLIC_FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${flask}/api/pbix/open-uploaded`);
    xhr.timeout = 30000;

    xhr.onload = () => {
      setIsOpeningDesktop(false);
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText || "{}");
          if (data.ok) {
            toast.success("Opening in Power BI Desktop");
            return;
          }
          setError(data.error || "Could not open file in Power BI Desktop.");
        } catch {
          setError("Invalid response from server while opening Power BI Desktop.");
        }
      } else {
        try {
          const parsed = JSON.parse(xhr.responseText || "{}");
          setError(parsed.error || parsed.launch_message || `Open failed (${xhr.status})`);
        } catch {
          setError(`Open failed (${xhr.status})`);
        }
      }
    };

    xhr.onerror = () => {
      setIsOpeningDesktop(false);
      setError("Network error while launching Power BI Desktop.");
    };

    xhr.ontimeout = () => {
      setIsOpeningDesktop(false);
      setError("Timed out while trying to launch Power BI Desktop.");
    };

    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  }, [file]);

  const value = useMemo(
    () => ({
      file,
      fileName,
      fileSize,
      pbixContext,
      isLoading,
      error,
      uploadProgress,
      uploadVersion,
      isOpeningDesktop,
      uploadFile,
      openInPowerBIDesktop,
      clearFile,
      setError,
    }),
    [
      file,
      fileName,
      fileSize,
      pbixContext,
      isLoading,
      error,
      uploadProgress,
      uploadVersion,
      isOpeningDesktop,
      uploadFile,
      openInPowerBIDesktop,
      clearFile,
    ]
  );

  return <PBIXContext.Provider value={value}>{children}</PBIXContext.Provider>;
}

export function usePBIX() {
  const ctx = useContext(PBIXContext);
  if (!ctx) throw new Error("usePBIX must be used within PBIXProvider");
  return ctx;
}

export { formatFileSize, PBIXContext };
