# Coding Agent 与视频剪辑 Agent

按任务书分阶段实现的 Python CLI 项目。通用 Coding Agent 使用真实模型工具循环编写和运行代码；视频业务由该 Agent 在阶段 A 验收后生成。用户已授权使用自制 10 秒演示素材，开发总预算按最新要求调整为两小时。

## 环境

已验证 Windows、Python 3.13.7、Git、FFmpeg/ffprobe 8.1.2、openai 2.44.0、pydantic 2.13.4。兼容 Python 3.12+。FFmpeg 需要 libx264、AAC、subtitles/ASS、xfade，并安装可用中文字体。

```powershell
python -m pip install -r requirements.txt
$env:OPENAI_API_KEY = "你的密钥"
python scripts/preflight.py --check-model --output reports/env_check.json
python scripts/make_demo_assets.py --output materials/demo
```

配置见 config.example.json。模型固定 `gpt-6-sol`、推理等级固定 `high`，不自动切换模型。密钥仅从环境变量读取。可通过 `OPENAI_BASE_URL` 或 `api_base` 指定兼容端点；接口必须支持 Responses、图片和函数工具。未知单价记为 null，不能解释为免费。

## 通用 Coding Agent

```powershell
python coding_agent.py run --task examples/coding_smoke.txt --workspace .local/smoke_workspace --run-dir runs/stage_a --config config.example.json --acceptance reports/coding_smoke_acceptance.json --read-only test_calculator.py --allow-write calculator.py --allow-write sum_cli.py
```

上述命令使用阶段 A 现场建立的独立工作目录；其中真实的初始失败、源码修改和成功执行保存在 runs/stage_a。通用任务可换用自己的目录、任务文本及验收 JSON；验收格式为 `{"commands":[["python","test_example.py"]]}`。只有独立验收通过的 finish 才视为完成。`--resume` 从指定 run-dir 检查点恢复；可用 `--max-turns` 和 `--total-timeout-s` 限定预算。

工具包括 list_files、read_file、write_file、replace_text、run_process、finish。进程使用 argv 与超时；文件变更记录 SHA256 和 diff。路径限制减少误操作，不是任意程序的安全沙箱。子代理禁止删除文件，不允许改写验收以求通过。阶段完成后由执行者提交并推送 Git。

## 阶段证据

- 实施范围与验收：docs/requirements.md。
- 环境检查与计时范围：reports/。
- 编程失败、修复、验收、模型用量：runs/stage_a/。
- 合成素材生成命令与哈希：materials/demo/manifest.json。

视频命令、两版成片及交付清单将在业务代码生成并运行后补充。媒体文件保存在本地，Git 保存源码、需求、时间线、日志与哈希清单。
