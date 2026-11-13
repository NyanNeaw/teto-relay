/**
 * The Teto Relay Market: a Cloudflare Worker over a D1 database (accounts,
 * listings, likes, reports) and an R2 bucket (the files and pictures).
 *
 * What people share: tunings (.tetostyle), voicebanks (a .zip/.7z/.rar the
 * creator is allowed to share, or a link to its official page), and sounds
 * (one-shots, loops, FX to lay on a Studio lane), each with an optional short
 * demo so it can be heard before it is got.
 *
 * Sharing needs an account (accounts.js), so everything shows who made it.
 * A listing is free (its file is here to download) or has a price: then it
 * is sold from the creator's own shop (Booth, Gumroad, Ko-fi...), linked
 * from the listing, and only its demo is kept here. The Market never takes
 * payment. With AUTO_APPROVE="1" an upload is listed at once and an admin
 * takes down what breaks the rules afterwards; with "0" it waits in a queue
 * until an admin lets it in.
 *
 * Nothing that arrives is trusted: every text is cut to length and stripped
 * of control characters, every file is checked by its first bytes and its
 * size, and the app treats what comes back as data, never as code.
 */

import {
  Refused, line, httpsUrl, sha256, randomKey, tooMany, counted, whoIs, mustBe, profile, userRow,
  signUp, signIn, signOut, me, editMe, setAvatar, closeAccount,
} from "./accounts.js";

export { line, httpsUrl };
export const KINDS = ["tuning", "voicebank", "sound"];
export const LANGUAGES = ["ja", "en", "th", "zh", "ko", "multi", "other", ""];
export const CURRENCIES = ["THB", "USD", "JPY", "EUR"];
export const MAX_PRICE = 1_000_000;
export const LIMITS = {
  tuning: 64_000,            // as presets.MAX_BYTES in the app
  voicebank: 95 * 1024 * 1024, // under the Workers 100 MB request limit
  sound: 20 * 1024 * 1024,
  demo: 3 * 1024 * 1024,
};
export const MAX_UPLOADS_PER_HOUR = 10;
/** Reports from this many people hide a listing until an admin looks. */
export const REPORTS_TO_HIDE = 5;
/** Kasane Teto's voicebank may only be passed on by its official site. */
const OFFICIAL_ONLY = ["kasaneteto", "kasane teto", "重音テト", "重音てと", "かさねてと"];
const ID = /^[a-z0-9][a-z0-9-]{0,47}$/;
const DEVICE = /^[A-Za-z0-9_-]{16,64}$/;

// ----------------------------------------------------------------- helpers
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Edit-Key",
  "Access-Control-Max-Age": "86400",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...CORS },
  });
}

/** Text that may keep its line breaks (a description, a licence). */
export function text(value, limit) {
  if (typeof value !== "string") return "";
  return value
    .split(/\r?\n/)
    .map((l) => line(l, limit))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
    .slice(0, limit);
}

function startsWith(bytes, ...sig) {
  return sig.every((b, i) => b === null || bytes[i] === b);
}

/** What a file is, from its first bytes: "zip", "7z", "rar", "wav", "mp3",
 * "ogg", "flac", or "". */
export function sniff(bytes) {
  const b = bytes;
  if (startsWith(b, 0x50, 0x4b, 0x03, 0x04)) return "zip";
  if (startsWith(b, 0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c)) return "7z";
  if (startsWith(b, 0x52, 0x61, 0x72, 0x21, 0x1a, 0x07)) return "rar";
  if (startsWith(b, 0x52, 0x49, 0x46, 0x46, null, null, null, null, 0x57, 0x41, 0x56, 0x45)) return "wav";
  if (startsWith(b, 0x49, 0x44, 0x33) || (b[0] === 0xff && (b[1] & 0xe0) === 0xe0)) return "mp3";
  if (startsWith(b, 0x4f, 0x67, 0x67, 0x53)) return "ogg";
  if (startsWith(b, 0x66, 0x4c, 0x61, 0x43)) return "flac";
  return "";
}

const TYPES = {
  zip: "application/zip", "7z": "application/x-7z-compressed", rar: "application/vnd.rar",
  wav: "audio/wav", mp3: "audio/mpeg", ogg: "audio/ogg", flac: "audio/flac",
  tetostyle: "application/json",
};

