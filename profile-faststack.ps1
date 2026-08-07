<#

Guided py-spy profiler for FastStack on Windows.

Harness version: 2026-08-05-v4-cache-trace



Outputs one interactive SVG flame graph plus an LLM-friendly text/TSV report

from the same SVG for every py-spy scenario. It also includes a separate,

matched --debugcache-trace workflow for cold/warm cache and decode analysis.

#>

[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\code\faststack",
    [string]$ImageDirectory = "",
    [string]$OutputRoot = "",
    [ValidateRange(3, 120)]
    [int]$StartupDurationSeconds = 12,
    [ValidateRange(5, 300)]
    [int]$ScenarioDurationSeconds = 20,
    [ValidateRange(1, 1000)]
    [int]$SamplingRate = 50,
    [switch]$SkipStartup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$ProfileNumber = 0
$HarnessVersion = "2026-08-05-v4-cache-trace"

function Write-Utf8File {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}

function Add-Utf8File {
    param([string]$Path, [string]$Text)
    [System.IO.File]::AppendAllText($Path, $Text, $Utf8NoBom)
}


function Invoke-NativeCommandLogged {
    param(
        [string]$FilePath,
        [object[]]$Arguments,
        [string]$LogPath,
        [switch]$Append
    )

    if (-not $Append) {
        Write-Utf8File -Path $LogPath -Text ''
    }

    # Windows PowerShell 5.1 converts native stderr into ErrorRecord objects.

    # With the script-wide ErrorActionPreference=Stop, a normal py-spy error

    # would otherwise abort this harness before it can write a useful log.

    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @Arguments 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            Add-Utf8File -Path $LogPath -Text ($line + [Environment]::NewLine)
        }
        return [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
}



function ConvertTo-WindowsArgumentString {
    param([object[]]$Arguments)

    $quoted = foreach ($value in $Arguments) {
        $argument = [string]$value
        if ($argument -notmatch '[\s"]') {
            $argument
        }
        else {
            # None of the harness-generated arguments end in a directory

            # separator. Escaping embedded quotes is therefore sufficient for

            # the paths and switches passed to py-spy here.

            '"' + ($argument -replace '"', '\"') + '"'
        }
    }
    return ($quoted -join ' ')
}

function Invoke-PySpyRecordWithWatchdog {
    param(
        [string]$FilePath,
        [object[]]$Arguments,
        [string]$LogPath,
        [int]$ExpectedDurationSeconds,
        [int]$GraceSeconds = 15,
        [switch]$Append
    )

    if (-not $Append) {
        Write-Utf8File -Path $LogPath -Text ''
    }

    $argumentString = ConvertTo-WindowsArgumentString -Arguments $Arguments
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $FilePath
    $processInfo.Arguments = $argumentString
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    [void]$process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()

    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $timeoutSeconds = $ExpectedDurationSeconds + $GraceSeconds
    $lastPrintedSecond = -1
    $timedOut = $false

    while (-not $process.HasExited) {
        $elapsedSecond = [int][Math]::Floor($watch.Elapsed.TotalSeconds)
        if ($elapsedSecond -ne $lastPrintedSecond -and ($elapsedSecond % 5 -eq 0)) {
            Write-Host ('Profiling... {0}s elapsed (hard limit {1}s)' -f $elapsedSecond, $timeoutSeconds) -ForegroundColor DarkGray
            $lastPrintedSecond = $elapsedSecond
        }
        if ($watch.Elapsed.TotalSeconds -gt $timeoutSeconds) {
            $timedOut = $true
            Write-Warning ('py-spy exceeded its {0}s hard limit; stopping the profiler. FastStack was sampled with --nonblocking and should remain usable.' -f $timeoutSeconds)
            try {
                $process.Kill()
            }
            catch {
                Write-Warning ('Could not stop py-spy PID {0}: {1}' -f $process.Id, $_.Exception.Message)
            }
            break
        }
        Start-Sleep -Milliseconds 200
        $process.Refresh()
    }

    try {
        $process.WaitForExit()
    }
    catch {
        # The process may already have been reaped after watchdog termination.

    }

    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $combined = ((@($stdout, $stderr) | Where-Object { $_ }) -join [Environment]::NewLine).Trim()
    if ($combined) {
        Write-Host $combined
        Add-Utf8File -Path $LogPath -Text ($combined + [Environment]::NewLine)
    }

    if ($timedOut) {
        Add-Utf8File -Path $LogPath -Text ("WATCHDOG_TIMEOUT: profiler stopped after $timeoutSeconds seconds." + [Environment]::NewLine)
        return 124
    }
    return [int]$process.ExitCode
}

function Get-DescendantProcessRows {
    param(
        [int]$RootProcessId,
        [object[]]$AllProcesses
    )

    $descendants = @()
    $frontier = @($RootProcessId)
    while ($frontier.Count -gt 0) {
        $next = @(
            $AllProcesses |
                Where-Object { $frontier -contains [int]$_.ParentProcessId }
        )
        if ($next.Count -eq 0) {
            break
        }
        $descendants += $next
        $frontier = @($next | ForEach-Object { [int]$_.ProcessId })
    }
    return @($descendants)
}

function Wait-FastStackPythonTarget {
    param(
        [int]$LauncherProcessId,
        [int[]]$ExistingIds,
        [string]$VenvPythonPath,
        [int]$TimeoutMilliseconds = 8000
    )

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    do {
        $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $descendants = @(Get-DescendantProcessRows -RootProcessId $LauncherProcessId -AllProcesses $allProcesses)

        $candidates = @(
            $descendants |
                Where-Object {
                    $ExistingIds -notcontains [int]$_.ProcessId -and
                    $_.Name -match '^(?i:python|pythonw)\.exe$' -and
                    $_.CommandLine -and
                    $_.CommandLine -match '(?i)(?:^|\s)-m\s+faststack(?:\s|$)'
                }
        )

        if ($candidates.Count -gt 0) {
            # A Windows venv python.exe is a redirector. The real CPython

            # interpreter is normally its child whose ExecutablePath points to

            # the base Python installation rather than venv\Scripts\python.exe.

            $venvExecutable = [System.IO.Path]::GetFullPath($VenvPythonPath).TrimEnd('\')
            $realInterpreterCandidates = @(
                $candidates |
                    Where-Object {
                        $_.ExecutablePath -and
                        -not [string]::Equals(
                            [System.IO.Path]::GetFullPath([string]$_.ExecutablePath).TrimEnd('\'),
                            $venvExecutable,
                            [System.StringComparison]::OrdinalIgnoreCase
                        )
                    }
            )

            # Do not select the redirector during the brief interval before its

            # real interpreter child appears. This is the exact process that

            # makes py-spy report "Failed to find python version" on Windows.

            if ($realInterpreterCandidates.Count -eq 0) {
                Start-Sleep -Milliseconds 20
                continue
            }

            $parentIds = @($realInterpreterCandidates | ForEach-Object { [int]$_.ParentProcessId })
            $leafCandidates = @(
                $realInterpreterCandidates |
                    Where-Object { $parentIds -notcontains [int]$_.ProcessId }
            )
            if ($leafCandidates.Count -eq 0) {
                $leafCandidates = $realInterpreterCandidates
            }

            $targetRow = $leafCandidates |
                Sort-Object -Property @(
                    @{ Expression = { [uint64]$_.WorkingSetSize }; Descending = $true },
                    @{ Expression = { [int]$_.ProcessId }; Descending = $true }
                ) |
                Select-Object -First 1

            try {
                $target = Get-Process -Id ([int]$targetRow.ProcessId) -ErrorAction Stop
                return [pscustomobject]@{
                    Process = $target
                    ProcessRow = $targetRow
                    DetectionMilliseconds = [int][Math]::Round($timer.Elapsed.TotalMilliseconds)
                }
            }
            catch {
                # The redirector may have exited between CIM enumeration and

                # Get-Process. Continue polling for the stable interpreter.

            }
        }

        Start-Sleep -Milliseconds 20
    } while ($timer.ElapsedMilliseconds -lt $TimeoutMilliseconds)

    $related = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^(?i:python|pythonw|faststack)\.exe$' -and
                $_.CommandLine -and
                $_.CommandLine -match '(?i)faststack'
            } |
            Select-Object ProcessId, ParentProcessId, Name, CommandLine
    )
    $details = ($related | Format-Table -AutoSize -Wrap | Out-String).Trim()
    throw "Could not find FastStack's real CPython child process within $TimeoutMilliseconds ms.`n$details"
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
    Write-Host $Title -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkCyan
}

function Get-FastStackProcesses {
    @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^(?i:faststack|python|pythonw)\.exe$' -and
                $_.CommandLine -and
                $_.CommandLine -match '(?i)(faststack\.exe|(?:^|\s)-m\s+faststack(?:\s|$))'
            }
    )
}

