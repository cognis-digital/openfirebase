# openfirebase installer (Windows / PowerShell).
# The package is source-available (not on PyPI); install from git or source.
$ErrorActionPreference = "Stop"

$Repo = "git+https://github.com/cognis-digital/openfirebase.git"

Write-Host "Installing openfirebase..."

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (Test-Cmd pipx) {
    Write-Host "-> using pipx"
    pipx install $Repo
}
elseif (Test-Cmd uv) {
    Write-Host "-> using uv"
    try { uv tool install $Repo } catch { uv pip install $Repo }
}
elseif (Test-Cmd pip) {
    Write-Host "-> using pip"
    pip install $Repo
}
elseif (Test-Cmd python) {
    Write-Host "-> using python -m pip on local source"
    python -m pip install .
}
else {
    throw "No pipx/uv/pip/python found on PATH."
}

Write-Host "Done. Try: openfirebase serve --memory"
