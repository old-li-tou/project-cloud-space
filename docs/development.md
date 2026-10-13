# 开发与验收

项目使用 Python 3.12、requests 和 PySide6-Essentials。当前明确支持 Windows，使用 Windows 文件句柄锁、DPAPI 和快捷方式。

- `cloud_space.py`：GUI、归档/恢复、队列、云端索引与 Agent CLI。
- `baidu_auth.py`：独立 OAuth 授权、令牌刷新和加密存储。
- `tests/`：离线标准库 unittest，不需要真实账号。

运行 `setup.cmd` 后：

```powershell
.\.venv\Scripts\python.exe -m py_compile cloud_space.py baidu_auth.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

真实上传/校验/恢复测试是显式操作：

```powershell
.\.venv\Scripts\python.exe tests/integration_roundtrip.py --live
```

先完成自己的网盘授权。此脚本生成约 8 MiB 随机文件，在临时项目中上传、验证云端索引、释放原件，再模拟无本地项目记录的整项目恢复及重复恢复。会创建真实云端测试目录，保留云端副本供检查，不自动删除云端数据。不能把真实账号凭据放入 CI。

状态默认位于程序目录 `.state`；自动测试可通过 `PROJECT_CLOUD_STATE` 指向独立目录。该环境变量只改变状态目录，不会迁移已有项目配置。

发布源码时使用 Git 中已检查的文件清单，排除运行数据。发布前核对凭据、私人路径、第三方许可证和安装说明；以后若发布 EXE，再单独检查 Qt/PySide6 的二进制分发要求。