export function slug(name, kind) {
  const base = name
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 36)
    .replace(/-+$/, "");
  const tail = randomKey(4).toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 5).padEnd(5, "0");
  return `${base || kind}-${tail}`;
}

function safeName(name, fallback) {
  const clean = line(name || "", 80).replace(/[\\/:*?"<>|]/g, "_");
  return clean || fallback;
}

async function addressOf(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || request.headers.get("X-Forwarded-For") || "";
  return sha256(`${env.SALT || "teto"}:${ip || "?"}`);
}

// ------------------------------------------------------------ the listings
const PUBLIC = `i.id, i.kind, i.name, i.author, i.description, i.tags, i.language, i.licence, i.official_url,
  i.file_size, i.file_type, i.file_name, i.demo_size, i.demo_type, i.likes, i.downloads, i.created_at, i.bpm,
  i.music_key, i.price, i.currency, i.shop_url, i.status, i.reports, i.user_id,
  u.handle AS user_handle, u.name AS user_name, u.avatar_type AS user_avatar, u.updated_at AS user_updated`;
const FROM = "FROM items i LEFT JOIN users u ON u.id = i.user_id";
/** Listings of closed or banned accounts are not shown. */
const SHOWN = "i.status = 'live' AND (i.user_id = '' OR u.status = 'active')";

export function present(row, origin) {
  const file = row.file_size
    ? { url: `${origin}/v1/files/${row.id}/file`, size: row.file_size, type: row.file_type, name: row.file_name }
    : null;
  const demo = row.demo_size ? { url: `${origin}/v1/files/${row.id}/demo`, size: row.demo_size, type: row.demo_type } : null;
  return {
    id: row.id,
    kind: row.kind,
    name: row.name,
    author: row.user_name || row.author,
    user: row.user_handle
      ? { handle: row.user_handle, name: row.user_name,
          avatar: row.user_avatar ? `${origin}/v1/users/${row.user_handle}/avatar?v=${encodeURIComponent(row.user_updated || "")}` : "" }
      : null,
    description: row.description,
    tags: JSON.parse(row.tags || "[]"),
    language: row.language || "",
    licence: row.licence || "",
    official_url: row.official_url || "",
    price: row.price || 0,
    currency: row.price ? row.currency : "",
    shop_url: row.price ? row.shop_url : "",
    bpm: row.bpm || null,
    key: row.music_key || "",
    file,
    demo,
    likes: row.likes,
    downloads: row.downloads,
    created: row.created_at,
  };
}

async function catalog(env, url) {
  const q = line(url.searchParams.get("q") || "", 60).toLowerCase();
  const kind = url.searchParams.get("kind") || "";
  const lang = url.searchParams.get("lang") || "";
  const sort = url.searchParams.get("sort") || "top";
  const limit = Math.max(1, Math.min(500, parseInt(url.searchParams.get("limit") || "500", 10) || 500));
  const where = [SHOWN];
  const args = [];
  if (KINDS.includes(kind)) { where.push("i.kind = ?"); args.push(kind); }
  if (LANGUAGES.includes(lang) && lang) { where.push("i.language = ?"); args.push(lang); }
  if (q) {
    where.push("(lower(i.name) LIKE ? OR lower(i.author) LIKE ? OR lower(i.description) LIKE ? OR lower(i.tags) LIKE ? OR lower(u.handle) LIKE ?)");
    const like = `%${q.replace(/[%_]/g, "")}%`;
    args.push(like, like, like, like, like);
  }
  const order = { new: "i.created_at DESC", name: "i.name COLLATE NOCASE", top: "i.likes DESC, i.downloads DESC, i.created_at DESC" }[sort]
    || "i.likes DESC, i.downloads DESC, i.created_at DESC";
  const { results } = await env.DB.prepare(`SELECT ${PUBLIC} ${FROM} WHERE ${where.join(" AND ")} ORDER BY ${order} LIMIT ?`)
    .bind(...args, limit)
    .all();
  const origin = url.origin;
  return { format: 2, updated: new Date().toISOString().slice(0, 10), items: results.map((r) => present(r, origin)) };
}

async function item(env, id) {
  if (!ID.test(id || "")) throw new Refused("No such listing.", 404);
  const row = await env.DB.prepare(`SELECT ${PUBLIC}, u.status AS user_status ${FROM} WHERE i.id = ?`).bind(id).first();
  if (!row) throw new Refused("No such listing.", 404);
  row.shown = row.status === "live" && (!row.user_id || row.user_status === "active");
  return row;
}

// ----------------------------------------------------------------- uploads
function checkTuning(bytes) {
  let data;
  try {
    data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes).replace(/^﻿/, ""));
  } catch {
    throw new Refused("That is not a tuning file (.tetostyle).");
  }
  if (!data || typeof data !== "object" || data.format !== 1 || typeof data.name !== "string"
      || !data.settings || typeof data.settings !== "object" || Array.isArray(data.settings)) {
    throw new Refused("That is not a tuning file (.tetostyle).");
  }
  for (const [key, value] of Object.entries(data.settings)) {
    if (!/^[a-z_]{1,40}$/.test(key) || !["number", "string", "boolean"].includes(typeof value)) {
      throw new Refused("That tuning has a setting Teto Relay would not accept.");
    }
  }
  return "tetostyle";
}

