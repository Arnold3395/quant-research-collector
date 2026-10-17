param(
    [ValidateSet('run','doctor','report','status','verify','retry','backup','restore','reclassify','sources','download','backfill','jobs','candidates','rescreen','quarantine','replay','resolve','metrics','search','export')][string]$Command = 'run',
    [string]$Python = '',
    [string]$Config = '',
    [string[]]$Sources = @(),
    [ValidateRange(0,2147483647)][int]$Days = 0,
    [ValidateRange(-1,2147483647)][int]$MaxDownloads = -1,
    [switch]$MetadataOnly,
    [string]$DataDir = '',
    [switch]$ForceSources,
    [ValidateRange(1,2147483647)][int[]]$RecordId = @(),
    [switch]$Repair,
    [string]$Output = '',
    [string]$Archive = '',
    [string]$FromDate = '',
    [string]$UntilDate = '',
    [ValidateRange(1,2147483647)][int]$ChunkDays = 7,
    [ValidateRange(1,2147483647)][int]$MaxTasks = 10,
    [string]$JobId = '',
    [ValidateRange(1,2147483647)][int[]]$ItemId = @(),
    [string]$Reason = '',
    [string]$Query = '',
    [string]$Author = '',
    [string]$Topic = '',
    [string]$Doi = '',
    [string]$PdfStatus = '',
    [ValidateSet('published','updated','first_seen')][string]$DateField = 'published',
    [switch]$AllVersions,
    [ValidateRange(0,2147483647)][int]$Limit = 0,
    [ValidateRange(0,2147483647)][int]$Offset = 0,
    [ValidateSet('json','csv','bibtex','ris')][string]$Format = 'json'
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectPath = $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $projectPath '.venv\Scripts\python.exe'
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $localPython) { $Python = $localPython }
    elseif (Test-Path -LiteralPath $bundledPython) { $Python = $bundledPython }
    else { $Python = 'python' }
}
$env:PYTHONUTF8 = '1'
if (-not $Config) { $Config = Join-Path $projectPath 'config.toml' }
$arguments = @((Join-Path $projectPath 'collector.py'), $Command, '--config', $Config)
if ($Sources) { $arguments += @('--sources', ($Sources -join ',')) }
if ($Days -gt 0) { $arguments += @('--days', "$Days") }
if ($MaxDownloads -ge 0) { $arguments += @('--max-downloads', "$MaxDownloads") }
if ($MetadataOnly) { $arguments += '--metadata-only' }
if ($DataDir) { $arguments += @('--data-dir', $DataDir) }
if ($ForceSources) { $arguments += '--force-sources' }
foreach ($id in $RecordId) { $arguments += @('--record-id', "$id") }
if ($Repair) { $arguments += '--repair' }
if ($Output) { $arguments += @('--output', $Output) }
if ($Archive) { $arguments += @('--archive', $Archive) }
if ($FromDate) { $arguments += @('--from-date', $FromDate) }
if ($UntilDate) { $arguments += @('--until-date', $UntilDate) }
if ($Command -eq 'backfill') { $arguments += @('--chunk-days', "$ChunkDays", '--max-tasks', "$MaxTasks") }
if ($JobId) { $arguments += @('--job-id', $JobId) }
foreach ($id in $ItemId) { $arguments += @('--item-id', "$id") }
if ($Reason) { $arguments += @('--reason', $Reason) }
if ($Query) { $arguments += @('--query', $Query) }
if ($Author) { $arguments += @('--author', $Author) }
if ($Topic) { $arguments += @('--topic', $Topic) }
if ($Doi) { $arguments += @('--doi', $Doi) }
if ($PdfStatus) { $arguments += @('--pdf-status', $PdfStatus) }
if ($Command -in @('search','export')) { $arguments += @('--date-field', $DateField, '--offset', "$Offset") }
if ($AllVersions) { $arguments += '--all-versions' }
if ($Limit -gt 0) { $arguments += @('--limit', "$Limit") }
if ($Command -eq 'export') { $arguments += @('--format', $Format) }
& $Python @arguments
exit $LASTEXITCODE
