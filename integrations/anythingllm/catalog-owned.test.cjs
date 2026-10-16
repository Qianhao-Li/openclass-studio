const test = require("node:test");
const assert = require("node:assert/strict");
const { SourceCatalogWorkflow } = require("../../third_party/anythingllm/server/utils/SourceCatalog/workflow");
const { directoryResult } = require("../../third_party/anythingllm/server/utils/SourceCatalog/output");
const { validateCatalog } = require("./contracts.cjs");
function fixture(pages, nativeEntries = []) {
  const { DatabaseSync } = require("node:sqlite");
  const source = { owner_user_id: "alice", package_id: "course", id: "original", source_version: "v1", content_hash: "a".repeat(64), catalog_version: 1 };
  const data = { source_id: source.id, source_version: source.source_version, content_hash: source.content_hash, format: "pdf", parser_provider: "anythingllm", parser_version: "anythingllm_positioned_document_v2",
    native_entries: nativeEntries, authored: nativeEntries.length > 0, units: pages.map((text,i) => ({ id: i+1, text,
      text_start: 0, text_end: Array.from(text).length, locator: { kind: "pdf_pages", start: i+1, end: i+1 } })) };
  const store = { db: new DatabaseSync(":memory:"), get(value) { assert.deepEqual(value,source); },
    content(value) { this.get(value); return data; }, catalog(value,chapters,metadata) { this.get(value); validateCatalog(chapters,data,metadata); } };
  return { workflow: new SourceCatalogWorkflow(store), source, data, db: store.db,
    options: { model: { model: "selected" }, max_input_bytes: 20000, max_image_count: 600, supports_images: true } };
}
const row = (title,start_unit=1,level=1) => ({ title, start_unit, level });

test("returned rows are displayed despite omitted declarations, uncertainty and unlocated titles", () => {
  const f = fixture(["Contents\n1 First .... 1\n2 Second .... 2\nXX IV", "1 First\nBody", "2 Second\nBody"]);
  const task = f.workflow.start(f.source,f.options);
  const result = f.workflow.advance(f.source,task.task_id,task.request.request_hash,{
    complete: false, unread_unit_ids: [1], unresolved: ["Uncertain location"], rows: [row("Model title",1,4)] });
  assert.equal(result.status,"ready");
  assert.deepEqual(result.result.chapters.map(chapter=>chapter.title),["Model title"]);
  assert.equal(result.result.metadata.catalog_quality_gates,"disabled");
  assert.equal(result.result.structure.quality.level,"unassessed");
  assert.equal(result.result.metadata.directory_status,"uncertain");
  assert.equal(result.result.chapters[0].range,null);
  f.db.close();
});

test("native navigation is returned without a second completeness or title gate", () => {
  const f=fixture(["Contents\nFirst .... 1\nBogus footer .... IV", "First\nBody"],[
    { title:"Original native label",level:4,range:{kind:"pdf_pages",start:2,end:2} }]);
  const task=f.workflow.start(f.source,f.options);
  assert.equal(task.status,"ready"); assert.equal(task.model_calls,0);
  assert.deepEqual(task.result.chapters.map(chapter=>chapter.title),["Original native label"]);
  f.db.close();
});

test("actual located sibling ranges exclude the next page without blocking unlocated rows", () => {
  const f=fixture(["1 First\nBody", "2 Second\nBody"]);
  f.data.authored=false;
  const result=directoryResult(f.source,f.data,[row("1 First",1),row("2 Second",2)],{engine:"anythingllm_source_catalog_v2"});
  assert.equal(result.chapters[0].range.end,1);
  assert.equal(result.chapters[1].range.start,2);
  f.db.close();
});

