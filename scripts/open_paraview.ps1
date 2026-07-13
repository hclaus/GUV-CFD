<#
.SYNOPSIS
    Open ParaView pointed at an OpenFOAM case directory (its case.foam file).

.PARAMETER CaseDir
    Path to the case directory (e.g. \\wsl.localhost\Ubuntu\home\...\run\4x3x2.5-3).
    Defaults to the current directory if not given.

.EXAMPLE
    .\open_paraview.ps1 '\\wsl.localhost\Ubuntu\home\hclaus\OpenFOAM\hclaus-v2412\run\4x3x2.5-3'
#>
param(
    [string]$CaseDir = (Get-Location).Path
)

$paraviewExe = Get-ChildItem "C:\Program Files\ParaView*\bin\paraview.exe", "C:\Program Files (x86)\ParaView*\bin\paraview.exe" `
    -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName

if (-not $paraviewExe) {
    Write-Error "ParaView not found under Program Files. Install it, or edit this script with the correct path."
    exit 1
}

$caseFoam = Join-Path $CaseDir "case.foam"
if (-not (Test-Path $caseFoam)) {
    Write-Error "No case.foam found at $caseFoam - check the folder path."
    exit 1
}

Write-Output "Opening $caseFoam with $paraviewExe ..."
Start-Process -FilePath $paraviewExe -ArgumentList "`"$caseFoam`""
