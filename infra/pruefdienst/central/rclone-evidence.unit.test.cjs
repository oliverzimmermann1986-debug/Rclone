'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { EVIDENCE_NAME, EVIDENCE_PATH, MAX_BYTES, TAR_MAX_BYTES, GATES, validateReport,
  validateFailureReport, metadata, tarPayload, captureEvidence, validateArtifact, artifactPath, artifactReference } = require('./rclone-evidence.cjs');
const { Executor } = require('./executor.cjs');
const { LogRepo } = require('./github.cjs');

const sha = 'a'.repeat(40), zip = Buffer.from([0x50, 0x4b, 0x03, 0x04, 1, 2, 3, 4]);
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const nodeTests = () => ({ tests: 40, pass: 40, fail: 0, cancelled: 0, skipped: 0, todo: 0 });
const tests = total => ({ total, passed: total, failed: [], skipped: [] });
const job = () => ({ id: 'a'.repeat(64), repo: 'owner/Rclone', sha, ref: 'refs/heads/main',
  refs: ['refs/heads/main'], created: '2026-11-18T10:00:00.000Z',
  config: { version: 1, image: 'rclone', command: 'bash scripts/ci/verify.sh', timeoutSeconds: 7200, postgres: false } });
function report() {
  const gates = GATES.map(id => ({ id, status: 'passed', durationMs: 1 }));
  const get = id => gates.find(gate => gate.id === id);
  const names = Array.from({ length: 48 }, (_, n) => `web-visual/case-${n}.png`);
  names.push(...Array.from({ length: 6 }, (_, n) => `web-visual/case-${n}-trace.zip`), 'web-visual/report.json');
  for (const version of ['3.10', '3.11', '3.12', '3.13']) {
    get(`python-tests-${version}`).tests = tests(1704);
    names.push(`python-tests-${version}.xml`);
  }
  for (const id of ['oauth-legacy', 'oauth-production']) {
    get(id).tests = tests(2); names.push(`${id}.xml`);
  }
  get('web-visual').tests = { ...tests(48), chromium: '159.0.8154.6' };
  get('central-controller').tests = nodeTests();
  get('central-controller').suites = ['core-status.unit.test.cjs', 'befund.unit.test.cjs',
    'pull-requests.unit.test.cjs', 'rclone-evidence.unit.test.cjs'];
  get('web-behavior').suites = { 'runtime-behavior.cjs': nodeTests(), 'review-regressions.cjs': nodeTests(),
    'workspace-dom.cjs': { status: 'passed' }, 'workspace-alpine.cjs': { status: 'passed' } };
  get('evidence').files = names.length; get('evidence').archiveBytes = zip.length;
  return { schemaVersion: 1, profile: 'rclone', sha, passed: true, startedAt: 1, finishedAt: 2, gates,
    evidenceFiles: names, artifacts: [{ name: EVIDENCE_NAME, path: EVIDENCE_PATH, encoding: 'binary',
      sha256: digest(zip), bytes: zip.length, files: names.length }] };
}

function tar(name, bytes, type = '0') {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, 'ascii');
  header.write('0000600\0', 100, 8, 'ascii');
  header.write('0001750\0', 108, 8, 'ascii'); header.write('0001750\0', 116, 8, 'ascii');
  header.write(bytes.length.toString(8).padStart(11, '0') + '\0', 124, 12, 'ascii');
  header.write('00000000001\0', 136, 12, 'ascii');
  header.fill(32, 148, 156); header.write(type, 156, 1, 'ascii');
  header.write('ustar\0', 257, 6, 'ascii'); header.write('00', 263, 2, 'ascii');
  const checksum = header.reduce((sum, value) => sum + value, 0);
  header.write(checksum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'ascii');
  return Buffer.concat([header, bytes, Buffer.alloc(Math.ceil(bytes.length / 512) * 512 - bytes.length + 1024)]);
}

