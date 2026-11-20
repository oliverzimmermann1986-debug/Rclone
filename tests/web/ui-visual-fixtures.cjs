'use strict';

// Synthetically generated UI data only. No environment, saved config or backend
// is read. Keep these routes explicit so a new frontend dependency fails loudly.
const now = Math.floor(Date.now() / 1000);
const GiB = 1024 ** 3;
const pairs = [
  { id: '1'.repeat(32), name: 'Fotos · Familie und gemeinsame Erinnerungen', local: '/srv/ui-fixture/Fotos', remote: 'fixture-cloud:Familienarchiv/Fotos', direction: 'push', mode: 'copy', enabled: true, backup_dir: 'fixture-cloud:Versionen/Fotos/{date}', max_delete: 50 },
  { id: '2'.repeat(32), name: 'Projektunterlagen und langfristige Dokumentation', local: '/srv/ui-fixture/Projektunterlagen', remote: 'fixture-cloud:Archiv/Projektunterlagen', direction: 'push', mode: 'copy', enabled: true, backup_dir: 'fixture-cloud:Versionen/Projekte/{date}', max_delete: 25 },
  { id: '3'.repeat(32), name: 'Unterwegs · pausierter Datenweg', local: '/srv/ui-fixture/Unterwegs', remote: 'fixture-cloud:Unterwegs', direction: 'pull', mode: 'copy', enabled: false },
];
const definitions = [
  { id: 'a'.repeat(32), name: 'Archiv am frühen Morgen', enabled: true, data_path_ids: [pairs[0].id, pairs[1].id], schedule: '0 3 * * *', execution_mode: 'sequential', max_parallel: 1, retry_minutes: 30 },
  { id: 'b'.repeat(32), name: 'Manuelle Prüfung vor längeren Reisen', enabled: false, data_path_ids: [pairs[2].id], schedule: 'manual', execution_mode: 'sequential', max_parallel: 1, retry_minutes: 60 },
];
const config = {
  _revision: 'f'.repeat(64),
  web: { username: 'ui-fixture-admin', secure_cookie: true, allowed_hosts: ['127.0.0.1'], local_browse_roots: ['/srv/ui-fixture'] },
  paths: { data_dir: '/var/lib/ui-fixture', logs_dir: '/var/log/ui-fixture', temp_dir: '/var/tmp/ui-fixture', recovery_dir: '/var/lib/ui-fixture/recovery' },
  backup: { enabled: true, default_schedule: '0 3 * * *', timezone: 'Europe/Berlin', max_parallel: 2, pairs, jobs: definitions, rclone_args: [], tuning: {}, vpn_probes: [] },
  notifications: { apns: { enabled: false, events: [] } },
  maintenance: { auto_prune: true, job_retention_days: 180, keep_latest_jobs: 500, log_retention_days: 90 },
  pbs: { enabled: false, targets: [], keep: {} },
};
const runs = [
  { id: 104, kind: 'backup', definition_id: definitions[0].id, definition_name: definitions[0].name, config_revision: config._revision, status: 'ok', display_status: 'ok', started_at: now - 2400, ended_at: now - 2220, dry_run: false, summary: { ok: true, pairs: [{ name: pairs[0].name, ok: true, transferred: '1,8 GiB', checked: 12480 }] } },
  { id: 103, kind: 'restoretest', definition_name: 'Stichprobe des Familienarchivs', status: 'warning', display_status: 'warning', started_at: now - 7200, ended_at: now - 7100, summary: { ok: false, warning: 'UI-Prüfdaten: Stichprobe wegen einer begrenzten Dateiliste unvollständig', pairs: [{ name: pairs[0].name, ok: false, verified: 12, sample_size: 12 }] } },
  { id: 102, kind: 'check', definition_name: 'Verbindung nach Wartungsfenster', status: 'error', display_status: 'error', started_at: now - 14400, ended_at: now - 14350, summary: { ok: false, error: 'UI-Prüfdaten: Cloud während eines simulierten Wartungsfensters nicht erreichbar' } },
];
const scheduler = { enabled: true, paused: false, until: null, remaining_seconds: null, reason: '' };
const current = { backup: null, check: null, quicksync: null, restoretest: null, recovery: null, bitrotscrub: null, pbs: null, bootdrill: null };
const activeJob = { id: 105, kind: 'backup', definition_id: definitions[0].id, definition_name: 'Fotosicherung der Familie mit gemeinsamen Erinnerungen und langfristigem Dokumentenarchiv', status: 'running', display_status: 'running', started_at: now - 180, ended_at: null, dry_run: false };
function progressFixture(caseName = '') {
  if (!caseName.endsWith('-busy')) return { running: false, current_status: current };
  return {
    running: true, current_status: { ...current, backup: activeJob }, job_id: activeJob.id,
    elapsed_sec: 180, total_pairs: 2, done_pairs: 0, percent: 35,
    current_file: 'Familienarchiv/Erinnerungen/2026/Urlaub am Meer/Gemeinsamer Sonnenuntergang mit der ganzen Familie – Originalaufnahme.jpg',
    pairs: [
      { name: pairs[0].name, status: 'running', percent: 62, transferred: '11,2 GiB', total: '18,0 GiB', speed: '24 MiB/s', eta: '4m 50s', last_progress_at: now - 2, stall_timeout_sec: 14400, max_runtime_sec: 0 },
      { name: pairs[1].name, status: 'pending', percent: 0, transferred: '0 B', total: '14,0 GiB', speed: '—', eta: '—' },
    ],
  };
}
const overview = {
  app: { name: 'Sicherpfad', version: 'UI-Prüfung' },
  system: { hostname: 'synthetischer-pruefserver', virtualization: 'LXC · UI-Prüfdaten', kernel: 'Linux · Fixture', addresses: ['192.0.2.25'], uptime_seconds: 183600,
    cpu: { load_percent: 18, capacity: 4, load_1: 0.72, source: 'UI-Prüfdaten', percent: 18, percent_used: 18, count: 4, load: [0.72, 0.3, 0.2] },
    memory: { percent_used: 37, used_bytes: 1.48 * GiB, total_bytes: 4 * GiB, source: 'UI-Prüfdaten' },
    data_disk: { percent_used: 42, free_bytes: 58 * GiB, total_bytes: 100 * GiB }, pids: { current: 43, max: 2048 } },
  services: { web: { active: 'active', enabled: 'enabled' }, scheduler: { active: 'active', enabled: 'enabled', configured_enabled: true, control: scheduler } },
  pairs: { total: 3, enabled: 2, scheduled: 2, manual: 0, health: pairs.slice(0, 2).map((pair, index) => ({ name: pair.name, last_status: 'ok', last_run: now - 2220, last_success: now - 2220, job_id: 104, overdue: false, error: '', next_run: now + 3600, history_key: `fixture-${index}` })) },
  jobs: { last: runs[0], stats_24h: { total: 3, by_status: { ok: 1, warning: 1, error: 1, running: 0 } } },
  alerts: [{ level: 'warn', message: 'Synthetische UI-Prüfdaten: Eine Restore-Stichprobe benötigt Aufmerksamkeit. Die letzte Sicherung ist erfolgreich abgeschlossen.' }],
};
const storagePairs = pairs.map((pair, index) => ({
  ...pair, source: pair.direction === 'pull' ? pair.remote : pair.local, target: pair.direction === 'pull' ? pair.local : pair.remote, local_path: pair.local, remote_path: pair.remote,
  source_size: { count: 12480 - index * 2300, bytes: (18 - index * 4) * GiB, measured_at: now - 80, measurement_status: 'fresh', measurement_state: 'loaded' },
  target_size: { count: 12480 - index * 2300, bytes: (18 - index * 4) * GiB, measured_at: now - 80, measurement_status: 'fresh', measurement_state: 'loaded' },
  source_measurement: { state: 'loaded', measured_at: now - 80 }, target_measurement: { state: 'loaded', measured_at: now - 80 },
  last_sync: now - 2220, last_transferred: '1,8 GiB',
  restore_evidence: index === 2 ? { state: 'never', verified_files: 0, sample_size: 0 } : { state: index === 1 ? 'passed' : 'partial', valid: index === 1, last_attempt_at: now - 7100, last_success_at: now - 86400, valid_until: now + 86400, verified_files: 12, sample_size: 12, checksum_verified: index === 1, invalid_reason: index === 1 ? null : 'incomplete_sample' },
}));
const copies = {
  sources: pairs.slice(0, 2).map((pair, index) => ({ id: `ui-source-${index}`, source: pair.local, copy_count: 1, offsite_count: 1, scope_count: 1, scopes: ['UI-Prüfdaten · Cloud'], newest_age_hours: 0.6, level: 'warn', findings: ['Nur eine unabhängige Sicherungskopie vorhanden'] })),
  totals: { sources: 2, copies: 2, single_copy: 2, without_offsite: 0, without_versioning: 0 }, warnings: [], level: 'warn',
};
const doctor = { level: 'ok', checks: [
  { name: 'Webdienst', level: 'ok', ok: true, message: 'Im synthetischen Prüfszenario erreichbar' },
  { name: 'Löschschutz', level: 'ok', ok: true, message: 'Kopiermodus ohne produktive Löschfreigabe' },
], pairs: pairs.slice(0, 2).map((pair) => ({ name: pair.name, level: 'ok', checks: [], warnings: [] })) };
const bundle = {
  overview, doctor, copies, scheduler,
  logs: { logs: [{ name: 'synthetischer-lauf.log', bytes: 24320, modified_at: now - 2220, path: 'synthetischer-lauf.log' }] },
  database: { integrity: { ok: true }, stats: { jobs: 3, pair_runs: 4, bytes: 1024 * 512 } },
  snapshots: { snapshots: [], max_snapshots: 30 },
  audit: { events: [{ id: 1, event_type: 'ui_fixture_loaded', actor: 'ui-fixture', created_at: now - 60, details: { synthetic: true }, entry_hash: 'c'.repeat(64), prev_hash: '0'.repeat(64), hash_version: 1 }], chain: { ok: true, checked: 1 } },
  push: { configured: false, enabled: false, devices: 0 },
  bitrot: { enabled: false, running: false, pairs: [], stats: {} },
  ransomware: { enabled: true, pairs: [], quarantines: [] },
  vpn: { tailscale: { available: false, running: false, peers: [] }, wireguard: { interfaces: [] }, probes: [] },
};