function Get-DetectedImageDirectory {
    foreach ($process in (Get-FastStackProcesses)) {
        $commandLine = [string]$process.CommandLine
        $tail = $null

        $match = [regex]::Match($commandLine, '(?i)faststack\.exe"?\s+(?<tail>.+)$')
        if ($match.Success) {
            $tail = $match.Groups['tail'].Value
        }
        else {
            $match = [regex]::Match($commandLine, '(?i)(?:^|\s)-m\s+faststack\s+(?<tail>.+)$')
            if ($match.Success) {
                $tail = $match.Groups['tail'].Value
            }
        }

        if (-not $tail) {
            continue
        }

        $flagPosition = $tail.IndexOf(' --')
        if ($flagPosition -ge 0) {
            $tail = $tail.Substring(0, $flagPosition)
        }

        $candidate = $tail.Trim().Trim('"')
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    return $null
}

function Resolve-PySpyPath {
    param([string]$ResolvedRepoRoot)

    $command = Get-Command py-spy -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidate = Join-Path $ResolvedRepoRoot '.venv-win\Scripts\py-spy.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return $candidate
    }

    throw "py-spy.exe was not found in PATH or at $candidate"
}

function Confirm-FastStackClosed {
    $running = @(Get-FastStackProcesses)
    if ($running.Count -eq 0) {
        return
    }

    Write-Host 'Close all currently running FastStack windows before profiling.' -ForegroundColor Yellow
    $running |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine |
        Format-Table -AutoSize -Wrap

    [void](Read-Host 'After closing them, press Enter')
    $running = @(Get-FastStackProcesses)
    if ($running.Count -gt 0) {
        throw ('FastStack is still running. Remaining PID(s): ' + ($running.ProcessId -join ', '))
    }
}

function Get-NextBaseName {
    param([string]$Slug)
    $script:ProfileNumber++
    return ('{0:D2}-{1}' -f $script:ProfileNumber, $Slug)
}

function Convert-SvgToReports {
    param(
        [string]$SvgPath,
        [string]$LlmPath,
        [string]$TsvPath,
        [string]$Title,
        [string]$Mode,
        [int]$Duration,
        [int]$Rate,
        [bool]$Native,
        [bool]$GilOnly,
        [string]$Instructions
    )

    $svg = [System.IO.File]::ReadAllText($SvgPath)
    $titleMatches = [regex]::Matches(
        $svg,
        '<title>(?<title>.*?)</title>',
        [System.Text.RegularExpressions.RegexOptions]::Singleline
    )

    $rows = @()
    foreach ($titleMatch in $titleMatches) {
        $text = [System.Net.WebUtility]::HtmlDecode($titleMatch.Groups['title'].Value).Trim()
        $sampleMatch = [regex]::Match(
            $text,
            '^(?<frame>.*)\s+\((?<samples>[\d,]+)\s+samples?,\s+(?<percent>[\d.]+)%\)$'
        )
        if (-not $sampleMatch.Success) {
            continue
        }

        $rows += [pscustomobject]@{
            Samples = [int64](($sampleMatch.Groups['samples'].Value) -replace ',', '')
            Percent = [double]$sampleMatch.Groups['percent'].Value
            Frame = $sampleMatch.Groups['frame'].Value
        }
    }

    $rows = @($rows | Sort-Object -Property Samples -Descending)

    $tsv = New-Object System.Text.StringBuilder
    [void]$tsv.AppendLine("Samples`tPercent`tFrame")
    foreach ($row in $rows) {
        $safeFrame = ([string]$row.Frame) -replace "`t", ' '
        [void]$tsv.AppendLine(("{0}`t{1:N3}`t{2}" -f $row.Samples, $row.Percent, $safeFrame))
    }
    Write-Utf8File -Path $TsvPath -Text $tsv.ToString()

    $report = New-Object System.Text.StringBuilder
    [void]$report.AppendLine('# FastStack py-spy profile')
    [void]$report.AppendLine('')
    [void]$report.AppendLine(('Title: {0}' -f $Title))
    [void]$report.AppendLine(('Mode: {0}' -f $Mode))
    [void]$report.AppendLine(('DurationSeconds: {0}' -f $Duration))
    [void]$report.AppendLine(('SamplingRateHz: {0}' -f $Rate))
    [void]$report.AppendLine(('NativeStacks: {0}' -f $Native))
    [void]$report.AppendLine(('GilOnly: {0}' -f $GilOnly))
    [void]$report.AppendLine(('Instructions: {0}' -f $Instructions))
    [void]$report.AppendLine(('ParsedFrameBoxes: {0}' -f $rows.Count))
    [void]$report.AppendLine('')
    [void]$report.AppendLine('Sample counts are inclusive for each flame-graph box. The same function may')
    [void]$report.AppendLine('appear more than once under different callers or threads. Use the SVG for')
    [void]$report.AppendLine('exact ancestry and this report/TSV for search and sorting.')
    [void]$report.AppendLine('')
    [void]$report.AppendLine('## Largest frame boxes')
    [void]$report.AppendLine("Samples`tPercent`tFrame")

    foreach ($row in ($rows | Select-Object -First 300)) {
        [void]$report.AppendLine(("{0}`t{1:N3}`t{2}" -f $row.Samples, $row.Percent, $row.Frame))
    }

    [void]$report.AppendLine('')
    [void]$report.AppendLine('## Largest FastStack-related frame boxes')
    [void]$report.AppendLine("Samples`tPercent`tFrame")
    foreach ($row in ($rows | Where-Object { $_.Frame -match '(?i)faststack[\\/\.]' } | Select-Object -First 200)) {
        [void]$report.AppendLine(("{0}`t{1:N3}`t{2}" -f $row.Samples, $row.Percent, $row.Frame))
    }

    Write-Utf8File -Path $LlmPath -Text $report.ToString()
}

function Add-IndexProfile {
    param(
        [string]$IndexPath,
        [string]$Title,
        [string]$BaseName,
        [string]$Mode,
        [int]$Duration,
        [int]$Rate,
        [bool]$Native,
        [bool]$GilOnly
    )

    $entry = @"

## $Title


- Mode: $Mode
- Duration: $Duration seconds
- Sampling rate: $Rate Hz
- Native stacks: $Native
- GIL-only: $GilOnly
- Flame graph: [$BaseName.svg]($BaseName.svg)
- LLM report: [$BaseName.llm.txt]($BaseName.llm.txt)
- Frame table: [$BaseName.frames.tsv]($BaseName.frames.tsv)
- py-spy log: [$BaseName.py-spy.log]($BaseName.py-spy.log)
"@

    Add-Utf8File -Path $IndexPath -Text $entry
}

