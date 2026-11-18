; OpenRec 安装程序脚本（Inno Setup 6）
; 编译：ISCC.exe OpenRec.iss

#define MyAppName "OpenRec"
#define MyAppVersion "1.6.8"
#define MyAppFileVersion "1.6.8.0"
#define MyAppPublisher "本地工具"
#define MyAppExeName "OpenRec.exe"
#define BuildRoot ".."
; 输出目录默认放在工程同级目录；可用 ISCC /DMyOutputDir=... 覆盖
#ifndef MyOutputDir
  #define MyOutputDir AddBackslash(SourcePath) + "..\..\OpenRec_安装包"
#endif

[Setup]
AppId={{7C6E2F41-9B3A-4E57-A2D1-5F8C0B4E91D6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppFileVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} 本地录屏 {#MyAppVersion} 安装程序
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
AllowNoIcons=yes
OutputDir={#MyOutputDir}
OutputBaseFilename=OpenRec_Setup_{#MyAppVersion}
SetupIconFile={#BuildRoot}\docs\OpenRec.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "zh"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加快捷方式："

[Files]
Source: "{#BuildRoot}\dist\OpenRec\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent unchecked

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 %1
UninstallAppTitle=卸载
UninstallAppFullTitle=卸载 %1
InformationTitle=提示
ConfirmTitle=确认
ErrorTitle=错误
SetupLdrStartupMessage=将安装 %1，是否继续？
SetupAlreadyRunning=安装程序已在运行。
WindowsVersionNotSupported=本程序不支持当前 Windows 版本。
OnlyOnTheseArchitectures=本程序只能安装到 64 位 Windows。
SetupAppRunningError=检测到 %1 正在运行，请先退出它再继续安装。
UninstallAppRunningError=检测到 %1 正在运行，请先退出它再卸载。
PrivilegesRequiredOverrideTitle=选择安装方式
PrivilegesRequiredOverrideInstruction=选择安装方式
PrivilegesRequiredOverrideAllUsers=为所有用户安装(&A)
PrivilegesRequiredOverrideAllUsersRecommended=为所有用户安装(&A)（推荐）
PrivilegesRequiredOverrideCurrentUser=仅为当前用户安装(&M)
PrivilegesRequiredOverrideCurrentUserRecommended=仅为当前用户安装(&M)（推荐）
ExitSetupTitle=退出安装
ExitSetupMessage=安装尚未完成。现在退出将不会安装程序。%n%n稍后可以重新运行安装程序。
ButtonBack=< 上一步
ButtonNext=下一步 >
ButtonInstall=安装
ButtonOK=确定
ButtonCancel=取消
ButtonYes=是
ButtonNo=否
ButtonFinish=完成
ButtonBrowse=浏览…
ButtonNewFolder=新建文件夹
ClickNext=点击“下一步”继续，或点击“取消”退出安装。
WelcomeLabel1=欢迎使用 [name] 安装向导
WelcomeLabel2=这将在您的电脑上安装 [name/ver]。%n%n录像默认保存在“视频\OpenRec”目录，可在程序内修改。
WizardSelectDir=选择安装位置
SelectDirDesc=要将 [name] 安装到哪里？
SelectDirLabel3=安装程序将把 [name] 安装到以下文件夹。
SelectDirBrowseLabel=点击“下一步”继续；如需更换文件夹，请点击“浏览”。
WizardSelectTasks=选择附加任务
WizardReady=准备安装
ReadyLabel1=安装程序已准备好，将在您的电脑上安装 [name]。
ReadyLabel2a=点击“安装”开始，或点击“上一步”修改设置。
WizardInstalling=正在安装
InstallingLabel=正在安装 [name]，请稍候。
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=已成功安装 [name]。双击桌面快捷方式或开始菜单即可启动。
ClickFinish=点击“完成”退出安装程序。
StatusClosingApplications=正在关闭相关程序…
StatusCreateDirs=正在创建目录…
StatusExtractFiles=正在解压文件…
StatusCreateIcons=正在创建快捷方式…
StatusSavingUninstall=正在保存卸载信息…
StatusRunProgram=正在完成安装…
ConfirmUninstall=确定要完全移除 %1 及其所有组件吗？
UninstallStatusLabel=正在移除 %1，请稍候。
UninstalledAll=%1 已成功移除。
WizardUninstalling=卸载状态
StatusUninstalling=正在卸载 %1…
ApplicationsFound=以下程序正在使用需要更新的文件，建议先退出它们。
FileExists2=文件已存在。
ExistingFileNewer2=现有文件比要安装的文件更新。
