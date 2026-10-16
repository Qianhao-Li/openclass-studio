# Third-party notices

OpenClass's MIT license covers its own code. Dependencies and redistributed assets
retain their original terms and notices. npm and Python lock/install metadata
identify the precise versions used in a release.

- Pi: MIT. Preserve its upstream license and pinned runtime version.
- Codex CLI: Apache-2.0. Preserve its shipped license and notices. ChatGPT device
  authorization depends on the account/workspace allowing device login; see
  https://developers.openai.com/codex/auth . Subscription access and API billing
  are separate provider channels.
- Tiptap/ProseMirror, React, Next.js and other browser dependencies: retain their shipped licenses.
- pypdf: BSD-3-Clause. pypdfium2: Apache-2.0/BSD-3-Clause; PDFium and its bundled
  dependencies have additional notices shipped in the wheel. Retain those notices
  in binary distributions.
- resvg-py and its resvg runtime: retain their shipped licenses and notices.
- AnythingLLM, when installed as an optional source adapter, retains its upstream
  license and revision. Its source is not relicensed as OpenClass code.
- Model provider names and logos remain subject to their respective rights.

Release checks must audit both runtime and optional adapters. PyMuPDF is not an
approved runtime dependency for this MIT/private-host distribution profile.
