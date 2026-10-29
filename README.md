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

## 视频 Agent（已实际运行）

```powershell
python video_agent.py create --assets materials/demo --request examples/request_v1.txt --run-dir runs/demo --config config.example.json
python video_agent.py revise --run-dir runs/demo --base-revision 1 --request examples/request_v2.txt --config config.example.json
python scripts/accept_video.py
```

`--config` 缺省为 `config.example.json`。FFmpeg/ffprobe 和中文字体必须可用，模型图片与工具调用需要配置支持 Responses API 的凭据。模型分析的是抽样素材帧和**实际编码成片**帧，不把未采样时段描述为已确认；演示为无对白合成素材，文案来自用户，不启动 ASR。若正式任务需要对白识别/自动字幕且没有带时间戳的真实 ASR 服务，将明确失败，不会编造转录。模型提取需求后冻结 `requirements_vN.json`；v2 继承除明确替代的标题和镜头顺序外的每项硬要求。视频镜头时长是源区间，图片显示时长独立；500ms dissolve 是真实交叠并含跨淡音频。中文 SRT 由本地文件烧录。Pydantic 和实际源文件共同约束时间线；渲染只允许经本地编译的滤镜，产物先验证 `.pending.mp4` 再原子发布。若通过版本已存在，重复运行不会覆盖。

交付在 `runs/demo/`：`assets.json`、`analysis.json`、`requirements_v1/v2.json`、`timeline_v1/v2.json`、`derived_v1/v2.json`、`output_v1/v2.mp4`、`validation_v1/v2.json`、`revision_diff.json`、`events.jsonl`、`usage.json`、`manifest.json`、两版 `agent_v*/checkpoint.json`、证据帧与 `commands/` 原始 argv/退出码/标准输出错误。失败候选和故障文件一并保留，禁止删除；`manifest.json` 不递归哈希自身，关联素材 SHA256。费用缺乏核实单价时为 null/unknown。

若正常渲染没有自然失败，独立的**显式故障注入**用于真实证明错误→修复：

```powershell
python video_agent.py fault-demo --run-dir runs/demo --config config.example.json
```

此命令用刻意无效的视频编码器触发 FFmpeg 非零退出，记录原始 stderr 和故障来源，再用 `libx264` 修正并 probe 真正生成的诊断媒体；诊断产物**不**替代两版正式成片。每版渲染最多首次加两次修复尝试。验证含完整解码、流参数、时长和模型复核的有限采样帧；不能保证未采样每帧内容，也不能凭混合音轨完全隔离证明单一路的增益，增益以受控滤镜链、源证据和真实 AAC 音轨为依据。指定服务、计费和评分细则等未确认事项仍需正式考核方确认。媒体文件保存在本地，由主执行者处理 Git 提交。
