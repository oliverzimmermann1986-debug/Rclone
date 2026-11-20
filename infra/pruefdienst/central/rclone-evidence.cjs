'use strict';
const crypto = require('node:crypto');

const EVIDENCE_NAME = 'rclone-ci-evidence.zip';
const EVIDENCE_PATH = `reports/${EVIDENCE_NAME}`;
const MAX_BYTES = 64 * 1024 * 1024;
const TAR_MAX_BYTES = MAX_BYTES + 64 * 1024;
const DIGEST = /^[a-f0-9]{64}$/;
const PYTHONS = ['3.10', '3.11', '3.12', '3.13'];
const GATES = ['runtime', 'central-controller',
  ...PYTHONS.flatMap(version => ['dependencies', 'quality', 'tests'].map(kind => `python-${kind}-${version}`)),
  'oauth-legacy', 'oauth-production', 'installer', 'web-dependencies', 'web-behavior', 'web-visual', 'evidence'];

function passingNode(value) {
  return value && ['tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'].every(key =>
    Number.isSafeInteger(value[key]) && value[key] >= 0) && value.tests > 0 &&
    value.pass === value.tests && ['fail', 'cancelled', 'skipped', 'todo'].every(key => value[key] === 0);
}

function passingTests(value, minimum, exact) {
  return value && Number.isSafeInteger(value.total) && value.total >= minimum &&
    (exact === undefined || value.total === exact) && value.passed === value.total &&
    Array.isArray(value.failed) && value.failed.length === 0 &&
    Array.isArray(value.skipped) && value.skipped.length === 0;
}

function validateReport(report, sha) {
  if (!report || report.schemaVersion !== 1 || report.profile !== 'rclone' ||
      !/^[a-f0-9]{40}$/.test(sha || '') || report.sha !== sha || report.passed !== true ||
      !Number.isFinite(report.startedAt) || !Number.isFinite(report.finishedAt) ||
      report.startedAt <= 0 || report.finishedAt < report.startedAt ||
      !Array.isArray(report.gates) || report.gates.length !== GATES.length)
    throw new Error('Rclone-Release-Report ist unvollstaendig oder gehoert zu einem anderen Commit');
  const gates = new Map(report.gates.map(gate => [gate?.id, gate]));
  if (gates.size !== GATES.length || GATES.some(id => !gates.has(id)) ||
      report.gates.some(gate => gate.status !== 'passed' || !Number.isFinite(gate.durationMs) || gate.durationMs < 0))
    throw new Error('Rclone-Release-Report enthaelt fehlende, doppelte oder nicht gruene Gates');
  for (const version of PYTHONS)
    if (!passingTests(gates.get(`python-tests-${version}`).tests, 1704))
      throw new Error('Rclone-Python-Matrix ist unvollstaendig');
  for (const id of ['oauth-legacy', 'oauth-production'])
    if (!passingTests(gates.get(id).tests, 2, 2)) throw new Error('Rclone-OAuth-Pruefung ist unvollstaendig');
  if (!passingTests(gates.get('web-visual').tests, 48, 48) ||
      gates.get('web-visual').tests.chromium !== '159.0.8154.6')
    throw new Error('Rclone-Browserpruefung ist unvollstaendig');
  const central = gates.get('central-controller');
  if (!passingNode(central.tests) || !Array.isArray(central.suites) ||
      !['pull-requests.unit.test.cjs', 'rclone-evidence.unit.test.cjs'].every(name => central.suites.includes(name)))
    throw new Error('Rclone-Controllerpruefung ist unvollstaendig');
  const web = gates.get('web-behavior').suites;
  if (!web || !['runtime-behavior.cjs', 'review-regressions.cjs'].every(name => passingNode(web[name])) ||
      !['workspace-dom.cjs', 'workspace-alpine.cjs'].every(name => web[name]?.status === 'passed'))
    throw new Error('Rclone-Webpruefung ist unvollstaendig');
  const expected = metadata(report), names = report.evidenceFiles;
  if (!Array.isArray(names) || names.length !== expected.files || new Set(names).size !== names.length ||
      names.some(name => typeof name !== 'string' || !name || name.startsWith('/') ||
        /[\\\x00-\x1f\x7f]/.test(name) || name.split('/').some(part => !part || part === '.' || part === '..')) ||
      names.filter(name => name.startsWith('web-visual/') && name.endsWith('.png')).length !== 48 ||
      names.filter(name => name.startsWith('web-visual/') && name.endsWith('-trace.zip')).length !== 6 ||
      !names.includes('web-visual/report.json') ||
      !PYTHONS.every(version => names.includes(`python-tests-${version}.xml`)) ||
      !['oauth-legacy.xml', 'oauth-production.xml'].every(name => names.includes(name)) ||
      gates.get('evidence').files !== expected.files || gates.get('evidence').archiveBytes !== expected.bytes)
    throw new Error('Rclone-Nachweisverzeichnis ist unvollstaendig');
  return report;
}

