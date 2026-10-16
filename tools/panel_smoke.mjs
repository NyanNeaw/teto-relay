// Browser smoke test for the control panel.
//
// Start a panel first, then run this with Node and Playwright:
//
//   python -m teto_relay --web --port 8799 --no-browser
//   node tools/panel_smoke.mjs            (PANEL_URL / CHROMIUM override)
//
// It loads the page, runs the setup check, changes a setting and presses Start,
// then prints any failed requests or page errors and saves a screenshot. It
// does not need audio hardware: Start is expected to fail politely on a
// machine without voicebanks or devices, and that message is printed too.
import { chromium } from 'playwright';

const url = process.env.PANEL_URL || 'http://127.0.0.1:8799/';
const browser = await chromium.launch(
  process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const problems = [];
page.on('pageerror', e => problems.push('page error: ' + e.message));
page.on('response', r => { if (r.status() >= 400) problems.push(r.status() + ' ' + r.url()); });

await page.goto(url);
await page.waitForTimeout(1500);
await page.click('#doctor');
await page.waitForFunction(
  () => document.getElementById('checks').textContent.includes('Voicebanks'), null, { timeout: 60000 });
// Settings apply as they change; nudge Transpose up and back with its stepper.
await page.click('button[aria-label="Raise Transpose"]');
await page.waitForTimeout(800);
const saved = await page.textContent('#toast');
await page.click('button[aria-label="Lower Transpose"]');
await page.waitForTimeout(500);
await page.click('#toggle');
await page.waitForTimeout(3000);
const started = await page.textContent('#toast');
await page.screenshot({ path: 'panel_smoke.png', fullPage: true });
console.log(JSON.stringify({ saved, started, problems }, null, 1));
await browser.close();
process.exit(problems.some(p => p.startsWith('page error')) ? 1 : 0);
