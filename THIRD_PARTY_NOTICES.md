# 第三方依赖与接口

本源码包不包含 Python、Qt/PySide6、requests 或其依赖的安装包。setup.cmd 通过 pip 安装依赖；它们的许可证不因本项目采用 MIT 而改变。

- [Requests](https://requests.readthedocs.io/)：Apache License 2.0。
- [Qt for Python / PySide6 / Shiboken](https://doc.qt.io/qtforpython-6/licenses.html)：按实际组件及安装包中提供的许可证使用。开源分发涉及 LGPL/GPL 及 Qt 所含第三方组件的许可；提供预编译安装包或 EXE 前需要核对实际打包内容和相应义务。
- requests 的间接依赖：urllib3、certifi、idna、charset-normalizer。权威文本以安装后各 `.dist-info/licenses` 或包内 LICENSE 文件为准。
- [Python](https://docs.python.org/3/license.html) 及 Windows 系统 API 保留各自权利。
- 百度网盘 API 属于百度服务；开发者需自行遵守平台协议、应用审核和配额。

接口参考：[百度 OAuth 文档](https://openauth.baidu.com/doc/doc.html)、[网盘开放平台](https://pan.baidu.com/union)。

维护者确认本项目及原个人 API 案例由本人或委托 AI 编写。当前版本已移除对原案例源码的导入；独立授权模块随本项目按 MIT 提供。
