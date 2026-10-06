:: This batch file is provided as an example.
:: The location of your Miniconda/Anaconda install must be filled in.
:: You may also wish to change the environment name from Main, if you are using an environment with a different name.
::
:: NOTE (venu.madhav): the template default environment name is "Main" -
:: changed to "dbth_main" below to match the environment this loader was
:: built and tested in. Confirm with Simon Newey whether the scheduling
:: host expects a specific shared environment name instead.

echo on
set FilePath =< "%~dp0..\..\AnacondaFilePath.txt"
echo %FilePath%
if not exist "%~dp0Logs" mkdir "%~dp0Logs"
call %FilePath%\condabin\conda run -n dbth_main python "%~dp0AutomaticRun.py"
pause
