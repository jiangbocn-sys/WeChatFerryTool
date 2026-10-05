# scripts/inject_dll.ps1
# 经典远程线程注入：OpenProcess -> VirtualAllocEx -> WriteProcessMemory -> CreateRemoteThread(LoadLibraryW)
# 把指定 DLL 强行加载到目标进程。

param(
    [string]$TargetExe = "Weixin",
    [string]$DllPath = "C:\tools\WeChatHook\version.dll",
    [int]$WaitSeconds = 8
)

if (-not (Test-Path $DllPath)) {
    Write-Host "DLL 不存在: $DllPath" -ForegroundColor Red
    exit 1
}

# 找目标进程（这里用 Weixin 进程里没有 --type= 的那个，aixed 源码里叫 main process）
Write-Host "查找 $TargetExe 主进程..." -ForegroundColor Cyan
$candidates = Get-CimInstance Win32_Process -Filter "Name='$TargetExe.exe'" -ErrorAction SilentlyContinue
if (-not $candidates) {
    Write-Host "找不到 $TargetExe.exe 进程" -ForegroundColor Red
    exit 1
}

$mainProc = $candidates | Where-Object {
    $_.CommandLine -and
    -not ($_.CommandLine -match '--type=') -and
    -not ($_.CommandLine -match '--crashpad-handler')
} | Select-Object -First 1

if (-not $mainProc) {
    Write-Host "找不到主进程（没有 --type= 的），退化为选 PID 最大的" -ForegroundColor Yellow
    $mainProc = $candidates | Sort-Object ProcessId -Descending | Select-Object -First 1
}

$targetPid = $mainProc.ProcessId
Write-Host "目标: PID=$targetPid, CmdLine: $($mainProc.CommandLine.Substring(0, [Math]::Min(120, $mainProc.CommandLine.Length)))..." -ForegroundColor Green

# 加载 Win32 API
Add-Type -Namespace Win32 -Name Kernel32 -MemberDefinition @"
[DllImport("kernel32.dll", SetLastError=true)]
public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

[DllImport("kernel32.dll", SetLastError=true)]
public static extern IntPtr VirtualAllocEx(IntPtr hProcess, IntPtr lpAddress, uint dwSize, uint flAllocationType, uint flProtect);

[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool WriteProcessMemory(IntPtr hProcess, IntPtr lpBaseAddress, byte[] lpBuffer, uint nSize, out IntPtr lpNumberOfBytesWritten);

[DllImport("kernel32.dll", SetLastError=true)]
public static extern IntPtr CreateRemoteThread(IntPtr hProcess, IntPtr lpThreadAttributes, uint dwStackSize, IntPtr lpStartAddress, IntPtr lpParameter, uint dwCreationFlags, IntPtr lpThreadId);

[DllImport("kernel32.dll", SetLastError=true)]
public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool CloseHandle(IntPtr hObject);

[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
public static extern IntPtr GetModuleHandleW(string lpModuleName);

[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Ansi)]
public static extern IntPtr GetProcAddress(IntPtr hModule, string lpProcName);
"@

$PROCESS_ALL_ACCESS = 0x001F0FFF
$MEM_COMMIT = 0x1000
$MEM_RESERVE = 0x2000
$PAGE_READWRITE = 0x04

# 打开进程
$hProc = [Win32.Kernel32]::OpenProcess($PROCESS_ALL_ACCESS, $false, $targetPid)
if ($hProc -eq [IntPtr]::Zero) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Host "OpenProcess 失败: $err (常见原因: 需要管理员权限)" -ForegroundColor Red
    exit 1
}
Write-Host "OpenProcess OK" -ForegroundColor Green

# 在目标进程分配内存，写入 DLL 路径
$dllBytes = [System.Text.Encoding]::Unicode.GetBytes($DllPath + [char]0)
$allocSize = [uint32]$dllBytes.Length
$remoteMem = [Win32.Kernel32]::VirtualAllocEx($hProc, [IntPtr]::Zero, $allocSize, $MEM_COMMIT -bor $MEM_RESERVE, $PAGE_READWRITE)
if ($remoteMem -eq [IntPtr]::Zero) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Host "VirtualAllocEx 失败: $err" -ForegroundColor Red
    [Win32.Kernel32]::CloseHandle($hProc) | Out-Null
    exit 1
}
Write-Host "VirtualAllocEx OK @ $remoteMem" -ForegroundColor Green

$written = [IntPtr]::Zero
$ok = [Win32.Kernel32]::WriteProcessMemory($hProc, $remoteMem, $dllBytes, $allocSize, [ref]$written)
if (-not $ok -or $written -eq [IntPtr]::Zero) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Host "WriteProcessMemory 失败: $err" -ForegroundColor Red
    [Win32.Kernel32]::CloseHandle($hProc) | Out-Null
    exit 1
}
Write-Host "WriteProcessMemory OK ($written bytes)" -ForegroundColor Green

# 拿到 kernel32 里 LoadLibraryW 的地址
$hKernel32 = [Win32.Kernel32]::GetModuleHandleW("kernel32.dll")
$loadLibAddr = [Win32.Kernel32]::GetProcAddress($hKernel32, "LoadLibraryW")
if ($loadLibAddr -eq [IntPtr]::Zero) {
    Write-Host "GetProcAddress(LoadLibraryW) 失败" -ForegroundColor Red
    [Win32.Kernel32]::CloseHandle($hProc) | Out-Null
    exit 1
}
Write-Host "LoadLibraryW @ $loadLibAddr" -ForegroundColor Green

# 创建远程线程
$hThread = [Win32.Kernel32]::CreateRemoteThread($hProc, [IntPtr]::Zero, 0, $loadLibAddr, $remoteMem, 0, [IntPtr]::Zero)
if ($hThread -eq [IntPtr]::Zero) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Host "CreateRemoteThread 失败: $err (常见原因: 没有 PROCESS_CREATE_THREAD 权限)" -ForegroundColor Red
    [Win32.Kernel32]::CloseHandle($hProc) | Out-Null
    exit 1
}
Write-Host "CreateRemoteThread OK，等待线程结束..." -ForegroundColor Green

# 等待结束
$WAIT_OBJECT_0 = 0
$WAIT_TIMEOUT = 0x102
$waitRes = [Win32.Kernel32]::WaitForSingleObject($hThread, [uint32]($WaitSeconds * 1000))
if ($waitRes -eq $WAIT_OBJECT_0) {
    Write-Host "线程完成，DLL 加载指令已发出" -ForegroundColor Green
} elseif ($waitRes -eq $WAIT_TIMEOUT) {
    Write-Host "等待超时（$WaitSeconds 秒），但线程已启动" -ForegroundColor Yellow
}

[Win32.Kernel32]::CloseHandle($hThread) | Out-Null
[Win32.Kernel32]::CloseHandle($hProc) | Out-Null
Write-Host "Inject flow completed." -ForegroundColor Cyan
