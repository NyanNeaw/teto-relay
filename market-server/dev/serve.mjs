/**
 * Run the Market on this PC: node dev/serve.mjs [port]
 *
 * The same worker.js as on Cloudflare, with its database and files kept in
 * market-server/.local/. Uploads are listed at once (AUTO_APPROVE). Point
 * the app at it with the Market server setting: http://127.0.0.1:8787
 * Only this PC can reach it.
 */
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import worker from "../src/worker.js";
import { d1, r2 } from "./local.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA = process.env.MARKET_DATA || join(HERE, "..", ".local");
const port = parseInt(process.argv[2] || process.env.PORT || "8787", 10);
const env = {
  DB: d1(join(DATA, "market.db")),
  FILES: r2(join(DATA, "files")),
  AUTO_APPROVE: process.env.AUTO_APPROVE ?? "1",
  ADMIN_TOKEN: process.env.ADMIN_TOKEN || "local-admin-token-0000",
  SALT: "local",
};

createServer(async (req, res) => {
  try {
    const url = `http://127.0.0.1:${port}${req.url}`;
    const body = req.method === "GET" || req.method === "HEAD" ? undefined : Readable.toWeb(req);
    const request = new Request(url, { method: req.method, headers: req.headers, body, duplex: "half" });
    const response = await worker.fetch(request, env);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    if (response.body) {
      for await (const chunk of response.body) res.write(chunk);
    }
    res.end();
  } catch (error) {
    console.error(error);
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: false, error: String(error) }));
  }
}).listen(port, "127.0.0.1", () => {
  console.log(`Teto Market on http://127.0.0.1:${port}/v1/catalog  (data in ${DATA})`);
});