function Stop-NewFastStackProcesses {
    param([int[]]$ExistingIds)

    Start-Sleep -Milliseconds 250
    $newProcesses = @(
        Get-FastStackProcesses |
            Where-Object { $ExistingIds -notcontains [int]$_.ProcessId }
    )

    foreach ($process in $newProcesses) {
        try {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
        }
        catch {
            Write-Warning ('Could not stop startup-profile PID {0}: {1}' -f $process.ProcessId, $_.Exception.Message)
        }
    }
}

function Invoke-StartupProfile {
    param(
        [string]$Title,
        [string]$Slug,
        [string]$SessionDirectory,
        [string]$IndexPath,
        [string]$PySpyPath,
        [string]$PythonPath,
        [string]$ResolvedRepoRoot,
        [string]$ResolvedImageDirectory
    )

    Write-Section $Title
    Write-Host 'Do not click, navigate, resize, or open a panel during this startup recording.' -ForegroundColor Yellow
    Write-Host 'FastStack will start normally; the harness will attach to the real CPython child as soon as it appears.' -ForegroundColor DarkGray
    [void](Read-Host 'Press Enter to launch FastStack and begin the startup profile')

    $baseName = Get-NextBaseName -Slug $Slug
    $svgPath = Join-Path $SessionDirectory ($baseName + '.svg')
    $llmPath = Join-Path $SessionDirectory ($baseName + '.llm.txt')
    $tsvPath = Join-Path $SessionDirectory ($baseName + '.frames.tsv')
    $logPath = Join-Path $SessionDirectory ($baseName + '.py-spy.log')

    $beforeIds = @((Get-FastStackProcesses | ForEach-Object { [int]$_.ProcessId }))
    $argumentLine = ('-m faststack "{0}" --loupe' -f $ResolvedImageDirectory)
    $launchTimer = [System.Diagnostics.Stopwatch]::StartNew()
    $launcher = $null
    $targetInfo = $null
    $exitCode = -1

    Push-Location $ResolvedRepoRoot
    try {
        $launcher = Start-Process -FilePath $PythonPath -ArgumentList $argumentLine -WorkingDirectory $ResolvedRepoRoot -PassThru
        $targetInfo = Wait-FastStackPythonTarget -LauncherProcessId $launcher.Id -ExistingIds $beforeIds -VenvPythonPath $PythonPath
        $target = $targetInfo.Process
        $attachDelay = [int][Math]::Round($launchTimer.Elapsed.TotalMilliseconds)

        $metadata = @"
Startup launch timestamp: $(Get-Date -Format o)
Venv launcher PID: $($launcher.Id)
Real CPython target PID: $($target.Id)
Target detection delay: $($targetInfo.DetectionMilliseconds) ms
py-spy invocation delay from launch: $attachDelay ms
Note: the very earliest Windows venv redirector handoff is not sampled; normal FastStack imports and UI startup after attachment are sampled.

"@

        Write-Utf8File -Path $logPath -Text $metadata
        Write-Host ('Venv launcher PID: {0}; real CPython PID: {1}; attaching after ~{2} ms' -f $launcher.Id, $target.Id, $attachDelay) -ForegroundColor Green

        $arguments = @(
            'record',
            '--pid', [string]$target.Id,
            '--duration', [string]$StartupDurationSeconds,
            '--rate', [string][Math]::Min($SamplingRate, 50),
            '--threads',
            '--nonblocking',
            '--full-filenames',
            '--output', $svgPath
        )
        $exitCode = Invoke-PySpyRecordWithWatchdog -FilePath $PySpyPath -Arguments $arguments -LogPath $logPath -ExpectedDurationSeconds $StartupDurationSeconds -Append
    }
    finally {
        Pop-Location
        Stop-NewFastStackProcesses -ExistingIds $beforeIds
    }

    if ($exitCode -ne 0) {
        Write-Warning ('py-spy exited with code {0}. Read {1}' -f $exitCode, $logPath)
    }

    if (Test-Path -LiteralPath $svgPath -PathType Leaf) {
        $attachDetail = if ($targetInfo) { 'Attached to the real CPython child approximately {0} ms after launch.' -f $targetInfo.DetectionMilliseconds } else { 'Startup PID attachment.' }
        $reportArguments = @{
            SvgPath = $svgPath
            LlmPath = $llmPath
            TsvPath = $tsvPath
            Title = $Title
            Mode = 'near-startup nonblocking PID attach to real CPython child'
            Duration = $StartupDurationSeconds
            Rate = [Math]::Min($SamplingRate, 50)
            Native = $false
            GilOnly = $false
            Instructions = ('Do not interact during startup. ' + $attachDetail)
        }
        Convert-SvgToReports @reportArguments
        Write-Host ('Created {0}' -f $svgPath) -ForegroundColor Green
    }
    else {
        Write-Warning ('No SVG was created. Read {0}' -f $logPath)
    }

    $indexArguments = @{
        IndexPath = $IndexPath
        Title = $Title
        BaseName = $baseName
        Mode = 'near-startup nonblocking PID attach to real CPython child'
        Duration = $StartupDurationSeconds
        Rate = [Math]::Min($SamplingRate, 50)
        Native = $false
        GilOnly = $false
    }
    Add-IndexProfile @indexArguments
}

function Start-FastStackDirect {
    param(
        [string]$PythonPath,
        [string]$ResolvedRepoRoot,
        [string]$ResolvedImageDirectory
    )

    Write-Section 'Launching FastStack for interactive profiles'
    $argumentLine = ('-m faststack "{0}" --loupe' -f $ResolvedImageDirectory)
    $beforeIds = @((Get-FastStackProcesses | ForEach-Object { [int]$_.ProcessId }))
    $launcher = Start-Process -FilePath $PythonPath -ArgumentList $argumentLine -WorkingDirectory $ResolvedRepoRoot -PassThru
    $targetInfo = Wait-FastStackPythonTarget -LauncherProcessId $launcher.Id -ExistingIds $beforeIds -VenvPythonPath $PythonPath
    $target = $targetInfo.Process

    Write-Host ('Venv launcher PID: {0}' -f $launcher.Id) -ForegroundColor DarkGray
    Write-Host ('Real FastStack CPython PID: {0}' -f $target.Id) -ForegroundColor Green
    Write-Host ('Detected the real interpreter after ~{0} ms.' -f $targetInfo.DetectionMilliseconds) -ForegroundColor DarkGray
    Write-Host 'The profiling scenarios will attach to this leaf Python process, not the Windows venv redirector.'
    [void](Read-Host 'Wait until FastStack is fully ready, then press Enter')
    return $target
}

