@echo off
chcp 65001 >nul
echo ========================================
echo   游戏网络工具箱 - 打包脚本
echo ========================================
echo.

:: 切到 bat 所在目录
cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 安装依赖
echo [1/3] 安装依赖...
python -m pip install -r requirements.txt pyinstaller
echo.

:: 打包 (输出到当前目录)
echo [2/3] 打包为 exe...
python -m PyInstaller --noconfirm --onefile --windowed ^
    --name "GameNetTool" ^
    --distpath "." ^
    --workpath "build" ^
    --specpath "build" ^
    main.py
echo.

:: 清理打包临时文件
if exist build rd /s /q build
if exist __pycache__ rd /s /q __pycache__

:: 完成
echo [3/3] 打包完成!
echo.
echo 输出文件: %~dp0GameNetTool.exe
echo.
echo 注意事项:
echo   1. 运行时需要管理员权限
echo   2. 限速功能可在程序内点击「一键安装」自动配置
echo.
pause
