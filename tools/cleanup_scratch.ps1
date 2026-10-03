<#
.SYNOPSIS
    Уборка одноразовых артефактов в репозитории.

.DESCRIPTION
    Скрипт удаляет ТОЛЬКО заранее проверенный список путей. Шаблонов и
    массового glob-удаления здесь нет намеренно: в этом репозитории лежит
    незакоммиченная работа, а параллельно в него пишет другой агент. Шаблон
    вида «*.tmp» однажды снёс бы чужой файл молча, и это невосстановимо.

    Что защищено и почему — перечислено в $Protected ниже. Если путь из
    манифеста окажется под git, скрипт откажется его удалять: значит, список
    устарел и требует пересмотра, а не молчаливого сноса.

    Отдельно перечислены $KnownDeadTrees — остатки прежнего корня файлов
    сессий и деревья бага с двойным `cache`. Это НЕ список на удаление: в них
    лежат данные прошлых сессий, скрипт их не трогает даже с -Apply и только
    показывает вместе с пометкой, что решение за владельцем.

.PARAMETER Apply
    Без этого ключа скрипт только показывает, что он удалил бы. Удаление
    запускается явно.

.PARAMETER IncludePycache
    Дополнительно снести каталоги __pycache__ (их тут сотни, это заметно
    ускоряет тесты). По умолчанию НЕ трогает: без причины замедлять
    следующий прогон незачем.

.EXAMPLE
    .\cleanup_scratch.ps1
    Показать план.

.EXAMPLE
    .\cleanup_scratch.ps1 -Apply
    Выполнить уборку.

.EXAMPLE
    .\cleanup_scratch.ps1 -Apply -IncludePycache
    Уборка вместе с кэшами байткода.
#>