function Invoke-AttachedProfile {
    param(
        [System.Diagnostics.Process]$FastStackProcess,
        [pscustomobject]$Scenario,
        [string]$SessionDirectory,
        [string]$IndexPath,
        [string]$PySpyPath
    )

    $FastStackProcess.Refresh()
    if ($FastStackProcess.HasExited) {
        throw 'The FastStack process launched by this script has exited.'
    }

    Write-Section ('Scenario {0}: {1}' -f $Scenario.Key, $Scenario.Title)
    foreach ($instruction in $Scenario.Instructions) {
        Write-Host ('- {0}' -f $instruction)
    }
    Write-Host ('Duration={0}s Rate={1}Hz Native={2} GIL-only={3}' -f $Scenario.Duration, $Scenario.Rate, $Scenario.Native, $Scenario.GilOnly) -ForegroundColor DarkGray
    [void](Read-Host 'Prepare FastStack as described, then press Enter immediately before doing it')

    $baseName = Get-NextBaseName -Slug $Scenario.Slug
    $svgPath = Join-Path $SessionDirectory ($baseName + '.svg')
    $llmPath = Join-Path $SessionDirectory ($baseName + '.llm.txt')
    $tsvPath = Join-Path $SessionDirectory ($baseName + '.frames.tsv')
    $logPath = Join-Path $SessionDirectory ($baseName + '.py-spy.log')

    $arguments = @(
        'record',
        '--pid', [string]$FastStackProcess.Id,
        '--duration', [string]$Scenario.Duration,
        '--rate', [string]$Scenario.Rate,
        '--threads',
        '--nonblocking',
        '--full-filenames',
        '--output', $svgPath
    )
    if ($Scenario.Native) {
        throw 'Native profiling is disabled in the safe harness because it repeatedly pauses FastStack on Windows. Use a separate low-rate experiment only when explicitly needed.'
    }
    if ($Scenario.GilOnly) {
        $arguments += '--gil'
    }

    Write-Host 'Recording now...' -ForegroundColor Yellow
    $exitCode = Invoke-PySpyRecordWithWatchdog -FilePath $PySpyPath -Arguments $arguments -LogPath $logPath -ExpectedDurationSeconds $Scenario.Duration
    if ($exitCode -ne 0) {
        Write-Warning ('py-spy exited with code {0}. Read {1}' -f $exitCode, $logPath)
    }

    if (Test-Path -LiteralPath $svgPath -PathType Leaf) {
        $reportArguments = @{
            SvgPath = $svgPath
            LlmPath = $llmPath
            TsvPath = $tsvPath
            Title = $Scenario.Title
            Mode = 'nonblocking attach to real CPython child process'
            Duration = $Scenario.Duration
            Rate = $Scenario.Rate
            Native = [bool]$Scenario.Native
            GilOnly = [bool]$Scenario.GilOnly
            Instructions = ($Scenario.Instructions -join ' | ')
        }
        Convert-SvgToReports @reportArguments
        Write-Host ('Created {0}' -f $svgPath) -ForegroundColor Green
        Write-Host ('Created {0}' -f $llmPath) -ForegroundColor Green
    }
    else {
        Write-Warning ('No SVG was created. Read {0}' -f $logPath)
    }

    $indexArguments = @{
        IndexPath = $IndexPath
        Title = $Scenario.Title
        BaseName = $baseName
        Mode = 'nonblocking PID attach to real CPython child'
        Duration = $Scenario.Duration
        Rate = $Scenario.Rate
        Native = [bool]$Scenario.Native
        GilOnly = [bool]$Scenario.GilOnly
    }
    Add-IndexProfile @indexArguments
}

function Invoke-HangDump {
    param(
        [System.Diagnostics.Process]$FastStackProcess,
        [string]$SessionDirectory,
        [string]$IndexPath,
        [string]$PySpyPath
    )

    $FastStackProcess.Refresh()
    if ($FastStackProcess.HasExited) {
        throw 'FastStack has exited.'
    }

    Write-Section 'Hang or freeze dump'
    Write-Host 'Leave FastStack frozen, return here, and capture all Python thread stacks.'
    [void](Read-Host 'Press Enter to capture the dump')

    $baseName = Get-NextBaseName -Slug 'hang-dump'
    $dumpPath = Join-Path $SessionDirectory ($baseName + '.txt')
    $dumpArguments = @('dump', '--pid', [string]$FastStackProcess.Id, '--nonblocking', '--full-filenames')
    $exitCode = Invoke-NativeCommandLogged -FilePath $PySpyPath -Arguments $dumpArguments -LogPath $dumpPath
    if ($exitCode -ne 0) {
        Write-Warning ('py-spy dump exited with code {0}. Read {1}' -f $exitCode, $dumpPath)
    }

    $entry = @"

## Hang or freeze dump


- Target PID: $($FastStackProcess.Id)
- Thread dump: [$baseName.txt]($baseName.txt)
"@

    Add-Utf8File -Path $IndexPath -Text $entry
}


function Get-SharedFileLength {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [int64]0
    }

    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        return [int64]$stream.Length
    }
    finally {
        if ($stream) {
            $stream.Dispose()
        }
    }
}

function Read-SharedFileRange {
    param(
        [string]$Path,
        [int64]$StartOffset,
        [int64]$EndOffset
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ''
    }

    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $safeStart = [Math]::Max([int64]0, [Math]::Min($StartOffset, $stream.Length))
        $safeEnd = [Math]::Max($safeStart, [Math]::Min($EndOffset, $stream.Length))
        $count = $safeEnd - $safeStart
        if ($count -le 0) {
            return ''
        }
        if ($count -gt [int]::MaxValue) {
            throw ('Trace segment is unexpectedly large: {0} bytes' -f $count)
        }

        [void]$stream.Seek($safeStart, [System.IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] ([int]$count)
        $readTotal = 0
        while ($readTotal -lt $buffer.Length) {
            $read = $stream.Read($buffer, $readTotal, $buffer.Length - $readTotal)
            if ($read -le 0) {
                break
            }
            $readTotal += $read
        }
        if ($readTotal -eq 0) {
            return ''
        }
        if ($readTotal -lt $buffer.Length) {
            $trimmed = New-Object byte[] $readTotal
            [Array]::Copy($buffer, $trimmed, $readTotal)
            $buffer = $trimmed
        }
        return [System.Text.Encoding]::UTF8.GetString($buffer)
    }
    finally {
        if ($stream) {
            $stream.Dispose()
        }
    }
}

function Get-TraceBoundary {
    param(
        [string]$StdoutPath,
        [string]$StderrPath
    )

    return [pscustomobject]@{
        Timestamp = Get-Date
        StdoutOffset = Get-SharedFileLength -Path $StdoutPath
        StderrOffset = Get-SharedFileLength -Path $StderrPath
    }
}

function Write-TraceSegment {
    param(
        [pscustomobject]$StartBoundary,
        [pscustomobject]$EndBoundary,
        [string]$StdoutPath,
        [string]$StderrPath,
        [string]$OutputPath,
        [string]$Label
    )

    $stdoutText = Read-SharedFileRange -Path $StdoutPath -StartOffset $StartBoundary.StdoutOffset -EndOffset $EndBoundary.StdoutOffset
    $stderrText = Read-SharedFileRange -Path $StderrPath -StartOffset $StartBoundary.StderrOffset -EndOffset $EndBoundary.StderrOffset
    $text = @"
# FastStack --debugcache-trace segment: $Label

# Started: $($StartBoundary.Timestamp.ToString('o'))

# Ended: $($EndBoundary.Timestamp.ToString('o'))


===== STDOUT (unbuffered print/[DBGCACHE] records) =====
$stdoutText

===== STDERR (logging records, including [NAVTRACE]) =====
$stderrText
"@

    Write-Utf8File -Path $OutputPath -Text $text
}

function Get-RegexCount {
    param(
        [string]$Text,
        [string]$Pattern
    )
    return [regex]::Matches(
        $Text,
        $Pattern,
        [System.Text.RegularExpressions.RegexOptions]::Multiline
    ).Count
}

