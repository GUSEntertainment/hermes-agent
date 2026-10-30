// Normalize SDK message content without losing threaded reply bodies.
const REACTION_TARGET_TEXT_CAP = 2000;
export function reactionTargetText(target) {
  const c = target && typeof target === "object" ? target.content : null;
  if (!c || typeof c !== "object") return null;
  let text = null;
  if (c.type === "text") {
    text = c.text;
  } else if (c.type === "richlink") {
    text = c.url;
  } else if (c.type === "group") {
    for (const item of Array.isArray(c.items) ? c.items : []) {
      const ic = item && typeof item === "object" ? item.content : null;
      if (ic && ic.type === "text" && ic.text) {
        text = ic.text;
        break;
      }
      if (ic && ic.type === "richlink" && ic.url) {
        text = ic.url;
        break;
      }
    }
  }
  if (typeof text !== "string" || !text) return null;
  return text.length > REACTION_TARGET_TEXT_CAP
    ? text.slice(0, REACTION_TARGET_TEXT_CAP)
    : text;
}

export async function normalizeContent(content, normalizeBinaryContent, depth = 0) {
  if (depth > 8) return { type: "unknown" };
  if (!content || typeof content !== "object") {
    return { type: "unknown" };
  }
  if (content.type === "reply") {
    const target = content.target;
    return {
      type: "reply",
      content: await normalizeContent(content.content, normalizeBinaryContent, depth + 1),
      targetMessageId: target?.id ?? null,
      targetSpaceId: target?.space?.id ?? null,
      targetDirection: target?.direction ?? null,
      targetText: reactionTargetText(target),
    };
  }
  if (content.type === "text") {
    return { type: "text", text: content.text || "" };
  }
  if (content.type === "richlink") {
    const out = { type: "richlink", url: content.url || "" };
    if (typeof content.title === "string") {
      out.title = content.title;
    }
    if (typeof content.summary === "string") {
      out.summary = content.summary;
    }
    return out;
  }
  if (content.type === "attachment" || content.type === "voice") {
    return await normalizeBinaryContent(content);
  }
  if (content.type === "group") {
    const items = [];
    for (const item of Array.isArray(content.items) ? content.items : []) {
      items.push({
        id: item && typeof item === "object" ? item.id ?? null : null,
        content: await normalizeContent(item?.content, normalizeBinaryContent, depth + 1),
      });
    }
    return { type: "group", items };
  }
  if (content.type === "read") {
    // spectrum-ts v12.5+ surfaces an inbound read receipt only after it has
    // verified that the target is one of our outbound messages. Keep the
    // receipt lightweight: Python records it as telemetry, never as an agent
    // prompt.
    return {
      type: "read",
      targetMessageId: content.target?.id ?? null,
      targetDirection: content.target?.direction ?? null,
    };
  }
  if (content.type === "reaction") {
    const target = content.target;
    return {
      type: "reaction",
      emoji: content.emoji || "",
      targetMessageId: target?.id ?? null,
      // Lets Python gate "is this a reaction to one of MY messages" without
      // tracking every outbound id. May be null if the provider doesn't
      // hydrate the target — Python falls back to its own sent-id cache.
      targetDirection: target?.direction ?? null,
      // Text of the reacted-to message, so Python can correlate the tapback to
      // the gateway's reply_to_text. Null for attachment/voice-only targets.
      targetText: reactionTargetText(target),
    };
  }
  // A user tapping a poll choice arrives as `poll_option` carrying the chosen
  // option title + whether it was selected (true) or deselected (false). This
  // is how a native iMessage poll's vote streams back — Python turns a
  // selection into the answer that resolves a pending `clarify`.
  if (content.type === "poll_option") {
    return {
      type: "poll_option",
      title: content.option?.title ?? content.title ?? "",
      selected: content.selected !== false,
      pollTitle: content.poll?.title ?? null,
    };
  }
  // The poll message itself (its creation) — surfaced for completeness so the
  // agent isn't told "content type not handled" if it sees the echo.
  if (content.type === "poll") {
    return {
      type: "poll",
      title: content.title ?? "",
      options: Array.isArray(content.options)
        ? content.options.map((o) => o?.title ?? "")
        : [],
    };
  }
  return { type: content.type || "unknown" };
}

