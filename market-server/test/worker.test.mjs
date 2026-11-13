// node --test   (from market-server/)
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { sniff, slug, line } from "../src/worker.js";
import { d1, r2 } from "../dev/local.mjs";

const ORIGIN = "http://market.test";
const ADMIN = "an-admin-token-for-tests";

function market(extra = {}) {
  return { DB: d1(), FILES: r2(), ADMIN_TOKEN: ADMIN, SALT: "t", ...extra };
}

function call(env, method, path, { body, headers = {}, token } = {}) {
  if (token) headers = { ...headers, Authorization: `Bearer ${token}` };
  return worker.fetch(new Request(ORIGIN + path, { method, body, headers }), env);
}

async function signUp(env, handle = "mika", ip = "5.5.5.5") {
  const r = await call(env, "POST", "/v1/auth/signup", {
    body: JSON.stringify({ email: `${handle}@example.com`, password: "a good password", handle, name: handle.toUpperCase() }),
    headers: { "CF-Connecting-IP": ip },
  });
  const body = await r.json();
  assert.equal(r.status, 201, JSON.stringify(body));
  return body.token;
}

const TUNING = JSON.stringify({ format: 1, name: "Soft", author: "me", description: "", settings: { fx_reverb: 0.3, legato: true } });
const WAV = (() => {
  const b = new Uint8Array(64);
  b.set([0x52, 0x49, 0x46, 0x46], 0);
  b.set([0x57, 0x41, 0x56, 0x45], 8);
  return b;
})();
const ZIP = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3, 4]);
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0]);

function form(meta, file, demo) {
  const f = new FormData();
  f.set("meta", JSON.stringify(meta));
  if (file) f.set("file", new Blob([file.bytes]), file.name);
  if (demo) f.set("demo", new Blob([demo]), "demo.wav");
  return f;
}

async function upload(env, token, meta, file, demo, ip = "1.2.3.4") {
  const r = await call(env, "POST", "/v1/items", { body: form(meta, file, demo), headers: { "CF-Connecting-IP": ip }, token });
  return { status: r.status, body: await r.json() };
}

test("sharing needs an account, and shows who shared it", async () => {
  const env = market();
  const anon = await upload(env, null, { kind: "sound", name: "Kick", permission: true }, { bytes: WAV, name: "k.wav" });
  assert.equal(anon.status, 401);
  const token = await signUp(env);
  const up = await upload(env, token, { kind: "tuning", name: "Soft voice", tags: ["Soft"] },
    { bytes: new TextEncoder().encode(TUNING), name: "soft.tetostyle" }, WAV);
  assert.equal(up.status, 201);
  assert.equal(up.body.status, "pending");
  assert.match(up.body.id, /^soft-voice-[a-z0-9]{5}$/);
  assert.equal((await (await call(env, "GET", "/v1/catalog")).json()).items.length, 0, "not listed before review");

  const queue = await (await call(env, "GET", "/v1/admin/queue", { token: ADMIN })).json();
  assert.equal(queue.items[0].user.handle, "mika");
  assert.equal((await call(env, "POST", `/v1/admin/items/${up.body.id}`, { body: JSON.stringify({ status: "live" }), token: ADMIN })).status, 200);

  const list = await (await call(env, "GET", "/v1/catalog?kind=tuning")).json();
  const it = list.items[0];
  assert.equal(it.author, "MIKA");
  assert.deepEqual(it.user, { handle: "mika", name: "MIKA", avatar: "" });
  assert.equal(it.price, 0);
  assert.equal(it.file.url, `${ORIGIN}/v1/files/${it.id}/file`);
  assert.equal(await (await call(env, "GET", `/v1/files/${it.id}/file`)).text(), TUNING);
  assert.equal((await (await call(env, "GET", `/v1/items/${it.id}`)).json()).downloads, 1);

  const page = await (await call(env, "GET", "/v1/users/mika")).json();
  assert.equal(page.name, "MIKA");
  assert.equal(page.items.length, 1);
  assert.equal(page.email, undefined, "an email is never shown");
});

