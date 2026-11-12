#!/usr/bin/env node
/* Tests the saved-views logic of the dashboard (index.html, between "// <saved-views-logic>" and "// </saved-views-logic>"):
 * naming rules, cleaning of filter sets, reading back whatever is in storage (damaged, hostile, wrong version), the
 * add / replace / rename / remove / default operations and the 30-view cap -- first with hand-written cases, then with
 * thousands of random operations checked against the invariants. The menu itself (DOM) is checked in a browser.
 * Run:  node scripts/test_saved_views.js [path/to/index.html]
 */
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.resolve(process.argv[2] || path.join(__dirname, '..', 'index.html')), 'utf8');
const a = html.indexOf('// <saved-views-logic>'), b = html.indexOf('// </saved-views-logic>');
if (a < 0 || b < 0) { console.error('saved-views markers not found in index.html'); process.exit(2); }
const L = new Function(html.slice(a, b + '// </saved-views-logic>'.length) + `
  return {SV_KEY, SV_MAX, SV_NAME_MAX, SV_IDS, svEmpty, svEsc, svCleanName, svSameName, svCleanList, svCleanFilters, svIdOk, svNewId,
          svSanitize, svSig, svSummary, svSave, svOverwrite, svRename, svRemove, svSetDefault, svKeep, svLoad, svPersist};`)();

let checks = 0, fails = 0;
const ok = (c, m) => { checks++; if (!c) { fails++; if (fails < 20) console.log('FAIL', m); } };
const J = x => JSON.stringify(x);
const eq = (x, y, m) => ok(J(x) === J(y), m + ' | got ' + J(x) + ' want ' + J(y));
const F = o => Object.assign({ brand: [], medium: [], month: [], bu: [], model: [], state: [], ims: '' }, o);

// ---- names -------------------------------------------------------------------------------------------------------------
eq(L.svCleanName('  Jawa   Nov  '), 'Jawa Nov', 'trims and collapses spaces');
eq(L.svCleanName('Jawa\t\nNov'), 'Jawa Nov', 'tabs / newlines become one space');
eq([L.svCleanName(null), L.svCleanName(undefined), L.svCleanName('   ')], ['', '', ''], 'nothing -> empty');
ok(L.svCleanName('x'.repeat(100)).length === L.SV_NAME_MAX, 'name capped at SV_NAME_MAX');
ok(!/\s$/.test(L.svCleanName('a'.repeat(L.SV_NAME_MAX - 1) + ' tail')), 'no trailing space left after the cut');
ok(L.svSameName('TVS Oct', 'tvs   OCT') && !L.svSameName('TVS Oct', 'TVS Nov'), 'names compare ignoring case and spacing');
ok(L.svEsc('<img src=x onerror="a">\'&') === '&lt;img src=x onerror=&quot;a&quot;&gt;&#39;&amp;', 'html escaping');

// ---- lists and filter sets ---------------------------------------------------------------------------------------------
eq(L.svCleanList('nope'), [], 'non-array -> empty');
eq(L.svCleanList(['a', 3, null, '', 'a', 'b', { x: 1 }, ['c']]), ['a', 'b'], 'keeps unique non-empty strings only');
ok(L.svCleanList(['x'.repeat(500)])[0].length === 120, 'long values cut to 120');
ok(L.svCleanList(Array.from({ length: 900 }, (_, i) => 'v' + i)).length === 500, 'list capped at 500 values');
eq(L.svCleanFilters(null), F({}), 'null -> empty filter set');
eq(L.svCleanFilters({ brand: ['TVS'], junk: 1, ims: 'X' }), F({ brand: ['TVS'] }), 'unknown keys dropped, bad IMS -> none');
eq([L.svCleanFilters({ ims: 'Y' }).ims, L.svCleanFilters({ ims: 'N' }).ims, L.svCleanFilters({ ims: 'n' }).ims], ['Y', 'N', ''], 'IMS accepts Y / N only');
ok(L.svSig(F({ brand: ['b', 'a'] })) === L.svSig(F({ brand: ['a', 'b'] })), 'signature ignores order');
ok(L.svSig({ brand: ['a'] }) === L.svSig(F({ brand: ['a'] })), 'missing lists count as empty');
ok(L.svSig(F({ brand: ['a'] })) !== L.svSig(F({ medium: ['a'] })), 'same value in another filter differs');
ok(L.svSig(F({ ims: 'Y' })) !== L.svSig(F({ ims: 'N' })) && L.svSig(F({ ims: 'Y' })) !== L.svSig(F({})), 'IMS is part of the signature');
eq(L.svSummary(F({})), 'No filters (all data)', 'summary: nothing set');
eq(L.svSummary(F({ brand: ['Jawa'], month: ["Nov'2026"] })), "Brand: Jawa · Month: Nov'2026", 'summary: single values');
eq(L.svSummary(F({ bu: ['MC', 'PB'], model: ['a', 'b', 'c'], state: ['x'], medium: ['Google', 'Organic'], ims: 'Y' })),
  'Source: 2 sources · BU: 2 BUs · Model: 3 models · State: x · IMS: Y', 'summary: counts, order and IMS');
