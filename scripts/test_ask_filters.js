// Regression test for how the Ask vs Actual tab applies the top-bar filters.
//
//     node scripts/test_ask_filters.js
//
// Pulls the real filter-aware Actuals code out of index.html (so it tests what ships, not a
// copy) and runs it against a small synthetic dashboard-data object, checking every figure the
// tab can show against a brute-force count of the same cells. No browser, network or
// credentials needed. Exit code is non-zero if any check fails.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
function pick(startMarker, endMarker) {
  const a = html.indexOf(startMarker);
  if (a < 0) throw new Error('marker not found in index.html: ' + startMarker);
  const b = html.indexOf(endMarker, a + startMarker.length);
  if (b < 0) throw new Error('end marker not found in index.html: ' + endMarker);
  return html.slice(a, b);
}
const code = [
  pick('function _hasF(arr)', '\n'),
  // variants table, _askResolveVariants, _askModelActual, _askBrandOtherModels and the whole
  // filter-aware provider (cube index, aggregation, legacy + cube providers, filter notes)
  pick('const _ASK_VARIANTS = {', "// Some brands enter a single lump 'Paid' figure"),
  pick('function _askModelDayActual(', 'function _askModelDailyTable('),
  ';globalThis.__api={_askActuals,_askUseCube,_askNoAch,_askFilterNote,_askFilterText,_askCubeIdx,_askExtraFilters};',
].join('\n');

