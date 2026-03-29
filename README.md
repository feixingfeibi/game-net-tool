# Game Net Tool - 游戏网络工具箱

Windows 游戏网络调整与测试工具，支持进程级网络封锁、带宽限速和全局快捷键控制。

## 功能特性

- **进程选择** - 搜索并选择运行中的游戏进程
- **网络封锁** - 完全断网 / 仅禁上行 / 仅禁下行
- **速度限制** - 上行/下行独立设置 (KB/s)，支持单向无限制
- **全局快捷键** - 默认 `Ctrl+F9` 一键开关，可自定义
- **WinDivert 一键安装** - 界面内按钮自动下载配置，无需手动操作

## 环境要求

- Windows 10/11
- Python 3.10+（打包为 exe 后无需）
- 管理员权限（启动时自动请求）

## 快速开始

### 方式一：直接运行

```bash
pip install -r requirements.txt
python main.py
```

### 方式二：打包为 exe

```bash
# 双击 build.bat 或手动执行：
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name GameNetTool main.py
# 输出: dist/GameNetTool.exe
```

## 使用方法

1. 启动程序（自动请求管理员权限）
2. 在进程列表中搜索并选择目标游戏
3. 选择控制模式：
   - **完全断网** - 禁止所有网络通信
   - **仅禁上行** - 只禁止上传
   - **仅禁下行** - 只禁止下载
   - **自定义速度** - 分别设置上行/下行速度（KB/s）
4. 点击「启用」或按 `Ctrl+F9`
5. 点击「停止」或再按 `Ctrl+F9` 恢复

## 速度限制功能

速度限制依赖 WinDivert 驱动。程序内置了**一键安装**按钮：

> 选择「自定义速度限制」→ 点击「一键安装」→ 自动下载 pydivert + WinDivert 驱动 → 安装完成即可使用

也可手动安装：
1. `pip install pydivert`
2. 从 [WinDivert 官网](https://reqrypt.org/windivert.html) 下载
3. 将 `WinDivert.dll` 和 `WinDivert64.sys` 放到程序同目录

> 不安装 WinDivert 时，封锁功能（断网/禁上行/禁下行）仍然完全可用。

## 技术原理

| 功能 | 实现方式 |
|------|---------|
| 网络封锁 | Windows 防火墙规则 (`netsh advfirewall`) |
| 速度限制 | WinDivert 数据包拦截 + 令牌桶算法 |
| 进程管理 | psutil 进程枚举与端口映射 |
| 全局快捷键 | keyboard 库全局热键监听 |

程序关闭时自动清理所有防火墙规则。

## 依赖

```
customtkinter >= 5.2.0
psutil >= 5.9.0
keyboard >= 0.13.5
pydivert >= 2.1.0  # 可选，限速功能需要
```

## License

MIT
