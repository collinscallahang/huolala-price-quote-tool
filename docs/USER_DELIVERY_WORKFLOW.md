# 一键脚本用户交付工作流

本文档定义普通用户的正式交付路径。默认用户是 Windows + Edge 环境，没有 Python，也不需要懂命令行。

## 官方用户路径

普通用户只下载并双击：

```text
HuolalaPriceQuoteTool.exe
```

启动器会自动完成：

1. 读取 GitHub 仓库里的最新 `VERSION`。
2. 对比本机 `%LOCALAPPDATA%\HuolalaPriceQuoteTool\app\VERSION`。
3. 首次安装或版本不一致时，下载 `releases/huolala-price-quote-tool-portable-latest.zip`。
4. 解压并启动本地服务。
5. 打开本机查价控制页。

如果网络暂时失败，但本机已有可用版本，脚本会继续启动本机版本；如果本机也没有可用版本，脚本才会失败退出。

## 发布前步骤

1. 修改代码、配置、脚本或文档。
2. 如需让已安装用户自动更新，递增仓库根目录 `VERSION`。
3. 运行开发验证：

```powershell
$env:PYTHONPATH='src'; python -m unittest discover -s tests
```

4. 确认便携 runtime 已存在；如果缺失，先运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prepare_portable_runtime.ps1
```

5. 生成便携包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_portable_zip.ps1
```

6. 运行便携包冒烟验证：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\smoke_portable.ps1
```

7. 提交并推送 `VERSION`、脚本、文档、源码变更，以及新的 `releases\huolala-price-quote-tool-portable-latest.zip`。

## 发布检查清单

- `VERSION` 非空，且需要触发用户更新时已经递增。
- `HuolalaPriceQuoteTool.exe` 仍然指向 GitHub 仓库 raw/main 的 `VERSION` 和便携 ZIP。
- `releases\huolala-price-quote-tool-portable-latest.zip` 内包含：
  - `VERSION`
  - `configs\site.huolala.json`
  - `src\price_quote_tool\server.py`
  - `runtime\python\python.exe`
  - `一键启动查价工具.bat`
  - `启动查价工具.bat`
  - `打开查价工具网页.hta`
- ZIP 内不包含：
  - `data`
  - 历史 `output` 或 `outputs`
  - `.tmp`、`.test_tmp`
  - `__pycache__`
  - `*.egg-info`
  - `runtime\downloads`
- 冒烟验证确认 `/api/config` 返回 `app_version`，首页和 `/static/app.js` 可访问，结果下载接口存在。

## 自动更新策略

- `VERSION` 是用户交付版本的唯一来源。
- 一键脚本本地版本缺失时，一律视为需要更新。
- 更新应用文件时保留用户数据：
  - `data\browser-profile`
  - `data\browser-storage-state.json`
  - `input`
  - `output`
  - `outputs`
- 新包配置优先生效，但继承旧配置中的用户可变项：
  - `default_input_dir`
  - `output_root`
  - `keep_browser_open_after_run`

## 常见失败处理

- 用户说“还是旧版”：让用户双击 `HuolalaPriceQuoteTool.exe`，打开网页右上角确认版本号；如果版本未变，检查能否访问 GitHub raw 链接。
- 用户下载后看到代码文本：说明点到了旧的 `.bat` 或源码文件页；让用户下载 `HuolalaPriceQuoteTool.exe`，或在 GitHub 文件页点 `Download raw file`。
- 首次安装失败：优先检查网络是否能访问 GitHub；如果不能，直接发送完整便携 ZIP。
- 服务启动失败：让用户查看“批量查价工具服务”窗口里的中文错误提示；常见原因是安全软件拦截、端口被占用，或便携包不完整。
- 货拉拉页面登录/验证码问题不属于发布冒烟范围，需要用户在专用 Edge 中手工完成。
