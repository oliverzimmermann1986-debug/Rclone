'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { augmentRefs, pullRequestRepos, requiredProfiles, isPullRef, isAllowedPullRef,
  preserveSharedPullRun, PULL_SUPERSEDED } = require('./pull-requests.cjs');
// Run from a staging copy of the real controller after applying the manifest.
// Missing controller files are an error, never a skipped integration check.
const { Queue, discover, refsOf, cancellationReason, validate } = require('./core.cjs');
const { configuration } = require('./service.cjs');
const { Executor } = require('./executor.cjs');

const repo = 'owner/Rclone', branch = 'refs/heads/main', pr = 'refs/pull/12/head';
const sha = n => n.toString(16).padStart(40, '0');
const configured = [repo.toLowerCase()];
const check = { version: 1, image: 'rclone', command: 'bash scripts/ci/verify.sh', timeoutSeconds: 7200, postgres: false };
const config = { owner: 'owner', accountType: 'user', logRepo: 'owner/logs', images: { rclone: 'sha256:' + 'a'.repeat(64) },
  checkTokenExpires: '2030-01-01', logTokenExpires: '2030-01-01', pullRequestRepos: configured,
  requiredProfiles: { 'owner/rclone': 'rclone' } };
const row = (number = 12, commit = sha(2)) => ({ number, state: 'open', base: { ref: 'main', repo: { full_name: repo } },
  head: { sha: commit, repo: { full_name: repo, fork: false } } });
function apiFor(rows, refs = [{ ref: branch, sha: sha(1) }]) {
  return { repos: async () => [{ full_name: repo, owner: { login: 'owner' }, default_branch: 'main' }],
    refs: async () => refs, pages: async () => rows, file: async () => JSON.stringify(check) };
}

test('PR configuration is optional, scoped to owner and normalized', () => {
  assert.deepEqual(pullRequestRepos(undefined, 'owner'), []);
  assert.deepEqual(pullRequestRepos(['OWNER/Rclone'], 'owner'), configured);
  assert.deepEqual(configuration({ ...config, pullRequestRepos: undefined }).pullRequestRepos, []);
  for (const value of [null, 'owner/Rclone', ['foreign/Rclone'], ['owner/Rclone', 'OWNER/rclone'], ['owner/Rclone\nother']])
    assert.throws(() => pullRequestRepos(value, 'owner'), /Pull-Request/);
});

test('non-opted repositories do not query PRs or alter existing branch/tag refs', async () => {
  const refs = [{ ref: branch, sha: sha(1) }, { ref: 'refs/tags/v1', sha: sha(1) }];
  assert.strictEqual(await augmentRefs({ pages: () => assert.fail('not opted in') }, repo, 'main', refs, []), refs);
  assert.equal(isAllowedPullRef('owner/Other', pr, configured), false);
  for (const ref of ['refs/pull/0/head', 'refs/pull/1/merge', 'refs/heads/feature', 'refs/pull/1/head\nother'])
    assert.equal(isPullRef(ref), false);
});

test('PR requests use paginated API helper and encode the target branch', async () => {
  const paths = [];
  const r = row(); r.base.ref = 'release/stable';
  const refs = await augmentRefs({ pages: async path => { paths.push(path); return [r, r]; } }, repo, 'release/stable', [], configured);
  assert.deepEqual(paths, ['/repos/owner/Rclone/pulls?state=open&base=release%2Fstable']);
  assert.deepEqual(refs, [{ ref: pr, sha: sha(2) }]);
});

test('fork, closed, removed-head and foreign/base-unrelated PRs are excluded', async () => {
  const variants = [row(), row(), row(), row(), row(), row()];
  variants[0].head.repo.full_name = 'foreign/Rclone';
  variants[1].head.repo.fork = true;
  variants[2].state = 'closed';
  variants[3].head.repo = null;
  variants[4].base.ref = 'other';
  variants[5].base.repo.full_name = 'owner/Other';
  assert.deepEqual(await augmentRefs({ pages: async () => variants }, repo, 'main', [], configured), []);
});

