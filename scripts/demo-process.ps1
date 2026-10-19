if (-not ('QualityDemo.CommandArguments' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace QualityDemo {
 public static class CommandArguments {
  [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
  [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
  public static string[] Parse(string commandLine) {
   int count; IntPtr pointer=CommandLineToArgvW(commandLine,out count);
   if(pointer==IntPtr.Zero) throw new InvalidOperationException("Cannot parse command line");
   try { var result=new string[count]; for(int i=0;i<count;i++)result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer,i*IntPtr.Size));return result; }
   finally { LocalFree(pointer); }
  }
 }
}
'@
}
function Get-DemoProcessIdentity($ProcessId) {
    $taskProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    if (-not $taskProcess) { return $null }
    return @{pid=[int]$ProcessId;created_utc=$taskProcess.CreationDate.ToUniversalTime().ToString('o')}
}
function Test-DemoProcessCommand($CommandLine,$Root,$Kind) {
    $taskArguments=[QualityDemo.CommandArguments]::Parse($CommandLine)
    if ($taskArguments.Count -lt 2) { return $false }
    $taskEntry=if($Kind -eq 'backend'){Join-Path $Root 'scripts\run-protocol-fixture.py'}else{Join-Path $Root 'frontend\node_modules\vite\bin\vite.js'}
    if ([IO.Path]::GetFullPath($taskArguments[1]) -ne [IO.Path]::GetFullPath($taskEntry)) { return $false }
    $taskExpected=if($Kind -eq 'backend'){@('--database','quality_demo','--port','8050')}else{@('--host','127.0.0.1','--port','5175','--strictPort','--configLoader','native')}
    if ($Kind -eq 'backend' -and $taskArguments.Count -eq 8) {
        if ($taskArguments[6] -cne '--postgres-port' -or $taskArguments[7] -notmatch '^[0-9]{1,5}$' -or [int]$taskArguments[7] -lt 1 -or [int]$taskArguments[7] -gt 65535) { return $false }
        $taskExpected+=@('--postgres-port',$taskArguments[7])
    }
    if ($taskArguments.Count -ne ($taskExpected.Count+2)) { return $false }
    for($taskIndex=0;$taskIndex -lt $taskExpected.Count;$taskIndex++){if($taskArguments[$taskIndex+2] -cne $taskExpected[$taskIndex]){return $false}}
    return $true
}
function Assert-DemoProcessIdentity($Identity,$Root,$Kind) {
    $taskProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($Identity.pid)"
    if (-not $taskProcess) { return $false }
    # PowerShell 7 ConvertFrom-Json可将ISO时间转换为DateTime；按UTC ticks比较同一时刻。
    $taskStoredTime=[DateTimeOffset]$Identity.created_utc
    if ($taskProcess.CreationDate.ToUniversalTime().Ticks -ne $taskStoredTime.UtcDateTime.Ticks -or -not (Test-DemoProcessCommand $taskProcess.CommandLine $Root $Kind)) { throw '进程创建时间或精确命令不匹配，拒绝结束' }
    return $true
}
