import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import crypto from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const children = new Set();

export function studioEnvironment(input, root, launchToken, webPort, apiPort) {
  const output = Object.fromEntries(Object.entries(input).filter(([key]) =>
    !key.startsWith("OPENCLASS_") && !key.startsWith("AI_USAGE_") && !key.startsWith("CODEX_")
    && !/(_API_KEY|_AUTH_TOKEN|_ACCESS_TOKEN|_REFRESH_TOKEN|_CLIENT_SECRET|_SECRET_ACCESS_KEY|_SESSION_TOKEN)$/.test(key)
    && !key.startsWith("OPENROUTER_") && !["GOOGLE_APPLICATION_CREDENTIALS", "PI_CODING_AGENT_DIR", "SSH_AUTH_SOCK", "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "PYTHONPATH", "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS"].includes(key)));
  Object.assign(output, {
    OPENCLASS_STUDIO_MODE: "local", OPENCLASS_STUDIO_DATA_DIR: root,
    OPENCLASS_STUDIO_LAUNCH_TOKEN: launchToken,
    OPENCLASS_STUDIO_WEB_ORIGIN: `http://127.0.0.1:${webPort}`,
    OPENCLASS_STUDIO_API_ORIGIN: `http://127.0.0.1:${apiPort}`,
    NEXT_PUBLIC_API_BASE_URL: `http://127.0.0.1:${apiPort}`,
    OPENCLASS_LOCAL_RUNTIME: "true", NEXT_PUBLIC_OPENCLASS_LOCAL_RUNTIME: "true",
    NEXT_TELEMETRY_DISABLED: "1", OPENCLASS_NEXT_DIST_DIR: ".next-studio",
    OPENCLASS_PROJECT_ROOT: projectRoot,
    PATH: `${path.join(projectRoot, "node_modules/.bin")}${path.delimiter}${input.PATH || ""}`,
  });
  if (input.OPENCLASS_STUDIO_POPPLER_ROOT) output.OPENCLASS_POPPLER_ROOT = input.OPENCLASS_STUDIO_POPPLER_ROOT;
  for (const key of ["OPENCLASS_STUDIO_SOURCE_PROCESSOR", "OPENCLASS_STUDIO_SOURCE_SERVER_PORT", "OPENCLASS_STUDIO_SOURCE_COLLECTOR_PORT", "OPENCLASS_STUDIO_PLATFORM_ORIGIN", "OPENCLASS_STUDIO_ALLOW_TEST_PLATFORM"]) {
    if (input[key]) output[key] = input[key];
  }
  return output;
}

export function acquireStudioLease(root) {
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  if (fs.lstatSync(root).isSymbolicLink()) throw new Error("The Studio data directory cannot be a symlink.");
  const lease = path.join(root, "server.lock");
  let value = JSON.stringify({ pid: process.pid, nonce: crypto.randomBytes(16).toString("hex"), children: [] });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      fs.writeFileSync(lease, value, { flag: "wx", mode: 0o600 });
      const release = () => { if (fs.existsSync(lease) && fs.readFileSync(lease, "utf8") === value) fs.unlinkSync(lease); };
      release.register = pid => {
        if (fs.readFileSync(lease, "utf8") !== value) throw new Error("Studio lost its workspace lease.");
        const record = JSON.parse(value); record.children.push(pid); value = JSON.stringify(record);
        fs.writeFileSync(lease, value, { mode: 0o600 });
      };
      return release;
    }
    catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (fs.lstatSync(lease).isSymbolicLink()) throw new Error("The Studio server lock cannot be a symlink.");
      const previous = fs.readFileSync(lease, "utf8");
      let owners;
      try { const record = JSON.parse(previous); owners = [record.pid, ...(record.children || [])]; } catch { throw new Error("The Studio server lock is invalid. Inspect it before starting another backend."); }
      if (!owners.every(owner => Number.isSafeInteger(owner) && owner > 0)) throw new Error("The Studio server lock is invalid.");
      for (const owner of owners) {
        try { process.kill(owner, 0); throw new Error("Studio is already running for this data directory."); }
        catch (failure) { if (failure.code !== "ESRCH") throw failure; }
      }
      if (fs.readFileSync(lease, "utf8") === previous) fs.unlinkSync(lease);
    }
  }
  throw new Error("Another Studio process acquired this workspace.");
}

function command(program, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(program, args, { cwd: projectRoot, stdio: "inherit", ...options });
    child.once("error", reject);
    child.once("exit", code => code === 0 ? resolve() : reject(new Error(`${path.basename(program)} exited with code ${code}`)));
  });
}

async function requireFreePort(port) {
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", () => reject(new Error(`Port ${port} is in use. Choose another Studio port; the existing process was preserved.`)));
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

function startChild(program, args, env) {
  const child = spawn(program, args, { cwd: projectRoot, env, stdio: "inherit", detached: true });
  children.add(child);
  if (child.pid) releaseLease.register?.(child.pid);
  child.once("error", error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; shutdown(); });
  child.once("exit", code => { children.delete(child); if (!stopping) { process.exitCode = code || 1; shutdown(); } });
  return child;
}