// ── synthetic data ────────────────────────────────────────────────────────────────────────
// brand TVS: TVS iQube (Ask row folding TVS iQube / iQube S / iQube ST), TVS Raider (Ask row),
// TVS Star City Plus (no Ask row). brand Hero: Hero Glamour (Ask row). BUs: TVS iQube* -> UB.
const MODELS = ['Hero Glamour', 'TVS Raider', 'TVS Star City Plus', 'TVS iQube', 'TVS iQube S', 'TVS iQube ST'];
const BRAND = {'Hero Glamour': 'Hero', 'TVS Raider': 'TVS', 'TVS Star City Plus': 'TVS', 'TVS iQube': 'TVS', 'TVS iQube S': 'TVS', 'TVS iQube ST': 'TVS'};
const BU = {'Hero Glamour': 'Hero', 'TVS Raider': 'TVS', 'TVS Star City Plus': 'TVS', 'TVS iQube': 'UB', 'TVS iQube S': 'UB', 'TVS iQube ST': 'UB'};
const STATES = ['Bihar', 'Karnataka', 'Maharashtra', 'Unknown'];
const MEDIA = ['Facebook', 'Google', 'Non-MS', 'Organic', 'Whatsapp'];
const MONTHS = ["Sep'2026", "Oct'2026"];
const DATES = ['', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03'];

// deterministic pseudo-random cells: [model, state, medium, month, date, leads]
let seed = 12345;
const rnd = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
const cells = [];
for (const m of MODELS) for (const s of STATES) for (const e of MEDIA) for (const mo of MONTHS) for (let d = 0; d < DATES.length; d++) {
  if (rnd(3) === 0) continue;                       // sparse
  if (mo === "Oct'2026" && DATES[d] === '2026-09-30' && rnd(2)) continue;
  cells.push([m, s, e, mo, DATES[d], 1 + rnd(9)]);
}
// grouped + delta-coded exactly like build_aggregations() writes it
const mi = Object.fromEntries(MODELS.map((v, i) => [v, i])), si = Object.fromEntries(STATES.map((v, i) => [v, i]));
const ei = Object.fromEntries(MEDIA.map((v, i) => [v, i])), oi = Object.fromEntries(MONTHS.map((v, i) => [v, i]));
const di = Object.fromEntries(DATES.map((v, i) => [v, i]));
const groups = new Map();
for (const c of cells.slice().sort((a, b) => (a[0] + a[1] + a[2] + a[3] + a[4]).localeCompare(b[0] + b[1] + b[2] + b[3] + b[4]))) {
  const k = [mi[c[0]], si[c[1]], ei[c[2]], oi[c[3]]].join(',');
  if (!groups.has(k)) groups.set(k, []);
  groups.get(k).push([di[c[4]], c[5]]);
}
const rows = [];
for (const [k, list] of groups) {
  list.sort((a, b) => a[0] - b[0]);
  rows.push(...k.split(',').map(Number), list.length);
  let prev = 0;
  for (const [d, n] of list) { rows.push(d - prev, n); prev = d; }
}

// legacy pre-aggregates, derived from the same cells (what the dashboard had before the cube)
const sum = (pred, f = (c) => c[5]) => cells.filter(pred).reduce((s, c) => s + f(c), 0);
const D = {
  ask_cube: {models: MODELS, states: STATES, mediums: MEDIA, months: MONTHS, dates: DATES, rows},
  model_bu: BU,
  model_brand: Object.fromEntries(MODELS.map((m) => [m, {[BRAND[m]]: 1}])),
  by_brand: {TVS: 1, Hero: 1},
  brand_month: {}, brand_medium_month: {}, model_month: {}, model_medium_month: {}, model_date: {}, model_medium_date: {},
  ask_data: {
    "Oct'2026": {
      TVS:  {'TVS iQube': {t: 1000, o: 400, g: 200, f: 300, p: 0}, 'TVS Raider': {t: 800, o: 300, g: 200, f: 300, p: 0}},
      Hero: {'Hero Glamour': {t: 500, o: 200, g: 100, f: 200, p: 0}},
    },
  },
};
for (const b of ['TVS', 'Hero']) {
  D.brand_month[b] = {}; D.brand_medium_month[b] = {};
  for (const mo of MONTHS) {
    D.brand_month[b][mo] = sum((c) => BRAND[c[0]] === b && c[3] === mo);
    for (const e of MEDIA) {
      D.brand_medium_month[b][e] = D.brand_medium_month[b][e] || {};
      D.brand_medium_month[b][e][mo] = sum((c) => BRAND[c[0]] === b && c[3] === mo && c[2] === e);
    }
  }
}
for (const m of MODELS) {
  D.model_month[m] = {}; D.model_medium_month[m] = {}; D.model_date[m] = {}; D.model_medium_date[m] = {};
  for (const mo of MONTHS) D.model_month[m][mo] = sum((c) => c[0] === m && c[3] === mo);
  for (const e of MEDIA) {
    D.model_medium_month[m][e] = {};
    D.model_medium_date[m][e] = {};
    for (const mo of MONTHS) D.model_medium_month[m][e][mo] = sum((c) => c[0] === m && c[2] === e && c[3] === mo);
    for (const dt of DATES.filter(Boolean)) {
      const v = sum((c) => c[0] === m && c[2] === e && c[4] === dt);
      if (v) { D.model_medium_date[m][e][dt] = v; }
    }
  }
  for (const dt of DATES.filter(Boolean)) { const v = sum((c) => c[0] === m && c[4] === dt); if (v) D.model_date[m][dt] = v; }
}

const ctx = {console, D, fState: [], fBU: [], fModel: [], fMedium: [], fBrand: [], fMonth: [], fIMS: ''};
vm.createContext(ctx);
vm.runInContext(code, ctx);
const A = ctx.__api;
const setF = (f) => { ctx.fState = f.state || []; ctx.fBU = f.bu || []; ctx.fModel = f.model || []; ctx.fMedium = f.medium || []; ctx.fIMS = f.ims || ''; };

let pass = 0, fail = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) { pass++; console.log('  PASS  ' + label); }
  else { fail++; console.log('  FAIL  ' + label + '\n        got  ' + JSON.stringify(got) + '\n        want ' + JSON.stringify(want)); }
}

// calendar-month prefix of a "Mon'YYYY" label — the daily trend is by lead DATE, not Lead_Month
const prefixOf = (month) => ({"Sep'2026": '2026-09', "Oct'2026": '2026-10'})[month];

