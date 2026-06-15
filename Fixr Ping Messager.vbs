' Opens the Fixr Ping Messager desktop app with no console window.
pyw = "C:\Users\kishi\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
dir = "C:\Users\kishi\OneDrive\Documents\VS Code personal projects\Fixr Ping Messager"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run """" & pyw & """ """ & dir & "\app.py""", 1, False