const artifact = () => ({ name: EVIDENCE_NAME, encoding: 'base64', content: zip.toString('base64'),
  sha256: digest(zip), bytes: zip.length });

test('only the complete exact-SHA 21-gate report can be green', () => {
  const good = report(); assert.equal(GATES.length, 21); assert.strictEqual(validateReport(good, sha), good);
  const mutations = [value => { value.sha = 'b'.repeat(40); }, value => { value.passed = false; },
    value => { value.gates.pop(); }, value => { value.gates[1].id = value.gates[0].id; },
    value => { value.gates[0].id = 'unknown'; }, value => { value.gates[0].status = 'not_run'; },
    value => { value.gates[0].durationMs = -1; }, value => { value.finishedAt = 0; }];
  for (const mutate of mutations) { const value = report(); mutate(value); assert.throws(() => validateReport(value, sha)); }
  assert.throws(() => validateReport({ gates: [{ id: 'runtime', status: 'passed' }] }, sha));
});

test('matrix, OAuth, controller, web and browser counts must be complete without skips', () => {
  for (const [id, mutate] of [
    ['python-tests-3.10', gate => { gate.tests.total = gate.tests.passed = 1703; }],
    ['python-tests-3.13', gate => { gate.tests.skipped = ['missing capability']; }],
    ['oauth-production', gate => { gate.tests.total = gate.tests.passed = 1; }],
    ['central-controller', gate => { gate.tests.skipped = 1; }],
    ['central-controller', gate => { gate.suites.pop(); }],
    ['web-visual', gate => { gate.tests.total = gate.tests.passed = 47; }],
    ['web-visual', gate => { gate.tests.chromium = 'other'; }],
    ['web-behavior', gate => { gate.suites['runtime-behavior.cjs'].tests = 0; }],
  ]) {
    const value = report(); mutate(value.gates.find(gate => gate.id === id));
    assert.throws(() => validateReport(value, sha));
  }
});

test('evidence metadata is fixed, bounded, complete and never contains inline payload', () => {
  for (const change of [{ path: '../secret' }, { name: 'other.zip' }, { encoding: 'base64' },
    { bytes: MAX_BYTES + 1 }, { bytes: 0 }, { files: '61' }, { content: 'inline' }]) {
    const value = report(); Object.assign(value.artifacts[0], change); assert.throws(() => metadata(value));
  }
  const duplicate = report(); duplicate.evidenceFiles[1] = duplicate.evidenceFiles[0];
  assert.throws(() => validateReport(duplicate, sha));
  const traversal = report(); traversal.evidenceFiles[0] = '../private.png';
  assert.throws(() => validateReport(traversal, sha));
  const missing = report(); missing.evidenceFiles[0] = 'wrong-file.txt';
  assert.throws(() => validateReport(missing, sha));
});

test('single regular Docker tar payload is checked without extraction', () => {
  assert.deepEqual(tarPayload(tar(EVIDENCE_NAME, zip), metadata(report())), zip);
  const wrongChecksum = tar(EVIDENCE_NAME, zip); wrongChecksum[100] ^= 1;
  const extra = tar(EVIDENCE_NAME, zip); extra[1024] = 1;
  for (const invalid of [tar('../' + EVIDENCE_NAME, zip), tar(EVIDENCE_NAME, zip, '2'),
    tar(EVIDENCE_NAME, zip, 'x'), wrongChecksum, extra, tar(EVIDENCE_NAME, zip).subarray(0, 1024)])
    assert.throws(() => tarPayload(invalid, metadata(report())));
  const mismatched = metadata(report()); mismatched.bytes++;
  assert.throws(() => tarPayload(tar(EVIDENCE_NAME, zip), mismatched));
});

