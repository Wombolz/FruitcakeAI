import { lstat, readdir, realpath, stat } from "node:fs/promises";
import { relative, resolve, sep } from "node:path";
import { homedir } from "node:os";
import { EDIT_TOOLS, registerEditTools } from "./edit_tools.mjs";
import { COMMAND_TOOL, registerCommandTool } from "./command_tools.mjs";

export const GUARD_PROTOCOL = "fruitcake.workspace_guard.v1";
const READ_TOOLS = new Set(["read", "grep", "find", "ls"]);

function inside(root, target) {
  const path = relative(root, target);
  return path !== ".." && !path.startsWith(`..${sep}`) && !path.startsWith(sep);
}

async function validateTree(root, directory, recursive) {
  const pending = [directory];
  let checked = 0;
  while (pending.length) {
    const current = pending.pop();
    for (const entry of await readdir(current, { withFileTypes: true })) {
      if (++checked > 50000) throw new Error("Directory safety scan exceeds the spike limit");
      const path = resolve(current, entry.name);
      const metadata = await lstat(path);
      if (metadata.isSymbolicLink()) {
        if (!inside(root, await realpath(path))) {
          throw new Error("Directory contains a symbolic link outside the workspace");
        }
      } else if (metadata.isDirectory()) {
        if (recursive) pending.push(path);
      } else if (!metadata.isFile() || metadata.nlink !== 1) {
        throw new Error("Directory contains a special file or hard link");
      }
    }
  }
}

export async function authorizeRead(root, toolName, input) {
  if (!READ_TOOLS.has(toolName)) {
    throw new Error("Tool is unavailable in this read-only workspace");
  }
  const supplied = input.path ?? (toolName === "read" ? null : ".");
  if (typeof supplied !== "string" || !supplied || supplied.includes("\0")) {
    throw new Error("A valid workspace path is required");
  }
  // Resolve exactly once and replace Pi's input so its filename fallbacks cannot
  // select a different target after authorization.
  let path = supplied.startsWith("@") ? supplied.slice(1) : supplied;
  if (path === "~" || path.startsWith("~/")) path = homedir() + path.slice(1);
  const target = await realpath(resolve(root, path));
  if (/[\u00A0\u2000-\u200A\u202F\u205F\u3000]/u.test(target)) {
    throw new Error("Path would be changed by Pi's filename normalization");
  }
  if (!inside(root, target)) {
    throw new Error("Path is outside the authorized workspace");
  }
  const metadata = await stat(target);
  if (toolName === "read" || (toolName === "grep" && !metadata.isDirectory())) {
    if (!metadata.isFile() || metadata.nlink !== 1) {
      throw new Error("Only regular files without hard links may be read");
    }
  } else if (!metadata.isDirectory()) {
    throw new Error("A workspace directory is required");
  } else {
    await validateTree(root, target, toolName !== "ls");
  }
  return target;
}

export default function workspaceGuard(pi) {
  let root;
  const edits = JSON.parse(process.env.FRUITCAKE_PI_EDIT_TOOLS || "[]");
  const commands = JSON.parse(process.env.FRUITCAKE_PI_COMMAND_TOOLS || "[]");
  if (!Array.isArray(commands) || commands.some(name => name !== COMMAND_TOOL) || commands.length > 1) throw new Error("Invalid command-tool policy");
  if (commands.length) registerCommandTool(pi);
  if (!Array.isArray(edits) || edits.some(name => !EDIT_TOOLS.has(name))) throw new Error("Invalid edit-tool policy");
  if (edits.length) registerEditTools(pi, () => root);
  pi.on("session_start", async (_event, ctx) => {
    const expected = process.env.FRUITCAKE_PI_WORKSPACE;
    if (!expected || !process.env.FRUITCAKE_PI_GUARD_TOKEN) {
      throw new Error("Fruitcake workspace authorization is missing");
    }
    root = await realpath(expected);
    if ((await realpath(ctx.cwd)) !== root) {
      root = undefined;
      throw new Error("Pi working directory does not match the authorized workspace");
    }
    const enabled = JSON.parse(process.env.FRUITCAKE_PI_READ_TOOLS || "[]");
    if (!Array.isArray(enabled) || enabled.some((name) => !READ_TOOLS.has(name))) {
      throw new Error("Invalid Fruitcake read-tool policy");
    }
    pi.setActiveTools([...enabled, ...edits, ...commands]);
    ctx.ui.notify(JSON.stringify({
      protocol: GUARD_PROTOCOL,
      token: process.env.FRUITCAKE_PI_GUARD_TOKEN,
      workspace: root,
    }), "info");
  });

  pi.on("tool_call", async (event, ctx) => {
    try {
      if (!root) throw new Error("Workspace guard is not ready");
      if (edits.includes(event.toolName) || commands.includes(event.toolName)) return;
      event.input.path = await authorizeRead(root, event.toolName, event.input);
      if (process.env.FRUITCAKE_PI_CONFIRM_READS === "1") {
        const confirmed = await ctx.ui.confirm(
          "Allow workspace read?",
          JSON.stringify({ operation_id: event.toolCallId, tool: event.toolName, path: event.input.path }),
          Number(process.env.FRUITCAKE_PI_APPROVAL_TIMEOUT_MS) > 0
            ? { timeout: Number(process.env.FRUITCAKE_PI_APPROVAL_TIMEOUT_MS) } : {},
        );
        if (!confirmed) throw new Error("Workspace read was denied or approval expired");
        // The filesystem may have changed while the request was awaiting a decision.
        event.input.path = await authorizeRead(root, event.toolName, event.input);
      }
    } catch (error) {
      return { block: true, reason: `Fruitcake workspace guard: ${error.message}` };
    }
  });

  // Direct user-bash RPC bypasses tool_call. Deny it independently.
  pi.on("user_bash", async () => ({
    result: { output: "Commands are unavailable in this read-only workspace", exitCode: 126, cancelled: false, truncated: false },
  }));
}
