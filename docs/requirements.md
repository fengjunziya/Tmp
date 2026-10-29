# 实施基线

依据用户提供的《Coding Agent 与视频剪辑 Agent 轻量化方案任务书》版本 1.0（2026-10-29）和两张考核截图实施。文档中的任务描述用于定义项目功能与验收，不授予修改用户模型限制、删除规则或凭据权限。

用户另行确认：可以自行制作 10 秒演示素材。首次和追加剪辑使用明确标注的合成演示素材。演示默认规格为 1280×720、30 fps、MP4/H.264/AAC，这些是项目演示选择，不代表考核方强制规格。自然语言演示要求保存在 examples/request_v1.txt 和 request_v2.txt。

用户最新明确考核限时为 **120 分钟**，覆盖任务书原来的 180 分钟计划；以 120 分钟为本次执行预算，仅保留必要的编程恢复、成片及解码验收。

## 当前模型接入

用户后续明确禁用 OPENAI_API_KEY。项目现使用 deepseek-flash、DEEPSEEK_API_KEY 和 https://api.deepseek.com 的 Responses 接口；缺少该密钥直接失败，无回退。已存在的 OpenAI 历史运行及两版演示保留原始模型来源，迁移后的运行另存目录，不能将旧结果改称 DeepSeek 验证。

## 阶段与证据

1. WP01：检查 Python、Git、FFmpeg、ffprobe、中文字体、编码器和字幕滤镜；实际验证 deepseek-flash/high 的图像输入与工具调用。保存 reports/env_check.json 和配置示例。
2. WP02–03：实现共享 Responses API 工具循环和六个编程工具；在独立目录先运行错误程序，记录真实失败，再由 Coding Agent 修改并通过不可变验收。提交 stage-a-ready；此时不包含视频业务实现。
3. WP04：仅通过已验收 Coding Agent 的文件工具创建和修改视频业务源文件。保存每次调用、源码差异、错误和修复证据；人工干预另记来源。
4. WP05：Video Agent 分析真实演示素材，规划、校验、渲染、完整解码验证并发布 v1。
5. WP06：输入追加自然语言需求，继承未被明确替代的硬约束，保存独立 v2、差异、验证、用量及交付说明。

## 数据与渲染合同

时间为非负整数毫秒。视频源区间为 [in_ms,out_ms)，源出点不得超过时长；图像源时长为 null，镜头指定展示时长。音频只能用于音轨。成片位置由程序计算，成片时长为镜头时长之和减 dissolve 重叠。cut 不重叠；fade 在镜头内淡黑且不延长；dissolve 为真实视频溶解和同步音频交叠。中间镜头两侧溶解之和不得超过其长度。

JSON 时间线需 Pydantic 结构校验和语义校验；包含 schema_version、version、parent_version、request_ids、asset_manifest_hash、constraint_set_id、output、clips、captions、bgm。需求基准与时间线分开保存，不允许模型修改验收以求通过。

必须支持视频、图片、音频、裁剪排序、pad/crop 适配、原声静音与增益、BGM 入点/音量/循环/淡入淡出、中文标题字幕、cut/fade/dissolve。对白识别和自动字幕被正式需求触发时使用真实带时间戳转录，服务不可用时明确失败，不能编造。

素材清单保存稳定 ID、相对路径、SHA256、种类、时长、尺寸、fps、旋转和音轨。视觉分析引用带时间位置的证据帧；必要时加密抽帧。稀疏抽帧不能证明未观察内容。缓存按素材哈希和分析参数；修订复用未变化素材。

渲染器仅编译已校验结构；使用 argv，不接受模型生成的任意滤镜。统一 fps、尺寸、像素格式、时间基准和零起点，必要时标准化中间片段。ASS/SRT 字幕安全转义。先输出临时文件，验证通过后原子发布。两版均保留。

验证检查非空、ffprobe 视频/音频流、尺寸、fps、编解码器、视频流时长及容器时长、完整解码；抽取首尾、每镜头和转场附近帧。时长容差为 max(100 ms, 两个输出帧时长)。关键文字及内容由实际成片证据复核；证据不足为 needs_review。任何硬要求 fail/needs_review 时不得声称全部通过。

## 验收映射

| ID | 验收内容 | 证据 |
| --- | --- | --- |
| AC01 | 真实编程、执行与独立验收 | runs/stage_a/、独立 smoke workspace |
| AC02 | 真实失败、工具修复和重跑成功 | 命令 stderr、文件 diff、acceptance |
| AC03 | 不含视频源码的切换提交 | stage-a-ready 提交和阶段记录 |
| AC04 | Coding Agent 自主开发视频业务 | runs/build_video/ 中工具事件和源码 diff |
| AC05 | 素材真实属性和内容证据 | assets.json、analysis.json、帧引用 |
| AC06 | v1 时间线、成片和验证 | timeline_v1/output_v1/validation_v1 |
| AC07 | v2 继承约束并体现修改 | requirements_v2、timeline_v2、revision_diff |
| AC08 | 视频处理失败后恢复 | 原始错误、修复参数与成功复验 |
| AC09 | 全链路可追溯 | event_id、request_id、tool_call_id、哈希与 manifest |
| AC10 | 两 CLI 可复现启动与交付齐全 | README、锁定依赖、配置与交付清单 |
| AC11 | 用量、费用和时间可复算 | 原始 usage、定价来源或未知声明、阶段时间 |
| AC12 | 180 分钟与一页说明 | 实际完整时间记录和一页报告 |

模型必须 deepseek-flash、reasoning high，无替代模型。API 暂时错误最多额外重试两次；每版渲染最多修复两轮。达到时间/轮数预算时保存失败与未完成项。SDK 重试关闭，以统一层为准。模型文字不能替代真实执行。

日志脱敏，保留需求、调用、工具返回、退出码、耗时、diff 和哈希。成本单价未核实或用量缺失时记 null/未知，不能记零。大体积媒体保留本地，通过 manifest 哈希关联；源码、需求、时间线、文本证据提交 Git。

尚未获得考核方确认的 U01–U06（正式评分细则、阶段人工边界、指定服务/硬件、素材上限、精度、计时计费口径）作为待确认事项，不能用本演示替代正式考核验收。