[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$IncludePycache
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot

# --- Что удаляем: проверенный список, каждый пункт подтверждён вручную ------

# Пути относительно корня репозитория.
$ScratchFiles = @(
    @{ Path = '_wd_check.py'; Why = 'временный скрипт проверки воркера' }
    @{ Path = '_negcheck_tmp.py'; Why = 'временный негативный тест проверки ## Scope' }
    @{ Path = 'logs/_tmp_linkcheck.py'; Why = 'временный скрипт проверки ссылок' }
    @{ Path = 'logs/_tmp_patchspec_check.py'; Why = 'временный скрипт проверки _PATCH_SPECS' }
)

$ScratchDirs = @(
    @{ Path = 'mcp-platform/.sessions_demo'; Why = 'демо-сессии платформы; .gitignore прямо называет их одноразовыми, ссылок в коде 0' }
    @{ Path = 'data_store/cache/scratch'; Why = 'одноразовые скрипты обследования от 2026-09-18; .gitignore игнорирует весь data_store/' }
    @{ Path = '.pytest_cache'; Why = 'пересоздаётся при каждом прогоне' }
)

# --- Что НЕ трогаем, даже если очень похоже на мусор ------------------------

$Protected = @(
    'workspace/data_store/sessions'        # ЖИВОЕ хранилище файлов сессий: корень объявляет платформа (platform.json → execution.session_root = ${NANOBOT_WORKSPACE}/data_store/sessions). Прежняя запись охраняла mcp-platform/.sessions, которая живым хранилищем больше не является.
    'mcp-platform/_live_audit_tables.py'      # Живая фикстура: на неё ссылается mcp-platform/tests/test_journal_contract_visibility.py
    'mcp-platform/servers/enterprise/tools'   # Операция read_result (недавняя работа, не закоммичена)
    'mcp-platform/tests/test_read_result_operation.py'  # Тесты read_result
    'openspec/specs/OWNERSHIP.md'             # Индекс владения спеками
    '.secrets.env'                            # Секреты
)

# --- Известные мёртвые деревья прежнего корня: НЕ сносить молча -------------

# Это НЕ список на удаление, и он намеренно не в `$ScratchDirs`. Каталоги
# остались от бага с двойным `cache` (`data_store/cache` + `cache` каталога
# сессии) и от прежнего корня `data_store/cache/sessions`. Код их больше не
# читает, но внутри лежат настоящие данные прошлых сессий — отчёт аудита в md,
# снимок application_context.py, строки pytest, черновики сообщений коммитов.
# Снести их молча нельзя, а разбирать содержимое уборщик не умеет: решение
# владельца. Разбор — `PENDING-DELETIONS.md`, «Фаза 4 (2026-10-03-session-files)».

$KnownDeadTrees = @(
    @{ Path = 'workspace/data_store/cache/cache'; Guard = $true;  Why = 'вывод теста, ушедший в дерево с двойным cache; внутри sessions/test_1/attachments/' }
    @{ Path = 'workspace/data_store/media/cache'; Guard = $true;  Why = 'пустые каталоги прежнего бага' }
    @{ Path = 'workspace/data_store/cache/sessions'; Guard = $true; Why = 'старый корень сессий: рабочие материалы прошлой сессии агента, НЕ мусор' }
    @{ Path = 'data_store'; Guard = $false; Why = 'корень репозитория: cache/commit_msg_*.txt, черновики сообщений коммитов. Без охраны по префиксу: data_store/cache/scratch в списке на удаление проверен отдельно и ждать решения владельца не должен' }
)

# --- Вспомогательное -------------------------------------------------------

function Test-RelativeSafe {
    param([string]$Rel)
    # Относительный путь из манифеста не должен быть укоренённым и не должен
    # содержать выходов вверх. Проверяем ДО склейки с корнем: `Join-Path` с
    # укоренённым аргументом даёт мусор вида `<корень>\C:\Windows\...`, на
    # котором `GetFullPath` бросает исключение, и скрипт падал бы вместо того,
    # чтобы вежливо отказать.
    if ($Rel -match '^[\\/]' -or $Rel -match '^[A-Za-z]:') { return $false }
    $depth = 0
    foreach ($part in ($Rel -split '[\\/]')) {
        if ($part -eq '..') { $depth-- } elseif ($part -ne '.' -and $part -ne '') { $depth++ }
        if ($depth -lt 0) { return $false }
    }
    return $true
}

function Test-InRepo {
    param([string]$FullPath)
    $root = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
    try {
        $target = [System.IO.Path]::GetFullPath($FullPath)
    } catch {
        # Отказ важнее подробностей: непонятный путь — это не путь для сноса.
        return $false
    }
    return $target.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-TrackedPaths {
    $root = [System.IO.Path]::GetFullPath($RepoRoot)
    $inside = $false
    $paths = New-Object System.Collections.Generic.List[string]
    try {
        Push-Location $root
        $inside = $true
        $out = & git ls-files 2>$null
        foreach ($line in $out) { $paths.Add($line) }
    } catch {
        Write-Warning "git недоступен: проверка отслеживаемых файлов будет пропущена"
    } finally {
        if ($inside) { Pop-Location }
    }
    return $paths
}

# --- Сбор плана ------------------------------------------------------------

$plan = New-Object System.Collections.Generic.List[object]
$skipped = New-Object System.Collections.Generic.List[string]
$tracked = Get-TrackedPaths
$trackedSet = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($t in $tracked) { [void]$trackedSet.Add(($t -replace '/', '\')) }

$items = @()
foreach ($f in $ScratchFiles) { $items += [pscustomobject]@{ Rel = $f.Path; IsDir = $false; Why = $f.Why } }
foreach ($d in $ScratchDirs)  { $items += [pscustomobject]@{ Rel = $d.Path; IsDir = $true;  Why = $d.Why } }

if ($IncludePycache) {
    $found = Get-ChildItem -Path $RepoRoot -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.worktrees\\|\\\.git\\|\\\.venv\\' }
    foreach ($c in $found) {
        $items += [pscustomobject]@{
            Rel   = $c.FullName.Substring($RepoRoot.Length + 1)
            IsDir = $true
            Why   = 'кэш байткода, пересоздаётся интерпретатором'
        }
    }
}

foreach ($item in $items) {
    $abs = Join-Path $RepoRoot $item.Rel

    if (-not (Test-RelativeSafe $item.Rel) -or -not (Test-InRepo $abs)) {
        $skipped.Add("$($item.Rel) — путь не относительный или вне корня репозитория, пропущен")
        continue
    }
    # Строка, равная охраняемому мёртвому дереву или лежащая ВНУТРИ него,
    # отбрасывается целиком: точный список на удаление таких путей не содержит,
    # а сравнение по префиксу ловит случайную попытку добавить их в
    # `$ScratchDirs` позже. В отчёт попадает и сам факт, и причина.
    $itemAbs = [System.IO.Path]::GetFullPath($abs)
    $inDeadTree = $false
    foreach ($k in $KnownDeadTrees) {
        if (-not $k.Guard) { continue }
        $kAbs = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $k.Path))
        if ($itemAbs.TrimEnd('\') -ieq $kAbs.TrimEnd('\') -or
            $itemAbs.StartsWith($kAbs.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            $skipped.Add("$($item.Rel) — внутри известного мёртвого дерева ($($k.Path)): $($k.Why)")
            $inDeadTree = $true
            break
        }
    }
    if ($inDeadTree) { continue }
    foreach ($p in $Protected) {
        $pAbs = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $p))
        $iAbs = [System.IO.Path]::GetFullPath($abs)
        if ($iAbs.TrimEnd('\') -ieq $pAbs.TrimEnd('\')) {
            $skipped.Add("$($item.Rel) — путь защищён списком $Protected")
            continue
        }
    }
    if (Test-Path -LiteralPath $abs) {
        $relNorm = ($item.Rel -replace '/', '\')
        if ($trackedSet.Contains($relNorm)) {
            $skipped.Add("$($item.Rel) — ОТСЛЕЖИВАЕТСЯ git, список устарел")
            continue
        }
        $fs = Get-Item -LiteralPath $abs
        $size = if ($fs.PSIsContainer) {
            $sum = (Get-ChildItem -LiteralPath $abs -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
            if ($null -eq $sum) { 0 } else { $sum }
        } else { $fs.Length }
        $plan.Add([pscustomobject]@{
            Rel   = $item.Rel
            IsDir = $item.IsDir
            Size  = $size
            Age   = "{0:yyyy-MM-dd}" -f $fs.LastWriteTime
            Why   = $item.Why
            Abs   = $abs
        })
    }
}

# --- Отчёт -----------------------------------------------------------------

Write-Host ""
Write-Host "  Уборка одноразовых артефактов" -ForegroundColor Cyan
Write-Host "  Корень: $RepoRoot" -ForegroundColor DarkGray
Write-Host ""

if ($plan.Count -eq 0) {
    Write-Host "  Удалять нечего: все пути уже убраны." -ForegroundColor Green
} else {
    Write-Host ("  {0,-58} {1,10} {2,-12} {3}" -f 'ПУТЬ', 'РАЗМЕР', 'ИЗМЕНЁН', 'ЧТО ЭТО')
    Write-Host ("  " + ('-' * 110))
    foreach ($row in $plan) {
        $size = if ($row.Size -ge 1MB) { "{0:N1} МБ" -f ($row.Size / 1MB) }
                elseif ($row.Size -ge 1KB) { "{0:N1} КБ" -f ($row.Size / 1KB) }
                else { "$($row.Size) Б" }
        Write-Host ("  {0,-58} {1,10} {2,-12} {3}" -f $row.Rel, $size, $row.Age, $row.Why)
    }
    $total = ($plan | Measure-Object -Property Size -Sum).Sum
    Write-Host ("  " + ('-' * 110))
    Write-Host ("  Итого: {0} путей, {1:N0} байт" -f $plan.Count, $total)
}

if ($skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "  Пропущено:" -ForegroundColor Yellow
    foreach ($s in $skipped) { Write-Host "    - $s" -ForegroundColor DarkYellow }
}

# Известные мёртвые деревья показываются всегда, а не только когда что-то
# попало в «Пропущено»: их содержимое ждёт решения владельца, а уборщик — тот
# инструмент, у которого это решение забывают принять.
$presentDead = @()
foreach ($k in $KnownDeadTrees) {
    $kAbs = Join-Path $RepoRoot $k.Path
    if (-not (Test-Path -LiteralPath $kAbs)) { continue }
    $fs = Get-Item -LiteralPath $kAbs
    $files = @(Get-ChildItem -LiteralPath $kAbs -Recurse -File -ErrorAction SilentlyContinue)
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    $size = if ($null -eq $sum) { 0 } else { $sum }
    $presentDead += [pscustomobject]@{
        Rel   = $k.Path
        Size  = $size
        Files = $files.Count
        Age   = "{0:yyyy-MM-dd}" -f $fs.LastWriteTime
        Why   = $k.Why
    }
}
if ($presentDead.Count -gt 0) {
    Write-Host ""
    Write-Host "  Решение владельца (уборщик не трогает, -Apply тоже):" -ForegroundColor Magenta
    foreach ($row in $presentDead) {
        $size = if ($row.Size -ge 1MB) { "{0:N1} МБ" -f ($row.Size / 1MB) }
                elseif ($row.Size -ge 1KB) { "{0:N1} КБ" -f ($row.Size / 1KB) }
                else { "$($row.Size) Б" }
        Write-Host ("    {0,-44} {1,10} {2,7}  {3}" -f $row.Rel, $size, "$($row.Files) ф", $row.Why) -ForegroundColor DarkMagenta
    }
}

# --- Действие --------------------------------------------------------------

if (-not $Apply) {
    Write-Host ""
    Write-Host "  Это был показ. Для удаления запустите с -Apply" -ForegroundColor Yellow
    exit 0
}

if ($plan.Count -eq 0) { exit 0 }

Write-Host ""
Write-Host "  Удаляю..." -ForegroundColor Cyan
$removed = 0
$failed = 0
foreach ($row in $plan) {
    try {
        Remove-Item -LiteralPath $row.Abs -Recurse -Force -ErrorAction Stop
        Write-Host ("    удалён  {0}" -f $row.Rel) -ForegroundColor DarkGray
        $removed++
    } catch {
        Write-Host ("    ОТКАЗ   {0} — {1}" -f $row.Rel, $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host ("  Готово: удалено {0}, с ошибкой {1}" -f $removed, $failed) -ForegroundColor Green
if ($failed -gt 0) { exit 1 }
exit 0
