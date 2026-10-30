// Margot-owned links share native card delivery. External publishers retain
// their own branding. Never fetch protected content to construct a caption.
export function secureLinkContent(message, builders) {
  const links = [...message.matchAll(/https?:\/\/[^\s<>"')\]]+/gi)];
  if (!links.length || links.length > 8) return null;
  const urls = [...new Set(links.map(match => match[0].replace(/[.,;!?]+$/, "")))];
  for (const url of urls) {
    let parsed;
    try { parsed = new URL(url); } catch { return null; }
    const secureForm = parsed.origin === "https://secure.gusentertainment.com" &&
      /^\/secure\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\/?$/i.test(parsed.pathname) && !parsed.search && !parsed.hash;
    const margotApp = parsed.origin === "https://margot.gusentertainment.com" &&
      !/^\/api(?:\/|$)/i.test(parsed.pathname);
    if ((!secureForm && !margotApp) || parsed.username || parsed.password) return null;
  }
  let caption = message;
  for (const url of urls) {
    const escaped = url.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    caption = caption.replace(new RegExp(`\\[([^\\]\\n]+)\\]\\(${escaped}\\)`, "g"), "$1")
      .split(url).join("");
  }
  const cards = urls.map(url => builders.richlink(url));
  return caption.trim() ? [builders.text(caption.trim()), ...cards] : cards;
}