test("resizing freezes a smaller batch and continuation retains all remaining original units", () => {
  const f=fixture(Array(8).fill(""));f.data.visual_only=true;
  const task=f.workflow.start(f.source,f.options);
  assert.equal(task.request.original_image_requests.length,8);
  const small=f.workflow.resize(f.source,task.task_id,task.request.request_hash,3);
  assert.deepEqual(small.request.unit_ids,[1,2]);
  const receipts=[1,2].map(unit_id=>({unit_id,content_hash:f.source.content_hash,source_version:"v1",payload_sha256:"b".repeat(64)}));
  const next=f.workflow.advance(f.source,small.task_id,small.request.request_hash,{complete:true,rows:[row("First observed page",1)]},receipts);
  assert.equal(next.status,"processing");
  assert.deepEqual(next.request.unit_ids,[3,4,5,6,7,8]);
  assert.equal(next.result.chapters.length,1);
  f.db.close();
});

test("cancel, owner, parser version and frozen request identity remain enforced", () => {
  const f=fixture(["# Original\nBody"]),task=f.workflow.start(f.source,f.options);
  assert.throws(()=>f.workflow.advance({...f.source,owner_user_id:"bob"},task.task_id,task.request.request_hash,{rows:[]}));
  assert.throws(()=>f.workflow.advance(f.source,task.task_id,"changed",{rows:[]}));
  f.workflow.stop(f.source,task.task_id,"cancelled");
  assert.throws(()=>f.workflow.advance(f.source,task.task_id,task.request.request_hash,{rows:[]}),/cancelled/);
  f.data.units[0].text="changed parser artifact";
  assert.throws(()=>f.workflow.get(f.source,task.task_id),/parsed original changed/);
  f.db.close();
});

test("original image receipts and duplicate advance identities remain enforced", () => {
  const f=fixture([""]);f.data.visual_only=true;
  const task=f.workflow.start(f.source,f.options),output={complete:true,rows:[row("Observed original image")]};
  assert.throws(()=>f.workflow.advance(f.source,task.task_id,task.request.request_hash,output),/receipt/);
  const receipt={unit_id:1,content_hash:f.source.content_hash,source_version:"v1",payload_sha256:"b".repeat(64)};
  const result=f.workflow.advance(f.source,task.task_id,task.request.request_hash,output,[receipt]);
  assert.equal(result.status,"ready");
  assert.equal(f.workflow.advance(f.source,task.task_id,task.request.request_hash,output,[receipt]).model_calls,1);
  f.db.close();
});

test("directory evidence points to read TOC units separately from body and read coverage", () => {
  const f=fixture(["Navigation leaf", "1 First\nBody", "Body continuation"]);
  const task=f.workflow.start(f.source,f.options);
  const result=f.workflow.advance(f.source,task.task_id,task.request.request_hash,{
    complete:true,authored_directory:true,directory_complete:true,directory_unit_ids:[1,999],
    rows:[{...row("1 First",1),directory_unit:1,body_unit:2,body_heading:"1 First"}]});
  const meta=result.result.metadata;
  assert.deepEqual(meta.catalog_directory_evidence.map(x=>x.locator.start),[1]);
  assert.equal(meta.catalog_directory_evidence[0].content_hash,f.source.content_hash);
  assert.equal(result.result.chapters[0].metadata.directory_evidence.locator.start,1);
  assert.equal(meta.catalog_read_ranges[0].locator.end,3);
  assert.equal(meta.catalog_directory_origin,"original_pages");
  f.db.close();
});

test("old outputs only supply candidate locations and native navigation claims no page reading", () => {
  const f=fixture(["Contents\nFirst .... 1","First\nBody"]);
  let task=f.workflow.start(f.source,f.options);
  const result=f.workflow.advance(f.source,task.task_id,task.request.request_hash,{
    complete:true,authored_directory:true,directory_complete:true,rows:[row("First",1)]});
  assert.deepEqual(result.result.metadata.catalog_directory_evidence,[]);
  assert.equal(result.result.metadata.catalog_directory_candidates[0].status,"candidate");
  f.db.close();
  const n=fixture(["First\nBody"],[{title:"First",level:1,range:{kind:"pdf_pages",start:1,end:1}}]);
  task=n.workflow.start(n.source,n.options);
  assert.equal(task.result.metadata.catalog_directory_origin,"native_navigation");
  assert.deepEqual(task.result.metadata.catalog_read_ranges,[]);
  n.db.close();
});
