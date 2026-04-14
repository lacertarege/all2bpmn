param(
    [string]$PythonExe = ".venv\Scripts\python.exe",
    [string]$AppName = "all2bpmn"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path $PythonExe)) {
    $pythonCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "No se encontro Python en '$PythonExe'. Ajusta el parametro -PythonExe."
    }
    $PythonExe = $pythonCommand.Source
}

& $PythonExe -m pip install --upgrade pip pyinstaller

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --name $AppName `
    --console `
    --paths "src" `
    --collect-submodules "PySide6" `
    --hidden-import "PySide6.QtWebEngineWidgets" `
    --hidden-import "PySide6.QtWebEngineCore" `
    --hidden-import "PySide6.QtWebChannel" `
    --add-data "src\pdf_to_bpmn\ui\assets;pdf_to_bpmn\ui\assets" `
    "src\pdf_to_bpmn\cli.py"

$distDir = Join-Path $root "dist\$AppName"
if (Test-Path ".env.example") {
    Copy-Item ".env.example" (Join-Path $distDir ".env.example") -Force
}

Write-Host ""
Write-Host "Build completado en: $distDir"
Write-Host "Coloca un archivo .env junto a $AppName.exe para que el ejecutable cargue las API keys."
