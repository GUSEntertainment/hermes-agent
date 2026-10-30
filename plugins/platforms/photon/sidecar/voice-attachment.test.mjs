import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { tmpdir } from "node:os";

import { buildAttachmentContent } from "./voice-attachment.mjs";

function makeShortMp3(directory) {
  const source = join(directory, "VOICE.MP3");
  execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.05", "-c:a", "libmp3lame", source]);
  return source;
}

function spectrumIMessageUploadFixture(content) {
  const converted = join(tmpdir(), `hermes-spectrum-${process.pid}-${Date.now()}.m4a`);
  try {
    execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-i", content.path, "-c:a", "aac", "-movflags", "+faststart", converted]);
    return { data: readFileSync(converted), fileName: content.name, mimeType: content.mimeType, isAudioMessage: true };
  } finally {
    rmSync(converted, { force: true });
  }
}

test("voice MP3 retains source MIME but uploads playable M4A bytes under an M4A name", () => {
  const directory = mkdtempSync(join(tmpdir(), "hermes-photon-voice-"));
  try {
    const source = makeShortMp3(directory);
    let built;
    const content = buildAttachmentContent(
      { path: source, mimeType: "audio/mpeg", kind: "voice" },
      { voice(path, options) { built = { type: "voice", path, ...options }; return built; }, attachment() { throw new Error("voice requests must not use attachment()"); } },
    );
    assert.equal(content, built);
    assert.equal(built.type, "voice");
    assert.equal(built.mimeType, "audio/mpeg");
    assert.match(built.name, /\.m4a$/i);
    assert.equal(built.name.includes(source), false);
    const upload = spectrumIMessageUploadFixture(built);
    assert.equal(upload.isAudioMessage, true);
    assert.match(upload.fileName, /\.m4a$/i);
    assert.equal(upload.mimeType, "audio/mpeg");
    assert.equal(upload.data.toString("ascii", 4, 8), "ftyp");
    const probe = execFileSync("ffprobe", ["-v", "error", "-show_entries", "stream=codec_name", "-of", "default=nw=1", "-"], { input: upload.data, encoding: "utf8" });
    assert.match(probe, /codec_name=aac/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("voice filename normalization strips paths, normalizes extensions, and falls back safely", () => {
  const seen = [];
  const builders = { voice(path, options) { seen.push({ path, ...options }); return seen.at(-1); }, attachment() { throw new Error("unexpected attachment"); } };
  for (const [name, expected] of [["../../My VOICE.MP3", "My VOICE.m4a"], ["C:\\private\\voice", "voice.m4a"], ["\u0000", "source.m4a"]]) {
    const content = buildAttachmentContent({ path: "/private/cache/source.mp3", name, mimeType: "audio/mpeg", kind: "voice" }, builders);
    assert.equal(content.name, expected);
    assert.equal(content.mimeType, "audio/mpeg");
  }
});

test("ordinary attachments retain their name and MIME unchanged", () => {
  const expected = { type: "attachment" };
  const content = buildAttachmentContent(
    { path: "/private/cache/report.pdf", name: "Q3 report.pdf", mimeType: "application/pdf", kind: "attachment" },
    { voice() { throw new Error("unexpected voice"); }, attachment(path, options) { assert.equal(path, "/private/cache/report.pdf"); assert.deepEqual(options, { name: "Q3 report.pdf", mimeType: "application/pdf" }); return expected; } },
  );
  assert.equal(content, expected);
});

test("voice builder failures leave no sidecar conversion state to clean up", () => {
  assert.throws(
    () => buildAttachmentContent({ path: "/private/cache/broken.mp3", mimeType: "audio/mpeg", kind: "voice" }, { voice() { throw new Error("Spectrum rejected voice"); }, attachment() { throw new Error("unexpected attachment"); } }),
    /Spectrum rejected voice/,
  );
});