test('invalid PR identity, conflicting snapshots and API outages fail closed', async () => {
  for (const invalid of [row(0), row(1.5), row(1, 'x'.repeat(40)), row(1, sha(2) + '\n')])
    await assert.rejects(augmentRefs({ pages: async () => [invalid] }, repo, 'main', [], configured), /ungueltiger/);
  await assert.rejects(augmentRefs({ pages: async () => [row(12, sha(2)), row(12, sha(3))] }, repo, 'main', [], configured), /widerspruechlicher/);
  await assert.rejects(augmentRefs({ pages: async () => { throw new Error('API unavailable'); } }, repo, 'main', [], configured), /API unavailable/);
});

test('discovery checks opted same-repo PR exact head and keeps default/tag behavior', async () => {
  const q = new Queue(), api = apiFor([row()], [{ ref: branch, sha: sha(1) }, { ref: 'refs/tags/v1', sha: sha(1) }]);
  await discover(api, config, q); await discover(api, config, q);
  assert.equal(q.jobs.length, 2);
  assert.deepEqual(refsOf(q.jobs.find(j => j.sha === sha(1))), ['refs/tags/v1', branch]);
  assert.deepEqual(refsOf(q.jobs.find(j => j.sha === sha(2))), [pr]);
  assert.equal(q.jobs.find(j => j.sha === sha(2)).config.image, 'rclone');
});

test('a replaced sole PR is cancelled, new head is not green and other PRs survive', () => {
  const q = new Queue();
  const old = q.add(repo, pr, sha(1), check), running = q.next();
  const other = q.add(repo, 'refs/pull/13/head', sha(3), check);
  const next = q.add(repo, pr, sha(2), check);
  assert.equal(running.signal.aborted, true);
  assert.equal(running.signal.reason, PULL_SUPERSEDED);
  assert.equal(cancellationReason(running.signal), PULL_SUPERSEDED);
  assert.equal(old.result, undefined, 'running result is published only after cleanup');
  assert.equal(next.result, undefined);
  assert.equal(other.phase, 'queued');
});

test('queued PR replacement has an explicit non-success result', () => {
  const q = new Queue(), old = q.add(repo, pr, sha(1), check);
  q.add(repo, pr, sha(2), check);
  assert.equal(old.result.state, 'error');
  assert.equal(old.result.description, PULL_SUPERSEDED);
});

test('API errors or absent checkfile cannot silently replace a PR with green', async () => {
  const q = new Queue();
  q.add(repo, pr, sha(1), check); const running = q.next();
  const api = apiFor([row(12, sha(2))], []); api.file = async () => null;
  await discover(api, config, q);
  assert.equal(running.signal.aborted, true);
  assert.equal(q.jobs.some(j => j.sha === sha(2)), false);
  api.pages = async () => { throw new Error('HTTP 403'); };
  await assert.rejects(discover(api, config, q), /HTTP 403/);
});

test('one SHA shared by PR/main/tag stays one run and survives ref replacements', () => {
  const q = new Queue();
  const shared = q.add(repo, pr, sha(1), check), running = q.next();
  q.add(repo, branch, sha(1), check);
  q.add(repo, 'refs/pull/13/head', sha(1), check);
  assert.equal(q.jobs.length, 1);
  q.add(repo, branch, sha(2), check);
  q.add(repo, pr, sha(3), check);
  assert.equal(running.signal.aborted, false);
  q.add(repo, 'refs/tags/v1', sha(1), check);
  q.finish(shared, { result: { state: 'success', description: 'All gates passed' } }); shared.phase = 'done';
  assert.equal(q.status(shared).state, 'success');
  assert.equal(q.jobs.find(j => j.sha === sha(3)).result, undefined, 'different PR head cannot inherit success');
  assert.equal(preserveSharedPullRun([pr], pr), false);
  assert.equal(preserveSharedPullRun([pr, branch], branch), true);
});

