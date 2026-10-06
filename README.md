# Container Bay XML Loader (Corridor Care - Solution C)

## Purpose

Loads CaMIS `WardLayout` XML data into
`[Production_CaMISStaging].[xml].[container_bay_xml]`, parsed into one row
per bed/chair/trolley placement with its parent Bay or Container context.

This is **"Solution C"** from the corridor-care / bed occupancy reporting
work: `vtbl_BedOccupancy` drops any bed/chair with no `FKWardBayID` (chairs
and trolleys placed in CaMIS "Containers" rather than formal Bays never get
a bay row created at all). This loader doesn't fix that drop on its own
(that's Solution A) - it builds the data needed to classify *which* of
those bay-less beds are genuine corridor care (e.g. a container captioned
`NEXT PATIENT`) versus something else entirely (`Virtual Discharge Area`,
`SWAP AREA`, etc.), by joining `PlacementID` to `BedIdentifierLOCAL`.

## How it works

1. **Discover** every ward code with a `WardLayout` document in CaMIS
   (small query, ward codes only - no XML transferred).
2. **Fetch** each ward's `WardLayout` XML via the `CAM_LIVE_ODBC` linked
   server (Informix-backed, accessed via 4-part naming).
3. **Recover** from two confirmed transit-corruption patterns
   automatically: unescaped `&` in free-text captions, and stray NUL
   bytes/newlines spliced into large values in transit (a buffer-boundary
   artifact at the MSDASQL/Informix driver bridge).
4. **Parse** the `Bay`/`Container` -> `Placement` structure (plus beds
   that sit with no wrapper at all, tagged `Unassigned` - confirmed on
   ward ERU).
5. **Guard**: refuse to overwrite the target table if the new pull's row
   count looks like a partial/failed run rather than a genuine change.
6. **Load** the result, tagged with `DocumentLength`, `IsDocumentComplete`,
   and `LoadTimestamp` so data quality is visible in the table itself.

## Repo structure

```
containerbeds-repo/
├── AutomaticRun.py              # Entry point - company template, run() calls our loader
├── Config.yml                   # Local config - NOT committed (see below)
├── Config.yml.example           # Template config - commit this, copy to Config.yml locally
├── Run.bat                      # Scheduled execution entry point
├── requirements.txt             # Unchanged from repo template (see note below)
├── LatestDevEnvironment.yml     # Unchanged from repo template (see note below)
├── Tools/
│   ├── Utilities.py              # Shared company infra (MakeSqlEngine, MakeStandardSetup) - unmodified
│   └── CamisXmlLoader.py         # All loader logic: query building, XML recovery/parsing, load orchestration
├── notebooks/
│   └── load_container_bay_xml.ipynb   # Original interactive dev notebook - kept for troubleshooting
└── Logs/                         # Run/error logs land here (gitignored, folder itself is kept)
```

## Known open item

Some wards (confirmed on A4, in certain runs) return `xdi_xml` truncated
at ~32,543 characters with no closing tag - close to Informix's practical
`VARCHAR`/`NVARCHAR`/`LVARCHAR` ceiling (~32,739 chars). This may be a
genuine source-side storage limit rather than a transit issue the loader
can recover from. If this recurs, the run summary will flag it as "near
Informix limit" - confirming it definitively needs checking `xdi_xml`'s
declared column length directly in Informix's own system catalog (not
reachable from this script).

## Setup

1. Copy `Config.yml.example` to `Config.yml` and fill in the real values
   (never commit `Config.yml` - it's gitignored deliberately).
   `LogsDirectory: Logs` places run/error logs in this repository's `Logs`
   folder; the folder is created automatically if needed.
2. Ensure the `dbth_main` conda environment has `pandas`, `sqlalchemy`,
   `pyodbc`, and `pyyaml` installed.
3. `requirements.txt` / `LatestDevEnvironment.yml` are unchanged from the
   repo template and reflect a different (TensorFlow-heavy) environment -
   they're **not** representative of this script's actual dependencies.
   Left as-is per the template's own README guidance; don't `pip install
   -r requirements.txt` expecting it to set up this script's environment.
4. Run manually via `notebooks/load_container_bay_xml.ipynb` (interactive,
   best for troubleshooting) or `python AutomaticRun.py` / `Run.bat`
   (scheduled path).

## Troubleshooting

Start with the notebook - it runs the same logic cell-by-cell with much
richer inline diagnostics than a scheduled run's log file gives you. The
`Tools/CamisXmlLoader.py` functions (`diagnose_parse_error`,
`explain_sql_error`) are shared between both paths, so whatever the
notebook shows you is the same logic that ran in production.
