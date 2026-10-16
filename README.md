# OpenClass Studio

OpenClass Studio is a local course IDE: AI conversation, editable course documents,
RIDOC files and complete learning history. The same Studio UI and course engine can
be embedded in the hosted OpenClass platform.

## Run locally

Install Node.js 22.19+ and Python 3.13+. On Windows, run the backend in WSL2 and
open the local Studio URL in your Windows browser.

```sh
npm run studio:setup
npm run studio
```

For development, run `npm run studio -- --dev`.
The launcher opens http://127.0.0.1:3510/studio. It never registers a platform
account or reads a platform `.env`. Data persists outside the source checkout:
`~/Library/Application Support/OpenClass Studio` on macOS and
`~/.local/share/openclass-studio` on Linux/WSL2. Set
`OPENCLASS_STUDIO_DATA_DIR` to choose another persistent directory.

Editing, saving, importing, exporting and replaying complete RIDOC courses work
without a platform account or internet connection after installation. Cloud AI
requires an internet connection and a supported personal model account or API key.
Personal credentials stay on the device. Platform connection is optional and does
not change ownership of local courses or authorize automatic uploads.

To enable the preserved AnythingLLM source adapter, install its pinned optional
dependencies with `npm ci --prefix integrations/anythingllm`, then launch with
`OPENCLASS_STUDIO_SOURCE_PROCESSOR=anythingllm npm run studio`. Originals and
derived objects remain in this profile's local source storage. Native ordinary
reading does not require this adapter. Cloud enhancement is a separate capability.

Bounded source tools cannot run general shell commands. Native source shell
inspection requires an isolated runner supplied and verified by its host; this
release fails explicitly when that runner is unavailable. It never falls back
to running those commands on the host.

## Shared packages

`@openclass/studio` exports the Studio React components, host contracts and styles.
It ships TypeScript sources for a Next.js host using `transpilePackages`.
`openclass-core` provides the Python course engine and local ASGI entrypoint.
Platform accounts, billing, repository permissions and distribution services are
provided by the host through explicit interfaces.

## License and course content

OpenClass-authored code is MIT licensed. Dependencies retain their own licenses;
see THIRD_PARTY_NOTICES.md. The code license does not determine the license of
users' courses, sources or other content. Names and logos do not grant permission
to impersonate the official OpenClass service.

## Validation

Run `npm run verify` and `npm run test:e2e`. Real provider calls, platform-credit
acceptance and Windows/WSL2 acceptance are recorded separately from offline tests.
This source candidate must pass the release checks before it is published.
