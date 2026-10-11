Option Explicit
Dim sh, fso, root, pythonw, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonw = root & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonw) Then
  MsgBox "Не найден:" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
         "Создай .venv и выполни: pip install -r requirements.txt", _
         vbExclamation, "PC Admin Bot"
  WScript.Quit 1
End If

sh.CurrentDirectory = root
' 0 = hidden window, False = do not wait
cmd = """" & pythonw & """ -m src.main"
sh.Run cmd, 0, False
