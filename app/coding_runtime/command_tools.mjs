export const COMMAND_TOOL = "fruitcake_run";

export function commandProposal(operationId, input) {
  if (!input || typeof input !== "object" || Array.isArray(input)
      || Object.keys(input).some(key => !["command", "working_directory", "timeout_seconds"].includes(key))) {
    throw new Error("Call fruitcake_run with command and optional working_directory/timeout_seconds only.");
  }
  const command = input.command;
  const directory = input.working_directory === undefined ? "." : input.working_directory;
  const seconds = input.timeout_seconds === undefined ? 120 : input.timeout_seconds;
  if (typeof command !== "string" || !command.trim() || Buffer.byteLength(command, "utf8") > 8192 || command.includes("\0")) {
    throw new Error("fruitcake_run requires a nonempty command of at most 8192 UTF-8 bytes without NUL.");
  }
  if (typeof directory !== "string" || directory.length > 512 || /[\x00-\x1f]/u.test(directory)
      || (directory !== "." && directory.split("/").some(part => !part || part === "." || part === ".." || part.toLowerCase() === ".git"))) {
    throw new Error("working_directory must be an existing checkout-relative directory. Use '.' or omit it for the checkout root, not '/workspace' or the Mac checkout path.");
  }
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > 300) {
    throw new Error("timeout_seconds must be an integer from 1 to 300.");
  }
  return {protocol: "fruitcake.container_command.v1", operation_id: operationId,
    command, working_directory: directory, timeout_seconds: seconds};
}

export function registerCommandTool(pi) {
  pi.on("before_agent_start", (event) => ({
    systemPrompt: event.systemPrompt + "\n\nFruitcake command approval workflow:\n"
      + "When the user asks to run commands or tests, CALL fruitcake_run with the exact command to request the host approval card. "
      + "Calling this tool proposes execution; it does not execute before approval. Do not ask for approval in chat first, invent an approval card in text, or stop after printing shell commands. "
      + "For example, call fruitcake_run with {\"command\":\"python -m unittest\",\"working_directory\":\".\"}. "
      + "Use '.' or omit working_directory for the checkout root; never pass its absolute Mac path or '/workspace' as working_directory. "
      + "Wait for the host tool result before claiming execution or proposing a dependent command. Chat replies such as 'approved' are not host approval. "
      + "If the tool reports invalid arguments, correct those arguments and request approval through the tool, not through prose. "
      + "If the user only asks to explain a command or not to run it, do not call the tool. Never retry a denied or stopped command without a new user request.",
  }));
  pi.registerTool({
    name: COMMAND_TOOL, label: "Container Command", executionMode: "sequential",
    description: "CALL THIS TOOL to request a command approval card when asked to run a command or tests; printing a command or asking for approval in chat does not request approval. Calling it proposes execution and waits for the user's host approval before anything runs. Fruitcake executes it ONLY in an offline Linux Docker container with this session's managed checkout mounted writable. No host shell, internet, LAN, host localhost, Docker socket, or personal folders are available. The image has its own installed tools; do not assume host dependencies exist. Commands can change checkout files, including unrelated files. Never claim execution before the host result arrives.",
    parameters: {type: "object", additionalProperties: false, properties: {
      command: {type: "string", description: "Exact noninteractive /bin/sh command, up to 8192 UTF-8 bytes."},
      working_directory: {type: "string", description: "Existing checkout-relative directory. Omit or use '.' for the root, NOT '/workspace' or an absolute Mac path. Within the command string, files are under /workspace."},
      timeout_seconds: {type: "integer", minimum: 1, maximum: 300},
    }, required: ["command"]},
    async execute(operationId, input, signal, _onUpdate, ctx) {
      if (signal?.aborted) throw new Error("Command stopped");
      // Reject malformed model arguments as a tool error it can correct, before
      // sending a private approval proposal that would terminate the host run.
      const proposal = commandProposal(operationId, input);
      // RPC input is a private result channel, not a free-form user prompt.
      // Human approval has no deadline. The host bounds execution after approval.
      const value = await ctx.ui.input("Approve container command?", JSON.stringify(proposal), { signal });
      if (!value || signal?.aborted) throw new Error("Command denied, expired, interrupted, or stopped");
      const result = JSON.parse(value);
      if (result.protocol !== proposal.protocol || result.operation_id !== operationId) throw new Error("Invalid host command result");
      if (result.status !== "completed") throw new Error(JSON.stringify(result));
      return {content: [{type: "text", text: JSON.stringify(result)}], details: undefined};
    },
  });
}
