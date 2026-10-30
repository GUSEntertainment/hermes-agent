// Plain-language edge filter for every Photon (iMessage) text send to Chris.
//
// Rules (Chris 2026-10-09): only finished work or one real decision; one to
// three plain sentences leading with the result or the choice; never mention
// cards, IDs, boards, runs, models, retries, handoffs, reviews, paths, hashes;
// suppress progress/status pings and synthetic monitor traffic.
//
// Returns { drop: true, reason } or { drop: false, text }. Ordinary
// conversational replies pass through unchanged unless they carry internal
// jargon; automated notices are rewritten and capped at three sentences.

const DROP_PATTERNS = [
  [/^\s*🔄/u, "status-change"],
  [/^\s*⏱/u, "timeout-retry"],
  [/^\s*✖/u, "worker-crash-retry"],
  [/^\s*👀/u, "ready-for-review"],
  [/^\s*⛔[^\n]*\bhermes kanban\b/iu, "operator-unblock-instructions"],
  [/^\s*🛑[^\n]*review requested changes/iu, "internal-review"],
  [/^\s*🛑[^\n]*for orchestration attention/iu, "orchestration-triage"],
  [/^\s*\[?(?:synthetic|canary|probe|heartbeat|smoke[- ]?test|test ping)\b/i, "synthetic"],
  [/^\s*(?:still working|working on it|progress update|status update|quick update|update:|in progress|picked (?:it )?up|started|starting now|queued|retrying|on it[.!]?\s*$)/i, "progress"],
];

const JARGON_LINE = /^\s*(?:run|run id|task|task id|card|board|model|provider|reviewer|review|handoff|path|paths|hash|digest|sha256|metadata|artifacts?|workspace|profile|fingerprint|session|logs?)\s*[:=]/i;

const SCRUBS = [
  [/\[[a-z0-9]+(?:-[a-z0-9]+)+\](?!\()\s*/gi, ""],     // [board-slug]
  [/(^|\s)@[a-z][a-z0-9_-]*\s+/gi, "$1"],               // @profile
  [/\bKanban\s+t_[0-9a-f]{6,}\b\s*/gi, ""],
  [/\b(?:card|task|run)\s+#?t?_?[0-9a-f]{6,}\b/gi, ""],
  [/\bt_[0-9a-f]{6,}\b/gi, ""],
  [/\brun\s+#?\d{2,}\b/gi, ""],
  [/`[^`\n]*`/g, ""],                                   // inline commands
  [/\b[0-9a-f]{32,}\b/gi, ""],                          // hashes
  [/(^|[\s(])(?:~|\/Users|\/private|\/var|\/opt|\/tmp)\/[^\s)]+/g, "$1"], // file paths (not URLs)
  [/\s*\((?:gpt|claude|gemini|opus|sonnet)[^)]*\)/gi, ""],
  [/\s+—\s+(?=\n|$)/g, ""],
  [/[ \t]{2,}/g, " "],
];

const NOTICE_PREFIX = /^\s*(?:✔|⏸|🛑|⛔|✅|⚠️?|ℹ️?)\s*/u;

function sentences(text) {
  return text.replace(/\s*\n+\s*/g, " ").match(/[^.!?]+[.!?]+["”’)]*|[^.!?]+$/g) || [];
}

const JARGON_SENTENCE = /\b(?:reviews?|reviewer|reviewed|cards?|boards?|kanban|runs?|retr(?:y|ied|ies)|handoffs?|models?|worker|dispatcher|hash|digest|artifacts?|metadata|workspace)\b/i;

function capSentences(text, n = 3) {
  const all = sentences(text).map((s) => s.trim()).filter(Boolean);
  const parts = all.filter((s, i) => i === 0 || !JARGON_SENTENCE.test(s));
  return parts.slice(0, n).join(" ");
}

function rewriteNotice(text) {
  const firstLine = text.split("\n")[0];
  const rest = text.split("\n").slice(1).join("\n").trim();
  let m = firstLine.match(/^\s*✔\s*(?:done\s*)?(?:—\s*)?(.*)$/u);
  if (m) {
    const title = m[1].replace(/^\s*done\s*—\s*/i, "").trim().replace(/[.\s]+$/, "");
    const lead = title ? `Done: ${title}.` : "Done.";
    return rest ? `${lead} ${rest}` : lead;
  }
  m = firstLine.match(/^\s*⏸\s*blocked\s*:?\s*(.*)$/iu);
  if (m) {
    const reason = m[1].trim();
    return reason ? `Need your call: ${reason}` : "";
  }
  m = firstLine.match(/^\s*🛑\s*routed to TRIAGE\s*—\s*needs a human decision[^:]*:?\s*(.*)$/iu);
  if (m) return m[1].trim() ? `Need your call: ${m[1].trim()}` : "";
  return text.replace(NOTICE_PREFIX, "");
}

// t_9fc9391a: URLs are opaque to every rewrite below. Scrubbing a card id or
// splitting "sentences" on the dots inside a link turned
// https://margot.gusentertainment.com/a/<token> into "https://margot.
// gusentertainment." — a dead, untappable approval link.
const URL_IN_TEXT = /https?:\/\/[^\s<>"')\]]+/gi;
const URL_SLOT = (i) => `\u2063URL${i}\u2063`;

export function plainOutbound(input) {
  const original = String(input ?? "");
  for (const [re, reason] of DROP_PATTERNS) {
    if (re.test(original)) return { drop: true, reason };
  }
  const urls = [];
  let text = original.replace(URL_IN_TEXT, (u) => URL_SLOT(urls.push(u) - 1));
  let touched = false;
  const kept = text.split("\n").filter((line) => !JARGON_LINE.test(line));
  if (kept.length !== text.split("\n").length) touched = true;
  text = kept.join("\n");
  for (const [re, rep] of SCRUBS) {
    const next = text.replace(re, rep);
    if (next !== text) { touched = true; text = next; }
  }
  if (NOTICE_PREFIX.test(text)) {
    touched = true;
    text = rewriteNotice(text);
  }
  text = text.replace(/\n{3,}/g, "\n\n").trim();
  if (touched) text = capSentences(text, 3);
  text = text.replace(/\u2063URL(\d+)\u2063/g, (_, i) => urls[Number(i)] ?? "");
  if (!text || !/[a-z0-9]/i.test(text)) return { drop: true, reason: "empty-after-cleanup" };
  return { drop: false, text, changed: text !== original.trim() };
}
