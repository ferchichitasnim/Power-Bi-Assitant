import { filenameFromContentDisposition } from "./parseDaxMeasures";

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * POST measures to Flask and download patched .pbix or Tabular Editor script.
 *
 * @returns {Promise<"direct" | "tabular-editor-script">}
 */
export async function patchPbixMeasures(pbixId, measures) {
  const flask = (process.env.NEXT_PUBLIC_FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
  const res = await fetch(`${flask}/api/pbix/patch-measures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pbix_id: pbixId, measures }),
  });

  if (!res.ok) {
    let message = `Patch failed (${res.status})`;
    try {
      const data = await res.json();
      if (data?.error) message = String(data.error);
    } catch {
      /* binary or empty body */
    }
    throw new Error(message);
  }

  const method = res.headers.get("X-Patch-Method") || "direct";
  const blob = await res.blob();
  const filename = filenameFromContentDisposition(res.headers.get("Content-Disposition"));
  triggerBlobDownload(blob, filename);
  return method === "tabular-editor-script" ? "tabular-editor-script" : "direct";
}
