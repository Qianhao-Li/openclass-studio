import type { AgentActivityEvent } from "../types";

const LEGACY_ACTIVITY_COPY: Record<string, string> = {
  "OpenClass 工作进展": "OpenClass progress",
  "OpenClass 正在思考": "OpenClass is thinking",
  "制定工作计划": "Preparing work plan",
  "运行命令": "Running command",
  "更新板书文档": "Updating board document",
  "调用工具": "Using tool",
  "协同处理": "Collaborating",
  "搜索网络": "Searching the web",
  "查看图片": "Viewing image",
  "生成图片": "Generating image",
  "整理上下文": "Organizing context",
  "OpenClass 已完成思考": "OpenClass finished thinking",
  "工作计划已更新": "Work plan updated",
  "命令执行完成": "Command completed",
  "板书文档已更新": "Board document updated",
  "工具调用完成": "Tool call completed",
  "协同处理完成": "Collaboration completed",
  "网络搜索完成": "Web search completed",
  "图片查看完成": "Image viewed",
  "图片生成完成": "Image generated",
  "上下文整理完成": "Context organized",
  "OpenClass 已连接模型": "OpenClass connected to the model",
  "OpenClass 已完成模型运行": "OpenClass completed the model run",
  "OpenClass 正在推理": "OpenClass is reasoning",
  "OpenClass 已完成推理": "OpenClass completed reasoning",
  "OpenClass 正在生成结果": "OpenClass is generating the result",
  "OpenClass 已生成模型结果": "OpenClass generated the model result",
  "OpenClass 模型运行未完成": "OpenClass model run did not complete",
  "模型连接中断，正在重试": "Model connection interrupted; retrying",
  "OpenClass 正在校验模型结果": "OpenClass is validating the model result",
  "模型结果需要结构修复": "Model result needs structural repair",
  "OpenClass 已校验模型结果": "OpenClass validated the model result",
  "OpenClass 正在处理当前步骤": "OpenClass is processing the current step",
  "OpenClass 已完成当前步骤": "OpenClass completed the current step",
  "OpenClass 当前步骤未完成": "OpenClass did not complete the current step",
  "用户已停止当前模型请求。": "The user stopped the current model request.",
  "模型进程返回失败状态。": "The model process returned a failure status.",
  "模型没有返回可用的助手结果。": "The model did not return a usable assistant result.",
  "首次结果未满足结构要求，正在请求模型修复。":
    "The first result did not match the required structure; requesting a repaired result.",
  "模型结果已通过结构校验。": "The model result passed schema validation.",
  "教学任务已开始": "Teaching task started",
  "教学任务已完成": "Teaching task completed",
  "正在扫描文件页面": "Scanning source pages",
  "正在映射目录节点": "Mapping catalog entries",
  "正在验证章节范围": "Checking chapter ranges",
  "正在写入完整目录": "Saving the complete catalog",
  "资料 Agent 正在读取文件结构": "Source agent is reading the file structure",
  "资料 Agent 已写入目录，正在自检": "Source agent saved the catalog and is checking it",
  "资料 Agent 已完成文件结构读取": "Source agent finished reading the file structure",
  "资料 Agent 文件结构读取已取消": "Source structure reading was cancelled",
  "资料 Agent 文件结构读取失败": "Source structure reading failed",
  "资料 Agent 任务已中断": "Source agent task was interrupted",
  "资料 Agent 任务已中断。原始文件和已有目录已保留，可点击刷新重新运行。":
    "The source agent task was interrupted. The original file and existing catalog were kept. Refresh to try again.",
  "资料解析已取消": "Source processing was cancelled",
  "用户停止了资料解析。": "The user stopped source processing.",
  "用户停止了资料解析；原始文件和已有目录记录均已保留。":
    "The user stopped source processing. The original file and existing catalog were kept.",
  "资料目录重试已取消；原件和已有目录已保留。":
    "Catalog retry was cancelled. The original file and existing catalog were kept.",
  "正在检查文件页数、原生目录和可用导航结构": "Checking page count, embedded contents, and navigation",
  "目录已通过校验并保存，可以展开和引用章节": "Catalog saved. Expand it to view and cite chapters.",
  "正在完成目录页码映射": "Mapping catalog page numbers",
  "确认目录边界": "Checking catalog boundaries",
  "标定 P": "Calibrating page offsets",
  "原生书签物理目标页": "Physical pages from embedded bookmarks",
  "生成正文范围": "Mapping content ranges",
  "验证索引": "Checking the index",
  "目录由原生正文索引生成。": "Catalog generated from the document's native index.",
  "已保留原生目录，所选 AI 仅补充缺失目录。": "Embedded contents were kept. The selected AI fills in missing entries.",
  "用户刷新目录；由所选 AI 直接读取源文件重新生成。": "The selected AI is reading the original file to rebuild its catalog.",
  "无可用原生正文索引；目录由所选 AI 生成。": "No native content index is available. The selected AI generates the catalog.",
  "目录结构已识别；正文范围尚未映射。": "Catalog structure identified. Content ranges have not been mapped yet.",
  "目录仅保存结构与范围，正文将在引用章节时按需读取。": "The catalog stores structure and ranges. Content is read when a chapter is cited.",
  "主动检索资料已关闭。": "Automatic source search is off.",
  "Google 检索已关闭，请手动引用资料章节读取原件。": "Google search is off. Select a chapter to read the original source.",
  "当前课程没有可用的 Google 索引资料。": "This course has no sources ready for Google search.",
  "当前课程没有已完成索引的资料。": "This course has no indexed sources yet.",
  "资料页面读取已取消。": "Source page reading was cancelled.",
  "资料目录生成已取消。": "Source catalog generation was cancelled.",
  "当前课程正在生成回复，请等待完成后再撤回或重做板书": "Wait for the reply to finish before undoing or redoing board changes.",
  "请先完成或放弃当前合并草稿，再撤回或重做板书": "Complete or discard the merge draft before undoing or redoing board changes.",
  "归档分支不能撤回或重做板书": "Board changes cannot be undone or redone on an archived branch.",
  "请先切换到这个分支，再撤回或重做板书": "Switch to this branch before undoing or redoing board changes.",
  "当前课程正在生成回复，请等待完成后再编辑历史": "Wait for the reply to finish before editing History.",
  "请先完成或放弃当前合并草稿，再编辑历史": "Complete or discard the merge draft before editing History.",
  "History 编辑已经结束": "History editing has ended.",
  "编辑草稿已在其他页面更新，请重新载入草稿": "This draft changed in another tab. Reload the draft.",
  "课程已经变化，编辑草稿已保留；请检查后放弃草稿并重新开始": "The course changed. Your draft was kept; review and discard it before starting again.",
  "课程已变化，请刷新后重新进入编辑模式": "The course changed. Refresh before entering edit mode again.",
  "请先进入编辑模式": "Enter edit mode first.",
  "编辑草稿已经变化，请重新载入": "The draft changed. Reload it before continuing.",
  "请选择要检查的历史节点": "Select a history node to inspect.",
  "当前课程有待确认的 History 更改，请先审阅并保留或放弃更改，再继续写入课程": "Review and keep or discard pending History changes before editing the course.",
  "课程已变化，请刷新后重新撤回或重做": "The course changed. Refresh before undoing or redoing.",
  "Repository 已变化，请刷新后重新操作": "The repository changed. Refresh before trying again.",
  "当前分支已变化，请刷新后重新操作": "The current branch changed. Refresh before trying again.",
  "只能撤回最近生效的板书动作，或按顺序重做下一个已撤回动作": "Undo the latest active board change, or redo the next withdrawn change in order.",
  "请先恢复所需的修改范围": "Restore the required changes first.",
  "课程已变化": "The course changed.",
};

