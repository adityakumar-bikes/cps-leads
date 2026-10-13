// Regression test for how the Ask vs Actual tab applies the top-bar filters.
//
//     node scripts/test_ask_filters.js [path/to/index.html]
//
// Pulls the real filter-aware Actuals code out of index.html (so it tests what ships, not a
// copy) and runs it against a small synthetic dashboard-data object, checking every figure the
// tab can show against a brute-force count of the same cells. No browser, network or
// credentials needed. Exit code is non-zero if any check fails.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.resolve(process.argv[2] || path.join(__dirname, '..', 'index.html')), 'utf8');
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
  ';globalThis.__api={_askActuals,_askUseCube,_askNoAch,_askFilterNote,_askFilterText,_askCubeIdx,_askExtraFilters,_askNoAskBrands,_askNoAskNote,_askFoldNoAsk,_askOtherGroups,_askOtherDayGroups,_askDaySources,_askDaySourceRows};',
].join('\n');

// ── synthetic data ────────────────────────────────────────────────────────────────────────
// brand TVS: TVS iQube (Ask row folding TVS iQube / iQube S / iQube ST), TVS Raider (Ask row),
// TVS Star City Plus (no Ask row). brand Hero: Hero Glamour (Ask row). BUs: TVS iQube* -> UB.
const MODELS = ['Hero Glamour', 'Hero Super Splendor XTEC', 'Hero Super Splendor', 'TVS Raider', 'TVS Star City Plus', 'TVS iQube', 'TVS iQube S', 'TVS iQube ST'];
const BRAND = {'Hero Glamour': 'Hero', 'Hero Super Splendor XTEC': 'Hero', 'Hero Super Splendor': 'Hero', 'TVS Raider': 'TVS', 'TVS Star City Plus': 'TVS', 'TVS iQube': 'TVS', 'TVS iQube S': 'TVS', 'TVS iQube ST': 'TVS'};
const BU = {'Hero Glamour': 'Hero', 'Hero Super Splendor XTEC': 'Hero', 'Hero Super Splendor': 'Hero', 'TVS Raider': 'TVS', 'TVS Star City Plus': 'TVS', 'TVS iQube': 'UB', 'TVS iQube S': 'UB', 'TVS iQube ST': 'UB'};
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
  brands_all: ['Hero', 'TVS'],
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
const setF = (f) => { ctx.fBrand = f.brand || []; ctx.fState = f.state || []; ctx.fBU = f.bu || []; ctx.fModel = f.model || []; ctx.fMedium = f.medium || []; ctx.fIMS = f.ims || ''; };

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
  const out = {brand: {}, model: {}, day: {}, paid: {}, split: {}};
  for (const b of ['TVS', 'Hero']) {
    const m = (pred) => sum((c) => ok(c) && BRAND[c[0]] === b && c[3] === month && pred(c));
    const g = m((c) => c[2] === 'Google'), f = m((c) => c[2] === 'Facebook'), wa = m((c) => c[2] === 'Whatsapp');
    out.brand[b] = {total: m(() => true), paid: g + f + wa, g, f, wa, org: m((c) => c[2] === 'Organic'), nonms: m((c) => c[2] === 'Non-MS')};
  }
  for (const mdl of MODELS) {
    out.model[mdl] = sum((c) => ok(c) && c[0] === mdl && c[3] === month);
    out.paid[mdl] = sum((c) => ok(c) && c[0] === mdl && c[3] === month && (c[2] === 'Google' || c[2] === 'Facebook' || c[2] === 'Whatsapp'));
    const sm = (med) => sum((c) => ok(c) && c[0] === mdl && c[3] === month && c[2] === med);
    out.split[mdl] = {g: sm('Google'), f: sm('Facebook'), wa: sm('Whatsapp'), org: sm('Organic')};
    out.day[mdl] = {};
    for (const dt of DATES.filter((d) => d.startsWith(prefixOf(month)))) out.day[mdl][dt] = sum((c) => ok(c) && c[0] === mdl && c[4] === dt);
  }
  return out;
}

