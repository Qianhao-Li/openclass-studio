const fs = require("fs").promises;

class PDFLoader {
  constructor(filePath, { splitPages = true, includeEmptyPages = false, preserveLayoutSpaces = false } = {}) {
    this.filePath = filePath;
    this.splitPages = splitPages;
    this.includeEmptyPages = includeEmptyPages;
    this.preserveLayoutSpaces = preserveLayoutSpaces;
    this.nativeMetadata = null;
  }

  async load() {
    const buffer = await fs.readFile(this.filePath);
    const { getDocument, version } = await this.getPdfJS();

    const pdf = await getDocument({
      data: new Uint8Array(buffer),
      useWorkerFetch: false,
      isEvalSupported: false,
      useSystemFonts: true,
    }).promise;

    const meta = await pdf.getMetadata().catch(() => null);
    const outline = [];
    const visit = async (items, level = 1) => {
      for (const item of items || []) {
        try {
          const destination = typeof item.dest === "string" ? await pdf.getDestination(item.dest) : item.dest;
          const page = typeof destination?.[0] === "number" ? destination[0] : await pdf.getPageIndex(destination[0]);
          if (Number.isInteger(page) && page >= 0 && page < pdf.numPages)
            outline.push({ title: item.title, level, page: page + 1 });
        } catch { /* A broken bookmark is not a verified body destination. */ }
        await visit(item.items, level + 1);
      }
    };
    await visit(await pdf.getOutline().catch(() => []));
    this.nativeMetadata = { totalPages: pdf.numPages, outline,
      pageLabels: await pdf.getPageLabels().catch(() => null) };
    const documents = [];

    for (let i = 1; i <= pdf.numPages; i += 1) {
      const page = await pdf.getPage(i);
      const content = await page.getTextContent();
      const viewport = Number(version.split(".")[0]) >= 3 ? page.getViewport({ scale: 1 }) : page.getViewport(1);
      const sizes = content.items.filter(item => item.str?.trim()).map(item => Math.hypot(item.transform[2], item.transform[3])).sort((a,b) => a-b);
      const typical = sizes[Math.floor(sizes.length / 2)] || 12;
      const pagination = content.items.filter(item => {
        const [x, y] = viewport.convertToViewportPoint(item.transform[4], item.transform[5]);
        const fontSize = Math.hypot(item.transform[2], item.transform[3]);
        return /^\s*(?:\d+|[ivxlcdm]+)\s*$/i.test(item.str || "") && fontSize <= 1.25 * typical &&
          x >= 0 && x <= viewport.width && y >= 0 && y <= viewport.height &&
          (y < viewport.height * .12 || y > viewport.height * .88);
      }).map(item => item.str.trim());

      if (content.items.length === 0 && !this.includeEmptyPages) {
        continue;
      }

      let lastY;
      let lastRight;
      let lastFontSize;
      const textItems = [];
      for (const item of content.items) {
        if ("str" in item) {
          const fontSize = Math.hypot(item.transform[2], item.transform[3]);
          if (this.preserveLayoutSpaces && (item.transform[5] + fontSize < page.view[1] ||
              item.transform[5] > page.view[3] || item.transform[4] + item.width < page.view[0] ||
              item.transform[4] > page.view[2])) continue;
          const sameLine = this.preserveLayoutSpaces && lastY !== undefined
            ? Math.abs(lastY - item.transform[5]) < .6 * Math.max(fontSize, lastFontSize || fontSize)
            : lastY === item.transform[5] || !lastY;
          if (sameLine) {
            if (this.preserveLayoutSpaces && lastRight !== undefined &&
                item.transform[4] - lastRight > Math.max(1, fontSize * 0.2)) textItems.push(" ");
            textItems.push(item.str);
          } else {
            textItems.push(`\n${item.str}`);
          }
          lastY = item.transform[5];
          lastRight = item.transform[4] + item.width;
          lastFontSize = fontSize;
        }
      }

      const text = textItems.join("");
      documents.push({
        pageContent: text.trim(),
        metadata: {
          source: this.filePath,
          pdf: {
            version,
            info: meta?.info,
            metadata: meta?.metadata,
            totalPages: pdf.numPages,
          },
          loc: { pageNumber: i },
          pagination,
        },
      });
    }

    if (this.splitPages) {
      await pdf.destroy();
      return documents;
    }

    if (documents.length === 0) {
      return [];
    }

    return [
      {
        pageContent: documents.map((doc) => doc.pageContent).join("\n\n"),
        metadata: {
          source: this.filePath,
          pdf: {
            version,
            info: meta?.info,
            metadata: meta?.metadata,
            totalPages: pdf.numPages,
          },
        },
      },
    ];
  }

  async getPdfJS() {
    try {
      if (this.preserveLayoutSpaces) {
        const pdfjs = await import(require("node:url").pathToFileURL(
          require.resolve("pdfjs-dist/legacy/build/pdf.mjs")
        ).href);
        return { getDocument: pdfjs.getDocument, version: pdfjs.version };
      }
      const pdfjs = await import(require("node:url").pathToFileURL(
        require.resolve("pdf-parse/lib/pdf.js/v1.10.100/build/pdf.js")
      ).href);
      const api = pdfjs.getDocument ? pdfjs : pdfjs.default;
      return { getDocument: api.getDocument, version: api.version };
    } catch (e) {
      console.error(e);
      throw new Error(
        "Failed to load pdf-parse. Please install it with eg. `npm install pdf-parse`."
      );
    }
  }
}

module.exports = PDFLoader;
