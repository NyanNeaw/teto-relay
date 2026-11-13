/**
 * Accounts: who shared what. An email and a password make an account, and
 * the account's public profile (a @handle, a name, a picture, a few words
 * and links) is what people see on everything it shares.
 *
 * Passwords are never kept: only PBKDF2-SHA256 of them, salted, at the most
 * rounds a Worker allows. Signing in gives a session token (kept by the app);
 * only its SHA-256 is stored, so the database alone cannot sign anyone in.
 * Emails are never shown to anyone. There is no email check yet (that needs
 * a mail sender): an address is taken as given.
 */

export const ROUNDS = 100_000;          // the most PBKDF2 rounds Workers allow
export const SESSION_DAYS = 60;
export const AVATAR_LIMIT = 300 * 1024;
const HANDLE = /^[a-z0-9_]{3,24}$/;
const EMAIL = /^[^\s@]{1,64}@[^\s@]{1,120}\.[^\s@]{2,24}$/;
const RESERVED = new Set(["admin", "teto", "tetorelay", "teto_relay", "official", "support", "market", "moderator", "mod"]);
const CONTROL = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g;

export class Refused extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

export function line(value, limit) {
  if (typeof value !== "string") return "";
  return value.replace(CONTROL, " ").split(/\s+/).filter(Boolean).join(" ").slice(0, limit);
}

export function httpsUrl(value) {
  if (typeof value !== "string" || value.length > 300 || /\s|\\/.test(value)) return "";
  try {
    const u = new URL(value);
    if (u.protocol !== "https:" || u.username || u.password || !u.hostname) return "";
    return u.toString();
  } catch {
    return "";
  }
}

const hex = (bytes) => [...bytes].map((x) => x.toString(16).padStart(2, "0")).join("");
const unhex = (text) => new Uint8Array(text.match(/../g).map((h) => parseInt(h, 16)));

export async function sha256(textOrBytes) {
  const data = typeof textOrBytes === "string" ? new TextEncoder().encode(textOrBytes) : textOrBytes;
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", data)));
}

export function randomKey(bytes = 18) {
  const raw = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...raw)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function hashPassword(password, saltHex) {
  const salt = saltHex ? unhex(saltHex) : crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: ROUNDS }, key, 256);
  return { salt: hex(salt), hash: hex(new Uint8Array(bits)) };
}

