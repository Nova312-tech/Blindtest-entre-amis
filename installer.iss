#define AppName "Blind test"
#define AppVersion "2.0.0"
#define AppExeName "Blind test.exe"
#ifndef AppSourceDir
  #define AppSourceDir "build\windows\dist\Blind test"
#endif
#ifndef AppOutputDir
  #define AppOutputDir "build\windows\installer"
#endif

[Setup]
AppId={{9D023A8C-4EB6-4BFD-8EF7-C0B4F50E9E7D}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Blind test
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#AppOutputDir}
OutputBaseFilename=Blind-test-v2-FR-Setup
WizardStyle=modern
UninstallDisplayName={#AppName}

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Lancer {#AppName}"; Flags: postinstall nowait skipifsilent
