param([string]$Python = 'python.exe')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$python = (Get-Command $Python -ErrorAction Stop).Source
$work = Join-Path $root 'build'
$dist = Join-Path $root 'dist'
& $python -m PyInstaller --noconfirm --noupx --onedir --windowed --name CodeXProjectFinder --add-data "$root\index.html;." --distpath $dist --workpath $work --specpath $work "$root\app.py"
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
$bundle = Join-Path $dist 'CodeXProjectFinder'
foreach ($name in @('README.md', 'LICENSE')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $bundle
}
$notices = Join-Path $bundle 'third-party-licenses'
New-Item -ItemType Directory -Path $notices -Force | Out-Null
$runtime = & $python -c 'import sys; print(sys.base_prefix)'
if ($LASTEXITCODE -ne 0) { throw 'Python runtime path not found.' }
Copy-Item -LiteralPath (Join-Path $runtime 'LICENSE.txt') -Destination (Join-Path $notices 'Python-LICENSE.txt')
$licenseDir = & $python -c 'import importlib.metadata as m; d=m.distribution("pyinstaller"); print(d.locate_file(next(f for f in d.files if f.name == "COPYING.txt")).parent)'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller license not found.' }
Get-ChildItem -LiteralPath $licenseDir -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $notices ('PyInstaller-' + $_.Name))
}
Write-Host "Bundle: $bundle"
