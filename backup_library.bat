@echo off
rem Back up a DJ library and rekordbox's own folder to another drive:
rem   backup_library.bat LIBRARY BACKUP      e.g.  backup_library.bat "D:\DJ\Library" "B:\DJ Backup"
rem   LIBRARY                     -> BACKUP\Library    (exact mirror: /MIR also deletes from the
rem                                                      backup what you deleted from the library)
rem   %APPDATA%\Pioneer\rekordbox -> BACKUP\rekordbox  (skipped while rekordbox is running,
rem                                                      because its database is in use)
rem LIBRARY\WAV (WAV / AIFF for rekordbox), if there is one, goes to the backup as its FLAC
rem copies in "LIBRARY\WAV - FLAC backup" (same audio, half the size), brought up to date first.
setlocal
if "%~2"=="" (
    echo usage: %~nx0 LIBRARY BACKUP   e.g. %~nx0 "D:\DJ\Library" "B:\DJ Backup"
    exit /b 2
)
set "SRC=%~1"
set "DST=%~2"
if not exist "%SRC%\" (
    echo no such library folder: %SRC%
    exit /b 2
)
if not exist "%~dp0cache" mkdir "%~dp0cache"
set "LOG=%~dp0cache\backup_library.log"

set FLACRC=0
if exist "%SRC%\WAV" (
    call "%~dp0mixarchive.bat" files flaccopy "--from=%SRC%\WAV" "--to=%SRC%\WAV - FLAC backup"
    if errorlevel 1 set FLACRC=1
)

rem robocopy exit codes: 0-7 = success (1 = files copied), 8+ = failures
robocopy "%SRC%" "%DST%\Library" /MIR /XD "%SRC%\WAV" /COPY:DAT /DCOPY:DAT /MT:8 /R:2 /W:5 /NP /NDL /NFL /LOG:"%LOG%" /TEE
set RC=%ERRORLEVEL%
if %FLACRC%==1 (
    echo FLAC COPIES HAD FAILURES - those WAV / AIFF files are not in this backup
    set RC=8
)
if %RC% GEQ 8 echo LIBRARY BACKUP HAD FAILURES - see %LOG%

tasklist /FI "IMAGENAME eq rekordbox.exe" | find /I "rekordbox.exe" >nul
if %ERRORLEVEL%==0 (
    echo rekordbox is running - skipped its database; close it and run this again to include it
) else (
    robocopy "%APPDATA%\Pioneer\rekordbox" "%DST%\rekordbox" /MIR /COPY:DAT /DCOPY:DAT /MT:8 /R:2 /W:5 /NP /NDL /NFL /LOG+:"%LOG%" /TEE
    if errorlevel 8 set RC=8
)
if %RC% GEQ 8 (exit /b 1) else (exit /b 0)
