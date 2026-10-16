type TransferOptions = {
  pendingFork: boolean;
  mergeActive: boolean;
  onError: (message: string) => void;
  importDocx: (file: File) => void | Promise<void>;
  exportDocx: () => void | Promise<void>;
  exportHtml: () => void | Promise<void>;
  importRidoc: (file: File) => void | Promise<unknown>;
  exportRidoc: () => void | Promise<void>;
};

/** Shared editor file actions preserve the existing fork and merge boundaries. */
export function studioDocumentTransfers(options: TransferOptions) {
  return {
    onImportDocx(file: File) {
      if (options.pendingFork) {
        options.onError("Return to the current version or save the first branch edit before importing DOCX.");
        return;
      }
      if (options.mergeActive) {
        options.onError("DOCX cannot be imported during the merge, please submit or abandon the merge first.");
        return;
      }
      void options.importDocx(file);
    },
    onExportDocx() {
      if (options.pendingFork) { options.onError("Return to the current version before exporting the server document."); return; }
      void options.exportDocx();
    },
    onExportHtml() {
      if (options.pendingFork) { options.onError("Return to the current version before exporting the server document."); return; }
      void options.exportHtml();
    },
    onImportRidoc(file: File) {
      if (options.pendingFork) { options.onError("Return to the current version before importing a course package."); return; }
      void options.importRidoc(file);
    },
    onExportRidoc() {
      if (options.pendingFork) { options.onError("Return to the current version before exporting a course package."); return; }
      void options.exportRidoc();
    },
  };
}
