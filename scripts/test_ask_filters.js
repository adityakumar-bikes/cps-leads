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
  pick('function _askPaid(v){', 'function renderAsk(){'),   // _askPaid, days in month, status / colour helpers, number formatters
  pick('function _toggleAskBrand(', 'function _askModelDayActual('),   // model families, the model table
  pick('function _computeAskData(', 'function _askScorecardTableXml('),   // the report data (alert lists)
  pick('function _askModelDayActual(', '</script>'),   // day-level helpers + the Daily Trend table
  ';globalThis.__api={_askActuals,_askUseCube,_askNoAch,_askFilterNote,_askFilterText,_askCubeIdx,_askExtraFilters,_askNoAskBrands,_askNoAskNote,_askFoldNoAsk,_askOtherGroups,_askOtherDayGroups,_askRowDaySrc,_askPickSrc,_askModelDailyTable,_ASK_DAY_SRCS,_askFamilyOf,_askClub,_askModelRows,_askModelItems,_askModelRowHtml,_askModelTable,_askModelExportRows,_askRowMetrics,_askRecent,_askProjLeads,_askProjPct,_askBrandModels,_askAlertLists,_computeAskData};',
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

const MCOL = {Organic: '#27AE60', Google: '#3498DB', Facebook: '#1877F2', 'Non-MS': '#E67E22', Whatsapp: '#25D366'};
const ctx = {console, D, MCOL, fState: [], fBU: [], fModel: [], fMedium: [], fBrand: [], fMonth: [], fIMS: ''};
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

