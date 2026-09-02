' Launch three-line-trader without a console window.
'
' Put a shortcut to this file in shell:startup so it runs at logon.
' The Tkinter window still appears -- only the cmd.exe console is hidden.
' Closing that window sends the program to the tray; quit from the tray menu.
'
' ASCII ONLY, CRLF. See the note in sync_journal.bat for why.
'
' stderr is kept in data\startup.log. Without a console there is no other way
' to find out why the program failed to start; you would just find it missing
' in the morning.

Option Explicit
Dim shell, fso, here, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

If Not fso.FolderExists(here & "\data") Then
    fso.CreateFolder here & "\data"
End If

' cmd /c is needed to redirect stderr; 0 = hidden window, False = do not wait.
cmd = "cmd /c uv run main.py 2>> """ & here & "\data\startup.log"""
shell.Run cmd, 0, False