async function readFile(entry, limit, what) {
  if (!entry || typeof entry === "string") return null;
  if (entry.size > limit) throw new Refused(`The ${what} is too big: at most ${Math.floor(limit / 1024 / 1024) || 1} MB.`, 413);
  const bytes = new Uint8Array(await entry.arrayBuffer());
  if (!bytes.length) return null;
  if (bytes.length > limit) throw new Refused(`The ${what} is too big.`, 413);
  return { bytes, name: entry.name || "" };
}

export function priceOf(meta) {
  const amount = Number(meta.price || 0);
  if (!Number.isFinite(amount) || amount < 0 || amount > MAX_PRICE) throw new Refused("That price is not a number the Market can show.");
  const price = Math.round(amount * 100) / 100;
  if (!price) return { price: 0, currency: "", shop: "" };
  const currency = CURRENCIES.includes(meta.currency) ? meta.currency : "";
  if (!currency) throw new Refused("Pick the price's currency.");
  const shop = httpsUrl(meta.shop_url || "");
  if (!shop) throw new Refused("A paid item needs the https address of the shop page where people buy it.");
  return { price, currency, shop };
}

export async function upload(request, env) {
  const user = await mustBe(env, request);
  const length = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (length > LIMITS.voicebank + LIMITS.demo + 200_000) throw new Refused("That upload is too big.", 413);
  const ipHash = await addressOf(request, env);
  if (await tooMany(env, ipHash, "upload", MAX_UPLOADS_PER_HOUR, 60)) throw new Refused("Too many uploads in an hour. Try again later.", 429);

  let form;
  try {
    form = await request.formData();
  } catch {
    throw new Refused("The upload could not be read.");
  }
  let meta;
  try {
    meta = JSON.parse(form.get("meta") || "{}");
  } catch {
    throw new Refused("The upload's details could not be read.");
  }
  const kind = meta.kind;
  if (!KINDS.includes(kind)) throw new Refused("Say what it is: a tuning, a voicebank or a sound.");
  const name = line(meta.name, 60);
  if (!name) throw new Refused("Give it a name.");
  const description = text(meta.description, 500);
  const language = LANGUAGES.includes(meta.language) ? meta.language : "";
  const licence = text(meta.licence, 500);
  const official = meta.official_url ? httpsUrl(meta.official_url) : "";
  if (meta.official_url && !official) throw new Refused("The official page must be an https address.");
  const tags = [...new Set((Array.isArray(meta.tags) ? meta.tags : []).map((t) => line(String(t), 20).toLowerCase()).filter(Boolean))].slice(0, 8);
  const bpm = Number.isFinite(+meta.bpm) && +meta.bpm >= 40 && +meta.bpm <= 300 ? Math.round(+meta.bpm) : null;
  const musicKey = line(meta.key, 8);
  const { price, currency, shop } = priceOf(meta);

  const file = await readFile(form.get("file"), LIMITS[kind], kind === "tuning" ? "tuning" : kind === "sound" ? "sound" : "voicebank");
  const demo = await readFile(form.get("demo"), LIMITS.demo, "demo");
  if (price && file) {
    throw new Refused("A paid item is sold from your shop, so its file is not uploaded here: add a demo and your shop link.");
  }
  let fileType = "";
  if (kind === "voicebank") {
    const said = `${name} ${description}`.toLowerCase().replace(/\s+/g, " ");
    if (OFFICIAL_ONLY.some((n) => said.includes(n) || said.replace(/ /g, "").includes(n.replace(/ /g, "")))) {
      throw new Refused("Kasane Teto's voicebank is shared only by its official site: link to kasaneteto.jp instead of uploading it.");
    }
    if (!licence) throw new Refused("Say what people may do with this voicebank (its licence).");
  }
  if (file) {
    if (kind !== "tuning" && meta.permission !== true) throw new Refused("Confirm you made this or are allowed to share it.");
    if (kind === "tuning") fileType = checkTuning(file.bytes);
    else if (kind === "voicebank") {
      fileType = sniff(file.bytes);
      if (!["zip", "7z", "rar"].includes(fileType)) throw new Refused("A voicebank is shared as a .zip, .7z or .rar.");
    } else {
      fileType = sniff(file.bytes);
      if (!["wav", "mp3", "ogg", "flac"].includes(fileType)) throw new Refused("A sound is shared as a .wav, .mp3, .ogg or .flac.");
    }
  } else if (!price) {
    if (kind === "voicebank" && !official) throw new Refused("Add the voicebank's file, its official page, or a price and your shop.");
    if (kind !== "voicebank") throw new Refused(kind === "tuning" ? "Add the tuning file." : "Add the sound file.");
  }
  let demoType = "";
  if (demo) {
    demoType = sniff(demo.bytes);
    if (!["wav", "mp3", "ogg", "flac"].includes(demoType)) throw new Refused("A demo is a .wav, .mp3, .ogg or .flac.");
  }
  if (price && !demo) throw new Refused("A paid item needs a demo, so people can hear what they would buy.");

  const id = slug(name, kind);
  const editKey = randomKey();
  const now = new Date().toISOString();
  const live = env.AUTO_APPROVE === "1";
  if (file) await env.FILES.put(`${id}/file`, file.bytes, { httpMetadata: { contentType: TYPES[fileType] } });
  if (demo) await env.FILES.put(`${id}/demo`, demo.bytes, { httpMetadata: { contentType: TYPES[demoType] } });
  const ext = fileType === "tetostyle" ? ".tetostyle" : `.${fileType}`;
  await env.DB.prepare(
    `INSERT INTO items (id, kind, status, name, author, user_id, description, tags, language, licence, official_url,
      file_size, file_type, file_name, file_sha256, demo_size, demo_type, bpm, music_key, price, currency, shop_url,
      likes, downloads, reports, edit_hash, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)`
  )
    .bind(id, kind, live ? "live" : "pending", name, user.name, user.id, description, JSON.stringify(tags), language, licence,
      official, file ? file.bytes.length : 0, fileType, file ? safeName(file.name, `${id}${ext}`) : "",
      file ? await sha256(file.bytes) : "", demo ? demo.bytes.length : 0, demoType, bpm, musicKey, price, currency, shop,
      await sha256(editKey), now, now)
    .run();
  await counted(env, ipHash, "upload");
  return { ok: true, id, status: live ? "live" : "pending", edit_key: editKey };
}

