# -*- mode: python ; coding: utf-8 -*-
import os
import re
import shutil

# 版本号唯一来源：openrec/config.py 的 APP_VERSION
_src = open('openrec/config.py', encoding='utf-8').read()
APP_VER = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _src).group(1)
_nums = ', '.join(str(int(x)) for x in (APP_VER.split('.') + ['0'] * 4)[:4])
_ver_file = os.path.join('build', '_version_info.txt')
os.makedirs('build', exist_ok=True)
with open(_ver_file, 'w', encoding='utf-8') as _f:
    _f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({_nums}), prodvers=({_nums}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'OpenRec'),
    StringStruct('FileDescription', 'OpenRec 本地录屏'),
    StringStruct('FileVersion', '{APP_VER}'),
    StringStruct('ProductName', 'OpenRec'),
    StringStruct('ProductVersion', '{APP_VER}'),
    StringStruct('LegalCopyright', ''),
    StringStruct('OriginalFilename', 'OpenRec.exe')])]),
  VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)""")


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[('bin/ffmpeg.exe', 'bin'), ('bin/ffprobe.exe', 'bin')],
    datas=[('docs/OpenRec.ico', 'docs'), ('LICENSE', '.'), ('LICENSES', 'LICENSES')],
    hiddenimports=['cv2', 'dxcam', 'dxcam.processor._numpy_kernels'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc_data',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtMultimedia', 'PySide6.QtQml', 'PySide6.QtQuick',
              'PySide6.Qt3DCore'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OpenRec',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='docs/OpenRec.ico',
    version=_ver_file,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OpenRec',
)

# PyInstaller 6 会把 datas 放进 _internal；许可文件需与 exe 同目录
# （「关于」页与 README 均按程序目录指路），故构建后显式复制。
_out = os.path.join(DISTPATH, 'OpenRec')
if os.path.isdir(_out):
    shutil.copy2('LICENSE', os.path.join(_out, 'LICENSE'))
    shutil.copytree('LICENSES', os.path.join(_out, 'LICENSES'), dirs_exist_ok=True)