eq(L.svKeep(['a', 'b', 'c'], ['c', 'a']), { kept: ['a', 'c'], dropped: 1 }, 'keep: partial');
eq([L.svKeep([], ['a']), L.svKeep(undefined, ['a'])], [{ kept: [], dropped: 0 }, { kept: [], dropped: 0 }], 'keep: nothing saved');

// ---- reading back storage ----------------------------------------------------------------------------------------------
for (const bad of [null, undefined, 5, 'x', [], {}, { v: 2, views: [] }, { v: 1 }, { v: 1, views: 'no' }])
  eq(L.svSanitize(bad), { views: [], def: null }, 'garbage -> empty: ' + J(bad));
{
  const raw = { v: 1, def: 'ghost', views: [
    null, 7, { name: '' }, { name: '   ' }, { id: 'a1b', name: 'First', f: { brand: ['TVS'], bad: 1 }, t: 5, u: 9 },
    { id: 'a1b', name: 'Second same id', f: {} }, { id: '!!', name: 'Bad id', f: 'x' }, { id: 'zz9', name: 'first', f: {} }, // repeated name, any case
    { id: 'q', name: 'x'.repeat(80), f: {} }] };
  const s = L.svSanitize(raw);
  eq(s.views.map(v => v.name), ['First', 'Second same id', 'Bad id', 'x'.repeat(L.SV_NAME_MAX)], 'invalid / repeated names dropped, long ones cut');
  ok(s.views.every(v => L.svIdOk(v.id)) && new Set(s.views.map(v => v.id)).size === s.views.length, 'ids repaired and unique');
  ok(s.views[0].id === 'a1b' && s.views[0].t === 5 && s.views[0].u === 9, 'good view kept as it was');
  eq(s.views[0].f, F({ brand: ['TVS'] }), 'filters cleaned');
  ok(s.def === null, 'default pointing nowhere is cleared');
  eq(L.svSanitize({ v: 1, def: 'a1b', views: [raw.views[4]] }).def, 'a1b', 'valid default kept');
  const many = L.svSanitize({ v: 1, views: Array.from({ length: 80 }, (_, i) => ({ id: 'id' + i, name: 'View ' + i, f: {} })) });
  ok(many.views.length === L.SV_MAX, 'at most SV_MAX views read back');
}
class Store {
  constructor() { this.d = {}; this.noGet = false; this.noSet = false; }
  getItem(k) { if (this.noGet) throw new Error('blocked'); return k in this.d ? this.d[k] : null; }
  setItem(k, v) { if (this.noSet) throw new Error('quota'); this.d[k] = String(v); }
}
{
  const st = new Store();
  eq(L.svLoad(st), { S: { views: [], def: null }, problem: null }, 'empty storage');
  let S = L.svSave(L.svEmpty(), 'Jawa – Nov', F({ brand: ['Jawa'], month: ["Nov'2026"] }), 1).S;
  S = L.svSetDefault(S, S.views[0].id).S;
  ok(L.svPersist(st, S) === true, 'persist ok');
  const back = L.svLoad(st);
  eq(back.S, S, 'round trip');
  ok(back.problem === null, 'no problem after a clean round trip');
  for (const junk of ['{not json', '[]', 'null', '"x"', '{"v":2,"views":[]}', '{"v":1}']) {
    const s2 = new Store(); s2.d[L.SV_KEY] = junk;
    const r = L.svLoad(s2);
    ok(r.problem === 'corrupt' && J(r.S) === J({ views: [], def: null }) && s2.d[L.SV_KEY + '.bak'] === junk, 'unreadable store kept as .bak: ' + junk);
  }
  const blocked = new Store(); blocked.noGet = true;
  eq(L.svLoad(blocked).problem, 'unavailable', 'blocked read');
  eq(L.svLoad(null).problem, 'unavailable', 'no storage at all');
  const full = new Store(); full.noSet = true;
  ok(L.svPersist(full, S) === false && L.svPersist(null, S) === false, 'failed write reports false');
  const s3 = new Store(); s3.noSet = true; s3.d[L.SV_KEY] = '{oops';
  ok(L.svLoad(s3).problem === 'corrupt', 'backup failing does not throw');
}

