# Lunmi media helper: fixed JSONL. No user value is interpolated.
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $env:PSModuleAnalysisCachePath='nul'
try { [Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false); [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false) } catch {}
$script:Manager=$null; $script:Token=$null; $script:Session=$null; $script:Fingerprint=$null; $script:InitError=$null
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $script:ManagerType=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
  $script:PropsType=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties,Windows.Media.Control,ContentType=WindowsRuntime]
  $script:AsTask=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.IsGenericType -and $_.GetParameters()[0].ParameterType.GetGenericTypeDefinition().FullName -eq 'Windows.Foundation.IAsyncOperation`1' } | Select-Object -First 1
  if ($null -eq $script:AsTask) { throw 'astask' }
} catch { $script:InitError='winrt_unavailable' }

function Limit($v,[int]$max) {
  if ($null -eq $v) { return '' }
  $t=[regex]::Replace([string]$v,'[\x00-\x1F\x7F]+',' '); $t=[regex]::Replace($t,'\s+',' ').Trim()
  if ($t.Length -gt $max) { $t=$t.Substring(0,$max) }; return $t
}
function Await($op,[Type]$type,[int]$ms) {
  $task=$script:AsTask.MakeGenericMethod($type).Invoke($null,@($op))
  if (-not $task.Wait($ms)) { throw 'timeout' }; return $task.Result
}
function Empty { return [ordered]@{v=1;available=$false;session_id=$null;title='';artist='';source_app='';playback='unknown';can_play=$false;can_pause=$false;can_previous=$false;can_next=$false} }
function Reset { $script:Token=$null; $script:Session=$null; $script:Fingerprint=$null }
function NewToken { return 'm-'+[guid]::NewGuid().ToString('N') }
function Fingerprint($s,$source,$title,$artist) {
  $end=0; try { $end=$s.GetTimelineProperties().EndTime.Ticks } catch {}
  return $source+[char]31+$title+[char]31+$artist+[char]31+$end
}
function Current {
  try {
    if ($null -eq $script:Manager) { $script:Manager=Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) $script:ManagerType 2500 }
    return $script:Manager.GetCurrentSession()
  } catch { $script:Manager=$null; return $null }
}
function GetState {
  if ($script:InitError) { return Empty }
  $s=Current; if ($null -eq $s) { Reset; return Empty }
  try { $info=$s.GetPlaybackInfo() } catch { Reset; return Empty }
  $source=Limit $s.SourceAppUserModelId 120; $title=''; $artist=''
  try { $p=Await ($s.TryGetMediaPropertiesAsync()) $script:PropsType 900; if ($null -ne $p) { $title=Limit $p.Title 160; $artist=Limit $p.Artist 120 } } catch {}
  $fp=Fingerprint $s $source $title $artist
  if ($script:Token -and [object]::ReferenceEquals($script:Session,$s) -and $script:Fingerprint -ceq $fp) { $token=$script:Token } else { $script:Token=NewToken; $script:Session=$s; $script:Fingerprint=$fp; $token=$script:Token }
  $status=[string]$info.PlaybackStatus; $playback='unknown'
  if ($status -eq 'Playing') { $playback='playing' } elseif ($status -eq 'Paused') { $playback='paused' } elseif ($status -eq 'Stopped' -or $status -eq 'Closed') { $playback='stopped' }
  $c=$info.Controls; $cp=$false; $cu=$false; $cprev=$false; $cnext=$false
  if ($null -ne $c) { $cp=($status -ne 'Playing' -and $status -ne 'Changing') -and ($c.IsPlayEnabled -or $c.IsPlayPauseToggleEnabled); $cu=($status -eq 'Playing') -and ($c.IsPauseEnabled -or $c.IsPlayPauseToggleEnabled); $cprev=[bool]$c.IsPreviousEnabled; $cnext=[bool]$c.IsNextEnabled }
  return [ordered]@{v=1;available=$true;session_id=$token;title=$title;artist=$artist;source_app=$source;playback=$playback;can_play=$cp;can_pause=$cu;can_previous=$cprev;can_next=$cnext}
}
function Result($ok,$error,$state) { return [ordered]@{v=1;ok=[bool]$ok;error=$error;state=$state} }
function DoCommand($command,$sessionId) {
  if ($script:InitError) { return Result $false 'media_unavailable' (Empty) }
  if ($script:Token -cne $sessionId -or $null -eq $script:Session) { return Result $false 'stale_session' (GetState) }
  $captured=$script:Session; $current=Current
  if ($null -eq $current) { Reset; return Result $false 'stale_session' (Empty) }
  if (-not [object]::ReferenceEquals($captured,$current)) { Reset; return Result $false 'stale_session' (GetState) }
  $source=Limit $current.SourceAppUserModelId 120; $title=''; $artist=''
  try { $p=Await ($current.TryGetMediaPropertiesAsync()) $script:PropsType 900; if ($null -ne $p) { $title=Limit $p.Title 160; $artist=Limit $p.Artist 120 } } catch {}
  if ((Fingerprint $current $source $title $artist) -cne $script:Fingerprint) { Reset; return Result $false 'stale_session' (GetState) }
  try { $info=$captured.GetPlaybackInfo() } catch { Reset; return Result $false 'stale_session' (GetState) }
  $status=[string]$info.PlaybackStatus; if ($status -eq 'Changing') { return Result $false 'busy' (GetState) }
  $c=$info.Controls; $ok=$false; $error=$null
  if ($null -eq $c) { $error='capability_unavailable' }
  elseif ($command -eq 'play') { if ($status -eq 'Playing') { $ok=$true } elseif ($c.IsPlayEnabled) { $ok=Await ($captured.TryPlayAsync()) ([bool]) 2500 } elseif ($c.IsPlayPauseToggleEnabled) { $ok=Await ($captured.TryTogglePlayPauseAsync()) ([bool]) 2500 } else { $error='capability_unavailable' } }
  elseif ($command -eq 'pause') { if ($status -eq 'Paused') { $ok=$true } elseif ($status -eq 'Playing' -and $c.IsPauseEnabled) { $ok=Await ($captured.TryPauseAsync()) ([bool]) 2500 } elseif ($status -eq 'Playing' -and $c.IsPlayPauseToggleEnabled) { $ok=Await ($captured.TryTogglePlayPauseAsync()) ([bool]) 2500 } else { $error='capability_unavailable' } }
  elseif ($command -eq 'previous') { if ($c.IsPreviousEnabled) { $ok=Await ($captured.TrySkipPreviousAsync()) ([bool]) 2500 } else { $error='capability_unavailable' } }
  elseif ($command -eq 'next') { if ($c.IsNextEnabled) { $ok=Await ($captured.TrySkipNextAsync()) ([bool]) 2500 } else { $error='capability_unavailable' } }
  else { $error='invalid_input' }
  if (-not $ok -and $null -eq $error) { $error='command_failed' }
  return Result $ok $error (GetState)
}
function Emit($v) { [Console]::Out.WriteLine(($v | ConvertTo-Json -Compress -Depth 8)); [Console]::Out.Flush() }
Emit ([ordered]@{v=1;type='ready'})
while ($true) {
  $line=[Console]::In.ReadLine(); if ($null -eq $line) { break }
  if ($line.Length -gt 4096) { Emit (Result $false 'message_too_large' (Empty)); continue }
  try { $r=$line | ConvertFrom-Json } catch { Emit ([ordered]@{v=1;id='unknown';ok=$false;error='invalid_json';state=(Empty)}); continue }
  $id='unknown'; if ($r.id -is [string] -and $r.id -match '^[A-Za-z0-9_-]{1,64}$') { $id=$r.id }
  if ($r.v -ne 1) { Emit ([ordered]@{v=1;id=$id;ok=$false;error='invalid_version';state=(Empty)}); continue }
  try {
    if ([string]$r.type -eq 'get') { Emit ([ordered]@{v=1;id=$id;ok=$true;error=$null;state=(GetState)}) }
    elseif ([string]$r.type -eq 'command') {
      $command=[string]$r.command; $sessionId=[string]$r.session_id
      if (($command -notin @('play','pause','previous','next')) -or ($sessionId -notmatch '^[A-Za-z0-9_-]{1,64}$')) { Emit ([ordered]@{v=1;id=$id;ok=$false;error='invalid_input';state=(Empty)}) }
      else { $result=DoCommand $command $sessionId; Emit ([ordered]@{v=1;id=$id;ok=$result.ok;error=$result.error;state=$result.state}) }
    } else { Emit ([ordered]@{v=1;id=$id;ok=$false;error='unknown_type';state=(Empty)}) }
  } catch { Emit ([ordered]@{v=1;id=$id;ok=$false;error='helper_error';state=(Empty)}) }
}
