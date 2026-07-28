/* Nur für die visuelle Prüfung des Stylesheets: ersetzt app() durch eine
   Attrappe mit festen Beispieldaten, damit das echte Produktions-Markup
   ohne Backend gerendert werden kann. Wird nicht ausgeliefert. */
(() => {
  const ts = (offsetMin) => Date.now() / 1000 - offsetMin * 60;

  const pair = (name, over = {}) => ({
    name, id: name, remote: `wasabi:${name}`, local: `/srv/${name}`,
    direction: 'push', mode: 'copy', enabled: true, schedule: '0 2 * * *',
    two_way: false, min_local_files: 1, min_remote_files: 0, min_free_gb: 0,
    max_success_age_hours: 48, allow_delete: false, max_delete: 100,
    require_mountpoint: false, mountpoint: '', sentinel_file: '',
    exclude: '- .DS_Store', include: '', filter: '', rclone_args: '',
    transfers: '', checkers: '', allow_empty_remote_target: false,
    backup_dir: '', backup_dir1: '', backup_dir2: '',
    ...over,
  });

  const OVERVIEW = {
    app: { version: '2.2.0', timezone: 'Europe/Berlin' },
    system: {
      hostname: 'pve-backup01', virtualization: 'LXC', kernel: '6.8.12-4-pve',
      uptime_seconds: 3550320, addresses: ['10.20.0.14'],
      cpu: { load_percent: 34, count: 4, capacity: 4, load_1: 1.42, source: 'cgroup v2' },
      memory: { percent_used: 61, used_bytes: 5261334937, total_bytes: 8589934592, source: 'proc' },
      data_disk: { percent_used: 88, free_bytes: 229734252544 },
      pids: { current: 142, max: 4096, percent_used: 3 },
    },
    services: {
      web: { enabled: 'enabled', active: 'active' },
      scheduler: { enabled: 'enabled', active: 'active', configured_enabled: true,
                   control: { paused: false, until: null, reason: '', actor: 'system' } },
    },
    pairs: {
      total: 6, enabled: 6, scheduled: 4, manual: 2, destructive: 2,
      health: [
        { name: 'mediathek', direction: 'push', last_status: 'running', last_run: ts(3), next_run: ts(-680), job_id: 1284, last_success: ts(1440), overdue: false },
        { name: 'vm-images', direction: 'push', last_status: 'ok', last_run: ts(8), next_run: ts(-680), job_id: 1283, last_success: ts(8), overdue: false },
        { name: 'archiv-langzeit', direction: 'push', last_status: 'error', last_run: ts(4320), next_run: ts(-680), job_id: 1281, last_success: null, overdue: true, error: 'hetzner-box: 502 Bad Gateway beim Verzeichnislisting' },
      ],
    },
    jobs: {
      last: { id: 1284, kind: 'backup', status: 'running', started_at: ts(5), summary: { pairs: [] } },
      last_success: { id: 1283, kind: 'backup', status: 'ok', started_at: ts(620) },
      last_error: { id: 1281, kind: 'backup', status: 'error', started_at: ts(740) },
      stats_24h: { total: 10, by_status: { ok: 7, error: 2, running: 1 } },
    },
    alerts: [
      { level: 'warn', message: 'Pair „archiv-langzeit" seit 3 Tagen ohne frischen erfolgreichen Lauf' },
      { level: 'error', message: 'Datenträger für Anwendungsdaten ist fast voll' },
      { level: 'info', message: 'Mindestens ein Mirror-Pair ist noch nicht für Löschungen freigegeben' },
    ],
    generated_at: Date.now() / 1000,
  };

  const JOBS = [
    { id: 1284, kind: 'backup', status: 'running', started_at: ts(5), ended_at: null, summary: { pairs: [{ name: 'mediathek' }] } },
    { id: 1283, kind: 'backup', status: 'ok', started_at: ts(620), ended_at: ts(607), summary: { transferred: '78,1 GB' } },
    { id: 1282, kind: 'pbs', status: 'ok', started_at: ts(680), ended_at: ts(672), summary: { targets: 1 } },
    { id: 1281, kind: 'backup', status: 'error', started_at: ts(740), ended_at: ts(739), summary: { error: '502 Bad Gateway' } },
    { id: 1280, kind: 'restoretest', status: 'ok', started_at: ts(800), ended_at: ts(795), summary: { verified_files: 20 } },
    { id: 1279, kind: 'check', status: 'skipped', started_at: ts(1500), ended_at: ts(1500), summary: {} },
  ];

  window.app = function app() {
    return {
      // Seite über den Anker wählbar: _preview.html#pairs
      page: (location.hash.replace('#', '') || 'dashboard'),
      navOpen: false, density: 'comfortable', theme: 'system',
      connectionState: 'online', connectionMessage: 'Live-Verbindung aktiv', refreshing: false,
      overview: { loading: false, data: OVERVIEW },
      jobs: { items: JOBS, total: 1284, offset: 0, limit: 25, loading: false, error: '', q: '', kind: '', status: '' },
      recentJobs: JOBS.slice(0, 5),
      doctor: { loading: false, data: null },
      copies: {
        loading: false,
        data: {
          totals: { sources: 4, single_copy: 2, without_offsite: 1, without_versioning: 3 },
          sources: [
            { id: 'a', source: '/srv/archiv', copy_count: 1, scope_count: 1, scopes: ['hetzner:'], offsite_count: 1, newest_age_hours: null, level: 'error', findings: ['nur eine Kopie', 'keine Versionsablage'] },
            { id: 'b', source: '/srv/fibu', copy_count: 2, scope_count: 2, scopes: ['wasabi:', '/mnt/nas1'], offsite_count: 1, newest_age_hours: 1.2, level: 'ok', findings: [] },
            { id: 'c', source: '/srv/media', copy_count: 1, scope_count: 1, scopes: ['wasabi:'], offsite_count: 1, newest_age_hours: 0.4, level: 'error', findings: ['nur eine Kopie'] },
          ],
        },
      },
      maintenance: { logs: [], loading: false, prune: null, database: { stats: { jobs: 1284, pair_runs: 5120, bytes: 19293798 }, integrity: { ok: true } }, logQuery: '' },
      audit: { items: [
        { id: 3, event_type: 'backup_requested', actor: 'web', created_at: ts(5), details: { dry_run: true } },
        { id: 2, event_type: 'scheduler_paused', actor: 'admin', created_at: ts(300), details: { reason: 'Wartung' } },
        { id: 1, event_type: 'config_saved', actor: 'admin', created_at: ts(900), details: {} },
      ], loading: false },
      snapshots: { items: [], loading: false, max: 10, restoreName: '', password: '' },
      pbs: { loading: false, status: { running: false, client_available: true, targets: [
        { name: 'tank/vm', paths: ['/var/lib/vz/dump'], schedule: '0 3 * * *', last_success: ts(680) },
      ] } },
      progress: { running: true, elapsed_sec: 323, total_pairs: 4, done_pairs: 2, pairs: [
        { name: 'vm-images', status: 'done', percent: 100, transferred: '62,4 GB', total: '62,4 GB', speed: '—', eta: '—' },
        { name: 'mediathek', status: 'running', percent: 44, transferred: '41,8 GB', total: '94,6 GB', speed: '118 MB/s', eta: '07:28' },
        { name: 'nextcloud-data', status: 'pending', percent: null, transferred: null, total: null, speed: null, eta: null },
      ] },
      config: {
        backup: { enabled: true, pairs: [
          pair('mediathek'), pair('vm-images'), pair('dokumente-fibu'),
          pair('archiv-langzeit', { max_success_age_hours: 24 }),
          pair('nextcloud-data'),
          pair('projekte-bisync', { direction: 'bisync', mode: 'bisync', two_way: true, schedule: 'manual' }),
        ], default_schedule: '0 3 * * *', timezone: 'Europe/Berlin', transfers: 4, checkers: 8,
           overdue_alerts: { enabled: true, repeat_hours: 24 },
           restore_test: { enabled: false, schedule: 'manual', sample_files: 20, max_total_mb: 256, max_scan_files: 20000 },
           tuning: { transfers: 4, checkers: 8, retries: 3, low_level_retries: 10, max_delete: 100,
                     fast_list: false, bwlimit: '', timeout_hours: 4, multi_thread_streams: 4,
                     buffer_size: '16M', order_by: '', exclude: '', include: '' },
           filters: { exclude: '', include: '' },
           bwlimit: '', timeout_hours: 4, conflict_resolve: 'auto', immutable: false,
           backup_dir: '', backup_dir1: '', backup_dir2: '',
           scheduler_retry_minutes: 60, scheduler_grace_minutes: 15, run_on_first_tick: false },
        pbs: {
          enabled: true, targets: [], repository: '', namespace: '', backup_id: '',
          keep: { keep_daily: 7, keep_weekly: 4, keep_monthly: 6, keep_yearly: 1 },
        },
        notifications: { webhooks: [{ id: 'hook1', enabled: true, type: 'discord', url: '', events: ['sync_error'] }] },
        maintenance: { log_retention_days: 90, job_retention_days: 180, enabled: true },
        web: {
          username: 'admin', allowed_hosts: ['*'], local_browse_roots: ['/srv', '/mnt'],
          hidden_remote_paths: [], secure_cookie: 'auto', hsts_seconds: 0,
          session_max_age_seconds: 604800, login_max_failures: 10,
          login_window_seconds: 300, login_lock_seconds: 900,
        },
        paths: { data_dir: '/opt/rclone-sync/data', logs_dir: '/opt/rclone-sync/logs', temp_dir: '/opt/rclone-sync/temp' },
      },
      configLoaded: true, configLoading: false, configError: '', configDirty: false,
      configValidation: { loading: false, ok: true, errors: [], warnings: ['projekte-bisync: Bi-Sync ohne backup_dir'] },
      pending: {}, pairOpen: { 0: true }, pairSearch: '', pairFilter: 'all', newPairPreset: 'push-copy',
      selectedPairs: [], storageLoading: false, pairSizes: {}, pairSelection: {},
      settingsTab: 'general', settingsTabs: ['general', 'scheduler', 'security', 'notifications', 'filters', 'account', 'pbs'],
      scheduleEditor: { mode: 'daily' }, schedulePreview: { loading: false, valid: true, next: [] },
      schedulerControl: { enabled: true, paused: false, reason: '', until: null },
      performancePreset: 'balanced',
      filterFile: { content: '', path: '', revision: '', loading: false, dirty: false },
      rcloneArgsText: '', globalFilterText: '',
      toast: null, jobModal: { open: false, job: null }, picker: { open: false },
      testResults: {}, planData: null, quick: {}, pwChange: {},

      init() {}, navigate(p) { this.page = p; this.navOpen = false; },
      pageTitle() { return ({ dashboard: 'Übersicht', pairs: 'Sync-Paare', jobs: 'Jobhistorie', doctor: 'System & Diagnose', settings: 'Einstellungen' })[this.page]; },
      systemLevel() { return 'ok'; }, systemLabel() { return 'Betrieb normal'; },
      connectionLabel() { return 'Live'; }, themeLabel() { return 'System'; },
      busy() { return true; }, rcloneBusy() { return true; }, runningKind() { return 'Backup'; },
      runningJob() { return JOBS[0]; },
      statusLabel(s) { return ({ ok: 'Erfolg', error: 'Fehler', running: 'Läuft', skipped: 'Übersprungen', stale: 'Verwaist', pending: 'Wartet', cancelled: 'Abgebrochen' })[s] || s || '—'; },
      kindLabel(k) { return ({ backup: 'Backup', check: 'Check', quicksync: 'Quick-Sync', pbs: 'PBS-Backup', restoretest: 'Restore-Drill' })[k] || k; },
      copyLevelLabel(l) { return ({ ok: 'Ausreichend', warn: 'Prüfen', error: 'Kritisch' })[l] || l; },
      formatTs(v) { return v ? new Date(v * 1000).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'; },
      formatDateTime(v) { return this.formatTs(v); },
      formatDur(v) { return v ? `${Math.floor(v / 60)}:${String(Math.round(v % 60)).padStart(2, '0')}` : '—'; },
      formatBytes(v) { return v ? `${(v / 1e9).toFixed(1)} GB` : '—'; },
      formatUptime(v) { return v ? `${Math.floor(v / 86400)} T` : '—'; },
      summaryShort(s) { return typeof s === 'string' ? s : JSON.stringify(s || {}).slice(0, 90); },
      auditLabel(t) { return t; },
      visiblePairCount() { return this.config.backup.pairs.length; },
      filteredPairs() { return this.config.backup.pairs; },
      pairVisible() { return true; },
      pairIssues(p) { return p.mode === 'bisync' ? ['Bi-Sync ohne Versionsablage'] : []; },
      pairRuntimeIssue(p) { return p.name === 'archiv-langzeit' ? '502 Bad Gateway' : ''; },
      pairLastRun() { return { last_run: ts(620), next_run: ts(-680) }; },
      pairSize() { return null; }, pairSizeText() { return '—'; },
      pairStatus() { return 'ok'; }, pairRuntimeIssueLevel() { return 'error'; },
      directionLabel(p) { return `${p.local} → ${p.remote}`; },
      schedulerControlLabel() { return 'Scheduler aktiv'; },
      schedulerRiskLevel() { return 'ok'; },
      jobPage() { return 1; }, jobPages() { return 52; },
      doctorCounts() { return { ok: 12, warn: 2, error: 1 }; },
      backupDirMissing(p) { return p.direction === 'bisync' || p.mode === 'sync'; },
      suggestedBackupDir(v) { return v ? `${v}-versions/{date}` : ''; },
      shortcutHint() { return ''; },
    };
  };

  // Jede im Template aufgerufene, hier nicht definierte Methode wird zu einem
  // Platzhalter. Kein Proxy: der würde Alpines Magics ($el, $refs, $watch)
  // abfangen und die Initialisierung abbrechen.
  const TEMPLATE_METHODS = window.__STUB_METHODS__ || [];
  const base = window.app;
  window.app = function () {
    const state = base();
    for (const name of TEMPLATE_METHODS) {
      if (!(name in state)) state[name] = () => undefined;
    }
    return state;
  };
})();