// ---- operations --------------------------------------------------------------------------------------------------------
{
  const e = L.svEmpty();
  ok(L.svSave(e, '   ', F({}), 1).err === 'empty', 'save: empty name refused');
  const r1 = L.svSave(e, '  TVS   Oct ', F({ brand: ['TVS'], model: ['a', 'a'], junk: 1 }), 100);
  ok(!r1.err && r1.replaced === false && r1.S.views.length === 1, 'save: first view');
  const v1 = r1.S.views[0];
  ok(v1.name === 'TVS Oct' && v1.t === 100 && v1.u === 100 && L.svIdOk(v1.id), 'save: name cleaned, stamped');
  eq(v1.f, F({ brand: ['TVS'], model: ['a'] }), 'save: filters cleaned');
  ok(e.views.length === 0, 'save: input state untouched');
  const r2 = L.svSave(r1.S, 'tvs oct', F({ brand: ['Hero'] }), 200);
  ok(r2.replaced === true && r2.S.views.length === 1 && r2.id === v1.id && r2.S.views[0].t === 100 && r2.S.views[0].u === 200 &&
     r2.S.views[0].name === 'TVS Oct' && J(r2.S.views[0].f) === J(F({ brand: ['Hero'] })), 'save: same name (any case) replaces the filters, keeps id / created / name');
  const r3 = L.svSave(r2.S, 'Second', F({}), 300);
  ok(r3.S.views.length === 2 && r3.S.views[1].name === 'Second', 'save: appended in order');
  // overwrite
  ok(L.svOverwrite(r3.S, 'nope', F({}), 1).err === 'missing', 'overwrite: unknown id');
  const o1 = L.svOverwrite(r3.S, v1.id, F({ state: ['Bihar'] }), 400);
  ok(J(o1.S.views[0].f) === J(F({ state: ['Bihar'] })) && o1.S.views[0].u === 400 && o1.S.views[0].name === 'TVS Oct', 'overwrite: filters replaced, name kept');
  // rename
  ok(L.svRename(r3.S, v1.id, '  ', 1).err === 'empty', 'rename: empty');
  ok(L.svRename(r3.S, 'nope', 'X', 1).err === 'missing', 'rename: unknown id');
  ok(L.svRename(r3.S, v1.id, 'SECOND', 1).err === 'exists', 'rename: another view has that name (any case)');
  const n1 = L.svRename(r3.S, v1.id, 'tvs OCT', 500);
  ok(!n1.err && n1.S.views[0].name === 'tvs OCT', 'rename: only the case of its own name is fine');
  const n2 = L.svRename(r3.S, v1.id, 'Brand new name', 600);
  ok(n2.S.views[0].name === 'Brand new name' && n2.S.views[0].u === 600 && n2.S.views[0].id === v1.id, 'rename: new name, same id');
  // default
  ok(L.svSetDefault(r3.S, 'nope').err === 'missing', 'default: unknown id');
  const d1 = L.svSetDefault(r3.S, v1.id);
  ok(d1.S.def === v1.id, 'default: set');
  ok(L.svSetDefault(d1.S, null).S.def === null, 'default: cleared with null');
  ok(L.svRemove(d1.S, r3.S.views[1].id).S.def === v1.id, 'remove: another view keeps the default');
  const rm = L.svRemove(d1.S, v1.id);
  ok(rm.S.views.length === 1 && rm.S.def === null, 'remove: the default view takes the default with it');
  ok(L.svRemove(rm.S, v1.id).err === 'missing', 'remove: unknown id');
  // the cap
  let S = L.svEmpty();
  for (let i = 0; i < L.SV_MAX; i++) S = L.svSave(S, 'View ' + i, F({ brand: ['b' + i] }), i).S;
  ok(S.views.length === L.SV_MAX, 'cap: filled to SV_MAX');
  ok(L.svSave(S, 'One too many', F({}), 1).err === 'full', 'cap: a new name is refused');
  const rep = L.svSave(S, 'view 3', F({ brand: ['changed'] }), 9);
  ok(!rep.err && rep.replaced && rep.S.views.length === L.SV_MAX, 'cap: replacing an existing name still works at the limit');
  ok(L.svSave(L.svRemove(S, S.views[0].id).S, 'One too many', F({}), 1).S.views.length === L.SV_MAX, 'cap: room again after a delete');
}