function Get-TraceWorkerRows {
    param([string]$Text)

    $rows = @()
    $matches = [regex]::Matches(
        $Text,
        '(?m)^.*\[NAVTRACE\]\s+worker\s+(?<fields>.*)$'
    )
    foreach ($match in $matches) {
        $fields = $match.Groups['fields'].Value

        $taskMatch = [regex]::Match($fields, '(?:^|\s)task=(?<value>\d+)')
        $seqMatch = [regex]::Match($fields, '(?:^|\s)seq=(?<value>\S+)')
        $roleMatch = [regex]::Match($fields, '(?:^|\s)role=(?<value>\S+)')
        $qualityMatch = [regex]::Match($fields, '(?:^|\s)quality=(?<value>\S+)')
        $generationMatch = [regex]::Match($fields, '(?:^|\s)display_gen=(?<value>-?\d+)')
        $pathMatch = [regex]::Match($fields, '(?:^|\s)path=(?:''(?<single>[^'']*)''|"(?<double>[^"]*)"|(?<bare>\S+))')
        $totalMatch = [regex]::Match($fields, '(?:^|\s)total=(?<value>[\d.]+)ms')
        $decodeMatch = [regex]::Match($fields, '(?:^|\s)decode=(?<value>[\d.]+)ms')
        $queueMatch = [regex]::Match($fields, '(?:^|\s)queue=(?<value>[\d.]+)ms')

        $path = ''
        if ($pathMatch.Success) {
            foreach ($name in @('single', 'double', 'bare')) {
                if ($pathMatch.Groups[$name].Success) {
                    $path = $pathMatch.Groups[$name].Value
                    break
                }
            }
        }

        $rows += [pscustomobject]@{
            Task = if ($taskMatch.Success) { [int]$taskMatch.Groups['value'].Value } else { $null }
            Seq = if ($seqMatch.Success) { $seqMatch.Groups['value'].Value } else { '' }
            Role = if ($roleMatch.Success) { $roleMatch.Groups['value'].Value } else { '' }
            Quality = if ($qualityMatch.Success) { $qualityMatch.Groups['value'].Value } else { '' }
            DisplayGeneration = if ($generationMatch.Success) { [int]$generationMatch.Groups['value'].Value } else { $null }
            Path = $path
            QueueMs = if ($queueMatch.Success) { [double]$queueMatch.Groups['value'].Value } else { $null }
            DecodeMs = if ($decodeMatch.Success) { [double]$decodeMatch.Groups['value'].Value } else { $null }
            TotalMs = if ($totalMatch.Success) { [double]$totalMatch.Groups['value'].Value } else { $null }
            Raw = $match.Value.Trim()
        }
    }
    return @($rows)
}

function Get-ObservedTraceTaskIds {
    param([string]$Text)

    $ids = @()
    foreach ($match in [regex]::Matches($Text, '(?m)\[NAVTRACE\].*?\btask=(?<task>\d+)')) {
        $ids += [int]$match.Groups['task'].Value
    }
    return @($ids | Sort-Object -Unique)
}

function Get-TraceStats {
    param(
        [string]$Label,
        [string]$Text
    )

    $workers = @(Get-TraceWorkerRows -Text $Text)
    $taskIds = @(Get-ObservedTraceTaskIds -Text $Text)
    $comboRows = @(
        $workers |
            Where-Object { $_.Path -and $_.Quality -and $null -ne $_.DisplayGeneration } |
            ForEach-Object {
                [pscustomobject]@{
                    Combination = '{0}|gen={1}|quality={2}' -f $_.Path, $_.DisplayGeneration, $_.Quality
                }
            }
    )
    $combos = @($comboRows | Group-Object -Property Combination | Sort-Object Count -Descending)
    $repeated = @($combos | Where-Object { $_.Count -gt 1 })

    $rejectReasons = @()
    foreach ($match in [regex]::Matches($Text, '(?m)\[NAVTRACE\]\s+cache_reject\b.*?\breason=(?<reason>.*?)\s+key=')) {
        $rejectReasons += $match.Groups['reason'].Value.Trim()
    }

    return [pscustomobject]@{
        Label = $Label
        Lines = @($Text -split '\r?\n').Count
        DbgCacheLines = Get-RegexCount -Text $Text -Pattern '\[DBGCACHE\]'
        NavTraceLines = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]'
        CacheHits = Get-RegexCount -Text $Text -Pattern 'get_decoded_image:\s+CACHE HIT\b'
        CacheMisses = Get-RegexCount -Text $Text -Pattern 'get_decoded_image:\s+CACHE MISS\b'
        QualitySubmits = Get-RegexCount -Text $Text -Pattern 'quality_decode:\s+SUBMIT\b'
        QualityRefreshes = Get-RegexCount -Text $Text -Pattern 'quality_decode:\s+REFRESH\b'
        WorkerCompletions = $workers.Count
        ObservedTaskIds = $taskIds.Count
        TaskCanceled = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+task_canceled\b'
        TaskAdopted = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+task_adopted\b'
        TaskMigrated = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+task_migrated\b'
        CacheRejects = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+cache_reject\b'
        BurstSummaries = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+burst\b'
        Placeholders = Get-RegexCount -Text $Text -Pattern 'frame=placeholder\b'
        NotPresented = Get-RegexCount -Text $Text -Pattern '(?:frame=not_presented\b|\bNOT_PRESENTED\b)'
        WrongImageBlocked = Get-RegexCount -Text $Text -Pattern '\bWRONG_IMAGE_BLOCKED\b'
        UnderflowEvents = Get-RegexCount -Text $Text -Pattern '\[NAVTRACE\]\s+underflow\b'
        RepeatedDecodeCombos = $repeated.Count
        WorkerRows = $workers
        RepeatedCombos = $repeated
        RejectReasons = @($rejectReasons | Group-Object | Sort-Object Count -Descending)
    }
}