// ---------------------------------------------------------------- the rest
async function sendFile(env, id, which) {
  const row = await item(env, id);
  if (!row.shown) throw new Refused("No such listing.", 404);
  const object = await env.FILES.get(`${id}/${which}`);
  if (!object) throw new Refused("That file is missing.", 404);
  if (which === "file") {
    await env.DB.prepare("UPDATE items SET downloads = downloads + 1 WHERE id = ?").bind(id).run();
  }
  const headers = { ...CORS, "Content-Type": object.httpMetadata?.contentType || "application/octet-stream",
    "Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff" };
  if (which === "file") {
    headers["Content-Disposition"] = `attachment; filename*=UTF-8''${encodeURIComponent(row.file_name || id)}`;
  }
  return new Response(object.body, { headers });
}

async function avatar(env, handle) {
  const row = await userRow(env, handle);
  const object = row.avatar_type ? await env.FILES.get(`avatars/${row.id}`) : null;
  if (!object) throw new Refused("No picture.", 404);
  return new Response(object.body, { headers: { ...CORS, "Content-Type": row.avatar_type,
    "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff" } });
}

async function person(env, handle, origin) {
  const row = await userRow(env, handle);
  const { results } = await env.DB.prepare(`SELECT ${PUBLIC} ${FROM} WHERE ${SHOWN} AND i.user_id = ? ORDER BY i.created_at DESC`)
    .bind(row.id).all();
  return { ...profile(row, origin), items: results.map((r) => present(r, origin)) };
}

