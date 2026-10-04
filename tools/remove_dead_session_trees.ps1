<#
.SYNOPSIS
    Снос деревьев прежнего корня файлов сессий
    (change 2026-10-03-session-files, п. 4.6).

.DESCRIPTION
    Корневой каталог сессии переехал: теперь его объявляет платформа
    (mcp-platform/platform.json -> execution.session_root =
    ${NANOBOT_WORKSPACE}/data_store/sessions). Деревья от старого корня
    остались на диске, и по ним нельзя понять, где теперь лежат файлы
    сессии. Код их больше не читает.

    Скрипт НЕ трогает `workspace/data_store/sessions` (живой корень) и
    каталог `data_store` в корне репозитория (черновики сообщений
    коммитов). Дерево `mvs_*` с настоящими данными прошлой сессии — тоже
    нет: решение о нём принимает владелец, а не уборщик, поэтому оно
    удаляется только с явным -AlsoSessionData.

    Без -Apply скрипт только показывает, что и сколько удалит. С -Apply
    файлы уходят в карантин `_quarantine/<метка времени>/` рядом с
    репозиторием — снос обратим одной командой, указанной в конце вывода.
    -Permanent удаляет сразу, без карантина.

    Путь, который git считает отслеживаемым, скрипт не удаляет: значит
    список устарел и его надо пересмотреть, а не снести молча.

.EXAMPLE
    .\tools\remove_dead_session_trees.ps1
    Показать план. Ничего не удаляет.

.EXAMPLE
    .\tools\remove_dead_session_trees.ps1 -Apply
    Убрать мусор в карантин (обратимо).

.EXAMPLE
    .\tools\remove_dead_session_trees.ps1 -Apply -AlsoSessionData
    То же плюс дерево с данными прошлой сессии.
#>

[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Permanent,
    [switch]$AlsoSessionData
)

$ErrorActionPreference = 'Stop'

# --- Где мы -----------------------------------------------------------------

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $RepoRoot

Write-Host "Репозиторий: $RepoRoot"
Write-Host ''

# --- Список целей -----------------------------------------------------------
# Пути перечислены поимённо, а не маской: маска по слову `cache` однажды
# снесла бы и живой каталог. Добавление нового пути — осознанное решение
# с записью в PENDING-DELETIONS.md, а не результат подстановки.

$DoubleCache = 'workspace/data_store/cache/cache'
$MediaTree   = 'workspace/data_store/media/cache'
$TestOutput  = 'workspace/data_store/cache/sessions/test_1'
$DebugLog    = 'workspace/data_store/cache/debug_stream.log'
$SessionData = 'workspace/data_store/cache/sessions/mvs_e8d15482d6dc48a1a1682a420a77fa0c'

$Targets = @(
    @{ Path = $DoubleCache; Why = 'дерево бага с двойным cache: 90 файлов — вывод tests/test_smoke_postgres_channel_media.py, попадал мимо files/, и хук его потом не находил' }
    @{ Path = $MediaTree;   Why = 'пустая структура каталогов того же бага' }
    @{ Path = $TestOutput;  Why = 'вывод того же теста в прежнем корне сессий: 20 файлов' }
    @{ Path = $DebugLog;    Why = 'лог прежнего отладочного запуска, последняя запись 2026-10-02' }
)

if ($AlsoSessionData) {
    $Targets += @{ Path = $SessionData; Why = 'ДАННЫЕ прошлой сессии агента (отчёт аудита, снимок application_context.py, строки pytest). Удаляются только с явным -AlsoSessionData' }
}

# --- Что защищено -----------------------------------------------------------
#
# Правило ровно такое, и порядок пунктов важен:
#   1. нельзя снести сам защищённый путь;
#   2. нельзя снести его КОНТЕЙНЕР — «workspace/data_store» содержит живой
#      корень, значит его удаление снесло бы и живое;
#   3. можно снести того, кто лежит ВНУТРИ защищённого, — иначе защита
#      «workspace» заблокировала бы весь data_store, и правило 2 стало бы
#      не нужным.
#
# Про исключение: внутри `workspace/data_store/sessions` спускаться нельзя
# (это живой корень), внутри `data_store` — можно: там черновики сообщений
# коммитов, и владелец сам решит, что с ними делать.