// ---- random operations vs. the invariants ------------------------------------------------------------------------------
{
  let seed = 20261112; const rnd = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };
  const pool = ['TVS Oct', 'tvs oct', 'Jawa', 'JAWA ', 'Hero – MH', 'A', 'a', 'B', 'c', 'D', 'e', 'F', 'g', 'H', 'i', 'J', 'k', 'L', 'm', 'N', 'o', 'P', 'q', 'R',
    's', 'T', 'u', 'V', 'w', 'X', 'y', 'Z', 'a1', 'a2', 'a3', 'a4', '', '   '];
  const vals = ['TVS', 'Hero', 'Jawa', "Oct'2026", "Nov'2026", 'MC', 'PB', 'Bihar', 'Google'];
  const rf = () => { const f = {}; for (const id of L.SV_IDS) f[id] = vals.filter(() => rnd() < .25); f.ims = ['', 'Y', 'N', 'bad'][Math.floor(rnd() * 4)]; return f; };
  let S = L.svEmpty(), stats = { save: 0, over: 0, ren: 0, rm: 0, def: 0, err: 0 };
  for (let step = 0; step < 6000; step++) {
    const before = J(S), op = Math.floor(rnd() * 5), pickId = () => (S.views.length && rnd() < .9) ? S.views[Math.floor(rnd() * S.views.length)].id : 'zzz';
    let r;
    if (op === 0) { r = L.svSave(S, pool[Math.floor(rnd() * pool.length)], rf(), step); stats.save++; }
    else if (op === 1) { r = L.svOverwrite(S, pickId(), rf(), step); stats.over++; }
    else if (op === 2) { r = L.svRename(S, pickId(), pool[Math.floor(rnd() * pool.length)], step); stats.ren++; }
    else if (op === 3) { r = rnd() < .4 ? L.svRemove(S, pickId()) : { err: 'skip' }; stats.rm++; }
    else { r = L.svSetDefault(S, rnd() < .2 ? null : pickId()); stats.def++; }
    ok(J(S) === before, 'step ' + step + ': operation changed the state it was given');
    if (r.err) { stats.err++; ok(r.S === undefined, 'step ' + step + ': an error carries no new state'); continue; }
    S = r.S;
    const names = S.views.map(v => L.svCleanName(v.name).toLowerCase());
    ok(S.views.length <= L.SV_MAX, 'step ' + step + ': at most SV_MAX views');
    ok(new Set(names).size === names.length && names.every(n => n), 'step ' + step + ': names unique (any case) and non-empty');
    ok(S.views.every(v => L.svIdOk(v.id)) && new Set(S.views.map(v => v.id)).size === S.views.length, 'step ' + step + ': ids valid and unique');
    ok(S.def === null || S.views.some(v => v.id === S.def), 'step ' + step + ': default points at a view that exists');
    ok(S.views.every(v => J(L.svCleanFilters(v.f)) === J(v.f)), 'step ' + step + ': filters already clean');
    ok(J(L.svSanitize(JSON.parse(J({ v: 1, views: S.views, def: S.def })))) === J(S), 'step ' + step + ': what is stored reads back as exactly this state');
  }
  ok(stats.save > 500 && stats.over > 500 && stats.ren > 500 && stats.def > 500 && stats.err > 300, 'random run exercised every operation and plenty of refusals: ' + J(stats));
}

console.log(`${checks} checks, ${fails} failed`);
process.exit(fails ? 1 : 0);