function validateFailureReport(report, sha) {
  if (report?.passed === true) return validateReport(report, sha);
  if (!report || report.schemaVersion !== 1 || report.profile !== 'rclone' ||
      !/^[a-f0-9]{40}$/.test(sha || '') || report.sha !== sha || report.passed !== false ||
      !Number.isFinite(report.startedAt) || !Number.isFinite(report.finishedAt) ||
      report.startedAt <= 0 || report.finishedAt < report.startedAt ||
      !Array.isArray(report.gates) || report.gates.length !== GATES.length)
    throw new Error('Rclone-Fehlerreport ist unvollstaendig oder gehoert zu einem anderen Commit');
  const gates = new Map(report.gates.map(gate => [gate?.id, gate]));
  if (gates.size !== GATES.length || GATES.some(id => !gates.has(id)) ||
      report.gates.some(gate => !['passed', 'failed', 'not_run'].includes(gate.status) ||
        !Number.isFinite(gate.durationMs) || gate.durationMs < 0) ||
      report.gates.every(gate => gate.status === 'passed'))
    throw new Error('Rclone-Fehlerreport enthaelt einen ungueltigen Gate-Plan');
  const expected = metadata(report), names = report.evidenceFiles;
  if (!Array.isArray(names) || names.length !== expected.files || new Set(names).size !== names.length ||
      names.some(name => typeof name !== 'string' || !name || name.startsWith('/') ||
        /[\\\x00-\x1f\x7f]/.test(name) || name.split('/').some(part => !part || part === '.' || part === '..')))
    throw new Error('Rclone-Fehlernachweisverzeichnis ist ungueltig');
  return report;
}

function metadata(report) {
  if (!Array.isArray(report?.artifacts) || report.artifacts.length !== 1)
    throw new Error('Rclone ZIP-Nachweis fehlt oder ist mehrdeutig');
  const value = report.artifacts[0];
  if (!value || Object.keys(value).sort().join(',') !== 'bytes,encoding,files,name,path,sha256' ||
      value.name !== EVIDENCE_NAME || value.path !== EVIDENCE_PATH || value.encoding !== 'binary' ||
      !DIGEST.test(value.sha256 || '') || !Number.isSafeInteger(value.bytes) ||
      value.bytes < 4 || value.bytes > MAX_BYTES || !Number.isSafeInteger(value.files) || value.files <= 0)
    throw new Error('Rclone ZIP-Nachweismetadaten ungueltig');
  return value;
}

function verifyBytes(bytes, expected) {
  if (!Buffer.isBuffer(bytes) || bytes.length !== expected.bytes || bytes.length > MAX_BYTES ||
      bytes.length < 4 || !bytes.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04])) ||
      crypto.createHash('sha256').update(bytes).digest('hex') !== expected.sha256)
    throw new Error('Rclone ZIP-Nachweis: Groesse, Format oder SHA-256 stimmt nicht');
}

