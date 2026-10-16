"use strict";
const { serve, serviceKey } = require("./io.cjs");
const { SourceStore } = require("./store.cjs");
const { SourceIndex } = require("./index.cjs");
const { CatalogWorkflow } = require("./catalog.cjs");
const { SourceCatalogWorkflow } = require("../../third_party/anythingllm/server/utils/SourceCatalog/workflow");
const store = new SourceStore();
const index = new SourceIndex(store);
const catalog = new CatalogWorkflow(store);
const sourceCatalog = new SourceCatalogWorkflow(store);
const collector = `http://127.0.0.1:${Number(process.env.OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT || 8895)}`;

serve(async (method, url, payload) => {
  if (method !== "POST") throw new Error("Unknown source operation.");
  const source = payload.source;
  switch (url) {
    case "/sources/import":
    case "/sources/collect": {
      const native = url === "/sources/collect";
      const response = await fetch(collector + (native ? "/collect-original" : "/collect"), { method: "POST", headers: {
        "Authorization": "Bearer " + serviceKey(), "Content-Type": "application/json",
      }, body: JSON.stringify(payload) });
      if (!response.ok) {
        const error = new Error("Source collection failed.");
        error.publicMessage = (await response.json()).error;
        throw error;
      }
      const result = await response.json();
      return { ...store.imported(source, result.document_name), ...(native ? result : {}) };
    }
    case "/catalog/owned/start":
    case "/catalog/owned/advance":
    case "/catalog/owned/resize":
    case "/catalog/owned/stop": {
      if (url.endsWith("/start")) return sourceCatalog.start(source, payload.options);
      if (url.endsWith("/advance")) return sourceCatalog.advance(source, payload.task_id, payload.request_hash, payload.output, payload.image_receipts);
      if (url.endsWith("/resize")) return sourceCatalog.resize(source, payload.task_id, payload.request_hash, payload.before_unit);
      return sourceCatalog.stop(source, payload.task_id, payload.reason);
    }
    case "/catalog/begin": return catalog.begin(source, payload.batch_hash, payload.request);
    case "/catalog/complete": return catalog.complete(source, payload.batch_hash, payload.output);
    case "/catalog/publish": return store.catalog(source, payload.chapters, payload.metadata);
    case "/catalog/read": return store.readCatalog(source);
    case "/sources/activate": { const result = store.activate(source); index.kick(); return result; }
    case "/sources/status": return { sources: payload.sources.map(item => store.status(item)) };
    case "/sources/revoke": return store.revoke(source);
    case "/sources/search": return await index.search(payload.owner_user_id, payload.grants, payload.query, payload.limit || 8);
    default: throw new Error("Unknown source operation.");
  }
}, Number(process.env.OPENCLASS_ANYTHINGLLM_SERVER_PORT || 3321));
index.kick();