function compareAll(label, f, month) {
  setF(f);
  const P = A._askActuals(month);
  const want = brute(f, month);
  const got = {brand: {}, model: {}, day: {}, paid: {}, split: {}};
  for (const b of ['TVS', 'Hero']) got.brand[b] = P.brand(b);
  for (const m of MODELS) {
    got.model[m] = P.leadModel(m);
    got.paid[m] = P.paidOf(m);
    got.split[m] = P.splitOf(m);
    got.day[m] = {};
    for (const dt of DATES.filter((d) => d.startsWith(prefixOf(month)))) got.day[m][dt] = P.day([m], dt);
  }
  check(label + ' — brand totals (Paid = CTWA + Google + Facebook, each split out), model totals, per-model splits and every day match a brute-force count', got, want);
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

console.log('\n== brands with leads but no Ask row for the month (e.g. Hero in Oct) ==');
{
  const askOct = D.ask_data["Oct'2026"];
  const savedHero = askOct.Hero; delete askOct.Hero;           // the Ask sheet has no Hero targets for the month
  setF({});
  let P = A._askActuals("Oct'2026");
  let nb = A._askNoAskBrands(askOct, P);
  check('Hero is found: leads but no Ask row, with its Actuals', nb.map((x) => [x.brand, x.a.total]), [['Hero', brute({}, "Oct'2026").brand.Hero.total]]);
  setF({brand: ['TVS']});
  check('a brand filter that excludes it hides it', A._askNoAskBrands(askOct, A._askActuals("Oct'2026")), []);
  setF({brand: ['Hero']});
  P = A._askActuals("Oct'2026");
  check('a brand filter that selects it keeps it', A._askNoAskBrands(askOct, P).map((x) => x.brand), ['Hero']);
  setF({brand: ['Hero'], state: ['Karnataka']});
  P = A._askActuals("Oct'2026");
  check('with a State filter its Actuals are the filtered ones (cube)', A._askNoAskBrands(askOct, P).map((x) => x.a.total), [brute({state: ['Karnataka']}, "Oct'2026").brand.Hero.total].filter((v) => v > 0));
  setF({brand: ['Hero'], state: ['Unknown'], medium: ['Whatsapp']});
  check('...and a brand with no leads left under the filters is not listed', A._askNoAskBrands(askOct, A._askActuals("Oct'2026")).length, brute({state: ['Unknown'], medium: ['Whatsapp']}, "Oct'2026").brand.Hero.total > 0 ? 1 : 0);
  setF({brand: ['Hero']});
  P = A._askActuals("Oct'2026");
  const g = A._askOtherGroups(P, 'Hero', new Set());
  const b0 = brute({}, "Oct'2026").model;
  check('"Hero Super Splendor" is folded into "Hero Super Splendor XTEC" on no-Ask rows', g.map((x) => x.name).includes('Hero Super Splendor') === false && g.some((x) => x.name === 'Hero Super Splendor XTEC' && x.members.length === 2), true);
  check('...and the folded row carries both spellings\' leads', g.find((x) => x.name === 'Hero Super Splendor XTEC').actual, b0['Hero Super Splendor XTEC'] + b0['Hero Super Splendor']);
  check('no-Ask rows keep the order otherModels() chose (biggest first); a folded group sits where its main model was',
        g.map((x) => x.name), [...new Set(P.otherModels('Hero', new Set()).map((m) => (m === 'Hero Super Splendor' ? 'Hero Super Splendor XTEC' : m)))]);
  check('daily groups fold the same way', A._askOtherDayGroups(P, 'Hero', new Set(), '2026-10').map((x) => x.name).includes('Hero Super Splendor'), false);
  check('a model whose main spelling has no row of its own is not folded away', A._askFoldNoAsk(['Hero Super Splendor']).map((x) => x.name), ['Hero Super Splendor']);
  check('the note names the brand and its leads', /Hero<\/strong> [\d,]+ leads/.test(A._askNoAskNote("Oct'2026", A._askNoAskBrands(askOct, P), false)), true);
  check('...and explains the scorecard treatment differently when every selected brand lacks an Ask',
        [/Overall Scorecard/.test(A._askNoAskNote("Oct'2026", A._askNoAskBrands(askOct, P), false)), /marked as not set/.test(A._askNoAskNote("Oct'2026", A._askNoAskBrands(askOct, P), true))], [true, true]);
  check('no such brands -> no note', A._askNoAskNote("Oct'2026", [], false), '');
  askOct.Hero = savedHero;
  setF({});
  check('with the Hero Ask row present again, nothing is listed as missing', A._askNoAskBrands(askOct, A._askActuals("Oct'2026")), []);
}

console.log('\n== Paid = CTWA (WhatsApp source) + Google + Facebook, per model / Ask row ==');
{
  const paidAll = brute({}, "Oct'2026").paid, splitAll = brute({}, "Oct'2026").split;
  setF({});
  let P = A._askActuals("Oct'2026");
  check('legacy path: paid per model = CTWA + Google + Facebook leads', MODELS.map((m) => P.paidOf(m)), MODELS.map((m) => paidAll[m]));
  check('legacy path: the split per model (Google, Facebook, CTWA, Organic)', MODELS.map((m) => P.splitOf(m)), MODELS.map((m) => splitAll[m]));
  const sum3 = (ms) => ms.reduce((s, m) => s + paidAll[m], 0);
  check('an Ask row folds its variants into its paid Actual', P.askRowPaid('TVS iQube'), sum3(['TVS iQube', 'TVS iQube S', 'TVS iQube ST']));
  check('...and into each part of the split', P.askRowSplit('TVS iQube'), ['TVS iQube', 'TVS iQube S', 'TVS iQube ST'].reduce((o, m) => ({g: o.g + splitAll[m].g, f: o.f + splitAll[m].f, wa: o.wa + splitAll[m].wa, org: o.org + splitAll[m].org}), {g: 0, f: 0, wa: 0, org: 0}));
  setF({medium: ['Google', 'Organic']});
  P = A._askActuals("Oct'2026");
  const paidG = brute({medium: ['Google', 'Organic']}, "Oct'2026").paid;
  check('legacy path honours the Source filter (only Google counts as paid)', MODELS.map((m) => P.paidOf(m)), MODELS.map((m) => paidG[m]));
  setF({medium: ['Whatsapp']});
  P = A._askActuals("Oct'2026");
  const paidW = brute({medium: ['Whatsapp']}, "Oct'2026");
  check('Source = WhatsApp only: all of paid is CTWA', MODELS.map((m) => [P.paidOf(m), P.splitOf(m).wa]), MODELS.map((m) => [paidW.paid[m], paidW.paid[m]]));
  setF({medium: ['Organic']});
  P = A._askActuals("Oct'2026");
  check('...and no paid Actual when only Organic is selected (Organic still shows)', [MODELS.map((m) => P.paidOf(m)), MODELS.map((m) => P.splitOf(m).org)], [MODELS.map(() => 0), MODELS.map((m) => brute({medium: ['Organic']}, "Oct'2026").split[m].org)]);
  setF({state: ['Karnataka', 'Bihar']});
  P = A._askActuals("Oct'2026");
  const bs = brute({state: ['Karnataka', 'Bihar']}, "Oct'2026");
  check('cube path: paid Actual and its split follow the State filter', [MODELS.map((m) => P.paidOf(m)), MODELS.map((m) => P.splitOf(m))], [MODELS.map((m) => bs.paid[m]), MODELS.map((m) => bs.split[m])]);
  check('cube path: Ask-row paid folds variants', P.askRowPaid('TVS iQube'), ['TVS iQube', 'TVS iQube S', 'TVS iQube ST'].reduce((s, m) => s + bs.paid[m], 0));
  setF({model: ['TVS iQube S']});
  P = A._askActuals("Oct'2026");
  const paidM = brute({model: ['TVS iQube S']}, "Oct'2026").paid;
  check('cube path: with a Model filter only the selected variant counts', P.askRowPaid('TVS iQube'), paidM['TVS iQube S']);
  setF({brand: ['Hero']});
  const askOct = D.ask_data["Oct'2026"]; const savedHero = askOct.Hero; delete askOct.Hero;
  P = A._askActuals("Oct'2026");
  const hg = A._askOtherGroups(P, 'Hero', new Set()).find((x) => x.name === 'Hero Super Splendor XTEC');
  const sp1 = splitAll['Hero Super Splendor XTEC'], sp2 = splitAll['Hero Super Splendor'];
  check('no-Ask groups carry paid and its split for all folded spellings', [hg.paid, hg.goog, hg.fb, hg.wa, hg.org], [paidAll['Hero Super Splendor XTEC'] + paidAll['Hero Super Splendor'], sp1.g + sp2.g, sp1.f + sp2.f, sp1.wa + sp2.wa, sp1.org + sp2.org]);
  askOct.Hero = savedHero;
  setF({});
  // the brand row: paid is the sum of its parts, and parts + organic + non-MS add up to the total
  for (const b of ['TVS', 'Hero']) {
    const a = A._askActuals("Oct'2026").brand(b);
    check(`brand ${b}: Paid = CTWA + Google + FB, and Paid + Organic + Non-MS = Total`, [a.paid === a.wa + a.g + a.f, a.paid + a.org + a.nonms === a.total], [true, true]);
  }
}

console.log('\n== Daily Trend by Source (day x source) ==');
{
  const okCell = (f, c) => (!f.state || f.state.includes(c[1])) && (!f.medium || f.medium.includes(c[2])) &&
                           (!f.model || f.model.includes(c[0])) && (!f.bu || f.bu.includes(BU[c[0]]));
  const days = DATES.filter((d) => d.startsWith('2026-10'));
  const norm = (o) => Object.fromEntries(Object.entries(o).sort());
  const bruteSrc = (f, models, dt) => { const o = {}; for (const c of cells) if (okCell(f, c) && models.includes(c[0]) && c[4] === dt) o[c[2]] = (o[c[2]] || 0) + c[5]; return o; };
  const cases = [['no filter (original aggregates)', {}], ['Source=Google+Organic', {medium: ['Google', 'Organic']}], ['State=Maharashtra', {state: ['Maharashtra']}],
                 ['Model=TVS iQube S', {model: ['TVS iQube S']}], ['BU=UB + State + Source', {bu: ['UB'], state: ['Karnataka'], medium: ['Facebook']}]];
  for (const [label, f] of cases) {
    setF(f);
    const P = A._askActuals("Oct'2026");
    const got = {}, want = {};
    for (const m of MODELS) for (const dt of days) { got[m + '|' + dt] = norm(P.daySrc([m], dt)); want[m + '|' + dt] = norm(bruteSrc(f, [m], dt)); }
    check(label + ': leads per source on every day, per model, match a brute-force count', got, want);
    const vs = ['TVS iQube', 'TVS iQube S', 'TVS iQube ST'];
    check(label + ': a folded Ask row sums its variants, per source', days.map((dt) => norm(P.daySrc(vs, dt))), days.map((dt) => norm(bruteSrc(f, vs, dt))));
    check(label + ': the sources of a day add up to that day\'s total', MODELS.every((m) => days.every((dt) => Object.values(P.daySrc([m], dt)).reduce((a, c) => a + c, 0) === P.day([m], dt))), true);

    // the table rows the By Source view shows
    const groups = MODELS.map((m) => [m]);
    const bySrc = A._askDaySources(P, groups, '2026-10', 31);
    const wantBy = {};
    for (const m of MODELS) for (const dt of days) { const d = parseInt(dt.slice(8), 10), sm = bruteSrc(f, [m], dt); for (const med in sm) (wantBy[med] || (wantBy[med] = new Array(32).fill(0)))[d] += sm[med]; }
    check(label + ': By Source day-by-day totals per source', norm(bySrc), norm(wantBy));
    const rows = A._askDaySourceRows(bySrc, 31);
    const dayOf = (meds) => Array.from({length: 32}, (_, d) => meds.reduce((s, m) => s + ((wantBy[m] || [])[d] || 0), 0));
    const byLabel = Object.fromEntries(rows.map((r) => [r.label, r]));
    const allWant = dayOf(MEDIA), paidWant = dayOf(['Whatsapp', 'Google', 'Facebook']);
    check(label + ': Paid row = CTWA + Google + Facebook, day by day', byLabel['Paid (CTWA + Google + FB)'] ? byLabel['Paid (CTWA + Google + FB)'].days : null, paidWant.some((v) => v) ? paidWant : null);
    const allGot = Array.from({length: 32}, (_, d) => rows.filter((r) => r.kind !== 'sub').reduce((s, r) => s + r.days[d], 0));
    check(label + ': All Sources (Paid + Organic + Non-MS) = every model\'s day total', allGot, Array.from({length: 32}, (_, d) => d ? MODELS.reduce((s, m) => s + P.day([m], '2026-10-' + String(d).padStart(2, '0')), 0) : 0));
    check(label + ': sources without leads get no row; parts sit under Paid', [rows.every((r) => r.total > 0), rows.filter((r) => r.kind === 'sub').every((r) => ['CTWA (WhatsApp)', 'Google', 'Facebook'].includes(r.label))], [true, true]);
  }
  setF({});
  check('a source outside the usual five still gets its own row, so All Sources keeps adding up',
        A._askDaySourceRows({Organic: [0, 1, 2], Weird: [0, 3, 0]}, 2).map((r) => [r.label, r.total, r.kind]), [['Organic', 3, 'main'], ['Weird', 3, 'main']]);
  check('no leads at all -> no rows', A._askDaySourceRows({}, 5), []);
}

console.log(`\n${'='.repeat(60)}\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