function legacySourceProgressText(text: string): string | undefined {
  let match = text.match(/^AI 正在逐页看图：已确认 (\d+)\/(\d+) 页$/);
  if (match) return `AI is reading page images: ${match[1]}/${match[2]} pages checked`;
  match = text.match(/^原页图像 (\d+) 页 · AI 已确认读取 (\d+) 页 · 未使用 OCR$/);
  if (match) return `Original images: ${match[1]} pages · AI checked: ${match[2]} pages · No OCR used`;
  match = text.match(/^正在扫描并核对 PDF：(\d+)\/(\d+) 页$/);
  if (match) return `Scanning and checking PDF pages: ${match[1]}/${match[2]}`;
  match = text.match(/^资料 Agent 正在调查文件：已完成 (\d+) 次工具检查$/);
  if (match) return `Source agent is inspecting the file: ${match[1]} tool checks completed`;
  match = text.match(/^目录版本 (\d+) 已可用，正文定位继续中$/);
  if (match) return `Catalog version ${match[1]} is available. Content mapping is continuing.`;
  match = text.match(/^目录版本 (\d+) 已完成$/);
  if (match) return `Catalog version ${match[1]} is complete`;
  match = text.match(/^(?:正文范围已映射 |已映射 )(\d+)\/(\d+)(?: 个节点)?，剩余 (\d+) 个(?:节点)?$/);
  if (match) return `Mapped ${match[1]}/${match[2]} content ranges; ${match[3]} remaining`;
  match = text.match(/^目录状态 ([\w-]+) · (\d+) 个节点$/);
  if (match) return `Catalog status: ${match[1]} · ${match[2]} entries`;
  match = text.match(/^索引状态 ([\w-]+) · 最终工作状态 ([\w-]+)$/);
  if (match) return `Index status: ${match[1]} · Task status: ${match[2]}`;
  const parts = text.split(" · ");
  const translated = parts.map((part) => {
    let segment = part.match(/^已扫描 (\d+)\/(\d+) 页$/);
    if (segment) return `Scanned ${segment[1]}/${segment[2]} pages`;
    segment = part.match(/^文件共 (\d+) 页$/);
    if (segment) return `${segment[1]} pages in the file`;
    segment = part.match(/^已渲染核对 (\d+) 页$/);
    if (segment) return `${segment[1]} rendered pages checked`;
    segment = part.match(/^已完成 (\d+) 次工具检查$/);
    if (segment) return `${segment[1]} tool checks completed`;
    segment = part.match(/^(?:正文范围 |已映射 )(\d+)\/(\d+)(?: 个节点)?$/);
    if (segment) return `Mapped content ranges: ${segment[1]}/${segment[2]}`;
    segment = part.match(/^(\d+) 个精确 P 区段$/);
    if (segment) return `${segment[1]} verified page-offset ranges`;
    return part;
  });
  return translated.some((part, index) => part !== parts[index]) ? translated.join(" · ") : undefined;
}

