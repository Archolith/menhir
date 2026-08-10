' Launch start-server.ps1 with no console window.
' Resolves start-server.ps1 next to this script, so the repo can live anywhere.
Set oFSO = CreateObject("Scripting.FileSystemObject")
Set oShell = CreateObject("WScript.Shell")
sScript = oFSO.BuildPath(oFSO.GetParentFolderName(WScript.ScriptFullName), "start-server.ps1")
oShell.Run "pwsh.exe -NonInteractive -File """ & sScript & """ -Action start", 0, False
