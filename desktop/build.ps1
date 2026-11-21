# Builds the AI Butler ERP desktop app: dist\AI Butler ERP\AI Butler ERP.exe
# Needs: backend\.venv with the backend installed plus the desktop extra
#   backend\.venv\Scripts\pip install -e "backend[local-ocr,desktop]"
# and Node for the web app. Run from the repository root:  .\desktop\build.ps1
# native tools write progress to stderr; failures are caught by exit code below
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "backend\.venv\Scripts\python.exe"

Push-Location (Join-Path $root "frontend")
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
} finally { Pop-Location }

$backend = Join-Path $root "backend"
$sep = ";"
$pyiArgs = @(
    "-m", "PyInstaller", (Join-Path $PSScriptRoot "launcher.py"),
    "--name", "AI Butler ERP",
    "--noconfirm", "--clean", "--windowed",
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build\pyinstaller"),
    "--specpath", (Join-Path $root "build"),
    "--paths", $backend,
    "--add-data", "$(Join-Path $root 'frontend\dist')${sep}web",
    "--add-data", "$(Join-Path $backend 'alembic')${sep}alembic",
    "--add-data", "$(Join-Path $backend 'demo_kb')${sep}demo_kb",
    "--collect-submodules", "app",
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "psycopg",
    "--collect-data", "tzdata",
    "--collect-all", "rapidocr",
    "--collect-all", "pwdlib",
    "--collect-all", "argon2",
    "--exclude-module", "fastembed",
    "--exclude-module", "pytest",
    "--exclude-module", "tkinter"
)
& $py @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built: $(Join-Path $root 'dist\AI Butler ERP\AI Butler ERP.exe')"
