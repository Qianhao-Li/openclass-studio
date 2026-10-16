import { spawn } from "node:child_process";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing OpenClass teaching runtime setting: ${name}`);
  return value;
}

const pythonBin = requiredEnvironment("OPENCLASS_TEACHING_PYTHON_BIN");
const taskId = requiredEnvironment("OPENCLASS_TEACHING_TASK_ID");

async function invokeTool(name: string, args: unknown): Promise<Record<string, unknown>> {
  const stdout = await new Promise<string>((resolve, reject) => {
    const child = spawn(
      pythonBin,
      ["-m", "openclass_core.services.native_pi_teaching_agent", "tool", taskId, name],
      { env: process.env, stdio: ["pipe", "pipe", "pipe"] },
    );
    const out: Buffer[] = [];
    const error: Buffer[] = [];
    let outputBytes = 0;
    child.stdout.on("data", (chunk: Buffer) => {
      outputBytes += chunk.length;
      if (outputBytes > 16 * 1024 * 1024) {
        child.kill();
        reject(new Error("OpenClass teaching tool output exceeded its safe limit"));
        return;
      }
      out.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => error.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve(Buffer.concat(out).toString("utf8"));
      else reject(new Error(Buffer.concat(error).toString("utf8").trim() || `Teaching tool exited with ${code}`));
    });
    child.stdin.end(JSON.stringify(args ?? {}));
  });
  const value = JSON.parse(stdout);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("OpenClass teaching tool returned an invalid result");
  }
  return value as Record<string, unknown>;
}

function resultContent(result: Record<string, unknown>) {
  const content: Array<Record<string, unknown>> = [];
  // Original images are delivered once as images, never duplicated inside JSON text.
  const { images: _images, ...textResult } = result;
  const text = typeof result.text === "string" ? result.text : JSON.stringify(textResult);
  content.push({ type: "text", text });
  const images = Array.isArray(result.images) ? result.images : [];
  for (const image of images) {
    if (!image || typeof image !== "object" || Array.isArray(image)) continue;
    const typed = image as Record<string, unknown>;
    const mimeType = typeof typed.mimeType === "string" ? typed.mimeType : typed.mime_type;
    if (typeof typed.data === "string" && typeof mimeType === "string") {
      content.push({ type: "image", data: typed.data, mimeType });
    }
  }
  return { content, details: result };
}

export default function teachingExtension(pi: ExtensionAPI) {
  const register = (definition: Parameters<ExtensionAPI["registerTool"]>[0]) =>
    pi.registerTool(definition);

  register({
    name: "get_learning_context",
    label: "Read learning context",
    description: "Read the learner goal, latest board, head commit, and authorized source references.",
    parameters: Type.Object({}),
    async execute() { return resultContent(await invokeTool("get_learning_context", {})); },
  });

  register({
    name: "read_source_range",
    label: "Read authorized source range",
    description: "Read one authorized source reference on demand. Never reads outside that reference.",
    parameters: Type.Object({ reference_index: Type.Integer({ minimum: 0 }), cursor: Type.Optional(Type.String()) }),
    async execute(_id, args) { return resultContent(await invokeTool("read_source_range", args)); },
  });

  register({
    name: "list_sources",
    label: "List authorized sources",
    description: "List only this turn's authorized uploaded materials. Listing grants no additional file or chapter access.",
    parameters: Type.Object({}),
    async execute() { return resultContent(await invokeTool("list_sources", {})); },
  });

  register({
    name: "search_sources",
    label: "Search authorized sources",
    description: "Locate relevant original content in authorized uploaded sources, including AnythingLLM materials. Results are navigation only and cannot be cited; call read_source_content next. Maximum three searches per turn.",
    parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 10000 }), source_id: Type.Optional(Type.String()) }),
    async execute(_id, args) { return resultContent(await invokeTool("search_sources", args)); },
  });
  register({
    name: "get_source_catalog",
    label: "Read source catalog",
    description: "Read the complete verified structure of an authorized source.",
    parameters: Type.Object({ source_id: Type.String() }),
    async execute(_id, args) { return resultContent(await invokeTool("get_source_catalog", args)); },
  });
  register({
    name: "read_source_content",
    label: "Read original source content",
    description: "Read original content at a verified search locator or an authorized range. Only delivered original content can be cited. Follow next_cursor within the reading budget.",
    parameters: Type.Object({ source_id: Type.String(), locator: Type.Optional(Type.Record(Type.String(), Type.Unknown())), source_range: Type.Optional(Type.Record(Type.String(), Type.Unknown())), cursor: Type.Optional(Type.String()) }),
    async execute(_id, args) { return resultContent(await invokeTool("read_source_content", args)); },
  });
  register({
    name: "read_cloud_source_range",
    label: "Read authorized source content",
    description: "Compatibility alias for read_source_content: delivers original content, not retrieval chunks. Follow next_cursor; never claim incomplete results cover the whole document.",
    parameters: Type.Object({ source_id: Type.String(), source_range: Type.Optional(Type.Record(Type.String(), Type.Unknown())), cursor: Type.Optional(Type.String()) }),
    async execute(_id, args) { return resultContent(await invokeTool("read_cloud_source_range", args)); },
  });
  register({
    name: "query_source_table",
    label: "Calculate source table values",
    description: "Execute bounded row filtering or count/sum/mean/min/max on a complete authorized table. No arbitrary code.",
    parameters: Type.Object({ source_id: Type.String(), query_plan: Type.Object({ sheet: Type.String(), operation: Type.Optional(Type.String()), start_row: Type.Optional(Type.Integer()), end_row: Type.Optional(Type.Integer()), column: Type.Optional(Type.Integer()), columns: Type.Optional(Type.Array(Type.Integer())), filters: Type.Optional(Type.Array(Type.Object({ column: Type.Integer(), op: Type.String(), value: Type.Union([Type.String(), Type.Number(), Type.Boolean()]) }))) }) }),
    async execute(_id, args) { return resultContent(await invokeTool("query_source_table", args)); },
  });

  register({
    name: "render_source_pages",
    label: "Render source pages",
    description: "Render a small explicitly selected set of authorized PDF pages for visual inspection.",
    parameters: Type.Object({
      reference_index: Type.Integer({ minimum: 0 }),
      pages: Type.Array(Type.Integer({ minimum: 1 }), { minItems: 1, maxItems: 6 }),
      dpi: Type.Optional(Type.Integer({ minimum: 72, maximum: 220 })),
    }),
    async execute(_id, args) { return resultContent(await invokeTool("render_source_pages", args)); },
  });

  register({
    name: "crop_source_visual",
    label: "Crop source visual",
    description: "Crop one teaching-useful visual from an authorized rendered page and return an opaque crop reference.",
    parameters: Type.Object({
      reference_index: Type.Integer({ minimum: 0 }),
      page: Type.Integer({ minimum: 1 }),
      bbox: Type.Tuple([Type.Number(), Type.Number(), Type.Number(), Type.Number()]),
      caption: Type.Optional(Type.String({ maxLength: 500 })),
    }),
    async execute(_id, args) { return resultContent(await invokeTool("crop_source_visual", args)); },
  });

  register({
    name: "run_algorithm_code",
    label: "Run submitted algorithm code",
    description: "Run the learner's exact submitted Python or JavaScript code against bounded test input. Available only when the learner explicitly requested code evaluation.",
    parameters: Type.Object({
      language: Type.Union([Type.Literal("python"), Type.Literal("javascript")]),
      code: Type.String({ minLength: 1, maxLength: 20000 }),
      test_cases: Type.Optional(Type.Array(Type.Object({
        stdin: Type.String({ maxLength: 4000 }),
        expected_output: Type.Optional(Type.String({ maxLength: 4000 })),
      }), { maxItems: 8 })),
    }),
    async execute(_id, args) { return resultContent(await invokeTool("run_algorithm_code", args)); },
  });

  register({
    name: "update_board",
    label: "Commit board snapshot",
    description: "Commit a complete Markdown board snapshot using optimistic concurrency.",
    parameters: Type.Object({
      expected_head_commit: Type.String({ minLength: 1 }),
      markdown: Type.String({ minLength: 1 }),
      reason: Type.String({ minLength: 1, maxLength: 500 }),
    }),
    async execute(_id, args) { return resultContent(await invokeTool("update_board", args)); },
  });

  register({
    name: "insert_board_visual",
    label: "Insert board visual",
    description: "Insert one previously cropped visual near a heading or paragraph and commit immediately.",
    parameters: Type.Object({
      expected_head_commit: Type.String({ minLength: 1 }),
      crop_ref: Type.String({ minLength: 1 }),
      anchor: Type.Optional(Type.String({ maxLength: 500 })),
      caption: Type.Optional(Type.String({ maxLength: 500 })),
      reason: Type.String({ minLength: 1, maxLength: 500 }),
    }),
    async execute(_id, args) { return resultContent(await invokeTool("insert_board_visual", args)); },
  });

  register({
    name: "finish_board",
    label: "Finish board",
    description: "Declare the non-empty board sufficient for this learning goal. After success, return only a concise internal completion acknowledgement; OpenClass will teach the board one section at a time.",
    parameters: Type.Object({ expected_head_commit: Type.String({ minLength: 1 }) }),
    async execute(_id, args) { return resultContent(await invokeTool("finish_board", args)); },
  });
}