function same(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** Counted tries per (hashed) address, for limits: "upload", "signup", "login". */
export async function tooMany(env, ipHash, kind, limit, minutes) {
  const since = new Date(Date.now() - minutes * 60_000).toISOString();
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM attempts WHERE ip_hash = ? AND kind = ? AND created_at > ?")
    .bind(ipHash, kind, since).first();
  return (row?.n || 0) >= limit;
}

export async function counted(env, ipHash, kind) {
  await env.DB.prepare("INSERT INTO attempts (ip_hash, kind, created_at) VALUES (?, ?, ?)")
    .bind(ipHash, kind, new Date().toISOString()).run();
}

async function body(request) {
  try {
    const data = await request.json();
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

async function startSession(env, userId) {
  const token = randomKey(32);
  const now = Date.now();
  await env.DB.prepare("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)")
    .bind(await sha256(token), userId, new Date(now).toISOString(), new Date(now + SESSION_DAYS * 86_400_000).toISOString())
    .run();
  return token;
}

/** The signed-in account sending `request`, or null. */
export async function whoIs(env, request) {
  const header = request.headers.get("Authorization") || "";
  const token = header.replace(/^Bearer\s+/i, "");
  if (!token || token === header || token.length < 20 || token === env.ADMIN_TOKEN) return null;
  const row = await env.DB.prepare(
    `SELECT u.id, u.handle, u.name, u.status FROM sessions s JOIN users u ON u.id = s.user_id
     WHERE s.token_hash = ? AND s.expires_at > ?`
  ).bind(await sha256(token), new Date().toISOString()).first();
  if (!row || row.status !== "active") return null;
  return row;
}

export async function mustBe(env, request) {
  const user = await whoIs(env, request);
  if (!user) throw new Refused("Sign in first.", 401);
  return user;
}

export function profile(row, origin) {
  return {
    handle: row.handle,
    name: row.name,
    bio: row.bio || "",
    links: JSON.parse(row.links || "[]"),
    avatar: row.avatar_type ? `${origin}/v1/users/${row.handle}/avatar?v=${encodeURIComponent(row.updated_at || "")}` : "",
    joined: (row.created_at || "").slice(0, 10),
  };
}

export async function signUp(env, request, ipHash) {
  if (await tooMany(env, ipHash, "signup", 5, 60)) throw new Refused("Too many new accounts from here. Try again later.", 429);
  const b = await body(request);
  const email = line(b.email, 200).toLowerCase();
  const password = typeof b.password === "string" ? b.password : "";
  const handle = line(b.handle, 30).toLowerCase().replace(/^@/, "");
  const name = line(b.name, 40) || handle;
  if (!EMAIL.test(email)) throw new Refused("That email address does not look right.");
  if (password.length < 8 || password.length > 200) throw new Refused("Use a password of at least 8 characters.");
  if (!HANDLE.test(handle)) throw new Refused("Your @name can use a-z, 0-9 and _, 3 to 24 of them.");
  if (RESERVED.has(handle)) throw new Refused("That @name is taken.");
  const taken = await env.DB.prepare("SELECT email, handle FROM users WHERE email = ? OR handle = ?").bind(email, handle).first();
  if (taken) throw new Refused(taken.handle === handle ? "That @name is taken." : "There is already an account with that email: sign in instead.", 409);
  const { salt, hash } = await hashPassword(password);
  const id = randomKey(12);
  const now = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO users (id, email, handle, name, bio, links, avatar_type, pass_salt, pass_hash, status, created_at, updated_at)
     VALUES (?, ?, ?, ?, '', '[]', '', ?, ?, 'active', ?, ?)`
  ).bind(id, email, handle, name, salt, hash, now, now).run();
  await counted(env, ipHash, "signup");
  return { ok: true, token: await startSession(env, id), handle, name };
}

export async function signIn(env, request, ipHash) {
  if (await tooMany(env, ipHash, "login", 10, 15)) throw new Refused("Too many tries. Wait a few minutes and try again.", 429);
  const b = await body(request);
  const email = line(b.email, 200).toLowerCase();
  const password = typeof b.password === "string" ? b.password : "";
  const row = await env.DB.prepare("SELECT id, handle, name, status, pass_salt, pass_hash FROM users WHERE email = ?").bind(email).first();
  // The same work whether or not the account exists, so timing does not tell.
  const { hash } = await hashPassword(password || "-", row ? row.pass_salt : "00".repeat(16));
  if (!row || !same(hash, row.pass_hash)) {
    await counted(env, ipHash, "login");
    throw new Refused("That email and password do not match an account.", 401);
  }
  if (row.status !== "active") throw new Refused("This account has been closed.", 403);
  return { ok: true, token: await startSession(env, row.id), handle: row.handle, name: row.name };
}

export async function signOut(env, request) {
  const token = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (token) await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await sha256(token)).run();
  return { ok: true };
}

export async function me(env, request, origin) {
  const user = await mustBe(env, request);
  const row = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(user.id).first();
  return { ...profile(row, origin), email: row.email };
}

export async function editMe(env, request, origin) {
  const user = await mustBe(env, request);
  const b = await body(request);
  const row = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(user.id).first();
  const name = b.name === undefined ? row.name : line(b.name, 40) || row.handle;
  const bio = b.bio === undefined ? row.bio : line(b.bio, 300);
  let links = JSON.parse(row.links || "[]");
  if (Array.isArray(b.links)) {
    links = b.links.map(httpsUrl).filter(Boolean).slice(0, 4);
    if (b.links.filter(Boolean).length !== links.length) throw new Refused("Links must be https addresses.");
  }
  await env.DB.prepare("UPDATE users SET name = ?, bio = ?, links = ?, updated_at = ? WHERE id = ?")
    .bind(name, bio, JSON.stringify(links), new Date().toISOString(), user.id).run();
  await env.DB.prepare("UPDATE items SET author = ? WHERE user_id = ?").bind(name, user.id).run();
  return me(env, request, origin);
}

export function imageType(bytes) {
  const b = bytes;
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return "image/png";
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) return "image/webp";
  return "";
}

export async function setAvatar(env, request, origin) {
  const user = await mustBe(env, request);
  const length = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (length > AVATAR_LIMIT) throw new Refused("That picture is too big: at most 300 KB.", 413);
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (!bytes.length || bytes.length > AVATAR_LIMIT) throw new Refused("That picture is empty or too big: at most 300 KB.", 413);
  const type = imageType(bytes);
  if (!type) throw new Refused("Use a .png, .jpg or .webp picture.");
  await env.FILES.put(`avatars/${user.id}`, bytes, { httpMetadata: { contentType: type } });
  await env.DB.prepare("UPDATE users SET avatar_type = ?, updated_at = ? WHERE id = ?")
    .bind(type, new Date().toISOString(), user.id).run();
  return me(env, request, origin);
}

/** Close the account: its listings, files and sessions go with it. */
export async function closeAccount(env, request, removeItem) {
  const user = await mustBe(env, request);
  const { results } = await env.DB.prepare("SELECT id FROM items WHERE user_id = ?").bind(user.id).all();
  for (const it of results) await removeItem(env, it.id);
  await env.FILES.delete(`avatars/${user.id}`);
  await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?").bind(user.id).run();
  await env.DB.prepare(
    "UPDATE users SET status = 'closed', email = ?, pass_hash = '', avatar_type = '', bio = '', links = '[]' WHERE id = ?"
  ).bind(`closed-${user.id}`, user.id).run();
  return { ok: true };
}

export async function userRow(env, handle) {
  if (!HANDLE.test(handle || "")) throw new Refused("No such person.", 404);
  const row = await env.DB.prepare("SELECT * FROM users WHERE handle = ? AND status = 'active'").bind(handle).first();
  if (!row) throw new Refused("No such person.", 404);
  return row;
}