test('ZIP capture uses only the fixed container path and a bounded binary transfer', async () => {
  const signal = new AbortController().signal;
  const captured = await captureEvidence(async (bin, args, options) => {
    assert.equal(bin, 'docker');
    assert.deepEqual(args, ['--host', 'unix:///isolated.sock', 'cp', 'check-known:/work/repo/reports/rclone-ci-evidence.zip', '-']);
    assert.equal(options.binary, true); assert.equal(options.maxBytes, TAR_MAX_BYTES); assert.equal(options.signal, signal);
    return { code: 0, output: tar(EVIDENCE_NAME, zip) };
  }, ['--host', 'unix:///isolated.sock'], 'check-known', report(), signal);
  assert.deepEqual(captured, artifact());
  await assert.rejects(captureEvidence(async () => ({ code: 1, output: Buffer.alloc(0) }), [], 'known', report(), signal));
});

test('publication rechecks canonical base64, SHA, size and ZIP magic', () => {
  const good = artifact(); assert.strictEqual(validateArtifact(good), good);
  for (const change of [{ content: good.content.replace('UE', '__') }, { sha256: '0'.repeat(64) },
    { bytes: zip.length + 1 }, { encoding: 'binary' }, { name: '../other.zip' }, { extra: 'unexpected' }])
    assert.throws(() => validateArtifact({ ...good, ...change }));
  const invalidZip = Buffer.from('not-a-zip');
  assert.throws(() => validateArtifact({ ...good, content: invalidZip.toString('base64'),
    bytes: invalidZip.length, sha256: digest(invalidZip) }));
  assert.equal(artifactPath(job()), 'logs/2026-11-18/' + 'a'.repeat(64) + '.zip');
  assert.equal(artifactReference(job(), good).name, 'a'.repeat(64) + '.zip');
  assert.throws(() => artifactPath({ ...job(), id: '../other' }));
});

function logApi(entries = []) {
  const calls = [];
  return { calls, request: async (url, method = 'GET', body) => {
    calls.push({ url, method, body });
    if (url === '/repos/owner/logs') return { private: true };
    if (url.endsWith('/git/ref/heads/logs')) return { object: { sha: 'old' } };
    if (url.endsWith('/git/commits/old')) return { tree: { sha: 'old-tree' } };
    if (url.endsWith('/git/trees/old-tree?recursive=1')) return { truncated: false, tree: entries };
    return { sha: 'b'.repeat(40) };
  } };
}

test('LogRepo publishes a binary Git blob and retains only bounded ZIP filenames for 30 days', async () => {
  const recent = `logs/2026-11-17/${'c'.repeat(64)}.zip`;
  const expired = `logs/2026-10-01/${'d'.repeat(64)}.zip`;
  const unsafe = 'logs/2026-11-17/arbitrary.zip';
  const api = logApi([recent, expired, unsafe].map(value => ({ path: value, type: 'blob', mode: '100644', sha: 'old-blob' })));
  await new LogRepo(api, 'owner/logs').update(job(), 'safe log', Date.parse('2026-11-18T12:00:00Z'), { artifact: artifact(), report: '{}' });
  const binaries = api.calls.filter(call => call.url.endsWith('/git/blobs') && call.body?.encoding === 'base64');
  assert.equal(binaries.length, 1); assert.equal(binaries[0].body.content, zip.toString('base64'));
  const tree = api.calls.find(call => call.url.endsWith('/git/trees') && call.method === 'POST').body.tree;
  assert.ok(tree.some(entry => entry.path === recent));
  assert.ok(tree.some(entry => entry.path === artifactPath(job())));
  assert.equal(tree.some(entry => entry.path === expired || entry.path === unsafe), false);
  const commit = api.calls.find(call => call.url.endsWith('/git/commits') && call.method === 'POST');
  assert.deepEqual(commit.body.parents, [], 'expired ZIPs do not survive in reachable snapshot history');
});