function tarPayload(tar, expected) {
  if (!Buffer.isBuffer(tar) || tar.length < 1536 || tar.length > TAR_MAX_BYTES || tar.length % 512)
    throw new Error('Rclone ZIP-Transport ungueltig');
  const header = tar.subarray(0, 512);
  const field = (offset, size) => header.subarray(offset, offset + size).toString('ascii').replace(/\0.*$/, '').trim();
  const sizeText = field(124, 12), checksumText = field(148, 8);
  if (field(0, 100) !== EVIDENCE_NAME || field(345, 155) !== '' ||
      !['0', '\0'].includes(header.subarray(156, 157).toString()) ||
      !/^[0-7]+$/.test(sizeText) || !/^[0-7]+$/.test(checksumText))
    throw new Error('Rclone ZIP-Transport ist keine einzelne regulaere Nachweisdatei');
  let checksum = 0;
  for (let i = 0; i < header.length; i++) checksum += i >= 148 && i < 156 ? 32 : header[i];
  const size = parseInt(sizeText, 8);
  if (parseInt(checksumText, 8) !== checksum || size !== expected.bytes || size > MAX_BYTES)
    throw new Error('Rclone ZIP-Transportpruefsumme oder Groesse stimmt nicht');
  const paddedEnd = 512 + Math.ceil(size / 512) * 512;
  if (tar.length < paddedEnd + 1024 || tar.subarray(512 + size).some(value => value !== 0))
    throw new Error('Rclone ZIP-Transport enthaelt weitere oder unvollstaendige Eintraege');
  const bytes = tar.subarray(512, 512 + size);
  verifyBytes(bytes, expected);
  return bytes;
}

async function captureEvidence(run, dockerArgs, container, report, signal) {
  const expected = metadata(report);
  const copied = await run('docker', [...dockerArgs, 'cp', `${container}:/work/repo/${EVIDENCE_PATH}`, '-'],
    { signal, binary: true, maxBytes: TAR_MAX_BYTES });
  if (copied.code !== 0) throw new Error('Rclone ZIP-Nachweis konnte nicht gelesen werden');
  const bytes = tarPayload(copied.output, expected);
  return { name: EVIDENCE_NAME, encoding: 'base64', content: bytes.toString('base64'),
    sha256: expected.sha256, bytes: expected.bytes };
}

function validateArtifact(value) {
  if (!value || Object.keys(value).sort().join(',') !== 'bytes,content,encoding,name,sha256' ||
      value.name !== EVIDENCE_NAME || value.encoding !== 'base64' || !DIGEST.test(value.sha256 || '') ||
      !Number.isSafeInteger(value.bytes) || value.bytes < 4 || value.bytes > MAX_BYTES ||
      typeof value.content !== 'string' || value.content.length !== Math.ceil(value.bytes / 3) * 4)
    throw new Error('Rclone ZIP-Veröffentlichung ungueltig');
  const bytes = Buffer.from(value.content, 'base64');
  if (bytes.toString('base64') !== value.content) throw new Error('Rclone ZIP-Base64 ist nicht kanonisch');
  verifyBytes(bytes, value);
  return value;
}

function artifactPath(job) {
  const date = typeof job?.created === 'string' ? job.created.slice(0, 10) : '';
  if (!/^[a-f0-9]{64}$/.test(job?.id || '') || !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
      !Number.isFinite(Date.parse(date))) throw new Error('Rclone ZIP-Laufidentitaet ungueltig');
  return `logs/${date}/${job.id}.zip`;
}

function artifactReference(job, artifact) {
  validateArtifact(artifact);
  artifactPath(job);
  return { name: `${job.id}.zip`, encoding: 'binary', sha256: artifact.sha256, bytes: artifact.bytes };
}

module.exports = { EVIDENCE_NAME, EVIDENCE_PATH, MAX_BYTES, TAR_MAX_BYTES, GATES, validateReport, validateFailureReport, metadata,
  tarPayload, captureEvidence, validateArtifact, artifactPath, artifactReference };