test("accounts: a taken @name or email, a wrong password, a session that ends", async () => {
  const env = market();
  const token = await signUp(env);
  const again = await call(env, "POST", "/v1/auth/signup", {
    body: JSON.stringify({ email: "other@example.com", password: "a good password", handle: "mika" }) });
  assert.equal(again.status, 409);
  const short = await call(env, "POST", "/v1/auth/signup", {
    body: JSON.stringify({ email: "x@example.com", password: "short", handle: "xx_yy" }) });
  assert.equal(short.status, 400);
  const wrong = await call(env, "POST", "/v1/auth/login", {
    body: JSON.stringify({ email: "mika@example.com", password: "not it" }), headers: { "CF-Connecting-IP": "7.7.7.7" } });
  assert.equal(wrong.status, 401);
  const right = await (await call(env, "POST", "/v1/auth/login", {
    body: JSON.stringify({ email: "MIKA@example.com", password: "a good password" }) })).json();
  assert.equal(right.handle, "mika");
  assert.equal((await (await call(env, "GET", "/v1/me", { token: right.token })).json()).email, "mika@example.com");
  await call(env, "POST", "/v1/auth/logout", { token });
  assert.equal((await call(env, "GET", "/v1/me", { token })).status, 401);
  for (let n = 0; n < 10; n++) {
    await call(env, "POST", "/v1/auth/login", { body: JSON.stringify({ email: "mika@example.com", password: "x" }), headers: { "CF-Connecting-IP": "8.8.8.8" } });
  }
  const locked = await call(env, "POST", "/v1/auth/login", {
    body: JSON.stringify({ email: "mika@example.com", password: "a good password" }), headers: { "CF-Connecting-IP": "8.8.8.8" } });
  assert.equal(locked.status, 429);
});

test("a profile: name, bio, links and a picture", async () => {
  const env = market();
  const token = await signUp(env);
  const edited = await (await call(env, "POST", "/v1/me", { token,
    body: JSON.stringify({ name: "Mika ✿", bio: "Thai UTAU covers", links: ["https://x.example/mika"] }) })).json();
  assert.equal(edited.name, "Mika ✿");
  assert.deepEqual(edited.links, ["https://x.example/mika"]);
  assert.equal((await call(env, "POST", "/v1/me", { token, body: JSON.stringify({ links: ["javascript:alert(1)"] }) })).status, 400);
  assert.equal((await call(env, "POST", "/v1/me/avatar", { token, body: new TextEncoder().encode("<svg>") })).status, 400);
  const pic = await (await call(env, "POST", "/v1/me/avatar", { token, body: PNG })).json();
  assert.match(pic.avatar, /\/v1\/users\/mika\/avatar\?v=/);
  const got = await call(env, "GET", "/v1/users/mika/avatar");
  assert.equal(got.headers.get("Content-Type"), "image/png");
});

test("a price: sold from the creator's shop, with a demo and no file here", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const token = await signUp(env);
  const withFile = await upload(env, token, { kind: "sound", name: "Pack", permission: true, price: 150, currency: "THB",
    shop_url: "https://mika.booth.pm/items/1" }, { bytes: WAV, name: "p.wav" }, WAV);
  assert.equal(withFile.status, 400);
  const noShop = await upload(env, token, { kind: "sound", name: "Pack", price: 150, currency: "THB" }, null, WAV);
  assert.equal(noShop.status, 400);
  const noDemo = await upload(env, token, { kind: "sound", name: "Pack", price: 150, currency: "THB",
    shop_url: "https://mika.booth.pm/items/1" });
  assert.equal(noDemo.status, 400);
  const paid = await upload(env, token, { kind: "voicebank", name: "Mai Pro", licence: "Commercial OK", language: "th",
    price: 150, currency: "THB", shop_url: "https://mika.booth.pm/items/1" }, null, WAV);
  assert.equal(paid.status, 201, JSON.stringify(paid.body));
  const it = (await (await call(env, "GET", "/v1/catalog")).json()).items[0];
  assert.deepEqual([it.price, it.currency, it.shop_url, it.file], [150, "THB", "https://mika.booth.pm/items/1", null]);
  assert.equal(it.demo.type, "wav");
  const tuning = await upload(env, token, { kind: "tuning", name: "Pro tuning", price: 2.5, currency: "USD",
    shop_url: "https://mika.gumroad.com/l/x" }, null, WAV);
  assert.equal(tuning.status, 201);
});

test("files are checked by what they are, not what they are called", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const token = await signUp(env);
  assert.equal((await upload(env, token, { kind: "sound", name: "Kick", permission: true },
    { bytes: new TextEncoder().encode("MZ not audio"), name: "kick.wav" })).status, 400);
  assert.equal((await upload(env, token, { kind: "tuning", name: "X" }, { bytes: new TextEncoder().encode("{}"), name: "x.tetostyle" })).status, 400);
  const sound = await upload(env, token, { kind: "sound", name: "Kick", permission: true, tags: ["drums"], bpm: 120 }, { bytes: WAV, name: "kick.wav" });
  assert.equal(sound.body.status, "live");
  assert.equal(sniff(ZIP), "zip");
  assert.equal(sniff(WAV), "wav");
});

