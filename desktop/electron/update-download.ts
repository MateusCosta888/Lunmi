import * as https from "node:https";

const HOSTS = new Set(["github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"]);

/** Redirects are GitHub asset delivery only; the renderer never supplies this URL. */
export function fetchReleaseFile(url: string, redirects = 0): Promise<Buffer> {
  const target = new URL(url);
  if (target.protocol !== "https:" || !HOSTS.has(target.hostname) || target.port || target.username || target.password || redirects > 5) {
    return Promise.reject(new Error("unavailable"));
  }
  return new Promise((resolve, reject) => {
    const request = https.get(target, { headers: { "User-Agent": "Lunmi-Updater" } }, response => {
      if (response.statusCode && response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        void fetchReleaseFile(new URL(response.headers.location, target).href, redirects + 1).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) { response.resume(); reject(new Error("unavailable")); return; }
      const chunks: Buffer[] = [];
      let size = 0;
      response.on("data", (chunk: Buffer) => {
        size += chunk.length;
        if (size > 16_384) { response.destroy(new Error("verification")); return; }
        chunks.push(chunk);
      });
      response.on("error", reject);
      response.on("end", () => resolve(Buffer.concat(chunks)));
    });
    const deadline = setTimeout(() => request.destroy(new Error("unavailable")), 20_000);
    request.on("close", () => clearTimeout(deadline));
    request.on("error", reject);
  });
}