test('invalid ZIPs and non-Rclone jobs cannot create a binary Git blob', async () => {
  for (const [value, extra] of [[job(), { ...artifact(), sha256: '0'.repeat(64) }],
    [{ ...job(), config: { image: 'rezepte' } }, artifact()]]) {
    const api = logApi();
    await assert.rejects(new LogRepo(api, 'owner/logs').update(value, '', Date.parse('2026-11-18T12:00:00Z'), { artifact: extra }));
    assert.equal(api.calls.some(call => call.body?.encoding === 'base64'), false);
  }
});

async function execute(t, value, evidenceCode = 0, runCode = 0) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'controller-evidence-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  let evidenceReads = 0;
  const run = async (bin, args, options) => {
    if (bin === 'docker' && args.includes('start')) return { code: runCode, output: '' };
    if (bin === 'docker' && args.includes('cp') && options?.binary) {
      if (args.some(arg => arg.includes('/reports/unified-quality-report.json')))
        return { code: 0, output: tar('unified-quality-report.json', Buffer.from(JSON.stringify(value))) };
      evidenceReads++;
      return { code: evidenceCode, output: tar(EVIDENCE_NAME, zip) };
    }
    return { code: 0, output: '' };
  };
  const executor = new Executor({ stateDir: directory, images: { rclone: 'sha256:' + 'f'.repeat(64) } }, run);
  return { output: await executor.execute(job(), new AbortController().signal), evidenceReads };
}

test('a complete successful run returns the separately verified ZIP', async t => {
  const { output, evidenceReads } = await execute(t, report());
  assert.equal(output.result.state, 'success'); assert.equal(evidenceReads, 1);
  assert.deepEqual(output.artifact, artifact());
});

test('a partial exit-zero report cannot trigger ZIP upload or green status', async t => {
  const { output, evidenceReads } = await execute(t, { gates: [{ id: 'runtime', status: 'passed' }] });
  assert.equal(output.result.state, 'error'); assert.equal(evidenceReads, 0);
  assert.equal(output.artifact, undefined);
});

test('a missing ZIP after an otherwise complete run remains an error', async t => {
  const { output, evidenceReads } = await execute(t, report(), 1);
  assert.equal(output.result.state, 'error'); assert.equal(evidenceReads, 1);
  assert.equal(output.artifact, undefined);
});

test('complete failed runs preserve bounded evidence and the actual failed gate', async t => {
  const failed = report(); failed.passed = false;
  failed.gates.find(gate => gate.id === 'web-visual').status = 'failed';
  failed.evidenceFiles = ['web-visual/failed-case.png', 'web-visual/report.json'];
  failed.artifacts[0].files = failed.evidenceFiles.length;
  const { output, evidenceReads } = await execute(t, failed, 0, 1);
  assert.equal(output.result.state, 'failure'); assert.equal(evidenceReads, 1);
  assert.match(output.result.description, /web-visual/);
  assert.deepEqual(output.artifact, artifact());
  assert.throws(() => validateReport(failed, sha), 'failed evidence can never satisfy the green contract');
  assert.strictEqual(validateFailureReport(failed, sha), failed);
});

test('missing failed-run ZIP does not hide the original failed gate or become green', async t => {
  const failed = report(); failed.passed = false;
  failed.gates.find(gate => gate.id === 'web-visual').status = 'failed';
  const { output } = await execute(t, failed, 1, 1);
  assert.equal(output.result.state, 'failure'); assert.match(output.result.description, /web-visual/);
  assert.equal(output.artifact, undefined); assert.ok(output.report.controllerEvidenceError);
});

test('nonzero exit still rejects partial, false-SHA and contradictory failure reports', async t => {
  for (const value of [{ gates: [{ id: 'runtime', status: 'failed' }] },
    { ...report(), sha: 'b'.repeat(40) }, { ...report(), passed: false }]) {
    const { output, evidenceReads } = await execute(t, value, 0, 1);
    assert.equal(output.result.state, 'error'); assert.equal(evidenceReads, 0);
    assert.equal(output.artifact, undefined);
  }
});