let releaseLease = () => {};
let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    try { process.kill(-child.pid, "SIGTERM"); } catch { /* The owned child already exited. */ }
  }
  const remaining = [...children];
  const timer = setTimeout(() => { for (const child of remaining) { try { process.kill(-child.pid, "SIGKILL"); } catch { /* Already stopped. */ } } releaseLease(); }, 5000);
  timer.unref();
  Promise.all(remaining.map(child => new Promise(resolve => child.once("exit", resolve)))).then(() => { clearTimeout(timer); releaseLease(); });
}

async function waitFor(url) {
  for (let count = 0; count < 120; count += 1) {
    if (stopping) throw new Error("Studio stopped during startup.");
    try { if ((await fetch(url, { signal: AbortSignal.timeout(1000) })).ok) return; } catch { /* Startup is still in progress. */ }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`Studio did not become ready at ${url}`);
}

async function main() {
  const args = process.argv.slice(2);
  if (process.platform === "win32") {
    const converted = spawnSync("wsl.exe", ["--", "wslpath", "-a", projectRoot], { encoding: "utf8" });
    if (converted.status !== 0) throw new Error("Install WSL2 with Node.js and Python, then run the Studio launcher inside WSL.");
    await command("wsl.exe", ["--", "node", `${converted.stdout.trim()}/scripts/studio.mjs`, ...args]);
    return;
  }
  const python = path.join(projectRoot, ".venv/bin/python");
  if (args.includes("--setup")) {
    if (fs.existsSync(path.join(projectRoot, ".venv")) && fs.lstatSync(path.join(projectRoot, ".venv")).isSymbolicLink()) throw new Error("Setup requires an independent virtual environment. Use a fresh clone.");
    if (!fs.existsSync(python)) {
      const candidate = ["python3.13", "python3.14", "python3"].find(name => spawnSync(name, ["-c", "import sys;sys.exit(sys.version_info<(3,13))"]).status === 0);
      if (!candidate) throw new Error("Install Python 3.13 or newer before setting up Studio.");
      await command(candidate, ["-m", "venv", ".venv"]);
    }
    await command(python, ["-m", "pip", "install", "-e", ".[dev]"]);
    await command("npm", ["ci", "--ignore-scripts"]);
    return;
  }
  if (!fs.existsSync(python)) throw new Error("Run npm run studio:setup to install Studio first.");
  const root = path.resolve(process.env.OPENCLASS_STUDIO_DATA_DIR || (process.platform === "darwin"
    ? path.join(os.homedir(), "Library/Application Support/OpenClass Studio") : path.join(os.homedir(), ".local/share/openclass-studio")));
  if (root === projectRoot || root.startsWith(projectRoot + path.sep)) throw new Error("Store Studio data outside the source checkout.");
  const webPort = Number(process.env.OPENCLASS_STUDIO_WEB_PORT || 3510);
  const apiPort = Number(process.env.OPENCLASS_STUDIO_API_PORT || 8510);
  if (![webPort, apiPort].every(port => Number.isInteger(port) && port >= 1024 && port <= 65535) || webPort === apiPort) throw new Error("Choose two different Studio ports between 1024 and 65535.");
  await Promise.all([requireFreePort(webPort), requireFreePort(apiPort)]);
  releaseLease = acquireStudioLease(root);
  const env = studioEnvironment(process.env, root, crypto.randomBytes(48).toString("base64url"), webPort, apiPort);
  process.once("SIGINT", shutdown); process.once("SIGTERM", shutdown); process.once("SIGHUP", shutdown);
  process.once("exit", () => { if (!children.size) releaseLease(); });
  const module = fs.existsSync(path.join(projectRoot, "apps/api/openclass_core/studio_main.py")) ? "openclass_core" : "app";
  const development = args.includes("--dev");
  if (!development) await command("npm", ["run", "build:web"], { env });
  startChild(python, ["-m", "uvicorn", `${module}.studio_main:create_app`, "--factory", "--app-dir", "apps/api", "--host", "127.0.0.1", "--port", String(apiPort)], env);
  startChild("npm", ["--prefix", "apps/web", "run", development ? "dev" : "start", "--", ...(development ? ["--webpack"] : []), "--hostname", "127.0.0.1", "--port", String(webPort)], env);
  await Promise.all([waitFor(`${env.OPENCLASS_STUDIO_API_ORIGIN}/health`), waitFor(`${env.OPENCLASS_STUDIO_WEB_ORIGIN}/studio`)]);
  const href = `${env.OPENCLASS_STUDIO_WEB_ORIGIN}/studio`;
  process.stdout.write(`OpenClass Studio: ${href}\nCourses are saved on this device.\n`);
  if (!args.includes("--no-open")) {
    const opener = process.platform === "darwin" ? ["open", [href]] : process.env.WSL_DISTRO_NAME ? ["cmd.exe", ["/c", "start", "", href]] : ["xdg-open", [href]];
    const child = spawn(opener[0], opener[1], { stdio: "ignore" }); child.on("error", () => {}); child.unref();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; shutdown(); });
}
