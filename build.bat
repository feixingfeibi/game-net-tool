@echo off
echo ========================================
echo   游戏网络工具箱 - 打包脚本
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 安装依赖
echo [1/3] 安装依赖...
pip install -r requirements.txt pyinstaller
echo.

:: 下载 WinDivert (如果需要限速功能)
echo [提示] 如需速度限制功能，请手动下载 WinDivert:
echo        https://reqrypt.org/windivert.html
echo        将 WinDivert64.sys 和 WinDivert.dll 放到 dist 目录
echo.

:: 打包
echo [2/3] 打包为 exe...
pyinstaller --noconfirm --onefile --windowed ^
    --name "GameNetTool" ^
    --add-data "requirements.txt;." ^
    --icon NUL ^
    main.py
echo.

:: 完成
echo [3/3] 打包完成!
echo 输出文件: dist\GameNetTool.exe
echo.
echo 注意事项:
echo   1. 运行时需要管理员权限
echo   2. 如需限速功能，将 WinDivert.dll 和 WinDivert64.sys
echo      放到 GameNetTool.exe 同目录
echo.
pause
