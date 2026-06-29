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

function flaskBase() {
  return (process.env.NEXT_PUBLIC_FLASK_URL || "http://127.0.0.1:5052").replace(/\/$/, "");
}

/**
 * Try live PBI Desktop injection, then file-based patch / Tabular Editor script.
 *
 * @returns {Promise<"live-inject" | "direct" | "tabular-editor-script">}
 */
export async function patchPbixMeasures(pbixId, measures) {
  const flask = flaskBase();

  // 1. Try live injection into running PBI Desktop
  try {
    const injectRes = await fetch(`${flask}/api/pbix/inject-measures`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ measures }),
    });

    if (injectRes.ok) {
      const data = await injectRes.json();
      if (data.ok) {
        return "live-inject";
      }
    }
  } catch {
    // Network error — fall through to file-based patching
  }

  // 2. Fall back to file-based patching / Tabular Editor script
  const res = await fetch(`${flask}/api/pbix/patch-measures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pbix_id: pbixId, measures }),
  });

  if (!res.ok) {
    let message = `Patch failed (${res.status})`;
    try {
      const err = await res.json();
      if (err?.error) message = String(err.error);
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