// brute force from the raw cells
function brute(f, month) {
  const ok = (c) => (!f.state || f.state.includes(c[1])) && (!f.medium || f.medium.includes(c[2])) &&
                    (!f.model || f.model.includes(c[0])) && (!f.bu || f.bu.includes(BU[c[0]]));
  const out = {brand: {}, model: {}, day: {}};
  for (const b of ['TVS', 'Hero']) {
    const m = (pred) => sum((c) => ok(c) && BRAND[c[0]] === b && c[3] === month && pred(c));
    out.brand[b] = {total: m(() => true), paid: m((c) => c[2] === 'Google' || c[2] === 'Facebook'), org: m((c) => c[2] === 'Organic'),
                    nonms: m((c) => c[2] === 'Non-MS'), wa: m((c) => c[2] === 'Whatsapp')};
  }
  for (const mdl of MODELS) {
    out.model[mdl] = sum((c) => ok(c) && c[0] === mdl && c[3] === month);
    out.day[mdl] = {};
    for (const dt of DATES.filter((d) => d.startsWith(prefixOf(month)))) out.day[mdl][dt] = sum((c) => ok(c) && c[0] === mdl && c[4] === dt);
  }
  return out;
}

function compareAll(label, f, month) {
  setF(f);
  const P = A._askActuals(month);
  const want = brute(f, month);
  const got = {brand: {}, model: {}, day: {}};
  for (const b of ['TVS', 'Hero']) got.brand[b] = P.brand(b);
  for (const m of MODELS) {
    got.model[m] = P.leadModel(m);
    got.day[m] = {};
    for (const dt of DATES.filter((d) => d.startsWith(prefixOf(month)))) got.day[m][dt] = P.day([m], dt);
  }
  check(label + ' — brand totals, model totals and every day match a brute-force count', got, want);
  return P;
}

console.log('\n== no State / BU / Model filter: original aggregates, unchanged ==');
setF({});
check('not in cube mode', [A._askUseCube("Oct'2026"), A._askNoAch("Oct'2026"), A._askActuals("Oct'2026").cube], [false, false, false]);
{
  const P = A._askActuals("Oct'2026");
  check('legacy brand totals come from brand_month / brand_medium_month', P.brand('TVS'), brute({}, "Oct'2026").brand.TVS);
  check('legacy Ask rows are all visible', [P.askRowVisible('TVS iQube'), P.askRowVisible('TVS Raider')], [true, true]);
}
setF({medium: ['Google']});
{
  const P = A._askActuals("Oct'2026");
  check('Source filter alone still handled by the legacy path', [P.cube, P.brand('TVS').total], [false, brute({medium: ['Google']}, "Oct'2026").brand.TVS.total]);
}

console.log('\n== State / BU / Model filters: exact cube figures ==');
compareAll('State=Maharashtra', {state: ['Maharashtra']}, "Oct'2026");
compareAll('State=Bihar+Karnataka', {state: ['Bihar', 'Karnataka']}, "Oct'2026");
compareAll('State=Unknown', {state: ['Unknown']}, "Oct'2026");
compareAll('State + Source', {state: ['Maharashtra', 'Bihar'], medium: ['Google', 'Organic']}, "Oct'2026");
compareAll('Model=TVS iQube S', {model: ['TVS iQube S']}, "Oct'2026");
compareAll('BU=UB', {bu: ['UB']}, "Oct'2026");
compareAll('BU=UB + State + Source', {bu: ['UB'], state: ['Karnataka'], medium: ['Facebook']}, "Oct'2026");
compareAll('Model + State, previous month', {model: ['TVS Raider'], state: ['Maharashtra']}, "Sep'2026");
{
  setF({state: ['Maharashtra']});
  check('State filter -> cube mode and no ACH', [A._askUseCube("Oct'2026"), A._askNoAch("Oct'2026")], [true, true]);
  setF({bu: ['UB']});
  check('BU filter -> cube mode but ACH stays (Ask can be limited to the same models)', [A._askUseCube("Oct'2026"), A._askNoAch("Oct'2026")], [true, false]);
  setF({model: ['TVS Raider']});
  check('Model filter -> cube mode, ACH stays', [A._askUseCube("Oct'2026"), A._askNoAch("Oct'2026")], [true, false]);
}

