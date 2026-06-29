#!/usr/bin/env python3
"""
PBIXRay MCP Server

This MCP server exposes the capabilities of PBIXRay as tools and resources
for LLM clients to interact with Power BI (.pbix) files.
"""

import os
import json
import numpy as np
import argparse
import functools
import sys
import anyio
import asyncio
from typing import Optional

from mcp.server.fastmcp import FastMCP, Context
from pbixray import PBIXRay
from storytelling.ollama_story import build_story_context, generate_story_with_ollama


# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="PBIXRay MCP Server")
    parser.add_argument("--disallow", nargs="+", help="Specify tools to disable", default=[])
    parser.add_argument("--max-rows", type=int, default=10, help="Maximum rows to return for table data (default: 10)")
    parser.add_argument("--page-size", type=int, default=10, help="Default page size for paginated results (default: 10)")
    parser.add_argument("--load-file", type=str, help="Automatically load a PBIX file at startup")
    return parser.parse_args()


args = parse_args()
disallowed_tools = args.disallow
MAX_ROWS = args.max_rows
PAGE_SIZE = args.page_size
AUTO_LOAD_FILE = args.load_file


# ---------------------------------------------------------------------------
# Internal Power BI table filtering
# ---------------------------------------------------------------------------
def _is_internal_table(name: str) -> bool:
    """
    Detect auto-generated Power BI helper tables that should be hidden
    from user-facing output. These include LocalDateTable_* and
    DateTableTemplate_* entries.
    """
    lower = str(name or "").strip().lower()
    return "localdatetable_" in lower or "datetabletemplate_" in lower


def _filter_table_list(tables):
    """Filter a list/array of table names, removing internal PBI tables."""
    if isinstance(tables, np.ndarray):
        tables = tables.tolist()
    if not isinstance(tables, list):
        return tables
    return [t for t in tables if not _is_internal_table(str(t))]


def _filter_df_by_table_column(df, column_name: str, *, drop_none: bool = False):
    """Filter a pandas DataFrame, removing rows where column_name matches an internal table.

    If drop_none=True, also removes rows where the column value is None/NaN/empty.
    This is useful for relationship columns where PBIXRay may store None when the
    target table is an internal auto-generated table.
    """
    if df is None or not hasattr(df, "columns"):
        return df
    if column_name not in df.columns:
        return df

    def _should_drop(v):
        s = str(v).strip() if v is not None else ""
        if _is_internal_table(s):
            return True
        if drop_none and (v is None or s == "" or s.lower() == "none" or s.lower() == "nan"):
            return True
        return False

    mask = ~df[column_name].apply(_should_drop)
    return df[mask]


def _filter_df_by_multiple_table_columns(df, column_names: list, *, drop_none: bool = False):
    """Filter a DataFrame removing rows where ANY of the given columns is an internal table.

    If drop_none=True, also drops rows where any column is None/empty (for relationships
    where PBIXRay may store None as the table name for internal auto-generated tables).
    """
    if df is None or not hasattr(df, "columns"):
        return df
    for col in column_names:
        if col in df.columns:
            df = _filter_df_by_table_column(df, col, drop_none=drop_none)
    return df


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        return super().default(obj)


mcp = FastMCP("PBIXRay")

original_tool_decorator = mcp.tool


def secure_tool(*args, **kwargs):
    original_decorator = original_tool_decorator(*args, **kwargs)

    def new_decorator(func):
        tool_name = func.__name__

        if tool_name in disallowed_tools:

            @functools.wraps(func)
            def disabled_tool(*f_args, **f_kwargs):
                return f"Error: The tool '{tool_name}' has been disabled by the server administrator."

            return original_decorator(disabled_tool)
        else:
            return original_decorator(func)

    return new_decorator


mcp.tool = secure_tool

current_model: Optional[PBIXRay] = None
current_model_path: Optional[str] = None


async def run_model_operation(ctx: Context, operation_name: str, operation_fn, *args, **kwargs):
    import time

    start_time = time.time()
    await ctx.info(f"Starting {operation_name}...")
    await ctx.report_progress(0, 100)

    try:

        def run_operation():
            return operation_fn(*args, **kwargs)

        result = await anyio.to_thread.run_sync(run_operation)
        elapsed_time = time.time() - start_time
        if elapsed_time > 1.0:
            await ctx.info(f"Completed {operation_name} in {elapsed_time:.2f} seconds")
        await ctx.report_progress(100, 100)
        return result
    except Exception as e:
        await ctx.error(f"Error in {operation_name}: {str(e)}")
        raise


