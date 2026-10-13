# Agent 使用说明

在 PowerShell 中使用本工具自己的虚拟环境；命令路径以实际安装位置为准。

```powershell
.\.venv\Scripts\python.exe cloud_space.py status
.\.venv\Scripts\python.exe cloud_space.py authorize
.\.venv\Scripts\python.exe cloud_space.py cloud-projects
.\.venv\Scripts\python.exe cloud_space.py backup-indexes
.\.venv\Scripts\python.exe cloud_space.py recover-project --remote "/项目云空间/具体项目目录" --destination "E:\找回项目"
```

- `status` 只读本地快照，不联网、不启动窗口、不改变队列。0=运行且心跳新鲜，3=已停止，2=缺失/过期/无法确认。心跳默认每秒刷新，超过 10 秒视为过期。
- 其余命令联网，须先正常关闭 GUI；使用同一状态目录的命令受单实例锁保护。
- `authorize` 是由用户在本机完成的交互式授权，Secret Key 和授权码不回显。不要要求用户在聊天或 issue 中粘贴凭据。
- `cloud-projects` 返回可选择的云端项目目录。`backup-indexes` 核对并备份本机已登记项目的归档索引。
- `recover-project` 不依赖本机项目记录；destination 是最终项目目录，GUI 则先选择父目录再创建项目子目录。
- 联网命令 stdout 返回 JSON，stderr 返回进度；0=成功，1=失败或部分完成，参数错误由 argparse 返回 2。
- GUI 心跳中的 queue 包含逐文件状态、尝试次数、待处理/失败数量和限流截止时间。recovery 包含整项目恢复状态、计数及报告路径。
- `running=true` 和 `accepting_archives=true` 表示监听可运行，不保证账号授权或网络一定正常。结合最近阶段、接口错误与实际归档记录判断。
- 上传 100% 不等于归档完成；必须等数据校验、索引核对成功后程序自身释放原件。不要代替程序手工删除。
- 新电脑可以重新安装、授权同一网盘账号，再从云端索引恢复。不要把 Windows 用户的加密凭据当作跨电脑授权备份。

只有用户确认已经停用且允许归档的文件才能移入云空间。不要移动活动数据库、运行中的项目资料或系统文件。
