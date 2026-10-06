# -*- coding: utf-8 -*-
"""
CamisXmlLoader.py

Description:
    Pulls WardLayout XML from CaMIS (via the CAM_LIVE_ODBC linked server),
    parses the Bay/Container -> Placement structure, and loads the result
    into [xml].[container_bay_xml] in the target database.

    This is "Solution C" from the corridor-care / bed occupancy work -
    classifying "no bay assigned" beds by their actual container type
    (e.g. "NEXT PATIENT" = true corridor care vs "Virtual Discharge Area" /
    "SWAP AREA" = not corridor care). PlacementID is the join key expected
    to match BedIdentifierLOCAL in ips_bed / tbl_WardBed.

    Ported from the development notebook (notebooks/load_container_bay_xml.ipynb)
    for use inside the AutomaticRun.py template. Engines are built by the
    caller using Tools.Utilities.MakeSqlEngine - this module only builds
    queries, parses XML, and orchestrates the load; it does not know how
    connections are made.

Confirmed corruption patterns this module recovers from automatically:
    1. Unescaped '&' in free-text captions (e.g. "A & E")
    2. Stray NUL bytes + spurious newlines spliced into large XML values
       in transit - a buffer-boundary artifact at the MSDASQL/Informix
       driver bridge behind CAM_LIVE_ODBC (confirmed on ward ERU)

Known open question (NOT resolved by this module):
    Some wards (e.g. A4, in certain runs) return XML truncated at
    ~32,543 characters with no closing tag - close to Informix's
    practical VARCHAR/NVARCHAR/LVARCHAR ceiling (~32,739 chars). This
    may be a genuine source-side storage limit rather than a transit
    issue - see the run summary's "near Informix limit" note if this
    recurs. Confirming this definitively requires checking xdi_xml's
    declared column length directly in Informix's own system catalog.
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime

import pandas as pd
from sqlalchemy import text


INFORMIX_LIKELY_LIMIT_CHARS = 32739


# ============================================================================
# Logging helper - every status message both prints (visible when run
# manually via Run.bat) AND logs (captured in RunLog/ErrorLog for
# unattended scheduled runs, where nobody is watching the console).
# ============================================================================

def _emit(msg, logger=None, level="info"):
    print(msg)
    if logger is not None:
        getattr(logger, level)(msg)


# ============================================================================
# XML recovery - handles known transit-corruption patterns automatically
# ============================================================================

def sanitize_xml(xml_string):
    """
    Two independent, safe fixes for known corruption patterns:

    1. Escape bare '&' characters not already part of a valid XML
       entity reference (&amp; &lt; &gt; &apos; &quot; &#123; &#xAB;) -
       fixes free-text captions like "A & E". Never touches already-
       valid entities.

    2. Strip NUL bytes (\\x00) and newline/carriage-return characters.
       Confirmed via a real example (ward ERU): large XML values
       fetched through the CAM_LIVE_ODBC linked server can come back
       with a stray NUL byte and extra newlines spliced in mid-content
       - a classic buffer-boundary artifact in ODBC chunk reassembly,
       most likely at the MSDASQL/Informix driver bridge. A NUL byte is
       NEVER valid in XML, so removing it is always safe. Stripping
       newlines is a slightly stronger assumption - but every real
       WardLayout document inspected during development (A4, ward 5,
       ERU's clean SSMS-copied reference) was legitimately single-line,
       so a newline appearing in a document that FAILS to parse is
       almost certainly a transit artifact, not real content. This is
       only ever applied as a recovery fallback - documents that
       already parse successfully as received are never touched.
    """
    fixed = re.sub(r'&(?!amp;|lt;|gt;|apos;|quot;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', xml_string)
    fixed = fixed.replace('\x00', '').replace('\r', '').replace('\n', '')
    return fixed


def try_parse_with_recovery(xml_string):
    """
    Try parsing as-is; if that fails, try again after sanitizing.
    Returns (root_or_None, used_sanitized, error_or_None).
    """
    try:
        return ET.fromstring(xml_string), False, None
    except ET.ParseError as first_error:
        sanitized = sanitize_xml(xml_string)
        if sanitized != xml_string:
            try:
                return ET.fromstring(sanitized), True, None
            except ET.ParseError:
                pass
        return None, False, first_error


def diagnose_parse_error(xml_string, ward_code, error, logger=None):
    """
    Build and emit full, non-truncated context around an XML parse
    failure - the raw error message alone ('not well-formed (invalid
    token): line N, column M') doesn't say what's actually wrong. This
    shows the offending line, whether the document is properly closed,
    and whether there's more than one XML declaration (a sign of
    concatenated/corrupted content).
    """
    lines_out = []
    lines_out.append(f"\n{'='*70}")
    lines_out.append(f"XML PARSE DIAGNOSTIC - ward {ward_code}")
    lines_out.append(f"{'='*70}")
    lines_out.append(f"Error: {error}")
    lines_out.append(f"String length: {len(xml_string)} characters")
    lines_out.append(f"Starts with BOM: {xml_string.startswith(chr(0xFEFF))}")
    lines_out.append(f"Number of '<?xml' declarations found: {xml_string.count('<?xml')}")
    lines_out.append(f"Ends with '</WardLayout>': {xml_string.rstrip().endswith('</WardLayout>')}")
    lines = xml_string.splitlines()
    lines_out.append(f"Line count (split on \\n): {len(lines)}")

    if hasattr(error, "position"):
        err_line, err_col = error.position
        lines_out.append(f"\nContext around line {err_line}, column {err_col}:")
        start = max(1, err_line - 1)
        end = min(len(lines), err_line + 1)
        for ln in range(start, end + 1):
            marker = ">>> " if ln == err_line else "    "
            content = lines[ln - 1] if ln - 1 < len(lines) else "(no such line)"
            lines_out.append(f"{marker}Line {ln}: {content[:150]}")
            if ln == err_line and err_col < len(content):
                pointer = " " * (len(marker) + len(f"Line {ln}: ") + min(err_col, 150)) + "^"
                lines_out.append(pointer)
                lines_out.append(f"    Character at that position: {content[err_col:err_col+1]!r}")
    lines_out.append(f"{'='*70}\n")

    _emit("\n".join(lines_out), logger, level="error")


# ============================================================================
# Query builders
# ============================================================================

def build_query_4part(ward_object_ids):
    """Original 4-part linked-server naming. Confirmed via diagnostics
    that some wards can return xdi_xml truncated at ~32.5k characters -
    but this is the only query form confirmed NOT to crash, so it's the
    safe default while that truncation's true cause (transport vs
    genuine source-side Informix column limit) is unresolved for
    specific large wards."""
    ids_list = ", ".join(f"'{w}'" for w in ward_object_ids)
    return f"""
    SELECT CAST(di.xdi_xml AS NVARCHAR(MAX)) AS xdi_xml, s2.xiv_value
    FROM      [CAM_LIVE_ODBC].[CAMIS].[camis].xss_docinstance di,
              [CAM_LIVE_ODBC].[CAMIS].[camis].xss_document d,
              [CAM_LIVE_ODBC].[CAMIS].[camis].xss_idxvalstring s2,
              [CAM_LIVE_ODBC].[CAMIS].[camis].xss_indexdef i2
    WHERE
        di.xdc_docid = d.xdc_docid
        AND s2.xdi_instance = di.xdi_instance
        AND s2.xid_indexid = i2.xid_indexid
        AND i2.xid_indexname = 'ObjectId'
        AND di.xdi_replacetime IS NULL
        AND d.xdt_doctypeid = (
            SELECT xdt_doctypeid
            FROM [CAM_LIVE_ODBC].[CAMIS].[camis].xss_documenttype
            WHERE xdt_doctypename = 'WardLayout'
        )
        AND s2.xiv_value IN ({ids_list})
    """


def build_query_openquery(ward_object_ids):
    """
    EXPERIMENTAL / UNVERIFIED - CAM_LIVE_ODBC is backed by Informix, not
    SQL Server (confirmed by an earlier syntax error testing this path).
    This version removes the SQL-Server-specific CAST(...AS NVARCHAR(MAX))
    and lets Informix return xdi_xml in its own native type - a best-
    effort fix for the SYNTAX problem only. Does NOT necessarily fix any
    truncation, if that turns out to be a genuine Informix column-size
    limit rather than a transport-layer issue.
    """
    ids_list = ", ".join(f"''{w}''" for w in ward_object_ids)
    inner_query = f"""
        SELECT di.xdi_xml, s2.xiv_value
        FROM      camis.xss_docinstance di,
                  camis.xss_document d,
                  camis.xss_idxvalstring s2,
                  camis.xss_indexdef i2
        WHERE
            di.xdc_docid = d.xdc_docid
            AND s2.xdi_instance = di.xdi_instance
            AND s2.xid_indexid = i2.xid_indexid
            AND i2.xid_indexname = ''ObjectId''
            AND di.xdi_replacetime IS NULL
            AND d.xdt_doctypeid = (
                SELECT xdt_doctypeid
                FROM camis.xss_documenttype
                WHERE xdt_doctypename = ''WardLayout''
            )
            AND s2.xiv_value IN ({ids_list})
    """
    inner_query_clean = " ".join(inner_query.split())
    return f"SELECT * FROM OPENQUERY(CAM_LIVE_ODBC, '{inner_query_clean}')"


def build_query(ward_object_ids, use_openquery=False):
    return build_query_openquery(ward_object_ids) if use_openquery else build_query_4part(ward_object_ids)


DISCOVER_WARDS_QUERY = """
SELECT DISTINCT s2.xiv_value AS WardCode
FROM      [CAM_LIVE_ODBC].[CAMIS].[camis].xss_docinstance di,
          [CAM_LIVE_ODBC].[CAMIS].[camis].xss_document d,
          [CAM_LIVE_ODBC].[CAMIS].[camis].xss_idxvalstring s2,
          [CAM_LIVE_ODBC].[CAMIS].[camis].xss_indexdef i2