$Protected = @(
    'workspace/data_store/sessions'
    'workspace/data_store'
    'workspace'
    'data_store'
    'mcp-platform'
    'lib'
    'tools'
    'docs'
    'config.py'
    'config.json'
)

#: Внутри этих путей снос запрещён на любой глубине (живые данные).
$NoDescend = @('workspace/data_store/sessions')

function Test-IsProtected {
    param([string]$Rel)

    $norm = $Rel.Replace('\', '/').TrimEnd('/')
    foreach ($p in $Protected) {
        $pn = $p.Replace('\', '/').TrimEnd('/')
        # 1. сам защищённый путь
        if ($norm -eq $pn) { return $true }
        # 2. контейнер защищённого: снеся его, снесём и живое
        if ($pn.StartsWith($norm + '/')) { return $true }
    }
    # 3. потомок защищённого — спускаться можно не всюду
    foreach ($d in $NoDescend) {
        if ($norm.StartsWith($d + '/')) { return $true }
    }
    return $false
}

function Test-IsInsideRepo {
    param([string]$Full)
    $root = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\') + '\'
    return $Full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-IsTracked {
    param([string]$Rel)
    # Пустой вывод — путь под git не значится (у нас всё это под .gitignore).
    $out = & git -C $RepoRoot ls-files -- $Rel 2>$null
    return [bool]($out -and $out.Trim())
}

function Format-Size {
    param([int64]$Bytes)
    if ($Bytes -ge 1MB) { return ('{0:N1} МБ' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} КБ' -f ($Bytes / 1KB)) }
    return "$Bytes Б"
}

# --- Проверки до любого действия -------------------------------------------

$Plan = @()
$Skipped = @()

foreach ($t in $Targets) {
    $rel = $t.Path.Replace('\', '/')
    $full = Join-Path $RepoRoot $rel

    if (Test-IsProtected $rel) {
        throw "ОТКАЗ: '$rel' попал под защиту. Список целей повреждён — снос остановлен."
    }
    if (-not (Test-IsInsideRepo $full)) {
        throw "ОТКАЗ: '$rel' указывает вне репозитория. Снос остановлен."
    }
    if (-not (Test-Path -LiteralPath $full)) {
        $Skipped += @{ Path = $rel; Why = 'уже отсутствует' }
        continue
    }
    if (Test-IsTracked $rel) {
        $Skipped += @{ Path = $rel; Why = 'ОТКАЗ: путь отслеживается git — список устарел, разбираться вручную' }
        continue
    }

    $item = Get-Item -LiteralPath $full
    if ($item.PSIsContainer) {
        $files = @(Get-ChildItem -LiteralPath $full -Recurse -File -Force)
        $size = ($files | Measure-Object -Property Length -Sum).Sum
    } else {
        $files = @($item)
        $size = $item.Length
    }
    $Plan += @{
        Path  = $rel
        Full  = $full
        Why   = $t.Why
        Files = $files.Count
        Bytes = [int64]$(if ($size) { $size } else { 0 })
    }
}

# --- Показ ------------------------------------------------------------------

if ($Plan.Count -eq 0) {
    Write-Host 'Удалять нечего: все пути уже отсутствуют или отказаны проверками.'
} else {
    Write-Host 'К СНОСУ:' -ForegroundColor Cyan
    foreach ($p in $Plan) {
        Write-Host ("  {0}" -f $p.Path) -ForegroundColor Yellow
        Write-Host ("      файлов: {0},  размер: {1}" -f $p.Files, (Format-Size $p.Bytes))
        Write-Host ("      почему: {0}" -f $p.Why) -ForegroundColor DarkGray
    }
    $total = ($Plan | ForEach-Object { $_.Bytes } | Measure-Object -Sum).Sum
    Write-Host ("  ИТОГО: {0} путей, {1}" -f $Plan.Count, (Format-Size ([int64]$total)))
    Write-Host ''
}

if ($Skipped.Count -gt 0) {
    Write-Host 'НЕ ТРОГАЕМ:' -ForegroundColor Cyan
    foreach ($s in $Skipped) {
        Write-Host ("  {0}  —  {1}" -f $s.Path, $s.Why) -ForegroundColor DarkGray
    }
    Write-Host ''
}

Write-Host 'ЖИВОЕ ХРАНИЛИЩЕ, НЕ ТРОГАЕМ НИ ПРИ КАКИХ ФЛАГАХ:' -ForegroundColor Cyan
Write-Host '  workspace/data_store/sessions  — корень, объявленный платформой' -ForegroundColor DarkGray
Write-Host '  data_store (корень репозитория) — черновики сообщений коммитов' -ForegroundColor DarkGray
Write-Host ''

if (-not $Apply) {
    Write-Host 'Это был показ. Ничего не удалено.' -ForegroundColor Green
    Write-Host 'Повторить с -Apply, чтобы убрать мусор.'
    return
}

# --- Действие ---------------------------------------------------------------

$quarantine = $null
if (-not $Permanent) {
    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $quarantine = Join-Path $RepoRoot "_quarantine\$stamp"
}

$done = @()
foreach ($p in $Plan) {
    if ($Permanent) {
        Remove-Item -LiteralPath $p.Full -Recurse -Force
    } else {
        $dest = Join-Path $quarantine $p.Path
        $destParent = Split-Path -Parent $dest
        if (-not (Test-Path -LiteralPath $destParent)) {
            New-Item -ItemType Directory -Path $destParent -Force | Out-Null
        }
        Move-Item -LiteralPath $p.Full -Destination $dest -Force
    }
    $done += $p.Path
    Write-Host ("  удалён: {0}" -f $p.Path)
}

# Опустевший родитель media/ убираем, только если он действительно пуст —
# иначе под ним может лежать то, что скрипт не видел.
$mediaRoot = Join-Path $RepoRoot 'workspace/data_store/media'
if ((Test-Path -LiteralPath $mediaRoot) -and -not (Get-ChildItem -LiteralPath $mediaRoot -Force)) {
    Remove-Item -LiteralPath $mediaRoot -Force
    Write-Host '  удалён опустевший каталог: workspace/data_store/media'
}

Write-Host ''
Write-Host ("Удалено путей: {0}" -f $done.Count) -ForegroundColor Green

if ($quarantine) {
    Write-Host ''
    Write-Host 'Файлы не уничтожены, а перенесены в карантин:' -ForegroundColor Cyan
    Write-Host "  $quarantine"
    Write-Host 'Вернуть обратно одной командой (PowerShell):' -ForegroundColor Cyan
    Write-Host ('  $q = "{0}"' -f $quarantine)
    Write-Host ('  $r = "{0}"' -f $RepoRoot)
    Write-Host '  # сначала каталоги (иначе файлам некуда падать), затем файлы'
    Write-Host '  Get-ChildItem -LiteralPath $q -Recurse -Force -Directory | ForEach-Object {'
    Write-Host '      $d = Join-Path $r $_.FullName.Substring($q.Length + 1)'
    Write-Host '      New-Item -ItemType Directory -Path $d -Force | Out-Null'
    Write-Host '  }'
    Write-Host '  Get-ChildItem -LiteralPath $q -Recurse -Force -File | ForEach-Object {'
    Write-Host '      $d = Join-Path $r $_.FullName.Substring($q.Length + 1)'
    Write-Host '      New-Item -ItemType Directory -Path (Split-Path -Parent $d) -Force | Out-Null'
    Write-Host '      Move-Item -LiteralPath $_.FullName -Destination $d -Force'
    Write-Host '  }'
} else {
    Write-Host 'Безвозвратно (-Permanent).' -ForegroundColor Red
}

Write-Host ''
Write-Host 'После сноса удалите карантин вручную: Remove-Item -LiteralPath "_quarantine" -Recurse -Force' -ForegroundColor DarkGray
