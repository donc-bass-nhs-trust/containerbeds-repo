# -*- coding: utf-8 -*-
"""
Created on Fri Sep 24 11:19:50 2021

@author: Simon.Newey

Description:
    This module contains utility functions for running standard scripts:
        SetLogs():
            Set up script loggers for script.
        MakeSqlEngine():
            Set up a sqlalchemy engine linking to specified database. This
            function has been seperated for security reasons, and should not be
            shared.
        MakeEnd():
            Returns a function for ending scripts properly. This logs activity
            and errors, and returns to the home directory from which the script
            was started.
        MakeStandardSetup():
            Make standard decorator to handle logging and directory management
            when applied to a generic run function

Last updated: 24/7/2023
Update 01/02/2024 (SN): Modified error notification procedure followind
                        sqlalchemy update. (Added text type and commit.)

Update 07/10/2024 (SN): Moved into project repo. Converted to use SecureConfig
                        to prodect server and database names.
"""

import logging
import sqlalchemy
import os
from datetime import datetime
from sqlalchemy import text
from yaml import safe_load

ConfigPath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'Config.yml')
with open(ConfigPath, 'r') as file:
    ConfigDict = safe_load(file)


def SetLogs(_name_, LogPath, FileName):
    '''Creates standard script loggers.
    Typically pass: _name_=__name__, LogPath=the configured log directory.
    Returns ErrorLog,RunLog'''
    os.makedirs(LogPath, exist_ok=True)
    ErrorLog = logging.getLogger(_name_+'(E)')
    ErrorLog.handlers.clear()
    h = logging.FileHandler(filename=os.path.join(LogPath, FileName+'_ErrorLog.log'))
    h.setFormatter(logging.Formatter(
        ('-'*65)+'\n%(asctime)s\n%(name)s: %(levelname)s\n%(message)s\n'))
    ErrorLog.addHandler(h)

    RunLog = logging.getLogger(_name_+'(R)')
    RunLog.handlers.clear()
    RunLog.setLevel(logging.INFO)
    h = logging.FileHandler(filename=os.path.join(LogPath, FileName+'_RunLog.log'))
    h.setFormatter(logging.Formatter('%(message)s'))
    RunLog.addHandler(h)
    RunLog.info(('-'*65))
    RunLog.info(f'{FileName}.py initiated at {datetime.now()}\n')
    h.setFormatter(logging.Formatter(
        '%(asctime)s\n%(name)s: %(levelname)s\n%(message)s\n'))
    RunLog.addHandler(h)

    return ErrorLog, RunLog


def MakeSqlEngine(database=ConfigDict['HomeDatabase'],
                  server=ConfigDict['SqlServerName']):
    engine = sqlalchemy.create_engine(
        f'mssql+pyodbc://{server}/{database}'
        '?driver=ODBC+Driver+17+for+SQL+Server',
        fast_executemany=True)
    return engine


def MakeEnd(RunLog, ErrorLog, FileName, LogPath, home_dir, log_notification_ID=78):
    def end(NoErrors):
        'Function writes to RunLog, and returns to starting directory.'
        if NoErrors:
            RunLog.info(f'{FileName}.py successfully executed.')
        else:
            RunLog.error(f'{FileName}.py executed *with* errors!')
            engine = MakeSqlEngine('Production_CorporateTeam')
            db_name = ConfigDict['EmailSystemDatabase']
            if db_name:     # Send email notification if apropriate database is provided
                sql = f'''
insert into  {db_name}.[dbo].[tblEmailNotificationEvents] (
    [FKNotificationID],
    [EventType],
    [Value])
values ({log_notification_ID},
        'New error logged',
        'Error in {FileName}.py.<br>Check {LogPath} for details.')
    '''
                with engine.connect() as conn:
                    conn.execute(text(sql))
                    conn.commit()

        os.chdir(home_dir)
    return end


def MakeStandardSetup(Root: str,
                      ScriptDirectory: str,
                      LogsDirectory: str,
                      init_dir: str,
                      WorkingDirectory: str,
                      FileName: str):
    '''Make standard decorator to handle logging and directory management when
    applied to a generic run function. This decorator will execute the run
    function within the working directory, then return to the init_dir.
    Run-logs and error logs will be populated. If errors occur, emails will be
    sent, via the sql email system.
    '''
    def StandardSetup(run):
        def out(*args, **kwargs):
            NoErrors = True
            ErrorLog, RunLog = SetLogs(
                _name_=__name__,
                LogPath=os.path.join(Root, ScriptDirectory, LogsDirectory),
                FileName=FileName
                )
            end = MakeEnd(
                RunLog=RunLog,
                ErrorLog=ErrorLog,
                FileName=FileName,
                LogPath=os.path.join(Root, ScriptDirectory, LogsDirectory),
                home_dir=init_dir,
                log_notification_ID=ConfigDict['FKNotificationID']
                )
            try:
                os.chdir(os.path.join(Root, WorkingDirectory))
            except Exception as e:
                NoErrors = False
                ErrorLog.exception('Dirrectory error')
                end(NoErrors)
                return
            try:
                run(*args, **kwargs)
            except Exception as e:
                NoErrors = False
                ErrorLog.exception('Run error')
            finally:
                end(NoErrors)
        return out
    return StandardSetup


if __name__ == '__main__':
    # Procedure testing
    engine = MakeSqlEngine()
    FileName = '*TestName*'
    LogPath = '*TestPath*'
    db_name = ConfigDict['EmailSystemDatabase']
    if db_name:
        sql = f"""
        insert into  {db_name}.[dbo].[tblEmailNotificationEvents] (
            [FKNotificationID],
            [EventType],
            [Value])
        values (78,
            'New error logged',
            'Error in {FileName}.py.<br>Check {LogPath} for details.');
    """
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
