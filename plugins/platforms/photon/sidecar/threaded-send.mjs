import { secureLinkContent } from "./secure-link-content.mjs";
import { plainOutbound } from "./outbound-plain.mjs";
// One send attempt, with a reply target resolved only in the destination space.
export async function sendText(body, deps) {
  const { spaceId, format = "text", logicalMessageId = null, replyTo = null } = body || {};
  let { text } = body || {};
  if (!spaceId || typeof text !== "string") throw new TypeError("spaceId and text are required");
  // Chris's notification rules: finished work or one decision, plain words, no internals.
  if (process.env.PHOTON_PLAIN_OUTBOUND !== "0") {
    const plain = plainOutbound(text);
    if (plain.drop) {
      console.error(`[photon-sidecar] suppressed outbound (${plain.reason})`);
      return { id: null, suppressed: plain.reason };
    }
    text = plain.text;
  }
  if (!["text", "markdown"].includes(format)) throw new TypeError("format must be text or markdown");
  const space = await deps.resolveSpace(spaceId);
  const target = await resolveReplyTarget(space, replyTo);
  const normalized = deps.normalizeOutboundText(text);
  // Preserve existing plain-message receipts; bind new reply intents to their target.
  deps.claimOutboundIntent({
    kind: "text", target: spaceId, logicalMessageId,
    payload: replyTo ? JSON.stringify({ replyTo, text: normalized }) : normalized,
  });
  // The pinned iMessage provider rejects richlinks inside multipart groups.
  // Send caption then card under the single existing intent claim. If either
  // attempt fails, never replay the sequence or fall back to another send.
  const cards = deps.richlink ? secureLinkContent(text, deps) : null;
  const builders = cards ?? [deps.chooseSendFormat(format, text) === "markdown"
    ? deps.markdown(text) : deps.text(text)];
  let result;
  for (let builder of builders) {
    if (target) builder = deps.reply(builder, target);
    result = await space.send(builder);
    if ((target || cards) && !result?.id) throw new Error("Message delivery was not acknowledged");
  }
  return result;
}

export async function resolveReplyTarget(space, replyTo = null) {
  if (replyTo !== null && (typeof replyTo !== "string" || !replyTo || replyTo.length > 256)) {
    throw new TypeError("replyTo must be a message id");
  }
  let target;
  if (replyTo) {
    target = await space.getMessage(replyTo);
    if (!target || target.id !== replyTo || target.space?.id !== space.id) {
      throw new TypeError("Reply target is unavailable in this conversation");
    }
  }
  return target;
}
