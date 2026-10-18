import { lstat, open, realpath } from "node:fs/promises";
import { constants } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

export const FILE_BYTES = 256 * 1024;
export const PROPOSAL_BYTES = 4 * 1024 * 1024;
const LIMIT = FILE_BYTES;
const PROTOCOL = "fruitcake.file_change.v1";
export const EDIT_TOOLS = new Set(["fruitcake_edit", "fruitcake_write"]);

export function changePath(root, supplied) {
  if (typeof supplied !== "string" || !supplied || supplied.length > 4096 || /[\x00-\x1f]/u.test(supplied)) throw new Error("Invalid change path");
  // Reads expose absolute paths. Accept those only within this checkout, then
  // freeze a relative path for the host's stricter proposal/approval contract.
  if (supplied.split("/").some(part => part === ".." || part.toLowerCase() === ".git")) {
    throw new Error("Traversal and Git metadata paths cannot be changed");
  }
  const path = isAbsolute(supplied) ? relative(root, supplied) : supplied;
  if (isAbsolute(path) || path === ".." || path.startsWith(`..${sep}`)) throw new Error("Path is outside the authorized checkout");
  const parts = path.split("/");
  if (path.length > 1024 || parts.some(part => !part || part === ".")) {
    throw new Error("Use a relative path outside Git metadata");
  }
  return path;
}

async function snapshot(root, path) {
  const parts = path.split("/");
  const target = resolve(root, path);
  let current = root;
  for (const part of parts.slice(0, -1)) {
    current = resolve(current, part);
    const entry = await lstat(current);
    if (!entry.isDirectory() || entry.isSymbolicLink()) throw new Error("Parent must be an existing directory without links");
  }
  try {
    const entry = await lstat(target);
    if (!entry.isFile() || entry.isSymbolicLink() || entry.nlink !== 1) throw new Error("File must be regular text without links");
    if (entry.size > LIMIT) throw new Error("File exceeds 256 KiB. Leave it unchanged; do not truncate or shorten files to fit.");
    // Read only: the host owns all writes, including the final safety checks.
    const file = await open(target, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const actual = await file.stat();
      if (!actual.isFile() || actual.nlink !== 1 || actual.size > LIMIT || await realpath(target) !== target) throw new Error("File changed during validation");
      const buffer = Buffer.alloc(LIMIT + 1);
      let bytesRead = 0;
      while (bytesRead < buffer.length) {
        const chunk = await file.read(buffer, bytesRead, buffer.length - bytesRead, bytesRead);
        if (!chunk.bytesRead) break;
        bytesRead += chunk.bytesRead;
      }
      if (bytesRead > LIMIT) throw new Error("File exceeds 256 KiB. Leave it unchanged; do not truncate or shorten files to fit.");
      const content = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, bytesRead));
      if (content.includes("\0")) throw new Error("Binary files are not supported");
      return content;
    } finally { await file.close(); }
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

export function registerEditTools(pi, getRoot) {
  for (const name of EDIT_TOOLS) {
    const editing = name === "fruitcake_edit";
    pi.registerTool({
      name, label: name,
      executionMode: "sequential",
      description: editing
        ? "Propose one exact, unique text replacement in a UTF-8 file up to 256 KiB. Prefer this for existing files; all other content is preserved. Fruitcake must approve and apply it. Never shorten or truncate a file to fit a limit."
        : "Propose a complete new or replacement UTF-8 file up to 256 KiB in an existing directory. Prefer fruitcake_edit for localized changes to existing files. Preserve all unrelated content; never write a truncated read preview or shorten a file to fit a limit. Fruitcake must approve and apply it.",
      parameters: {
        type: "object", additionalProperties: false,
        properties: editing ? { path: { type: "string", description: "Relative to the checkout, or an absolute path inside it." }, oldText: { type: "string" }, newText: { type: "string" } }
          : { path: { type: "string", description: "Relative to the checkout, or an absolute path inside it." }, content: { type: "string" } },
        required: editing ? ["path", "oldText", "newText"] : ["path", "content"],
      },
      async execute(operationId, input, signal, _onUpdate, ctx) {
        const root = getRoot();
        if (!root || signal?.aborted || (await realpath(ctx.cwd)) !== root) throw new Error("Checkout is unavailable");
        const path = changePath(root, input.path);
        const before = await snapshot(root, path);
        let after = input.content;
        if (editing) {
          if (before === null || typeof input.oldText !== "string" || !input.oldText || typeof input.newText !== "string") throw new Error("An exact replacement is required");
          const offset = before.indexOf(input.oldText);
          if (offset < 0 || before.indexOf(input.oldText, offset + 1) >= 0) throw new Error("oldText must match exactly once");
          after = before.slice(0, offset) + input.newText + before.slice(offset + input.oldText.length);
        }
        if (typeof after !== "string" || after.includes("\0") || Buffer.byteLength(after) > LIMIT || after === before) throw new Error("Propose a nonempty text change within 256 KiB. Leave oversized files unchanged; do not truncate or shorten files to fit.");
        const message = JSON.stringify({ protocol: PROTOCOL, operation_id: operationId, tool: name, path, before, after });
        if (Buffer.byteLength(message) > PROPOSAL_BYTES) throw new Error("Change is too large to approve completely. Leave the file unchanged; do not truncate it.");
        if (signal?.aborted) throw new Error("Operation stopped");
        const timeout = Number(process.env.FRUITCAKE_PI_APPROVAL_TIMEOUT_MS);
        const allowed = await ctx.ui.confirm("Approve file change?", message, timeout > 0 ? { timeout, signal } : { signal });
        if (!allowed || signal?.aborted) throw new Error("Change denied, expired, or stopped");
        if (await snapshot(root, path) !== after) throw new Error("Host did not apply the approved content; inspect the saved change record");
        return { content: [{ type: "text", text: `Fruitcake applied the approved change to ${path}.` }], details: undefined };
      },
    });
  }
}
