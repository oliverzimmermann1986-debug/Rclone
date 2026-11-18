'use strict';

// Trusted controller helper. Repository code still runs only in the existing
// credential-free containers; PR support is an explicit per-repository opt-in.
const REPOSITORY = /^[a-zA-Z0-9-]+\/[a-zA-Z0-9_.-]+$/;
const PULL_REF = /^refs\/pull\/([1-9][0-9]{0,9})\/head$/;
const PULL_SUPERSEDED = 'Durch neueren Pull-Request-Commit ersetzt';

function pullRequestRepos(value, owner) {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 64 || typeof owner !== 'string')
    throw new Error('Pull-Request-Konfiguration ungueltig');
  const normalized = value.map(repo => {
    if (typeof repo !== 'string' || !REPOSITORY.test(repo) ||
        repo.split('/')[0].toLowerCase() !== owner.toLowerCase())
      throw new Error('Pull-Request-Repositories muessen zum konfigurierten Konto gehoeren');
    return repo.toLowerCase();
  });
  if (new Set(normalized).size !== normalized.length)
    throw new Error('Pull-Request-Repository mehrfach konfiguriert');
  return normalized;
}

function isPullRef(ref) { return typeof ref === 'string' && PULL_REF.test(ref); }

function isAllowedPullRef(repo, ref, configured = []) {
  return typeof repo === 'string' && isPullRef(ref) &&
    configured.some(value => value.toLowerCase() === repo.toLowerCase());
}

async function augmentRefs(api, repo, base, refs, configured = []) {
  if (!isAllowedPullRef(repo, 'refs/pull/1/head', configured)) return refs;
  if (!REPOSITORY.test(repo) || typeof base !== 'string' || !base || /[\x00-\x20\x7f]/.test(base))
    throw new Error('Pull-Request-Abfrage ungueltig');
  const rows = await api.pages(`/repos/${repo}/pulls?state=open&base=${encodeURIComponent(base)}`);
  if (!Array.isArray(rows)) throw new Error('GitHub API: Pull-Request-Liste erwartet');
  const found = new Map();
  for (const row of rows) {
    // Forks and unrelated targets never extend the trusted repository scope.
    if (row?.state !== 'open' || row.base?.ref !== base ||
        row.base?.repo?.full_name?.toLowerCase() !== repo.toLowerCase() ||
        row.head?.repo?.full_name?.toLowerCase() !== repo.toLowerCase() ||
        row.head?.repo?.fork === true) continue;
    if (!Number.isSafeInteger(row.number) || row.number <= 0 || row.number > 9999999999 ||
        !/^[a-f0-9]{40}$/.test(row.head?.sha || ''))
      throw new Error('GitHub API: ungueltiger Pull-Request-Commit');
    const ref = `refs/pull/${row.number}/head`;
    if (found.has(ref) && found.get(ref) !== row.head.sha)
      throw new Error('GitHub API: widerspruechlicher Pull-Request-Commit');
    found.set(ref, row.head.sha);
  }
  return [...refs, ...Array.from(found, ([ref, sha]) => ({ ref, sha }))];
}

function preserveSharedPullRun(refs, replacedRef) {
  // The result belongs to an exact SHA. Keep work still shared with another
  // PR/main ref; a replacement gets its own SHA/job and can never inherit green.
  return refs.some(isPullRef) && refs.some(ref => ref !== replacedRef);
}

module.exports = { PULL_SUPERSEDED, pullRequestRepos, isPullRef, isAllowedPullRef,
  augmentRefs, preserveSharedPullRun };