export function publicAgentActivityText(text: string): string {
  const branded = text.replace(/^(?:Codex|OpenAI)\b/i, "OpenClass");
  const fixedCopy = LEGACY_ACTIVITY_COPY[branded];
  if (fixedCopy) {
    return fixedCopy;
  }
  const sourceProgress = legacySourceProgressText(branded);
  if (sourceProgress) return sourceProgress;

  let historyMatch = branded.match(/^请先撤回依赖此修改范围的动作：(.+)$/);
  if (historyMatch) return `Undo the dependent actions first: ${historyMatch[1].replaceAll("、", ", ")}`;
  historyMatch = branded.match(/^请先恢复所需的修改范围：(.+)$/);
  if (historyMatch) return `Restore the required changes first: ${historyMatch[1].replaceAll("、", ", ")}`;

  let match = branded.match(/^正在使用 (.+) 处理当前步骤。$/);
  if (match) {
    return `Using ${match[1]} to process the current step.`;
  }
  match = branded.match(/^(.+) 已返回本步骤结果。$/);
  if (match) {
    return `${match[1]} returned the result for this step.`;
  }
  match = branded.match(/^模型(推理|结果)阶段已完成(?:，共接收 (\d+) 个字符)?。$/);
  if (match) {
    const noun = match[1] === "推理" ? "reasoning" : "output";
    return `Model ${noun} completed${match[2] ? `; received ${match[2]} characters` : ""}.`;
  }
  match = branded.match(/^模型正在生成(推理|结果)(?:，已接收 (\d+) 个字符)?(；逐字私有思维不写入聊天记录)?。$/);
  if (match) {
    const noun = match[1] === "推理" ? "reasoning" : "output";
    const characterCount = match[2] ? `; received ${match[2]} characters` : "";
    const privacyNote = match[3] ? "; private chain-of-thought is not recorded in chat history" : "";
    return `The model is generating ${noun}${characterCount}${privacyNote}.`;
  }
  match = branded.match(/^模型请求在 (.+) 秒后超时。$/);
  if (match) {
    return `The model request timed out after ${match[1]} seconds.`;
  }
  match = branded.match(/^正在进行第 (\d+) 次模型请求。$/);
  if (match) {
    return `Starting model request attempt ${match[1]}.`;
  }
  match = branded.match(/^正在按照 (.+) 的结构要求检查结果。$/);
  if (match) {
    return `Checking the result against the ${match[1]} schema.`;
  }
  return branded;
}

export function publicAgentActivityLabel(label: string): string {
  return publicAgentActivityText(label);
}

function activityLifecycleKey(event: AgentActivityEvent): string {
  const kind = typeof event.metadata.kind === "string" ? event.metadata.kind : "";
  const teachingTaskId =
    typeof event.metadata.teaching_task_id === "string" ? event.metadata.teaching_task_id : "";
  if (kind === "teaching_task" && teachingTaskId) {
    return `teaching_task:${teachingTaskId}`;
  }
  return `event:${event.id}`;
}

export function mergeAgentActivityEvent(
  events: AgentActivityEvent[],
  nextEvent: AgentActivityEvent
): AgentActivityEvent[] {
  const nextKey = activityLifecycleKey(nextEvent);
  const existingIndex = events.findIndex((event) => activityLifecycleKey(event) === nextKey);
  if (existingIndex < 0) {
    return [...events, nextEvent];
  }
  return events.map((event, index) => (index === existingIndex ? nextEvent : event));
}

export function normalizeAgentActivityEvents(events: AgentActivityEvent[]): AgentActivityEvent[] {
  return events.reduce<AgentActivityEvent[]>(mergeAgentActivityEvent, []);
}
