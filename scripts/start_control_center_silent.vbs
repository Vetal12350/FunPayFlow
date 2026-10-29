' Optional graphical launcher. This script does not start Codex Worker.
' Run it by double-clicking in Explorer; Windows Script Host is required.
Option Explicit

Dim shell, fs, appData, scriptDir, pythonw, launcher, cmd
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")

appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
If Len(appData) = 0 Or InStr(appData, "%") > 0 Then
    MsgBox "LOCALAPPDATA is unavailable. Use the diagnostic .bat launcher.", vbCritical, "Control Center"
    WScript.Quit 1
End If

scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
pythonw = fs.BuildPath(appData, "FunPayFlowCodexWorker\control-center-venv\Scripts\pythonw.exe")
launcher = fs.BuildPath(scriptDir, "control_center_launcher.pyw")

If Not fs.FileExists(pythonw) Then
    MsgBox "The optional Control Center UI is not installed." & vbCrLf & _
           "Run scripts\setup_control_center_ui.ps1 as documented.", vbExclamation, "Control Center"
    WScript.Quit 1
End If

If Not fs.FileExists(launcher) Then
    MsgBox "Missing control_center_launcher.pyw in scripts directory.", vbCritical, "Control Center"
    WScript.Quit 1
End If

' wscript + pythonw create no command-prompt window. Show the actual GUI normally.
cmd = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & launcher & Chr(34)
shell.Run cmd, 1, False