console.log('\n== Daily Trend: one row per model, one source at a time ==');
{
  const okCell = (f, c) => (!f.state || f.state.includes(c[1])) && (!f.medium || f.medium.includes(c[2])) &&
                           (!f.model || f.model.includes(c[0])) && (!f.bu || f.bu.includes(BU[c[0]]));
  const days = DATES.filter((d) => d.startsWith('2026-10'));
  const norm = (o) => Object.fromEntries(Object.entries(o).sort());
  const bruteSrc = (f, models, dt) => { const o = {}; for (const c of cells) if (okCell(f, c) && models.includes(c[0]) && c[4] === dt) o[c[2]] = (o[c[2]] || 0) + c[5]; return o; };
  const PAID = ['Whatsapp', 'Google', 'Facebook'];
  const inSrc = (key, med) => key === 'all' ? true : key === 'paid' ? PAID.includes(med) : med === key;
  const bruteDays = (f, models, key) => days.reduce((o, dt) => { const sm = bruteSrc(f, models, dt); o[dt] = Object.entries(sm).filter(([med]) => inSrc(key, med)).reduce((s, [, n]) => s + n, 0); return o; }, {});
  const cases = [['no filter (original aggregates)', {}], ['Source=Google+Organic', {medium: ['Google', 'Organic']}], ['State=Maharashtra', {state: ['Maharashtra']}],
                 ['Model=TVS iQube S', {model: ['TVS iQube S']}], ['BU=UB + State + Source', {bu: ['UB'], state: ['Karnataka'], medium: ['Facebook']}]];
  const KEYS = ['all', 'paid', 'Whatsapp', 'Google', 'Facebook', 'Organic', 'Non-MS'];
  const LABEL = {all: 'All Sources', paid: 'Paid', Whatsapp: 'CTWA', Google: 'Google', Facebook: 'Facebook', Organic: 'Organic', 'Non-MS': 'Non-MS'};
  // read the Daily Trend HTML back: {sourceKey: {empty:bool, rows:{model:[total, day1, day2, ...]}, shown:bool}}
  const strip = (t) => t.replace(/<[^>]*>/g, '').replace(/,/g, '').replace(/\s+/g, ' ').trim();
  function readViews(html) {
    const out = {};
    for (const chunk of html.split('<div class="ask-day-view" data-src="').slice(1)) {
      const key = chunk.slice(0, chunk.indexOf('"'));
      const shown = /^[^>]*style="display:(?!none)/.test(chunk.slice(chunk.indexOf('"') + 1));
      const hasTable = chunk.includes('<table');
      const rows = {};
      if (hasTable) for (const tr of chunk.match(/<tr[^>]*>[\s\S]*?<\/tr>/g).slice(1)) {
        const cs = (tr.match(/<td[^>]*>[\s\S]*?<\/td>/g) || []).map(strip);
        rows[cs[0].replace(/ · no ask$/, '')] = cs.slice(1).map((x) => (x === '—' ? 0 : Number(x)));
      }
      out[key] = {empty: !hasTable, rows, shown};
    }
    return out;
  }
  for (const [label, f] of cases) {
    setF(f);
    const P = A._askActuals("Oct'2026");
    // provider: leads per source on every day, per model
    const got = {}, want = {};
    for (const m of MODELS) for (const dt of days) { got[m + '|' + dt] = norm(P.daySrc([m], dt)); want[m + '|' + dt] = norm(bruteSrc(f, [m], dt)); }
    check(label + ': leads per source on every day, per model, match a brute-force count', got, want);
    const vs = ['TVS iQube', 'TVS iQube S', 'TVS iQube ST'];
    check(label + ': a folded Ask row sums its variants, per source', days.map((dt) => norm(P.daySrc(vs, dt))), days.map((dt) => norm(bruteSrc(f, vs, dt))));
    check(label + ': the sources of a day add up to that day\'s total', MODELS.every((m) => days.every((dt) => Object.values(P.daySrc([m], dt)).reduce((a, c) => a + c, 0) === P.day([m], dt))), true);

    // one row's source x day arrays, and the source choices picked out of them
    const bySrc = A._askRowDaySrc(P, vs, '2026-10', 31);
    const wantBy = {};
    for (const dt of days) { const d = parseInt(dt.slice(8), 10), sm = bruteSrc(f, vs, dt); for (const med in sm) (wantBy[med] || (wantBy[med] = new Array(32).fill(0)))[d] += sm[med]; }
    check(label + ': a row\'s leads per source and day', norm(bySrc), norm(wantBy));
    const pick = (key) => A._askPickSrc(bySrc, key, 31);
    const wantPick = (key) => Array.from({length: 32}, (_, d) => Object.entries(wantBy).filter(([med]) => inSrc(key, med)).reduce((s, [, a]) => s + a[d], 0));
    check(label + ': All Sources / Paid (CTWA + Google + Facebook) / each source pick the right days', KEYS.map(pick), KEYS.map(wantPick));
    check(label + ': a source the row never had is all zeros', pick('Weird').every((v) => v === 0) && pick('Weird').length === 32, true);

    // the table itself, for the TVS brand
    const askRows = Object.entries(D.ask_data["Oct'2026"].TVS).filter(([mn]) => P.askRowVisible(mn)).sort(([, a], [, b]) => b.t - a.t);
    const html = A._askModelDailyTable(askRows, "Oct'2026", 31, 'TVS');
    if (!html) { check(label + ': table rendered', 'empty', 'html'); continue; }
    const views = readViews(html);
    check(label + ': a view for All Sources, Paid and each source', Object.keys(views), KEYS);
    check(label + ': a button for each, "All Sources" pressed and its table showing by default',
          [KEYS.map((k) => new RegExp('<button class="pg-btn' + (k === 'all' ? ' active' : '') + '" data-src="' + k + '"').test(html)).every(Boolean), views.all.shown, KEYS.filter((k) => k !== 'all').some((k) => views[k].shown)], [true, true, false]);
    // the rows an Ask row / no-Ask model contributes: groups of lead-data models
    const TVSM = MODELS.filter((m) => BRAND[m] === 'TVS');
    const groupsOf = {'TVS iQube': vs, 'TVS Raider': ['TVS Raider'], 'TVS Star City Plus': ['TVS Star City Plus']};
    for (const key of KEYS) {
      const v = views[key];
      const wantRows = {};
      for (const [name, ms] of Object.entries(groupsOf)) {
        const d = bruteDays(f, ms, key), tot = Object.values(d).reduce((a, c) => a + c, 0);
        if (tot > 0) wantRows[name] = [tot, ...Array.from({length: 31}, (_, i) => d['2026-10-' + String(i + 1).padStart(2, '0')] || 0)];
      }
      const allWant = Object.values(wantRows).reduce((acc, r) => acc.map((x, i) => x + r[i]), new Array(32).fill(0));
      if (!Object.keys(wantRows).length) {
        check(label + ' / ' + LABEL[key] + ': nothing from this source -> a message, not an empty table', [v.empty, /leads in Oct'2026 for this selection/.test(html)], [true, true]);
      } else {
        const { ['All Models']: allGot, ...modelRows } = v.rows;
        check(label + ' / ' + LABEL[key] + ': every model row (total + each day) matches a brute-force count', norm(modelRows), norm(wantRows));
        check(label + ' / ' + LABEL[key] + ': "All Models" is the sum of the rows, day by day', allGot, allWant);
      }
    }
    // the order of rows: biggest first, within each source
    const order = (key) => Object.entries(views[key].rows).filter(([n]) => n !== 'All Models').map(([, r]) => r[0]);
    check(label + ': rows sorted biggest first in every view', KEYS.every((k) => order(k).every((x, i, a) => i === 0 || a[i - 1] >= x)), true);
    // "paid" is exactly CTWA + Google + Facebook, model by model
    const mrow = (k, n) => (views[k].rows[n] || [0])[0];
    check(label + ': Paid = CTWA + Google + Facebook for every model', Object.keys(groupsOf).map((n) => mrow('paid', n)), Object.keys(groupsOf).map((n) => mrow('Whatsapp', n) + mrow('Google', n) + mrow('Facebook', n)));
  }
  setF({});
  // the chosen source is remembered, and a table without that source falls back to All Sources
  vm.runInContext("_askDaySrc='Google'", ctx);
  {
    const P = A._askActuals("Oct'2026");
    const askRows = Object.entries(D.ask_data["Oct'2026"].TVS).sort(([, a], [, b]) => b.t - a.t);
    const html = A._askModelDailyTable(askRows, "Oct'2026", 31, 'TVS');
    const v = readViews(html);
    check('the remembered source (Google) is the table showing, and its button is the pressed one', [v.Google.shown, v.all.shown, /<button class="pg-btn active" data-src="Google"/.test(html)], [true, false, true]);
    vm.runInContext("_askDaySrc='Nope'", ctx);
    const v2 = readViews(A._askModelDailyTable(askRows, "Oct'2026", 31, 'TVS'));
    check('a remembered source this table does not have falls back to All Sources', [v2.all.shown, Object.keys(v2).includes('Nope')], [true, false]);
    vm.runInContext("_askDaySrc='all'", ctx);
  }
  // a source outside the usual five still counts and gets its own button
  {
    const saved = D.model_medium_date['TVS Raider'];
    D.model_medium_date['TVS Raider'] = Object.assign({}, saved, {Weird: {'2026-10-02': 7}});
    const askRows = Object.entries(D.ask_data["Oct'2026"].TVS).sort(([, a], [, b]) => b.t - a.t);
    const html = A._askModelDailyTable(askRows, "Oct'2026", 31, 'TVS');
    const v = readViews(html);
    check('an unusual source gets a button and a view, and is part of All Sources', [Object.keys(v).includes('Weird'), v.Weird.rows['TVS Raider'] && v.Weird.rows['TVS Raider'][0], /data-src="Weird"/.test(html)], [true, 7, true]);
    D.model_medium_date['TVS Raider'] = saved;
  }
  check('no leads at all for a brand -> the "no daily data" message instead of tables', /No daily data/.test(A._askModelDailyTable([], "Oct'2026", 31, 'Nobody')), true);
}

console.log('\n== Model families: Glamour, Destini and Xoom clubbed into one row ==');
{
  const A0 = A;
  check('only Glamour, Destini and Xoom are families (whole-word match on the name)',
        ['Hero Glamour', 'Hero Glamour X 125', 'Hero Destini 125', 'Hero Destini Prime', 'Hero Xoom 110', 'Hero Xoom', 'Hero Xtreme 125R', 'Hero Passion Plus', 'Hero Glamourous', 'TVS Hero Xoom 110', 'Hero Super Splendor XTEC'].map((m) => A0._askFamilyOf(m)),
        ['Hero Glamour', 'Hero Glamour', 'Hero Destini', 'Hero Destini', 'Hero Xoom', 'Hero Xoom', null, null, null, null, null]);
  check('a family needs two or more rows to be clubbed; a lone variant stays a plain row, order is kept',
        A0._askClub([{n: 'Hero Destini 125'}, {n: 'Hero Xtreme 125R'}], (r) => r.n).map((i) => (i.family ? 'F:' + i.family : i.row.n)), ['Hero Destini 125', 'Hero Xtreme 125R']);
  check('two variants become one family item at the position of the first, the other rows keep their order',
        A0._askClub([{n: 'Hero Xtreme 125R'}, {n: 'Hero Destini 125'}, {n: 'Hero Passion Plus'}, {n: 'Hero Destini 110'}], (r) => r.n).map((i) => (i.family ? 'F:' + i.family + ':' + i.members.map((m) => m.n).join('+') : i.row.n)),
        ['Hero Xtreme 125R', 'F:Hero Destini:Hero Destini 125+Hero Destini 110', 'Hero Passion Plus']);

  // a small separate dataset (legacy provider): ten Hero models, one Xoom with no Ask row
  const FM = ['Hero Xtreme 125R', 'Hero Glamour', 'Hero Glamour X 125', 'Hero Destini 125', 'Hero Destini 110', 'Hero Destini Prime', 'Hero Xoom 125', 'Hero Xoom 110', 'Hero Xoom 160', 'Hero Passion Plus'];
  const ASK = {'Hero Xtreme 125R': 33700, 'Hero Glamour': 16500, 'Hero Glamour X 125': 14000, 'Hero Destini 125': 15000, 'Hero Destini 110': 5500, 'Hero Destini Prime': 3500,
               'Hero Xoom 125': 7700, 'Hero Xoom 110': 5300, 'Hero Passion Plus': 7500};          // Xoom 160: no Ask
  const M0 = "Oct'2026", FDATES = ['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07'];
  const fcells = [];
  let sd = 4242; const r2 = (n) => { sd = (sd * 1103515245 + 12345) & 0x7fffffff; return sd % n; };
  for (const m of FM) for (const e of MEDIA) for (const dt of FDATES) fcells.push([m, e, dt, 1 + r2(40)]);
  const fsum = (pred, f = (c) => c[3]) => fcells.filter(pred).reduce((a, c) => a + f(c), 0);
  const D2 = {ask_cube: null, model_bu: {}, model_brand: {}, by_brand: {Hero: 1}, brands_all: ['Hero'], brand_month: {Hero: {[M0]: fsum(() => true)}}, brand_medium_month: {Hero: {}},
              model_month: {}, model_medium_month: {}, model_date: {}, model_medium_date: {}, brand_date_range: {Hero: {max: '10/07/2026'}},
              ask_data: {[M0]: {Hero: Object.fromEntries(Object.entries(ASK).map(([m, t]) => [m, {t, o: Math.round(t * .2), g: Math.round(t * .3), f: Math.round(t * .5), p: 0}]))}}};
  for (const e of MEDIA) D2.brand_medium_month.Hero[e] = {[M0]: fsum((c) => c[1] === e)};
  for (const m of FM) {
    D2.model_brand[m] = {Hero: 1}; D2.model_bu[m] = 'Hero';
    D2.model_month[m] = {[M0]: fsum((c) => c[0] === m)};
    D2.model_medium_month[m] = {}; D2.model_medium_date[m] = {}; D2.model_date[m] = {};
    for (const e of MEDIA) { D2.model_medium_month[m][e] = {[M0]: fsum((c) => c[0] === m && c[1] === e)}; D2.model_medium_date[m][e] = {}; }
    for (const dt of FDATES) {
      D2.model_date[m][dt] = fsum((c) => c[0] === m && c[2] === dt);
      for (const e of MEDIA) D2.model_medium_date[m][e][dt] = fsum((c) => c[0] === m && c[1] === e && c[2] === dt);
    }
  }
  const ctx2 = {console, D: D2, MCOL, fState: [], fBU: [], fModel: [], fMedium: [], fBrand: [], fMonth: [], fIMS: ''};
  vm.createContext(ctx2); vm.runInContext(code, ctx2);
  const F = ctx2.__api;
  const P = F._askActuals(M0);
  const R = F._askModelRows(P, 'Hero', M0, 7);
  const act = (m) => fsum((c) => c[0] === m);
  const split = (m) => ({wa: fsum((c) => c[0] === m && c[1] === 'Whatsapp'), g: fsum((c) => c[0] === m && c[1] === 'Google'), f: fsum((c) => c[0] === m && c[1] === 'Facebook'), org: fsum((c) => c[0] === m && c[1] === 'Organic')});
  const RECENT = FDATES.slice(3);   // the last four days with data (7 days so far)
  const recent = (m) => fsum((c) => c[0] === m && RECENT.includes(c[2]));
  const wantRow = (m) => { const a = ASK[m], sp = split(m); return {name: m, noAsk: !a, ask: a || 0, act: act(m), paidAsk: a ? Math.round(a * .3) + Math.round(a * .5) : 0, paidAct: sp.wa + sp.g + sp.f, wa: sp.wa, g: sp.g, f: sp.f, orgAsk: a ? Math.round(a * .2) : 0, orgAct: sp.org, rec: recent(m), recN: 4}; };
  check('every model row (Ask rows and the no-Ask one) carries the right Ask, Actual and split', [...R.askRows, ...R.noAskRows].map((r) => r.name).sort().join('|') + '##' + JSON.stringify([...R.askRows, ...R.noAskRows].sort((a, b) => a.name.localeCompare(b.name))),
        FM.slice().sort().join('|') + '##' + JSON.stringify(FM.slice().sort().map(wantRow)));
  const items = F._askModelItems(R);
  const famWant = (fam) => { const ms = FM.filter((m) => m.startsWith(fam)); const rows = ms.map(wantRow); const t = {}; for (const k of ['ask', 'act', 'paidAsk', 'paidAct', 'wa', 'g', 'f', 'orgAsk', 'orgAct', 'rec']) t[k] = rows.reduce((a, r) => a + r[k], 0); return t; };
  const pickKeys = (r) => { const o = {}; for (const k of ['ask', 'act', 'paidAsk', 'paidAct', 'wa', 'g', 'f', 'orgAsk', 'orgAct', 'rec']) o[k] = r[k]; return o; };
  check('top level: Xtreme 125R, the Glamour / Destini / Xoom families and Passion Plus, biggest Ask first',
        items.map((i) => i.row.name), ['Hero Xtreme 125R', 'Hero Glamour family', 'Hero Destini family', 'Hero Xoom family', 'Hero Passion Plus']);
  for (const fam of ['Hero Glamour', 'Hero Destini', 'Hero Xoom']) {
    const it = items.find((i) => i.family === fam);
    check(fam + ' family row = the sum of its models (the no-Ask Xoom 160 counts in Actual, not in Ask)', pickKeys(it.row), famWant(fam));
  }
  check('members inside a family: biggest Ask first, the no-Ask model last',
        items.filter((i) => i.family).map((i) => i.members.map((m) => m.name)), [['Hero Glamour', 'Hero Glamour X 125'], ['Hero Destini 125', 'Hero Destini 110', 'Hero Destini Prime'], ['Hero Xoom 125', 'Hero Xoom 110', 'Hero Xoom 160']]);
  const tot = (rows) => pickKeys(rows.reduce((o, r) => { for (const k in o) o[k] += r[k]; return o; }, {ask: 0, act: 0, paidAsk: 0, paidAct: 0, wa: 0, g: 0, f: 0, orgAsk: 0, orgAct: 0, rec: 0}));
  check('the top-level rows add up to the same Ask and Actual as every model on its own (nothing lost or counted twice)', tot(items.map((i) => i.row)), tot(FM.map(wantRow)));
  check('...and the brand\'s Actual is the sum of the top-level rows', items.reduce((s, i) => s + i.row.act, 0), P.brand('Hero').total);
  check('a family with no Ask target at all would be a no-Ask row, listed after the others',
        F._askModelItems({askRows: [], noAskRows: [{name: 'Hero Xoom 110', noAsk: true, ask: 0, act: 5, paidAsk: 0, paidAct: 3, wa: 1, g: 1, f: 1, orgAsk: 0, orgAct: 2}, {name: 'Hero Xoom 160', noAsk: true, ask: 0, act: 7, paidAsk: 0, paidAct: 4, wa: 2, g: 1, f: 1, orgAsk: 0, orgAct: 3}]}).map((i) => [i.row.name, i.row.noAsk, i.row.act]),
        [['Hero Xoom family', true, 12]]);

  // the HTML of the model table
  const html = F._askModelTable('Hero', M0, 31, 7, 23);
  const modelHtml = html.split('<div class="ask-day-wrap">')[0];   // the model table, without the Daily Trend that follows it
  const trs = modelHtml.match(/<tr[^>]*>[\s\S]*?<\/tr>/g);
  const famRows = trs.filter((t) => /class="ask-fam-row"/.test(t)), memRows = trs.filter((t) => /class="ask-fam-member"/.test(t));
  const cellsOf = (tr) => (tr.match(/<td[^>]*>[\s\S]*?<\/td>/g) || []).map((c) => c.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim());
  check('three family rows, each with its models underneath (2 + 3 + 3), the models hidden until clicked',
        [famRows.length, memRows.length, memRows.every((t) => /display:none/.test(t)), famRows.every((t) => /▶/.test(t) && /onclick="_askToggleFamily\('ask-fam-m-hero-hero-(glamour|destini|xoom)'\)"/.test(t))], [3, 8, true, true]);
  const famCells = Object.fromEntries(famRows.map((t) => { const c = cellsOf(t); return [c[0].replace(/^▶ /, '').replace(/ · .*$/, ''), c]; }));
  const n = (v) => v.toLocaleString('en-IN');
  for (const fam of ['Hero Glamour', 'Hero Destini', 'Hero Xoom']) {
    const w = famWant(fam), c = famCells[fam + ' family'];
    check(fam + ' family row shows the family Ask, Actual, ACH%, Paid Actual, CTWA, Google, FB and Org Actual', [c[1], c[2], c[3], c[5], c[6], c[7], c[8], c[11]],
          [n(w.ask), n(w.act), (w.act / w.ask * 100).toFixed(1) + '%', n(w.paidAct), n(w.wa), n(w.g), n(w.f), n(w.orgAct)]);
  }
  check('the legend explains the family rows', /clubbed into one family row — click it/.test(html), true);
  check('the other models keep their own plain rows', ['Hero Xtreme 125R', 'Hero Passion Plus'].every((m) => trs.some((t) => !/class="ask-fam/.test(t) && cellsOf(t)[0] === m)), true);
  // open one family
  vm.runInContext("_askFamOpen.add('ask-fam-m-hero-hero-destini')", ctx2);
  const html2 = F._askModelTable('Hero', M0, 31, 7, 23);
  const open = (h, fam) => (h.match(/<tr class="ask-fam-member"[^>]*>/g) || []).filter((t) => t.includes('data-fam="ask-fam-m-hero-hero-' + fam + '"')).map((t) => /display:none/.test(t));
  check('an opened family stays open when the table is rebuilt, the others stay closed', [open(html2, 'destini'), open(html2, 'glamour'), open(html2, 'xoom'), /<span class="ask-fam-arr" data-fam="ask-fam-m-hero-hero-destini">▼/.test(html2)],
        [[false, false, false], [true, true], [true, true, true], true]);
  vm.runInContext("_askFamOpen.clear()", ctx2);
  // exports follow the same rows: a family row, then its models
  const ex = F._askModelExportRows(P, 'Hero', M0);
  check('exports list each family row followed by its models, then the other rows', ex.map((x) => (x.kind === 'member' ? '  ' : '') + x.name),
        ['Hero Xtreme 125R', 'Hero Glamour family', '  Hero Glamour', '  Hero Glamour X 125', 'Hero Destini family', '  Hero Destini 125', '  Hero Destini 110', '  Hero Destini Prime', 'Hero Xoom family', '  Hero Xoom 125', '  Hero Xoom 110', '  Hero Xoom 160', 'Hero Passion Plus']);

  // the Daily Trend
  const dh = F._askModelDailyTable(R.visible, M0, 31, 'Hero');
  const views = dh.split('<div class="ask-day-view" data-src="').slice(1);
  const viewOf = (key) => views.find((v) => v.startsWith(key + '"'));
  const dayRows = (v) => (v.match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || []).slice(1).map((t) => ({fam: /class="ask-fam-row"/.test(t), mem: /class="ask-fam-member"/.test(t), hidden: /display:none/.test(t), cells: cellsOf(t)}));
  const allRows = dayRows(viewOf('all'));
  check('Daily Trend: a family row per family with its models hidden underneath, and the rows are sorted by total (families by their sum)',
        [allRows.filter((r) => r.fam).length, allRows.filter((r) => r.mem).length, allRows.filter((r) => r.mem).every((r) => r.hidden)], [3, 8, true]);
  const dayWant = (ms, key) => { const meds = key === 'all' ? MEDIA : key === 'paid' ? ['Whatsapp', 'Google', 'Facebook'] : [key]; const days = FDATES.map((dt) => fsum((c) => ms.includes(c[0]) && meds.includes(c[1]) && c[2] === dt)); return [days.reduce((a, c) => a + c, 0), ...days]; };
  for (const key of ['all', 'paid', 'Google']) {
    const rows = dayRows(viewOf(key));
    for (const fam of ['Hero Glamour', 'Hero Destini', 'Hero Xoom']) {
      const fr = rows.find((r) => r.fam && r.cells[0].startsWith('▶ ' + fam + ' family'));
      const w = dayWant(FM.filter((m) => m.startsWith(fam)), key);
      check(`Daily Trend (${key}): the ${fam} family row = the sum of its models, total and the seven days`, fr.cells.slice(1, 2 + FDATES.length).map((x) => Number(x.replace(/,/g, '')) || 0), w);
    }
    const tops = rows.filter((r) => !r.mem).map((r) => Number(r.cells[1].replace(/,/g, '')));
    check(`Daily Trend (${key}): rows sorted biggest first`, tops.slice(1).every((x, i, a) => i === 0 || a[i - 1] >= x), true);
    const allModelsRow = rows.length ? cellsOf((viewOf(key).match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || [])[1]) : null;
    check(`Daily Trend (${key}): "All Models" counts every model once (families not added twice)`, Number(allModelsRow[1].replace(/,/g, '')), dayWant(FM, key)[0]);
  }
  // ── projection: leads so far + the last 4 days' average × the days left ──────────────────────────────────────────
  check('projection maths: 100 so far, 40 in the last 4 days, day 10 of 30 -> 100 + 10/day x 20 days = 300 leads = 75% of a 400 Ask',
        [F._askProjLeads(100, 40, 4, 10, 30), F._askProjPct(100, 40, 4, 400, 10, 30)], [300, 75]);
  check('...a finished month projects to what it did; no recent days means no growth; no Ask means no %',
        [F._askProjLeads(100, 40, 4, 30, 30), F._askProjLeads(100, 0, 0, 10, 30), F._askProjPct(100, 40, 4, 0, 10, 30)], [100, 100, null]);
  const DAYS = 7, TOT = 31;
  const sums = (models, dates) => { const o = {all: 0, paid: 0, org: 0, nonms: 0}; for (const c of fcells) if (models.includes(c[0]) && dates.includes(c[2])) { o.all += c[3]; if (['Whatsapp', 'Google', 'Facebook'].includes(c[1])) o.paid += c[3]; else if (c[1] === 'Organic') o.org += c[3]; else o.nonms += c[3]; } return o; };
  for (const [label, models] of [['one model', ['Hero Passion Plus']], ['a family', FM.filter((m) => m.startsWith('Hero Destini'))], ['the whole brand', FM]]) {
    const got = F._askRecent(P, models, M0, DAYS), w = sums(models, RECENT);
    check(`recent leads of ${label}: the last 4 of 7 days, split all / paid / organic / non-MS`, got, {n: 4, ...w});
  }
  check('early in the month the window is the days there are (3 days of data -> 3), and no days -> nothing',
        [F._askRecent(P, FM, M0, 3), F._askRecent(P, FM, M0, 0).n, F._askRecent(P, [], M0, 7).all], [{n: 3, ...sums(FM, FDATES.slice(0, 3))}, 0, 0]);
  check('every lead-data model of a brand', F._askBrandModels('Hero').slice().sort(), FM.slice().sort());
  // the model table's projection column, for a model row and a family row
  const projWant = (models, ask) => { const a = sums(models, FDATES).all, r = sums(models, RECENT).all; return Math.round((a + r / 4 * (TOT - DAYS)) / ask * 100); };
  const tHtml = F._askModelTable('Hero', M0, TOT, DAYS, 23).split('<div class="ask-day-wrap">')[0];
  const tRows = tHtml.match(/<tr[^>]*>[\s\S]*?<\/tr>/g).slice(1).map((t) => ({fam: /ask-fam-row/.test(t), c: cellsOf(t)}));
  const projCol = tRows[0].c.length - 2;   // Proj Month-End sits before Status
  const rowOf = (name) => tRows.find((r) => r.c[0].replace(/^[▶▼] /, '').replace(/ · .*$/, '') === name);
  check('model table: "Proj Month-End" of a model = (so far + last-4-day average x days left) / Ask',
        ['Hero Xtreme 125R', 'Hero Passion Plus'].map((m) => rowOf(m).c[projCol]), ['Hero Xtreme 125R', 'Hero Passion Plus'].map((m) => '~' + projWant([m], ASK[m]) + '%'));
  check('...and of a family, from the family\'s own totals (not an average of its models\' projections)',
        ['Hero Glamour', 'Hero Destini', 'Hero Xoom'].map((f) => rowOf(f + ' family').c[projCol]),
        ['Hero Glamour', 'Hero Destini', 'Hero Xoom'].map((f) => '~' + projWant(FM.filter((m) => m.startsWith(f)), FM.filter((m) => m.startsWith(f)).reduce((a, m) => a + (ASK[m] || 0), 0)) + '%'));
  check('the column header explains the method', /<th title="[^"]*last 4 days[^"]*">Proj Month-End<\/th>/.test(tHtml), true);

  // a Model filter that leaves one Destini: it is shown as the plain row
  check('with a single model of a family left, it is the plain row (no family row)', F._askModelItems({askRows: [{name: 'Hero Destini 125', noAsk: false, ask: 9, act: 5, paidAsk: 1, paidAct: 1, wa: 1, g: 0, f: 0, orgAsk: 1, orgAct: 1}], noAskRows: []}).map((i) => [i.family || null, i.row.name]), [[null, 'Hero Destini 125']]);
}

console.log('\n== Over / under-delivery lists project from the recent run rate ==');
{
  const M0 = "Oct'2026", dates = ['01', '02', '03', '04', '05', '06', '07'].map((d) => '2026-10-' + d);
  // daily leads so far (7 days of an October of 31): one model speeding up, one slowing down, a flat one, a hopeless one
  const series = {'Zed Up': [10, 10, 10, 200, 200, 200, 200], 'Zed Down': [300, 300, 300, 10, 10, 10, 10], 'Zed Flat': [100, 100, 100, 100, 100, 100, 100], 'Zed Slow': [20, 20, 20, 20, 20, 20, 20]};
  const askT = {'Zed Up': 4000, 'Zed Down': 3000, 'Zed Flat': 6000, 'Zed Slow': 10000};
  const tot = (m) => series[m].reduce((a, c) => a + c, 0);
  const D3 = {ask_cube: null, model_bu: {}, model_brand: {}, by_brand: {Zed: 1}, brands_all: ['Zed'], brand_date_range: {Zed: {max: '10/07/2026'}},
              brand_month: {Zed: {[M0]: Object.keys(series).reduce((a, m) => a + tot(m), 0)}}, brand_medium_month: {Zed: {Organic: {[M0]: Object.keys(series).reduce((a, m) => a + tot(m), 0)}}},
              model_month: {}, model_medium_month: {}, model_date: {}, model_medium_date: {},
              ask_data: {[M0]: {Zed: Object.fromEntries(Object.entries(askT).map(([m, t]) => [m, {t, o: 0, g: 0, f: 0, p: 0}]))}}};
  for (const m of Object.keys(series)) {
    D3.model_brand[m] = {Zed: 1}; D3.model_bu[m] = 'Zed';
    D3.model_month[m] = {[M0]: tot(m)}; D3.model_medium_month[m] = {Organic: {[M0]: tot(m)}};
    D3.model_date[m] = {}; D3.model_medium_date[m] = {Organic: {}};
    dates.forEach((dt, i) => { D3.model_date[m][dt] = series[m][i]; D3.model_medium_date[m].Organic[dt] = series[m][i]; });
  }
  const ctx3 = {console, D: D3, MCOL, fState: [], fBU: [], fModel: [], fMedium: [], fBrand: [], fMonth: [], fIMS: ''};
  vm.createContext(ctx3); vm.runInContext(code, ctx3);
  const G = ctx3.__api;
  const d = G._computeAskData(M0);
  const projNew = (m) => (tot(m) + series[m].slice(3).reduce((a, c) => a + c, 0) / 4 * (31 - 7)) / askT[m] * 100;
  const projOld = (m) => tot(m) / 7 * 31 / askT[m] * 100;
  check('the month is partial: 7 of 31 days, pace 23%', [d.isPartial, d.daysElapsed, d.totalDays, d.pacePct], [true, 7, 31, 23]);
  check('over list: Zed Up (speeding up: 141% from its last 4 days) is flagged; Zed Down (slowed to 10 a day) is not, though its month average still says 139%',
        [d.overM.map((x) => [x.model, x.proj]), projOld('Zed Down') > 130, projNew('Zed Down') < 130], [[['Zed Up', projNew('Zed Up').toFixed(0)]], true, true]);
  check('under list: the hopeless model with its recent-rate projection (6% of its Ask)', d.underM.map((x) => [x.model, x.ach, x.proj]), [['Zed Slow', (tot('Zed Slow') / askT['Zed Slow'] * 100).toFixed(1), projNew('Zed Slow').toFixed(0)]]);
  check('the flat model is on neither list', [...d.overM, ...d.underM].some((x) => x.model === 'Zed Flat'), false);
  check('a full month has no lists (nothing left to project)', (() => { D3.brand_date_range.Zed.max = '10/31/2026'; const e = G._computeAskData(M0); D3.brand_date_range.Zed.max = '10/07/2026'; return [e.isPartial, e.overM.length, e.underM.length]; })(), [false, 0, 0]);
}

console.log(`\n${'='.repeat(60)}\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
