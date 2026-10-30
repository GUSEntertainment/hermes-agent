// Persistent, fail-closed duplicate guard for Photon iMessage sends.
// An intent is written before transport, so ambiguous outcomes cannot trigger
// an automated duplicate for the same logical target and payload.
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const OUTBOUND_INTENT_TTL_MS = 24 * 60 * 60 * 1000;

function hermesHome() {
  return process.env.HERMES_HOME || path.join(os.homedir(), ".hermes");
}
function guardRoot() {
  return path.join(hermesHome(), "state", "imessage-send-guard");
}
function killSwitchActive() {
  const switchPath = path.join(hermesHome(), "state", "imessage-outbound.disabled");
  try { fs.statSync(switchPath); return true; }
  catch (error) {
    if (error?.code === "ENOENT") return false;
    throw new OutboundIntentBlocked("unable to verify automated iMessage kill switch");
  }
}
function pruneExpiredIntents(intents) {
  const cutoff = Date.now() - OUTBOUND_INTENT_TTL_MS * 2;
  let names;
  try { names = fs.readdirSync(intents).filter((name) => name.endsWith(".json")).slice(0, 1000); }
  catch { throw new OutboundIntentBlocked("unable to inspect iMessage intent store"); }
  for (const name of names) {
    const intentPath = path.join(intents, name);
    try { if (fs.statSync(intentPath).mtimeMs < cutoff) fs.unlinkSync(intentPath); }
    catch (error) {
      if (error?.code === "ENOENT") continue;
      throw new OutboundIntentBlocked("unable to prune iMessage intent store");
    }
  }
}
export function normalizeOutboundText(value) {
  return String(value ?? "").replace(/\r\n?/g, "\n").normalize("NFC").trim();
}
export function canonicalOutboundTarget(spaceId) {
  const value = String(spaceId ?? "").trim();
  const dm = value.match(/^(?:any|imessage|sms);-;(.+)$/i);
  if (dm) return `dm:${dm[1].trim().toLowerCase()}`;
  if (value.toLowerCase().startsWith("dm:")) {
    return `dm:${value.slice(3).trim().toLowerCase()}`;
  }
  if (value.startsWith("+") || value.includes("@")) {
    return `dm:${value.toLowerCase()}`;
  }
  return value;
}

export function canonicalOutboundKind(kind) {
  const value = String(kind ?? "").trim().toLowerCase();
  return value === "richlink" ? "text" : value;
}
export function attachmentPayload(filePath, caption = "") {
  const resolved = fs.realpathSync.native(path.resolve(filePath));
  let size = "unknown";
  let mtimeNs = "unknown";
  try {
    const stat = fs.statSync(resolved, { bigint: true });
    size = stat.size.toString(); mtimeNs = stat.mtimeNs.toString();
  } catch { /* sender surfaces unreadable files; keep intent fail-closed */ }
  return `${normalizeOutboundText(caption)}\0file:${resolved}\0size:${size}\0mtime_ns:${mtimeNs}`;
}
export function outboundFingerprint(kind, target, payload, logicalMessageId = null) {
  // logicalMessageId is correlation metadata, never transport identity.  The
  // installed Python hermes-imsg guard cannot observe it, so including it
  // would split one visible send into two duplicate domains.
  void logicalMessageId;
  return crypto.createHash("sha256").update(`v1\0${canonicalOutboundKind(kind)}\0${canonicalOutboundTarget(target)}\0${payload}`, "utf8").digest("hex");
}
export class OutboundIntentBlocked extends Error {
  constructor(reason, fingerprint = null) {
    super(reason); this.name = "OutboundIntentBlocked";
    this.code = "OUTBOUND_INTENT_BLOCKED"; this.fingerprint = fingerprint;
  }
}
export function outboundGuardSnapshot() {
  return { enabled: !killSwitchActive(), ttlSeconds: OUTBOUND_INTENT_TTL_MS / 1000 };
}
export function claimOutboundIntent({ kind, target, payload, logicalMessageId = null, source = "photon-sidecar" }) {
  if (killSwitchActive()) throw new OutboundIntentBlocked("automated iMessage outbound kill switch is active");
  const canonicalTarget = canonicalOutboundTarget(target);
  const fingerprint = outboundFingerprint(kind, canonicalTarget, payload, logicalMessageId);
  const intents = path.join(guardRoot(), "intents");
  fs.mkdirSync(intents, { recursive: true, mode: 0o700 });
  pruneExpiredIntents(intents);
  const now = Date.now();
  const bucket = Math.floor(now / OUTBOUND_INTENT_TTL_MS);
  // Legacy claims used the unsuffixed name. Deterministic TTL buckets make
  // expired replacement a single O_EXCL CAS: no contender ever unlinks a
  // pathname that another contender may just have recreated. The previous
  // bucket preserves a sliding 24-hour TTL across bucket boundaries.
  const candidates = [
    path.join(intents, `${fingerprint}.json`),
    path.join(intents, `${fingerprint}.${bucket}.json`),
    path.join(intents, `${fingerprint}.${bucket - 1}.json`),
  ];
  for (const candidate of candidates) {
    try {
      if (now - fs.statSync(candidate).mtimeMs < OUTBOUND_INTENT_TTL_MS) {
        throw new OutboundIntentBlocked("blocked duplicate iMessage intent for the same thread and payload", fingerprint);
      }
    } catch (error) {
      if (error instanceof OutboundIntentBlocked) throw error;
      if (error?.code !== "ENOENT") throw error;
    }
  }
  const intentPath = path.join(intents, `${fingerprint}.${bucket}.json`);
  let fd;
  try { fd = fs.openSync(intentPath, "wx", 0o600); }
  catch (error) {
    if (error?.code === "EEXIST") throw new OutboundIntentBlocked("blocked duplicate iMessage intent for the same thread and payload", fingerprint);
    throw error;
  }
  try {
    fs.writeFileSync(fd, `${JSON.stringify({ attempted_at: new Date().toISOString(), fingerprint, kind, payload_hash: crypto.createHash("sha256").update(payload, "utf8").digest("hex"), source, target_hash: crypto.createHash("sha256").update(canonicalTarget, "utf8").digest("hex"), version: 1 })}\n`, "utf8");
  } finally { fs.closeSync(fd); }
  return fingerprint;
}
