#!/usr/bin/env node
/* Tests the dashboard's lead-table engine (index.html, between "// <fact-engine>" and "// </fact-engine>").
 *
 * Python (scripts/make_fact_fixture.py) encodes 60,000 random leads with the pipeline's real FactTable; this
 * script inflates and decodes them with the dashboard's own JS and checks every count the tabs rely on against a
 * brute-force count over the raw rows -- for random mixes of all the top-bar filters (Brand, Source, Month, BU,
 * Model, State, IMS) and for 1- to 4-dimension groupings (brand, BU, model, state, city, dealer, source, lead
 * type, month, date, OEM priority tier). Run:  node scripts/test_fact_engine.js [path/to/index.html]   (needs python3)
 */
const fs = require('fs'), zlib = require('zlib'), path = require('path'), cp = require('child_process');
const root = path.join(__dirname, '..');
const fx = JSON.parse(cp.execFileSync('python3', [path.join(__dirname, 'make_fact_fixture.py')], {maxBuffer: 1 << 28}).toString());
let code = fs.readFileSync(path.resolve(process.argv[2] || path.join(root, 'index.html')), 'utf8');
const a = code.indexOf('// <fact-engine>'), b = code.indexOf('// </fact-engine>');
if (a < 0 || b < 0) { console.error('engine markers not found in index.html'); process.exit(2); }
code = code.slice(a, b + '// </fact-engine>'.length);

// the engine reads the top-bar filters from globals, like the page does
const filt = {};
for (const k of ['fBrand', 'fMedium', 'fMonth', 'fBU', 'fModel', 'fState'])
  Object.defineProperty(globalThis, k, { get: () => filt[k] || [], configurable: true });
Object.defineProperty(globalThis, 'fIMS', { get: () => filt.fIMS || '', configurable: true });
const D = { ims_mapping: fx.ims };
const _hasF = x => Array.isArray(x) && x.length > 0;
const E = new Function('D', '_hasF', code + `
  return {fxDecode, fxGroup, fxCounts, fxTotal, fxInvalidate, fxSetPriorities, fxIx, fxDealerName, fxDealerCode, setFX: x => { FX = x; }};`)(D, _hasF);

let fails = 0, checks = 0;
const ok = (c, m) => { checks++; if (!c) { fails++; if (fails < 15) console.log('FAIL', m); } };

const bytes = new Uint8Array(zlib.inflateSync(Buffer.from(fx.fact.data, 'base64')));
const FX = E.fxDecode(fx.fact, bytes);
E.setFX(FX);
E.fxSetPriorities(fx.oem);
const rows = fx.rows;
ok(FX.n === rows.length, 'row count');
ok(E.fxDealerName(fx.fact.dealers[5]) === 'Dealer 5' && E.fxDealerCode(fx.fact.dealers[5]) === 'code5', 'dealer names travel with the table');
ok(E.fxDealerName('nope') === '', 'unknown dealer -> empty name');

const priOf = r => {
  const m = (fx.oem[r[3][0]] || {})[r[3][1]]; if (!m) return 'ROI';
  const c = r[1].toLowerCase();
  for (const [k, o] of Object.entries(m)) if (k.toLowerCase() === c) return o.p || 'ROI';
  return 'ROI';
};
const DIMS = {
  brand: r => r[3][0], bu: r => r[3][1], model: r => r[3][2], product: r => r[3].join(' · '), state: r => r[0], city: r => r[1],
  dealer: r => r[2], medium: r => r[5], lt: r => r[6], month: r => r[4], date: r => r[7], pri: priOf };
function pass(r, f) {
  if (f.fBrand && f.fBrand.length && !f.fBrand.includes(r[3][0])) return false;
  if (f.fBU && f.fBU.length && !f.fBU.includes(r[3][1])) return false;
  if (f.fModel && f.fModel.length && !f.fModel.includes(r[3][2])) return false;
  if (f.fState && f.fState.length && !f.fState.includes(r[0])) return false;
  if (f.fMedium && f.fMedium.length && !f.fMedium.includes(r[5])) return false;
  if (f.fMonth && f.fMonth.length && !f.fMonth.includes(r[4])) return false;
  if (f.fIMS) { const m = fx.ims[r[3][0]]; if (!m || m[r[0]] !== f.fIMS) return false; }
  return true;
}
function brute(dims, f) {
  const m = new Map();
  for (const r of rows) { if (!pass(r, f)) continue; const k = dims.map(d => DIMS[d](r)).join('\u0001'); m.set(k, (m.get(k) || 0) + 1); }
  return m;
}
function engine(dims, f) {
  Object.keys(filt).forEach(k => delete filt[k]); Object.assign(filt, f);
  const g = E.fxGroup(dims), m = new Map(), nd = dims.length;
  for (let c = 0; c < g.counts.length; c++) {
    const v = g.counts[c]; if (!v) continue;
    let r = c; const idx = new Array(nd);
    for (let i = nd - 1; i >= 0; i--) { idx[i] = r % g.sizes[i]; r = (r - idx[i]) / g.sizes[i]; }
    const key = idx.map((x, i) => g.labels[i][x]).join('\u0001');
    m.set(key, (m.get(key) || 0) + v);          // 'ROI' appears as two tiers (unlisted city / a listed ROI city): same label
  }
  return m;
}
const same = (x, y) => { if (x.size !== y.size) return false; for (const [k, v] of x) if (y.get(k) !== v) return false; return true; };