test('a superseded PR attempt can be retried as tag without adopting cancellation', () => {
  const q = new Queue(), old = q.add(repo, pr, sha(1), check);
  q.add(repo, pr, sha(2), check);
  old.phase = 'done';
  const tagged = q.add(repo, 'refs/tags/v1', sha(1), check);
  assert.ok(tagged && tagged.id !== old.id);
  assert.equal(q.next().job, tagged);
  q.finish(tagged, { result: { state: 'success', description: 'All gates passed' } });
  assert.equal(q.status(tagged).state, 'success');
});

test('rclone exit zero without a real report remains an error', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'controller-report-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const run = async (bin, args, options) => {
    assert.ok(bin === 'git' || bin === 'chmod' || bin === 'docker');
    if (bin === 'docker' && args.includes('cp') && options?.binary)
      return { code: 1, output: Buffer.alloc(0) };
    return { code: 0, output: '' };
  };
  const executor = new Executor({ ...config, stateDir: directory }, run);
  const job = { id: 'a'.repeat(64), repo, sha: sha(1), ref: branch, refs: [branch], config: check };
  const output = await executor.execute(job, new AbortController().signal);
  assert.equal(output.result.state, 'error');
  assert.match(output.result.description, /Release-Report/);
  assert.equal(output.report, undefined);
  assert.equal(executor.slots.size, 0, 'cleanup releases the slot');
});

test('required profile configuration is optional, owner-scoped and registered', () => {
  assert.deepEqual(requiredProfiles(undefined, 'owner', config.images), {});
  assert.deepEqual(requiredProfiles({ 'OWNER/Rclone': 'rclone' }, 'owner', config.images), { 'owner/rclone': 'rclone' });
  for (const value of [null, [], { 'foreign/Rclone': 'rclone' }, { 'owner/Rclone': 'missing' },
    { 'owner/Rclone': 'rclone', 'OWNER/rclone': 'rclone' }])
    assert.throws(() => requiredProfiles(value, 'owner', config.images));
});

test('Rclone cannot switch to a generic profile or shorten its required command', async () => {
  const q = new Queue(), api = apiFor([row()]);
  api.file = async () => JSON.stringify({ ...check, image: 'rezepte', command: 'true' });
  await discover(api, { ...config, images: { ...config.images, rezepte: 'sha256:' + 'b'.repeat(64) } }, q);
  assert.ok(q.jobs.length > 0 && q.jobs.every(job => job.phase === 'publishing' && job.result.state === 'error'));
  assert.equal(q.next(), null);
  assert.throws(() => validate(JSON.stringify({ ...check, command: 'true' }), config.images), /vollstaendigen/);
  assert.throws(() => validate(JSON.stringify({ ...check, postgres: true }), config.images), /vollstaendigen/);
});

test('work shared by two PRs stops only after both heads have been replaced', () => {
  const q = new Queue(), old = q.add(repo, pr, sha(1), check), running = q.next();
  const second = 'refs/pull/13/head';
  q.add(repo, second, sha(1), check);
  q.add(repo, pr, sha(2), check);
  assert.equal(running.signal.aborted, false);
  assert.deepEqual(refsOf(old), [pr, second], 'historical refs remain available for the log');
  q.add(repo, second, sha(3), check);
  assert.equal(running.signal.aborted, true);
  assert.equal(running.signal.reason, PULL_SUPERSEDED);
});

test('an existing ref returning to a still-live shared SHA is reactivated', () => {
  const q = new Queue(), old = q.add(repo, pr, sha(1), check), running = q.next();
  const second = 'refs/pull/13/head';
  q.add(repo, second, sha(1), check);
  q.add(repo, pr, sha(2), check);
  assert.deepEqual(old.replacedRefs, [pr]);
  assert.equal(q.known(repo, pr, sha(1)), true);
  assert.deepEqual(old.replacedRefs, []);
  q.add(repo, second, sha(3), check);
  assert.equal(running.signal.aborted, false);
  q.add(repo, pr, sha(4), check);
  assert.equal(running.signal.aborted, true);
});