@mcp.tool()
async def load_pbix_file(file_path: str, ctx: Context) -> str:
    """
    Load a Power BI (.pbix) file for analysis.

    Args:
        file_path: Path to the .pbix file to load

    Returns:
        A message confirming the file was loaded
    """
    global current_model, current_model_path

    file_path = os.path.expanduser(file_path)
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' not found."

    if not file_path.lower().endswith(".pbix"):
        return f"Error: File '{file_path}' is not a .pbix file."

    try:
        ctx.info(f"Loading PBIX file: {file_path}")
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        ctx.info(f"File size: {file_size_mb:.2f} MB")
        await ctx.report_progress(0, 100)

        if file_size_mb > 50:
            await ctx.info(f"Large file detected ({file_size_mb:.2f} MB). Loading asynchronously...")
            cancel_progress = anyio.Event()
            load_error = None

            async def progress_reporter():
                progress = 5
                try:
                    while not cancel_progress.is_set() and progress < 95:
                        await ctx.report_progress(progress, 100)
                        progress += 5 if progress < 50 else 2
                        await anyio.sleep(1)
                except Exception as e:
                    await ctx.info(f"Progress reporting error: {str(e)}")

            def load_pbixray():
                nonlocal load_error
                try:
                    return PBIXRay(file_path)
                except Exception as e:
                    load_error = e
                    return None

            progress_task = asyncio.create_task(progress_reporter())

            try:
                pbix_model = await anyio.to_thread.run_sync(load_pbixray)
                if load_error:
                    await ctx.info(f"Error loading PBIX file: {str(load_error)}")
                    return f"Error loading file: {str(load_error)}"
                current_model = pbix_model
            finally:
                cancel_progress.set()
                await progress_task
        else:
            current_model = PBIXRay(file_path)

        current_model_path = file_path
        await ctx.report_progress(100, 100)
        return f"Successfully loaded '{os.path.basename(file_path)}'"
    except Exception as e:
        ctx.info(f"Error loading PBIX file: {str(e)}")
        return f"Error loading file: {str(e)}"


