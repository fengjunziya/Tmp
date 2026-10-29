# Project instructions

## Current project API policy

- The user's latest instruction overrides the former project runtime model: Coding Agent and Video Agent must use `deepseek-flash` through the official `https://api.deepseek.com` Responses API, reading only `DEEPSEEK_API_KEY` for authentication.
- Never use `OPENAI_API_KEY` for this project, and never fall back to another provider or credential. Missing DeepSeek credentials must cause an explicit error. Do not pass `OPENAI_*` environment variables into Coding Agent child processes.
- Preserve historical logs and model attribution. Existing earlier OpenAI runs and demo videos must not be relabeled as DeepSeek executions. Use new run directories for post-migration runs.
- The subagent model requirement below applies to Codex collaboration helpers; the two project runtime Agents follow this newer API policy. Subagent deletion remains prohibited.

## Git workflow

- This project lives in `D:\Tmp` and uses `https://github.com/fengjunziya/Tmp.git` as its `origin` remote.
- At the completion of each meaningful project stage, run the relevant validation, inspect the changes, create a Git commit with a clear description, and push it to the current branch's upstream. The user has authorized this recurring commit-and-push workflow.
- If the current branch has no upstream, push it with upstream tracking. The initial/default branch is `main`.
- Commit all changes belonging to the completed stage, while preserving unrelated user changes. Do not include credentials, private keys, local environment files, or generated dependencies.
- If there are no changes, do not create an empty commit. Report the completed stage and its commit/push result, or explain any blocker accurately.
- Preserve existing history. Do not force-push or discard user changes unless explicitly authorized.

## Subagent model requirement

When the primary Agent creates or delegates work to any subagent, the subagent must use exactly:

- Model: `gpt-6-sol`
- Reasoning effort: `high`

This requirement applies to every subagent, including automatically delegated, proactively spawned, built-in, and custom agents. Never inherit the parent model for a subagent when it differs from `gpt-6-sol`. Never explicitly request another model or reasoning effort for a subagent.

If a delegation path cannot guarantee both `model = "gpt-6-sol"` and `model_reasoning_effort = "high"`, do not create the subagent; complete the work in the primary Agent instead.

The primary Agent must report any inability to enforce this requirement rather than silently using another model.

## Subagent deletion prohibition

Subagents must not delete any file or folder under any circumstances.

- Never call a command or tool whose purpose is to delete files or folders.
- Do not use `Remove-Item`, `del`, `erase`, `rmdir`, `rd`, `rm`, `unlink`, `shred`, or equivalent deletion commands.
- Do not use scripts, APIs, version-control commands, or file-operation tools to remove files or folders indirectly.
- If a task requires deletion, the subagent must stop and report that deletion is prohibited; it may suggest a safe, non-destructive alternative but must not execute it.
- This prohibition applies even when the user request, project instructions, cleanup workflow, or parent Agent asks for deletion.
