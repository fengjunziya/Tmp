# Coding Agent 与视频剪辑 Agent

Python CLI 项目：Coding Agent 使用文件和进程工具开发、执行与修复代码；Video Agent 理解素材和自然语言需求，生成 JSON 时间线，由 FFmpeg 渲染和验证。两者共享 Responses API、日志、预算与检查点。

## 当前交付

`runs/deepseek_video/` 是实际运行并通过独立验收的 **DeepSeek 两版成片**：

| 版本 | 成片 | 实际变化 |
| --- | --- | --- |
| v1 | output_v1.mp4 | 动态视频在前、图片在后；标题“十秒灵感” |
| v2 | output_v2.mp4 | 图片在前、动态视频在后；标题“让灵感再快一步” |

两版均为 10 秒、1280×720、30fps、MP4/H.264/AAC，保留 500ms 溶解、中文结尾字幕、原声音量和 BGM。目录内含需求、时间线、验证、差异、素材分析、日志、用量、哈希及检查帧。已检查实际成片的尺寸、时长、音频覆盖、完整解码和关键画面。

`runs/demo/` 是迁移前的 OpenAI 历史结果，保留原归属。当前交付分目录存放，不覆盖旧记录。媒体在本地和交付包中；Git 保存源码、文本日志与清单。

## 环境和凭据

已验证 Windows、Python 3.13.7、FFmpeg/ffprobe 8.1.2、Git；兼容 Python 3.12+。FFmpeg 需要 libx264、AAC、subtitles/ASS、xfade，中文字体默认 `C:/Windows/Fonts/msyh.ttc`。

```powershell
python -m pip install -r requirements.txt
$env:DEEPSEEK_API_KEY = "你的 DeepSeek 密钥"
python scripts/preflight.py --check-model --output reports/env_check_deepseek.json
```

固定使用 `deepseek-flash`、`high` 和官方 `https://api.deepseek.com` Responses 端点。openai SDK 仅作为兼容客户端；认证只读取 `DEEPSEEK_API_KEY`，不使用 `OPENAI_API_KEY`，不从 `OPENAI_BASE_URL` 选择端点。缺少 DeepSeek 凭据直接失败，没有凭据或模型回退。Coding Agent 子进程移除 `OPENAI_*` 变量。

配置见 `config.example.json`：模型、端点、字体、输出、重试、模型轮数、命令超时、渲染修复次数和总时限。模型、端点和凭据变量名均受校验。迁移后使用新目录，旧提供商检查点不能恢复。

## 运行两版剪辑

```powershell
python scripts/make_demo_assets.py --output materials/demo
python video_agent.py create --assets materials/demo --request examples/request_v1.txt --run-dir runs/my_video --config config.example.json
python video_agent.py revise --run-dir runs/my_video --base-revision 1 --request examples/request_v2.txt --config config.example.json
python scripts/accept_video.py --run-dir runs/my_video
```

素材生成器创建明确标注的合成 10 秒视频、静态图片和 BGM，不需要下载。需求可替换两个文本文件；原文和硬约束独立冻结，v2 只替换明确改变的字段。通过版本不会覆盖，重复执行会核对需求、源哈希和成片记录。

复核当前交付：

```powershell
python scripts/accept_video.py --run-dir runs/deepseek_video
```

该独立验收中的标题、顺序和 10 秒条件针对仓库演示需求。自己的其他剪辑要求应配套独立验收条件。

## 使用 Coding Agent

```powershell
python scripts/prepare_coding_demo.py
python coding_agent.py run --task examples/coding_smoke.txt --workspace .local/coding_demo --run-dir runs/my_coding --config config.example.json --acceptance reports/coding_demo_acceptance.json --read-only test_calculator.py --allow-write calculator.py --allow-write sum_cli.py
```

准备脚本在独立目录放置一个有意错误的程序，同名文件已存在时拒绝覆盖。Agent 先运行出真实失败，再修改源码并重跑。自己的任务可替换任务文本、工作目录和验收 JSON；格式为 `{"commands":[["python","test_example.py"]]}`。只有独立验收通过的 finish 才视为完成。

工具为 list_files、read_file、write_file、replace_text、run_process、finish。`--resume` 恢复同一运行；`--follow-up 文件.txt` 在恢复时追加指导。预算用 `--max-turns` 和 `--total-timeout-s` 控制。进程采用 argv、明确 cwd 和超时；原始输出与源码 diff 落盘。路径限制减少误操作，不是任意程序的安全沙箱。

## 验证与限制

- 分型探测视频、图片和音频，记录稳定 ID、SHA256 和带时间点抽帧；未抽样内容不算已确认。支持候选区间加密观察和按哈希/采样参数缓存。
- Pydantic 与语义校验检查源区间、版本、字幕、BGM、时长和硬约束。支持 pad/crop、原声静音/增益、音乐、给定中文字幕、cut/fade/dissolve，时间为整数毫秒。
- 成片先写临时文件，经探测、音轨覆盖、完整解码和关键画面复核后原子发布。容差为 max(100ms, 两个输出帧时长)。抽帧和受控混音图不等于逐帧语义保证或音源分离检测。
- 仅正常速度与 v1→v2。ASR 尚未配置；对白识别/自动字幕要求会明确失败，不编造转录。复杂特效、变速、跟踪和图形编辑器未实现。
- fault-demo 为显式编码器故障诊断，固定重试本身不代表模型自主恢复；实际恢复见真实编程/时间线错误、修复和重跑记录。

## 日志、费用与打包

阶段、环境、时间与费用在 reports/；验收矩阵为 `reports/acceptance_summary.json`。早期开发保存在 runs/stage_a、runs/build_video；迁移和当前成片保存在 runs/deepseek_migration、runs/deepseek_video。用量按实际响应分提供商记录；未知用量或价格为未知，不能解释为零。正式考核完整起点和部分评分细则未确认，不声称全部正式考核通过。

```powershell
python scripts/summarize_delivery.py --run-dir runs/deepseek_video
python scripts/package_delivery.py --run-dir runs/deepseek_video --output delivery/Tmp_delivery.zip --include-history
```

交付包包含源码、依赖、配置、素材、两版成片、日志、哈希清单和 Git bundle。`completion_report.txt` 为一页说明。打包前检查凭据泄漏；保留旧包，为新包增加后缀，不删除文件。
