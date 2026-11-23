; Data Intelligence Lab v1.0 - Inno Setup Packaging Script
; Target: Per-user Windows installation under {localappdata}\Programs\Data Intelligence Lab
; Implements crash-safe manifest transitions, 3-category process detection, and storage preservation.

#define MyAppName "Data Intelligence Lab"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Data Intelligence Lab Project"
#define MyAppURL "http://127.0.0.1:3000"
#define MyAppExeName "launcher.py"
#define MyAppId "{{D1A7B1CE-1A8B-4C9D-9E5A-3A6D47C3B8E1}}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\Data Intelligence Lab
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#SourcePath}\..\dist
OutputBaseFilename=DataIntelligenceLab-Setup-1.0.0
PrivilegesRequired=lowest
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableDirPage=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\runtime\python\python.exe
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The runtime bundle is staged into a temp directory or specified bundle directory prior to compilation
Source: "{#SourcePath}\..\bundle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Safely remove installed runtime artifacts and manifest journals
Type: files; Name: "{app}\manifest.json"
Type: files; Name: "{app}\manifest.current.json"
Type: files; Name: "{app}\manifest.previous.json"
Type: files; Name: "{app}\install.journal"
; CRITICAL SAFETY RULE: Never delete or reference {userprofile}\.data-intelligence-lab\storage!

[Code]
// ---------------------------------------------------------------------------
// Win32 Process Detection & Manifest State Machine
// ---------------------------------------------------------------------------

const
  PROCESS_QUERY_LIMITED_INFORMATION = $1000;
  TH32CS_SNAPPROCESS = $00000002;
  INVALID_HANDLE_VALUE = -1;
  ERROR_SUCCESS = 0;
  ERROR_ACCESS_DENIED = 5;
  ERROR_INVALID_PARAMETER = 87;

type
  TProcessEntry32W = record
    dwSize: DWORD;
    cntUsage: DWORD;
    th32ProcessID: DWORD;
    th32DefaultHeapID: DWORD;
    th32ModuleID: DWORD;
    cntThreads: DWORD;
    th32ParentProcessID: DWORD;
    pcPriClassBase: Longint;
    dwFlags: DWORD;
    szExeFile: array[0..259] of WideChar;
  end;

function CreateToolhelp32Snapshot(dwFlags, th32ProcessID: DWORD): THandle;
  external 'CreateToolhelp32Snapshot@kernel32.dll stdcall';

function Process32FirstW(hSnapshot: THandle; var lppe: TProcessEntry32W): BOOL;
  external 'Process32FirstW@kernel32.dll stdcall';

function Process32NextW(hSnapshot: THandle; var lppe: TProcessEntry32W): BOOL;
  external 'Process32NextW@kernel32.dll stdcall';

function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';

function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function QueryFullProcessImageNameW(hProcess: THandle; dwFlags: DWORD; lpExeName: String; var lpdwSize: DWORD): BOOL;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall';

function GetLastError(): DWORD;
  external 'GetLastError@kernel32.dll stdcall';

// Checks running processes and categorizes instances matching {app}
function CheckRunningInstances(): Boolean;
var
  AppDir, AppPrefix: String;
  hSnapshot: THandle;
  pe: TProcessEntry32W;
  hProc: THandle;
  ImageBuf: String;
  BufSize: DWORD;
  Err: DWORD;
  ExeName: String;
  RetryCheck: Boolean;
  Category1Detected: Boolean;
  Category3Detected: Boolean;
  Category3Reason: String;
  ConflictPID: DWORD;
