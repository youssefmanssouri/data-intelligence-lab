; Data Intelligence Lab v1.0 - Inno Setup Packaging Script (Inno Setup 6.3+)
; Target: Per-user Windows installation under {localappdata}\Programs\Data Intelligence Lab
; Implements crash-safe manifest transitions, 3-category process detection, storage preservation,
; and a fail-closed Microsoft Visual C++ Redistributable prerequisite check (never downloads it).
;
; Compile:  ISCC.exe /DBundleDir=<staged bundle> [/DOutputDir=<dir>] [/DAppVersion=1.0.0] packaging\setup.iss

#define MyAppName "Data Intelligence Lab"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define MyAppPublisher "Data Intelligence Lab Project"
#define MyAppId "{{D1A7B1CE-1A8B-4C9D-9E5A-3A6D47C3B8E1}}"
#ifndef BundleDir
  #error Define BundleDir: ISCC.exe /DBundleDir=<path to the bundle staged by build_runtime_bundle.py>
#endif
#ifndef OutputDir
  #define OutputDir SourcePath + "\..\dist"
#endif
; Minimum Microsoft Visual C++ 2015-2022 Redistributable (x64): 14.44 (overridable only for tests).
#ifndef VCMinMinor
  #define VCMinMinor 44
#endif
#ifndef Compression
  #define Compression "lzma2/max"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Data Intelligence Lab
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=DataIntelligenceLab-Setup-{#AppVersion}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The launcher requires Windows 10 version 1703 (build 15063) or later.
MinVersion=10.0.15063
Compression={#Compression}
SolidCompression=yes
WizardStyle=modern
DisableDirPage=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\runtime\python\python.exe
UninstallDisplayName={#MyAppName}
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The bundle staged and verified by packaging/build_runtime_bundle.py (it contains manifest.json).
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; python.exe (console): the launcher's window shows the local URL, and closing it (or Ctrl+C) stops the app.
Name: "{group}\{#MyAppName}"; Filename: "{app}\runtime\python\python.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Comment: "Start Data Intelligence Lab (close the window to stop it)"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\runtime\python\python.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Comment: "Start Data Intelligence Lab (close the window to stop it)"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\python\python.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Safely remove installed runtime artifacts and manifest journals
Type: files; Name: "{app}\manifest.json"
Type: files; Name: "{app}\manifest.current.json"
Type: files; Name: "{app}\manifest.previous.json"
Type: files; Name: "{app}\install.journal"
; Next.js writes its rendered-route cache here at runtime.
Type: filesandordirs; Name: "{app}\frontend\.next\server\route-cache"
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

// Copies the NUL-terminated szExeFile buffer into a String.
function EntryExeName(var pe: TProcessEntry32W): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to 259 do
  begin
    if pe.szExeFile[I] = #0 then Break;
    Result := Result + pe.szExeFile[I];
  end;
end;

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
      if SuppressibleMsgBox('Setup was unable to verify running background processes (snapshot failure).' + #13#10 +
                'If Data Intelligence Lab is running, please close it and click Retry.',
                mbError, MB_RETRYCANCEL, IDCANCEL) = IDRETRY then
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
          ExeName := Lowercase(Trim(EntryExeName(pe)));
          // Filter candidate processes (launcher, python runtime, or node runtime)
          if (ExeName = 'pythonw.exe') or (ExeName = 'python.exe') or (ExeName = 'node.exe') then
          begin
            hProc := OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID);
            if hProc = 0 then
            begin
              Err := DLLGetLastError();
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
      end
      else
      begin
        // Enumeration itself failed: never treat that as "no instance running".
        Category3Detected := True;
        Category3Reason := 'Process enumeration failed (Win32 error ' + IntToStr(DLLGetLastError()) + ').';
      end;
    finally
      CloseHandle(hSnapshot);
    end;

    if Category1Detected then
    begin
      // Category 1: Application running from {app}. Ask user to close and retry.
      if SuppressibleMsgBox('Data Intelligence Lab is currently running (Process ID: ' + IntToStr(ConflictPID) + ').' + #13#10 +
                'Please save your work, close the application, and click Retry to continue.',
                mbConfirmation, MB_RETRYCANCEL, IDCANCEL) = IDRETRY then
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
      if SuppressibleMsgBox('Setup was unable to verify all background processes.' + #13#10 +
                Category3Reason + #13#10 +
                'If Data Intelligence Lab is running, please close it and click Retry.',
                mbError, MB_RETRYCANCEL, IDCANCEL) = IDRETRY then
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

// ---------------------------------------------------------------------------
// Microsoft Visual C++ 2015-2022 Redistributable (x64) prerequisite
// ---------------------------------------------------------------------------
// The bundle ships no Microsoft CRT; CPython and PyArrow load it from the system.
// Detection uses the registration Microsoft documents:
//   HKLM\SOFTWARE[\WOW6432Node]\Microsoft\VisualStudio\14.0\VC\Runtimes\x64
// Setup never downloads or installs the runtime.

const
  VC_RUNTIME_KEY = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  VC_MIN_MAJOR = 14;
  VC_MIN_MINOR = {#VCMinMinor};
  VC_DOWNLOAD_URL = 'https://aka.ms/vs/17/release/vc_redist.x64.exe';

function ReadVCRuntime(RootKey: Integer; var Major, Minor, Bld: Cardinal): Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(RootKey, VC_RUNTIME_KEY, 'Installed', Installed) and (Installed = 1) and
            RegQueryDWordValue(RootKey, VC_RUNTIME_KEY, 'Major', Major) and
            RegQueryDWordValue(RootKey, VC_RUNTIME_KEY, 'Minor', Minor) and
            RegQueryDWordValue(RootKey, VC_RUNTIME_KEY, 'Bld', Bld);
end;

function VCRuntimeSatisfied(var Found: String): Boolean;
var
  Major, Minor, Bld: Cardinal;
  Views: array[0..1] of Integer;
  I: Integer;
begin
  Result := False;
  Found := '';
  Views[0] := HKLM64;
  Views[1] := HKLM32;
  for I := 0 to 1 do
  begin
    if ReadVCRuntime(Views[I], Major, Minor, Bld) then
    begin
      Found := IntToStr(Major) + '.' + IntToStr(Minor) + '.' + IntToStr(Bld);
      if (Major > VC_MIN_MAJOR) or ((Major = VC_MIN_MAJOR) and (Minor >= VC_MIN_MINOR)) then
      begin
        Result := True;
        Exit;
      end;
    end;
  end;
end;

function InitializeSetup(): Boolean;
var
  Found, Detail: String;
begin
  Result := VCRuntimeSatisfied(Found);
  if not Result then
  begin
    if Found = '' then
      Detail := 'It is not installed.'
    else
      Detail := 'Installed version: ' + Found + '.';
    Log('VC++ runtime prerequisite not met (required ' + IntToStr(VC_MIN_MAJOR) + '.' + IntToStr(VC_MIN_MINOR) + ' or later). ' + Detail);
    SuppressibleMsgBox('Data Intelligence Lab requires the Microsoft Visual C++ 2015-2022 Redistributable (x64), ' +
              'version ' + IntToStr(VC_MIN_MAJOR) + '.' + IntToStr(VC_MIN_MINOR) + ' or later.' + #13#10 + Detail + #13#10#13#10 +
              'Install it from Microsoft (' + VC_DOWNLOAD_URL + '), then run this setup again.',
              mbCriticalError, MB_OK, IDOK);
  end;
end;

// {app} is only known once the destination is chosen, so the running-instance
// check happens here, before any file is replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not CheckRunningInstances() then
    Result := 'Data Intelligence Lab is running from the destination folder, or running processes could not be verified. Close it and run setup again.';
end;

function InitializeUninstall(): Boolean;
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