function Write-TraceReports {
    param(
        [string]$ColdPath,
        [string]$WarmPath,
        [string]$SummaryPath,
        [string]$EventsTsvPath
    )

    $coldText = [System.IO.File]::ReadAllText($ColdPath)
    $warmText = [System.IO.File]::ReadAllText($WarmPath)
    $cold = Get-TraceStats -Label 'Cold' -Text $coldText
    $warm = Get-TraceStats -Label 'Warm' -Text $warmText

    $metrics = @(
        'Lines',
        'DbgCacheLines',
        'NavTraceLines',
        'CacheHits',
        'CacheMisses',
        'QualitySubmits',
        'QualityRefreshes',
        'WorkerCompletions',
        'ObservedTaskIds',
        'TaskCanceled',
        'TaskAdopted',
        'TaskMigrated',
        'CacheRejects',
        'BurstSummaries',
        'Placeholders',
        'NotPresented',
        'WrongImageBlocked',
        'UnderflowEvents',
        'RepeatedDecodeCombos'
    )

    $report = New-Object System.Text.StringBuilder
    [void]$report.AppendLine('# FastStack matched cache/navigation trace')
    [void]$report.AppendLine('')
    [void]$report.AppendLine('FastStack was launched separately with `-u -m faststack --loupe --debugcache-trace`.')
    [void]$report.AppendLine('No py-spy profiler was attached during these runs. Trace logging itself adds overhead,')
    [void]$report.AppendLine('so use event counts and relationships as primary evidence; exact latency is secondary.')
    [void]$report.AppendLine('')
    [void]$report.AppendLine('`ObservedTaskIds` counts unique numeric task IDs appearing anywhere in NAVTRACE lines.')
    [void]$report.AppendLine('It is a strong task-identity signal, but it is not claimed to be a perfect count of every')
    [void]$report.AppendLine('submission because a process can end before all queued tasks produce a terminal trace.')
    [void]$report.AppendLine('')
    [void]$report.AppendLine('## Cold versus warm')
    [void]$report.AppendLine("Metric`tCold`tWarm")
    foreach ($metric in $metrics) {
        [void]$report.AppendLine(('{0}`t{1}`t{2}' -f $metric, $cold.$metric, $warm.$metric))
    }

    foreach ($stats in @($cold, $warm)) {
        [void]$report.AppendLine('')
        [void]$report.AppendLine(('## {0}: repeated path/generation/quality worker combinations' -f $stats.Label))
        if ($stats.RepeatedCombos.Count -eq 0) {
            [void]$report.AppendLine('None found among completed worker records.')
        }
        else {
            [void]$report.AppendLine("Count`tCombination")
            foreach ($group in ($stats.RepeatedCombos | Select-Object -First 100)) {
                [void]$report.AppendLine(('{0}`t{1}' -f $group.Count, $group.Name))
            }
        }

        [void]$report.AppendLine('')
        [void]$report.AppendLine(('## {0}: cache rejection reasons' -f $stats.Label))
        if ($stats.RejectReasons.Count -eq 0) {
            [void]$report.AppendLine('None.')
        }
        else {
            [void]$report.AppendLine("Count`tReason")
            foreach ($group in $stats.RejectReasons) {
                [void]$report.AppendLine(('{0}`t{1}' -f $group.Count, $group.Name))
            }
        }

        [void]$report.AppendLine('')
        [void]$report.AppendLine(('## {0}: burst summary lines' -f $stats.Label))
        $segmentText = if ($stats.Label -eq 'Cold') { $coldText } else { $warmText }
        $burstLines = @(
            $segmentText -split '\r?\n' |
                Where-Object { $_ -match '\[NAVTRACE\]\s+burst\b' }
        )
        if ($burstLines.Count -eq 0) {
            [void]$report.AppendLine('None found.')
        }
        else {
            foreach ($line in $burstLines) {
                [void]$report.AppendLine($line.Trim())
            }
        }
    }

    Write-Utf8File -Path $SummaryPath -Text $report.ToString()

    $tsv = New-Object System.Text.StringBuilder
    [void]$tsv.AppendLine("Run`tTask`tSeq`tRole`tQuality`tDisplayGeneration`tPath`tQueueMs`tDecodeMs`tTotalMs`tRaw")
    foreach ($stats in @($cold, $warm)) {
        $label = $stats.Label
        foreach ($row in $stats.WorkerRows) {
            $safePath = ([string]$row.Path) -replace "`t", ' '
            $safeRaw = ([string]$row.Raw) -replace "`t", ' '
            [void]$tsv.AppendLine(('{0}`t{1}`t{2}`t{3}`t{4}`t{5}`t{6}`t{7}`t{8}`t{9}`t{10}' -f
                $label,
                $row.Task,
                $row.Seq,
                $row.Role,
                $row.Quality,
                $row.DisplayGeneration,
                $safePath,
                $row.QueueMs,
                $row.DecodeMs,
                $row.TotalMs,
                $safeRaw
            ))
        }
    }
    Write-Utf8File -Path $EventsTsvPath -Text $tsv.ToString()
}

function Start-FastStackTraceProcess {
    param(
        [string]$PythonPath,
        [string]$ResolvedRepoRoot,
        [string]$ResolvedImageDirectory,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    Write-Utf8File -Path $StdoutPath -Text ''
    Write-Utf8File -Path $StderrPath -Text ''
    $beforeIds = @((Get-FastStackProcesses | ForEach-Object { [int]$_.ProcessId }))
    $argumentLine = ('-u -m faststack "{0}" --loupe --debugcache-trace' -f $ResolvedImageDirectory)
    $launcher = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList $argumentLine `
        -WorkingDirectory $ResolvedRepoRoot `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -PassThru
    $targetInfo = Wait-FastStackPythonTarget -LauncherProcessId $launcher.Id -ExistingIds $beforeIds -VenvPythonPath $PythonPath
    return [pscustomobject]@{
        Launcher = $launcher
        Process = $targetInfo.Process
        DetectionMilliseconds = $targetInfo.DetectionMilliseconds
        ExistingIds = $beforeIds
    }
}

function Wait-ForFastStackClose {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$Prompt
    )

    $Process.Refresh()
    if ($Process.HasExited) {
        return
    }

    [void](Read-Host $Prompt)
    for ($i = 0; $i -lt 50; $i++) {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }
        Start-Sleep -Milliseconds 100
    }

    try {
        [void]$Process.CloseMainWindow()
    }
    catch {
        # Continue to the explicit failure below.

    }
    for ($i = 0; $i -lt 50; $i++) {
        $Process.Refresh()
        if ($Process.HasExited) {
            return
        }
        Start-Sleep -Milliseconds 100
    }

    throw ('FastStack PID {0} is still running. Close it normally, then rerun the cache trace.' -f $Process.Id)
}

