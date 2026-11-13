# Teto Relay Market server

Where people share **tunings** (.tetostyle), **voicebanks** and **sounds**
(one-shots, loops, FX for Studio lanes), each with an optional short demo.
One Cloudflare Worker (`src/worker.js`) over a D1 database (listings, likes,
reports) and an R2 bucket (the files). Free tier is enough for a small
community.

The app talks to it when **Settings → Setup → Market server** has its address.
Empty, the app shows the built-in list as before.

## Accounts and prices

- Sharing needs an account: email + password, and a public profile (@name,
  shown name, picture, a few words, links). Everything shared shows who made
  it; each creator has a page. Emails are never shown. Passwords are stored
  only as salted PBKDF2-SHA256; sessions only as hashes. There is no email
  check yet (it needs a mail sender).
- A listing is **free** (its file is downloaded from here) or has a **price**:
  then it is sold from the creator's own shop (Booth, Gumroad, Ko-fi…) linked
  on the listing, only its demo is kept here, and **the Market never takes
  payment**.
- Admins can ban an account (`tools/market_admin.py ban @name`): its listings
  are hidden and it is signed out. Anyone can close their own account.

## Rules it enforces

- Uploads are listed at once (`AUTO_APPROVE = "1"`). What breaks the rules is
  taken down afterwards: `tools/market_admin.py remove <id>`, or
  `ban @name` for everything an account shared. Set it to `"0"` to hold every
  upload for review instead.
- Files are checked by their first bytes, not their names: tunings must be a
  `.tetostyle`, voicebanks `.zip/.7z/.rar` (≤ 95 MB), sounds and demos
  `.wav/.mp3/.ogg/.flac` (≤ 20 MB and ≤ 3 MB).
- A voicebank needs a licence text, and a file upload needs the uploader to
  confirm they made it or may share it.
- **Kasane Teto's voicebank is refused**: it may only be passed on by its
  official site, so it can only be listed as a link to that site.
- 10 uploads an hour per address; 5 reports hide a listing until reviewed.
- Only who shared something (signed in) can take it down; the app also
  keeps an edit key for each upload in `market.json`.

## On this PC (no account needed)

Needs Node 22.5 or newer (it uses Node's built-in SQLite); nothing to install.

```bash
cd market-server
node dev/serve.mjs 8787
```

Then set the app's Market server to `http://127.0.0.1:8787` and make an
account from the Market's Sign in button (it lives only on this PC). Data is kept in
`market-server/.local/`. Uploads are listed at once here (AUTO_APPROVE).

Tests: `node --test` in this folder.

## Putting it online (Cloudflare)

You need a free Cloudflare account. These steps create things in your
account; run them yourself.

```bash
cd market-server
npm install --save-dev wrangler
npx wrangler login
copy wrangler.toml wrangler.local.toml      # your own copy; git ignores it
npx wrangler d1 create teto-market          # put the database_id in wrangler.local.toml
npx wrangler r2 bucket create teto-market-files
npx wrangler d1 execute teto-market --remote --file=schema.sql -c wrangler.local.toml
npx wrangler secret put ADMIN_TOKEN -c wrangler.local.toml   # a long random string; keep it private
npx wrangler secret put SALT -c wrangler.local.toml          # any random text
npx wrangler deploy -c wrangler.local.toml
```

Your own server's address and database id stay in `wrangler.local.toml`
and your app's `config.json`, never in the shared code. `wrangler deploy` prints the address (`https://teto-market.<you>.workers.dev`).
Put it in the app's Market server setting, and use it with the admin tool:

```bash
set MARKET_URL=https://teto-market.<you>.workers.dev
set MARKET_ADMIN_TOKEN=<the ADMIN_TOKEN you set>
python tools/market_admin.py queue
python tools/market_admin.py live <id>
```

Before opening it to everyone: write the Market's rules and a takedown
contact (for copyright complaints), and decide who takes things down.