function apiFixture(method, url, caseName = '') {
  if (method === 'POST' && url.pathname === '/api/test/rclone') {
    return { ok: true, message: 'rclone-Konfiguration geprüft.' };
  }
  if (method === 'POST' && url.pathname === '/api/config/schedule-preview') {
    return { ok: true, enabled: true, timezone: 'Europe/Berlin', next_runs: [1, 7, 13, 19].map((hour) => ({ timestamp: now + hour * 3600, iso: new Date((now + hour * 3600) * 1000).toISOString() })) };
  }
  if (method !== 'GET') return undefined;
  const progress = progressFixture(caseName);
  const listedRuns = progress.running ? [activeJob, ...runs] : runs;
  const table = {
    '/api/config': config,
    '/api/browse/rclone': { path: '', parent: null, is_root: true, entries: [
      { name: 'fixture-cloud', path: 'fixture-cloud:', is_dir: true },
      { name: 'fixture-archive', path: 'fixture-archive:', is_dir: true },
    ] },
    '/api/config/hooks': { hooks: [] },
    '/api/config/filter-file': { content: '# Ausschließlich synthetische UI-Prüfdaten\n- *.tmp\n', path: '/var/lib/ui-fixture/filter.rules', revision: 'd'.repeat(64) },
    '/api/lockdown': { active: false },
    '/api/jobs/status/current': progress.current_status,
    '/api/jobs/backup/progress': progress,
    '/api/jobs/list': listedRuns,
    '/api/jobs/search': { items: listedRuns, total: listedRuns.length, limit: 25, offset: 0 },
    '/api/jobs/definitions': definitions,
    '/api/jobs/definitions/durations': { durations: { [definitions[0].id]: { avg_duration_sec: 180 } } },
    '/api/jobs/scheduler/state': scheduler,
    '/api/jobs/bandwidth/heatmap': { days: 90, cells: Array.from({ length: 7 }, (_, day) => Array.from({ length: 24 }, (_, hour) => hour === 3 ? (day + 1) * 1024 * 1024 : null)), samples: Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 1)) },
    '/api/jobs/bandwidth/suggestions': { suggestions: [] },
    '/api/diagnostics/overview': overview,
    '/api/diagnostics/system-bundle': bundle,
    '/api/diagnostics/doctor': doctor,
    '/api/diagnostics/copies': copies,
    '/api/storage/overview': { pairs: storagePairs, measured_at: now - 80, measurement: { state: 'loaded', total: 6, loaded: 6, failed: 0, stale: 0, measured_at: now - 80, measurement_error: null } },
    '/api/storage/object-lock': { enabled: false, supported: false, remotes: [] },
    '/api/storage/quota': { remotes: [{ remote: 'fixture-cloud:', used: 32 * GiB, total: 100 * GiB, free: 68 * GiB, percent_used: 32 }], budget: { egress_bytes: 1.8 * GiB, upload_bytes: 3.2 * GiB, throttle_optional_active: false } },
    '/api/webauthn/status': { enabled: true, passkey: false, security_key: true },
  };
  if (Object.hasOwn(table, url.pathname)) return table[url.pathname];
  const detail = /^\/api\/jobs\/(\d+)$/.exec(url.pathname);
  if (detail) return listedRuns.find((run) => run.id === Number(detail[1]));
  return undefined;
}

module.exports = { apiFixture, pairs, progressFixture, runs };