begin
  Result := True;
  AppDir := ExpandConstant('{app}');
  AppPrefix := Uppercase(AddBackslash(AppDir));

  repeat
    RetryCheck := False;
    Category1Detected := False;
    Category3Detected := False;
    Category3Reason := '';
    ConflictPID := 0;

    hSnapshot := CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (hSnapshot = 0) or (hSnapshot = THandle(INVALID_HANDLE_VALUE)) then
    begin
      // Category 3: Process enumeration API failure
      if MsgBox('Setup was unable to verify running background processes (snapshot failure).' + #13#10 +
                'If Data Intelligence Lab is running, please close it and click Retry.',
                mbError, MB_RETRYCANCEL) = IDRETRY then
      begin
        RetryCheck := True;
        Continue;
      end
      else
      begin
        Result := False;
        Exit;
      end;
    end;

    try
      pe.dwSize := 556; // sizeof(PROCESSENTRY32W): 36 bytes header + 260*2 bytes szExeFile
      if Process32FirstW(hSnapshot, pe) then
      begin
        repeat
          ExeName := Lowercase(Trim(pe.szExeFile));
          // Filter candidate processes (launcher, python runtime, or node runtime)
          if (ExeName = 'pythonw.exe') or (ExeName = 'python.exe') or (ExeName = 'node.exe') then
          begin
            hProc := OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID);
            if hProc = 0 then
            begin
              Err := GetLastError();
              // Process exited during enumeration -> safe to ignore
              if (Err = ERROR_INVALID_PARAMETER) then
              begin
                // Process already exited, ignore
              end
              else if (Err = ERROR_ACCESS_DENIED) then
              begin
                // Category 3: Access denied on a candidate process. Fail safe.
                Category3Detected := True;
                Category3Reason := 'Access denied querying process ID ' + IntToStr(pe.th32ProcessID) + ' (' + ExeName + ').';
                Break;
              end
              else
              begin
                // Category 3: Unexpected Win32 error
                Category3Detected := True;
                Category3Reason := 'Win32 error ' + IntToStr(Err) + ' querying process ID ' + IntToStr(pe.th32ProcessID) + '.';
                Break;
              end;
            end
            else
            begin
              try
                SetLength(ImageBuf, 1024);
                BufSize := 1024;
                if QueryFullProcessImageNameW(hProc, 0, ImageBuf, BufSize) then
                begin
                  SetLength(ImageBuf, BufSize);
                  // Normalize slashes and case
                  StringChange(ImageBuf, '/', '\');
                  ImageBuf := Uppercase(ImageBuf);

                  // Strict directory boundary matching
                  if Pos(AppPrefix, ImageBuf) = 1 then
                  begin
                    // Category 1: Confirmed DIL process running from {app}
                    Category1Detected := True;
                    ConflictPID := pe.th32ProcessID;
                    Break;
                  end;
                  // Category 2: Confirmed unrelated process (path does not begin with AppPrefix) -> Ignored
                end
                else
                begin
                  // Query failed -> Category 3
                  Category3Detected := True;
                  Category3Reason := 'Unable to query image path for process ID ' + IntToStr(pe.th32ProcessID) + '.';
                  Break;
                end;
              finally
                CloseHandle(hProc);
              end;
            end;
          end;
        until not Process32NextW(hSnapshot, pe);
      end;
    finally
      CloseHandle(hSnapshot);
    end;

    if Category1Detected then
    begin
      // Category 1: Application running from {app}. Ask user to close and retry.
      if MsgBox('Data Intelligence Lab is currently running (Process ID: ' + IntToStr(ConflictPID) + ').' + #13#10 +
                'Please save your work, close the application, and click Retry to continue.',
                mbConfirmation, MB_RETRYCANCEL) = IDRETRY then
      begin
        RetryCheck := True;
      end
      else
      begin
        Result := False;
        Exit;
      end;
    end
    else if Category3Detected then
    begin
      // Category 3: Uncertainty / Access Denied. Fail safe with advisory dialog.
      if MsgBox('Setup was unable to verify all background processes.' + #13#10 +
                Category3Reason + #13#10 +
                'If Data Intelligence Lab is running, please close it and click Retry.',
                mbError, MB_RETRYCANCEL) = IDRETRY then
      begin
        RetryCheck := True;
      end
      else
      begin
        Result := False;
        Exit;
      end;
    end;

  until not RetryCheck;
end;

function InitializeSetup(): Boolean;
begin
  Result := CheckRunningInstances();
end;

// Extracts declared relative file paths from a manifest.json file
procedure ExtractManifestPaths(const ManifestFile: String; var Paths: TStringList);
var
  Lines: TArrayOfString;
  I, P1, P2, NumLines: Integer;
  Line, RelPath: String;
begin
  if not FileExists(ManifestFile) then Exit;
  if LoadStringsFromFile(ManifestFile, Lines) then
  begin
    NumLines := GetArrayLength(Lines);
    for I := 0 to NumLines - 1 do
    begin
      Line := Lines[I];
      P1 := Pos('"path": "', Line);
      if P1 > 0 then
      begin
        P1 := P1 + 9; // Skip past '"path": "'
        P2 := Pos('"', Copy(Line, P1, Length(Line) - P1 + 1));
        if P2 > 0 then
        begin
          RelPath := Copy(Line, P1, P2 - 1);
          StringChange(RelPath, '/', '\');
          Paths.Add(RelPath);
        end;
      end;
    end;
  end;
end;

// Validates that a candidate relative path is safe to delete from {app}
function IsSafeRelativeAppPath(const RelPath, AppDir: String; var FullPath: String): Boolean;
var
  NormRel, AppPrefix: String;
begin
  Result := False;
  FullPath := '';

  if Length(RelPath) = 0 then Exit;

  // Reject drive-qualified, UNC, or absolute paths
  if Pos(':', RelPath) > 0 then Exit;
  if (Copy(RelPath, 1, 1) = '\') or (Copy(RelPath, 1, 1) = '/') then Exit;
  if Pos('\\', RelPath) > 0 then Exit;

  // Reject directory traversal
  if Pos('..', RelPath) > 0 then Exit;

  // Construct target full path
  NormRel := RelPath;
  StringChange(NormRel, '/', '\');
  FullPath := AddBackslash(AppDir) + NormRel;
  AppPrefix := Uppercase(AddBackslash(AppDir));

  // Enforce directory boundary containment within AppDir
  if Pos(AppPrefix, Uppercase(FullPath)) <> 1 then Exit;

  // Protect manifest and journal files
  if (Uppercase(NormRel) = 'MANIFEST.JSON') or
     (Uppercase(NormRel) = 'MANIFEST.CURRENT.JSON') or
     (Uppercase(NormRel) = 'MANIFEST.PREVIOUS.JSON') or
     (Uppercase(NormRel) = 'INSTALL.JOURNAL') then Exit;

  // Protect user storage directory
  if Pos('.DATA-INTELLIGENCE-LAB\STORAGE', Uppercase(FullPath)) > 0 then Exit;

  Result := True;
end;

// Safely deletes obsolete files between previous and promoted current manifests
procedure CleanupObsoleteFiles(const PreviousManifest, CurrentManifest, AppDir, JournalPath: String);
var
  PrevPaths, CurrPaths: TStringList;
  I: Integer;
  Candidate, TargetPath: String;
begin
  if (not FileExists(PreviousManifest)) or (not FileExists(CurrentManifest)) then Exit;

  SaveStringToFile(JournalPath, 'STEP=CLEANING' + #13#10, False);

  PrevPaths := TStringList.Create;
  CurrPaths := TStringList.Create;
  try
    ExtractManifestPaths(PreviousManifest, PrevPaths);
    ExtractManifestPaths(CurrentManifest, CurrPaths);

    for I := 0 to PrevPaths.Count - 1 do
    begin
      Candidate := PrevPaths[I];
      if CurrPaths.IndexOf(Candidate) = -1 then
      begin
        // Candidate file was in previous manifest but is absent from current manifest
        if IsSafeRelativeAppPath(Candidate, AppDir, TargetPath) then
        begin
          if FileExists(TargetPath) then
          begin
            if not DeleteFile(TargetPath) then
            begin
              Log('Warning: Could not delete obsolete file (locked or in-use): ' + TargetPath);
            end
            else
            begin
              Log('Cleaned obsolete file: ' + TargetPath);
            end;
          end;
        end
        else
        begin
          Log('Warning: Rejected unsafe candidate obsolete path: ' + Candidate);
        end;
      end;
    end;
  finally
    PrevPaths.Free;
    CurrPaths.Free;
  end;
end;

// Journal recovery and manifest atomic transition
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir, JournalPath, CurrentManifest, PreviousManifest, StagedManifest: String;
begin
  AppDir := ExpandConstant('{app}');
  JournalPath := ExpandConstant('{app}\install.journal');
  CurrentManifest := ExpandConstant('{app}\manifest.current.json');
  PreviousManifest := ExpandConstant('{app}\manifest.previous.json');
  StagedManifest := ExpandConstant('{app}\manifest.json');

  if CurStep = ssInstall then
  begin
    // Check if previous incomplete install left a journal
    if FileExists(JournalPath) then
    begin
      Log('Detected incomplete prior installation journal; reconciling state...');
    end;
    SaveStringToFile(JournalPath, 'STEP=INSTALLING' + #13#10, False);
  end
  else if CurStep = ssPostInstall then
  begin
    // Promote staged manifest to current
    if FileExists(StagedManifest) then
    begin
      if FileExists(CurrentManifest) then
      begin
        // Atomically rotate current manifest to previous
        if FileExists(PreviousManifest) then DeleteFile(PreviousManifest);
        RenameFile(CurrentManifest, PreviousManifest);
      end;
      CopyFile(StagedManifest, CurrentManifest, False);
    end;

    // Perform manifest-driven obsolete file cleanup
    CleanupObsoleteFiles(PreviousManifest, CurrentManifest, AppDir, JournalPath);

    // Complete journal
    SaveStringToFile(JournalPath, 'STEP=COMPLETED' + #13#10, False);
    DeleteFile(JournalPath);
  end;
end;

// Uninstaller safety guard: ensures user datasets are never uninstalled or pruned
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Log('Uninstallation complete. User dataset directory at %USERPROFILE%\.data-intelligence-lab\storage was preserved.');
  end;
end;
