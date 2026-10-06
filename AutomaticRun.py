# -*- coding: utf-8 -*-
r"""
@author: venu.madhav (based on template by simon.newey)

Title: AutomaticRun - CaMIS Container/Bay XML Loader
File Name: AutomaticRun.py

Description:
    Scheduled version of the CaMIS WardLayout XML loader. Pulls
    WardLayout XML for every ward in CaMIS, parses the Bay/Container ->
    Placement structure, and loads it into
    [xml].[container_bay_xml] in Production_CaMISStaging.

    This is "Solution C" from the corridor-care / bed occupancy work -
    classifying "no bay assigned" beds by their actual container type.

    See notebooks/load_container_bay_xml.ipynb for the original
    interactive development version (kept for troubleshooting - run it
    cell-by-cell if something breaks here, the diagnostics are easier
    to read interactively).

    Core logic lives in Tools/CamisXmlLoader.py, per the template's
    convention of keeping imported logic out of this file.

Last updated: 30/09/2026
"""

import os
import sys
import logging
from yaml import safe_load
from Tools.Utilities import MakeStandardSetup, MakeSqlEngine
from Tools.CamisXmlLoader import run_load

Root = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(Root, 'Config.yml'), 'r') as file:
    config_dict = safe_load(file)

WorkingDirectory = config_dict['WorkingDirectory']
LogsDirectory = config_dict['LogsDirectory']

FileName = os.path.splitext(os.path.basename(__file__))[0]
ScriptDirectory = ''
WorkingPath = os.path.join(Root, WorkingDirectory)
init_dir = os.getcwd()
if WorkingPath not in sys.path:
    sys.path.insert(0, WorkingPath)

StandardSetup = MakeStandardSetup(Root=Root,
                                  ScriptDirectory=ScriptDirectory,
                                  LogsDirectory=LogsDirectory,
                                  init_dir=init_dir,
                                  WorkingDirectory=WorkingDirectory,
                                  FileName=FileName)


@StandardSetup
def run():
    # *** Your code here ***

    # SetLogs() (called inside the StandardSetup decorator, before this
    # function runs) configures loggers named '__main__(R)' / '__main__(E)'
    # when this script is run directly. Fetching them again by the SAME
    # name returns the SAME configured logger objects (Python's logging
    # module is a global registry keyed by name) - this is how run_load()
    # gets access to RunLog without the decorator needing to pass it in
    # as an argument. This means our run summary and diagnostics land in
    # the actual log files Simon's system monitors, not just stdout that
    # nobody sees on an unattended scheduled run.
    RunLog = logging.getLogger(__name__ + '(R)')

    # --- Source connection ---
    # CAM_LIVE_ODBC is a LINKED SERVER on INFODW1, referenced via 4-part
    # naming inside the query itself - so the database we connect to here
    # doesn't matter for the source query (it never references local
    # unqualified tables). Defaults to Config.yml's HomeDatabase.
    #
    # NOTE: MakeSqlEngine hardcodes 'ODBC Driver 17 for SQL Server'. If
    # this ever fails with an IM002 "data source name not found" error,
    # that's a driver mismatch on THIS machine, not a bug in this script
    # - see Tools/CamisXmlLoader.explain_sql_error's IM002 branch, and
    # raise it with Simon Newey.
    source_engine = MakeSqlEngine(server=config_dict.get('CamisSourceServer', 'INFODW1'))

    # --- Target connection ---
    target_engine = MakeSqlEngine(
        server=config_dict.get('CamisTargetServer', 'INFODW1'),
        database=config_dict.get('CamisTargetDatabase', 'Production_CaMISStaging'),
    )

    result = run_load(
        source_engine, target_engine,
        target_database=config_dict.get('CamisTargetDatabase', 'Production_CaMISStaging'),
        target_schema=config_dict.get('CamisTargetSchema', 'xml'),
        target_table=config_dict.get('CamisTargetTable', 'container_bay_xml'),
        auto_discover_all_wards=config_dict.get('CamisAutoDiscoverAllWards', True),
        use_openquery=config_dict.get('CamisUseOpenquery', False),
        min_row_count_ratio=config_dict.get('CamisMinRowCountRatio', 0.7),
        force_overwrite=config_dict.get('CamisForceOverwrite', False),
        logger=RunLog,
    )

    if result["write_skipped"]:
        # Not necessarily an error (could be a legitimately empty pull),
        # but worth failing the run loudly so it shows up in the error
        # log / email notification rather than silently doing nothing -
        # a scheduled job that "succeeds" while writing zero rows is
        # exactly the kind of silent failure the data-loss guard exists
        # to surface, not hide.
        raise RuntimeError(
            f"Write was skipped: {result['skip_reason']}. "
            f"See RunLog for the full run summary."
        )


if __name__ == '__main__':
    run()