async function mine(env, request, origin) {
  const user = await mustBe(env, request);
  const { results } = await env.DB.prepare(`SELECT ${PUBLIC} ${FROM} WHERE i.user_id = ? AND i.status != 'removed' ORDER BY i.created_at DESC`)
    .bind(user.id).all();
  return { items: results.map((r) => ({ ...present(r, origin), status: r.status })) };
}

async function device(request) {
  let body = {};
  try {
    body = await request.json();
  } catch { /* an empty body is refused below */ }
  if (!DEVICE.test(body.device || "")) throw new Refused("Missing who is asking.");
  return body;
}

async function like(env, id, request) {
  const row = await item(env, id);
  if (!row.shown) throw new Refused("No such listing.", 404);
  const body = await device(request);
  if (body.on === false) {
    await env.DB.prepare("DELETE FROM likes WHERE item_id = ? AND device = ?").bind(id, body.device).run();
  } else {
    await env.DB.prepare("INSERT OR IGNORE INTO likes (item_id, device) VALUES (?, ?)").bind(id, body.device).run();
  }
  const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM likes WHERE item_id = ?").bind(id).first();
  await env.DB.prepare("UPDATE items SET likes = ? WHERE id = ?").bind(n.n, id).run();
  return { ok: true, likes: n.n };
}

async function report(env, id, request) {
  await item(env, id);
  const body = await device(request);
  const reason = text(body.reason, 300);
  await env.DB.prepare("INSERT OR IGNORE INTO reports (item_id, device, reason, created_at) VALUES (?, ?, ?, ?)")
    .bind(id, body.device, reason, new Date().toISOString()).run();
  const n = await env.DB.prepare("SELECT COUNT(*) AS n FROM reports WHERE item_id = ?").bind(id).first();
  await env.DB.prepare("UPDATE items SET reports = ? WHERE id = ?").bind(n.n, id).run();
  if (n.n >= REPORTS_TO_HIDE) {
    await env.DB.prepare("UPDATE items SET status = 'pending' WHERE id = ? AND status = 'live'").bind(id).run();
  }
  return { ok: true };
}

/** Taken down by the account that shared it (or with the edit key). */
async function takeDown(env, id, request) {
  const row = await item(env, id);
  const user = await whoIs(env, request);
  const key = request.headers.get("X-Edit-Key") || "";
  const known = await env.DB.prepare("SELECT edit_hash FROM items WHERE id = ?").bind(id).first();
  const owner = user && row.user_id && user.id === row.user_id;
  if (!owner && !(key && (await sha256(key)) === known.edit_hash)) throw new Refused("Only who shared it can take it down.", 403);
  await remove(env, row.id);
  return { ok: true };
}

async function remove(env, id) {
  await env.FILES.delete(`${id}/file`);
  await env.FILES.delete(`${id}/demo`);
  await env.DB.prepare("UPDATE items SET status = 'removed', file_size = 0, demo_size = 0, updated_at = ? WHERE id = ?")
    .bind(new Date().toISOString(), id).run();
}

function isAdmin(request, env) {
  const token = env.ADMIN_TOKEN || "";
  const sent = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  return token.length >= 16 && sent === token;
}