test("a voicebank needs a licence and permission, and Kasane Teto only links to her official site", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const token = await signUp(env);
  assert.equal((await upload(env, token, { kind: "voicebank", name: "Mai", permission: true }, { bytes: ZIP, name: "mai.zip" })).status, 400);
  assert.equal((await upload(env, token, { kind: "voicebank", name: "Mai", licence: "Free" }, { bytes: ZIP, name: "mai.zip" })).status, 400);
  const teto = await upload(env, token, { kind: "voicebank", name: "Kasane Teto (重音テト)", licence: "x", permission: true },
    { bytes: ZIP, name: "teto.zip" });
  assert.equal(teto.status, 400);
  assert.match(teto.body.error, /official/);
  assert.equal((await upload(env, token, { kind: "voicebank", name: "Mai ไทย", licence: "Free for covers", permission: true, language: "th" },
    { bytes: ZIP, name: "mai.zip" })).status, 201);
  assert.equal((await upload(env, token, { kind: "voicebank", name: "Link only", licence: "See site", official_url: "https://example.com/vb" })).status, 201);
  const thaiOnly = await (await call(env, "GET", "/v1/catalog?kind=voicebank&lang=th")).json();
  assert.deepEqual(thaiOnly.items.map((i) => i.name), ["Mai ไทย"]);
});

test("likes count once per device, and enough reports hide a listing", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const token = await signUp(env);
  const { body } = await upload(env, token, { kind: "sound", name: "Snare", permission: true }, { bytes: WAV, name: "s.wav" });
  const like = (device, on = true) => call(env, "POST", `/v1/items/${body.id}/like`, { body: JSON.stringify({ device, on }) });
  await like("device-aaaaaaaaaaaaaaaa");
  assert.equal((await (await like("device-aaaaaaaaaaaaaaaa")).json()).likes, 1);
  assert.equal((await (await like("device-bbbbbbbbbbbbbbbb")).json()).likes, 2);
  assert.equal((await (await like("device-bbbbbbbbbbbbbbbb", false)).json()).likes, 1);
  assert.equal((await like("x")).status, 400);
  for (let n = 0; n < 5; n++) {
    await call(env, "POST", `/v1/items/${body.id}/report`, { body: JSON.stringify({ device: `reporter-${n}-aaaaaaaaaaaa`, reason: "spam" }) });
  }
  assert.equal((await (await call(env, "GET", "/v1/catalog")).json()).items.length, 0);
});

test("only who shared it takes it down; a banned or closed account's listings go", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const mika = await signUp(env, "mika");
  const other = await signUp(env, "other_one");
  const { body } = await upload(env, mika, { kind: "sound", name: "Clap", permission: true }, { bytes: WAV, name: "c.wav" });
  assert.equal((await call(env, "DELETE", `/v1/items/${body.id}`, { token: other })).status, 403);
  assert.equal((await call(env, "DELETE", `/v1/items/${body.id}`, { token: mika })).status, 200);
  assert.equal((await call(env, "GET", `/v1/files/${body.id}/file`)).status, 404);

  await upload(env, other, { kind: "sound", name: "Hat", permission: true }, { bytes: WAV, name: "h.wav" });
  assert.equal((await (await call(env, "GET", "/v1/catalog")).json()).items.length, 1);
  await call(env, "POST", "/v1/admin/users/other_one", { body: JSON.stringify({ status: "banned" }), token: ADMIN });
  assert.equal((await (await call(env, "GET", "/v1/catalog")).json()).items.length, 0);
  assert.equal((await call(env, "GET", "/v1/me", { token: other })).status, 401);

  await upload(env, mika, { kind: "sound", name: "Tom", permission: true }, { bytes: WAV, name: "t.wav" });
  assert.equal((await call(env, "DELETE", "/v1/me", { token: mika })).status, 200);
  assert.equal((await (await call(env, "GET", "/v1/catalog")).json()).items.length, 0);
  assert.equal((await call(env, "POST", "/v1/auth/login", { body: JSON.stringify({ email: "mika@example.com", password: "a good password" }) })).status, 401);
});

test("uploads are limited per hour per address", async () => {
  const env = market({ AUTO_APPROVE: "1" });
  const token = await signUp(env);
  for (let n = 0; n < 10; n++) {
    assert.equal((await upload(env, token, { kind: "sound", name: `S${n}`, permission: true }, { bytes: WAV, name: "s.wav" }, null, "9.9.9.9")).status, 201);
  }
  assert.equal((await upload(env, token, { kind: "sound", name: "S11", permission: true }, { bytes: WAV, name: "s.wav" }, null, "9.9.9.9")).status, 429);
  assert.equal((await upload(env, token, { kind: "sound", name: "Other", permission: true }, { bytes: WAV, name: "s.wav" }, null, "8.8.8.8")).status, 201);
});

test("texts are cleaned and ids are safe", () => {
  assert.equal(line("  a\u0000b\n\nc  ", 10), "a b c");
  assert.match(slug("重音", "voicebank"), /^voicebank-[a-z0-9]{5}$/);
  assert.match(slug("Hello World!", "sound"), /^hello-world-[a-z0-9]{5}$/);
});
