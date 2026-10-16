"use strict";
class SourceDocumentError extends Error {
  constructor(message) { super(message); this.publicMessage = message; }
}
module.exports = { SourceDocumentError };
