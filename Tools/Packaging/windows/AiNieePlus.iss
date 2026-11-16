; Full x86-64 per-user installer: independent desktop, backend, models and fonts.
#ifndef AppVersion
  #define AppVersion "7.3.1"
#endif
#ifndef SourceRoot
  #define SourceRoot "..\..\..\dist\AiNiee"
#endif
#ifndef OutputRoot
  #define OutputRoot "..\..\..\dist\installer"
#endif

[Setup]
AppId={{B6169B4A-CB69-4B9E-ACEA-A964F391E0A8}
AppName=AiNieePlus
AppVersion={#AppVersion}
AppPublisher=Xitaoyou
AppPublisherURL=https://github.com/Xitaoyou/AiNieePlus
AppSupportURL=https://github.com/Xitaoyou/AiNieePlus/issues
DefaultDirName={localappdata}\Programs\AiNieePlus
DefaultGroupName=AiNieePlus
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\AiNiee.exe
OutputDir={#OutputRoot}
OutputBaseFilename=AiNieePlus-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Excludes: "portable.txt"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AiNieePlus"; Filename: "{app}\AiNiee.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\AiNieePlus"; Filename: "{app}\AiNiee.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\AiNiee.exe"; Description: "Launch AiNieePlus"; Flags: nowait postinstall skipifsilent
