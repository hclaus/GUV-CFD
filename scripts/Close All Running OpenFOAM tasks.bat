@echo off
setlocal

echo Currently running OpenFOAM processes in WSL:
echo.
wsl -e bash -lc "ps aux | grep -E 'pimpleFoam|simpleFoam|potentialFoam|blockMesh|snappyHexMesh|checkMesh|topoSet|createPatch|decomposePar|reconstructPar|postProcess|writeCellCentres' | grep -v grep"

echo.
echo ---------------------------------------------------------------
echo This will force-kill ALL of the above, in every WSL distro/project -
echo not just one run. Anything currently solving will be lost.
echo ---------------------------------------------------------------
echo.
choice /C YN /M "Kill all of the above now"
if errorlevel 2 goto :cancelled

echo.
echo Killing OpenFOAM processes...
wsl -e bash -lc "pkill -9 -f pimpleFoam; pkill -9 -f simpleFoam; pkill -9 -f potentialFoam; pkill -9 -f blockMesh; pkill -9 -f snappyHexMesh; pkill -9 -f checkMesh; pkill -9 -f topoSet; pkill -9 -f createPatch; pkill -9 -f decomposePar; pkill -9 -f reconstructPar; pkill -9 -f postProcess; pkill -9 -f writeCellCentres"

echo.
echo Remaining OpenFOAM processes (should be none):
wsl -e bash -lc "ps aux | grep -E 'pimpleFoam|simpleFoam|potentialFoam|blockMesh|snappyHexMesh|checkMesh|topoSet|createPatch|decomposePar|reconstructPar|postProcess|writeCellCentres' | grep -v grep"
goto :end

:cancelled
echo.
echo Cancelled - nothing was killed.

:end
echo.
pause