function Invoke-CacheTraceWorkflow {
    param(
        [System.Diagnostics.Process]$FastStackProcess,
        [string]$PythonPath,
        [string]$ResolvedRepoRoot,
        [string]$ResolvedImageDirectory,
        [string]$SessionDirectory,
        [string]$IndexPath
    )

    Write-Section 'Scenario 9: Matched cache/navigation trace'
    Write-Host 'This is intentionally separate from py-spy.' -ForegroundColor Yellow
    Write-Host 'The normal profiling instance must close, then FastStack is relaunched with --debugcache-trace.'
    Write-Host 'Use the same navigation range and pattern for the cold and warm segments.'
    Write-Host 'The script records byte boundaries in unbuffered stdout/stderr, so the two logs are matched cleanly.' -ForegroundColor DarkGray

    Wait-ForFastStackClose -Process $FastStackProcess -Prompt 'Close the current FastStack window normally, then press Enter'

    $baseName = Get-NextBaseName -Slug 'cache-trace'
    $stdoutPath = Join-Path $SessionDirectory ($baseName + '.stdout.log')
    $stderrPath = Join-Path $SessionDirectory ($baseName + '.stderr.log')
    $fullPath = Join-Path $SessionDirectory ($baseName + '-full.log')
    $coldPath = Join-Path $SessionDirectory ($baseName + '-cold.log')
    $warmPath = Join-Path $SessionDirectory ($baseName + '-warm.log')
    $summaryPath = Join-Path $SessionDirectory ($baseName + '-summary.txt')
    $eventsPath = Join-Path $SessionDirectory ($baseName + '-workers.tsv')

    $trace = Start-FastStackTraceProcess `
        -PythonPath $PythonPath `
        -ResolvedRepoRoot $ResolvedRepoRoot `
        -ResolvedImageDirectory $ResolvedImageDirectory `
        -StdoutPath $stdoutPath `
        -StderrPath $stderrPath
    $traceProcess = $trace.Process

    Write-Host ('Trace FastStack CPython PID: {0}' -f $traceProcess.Id) -ForegroundColor Green
    Write-Host ('Detected after ~{0} ms; output is unbuffered.' -f $trace.DetectionMilliseconds) -ForegroundColor DarkGray
    [void](Read-Host 'Wait until FastStack is fully ready in loupe view, then press Enter')
    Start-Sleep -Milliseconds 1000

    Write-Section 'Cache trace: cold navigation segment'
    Write-Host '- Start near images this trace process has not visited.'
    Write-Host '- Hold Right Arrow about 10 seconds, release briefly, then hold Left Arrow back.'
    Write-Host '- End on the original image and wait for settled cover quality.'
    [void](Read-Host 'Prepare the starting image, then press Enter to mark the cold segment start')
    Start-Sleep -Milliseconds 300
    $coldStart = Get-TraceBoundary -StdoutPath $stdoutPath -StderrPath $stderrPath
    Write-Host 'Perform the cold navigation now. Return here only after the final image has settled.' -ForegroundColor Yellow
    [void](Read-Host 'Press Enter to mark the cold segment end')
    Start-Sleep -Milliseconds 1200
    $coldEnd = Get-TraceBoundary -StdoutPath $stdoutPath -StderrPath $stderrPath
    Write-TraceSegment -StartBoundary $coldStart -EndBoundary $coldEnd -StdoutPath $stdoutPath -StderrPath $stderrPath -OutputPath $coldPath -Label 'cold navigation'
    Write-Host ('Created {0}' -f $coldPath) -ForegroundColor Green

    Write-Section 'Cache trace: warm navigation segment'
    Write-Host '- Repeat exactly the same image range and arrow-key pattern.'
    Write-Host '- End on the same original image and wait for settled cover quality.'
    [void](Read-Host 'Press Enter to mark the warm segment start')
    Start-Sleep -Milliseconds 300
    $warmStart = Get-TraceBoundary -StdoutPath $stdoutPath -StderrPath $stderrPath
    Write-Host 'Perform the warm navigation now. Return here only after the final image has settled.' -ForegroundColor Yellow
    [void](Read-Host 'Press Enter to mark the warm segment end')
    Start-Sleep -Milliseconds 1200
    $warmEnd = Get-TraceBoundary -StdoutPath $stdoutPath -StderrPath $stderrPath
    Write-TraceSegment -StartBoundary $warmStart -EndBoundary $warmEnd -StdoutPath $stdoutPath -StderrPath $stderrPath -OutputPath $warmPath -Label 'warm navigation'
    Write-Host ('Created {0}' -f $warmPath) -ForegroundColor Green

    Wait-ForFastStackClose -Process $traceProcess -Prompt 'Close the trace FastStack window normally, then press Enter'
    Start-Sleep -Milliseconds 500

    $stdoutText = if (Test-Path -LiteralPath $stdoutPath) { [System.IO.File]::ReadAllText($stdoutPath) } else { '' }
    $stderrText = if (Test-Path -LiteralPath $stderrPath) { [System.IO.File]::ReadAllText($stderrPath) } else { '' }
    $fullText = @"
# FastStack complete --debugcache-trace capture

# Command: $PythonPath -u -m faststack "$ResolvedImageDirectory" --loupe --debugcache-trace


===== STDOUT =====
$stdoutText

===== STDERR =====
$stderrText
"@

    Write-Utf8File -Path $fullPath -Text $fullText
    Write-TraceReports -ColdPath $coldPath -WarmPath $warmPath -SummaryPath $summaryPath -EventsTsvPath $eventsPath

    $entry = @"

## Matched cache/navigation trace


- Mode: separate unbuffered FastStack process with `--debugcache-trace`; no py-spy attached
- Full combined log: [$baseName-full.log]($baseName-full.log)
- Cold segment: [$baseName-cold.log]($baseName-cold.log)
- Warm segment: [$baseName-warm.log]($baseName-warm.log)
- LLM summary: [$baseName-summary.txt]($baseName-summary.txt)
- Parsed worker table: [$baseName-workers.tsv]($baseName-workers.tsv)
- Raw stdout: [$baseName.stdout.log]($baseName.stdout.log)
- Raw stderr: [$baseName.stderr.log]($baseName.stderr.log)
"@

    Add-Utf8File -Path $IndexPath -Text $entry

    Write-Host ('Created {0}' -f $summaryPath) -ForegroundColor Green
    Write-Host ('Created {0}' -f $eventsPath) -ForegroundColor Green
    Write-Host 'Relaunching ordinary FastStack for any further py-spy scenarios.' -ForegroundColor DarkGray
    return Start-FastStackDirect -PythonPath $PythonPath -ResolvedRepoRoot $ResolvedRepoRoot -ResolvedImageDirectory $ResolvedImageDirectory
}

# Resolve the repository and use the current FastStack command as the default photo folder.

$ResolvedRepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$PythonPath = Join-Path $ResolvedRepoRoot '.venv-win\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "FastStack virtualenv Python was not found: $PythonPath"
}

$detectedDirectory = Get-DetectedImageDirectory
if (-not $ImageDirectory) {
    if ($detectedDirectory) {
        $entered = Read-Host ('Photo directory [{0}]' -f $detectedDirectory)
        if ($entered.Trim()) {
            $ImageDirectory = $entered.Trim()
        }
        else {
            $ImageDirectory = $detectedDirectory
        }
    }
    else {
        $ImageDirectory = (Read-Host 'Photo directory to open in FastStack').Trim()
    }
}
if (-not (Test-Path -LiteralPath $ImageDirectory -PathType Container)) {
    throw "Image directory does not exist: $ImageDirectory"
}
$ResolvedImageDirectory = (Resolve-Path -LiteralPath $ImageDirectory).Path
$PySpyPath = Resolve-PySpyPath -ResolvedRepoRoot $ResolvedRepoRoot

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $ResolvedRepoRoot 'py-spy-profiles'
}
[void](New-Item -ItemType Directory -Path $OutputRoot -Force)
$SessionDirectory = Join-Path $OutputRoot ('faststack-profile-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
[void](New-Item -ItemType Directory -Path $SessionDirectory -Force)
$IndexPath = Join-Path $SessionDirectory 'INDEX.md'

$indexHeader = @"
# FastStack py-spy profiling session


- Started: $(Get-Date -Format o)
- Repository: $ResolvedRepoRoot
- Python: $PythonPath
- py-spy: $PySpyPath
- Image directory: $ResolvedImageDirectory

Each `.llm.txt` and `.frames.tsv` is extracted from the matching SVG, so the
text and visual represent exactly the same sample set. Interactive and startup recordings use py-spy's --nonblocking mode and a hard watchdog to protect FastStack responsiveness. The optional cache trace is collected in a separate FastStack process without py-spy.

"@

Write-Utf8File -Path $IndexPath -Text $indexHeader

$environmentPath = Join-Path $SessionDirectory 'environment.txt'
$pySpyVersion = (& $PySpyPath --version 2>&1 | Out-String).Trim()
$pythonVersion = (& $PythonPath --version 2>&1 | Out-String).Trim()
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$environment = @"
Captured: $(Get-Date -Format o)
Computer: $env:COMPUTERNAME
PowerShell: $($PSVersionTable.PSVersion)
OS: $($os.Caption) $($os.Version) build $($os.BuildNumber)
CPU: $($cpu.Name)
Cores: $($cpu.NumberOfCores)
Logical processors: $($cpu.NumberOfLogicalProcessors)
py-spy: $pySpyVersion
py-spy path: $PySpyPath
Python: $pythonVersion
Python path: $PythonPath
Repository: $ResolvedRepoRoot
Image directory: $ResolvedImageDirectory
"@

Write-Utf8File -Path $environmentPath -Text $environment

Write-Section 'FastStack py-spy profiling harness'
Write-Host ('Harness version: {0}' -f $HarnessVersion) -ForegroundColor Magenta
Write-Host ('Output directory: {0}' -f $SessionDirectory) -ForegroundColor Green
Write-Host ('Photo directory: {0}' -f $ResolvedImageDirectory)
Write-Host ('py-spy: {0}' -f $pySpyVersion)
Write-Host 'Safe mode: <=50 Hz, --nonblocking, native unwinding disabled, hard timeout enabled.' -ForegroundColor Yellow

Confirm-FastStackClosed

if (-not $SkipStartup) {
    $startupChoice = (Read-Host 'Run two startup profiles first (first launch and immediate repeat)? [Y/n]').Trim()
    if (-not $startupChoice -or $startupChoice -match '^(?i:y|yes)$') {
        Invoke-StartupProfile -Title 'Startup profile 1: first launch' -Slug 'startup-first' -SessionDirectory $SessionDirectory -IndexPath $IndexPath -PySpyPath $PySpyPath -PythonPath $PythonPath -ResolvedRepoRoot $ResolvedRepoRoot -ResolvedImageDirectory $ResolvedImageDirectory
        Invoke-StartupProfile -Title 'Startup profile 2: immediate repeat' -Slug 'startup-repeat' -SessionDirectory $SessionDirectory -IndexPath $IndexPath -PySpyPath $PySpyPath -PythonPath $PythonPath -ResolvedRepoRoot $ResolvedRepoRoot -ResolvedImageDirectory $ResolvedImageDirectory
    }
}

Confirm-FastStackClosed
$FastStackProcess = Start-FastStackDirect -PythonPath $PythonPath -ResolvedRepoRoot $ResolvedRepoRoot -ResolvedImageDirectory $ResolvedImageDirectory

$scenarios = @(
    [pscustomobject]@{
        Key = '1'; Slug = 'idle'; Title = 'Idle baseline'; Duration = 15; Rate = 25; Native = $false; GilOnly = $false
        Instructions = @('Use loupe view with one image fully settled.', 'Do nothing during the recording.', 'This exposes unexpected timers or background Python work.')
    },
    [pscustomobject]@{
        Key = '2'; Slug = 'navigation-cold'; Title = 'Cold application-cache navigation'; Duration = $ScenarioDurationSeconds; Rate = [Math]::Min($SamplingRate, 50); Native = $false; GilOnly = $false
        Instructions = @('Start near images this new FastStack process has not visited.', 'Hold Right Arrow about 10 seconds, release briefly, then hold Left Arrow back.', 'Release at the end and let the final image settle.')
    },
    [pscustomobject]@{
        Key = '3'; Slug = 'navigation-warm'; Title = 'Warm navigation'; Duration = $ScenarioDurationSeconds; Rate = [Math]::Min($SamplingRate, 50); Native = $false; GilOnly = $false
        Instructions = @('Repeat the same image range and arrow-key pattern as scenario 2.', 'This emphasizes cache reuse and UI/provider overhead.')
    },
    [pscustomobject]@{
        Key = '4'; Slug = 'editor-tonal'; Title = 'Editor tonal controls'; Duration = $ScenarioDurationSeconds; Rate = [Math]::Min($SamplingRate, 50); Native = $false; GilOnly = $false
        Instructions = @('Open the compact editor before recording.', 'Drag Exposure, Brightness, Contrast, Highlights, or Shadows.', 'Release and pause so settled high-quality refinement runs.')
    },
    [pscustomobject]@{
        Key = '5'; Slug = 'editor-detail'; Title = 'Editor detail controls (safe Python stacks)'; Duration = $ScenarioDurationSeconds; Rate = [Math]::Min($SamplingRate, 50); Native = $false; GilOnly = $false
        Instructions = @('Open the compact editor before recording.', 'Drag Clarity, Texture, and Sharpness one at a time.', 'Pause briefly after each control.')
    },
    [pscustomobject]@{
        Key = '6'; Slug = 'thumbnail-grid'; Title = 'Thumbnail grid scrolling'; Duration = $ScenarioDurationSeconds; Rate = 40; Native = $false; GilOnly = $false
        Instructions = @('Switch to grid view before recording.', 'Scroll rapidly through uncached rows, reverse, then pause.', 'Do not open a photo during this run.')
    },
    [pscustomobject]@{
        Key = '7'; Slug = 'save-cycle'; Title = 'Save and watcher refresh'; Duration = $ScenarioDurationSeconds; Rate = 40; Native = $false; GilOnly = $false
        Instructions = @('Use a disposable image copy.', 'Open the editor and make a small unsaved edit before recording.', 'When recording begins, click the editor Save button once, then wait without navigating while the save and watcher refresh finish.', 'Do not press Ctrl+S; in FastStack that starts or changes stack selection.')
    },
    [pscustomobject]@{
        Key = '8'; Slug = 'navigation-gil'; Title = 'Warm navigation GIL-only comparison'; Duration = $ScenarioDurationSeconds; Rate = [Math]::Min($SamplingRate, 50); Native = $false; GilOnly = $true
        Instructions = @('Repeat the warmed navigation pattern from scenario 3.', 'This intentionally records only threads holding the Python GIL.')
    }
)

$scenarioMap = @{}
foreach ($scenario in $scenarios) {
    $scenarioMap[$scenario.Key] = $scenario
}

$finished = $false
while (-not $finished) {
    Write-Section 'Interactive profiling menu'
    foreach ($scenario in $scenarios) {
        Write-Host ('{0}. {1}' -f $scenario.Key, $scenario.Title)
    }
    Write-Host '9. Matched cache/navigation trace (separate relaunch, no py-spy)'
    Write-Host 'A. Run all py-spy scenarios in order'
    Write-Host 'H. Capture a hang/freeze thread dump'
    Write-Host 'O. Open the output folder'
    Write-Host 'Q. Finish and zip the results'
    Write-Host 'Press Enter for the recommended first set: 1,2,3.' -ForegroundColor DarkGray

    $choice = (Read-Host 'Selection').Trim()
    if (-not $choice) {
        $choice = '1,2,3'
    }

    if ($choice -match '^(?i:q|quit)$') {
        $finished = $true
        continue
    }
    if ($choice -match '^(?i:o|open)$') {
        Start-Process $SessionDirectory
        continue
    }
    if ($choice -match '^(?i:h|hang)$') {
        Invoke-HangDump -FastStackProcess $FastStackProcess -SessionDirectory $SessionDirectory -IndexPath $IndexPath -PySpyPath $PySpyPath
        continue
    }

    if ($choice -match '^(?i:9|cache|trace|cache-trace)$') {
        $FastStackProcess = Invoke-CacheTraceWorkflow -FastStackProcess $FastStackProcess -PythonPath $PythonPath -ResolvedRepoRoot $ResolvedRepoRoot -ResolvedImageDirectory $ResolvedImageDirectory -SessionDirectory $SessionDirectory -IndexPath $IndexPath
        continue
    }

    if ($choice -match '^(?i:a|all)$') {
        $selectedScenarios = $scenarios
    }
    else {
        $selectedScenarios = @()
        foreach ($key in ($choice -split '[,;\s]+' | Where-Object { $_ })) {
            if ($scenarioMap.ContainsKey($key)) {
                $selectedScenarios += $scenarioMap[$key]
            }
            else {
                Write-Warning ('Unknown selection: {0}' -f $key)
            }
        }
    }

    foreach ($scenario in $selectedScenarios) {
        Invoke-AttachedProfile -FastStackProcess $FastStackProcess -Scenario $scenario -SessionDirectory $SessionDirectory -IndexPath $IndexPath -PySpyPath $PySpyPath
    }
}

$zipPath = $SessionDirectory + '.zip'
Compress-Archive -Path (Join-Path $SessionDirectory '*') -DestinationPath $zipPath -Force

Write-Section 'Profiling complete'
Write-Host ('Results: {0}' -f $SessionDirectory) -ForegroundColor Green
Write-Host ('Upload-ready zip: {0}' -f $zipPath) -ForegroundColor Green
Write-Host 'Open any SVG in a browser; click a frame to zoom into that call path.'

$openChoice = (Read-Host 'Open the output folder now? [Y/n]').Trim()
if (-not $openChoice -or $openChoice -match '^(?i:y|yes)$') {
    Start-Process $SessionDirectory
}

$FastStackProcess.Refresh()
if (-not $FastStackProcess.HasExited) {
    $closeChoice = (Read-Host 'Close the FastStack instance started by this script? [y/N]').Trim()
    if ($closeChoice -match '^(?i:y|yes)$') {
        Stop-Process -Id $FastStackProcess.Id -Force
    }
}

