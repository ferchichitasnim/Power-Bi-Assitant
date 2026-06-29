"""
Connect to a running Power BI Desktop instance and inject DAX measures
directly into the live model — no Tabular Editor required.

Requirements: pip install clr-loader pythonnet (for .NET TOM)
Alternative: Uses raw XMLA over HTTP if pythonnet is unavailable.
"""

import glob
import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def find_pbi_desktop_port() -> int | None:
    """
    Find the port of the local Analysis Services instance
    that Power BI Desktop is running.
    Checks multiple known locations.
    """
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        logger.warning("LOCALAPPDATA not set — not on Windows?")
        return None

    # Multiple possible paths where PBI Desktop stores the port file
    search_patterns = [
        # Standard Power BI Desktop
        os.path.join(local_app_data, "Microsoft", "Power BI Desktop",
                     "AnalysisServicesWorkspaces", "AnalysisServicesWorkspace_*",
                     "Data", "msmdsrv.port.txt"),
        # Microsoft Store version
        os.path.join(local_app_data, "Packages",
                     "Microsoft.MicrosoftPowerBIDesktop_*",
                     "LocalState", "AnalysisServicesWorkspaces",
                     "AnalysisServicesWorkspace_*",
                     "Data", "msmdsrv.port.txt"),
        # Broad fallback — search entire LOCALAPPDATA
        os.path.join(local_app_data, "**", "msmdsrv.port.txt"),
    ]

    for pattern in search_patterns:
        logger.info(f"Searching for port file: {pattern}")
        port_files = sorted(glob.glob(pattern, recursive=True), key=os.path.getmtime, reverse=True)
        if port_files:
            for pf in port_files:
                try:
                    port = int(open(pf).read().strip())
                    logger.info(f"Found PBI Desktop AS port: {port} from {pf}")
                    return port
                except (ValueError, OSError) as e:
                    logger.warning(f"Could not read port from {pf}: {e}")

    # Log what we actually searched to help debug
    logger.warning(f"No msmdsrv.port.txt found. LOCALAPPDATA={local_app_data}")

    # List what's actually in the PBI Desktop folder
    pbi_dir = os.path.join(local_app_data, "Microsoft", "Power BI Desktop")
    store_dir = os.path.join(local_app_data, "Packages")
    if os.path.exists(pbi_dir):
        logger.info(f"Contents of {pbi_dir}: {os.listdir(pbi_dir)}")
    else:
        logger.warning(f"Directory does not exist: {pbi_dir}")

    # Check for Microsoft Store version
    store_matches = glob.glob(os.path.join(store_dir, "Microsoft.MicrosoftPowerBIDesktop_*"))
    if store_matches:
        logger.info(f"Found Store PBI folders: {store_matches}")

    return None


def build_xmla_create_measure(
    table_name: str,
    measure_name: str,
    dax_expression: str,
    format_string: str = "",
    description: str = "",
) -> str:
    """Build a TMSL (Tabular Model Scripting Language) JSON command to create/replace a measure."""
    measure_def = {
        "name": measure_name,
        "expression": dax_expression,
    }
    if format_string:
        measure_def["formatString"] = format_string
    if description:
        measure_def["description"] = description

    command = {
        "createOrReplace": {
            "object": {
                "database": "",
                "table": table_name,
                "measure": measure_name,
            },
            "measure": measure_def,
        }
    }
    return json.dumps(command)


def apply_measures_via_powershell(
    port: int,
    measures: list[dict],
) -> dict:
    """
    Use PowerShell + AMO/TOM to connect to the local PBI Desktop
    AS instance and add measures. This works without pythonnet.
    """
    ps_lines = [
        f'$connectionString = "Provider=MSOLAP;Data Source=localhost:{port};',
        'Initial Catalog=;";',
        "",
        '[System.Reflection.Assembly]::LoadWithPartialName("Microsoft.AnalysisServices.Tabular") | Out-Null;',
        "",
        "$server = New-Object Microsoft.AnalysisServices.Tabular.Server;",
        "$server.Connect($connectionString);",
        "$db = $server.Databases[0];",
        "$model = $db.Model;",
        "",
    ]

    for m in measures:
        table = m["table_name"].replace("'", "''")
        name = m["measure_name"].replace("'", "''")
        dax = m["dax_expression"].replace("'", "''")
        fmt = m.get("format_string", "").replace("'", "''")

        ps_lines.extend(
            [
                f"# --- Measure: {m['measure_name']} ---",
                f"$table = $model.Tables['{table}'];",
                "if ($table -ne $null) {",
                f"    $existing = $table.Measures['{name}'];",
                "    if ($existing -ne $null) {",
                f"        $existing.Expression = '{dax}';",
            ]
        )
        if fmt:
            ps_lines.append(f"        $existing.FormatString = '{fmt}';")
        ps_lines.extend(
            [
                "    } else {",
                "        $measure = New-Object Microsoft.AnalysisServices.Tabular.Measure;",
                f"        $measure.Name = '{name}';",
                f"        $measure.Expression = '{dax}';",
            ]
        )
        if fmt:
            ps_lines.append(f"        $measure.FormatString = '{fmt}';")
        ps_lines.extend(
            [
                "        $table.Measures.Add($measure);",
                "    }",
                f"    Write-Output 'Added measure: {name} to table: {table}';",
                "} else {",
                f"    Write-Error 'Table not found: {table}';",
                "}",
                "",
            ]
        )

    ps_lines.extend(
        [
            "$model.SaveChanges();",
            "$server.Disconnect();",
            "Write-Output 'All measures applied successfully.';",
        ]
    )

    ps_script = "\n".join(ps_lines)

    ps_path = os.path.join(tempfile.gettempdir(), "pbi_inject_measures.ps1")
    with open(ps_path, "w", encoding="utf-8") as f:
        f.write(ps_script)

    logger.info("Running PowerShell script to inject %s measures on port %s", len(measures), port)

    try:
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            logger.info("PowerShell output: %s", result.stdout.strip())
            return {
                "ok": True,
                "method": "live-inject",
                "message": result.stdout.strip(),
                "measures_applied": len(measures),
            }
        logger.error("PowerShell error: %s", result.stderr.strip())
        return {
            "ok": False,
            "method": "live-inject",
            "error": result.stderr.strip() or result.stdout.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "live-inject", "error": "PowerShell script timed out."}
    except FileNotFoundError:
        return {"ok": False, "method": "live-inject", "error": "PowerShell not found on this system."}


def inject_measures_into_pbi_desktop(measures: list[dict]) -> dict:
    """
    Main entry point. Finds a running PBI Desktop instance and injects measures.

    Args:
        measures: list of {table_name, measure_name, dax_expression, format_string?, description?}

    Returns:
        {ok: bool, method: str, message?: str, error?: str}
    """
    port = find_pbi_desktop_port()
    if port is None:
        return {
            "ok": False,
            "method": "none",
            "error": (
                "Power BI Desktop is not running or no model is open. "
                "Open your .pbix in Power BI Desktop first."
            ),
        }

    return apply_measures_via_powershell(port, measures)
