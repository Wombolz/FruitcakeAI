export const REVIEW_TOOL = "fruitcake_review_read";
export const REVIEW_PROTOCOL = "fruitcake.review_read.v1";

export function registerReviewTool(pi) {
  pi.registerTool({
    name: REVIEW_TOOL, label: "Read Captured File", executionMode: "sequential",
    description: "Read an inventoried immutable capture, never live files. before/after returns all source with L<number>| line labels for exact citations; do not include labels in evidence. patch returns the retained unified diff, not a source side. Read each needed side once and reuse it; do not retry an unavailable result. The host may require human approval. No commands or writes.",
    parameters: {type: "object", additionalProperties: false, properties: {
      path: {type: "string"}, side: {type: "string", enum: ["before", "after", "patch"]},
    }, required: ["path", "side"]},
    async execute(operationId, input, signal, _onUpdate, ctx) {
      if (signal?.aborted) throw new Error("Review stopped");
      if (!input || Object.keys(input).some(key => !["path", "side"].includes(key))
          || typeof input.path !== "string" || !input.path || input.path.length > 1024
          || !["before", "after", "patch"].includes(input.side)) throw new Error("Choose a captured path and side");
      const proposal = {protocol: REVIEW_PROTOCOL, operation_id: operationId, ...input};
      const value = await ctx.ui.input("Read captured review file?", JSON.stringify(proposal), {signal});
      if (!value || signal?.aborted) throw new Error("Review read stopped");
      const result = JSON.parse(value);
      if (result.protocol !== REVIEW_PROTOCOL || result.operation_id !== operationId) throw new Error("Invalid host read result");
      if (result.status !== "completed") throw new Error(result.error || "Captured read unavailable");
      return {content: [{type: "text", text: result.text}], details: undefined};
    },
  });
}