WHERE
    di.xdc_docid = d.xdc_docid
    AND s2.xdi_instance = di.xdi_instance
    AND s2.xid_indexid = i2.xid_indexid
    AND i2.xid_indexname = 'ObjectId'
    AND di.xdi_replacetime IS NULL
    AND d.xdt_doctypeid = (
        SELECT xdt_doctypeid
        FROM [CAM_LIVE_ODBC].[CAMIS].[camis].xss_documenttype
        WHERE xdt_doctypename = 'WardLayout'
    )
ORDER BY WardCode
"""


# ============================================================================
# Error explanation - short, readable summaries instead of raw tracebacks
# ============================================================================

def explain_sql_error(e, query_used, logger=None):
    """
    Build and emit a short, readable summary of a SQL failure instead of
    the ~40-line nested SQLAlchemy/pandas traceback. The genuinely
    useful part of a pyodbc/SQLAlchemy error is almost always in the
    first few hundred characters (the driver's own error message) - the
    rest is internal call-stack noise.
    """
    error_text = str(e)
    lines_out = []
    lines_out.append(f"\n{'='*70}")
    lines_out.append("QUERY FAILED")
    lines_out.append(f"{'='*70}")
    lines_out.append("\nDriver/database message:")
    lines_out.append(f"  {error_text[:500]}")
    lines_out.append("\nQuery attempted (first 300 chars):")
    lines_out.append(f"  {' '.join(query_used.split())[:300]}")

    lowered = error_text.lower()
    lines_out.append("\nLikely cause:")
    if "syntax error" in lowered and "informix" in lowered:
        lines_out.append("  Informix rejected the inner OPENQUERY SQL as invalid syntax.")
        lines_out.append("  Confirms CAM_LIVE_ODBC is Informix-backed, not SQL Server - T-SQL")
        lines_out.append("  syntax (e.g. NVARCHAR(MAX)) will not work inside an OPENQUERY")
        lines_out.append("  string sent to it. Try use_openquery=False, or adjust")
        lines_out.append("  build_query_openquery()'s inner SQL to valid Informix syntax.")
    elif "login failed" in lowered or "28000" in error_text:
        lines_out.append("  Authentication/permission issue - check the account this script")
        lines_out.append("  runs as has access to this server/database.")
    elif "im002" in lowered or ("data source name not found" in lowered):
        lines_out.append("  ODBC driver mismatch - Tools.Utilities.MakeSqlEngine hardcodes")
        lines_out.append("  'ODBC Driver 17 for SQL Server'. If this host doesn't have that")
        lines_out.append("  exact driver installed, this fails. Check with Simon Newey whether")
        lines_out.append("  the scheduling host has ODBC Driver 17 installed.")
    elif "timeout" in lowered:
        lines_out.append("  Connection or query timed out - check network/VPN, or the query")
        lines_out.append("  may need a longer timeout for this data volume.")
    elif "invalid object name" in lowered or "invalid column name" in lowered:
        lines_out.append("  A table/column name in the query doesn't exist as written - check")
        lines_out.append("  spelling and that the linked server/database/schema names match.")
    else:
        lines_out.append("  Not one of the previously-seen failure patterns - read the driver")
        lines_out.append("  message above for specifics.")
    lines_out.append(f"{'='*70}\n")

    _emit("\n".join(lines_out), logger, level="error")


# ============================================================================
# XML parsing
# ============================================================================

def get_caption_text(element):
    """
    Pull the display text out of an element's OWN Caption/Content child
    (e.g. Bay's "Bay 2", Container's "NEXT PATIENT", Placement's "Bed 6").
    Uses a direct-child path so it never picks up a nested Placement's
    caption by mistake.
    """
    content_el = element.find("Caption/Content")
    if content_el is not None and content_el.text:
        return content_el.text.strip()
    return None


def parse_ward_layout_xml(xml_string, ward_code_from_query, logger=None):
    """
    Extract one row per Placement, grouped under its parent Bay or Container.

    Columns produced:
        WardCode          - ward object id (from the WardLayout root, cross-
                             checked against the value the query matched on)
        ParentElementType - 'Bay', 'Container', or 'Unassigned' (a Placement
                             with no Bay/Container wrapper at all - confirmed
                             on ward ERU: 16 beds this way)
        ParentID          - Bay's id attribute (None for Container/Unassigned)
        ParentTypeCode    - Bay's type attribute: 'B' (bay) or 'R' (side room);
                             None for Container/Unassigned
        ParentCaption     - display name, e.g. 'Bay 2', 'Side Room A',
                             'NEXT PATIENT', 'Virtual Discharge Area', 'SWAP AREA'
        PlacementID       - the id CaMIS assigns to the bed/chair/trolley -
                             the join key expected to match BedIdentifierLOCAL
                             in ips_bed / tbl_WardBed
        PlacementType     - 'BED', 'CHAIR', 'TROLLEY' etc.
        PlacementCaption  - display name, e.g. 'Bed 6', 'Chair A', 'Trolley 1'

    <Object> elements (decorative text boxes like "Nurses Station" or the
    ward title banner) are deliberately NOT included - they aren't bed
    locations.

    Returns (rows, used_sanitized).
    """
    root, used_sanitized, parse_error = try_parse_with_recovery(xml_string)
    if root is None:
        raise parse_error
    if used_sanitized:
        _emit(f"WARNING Ward {ward_code_from_query}: had invalid content (unescaped '&', "
              f"NUL bytes, and/or spurious newlines) - auto-fixed before parsing",
              logger, level="warning")
    ward_code = root.attrib.get("objectId", ward_code_from_query)

    if ward_code != ward_code_from_query:
        _emit(f"NOTE: XML objectId '{ward_code}' differs from query match "
              f"value '{ward_code_from_query}' - using XML's objectId.", logger, level="warning")

    rows = []
    parent_elements = root.findall("Bay") + root.findall("Container")

    for placement_el in root.findall("Placement"):
        rows.append({
            "WardCode": ward_code,
            "ParentElementType": "Unassigned",
            "ParentID": None,
            "ParentTypeCode": None,
            "ParentCaption": None,
            "PlacementID": placement_el.attrib.get("id"),
            "PlacementType": placement_el.attrib.get("type"),
            "PlacementCaption": get_caption_text(placement_el),
        })

    for parent_el in parent_elements:
        parent_tag = parent_el.tag
        parent_id = parent_el.attrib.get("id")
        parent_type = parent_el.attrib.get("type")
        parent_caption = get_caption_text(parent_el)

        placements = parent_el.findall("Placement")

        if not placements:
            rows.append({
                "WardCode": ward_code,
                "ParentElementType": parent_tag,
                "ParentID": parent_id,
                "ParentTypeCode": parent_type,
                "ParentCaption": parent_caption,
                "PlacementID": None,
                "PlacementType": None,
                "PlacementCaption": None,
            })
            continue

        for placement_el in placements:
            rows.append({
                "WardCode": ward_code,
                "ParentElementType": parent_tag,
                "ParentID": parent_id,
                "ParentTypeCode": parent_type,
                "ParentCaption": parent_caption,
                "PlacementID": placement_el.attrib.get("id"),
                "PlacementType": placement_el.attrib.get("type"),
                "PlacementCaption": get_caption_text(placement_el),
            })

    return rows, used_sanitized


# ============================================================================
# SQL Server helpers
# ============================================================================

def ensure_schema_exists(engine, schema_name, database_label="", logger=None):
    """Create the target schema if it doesn't already exist."""
    try:
        with engine.connect() as conn:
            conn.execute(text(f"""
                IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = '{schema_name}')
                EXEC('CREATE SCHEMA {schema_name}')
            """))
            conn.commit()
        _emit(f"Schema '{schema_name}' confirmed", logger)
    except Exception as e:
        lines_out = [
            f"\nFailed to create/confirm schema '{schema_name}'",
            f"  {str(e)[:400]}",
            "  Likely cause: the account this script runs as may not have CREATE",
            f"  SCHEMA permission on {database_label} - ask Simon Newey to grant it,",
            f"  or to create the '{schema_name}' schema manually.",
        ]
        _emit("\n".join(lines_out), logger, level="error")
        raise RuntimeError("Schema check/creation failed - see above") from None


def discover_ward_codes(source_engine, logger=None):
    """Return every ward code that has a WardLayout document in CaMIS.
    Only returns ward codes (tiny strings), never the XML itself - fast,
    and carries none of the large-document transit-corruption risk."""
    _emit("Auto-discovering all ward codes with a WardLayout document...", logger)
    try:
        with source_engine.connect() as discover_conn:
            df_wards = pd.read_sql_query(DISCOVER_WARDS_QUERY, discover_conn)
        ward_ids = df_wards["WardCode"].tolist()
        _emit(f"Discovered {len(ward_ids)} ward(s): {ward_ids}", logger)
        return ward_ids
    except Exception as e:
        explain_sql_error(e, DISCOVER_WARDS_QUERY, logger)
        raise RuntimeError("Ward discovery failed - see above") from None


def fetch_ward_xml(source_engine, ward_object_ids, use_openquery=False, logger=None):
    """Run the WardLayout query for the given ward codes and return a
    DataFrame with columns xdi_xml, xiv_value."""
    query = build_query(ward_object_ids, use_openquery=use_openquery)
    _emit(f"Running WardLayout query for ward(s): {ward_object_ids} "
          f"(via {'OPENQUERY (experimental)' if use_openquery else '4-part naming'})...", logger)
    try:
        with source_engine.connect() as source_conn:
            source_conn.execute(text("SET TEXTSIZE 2147483647"))
            df_raw = pd.read_sql_query(query, source_conn)
        _emit(f"Retrieved {len(df_raw)} row(s)", logger)
        return df_raw
    except Exception as e:
        explain_sql_error(e, query, logger)
        raise RuntimeError("Query failed - see diagnostic above for details") from None


# ============================================================================
# Full orchestration
# ============================================================================

def run_load(source_engine, target_engine, *,
             target_database, target_schema, target_table,
             ward_object_ids=None, auto_discover_all_wards=True,
             use_openquery=False,
             min_row_count_ratio=0.7, force_overwrite=False,
             logger=None):
    """
    Full discover -> fetch -> parse -> guard -> write -> verify pipeline.

    Returns a summary dict:
        {
            "rows_written": int,
            "write_skipped": bool,
            "skip_reason": str or None,
            "ward_status": DataFrame (WardCode, Status, RowCount, DocLength),
        }

    Raises RuntimeError if a step fails outright (discovery/query/schema/
    write) - diagnostics are emitted before raising, both to console and
    to `logger` if provided, so a scheduled run's error log shows exactly
    what happened.
    """
    # --- Step 1: determine ward list ---
    if auto_discover_all_wards:
        ward_object_ids = discover_ward_codes(source_engine, logger)
    else:
        _emit(f"Using manually configured ward_object_ids: {ward_object_ids}", logger)

    if not ward_object_ids:
        _emit("No ward codes to process - nothing to do.", logger, level="warning")
        return {"rows_written": 0, "write_skipped": True,
                "skip_reason": "no ward codes to process", "ward_status": pd.DataFrame()}

    # --- Step 2: fetch raw XML ---
    df_raw = fetch_ward_xml(source_engine, ward_object_ids, use_openquery=use_openquery, logger=logger)

    # --- Step 3: parse every row, tracking per-ward outcome ---
    load_timestamp = datetime.now()
    all_rows = []
    ward_status = []

    for _, row in df_raw.iterrows():
        ward_code = row["xiv_value"]
        xml_string = row["xdi_xml"]
        doc_length = len(xml_string)
        try:
            rows, used_sanitized = parse_ward_layout_xml(xml_string, ward_code, logger=logger)

            for r in rows:
                r["DocumentLength"] = doc_length
                r["IsDocumentComplete"] = not used_sanitized
                r["LoadTimestamp"] = load_timestamp

            all_rows.extend(rows)
            bay_count = sum(1 for r in rows if r["ParentElementType"] == "Bay")
            container_count = sum(1 for r in rows if r["ParentElementType"] == "Container")
            _emit(f"Ward {ward_code}: {len(rows)} placement row(s) "
                  f"({bay_count} in bays, {container_count} in containers)", logger)

            status = "Recovered" if used_sanitized else ("Empty" if len(rows) == 0 else "Success")
            ward_status.append({"WardCode": ward_code, "Status": status,
                                 "RowCount": len(rows), "DocLength": doc_length})
        except ET.ParseError as e:
            _emit(f"Failed to parse XML for ward {ward_code}: {e}", logger, level="error")
            diagnose_parse_error(xml_string, ward_code, e, logger=logger)
            ward_status.append({"WardCode": ward_code, "Status": "Failed",
                                 "RowCount": 0, "DocLength": doc_length})

    df_parsed = pd.DataFrame(all_rows)
    _emit(f"Parsed {len(df_parsed)} total row(s) across {len(df_raw)} ward document(s)", logger)

    # --- Run summary: a partial failure should never be easy to miss ---
    df_status = pd.DataFrame(ward_status)
    summary_lines = [f"\n{'='*70}", "RUN SUMMARY", f"{'='*70}",
                      df_status.to_string(index=False) if not df_status.empty else "(no wards processed)"]
    n_success = (df_status["Status"] == "Success").sum() if not df_status.empty else 0
    n_recovered = (df_status["Status"] == "Recovered").sum() if not df_status.empty else 0
    n_empty = (df_status["Status"] == "Empty").sum() if not df_status.empty else 0
    n_failed = (df_status["Status"] == "Failed").sum() if not df_status.empty else 0
    summary_lines.append(f"\n{n_success} succeeded cleanly, {n_recovered} recovered (auto-fixed), "
                          f"{n_empty} legitimately empty, {n_failed} FAILED")
    if n_failed > 0:
        failed_wards = df_status.loc[df_status["Status"] == "Failed", "WardCode"].tolist()
        summary_lines.append(f"Failed ward(s): {failed_wards} - see diagnostics above for each")
    summary_lines.append(f"{'='*70}")
    _emit("\n".join(summary_lines), logger)

    # --- Step 4: schema check ---
    ensure_schema_exists(target_engine, target_schema, database_label=target_database, logger=logger)

    # --- Step 5: write, with data-loss guard ---
    write_skipped = df_parsed.empty
    skip_reason = "df_parsed was empty (no bays/containers/placements found)" if write_skipped else None

    if write_skipped:
        _emit("Nothing to write - df_parsed is empty (no bays/containers/placements "
              "found for the requested ward(s)). Skipping the write rather than "
              "attempting to create a table with no columns.", logger, level="warning")
    else:
        new_row_count = len(df_parsed)
        existing_row_count = None
        try:
            with target_engine.connect() as check_conn:
                result = check_conn.execute(text(
                    f"SELECT COUNT(*) FROM [{target_schema}].[{target_table}]"
                ))
                existing_row_count = result.scalar()
        except Exception:
            pass  # table doesn't exist yet - first run, nothing to compare against

        proceed = True
        if existing_row_count is not None and existing_row_count > 0:
            ratio = new_row_count / existing_row_count
            _emit(f"Existing row count: {existing_row_count} | New row count: {new_row_count} "
                  f"({ratio:.0%} of existing)", logger)
            if ratio < min_row_count_ratio and not force_overwrite:
                proceed = False
                write_skipped = True
                skip_reason = (f"new row count ({new_row_count}) was only {ratio:.0%} of the "
                                f"existing table ({existing_row_count}) - refused by the data-loss guard")
                _emit(f"\nREFUSING TO OVERWRITE: new row count is only {ratio:.0%} of the "
                      f"existing table (threshold: {min_row_count_ratio:.0%}). This looks like "
                      f"a partial/failed run, not a genuine data change. If this drop is "
                      f"expected (e.g. wards were decommissioned), pass force_overwrite=True.",
                      logger, level="warning")

        if proceed:
            _emit(f"Writing {new_row_count} row(s) to "
                  f"[{target_database}].[{target_schema}].[{target_table}]...", logger)
            try:
                df_parsed.to_sql(
                    name=target_table,
                    con=target_engine,
                    schema=target_schema,
                    if_exists="replace",
                    index=False,
                )
                _emit(f"Load complete: [{target_database}].[{target_schema}].[{target_table}]", logger)
            except Exception as e:
                lines_out = [
                    f"\nFailed to write to [{target_database}].[{target_schema}].[{target_table}]",
                    f"  {str(e)[:400]}",
                    "  Likely cause: permissions (INSERT/CREATE TABLE), a column type",
                    "  mismatch, or the table being locked/in use by another session.",
                ]
                _emit("\n".join(lines_out), logger, level="error")
                raise RuntimeError("Write to SQL Server failed - see above") from None

    # --- Step 6: verify ---
    if write_skipped:
        _emit(f"Skipping verification - no write was attempted this run. "
              f"Reason: {skip_reason}. Any row count in the target table right now "
              f"is from a PREVIOUS run, not this one.", logger)
        rows_written = 0
    else:
        with target_engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM [{target_schema}].[{target_table}]"))
            rows_written = result.scalar()
        _emit(f"Verified row count in target table: {rows_written}", logger)

    _emit(f"DONE - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", logger)

    return {
        "rows_written": rows_written,
        "write_skipped": write_skipped,
        "skip_reason": skip_reason,
        "ward_status": df_status,
    }
