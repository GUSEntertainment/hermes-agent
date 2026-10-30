import { basename } from "node:path";

function safeBasename(value) {
  if (typeof value !== "string") return "";
  return basename(value.replace(/\\/g, "/"))
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .replace(/[<>:"|?*]/g, "_")
    .trim();
}

/** Keep the iMessage voice filename aligned with Spectrum's converted M4A bytes. */
export function voiceM4aFilename(name, sourcePath) {
  const candidate = safeBasename(name) || safeBasename(sourcePath);
  const stem = candidate.replace(/\.[^.]*$/, "").replace(/^[. ]+|[. ]+$/g, "").trim();
  return `${stem || "voice"}.m4a`;
}

/** Build Spectrum attachment content, normalizing only native voice filenames. */
export function buildAttachmentContent({ path, name, mimeType, kind }, { attachment, voice }) {
  const options = {};
  if (kind === "voice") {
    options.name = voiceM4aFilename(name, path);
    if (mimeType) options.mimeType = mimeType;
    return voice(path, options);
  }
  if (name) options.name = name;
  if (mimeType) options.mimeType = mimeType;
  return attachment(path, Object.keys(options).length ? options : undefined);
}
