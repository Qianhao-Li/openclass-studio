import { mkdir, rename, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { resolve, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const base = process.env.OPENCLASS_PI_REPOSITORY_BROKER_URL ?? "";
const token = process.env.OPENCLASS_PI_REPOSITORY_BROKER_TOKEN ?? "";
const parsed = new URL(base);
if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || !parsed.port || token.length < 32) {
  throw new Error("Invalid single-file catalog broker");
}
const scratch = resolve(process.cwd(), process.env.OPENCLASS_PI_SOURCE_SCRATCH ?? "scratch");
if (scratch !== join(process.cwd(), "scratch")) throw new Error("Invalid catalog scratch directory");

export default function (pi: ExtensionAPI) {
  const directoryNode = Type.Object({key:Type.String({minLength:1}),title:Type.String({minLength:1}),
    level:Type.Optional(Type.Integer({minimum:1})),printed_page_label:Type.Optional(Type.String()),
    source_range:Type.Optional(Type.Any()),read_ids:Type.Optional(Type.Array(Type.String())),
    range_complete:Type.Optional(Type.Boolean()),
    directory_printed_page_label:Type.Optional(Type.String()),page_label_reviewed:Type.Optional(Type.Boolean()),
    directory_read_ids:Type.Optional(Type.Array(Type.String()))});
  const bodyAnchor = Type.Object({node_key:Type.String(),observed_label:Type.String(),printed_page:Type.Integer({minimum:1}),
    pdf_file_page:Type.Integer({minimum:1}),starts_at_page_top:Type.Boolean(),read_ids:Type.Array(Type.String(),{minItems:1})});
  const region = Type.Array(Type.Number({minimum:0,maximum:1}),{minItems:4,maxItems:4});
  const directoryHeading = Type.Object({title:Type.String({minLength:1}),level:Type.Integer({minimum:1}),
    ownership_point:Type.Array(Type.Number({minimum:0,maximum:1}),{minItems:2,maxItems:2})},{additionalProperties:false});
  const fragmentNode = Type.Object({...directoryNode.properties,
    ownership_point:Type.Array(Type.Number({minimum:0,maximum:1}),{minItems:2,maxItems:2}),
    continues_key:Type.Optional(Type.String({minLength:1}))});
  const stopReasons = new Set(["budget_exhausted","call_limit","step_timeout","cancelled","interrupted","version_changed","failed"]);
  const stopProcess = (reason: string): never => {
    // Pi reports and swallows ordinary extension-hook exceptions. Exiting this
    // isolated source process is necessary to prevent a denied provider call.
    process.stderr.write("Pi catalog refinement stopped: " + (stopReasons.has(reason) ? reason : "failed") + "\n");
    process.exit(75);
  };
  const reserveProviderRequest = async () => {
    let reason = "failed";
    try {
      const response = await fetch(base + "/provider-request", {method:"POST",
        headers:{"Content-Type":"application/json",Authorization:"Bearer " + token},body:"{}",signal:AbortSignal.timeout(5000)});
      const budget = await response.json() as {reason?:string;result?:{approved?:boolean}};
      if (response.ok && budget.result?.approved === true) return;
      if (budget.reason && stopReasons.has(budget.reason)) reason = budget.reason;
    } catch { /* An unreachable budget broker does not authorize model access. */ }
    stopProcess(reason);
  };
  let requestLimit = 0;
  pi.on("before_provider_request", async event => {
    await reserveProviderRequest();
    if (!requestLimit) return;
    try {
      const payload = structuredClone(event.payload) as Record<string,unknown>;
      const size = () => Buffer.byteLength(JSON.stringify(payload));
      const omission = "An image from an earlier read was omitted from this request to fit transfer capacity. Its original bytes were not resized. Read the original again if needed; retained read receipts and directory checkpoints remain available.";
      const evictHistory = (items: Array<Record<string,unknown>>, responses: boolean) => {
        // Responses puts images in both user content and tool output arrays.
        // Preserve every result after the newest assistant action, including
        // parallel tool results. Without an assistant, preserve the last input.
        let boundary = Math.max(0,items.length-1);
        for (let index=items.length-1; index>=0; index--) {
          const item=items[index];
          if (item && (item.role === "assistant" || (responses &&
            ["function_call","custom_tool_call","reasoning"].includes(String(item.type))))) {
            boundary=index; break;
          }
        }
        for (let index=0; index<boundary && size()>requestLimit; index++) {
          const item=items[index];
          if (!item || typeof item !== "object") continue;
          for (const field of responses ? ["content","output"] : ["content"]) {
            if (!Array.isArray(item[field])) continue;
            item[field]=item[field].map((part: {type?:string}) => {
              if (size()>requestLimit && part && typeof part === "object" &&
                ["image","image_url","input_image"].includes(part.type ?? "")) {
                return {type:responses?"input_text":"text",text:omission};
              }
              return part;
            });
          }
        }
      };
      if (payload && typeof payload === "object") {
        if (Array.isArray(payload.messages)) evictHistory(payload.messages,false);
        if (Array.isArray(payload.input)) evictHistory(payload.input,true);
      }
      // An unfamiliar payload shape must still obey the complete-request limit.
      if (size()>requestLimit) stopProcess("failed");
      return payload;
    } catch { stopProcess("failed"); }
  });
  const definitions = [
    ["source_info", "Get the sole original's identity, TOC location clues, catalog and reading checkpoints.", Type.Object({})],
    ["get_source_catalog", "Page through the current directory and positioned original units.", Type.Object({start_index:Type.Optional(Type.Integer({minimum:0})),limit:Type.Optional(Type.Integer({minimum:1,maximum:100}))})],
    ["search_sources", "Locate clues within this file; uses its Google enhanced index when ready. Results are navigation only.", Type.Object({query:Type.String({minLength:1}),limit:Type.Optional(Type.Integer({minimum:1,maximum:8}))})],
    ["read_source_content", "Read verified original content/images at a location or range. Continue next_cursor and widen nearby ranges as needed.", Type.Object({source_range:Type.Optional(Type.Any()),locator:Type.Optional(Type.Any()),cursor:Type.Optional(Type.String())})],
    ["pdf_text", "Read original text on a physical PDF page interval; scanned pages return original images.", Type.Object({first_page:Type.Integer({minimum:1}),last_page:Type.Integer({minimum:1}),cursor:Type.Optional(Type.String())})],
    ["pdf_page_image", "Inspect one original physical PDF page. Optional normalized region [left,top,right,bottom] adds a detail view while retaining the whole original image and its receipt; no local OCR.", Type.Object({page:Type.Integer({minimum:1}),region:Type.Optional(Type.Array(Type.Number({minimum:0,maximum:1}),{minItems:4,maxItems:4}))})],
    ["plan_directory_transcription", "Plan every original TOC unit in authored order, one physical PDF page per unit. Confirm continuation discovery before discovery_complete. Completed units survive revised plans.", Type.Object({source_ranges:Type.Array(Type.Any()),discovery_complete:Type.Boolean()})],
    ["plan_directory_fragments", "After a whole original-page layout read, partition the entire page into 1-8 nonoverlapping ownership rectangles in reading order. Observation rectangles contain each ownership rectangle and may overlap for context. Include margins and continuation rows; use one full-page fragment for a simple page. Replanning requires the current expected_plan_revision. This saves a checkpoint; return immediately.", Type.Object({source_range:Type.Any(),read_ids:Type.Array(Type.String(),{minItems:1}),expected_plan_revision:Type.Optional(Type.Integer({minimum:0})),fragments:Type.Array(Type.Object({id:Type.String({minLength:1,maxLength:128}),ownership_region:region,observation_region:region}),{minItems:1,maxItems:8})})],
    ["record_directory_headings", "During heading_inventory, read the whole original page independently of any catalog or draft. Record every prominent section and chapter parent heading in original reading order, including colored headings and headings without page numbers. Give its literal title, absolute hierarchy level and normalized original starting point. Use headings=[] only if the actual page has no such headings. Supply the current plan_revision, merged_revision and fresh whole-page read_ids. This records observations without writing catalog rows; return immediately after the checkpoint.", Type.Object({source_range:Type.Any(),plan_revision:Type.Integer({minimum:1}),merged_revision:Type.Integer({minimum:1}),read_ids:Type.Array(Type.String(),{minItems:1}),headings:Type.Array(directoryHeading)})],
    ["record_directory_heading_literal", "During review_directory_heading_line, read only the planned exact original PDF detail region. Independently copy that heading's visible literal wording including its section number, excluding dot leaders and right-side page labels. Do not reconstruct a familiar title or consult an older title. Supply this host line_id and inventory_fingerprint with the fresh detail read_ids; save the single observation and return immediately. The host alone updates the matching heading constraint and reopens its owner fragment when needed.", Type.Object({line_id:Type.String({minLength:1,maxLength:128}),inventory_fingerprint:Type.String({minLength:64,maxLength:64}),read_ids:Type.Array(Type.String(),{minItems:1}),literal_title:Type.String({minLength:1,maxLength:600})})],
    ["transcribe_directory_fragment", "Read this fragment's observation_region with pdf_page_image region, then upsert every literal TOC row owned by its ownership_region. ownership_point is the row's original normalized starting position. Preserve absolute hierarchy and stable keys; equal titles are distinct rows. A cross-fragment continuation must use the prior row key as both key and continues_key, with the full literal continued title and unchanged level. remove_keys deletes only this fragment's mistaken rows. When inserting a missing row or correcting order, supply ordered_keys listing EVERY retained key once in original order; upserts alone retain old positions. Never set source_range, range_complete or chapter read_ids. Honor required_headings supplied by the host at their observed positions and hierarchy levels. complete covers this fragment only; return after the saved checkpoint.", Type.Object({source_range:Type.Any(),fragment_id:Type.String(),plan_revision:Type.Integer({minimum:1}),read_ids:Type.Array(Type.String(),{minItems:1}),nodes:Type.Array(fragmentNode),complete:Type.Boolean(),remove_keys:Type.Optional(Type.Array(Type.String())),ordered_keys:Type.Optional(Type.Array(Type.String()))})],
    ["verify_directory_page", "If work.inspection_mode is heading_inventory, first use record_directory_headings without reading old drafts. Only after every fragment is complete, reread the WHOLE original page without a region and compare the latest merged_revision against every original row, order, printed label and hierarchy. Never reuse an earlier or cropped read. verified=true commits the old-compatible whole-page unit after this fresh check. Compare only the latest page rows and preceding-page ancestors against this actual page; old global catalogs are not completeness requirements. verified=false reopens retry_fragment_ids (all by default) for correction. Return immediately after the checkpoint.", Type.Object({source_range:Type.Any(),plan_revision:Type.Integer({minimum:1}),merged_revision:Type.Integer({minimum:1}),read_ids:Type.Array(Type.String(),{minItems:1}),verified:Type.Boolean(),retry_fragment_ids:Type.Optional(Type.Array(Type.String()))})],
    ["confirm_directory_gap", "Classify one pending page omitted inside the original TOC span after actual reading. A continuation joins the transcription plan; a blank or body page may be excluded with current read proof. Explain its observed shape without quoting the body.", Type.Object({source_range:Type.Any(),read_ids:Type.Array(Type.String(),{minItems:1}),is_directory:Type.Boolean(),reason:Type.String({minLength:1,maxLength:300})})],
    ["transcribe_directory_unit", "Save ALL authored rows of exactly one actually read TOC unit, including nested rows and both columns. Original title, order, absolute hierarchy level and printed_page_label are required. Repeated headers are context, not rows. Complete means this entire unit was transcribed. For a pending reconciliation, inspect displaced old row clues against a NEW original read, allowing corrections or intentional removals, then set reconciliation_complete=true. The published directory remains while other units are pending.", Type.Object({source_range:Type.Any(),read_ids:Type.Array(Type.String(),{minItems:1}),nodes:Type.Array(directoryNode,{minItems:1}),complete:Type.Boolean(),reconciliation_complete:Type.Optional(Type.Boolean())})],
    ["confirm_directory_unit", "During review_seeded_directory_unit, compare every saved row with one fresh FULL original page. Only when wording, hierarchy, order, printed labels and completeness match, confirm unchanged=true with that page's current read_ids. This reuses the existing rows without rewriting them. If anything differs, use transcribe_directory_unit to correct the entire page. Return the saved checkpoint immediately.", Type.Object({source_range:Type.Any(),read_ids:Type.Array(Type.String(),{minItems:1}),unchanged:Type.Literal(true)})],
    ["set_pdf_pagination", "Calibrate PDF file pages against printed numbers using three widely separated original body pages. Observe their headings and printed footer labels, attach actual read_ids, exclude TOC pages. P = pdf_file_page - printed_page + 1. Multiple numbering regimes are supported.", Type.Object({regime:Type.Object({id:Type.String(),printed_page_start:Type.Integer({minimum:1}),printed_page_end:Type.Integer({minimum:1}),page_offset_p:Type.Integer(),anchors:Type.Array(bodyAnchor,{minItems:3,maxItems:16})})})],
    ["confirm_pdf_heading", "Confirm one authored row's heading and printed number on an actually read original body page. starts_at_page_top records what is visible; ordinary page-level chapter references may share a physical page.", Type.Object({anchor:bodyAnchor})],
    ["record_pdf_chapter_boundaries", "During verify_chapter_scope, independently read the requested FULL original page and observe exactly its supplied boundary keys. A heading y is the top of its unique actual title; the next section starts there and is excluded from the current chapter. full_width_bands allows internal columns only when horizontal cuts faithfully separate the chapters; reject interleaved columns or crossing sidebars. Use null/false for uncertain positions. Include the host plan_digest, pass_index and NEW whole-page read_ids. Never copy another pass's coordinates or submit arbitrary chapter crops. Save this observation and return immediately; the host alone compares the independent passes and grants a scope.", Type.Object({plan_digest:Type.String({minLength:1}),physical_page:Type.Integer({minimum:1}),pass_index:Type.Integer({minimum:1,maximum:2}),read_ids:Type.Array(Type.String(),{minItems:1}),layout:Type.Union([Type.Literal("full_width_bands"),Type.Literal("unsupported"),Type.Literal("uncertain")]),observations:Type.Array(Type.Object({key:Type.String({minLength:1}),heading_found:Type.Boolean(),unique:Type.Boolean(),y:Type.Union([Type.Number({minimum:0,maximum:1}),Type.Null()])},{additionalProperties:false}),{maxItems:256})})],
    ["catalog_apply", "Save a useful partial directory: upsert nodes, remove wrong keys, optionally specify preorder. replace_all starts a corrected directory from actual reads. A source_range is only a candidate unless range_complete=true confirms the entire chapter and actual read_ids prove boundaries. A read batch is not a chapter range.", Type.Object({nodes:Type.Optional(Type.Array(directoryNode)),replace_all:Type.Optional(Type.Boolean()),remove_keys:Type.Optional(Type.Array(Type.String())),ordered_keys:Type.Optional(Type.Array(Type.String())),origin:Type.Optional(Type.Union([Type.Literal("authored"),Type.Literal("ai_organized")])),directory_read_ids:Type.Optional(Type.Array(Type.String())),summary:Type.Optional(Type.String()),next_plan:Type.Optional(Type.String())})],
    ["write_catalog", "Publish the retained catalog and final artifact; mark complete only after actual directory coverage.", Type.Object({directory_complete:Type.Boolean(),directory_read_ids:Type.Optional(Type.Array(Type.String())),summary:Type.Optional(Type.String()),next_plan:Type.Optional(Type.String())})],
  ] as const;
  for (const [name, description, parameters] of definitions) {
    pi.registerTool({name,label:name,description,parameters,
      async execute(_id, args) {
        const response = await fetch(base + "/tool", {method:"POST",headers:{"Content-Type":"application/json",Authorization:"Bearer " + token},body:JSON.stringify({name,args})});
        const payload = await response.json() as {error?:string;result?:Record<string,unknown>};
        if (!response.ok || !payload.result) throw new Error(payload.error ?? "Catalog tool failed");
        const result = payload.result;
        if (result.image_transfer_limits && typeof result.image_transfer_limits === "object") {
          requestLimit = Number((result.image_transfer_limits as {request_bytes:number}).request_bytes);
        }
        if (name === "write_catalog" || result.checkpoint_report) {
          await mkdir(scratch, {recursive:true});
          const temporary = join(scratch,"catalog.next.json");
          const artifact = JSON.stringify(result.checkpoint_report ?? result.report);
          await writeFile(temporary, artifact, {mode:0o600});
          await rename(temporary,join(scratch,"catalog.json"));
          const receipt = {artifact_path:"scratch/catalog.json",byte_count:Buffer.byteLength(artifact),
            sha256:createHash("sha256").update(artifact).digest("hex")};
          await writeFile(join(scratch,"catalog-receipt.next.json"),JSON.stringify(receipt),{mode:0o600});
          await rename(join(scratch,"catalog-receipt.next.json"),join(scratch,"catalog-receipt.json"));
          if (name === "write_catalog") {
            return {content:[{type:"text" as const,text:JSON.stringify({published:result.published,catalog_version:result.catalog_version,node_count:result.node_count,directory_complete:result.directory_complete})}],details:{}};
          }
        }
        const {images,checkpoint_report,...text} = result;
        const content: Array<{type:"text";text:string}|{type:"image";data:string;mimeType:string}> = [{type:"text",text:JSON.stringify(text)}];
        for (const item of (images ?? []) as Array<{data:string;mimeType:string}>) content.push({type:"image",data:item.data,mimeType:item.mimeType});
        return {content,details:{}};
      }
    });
  }
}