async function admin(env, request, url, parts) {
  if (!isAdmin(request, env)) throw new Refused("Not allowed.", 403);
  if (request.method === "GET" && parts[2] === "queue") {
    const { results } = await env.DB.prepare(
      `SELECT ${PUBLIC} ${FROM} WHERE i.status = 'pending' OR (i.reports > 0 AND i.status = 'live') ORDER BY i.created_at`
    ).all();
    const reasons = await env.DB.prepare("SELECT item_id, reason FROM reports ORDER BY created_at DESC LIMIT 500").all();
    return { items: results.map((r) => ({ ...present(r, url.origin), status: r.status, reports: r.reports })), reports: reasons.results };
  }
  if (request.method === "POST" && parts[2] === "items" && parts[3]) {
    const row = await item(env, parts[3]);
    const body = await request.json().catch(() => ({}));
    if (body.status === "removed") {
      await remove(env, row.id);
    } else if (["live", "rejected", "pending"].includes(body.status)) {
      await env.DB.prepare("UPDATE items SET status = ?, reports = CASE WHEN ? = 'live' THEN 0 ELSE reports END, updated_at = ? WHERE id = ?")
        .bind(body.status, body.status, new Date().toISOString(), row.id).run();
      if (body.status === "live") await env.DB.prepare("DELETE FROM reports WHERE item_id = ?").bind(row.id).run();
    } else {
      throw new Refused("Say live, rejected, pending or removed.");
    }
    return { ok: true };
  }
  if (request.method === "POST" && parts[2] === "users" && parts[3]) {
    const row = await userRow(env, parts[3]).catch(async () => {
      const banned = await env.DB.prepare("SELECT * FROM users WHERE handle = ?").bind(parts[3]).first();
      if (!banned) throw new Refused("No such person.", 404);
      return banned;
    });
    const body = await request.json().catch(() => ({}));
    if (!["active", "banned"].includes(body.status)) throw new Refused("Say active or banned.");
    await env.DB.prepare("UPDATE users SET status = ? WHERE id = ?").bind(body.status, row.id).run();
    if (body.status === "banned") await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(row.id).run();
    return { ok: true };
  }
  throw new Refused("Not found.", 404);
}

// ------------------------------------------------------------------ router
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);   // ["v1", ...]
    const method = request.method;
    if (method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    try {
      if (parts[0] !== "v1") throw new Refused("Not found.", 404);
      const [, what, id, action] = parts;
      if (method === "GET" && what === "health") return json({ ok: true });
      if (method === "GET" && what === "catalog") return json(await catalog(env, url));
      if (what === "auth" && method === "POST") {
        if (id === "signup") return json(await signUp(env, request, await addressOf(request, env)), 201);
        if (id === "login") return json(await signIn(env, request, await addressOf(request, env)));
        if (id === "logout") return json(await signOut(env, request));
      }
      if (what === "me") {
        if (!id && method === "GET") return json(await me(env, request, url.origin));
        if (!id && method === "POST") return json(await editMe(env, request, url.origin));
        if (!id && method === "DELETE") return json(await closeAccount(env, request, remove));
        if (id === "avatar" && method === "POST") return json(await setAvatar(env, request, url.origin));
        if (id === "items" && method === "GET") return json(await mine(env, request, url.origin));
      }
      if (what === "users" && id && method === "GET") {
        if (action === "avatar") return await avatar(env, id);
        if (!action) return json(await person(env, id, url.origin));
      }
      if (what === "items" && !id && method === "POST") return json(await upload(request, env), 201);
      if (what === "items" && id && !action && method === "GET") {
        const row = await item(env, id);
        if (!row.shown) throw new Refused("No such listing.", 404);
        return json(present(row, url.origin));
      }
      if (what === "items" && id && !action && method === "DELETE") return json(await takeDown(env, id, request));
      if (what === "items" && action === "like" && method === "POST") return json(await like(env, id, request));
      if (what === "items" && action === "report" && method === "POST") return json(await report(env, id, request));
      if (what === "files" && (action === "file" || action === "demo") && method === "GET") {
        return await sendFile(env, id, action);
      }
      if (what === "admin") return json(await admin(env, request, url, parts));
      throw new Refused("Not found.", 404);
    } catch (error) {
      if (error instanceof Refused) return json({ ok: false, error: error.message }, error.status);
      console.error(error);
      return json({ ok: false, error: "The Market had a problem. Try again." }, 500);
    }
  },
};