let seed = 12345; const rnd = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
const pick = (arr, k) => [...arr].sort(() => rnd() - .5).slice(0, k);
const uniq = fn => [...new Set(rows.map(fn))];
const allBrands = uniq(r => r[3][0]), allBUs = uniq(r => r[3][1]), allModels = uniq(r => r[3][2]), allStates = uniq(r => r[0]),
  allMed = uniq(r => r[5]), allMonths = uniq(r => r[4]);
const dimSets = [['brand'], ['bu'], ['model'], ['product'], ['state'], ['city'], ['dealer'], ['medium'], ['lt'], ['month'], ['date'], ['pri'],
  ['brand', 'medium'], ['bu', 'month'], ['model', 'state'], ['city', 'medium'], ['dealer', 'month'], ['brand', 'bu', 'model'],
  ['product', 'city', 'month'], ['state', 'medium', 'lt'], ['city', 'state'], ['lt', 'medium'], ['model', 'medium'], ['brand', 'state', 'medium'],
  ['pri', 'date'], ['brand', 'bu', 'pri', 'month'], ['brand', 'bu', 'pri', 'date'], ['state', 'product', 'pri', 'medium'], ['dealer', 'brand'], ['brand', 'bu', 'city']];
const cases = [{}];
for (let i = 0; i < 40; i++) {
  const f = {};
  if (rnd() < .5) f.fBrand = pick(allBrands, 1 + Math.floor(rnd() * 2));
  if (rnd() < .4) f.fBU = pick(allBUs, 1 + Math.floor(rnd() * 2));
  if (rnd() < .4) f.fModel = pick(allModels, 1 + Math.floor(rnd() * 4));
  if (rnd() < .5) f.fState = pick(allStates, 1 + Math.floor(rnd() * 4));
  if (rnd() < .4) f.fMedium = pick(allMed, 1 + Math.floor(rnd() * 3));
  if (rnd() < .5) f.fMonth = pick(allMonths, 1 + Math.floor(rnd() * 3));
  if (rnd() < .2) { f.fIMS = rnd() < .5 ? 'Y' : 'N'; f.fBrand = pick(['Alpha', 'Gamma'], 1 + Math.floor(rnd() * 2)); }
  cases.push(f);
}
for (const f of cases) {
  for (const dims of dimSets) {
    const x = brute(dims, f), y = engine(dims, f);
    ok(same(x, y), 'dims=' + dims.join('+') + ' filters=' + JSON.stringify(f) + ' brute=' + x.size + ' engine=' + y.size);
  }
  Object.keys(filt).forEach(k => delete filt[k]); Object.assign(filt, f); E.fxInvalidate();
  let bt = 0; for (const r of rows) if (pass(r, f)) bt++;
  ok(E.fxTotal() === bt, 'total ' + E.fxTotal() + ' vs ' + bt);
}
// the ignore option leaves a filter out of the count
Object.keys(filt).forEach(k => delete filt[k]); Object.assign(filt, { fMonth: [allMonths[0]], fState: [allStates[0]] }); E.fxInvalidate();
ok(same(new Map(Object.entries(E.fxCounts('medium', { ignore: { month: 1 } }))), brute(['medium'], { fState: [allStates[0]] })), 'ignore month');
// get() reads one cell by label; labels that don't exist read as 0
Object.keys(filt).forEach(k => delete filt[k]); E.fxInvalidate();
const g = E.fxGroup(['brand', 'medium']);
ok(g.get(rows[0][3][0], rows[0][5]) === rows.filter(r => r[3][0] === rows[0][3][0] && r[5] === rows[0][5]).length, 'get()');
ok(g.get('nope', 'Google') === 0, 'get() of a missing label is 0');
console.log(`${checks} checks, ${fails} failed`);
process.exit(fails ? 1 : 0);