console.log('\n== Ask rows follow Model / BU filters (variants folded in) ==');
{
  setF({model: ['TVS iQube S']});
  let P = A._askActuals("Oct'2026");
  check('Model=iQube S keeps the "TVS iQube" Ask row (its variants include iQube S) and hides the others', [P.askRowVisible('TVS iQube'), P.askRowVisible('TVS Raider'), P.askRowVisible('Hero Glamour')], [true, false, false]);
  check('...and its Actual is iQube S only, not the whole row', P.askRow('TVS iQube'), brute({model: ['TVS iQube S']}, "Oct'2026").model['TVS iQube S']);
  setF({bu: ['UB']});
  P = A._askActuals("Oct'2026");
  check('BU=UB keeps the iQube row only', [P.askRowVisible('TVS iQube'), P.askRowVisible('TVS Raider')], [true, false]);
  check('BU=UB row Actual = iQube + iQube S + iQube ST', P.askRow('TVS iQube'),
        ['TVS iQube', 'TVS iQube S', 'TVS iQube ST'].reduce((s, m) => s + brute({bu: ['UB']}, "Oct'2026").model[m], 0));
}

console.log('\n== models with no Ask row ==');
{
  setF({state: ['Karnataka']});
  const P = A._askActuals("Oct'2026");
  const covered = new Set(['TVS iQube', 'TVS iQube S', 'TVS iQube ST', 'TVS Raider']);
  const other = P.otherModels('TVS', covered);
  const wantOther = MODELS.filter((m) => BRAND[m] === 'TVS' && !covered.has(m) && brute({state: ['Karnataka']}, "Oct'2026").model[m] > 0);
  check('otherModels lists only brand models outside the Ask that have filtered Actuals', other, wantOther);
  check('otherDayModels lists models with any filtered day data', P.otherDayModels('TVS', covered, '2026-10'),
        MODELS.filter((m) => BRAND[m] === 'TVS' && !covered.has(m) && Object.values(brute({state: ['Karnataka']}, "Oct'2026").day[m]).some((v) => v > 0)));
  setF({model: ['TVS Star City Plus']});
  const P2 = A._askActuals("Oct'2026");
  check('a Model filter on a no-Ask model hides every Ask row but still reports that model', [P2.askRowVisible('TVS iQube'), P2.askRowVisible('TVS Raider'), P2.otherModels('TVS', covered)], [false, false, ['TVS Star City Plus']]);
}

console.log('\n== months, notes ==');
{
  setF({state: ['Maharashtra']});
  check('a month the cube does not cover falls back to the original figures', [A._askUseCube("Mar'2026"), A._askActuals("Mar'2026").cube, A._askNoAch("Mar'2026")], [false, false, false]);
  check('...and the note says so', /can only be applied for/.test(A._askFilterNote("Mar'2026")), true);
  check('note for a covered month names the filter and the national-Ask caveat', /Filtered Actuals — State: Maharashtra/.test(A._askFilterNote("Oct'2026")) && /national/.test(A._askFilterNote("Oct'2026")), true);
  setF({bu: ['UB']});
  check('BU/Model note says Asks are limited to the matching models', /limited to the matching models/.test(A._askFilterNote("Oct'2026")), true);
  setF({});
  check('no filter, no note', [A._askFilterNote("Oct'2026"), A._askFilterText("Oct'2026")], ['', '']);
  setF({state: ['Maharashtra']});
  const saved = D.ask_cube; D.ask_cube = null;
  check('data without a cube (old refresh): filters are not applied and the note warns about it', [A._askUseCube("Oct'2026"), /cannot be applied/.test(A._askFilterNote("Oct'2026"))], [false, true]);
  D.ask_cube = saved;
  check('IMS appears in the filter text', (() => { ctx.fIMS = 'Y'; return A._askFilterNote("Oct'2026").includes('IMS: Y'); })(), true);
}

console.log(`\n${'='.repeat(60)}\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