@mcp.tool()
def get_tables(ctx: Context) -> str:
    """
    List all tables in the model.

    Returns:
        A list of tables in the model
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        tables = current_model.tables
        if isinstance(tables, (list, np.ndarray)):
            filtered = _filter_table_list(tables)
            return json.dumps(filtered, indent=2)
        else:
            return str(tables)
    except Exception as e:
        ctx.info(f"Error retrieving tables: {str(e)}")
        return f"Error retrieving tables: {str(e)}"


@mcp.tool()
def get_metadata(ctx: Context) -> str:
    """
    Get metadata about the Power BI configuration used during model creation.

    Returns:
        The metadata as a formatted string
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        metadata_df = current_model.metadata
        result = {}
        for _, row in metadata_df.iterrows():
            name = row["Name"]
            value = row["Value"]
            result[name] = value
        return json.dumps(result, indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving metadata: {str(e)}")
        return f"Error retrieving metadata: {str(e)}"


@mcp.tool()
def get_power_query(ctx: Context) -> str:
    """
    Display all M/Power Query code used for data transformation.

    Returns:
        A list of all Power Query expressions with their table names
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        power_query = current_model.power_query
        # Filter out internal tables if TableName column exists
        power_query = _filter_df_by_table_column(power_query, "TableName")
        return power_query.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving Power Query: {str(e)}")
        return f"Error retrieving Power Query: {str(e)}"


@mcp.tool()
def get_m_parameters(ctx: Context) -> str:
    """
    Display all M Parameters values.

    Returns:
        A list of parameter info with names, descriptions, and expressions
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        m_parameters = current_model.m_parameters
        return m_parameters.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving M Parameters: {str(e)}")
        return f"Error retrieving M Parameters: {str(e)}"


@mcp.tool()
def get_rls_roles(ctx: Context) -> str:
    """
    Get Row-Level Security (RLS) roles and table permissions if exposed by PBIXRay.

    PBIXRay versions differ on which attribute holds RLS data, so this tool tries
    several likely attribute names safely and returns whichever ones exist.

    Returns:
        JSON: { "has_rls": bool, "details": [ { "source": str, "count": int, "entries": [...] } ] }
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        details = []
        has_rls = False

        candidate_attrs = (
            "roles",
            "rls",
            "role_permissions",
            "table_permissions",
            "dax_rls",
            "row_level_security",
        )

        for attr in candidate_attrs:
            try:
                value = getattr(current_model, attr, None)
                if value is None:
                    continue

                if hasattr(value, "to_dict"):
                    rows = value.to_dict(orient="records")
                    if rows:
                        has_rls = True
                        details.append(
                            {
                                "source": attr,
                                "count": len(rows),
                                "entries": rows[:20],
                            }
                        )
                    continue

                if isinstance(value, (list, tuple)):
                    if len(value) > 0:
                        has_rls = True
                        details.append(
                            {
                                "source": attr,
                                "count": len(value),
                                "entries": list(value)[:20],
                            }
                        )
                    continue

                if isinstance(value, dict):
                    if len(value) > 0:
                        has_rls = True
                        details.append(
                            {
                                "source": attr,
                                "count": len(value),
                                "entries": value,
                            }
                        )
                    continue
            except Exception:
                continue

        return json.dumps({"has_rls": has_rls, "details": details}, cls=NumpyEncoder, indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving RLS roles: {str(e)}")
        return f"Error retrieving RLS roles: {str(e)}"


@mcp.tool()
def get_model_size(ctx: Context) -> str:
    """
    Get the model size in bytes.

    Returns:
        The size of the model in bytes
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        size = current_model.size
        return f"Model size: {size} bytes ({size / (1024 * 1024):.2f} MB)"
    except Exception as e:
        ctx.info(f"Error retrieving model size: {str(e)}")
        return f"Error retrieving model size: {str(e)}"


@mcp.tool()
def get_dax_tables(ctx: Context) -> str:
    """
    View DAX calculated tables.

    Returns:
        A list of DAX calculated tables with names and expressions
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        dax_tables = current_model.dax_tables
        # Filter out internal tables
        dax_tables = _filter_df_by_table_column(dax_tables, "TableName")
        dax_tables = _filter_df_by_table_column(dax_tables, "Name")
        return dax_tables.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving DAX tables: {str(e)}")
        return f"Error retrieving DAX tables: {str(e)}"


@mcp.tool()
def get_dax_measures(ctx: Context, table_name: str = None, measure_name: str = None) -> str:
    """
    Access DAX measures in the model with optional filtering.

    Args:
        table_name: Optional filter for measures from a specific table
        measure_name: Optional filter for a specific measure by name

    Returns:
        A list of DAX measures with names, expressions, and other metadata
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        dax_measures = current_model.dax_measures
        # Filter out internal tables
        dax_measures = _filter_df_by_table_column(dax_measures, "TableName")

        if table_name:
            dax_measures = dax_measures[dax_measures["TableName"] == table_name]
        if measure_name:
            dax_measures = dax_measures[dax_measures["Name"] == measure_name]

        if len(dax_measures) == 0:
            filters = []
            if table_name:
                filters.append(f"table '{table_name}'")
            if measure_name:
                filters.append(f"name '{measure_name}'")
            filter_text = " and ".join(filters)
            return f"No measures found with {filter_text}."

        return dax_measures.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving DAX measures: {str(e)}")
        return f"Error retrieving DAX measures: {str(e)}"


@mcp.tool()
def get_dax_columns(ctx: Context, table_name: str = None, column_name: str = None) -> str:
    """
    Access calculated column DAX expressions with optional filtering.

    Args:
        table_name: Optional filter for columns from a specific table
        column_name: Optional filter for a specific column by name

    Returns:
        A list of calculated columns with names and expressions
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        dax_columns = current_model.dax_columns
        # Filter out internal tables
        dax_columns = _filter_df_by_table_column(dax_columns, "TableName")

        if table_name:
            dax_columns = dax_columns[dax_columns["TableName"] == table_name]
        if column_name:
            dax_columns = dax_columns[dax_columns["ColumnName"] == column_name]

        if len(dax_columns) == 0:
            filters = []
            if table_name:
                filters.append(f"table '{table_name}'")
            if column_name:
                filters.append(f"name '{column_name}'")
            filter_text = " and ".join(filters)
            return f"No calculated columns found with {filter_text}."

        return dax_columns.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving DAX columns: {str(e)}")
        return f"Error retrieving DAX columns: {str(e)}"


@mcp.tool()
def get_schema(ctx: Context, table_name: str = None, column_name: str = None) -> str:
    """
    Get details about the data model schema and column types with optional filtering.

    Args:
        table_name: Optional filter for columns from a specific table
        column_name: Optional filter for a specific column by name

    Returns:
        A description of the schema with table names, column names, and data types
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        schema = current_model.schema
        # Filter out internal tables
        schema = _filter_df_by_table_column(schema, "TableName")

        if table_name:
            schema = schema[schema["TableName"] == table_name]
        if column_name:
            schema = schema[schema["ColumnName"] == column_name]

        if len(schema) == 0:
            filters = []
            if table_name:
                filters.append(f"table '{table_name}'")
            if column_name:
                filters.append(f"column '{column_name}'")
            filter_text = " and ".join(filters)
            return f"No schema entries found with {filter_text}."

        return schema.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving schema: {str(e)}")
        return f"Error retrieving schema: {str(e)}"


@mcp.tool()
async def get_relationships(ctx: Context, from_table: str = None, to_table: str = None) -> str:
    """
    Get the details about the data model relationships with optional filtering.

    Args:
        from_table: Optional filter for relationships from a specific table
        to_table: Optional filter for relationships to a specific table

    Returns:
        A description of the relationships between tables in the model
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:

        def get_filtered_relationships():
            model = current_model
            relationships = model.relationships
            # Filter out internal tables from both sides, and also drop
            # rows where either table name is None/empty (PBIXRay stores
            # None when the target is an internal auto-generated table).
            relationships = _filter_df_by_multiple_table_columns(
                relationships, ["FromTableName", "ToTableName"], drop_none=True
            )
            if from_table:
                relationships = relationships[relationships["FromTableName"] == from_table]
            if to_table:
                relationships = relationships[relationships["ToTableName"] == to_table]
            return relationships

        operation_name = "relationship retrieval"
        if from_table or to_table:
            filters = []
            if from_table:
                filters.append(f"from '{from_table}'")
            if to_table:
                filters.append(f"to '{to_table}'")
            filter_text = " and ".join(filters)
            operation_name += f" ({filter_text})"

        relationships = await run_model_operation(ctx, operation_name, get_filtered_relationships)

        if len(relationships) == 0:
            filters = []
            if from_table:
                filters.append(f"from table '{from_table}'")
            if to_table:
                filters.append(f"to table '{to_table}'")
            filter_text = " and ".join(filters)
            return f"No relationships found {filter_text}."

        return relationships.to_json(orient="records", indent=2)
    except Exception as e:
        await ctx.info(f"Error retrieving relationships: {str(e)}")
        return f"Error retrieving relationships: {str(e)}"


@mcp.tool()
async def get_table_contents(ctx: Context, table_name: str, filters: str = None, page: int = 1, page_size: int = None) -> str:
    """
    Retrieve the contents of a specified table with optional filtering and pagination.

    Args:
        table_name: Name of the table to retrieve
        filters: Optional filter conditions separated by semicolons (;)
        page: Page number to retrieve (starting from 1)
        page_size: Number of rows per page (defaults to value from --page-size)

    Returns:
        The table contents in JSON format with pagination metadata
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    # Block access to internal tables
    if _is_internal_table(table_name):
        return f"Error: Table '{table_name}' is an internal Power BI table and cannot be accessed."

    try:
        import time

        start_time = time.time()

        if page_size is None:
            page_size = PAGE_SIZE
        if page < 1:
            return "Error: Page number must be 1 or greater."
        if page_size < 1:
            return "Error: Page size must be 1 or greater."

        if filters:
            await ctx.info(f"Retrieving filtered data from table '{table_name}'...")
        else:
            await ctx.info(f"Retrieving page {page} from table '{table_name}'...")

        await ctx.report_progress(0, 100)

        def fetch_table():
            return current_model.get_table(table_name)

        table_contents = await anyio.to_thread.run_sync(fetch_table)
        await ctx.report_progress(25, 100)

        if filters:
            await ctx.info(f"Applying filters: {filters}")
            filter_conditions = filters.split(";")

            for condition in filter_conditions:
                for op in [">=", "<=", "!=", "=", ">", "<"]:
                    if op in condition:
                        col_name, value = condition.split(op, 1)
                        col_name = col_name.strip()
                        value = value.strip()

                        if col_name not in table_contents.columns:
                            return f"Error: Column '{col_name}' not found in table '{table_name}'."

                        try:
                            try:
                                if "." in value:
                                    numeric_value = float(value)
                                else:
                                    numeric_value = int(value)
                                value = numeric_value
                            except ValueError:
                                pass

                            if op == "=":
                                table_contents = table_contents[table_contents[col_name] == value]
                            elif op == ">":
                                table_contents = table_contents[table_contents[col_name] > value]
                            elif op == "<":
                                table_contents = table_contents[table_contents[col_name] < value]
                            elif op == ">=":
                                table_contents = table_contents[table_contents[col_name] >= value]
                            elif op == "<=":
                                table_contents = table_contents[table_contents[col_name] <= value]
                            elif op == "!=":
                                table_contents = table_contents[table_contents[col_name] != value]
                        except Exception as e:
                            return f"Error applying filter '{condition}': {str(e)}"
                        break
                else:
                    return f"Error: Invalid filter condition '{condition}'. Must contain one of these operators: =, >, <, >=, <=, !="

        await ctx.report_progress(50, 100)

        total_rows = len(table_contents)
        total_pages = (total_rows + page_size - 1) // page_size

        if total_rows > 10000:
            if filters:
                await ctx.info(f"Large result set: {total_rows} rows after filtering")
            else:
                await ctx.info(f"Large table detected: '{table_name}' has {total_rows} rows")

        start_idx = (page - 1) * page_size
        end_idx = min(start_idx + page_size, total_rows)

        if start_idx >= total_rows:
            if filters:
                return f"Error: Page {page} does not exist. The filtered table has {total_pages} page(s)."
            else:
                return f"Error: Page {page} does not exist. The table has {total_pages} page(s)."

        page_data = table_contents.iloc[start_idx:end_idx]
        await ctx.report_progress(75, 100)

        def serialize_data():
            return json.loads(page_data.to_json(orient="records"))

        serialized_data = await anyio.to_thread.run_sync(serialize_data)

        response = {
            "pagination": {
                "total_rows": total_rows,
                "total_pages": total_pages,
                "current_page": page,
                "page_size": page_size,
                "showing_rows": len(page_data),
            },
            "data": serialized_data,
        }

        await ctx.report_progress(100, 100)

        elapsed_time = time.time() - start_time
        if elapsed_time > 1.0:
            if filters:
                await ctx.info(
                    f"Retrieved filtered data from '{table_name}' ({total_rows} rows after filtering) in {elapsed_time:.2f} seconds"
                )
            else:
                await ctx.info(f"Retrieved data from '{table_name}' ({total_rows} rows) in {elapsed_time:.2f} seconds")

        return json.dumps(response, indent=2, cls=NumpyEncoder)
    except Exception as e:
        await ctx.info(f"Error retrieving table contents: {str(e)}")
        return f"Error retrieving table contents: {str(e)}"


@mcp.tool()
def get_statistics(ctx: Context, table_name: str = None, column_name: str = None) -> str:
    """
    Get statistics about the model with optional filtering.

    Args:
        table_name: Optional filter for statistics from a specific table
        column_name: Optional filter for statistics of a specific column

    Returns:
        Statistics about column cardinality and byte sizes
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        statistics = current_model.statistics
        # Filter out internal tables
        statistics = _filter_df_by_table_column(statistics, "TableName")

        if table_name:
            statistics = statistics[statistics["TableName"] == table_name]
        if column_name:
            statistics = statistics[statistics["ColumnName"] == column_name]

        if len(statistics) == 0:
            filters = []
            if table_name:
                filters.append(f"table '{table_name}'")
            if column_name:
                filters.append(f"column '{column_name}'")
            filter_text = " and ".join(filters)
            return f"No statistics found with {filter_text}."

        return statistics.to_json(orient="records", indent=2)
    except Exception as e:
        ctx.info(f"Error retrieving statistics: {str(e)}")
        return f"Error retrieving statistics: {str(e)}"


@mcp.tool()
async def generate_storytelling_narrative(ctx: Context, model_name: str = None) -> str:
    """
    Generate a business storytelling narrative from the currently loaded PBIX model using Ollama.

    Args:
        model_name: Optional Ollama model override (for example: "llama3.2:3b")

    Returns:
        A full narrative with overview, insights, risks, and recommended actions.
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        await ctx.report_progress(10, 100)

        # Use filtered tables for storytelling
        filtered_tables = _filter_table_list(current_model.tables)
        statistics = current_model.statistics
        statistics = _filter_df_by_table_column(statistics, "TableName")

        context = build_story_context(
            current_model_path or "unknown.pbix",
            filtered_tables,
            statistics,
        )
        await ctx.report_progress(50, 100)
        story = generate_story_with_ollama(context=context, model=model_name)
        await ctx.report_progress(100, 100)
        return story
    except Exception as e:
        await ctx.info(f"Error generating storytelling narrative: {str(e)}")
        return f"Error generating storytelling narrative: {str(e)}"


@mcp.tool()
async def get_model_summary(ctx: Context) -> str:
    """
    Get a comprehensive summary of the current Power BI model.

    Returns:
        A summary of the model with key metrics and information
    """
    if current_model is None:
        return "Error: No Power BI file loaded. Please use load_pbix_file first."

    try:
        await ctx.report_progress(0, 100)

        summary = {
            "file_path": current_model_path,
            "file_name": os.path.basename(current_model_path),
            "size_bytes": current_model.size,
            "size_mb": round(current_model.size / (1024 * 1024), 2),
        }

        await ctx.report_progress(25, 100)

        # Filter internal tables from the summary
        filtered_tables = _filter_table_list(current_model.tables)
        summary["tables_count"] = len(filtered_tables)
        summary["tables"] = filtered_tables

        await ctx.report_progress(50, 100)

        # Filter measures that belong to internal tables
        dax_measures = current_model.dax_measures
        if hasattr(dax_measures, "columns") and "TableName" in dax_measures.columns:
            dax_measures = _filter_df_by_table_column(dax_measures, "TableName")
        summary["measures_count"] = len(dax_measures) if hasattr(dax_measures, "__len__") else "Unknown"

        await ctx.report_progress(75, 100)

        # Filter relationships involving internal tables
        relationships = current_model.relationships
        if hasattr(relationships, "columns"):
            relationships = _filter_df_by_multiple_table_columns(
                relationships, ["FromTableName", "ToTableName"], drop_none=True
            )
        summary["relationships_count"] = len(relationships) if hasattr(relationships, "__len__") else "Unknown"

        await ctx.report_progress(100, 100)

        return json.dumps(summary, indent=2, cls=NumpyEncoder)
    except Exception as e:
        await ctx.info(f"Error creating model summary: {str(e)}")
        return f"Error creating model summary: {str(e)}"


def load_file_sync(file_path):
    global current_model, current_model_path

    file_path = os.path.expanduser(file_path)
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' not found."

    if not file_path.lower().endswith(".pbix"):
        return f"Error: File '{file_path}' is not a .pbix file."

    try:
        print(f"Loading PBIX file: {file_path}", file=sys.stderr)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        print(f"File size: {file_size_mb:.2f} MB", file=sys.stderr)
        current_model = PBIXRay(file_path)
        current_model_path = file_path
        return f"Successfully loaded '{os.path.basename(file_path)}'"
    except Exception as e:
        print(f"Error loading PBIX file: {str(e)}", file=sys.stderr)
        return f"Error loading file: {str(e)}"


def main():
    print("Starting PBIXRay MCP Server...", file=sys.stderr)

    if disallowed_tools:
        print(f"Security: Disallowed tools: {', '.join(disallowed_tools)}", file=sys.stderr)

    print("Configuring extended timeouts for large file handling...", file=sys.stderr)

    if AUTO_LOAD_FILE:
        file_path = os.path.expanduser(AUTO_LOAD_FILE)
        if os.path.exists(file_path):
            print(f"Auto-load file specified: {file_path}", file=sys.stderr)
            result = load_file_sync(file_path)
            print(f"Auto-load result: {result}", file=sys.stderr)
        else:
            print(f"Warning: Auto-load file not found: {file_path}", file=sys.stderr)

    try:
        mcp.run(transport="stdio")
    except Exception as e:
        print(f"PBIXRay MCP Server error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
