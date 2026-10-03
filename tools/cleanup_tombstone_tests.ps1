<#
.SYNOPSIS
    Removes dead test tombstones: test files that contain zero tests.

.DESCRIPTION
    During the MCP testing audit these files were left behind. They match the
    test-file pattern but contain no test functions, so pytest never collects
    them (python_files = ["test_*.py"]). They are dead weight: they look like
    coverage but provide none.

    Safety rules the script enforces before touching anything:
      1. DRY RUN BY DEFAULT. Pass -Apply to actually delete.
      2. Every file is re-checked at run time: if it now contains a
         "def test_" function, the script ABORTS for that file. Someone may
         have restored it since the audit.
      3. The empty package skeletons are only removed if they contain
         nothing but zero-byte __init__.py files.
      4. Files that look similar but are NOT tombstones are explicitly
         protected and never touched:
           - tools/apply_test_profile_tables.py  (real utility, 97 lines;
             its name merely CONTAINS "_test_", it does not start with it)
           - tests/benchmarks/_conftest.py       (fixtures for the benchmark)
           - mcp-platform/tests/legal_summarizer/fixtures/analysis_cases/
                                                  (README.md with real content)
      5. Dirs are removed with rmdir semantics, never recursively, so a
         directory that still holds files is left in place.

.PARAMETER Apply
    Perform the deletion. Without it, the script only reports.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools/cleanup_tombstone_tests.ps1
    powershell -ExecutionPolicy Bypass -File tools/cleanup_tombstone_tests.ps1 -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot) | Out-Null

# --- Tombstone files: 0 test functions each, never collected by pytest ------
$tombstones = @(
    'tests/_test_information_preservation.py'
    'tests/_test_legal_summarizer_identity.py'
    'tests/_test_legal_summarizer_query_ipc.py'
    'tests/_test_legal_summarizer_query_manifest_integration.py'
    'tests/_test_legal_summarizer_running_subprocess.py'
    'tests/_test_manifest.py'
    'tests/_test_resume_scenarios.py'
    'tests/_test_skill_legal_summarizer_characterization.py'
    'tests/_test_structure_physical.py'
    'tests/benchmarks/_test_acceptance_matrix.py'
    'mcp-platform/tests/legal_summarizer/_test_legal_summarizer_no_legacy.py'
    'mcp-platform/tests/legal_summarizer/_test_structure_architecture_guard.py'
)

# --- Empty package skeletons: only zero-byte __init__.py, no references ------
$skeletons = @(
    'mcp-platform/tests/legal_summarizer/unit'
    'mcp-platform/tests/legal_summarizer/integration'
)

# --- Never touch, even if a future glob would match them --------------------
$protected = @(
    'tools/apply_test_profile_tables.py'
    'tests/benchmarks/_conftest.py'
    'tests/benchmarks/test_quality_benchmark.py'
    'mcp-platform/tests/legal_summarizer/fixtures'
)

$removed = 0
$skipped = @()

Write-Host ''
Write-Host 'Tombstone cleanup' -ForegroundColor Cyan
Write-Host ("  mode: " + $(if ($Apply) { 'APPLY' } else { 'DRY RUN (pass -Apply to delete)' }))
Write-Host ''

# ---- 1. Files ---------------------------------------------------------------
foreach ($f in $tombstones) {
    if ($protected -contains $f) {
        $skipped += "$f  [protected]"
        continue
    }
    if (-not (Test-Path $f)) {
        $skipped += "$f  [already gone]"
        continue
    }

    $testCount = (Select-String -Path $f -Pattern '^\s*(async\s+)?def\s+test_' | Measure-Object).Count
    if ($testCount -ne 0) {
        $skipped += "$f  [SKIPPED: now has $testCount test(s) - someone restored it]"
        continue
    }

    if ($Apply) {
        $out = git rm --quiet -- $f 2>&1
        if ($LASTEXITCODE -ne 0) { $skipped += "$f  [git rm failed: $out]"; continue }
        Write-Host "  removed  $f" -ForegroundColor Green
    } else {
        Write-Host "  would remove  $f" -ForegroundColor DarkGray
    }
    $removed++
}

# ---- 2. Empty package skeletons ---------------------------------------------
foreach ($d in $skeletons) {
    if (-not (Test-Path $d)) {
        $skipped += "$d/  [already gone]"
        continue
    }

    $contents = @(Get-ChildItem $d -Recurse -File)
    # A file counts as empty when it holds nothing but whitespace (these
    # __init__.py are a single newline byte, not literally zero-length).
    $nonEmpty = @($contents | Where-Object {
        ([System.IO.File]::ReadAllText($_.FullName)).Trim().Length -ne 0
    })
    $notInit  = @($contents | Where-Object { $_.Name -ne '__init__.py' })

    if ($nonEmpty.Count -ne 0) {
        $skipped += "$d/  [SKIPPED: holds $($nonEmpty.Count) non-empty file(s)]"
        continue
    }
    if ($notInit.Count -ne 0) {
        $skipped += "$d/  [SKIPPED: holds files other than __init__.py]"
        continue
    }

    if ($Apply) {
        # Only zero-byte __init__.py files remain, and only here: safe to drop.
        Get-ChildItem $d -Recurse -File | ForEach-Object { [System.IO.File]::Delete($_.FullName) }
        $dirs = @(Get-ChildItem $d -Recurse -Directory | Sort-Object { $_.FullName.Length } -Descending)
        $dirs += Get-Item $d
        foreach ($dir in $dirs) {
            if (@(Get-ChildItem $dir.FullName -Force).Count -eq 0) {
                [System.IO.Directory]::Delete($dir.FullName)
            }
        }
        Write-Host "  removed  $d/  ($($contents.Count) empty __init__.py)" -ForegroundColor Green
    } else {
        Write-Host "  would remove  $d/  ($($contents.Count) empty __init__.py)" -ForegroundColor DarkGray
    }
    $removed++
}

# ---- 3. Report --------------------------------------------------------------
if ($skipped.Count -ne 0) {
    Write-Host ''
    Write-Host '  Skipped:' -ForegroundColor Yellow
    foreach ($s in $skipped) { Write-Host "    $s" -ForegroundColor Yellow }
}

Write-Host ''
if ($Apply) {
    Write-Host "Removed: $removed item(s)." -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'Verify and commit:' -ForegroundColor Cyan
    Write-Host '  python -m pytest tests --collect-only -q'
    Write-Host '  python -m pytest tests -q -m "not live and not integration"'
    Write-Host '  git status'
    Write-Host ''
    Write-Host 'Undo (restores the deleted files):' -ForegroundColor Cyan
    Write-Host '  git restore --staged --worktree tests mcp-platform/tests'
} else {
    Write-Host "Would remove: $removed item(s). Nothing was changed." -ForegroundColor Cyan
    Write-Host 'Re-run with -Apply to delete.' -ForegroundColor Cyan
}
Write-Host ''
