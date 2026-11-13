/**
 * The Worker's Cloudflare bindings, on this PC: D1 over Node's own SQLite
 * (node:sqlite) and R2 over a folder. Enough of each API for worker.js, so
 * the Market runs and is tested here with nothing to install.
 */
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const SCHEMA = readFileSync(join(HERE, "..", "schema.sql"), "utf8");

class Statement {
  constructor(db, sql, args = []) {
    this.db = db;
    this.sql = sql;
    this.args = args;
  }
  bind(...args) {
    return new Statement(this.db, this.sql, args.map((a) => (a === undefined ? null : a)));
  }
  async first() {
    return this.db.prepare(this.sql).get(...this.args) ?? null;
  }
  async all() {
    return { results: this.db.prepare(this.sql).all(...this.args) };
  }
  async run() {
    const info = this.db.prepare(this.sql).run(...this.args);
    return { success: true, meta: { changes: info.changes } };
  }
}

/** A D1 database in `file` (":memory:" for tests), with the schema applied. */
export function d1(file = ":memory:") {
  if (file !== ":memory:") mkdirSync(dirname(file), { recursive: true });
  const db = new DatabaseSync(file);
  db.exec(SCHEMA);
  return { prepare: (sql) => new Statement(db, sql), _db: db };
}

/** An R2 bucket kept as files in `folder` (or in memory with no folder). */
export function r2(folder = null) {
  const memory = new Map();
  const path = (key) => join(folder, ...key.split("/").map((p) => p.replace(/[^a-z0-9.-]/gi, "_")));
  return {
    async put(key, bytes, options = {}) {
      const data = Buffer.from(bytes);
      const type = options.httpMetadata?.contentType || "application/octet-stream";
      if (!folder) { memory.set(key, { data, type }); return; }
      mkdirSync(dirname(path(key)), { recursive: true });
      writeFileSync(path(key), data);
      writeFileSync(`${path(key)}.type`, type);
    },
    async get(key) {
      let found = memory.get(key);
      if (folder && existsSync(path(key))) found = { data: readFileSync(path(key)), type: readFileSync(`${path(key)}.type`, "utf8") };
      if (!found) return null;
      return {
        body: new Blob([found.data]).stream(),
        size: found.data.length,
        httpMetadata: { contentType: found.type },
        async arrayBuffer() { return found.data.buffer.slice(found.data.byteOffset, found.data.byteOffset + found.data.length); },
      };
    },
    async delete(key) {
      memory.delete(key);
      if (folder) { rmSync(path(key), { force: true }); rmSync(`${path(key)}.type`, { force: true }); }
    },
  };
}
