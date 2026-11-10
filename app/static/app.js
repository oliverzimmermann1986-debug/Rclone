// Einstiegsmodul: baut die Alpine-Komponente app() aus den Feature-Modulen zusammen.
// window.app muss vor Alpine.start() stehen (Ladereihenfolge in index.html: app.js → workspace.js → alpine.min.js).
import { coreMethods } from './app-core.js';
import { dashboardMethods } from './app-dashboard.js';
import { pairsMethods, emptyCrypt } from './app-pairs.js';
import { jobsMethods, emptyQuick } from './app-jobs.js';
import { systemMethods } from './app-system.js';
import { recoveryMethods } from './app-recovery.js';
import {
  settingsMethods, lifecycleDefaults, scrubDefaults, gcThrottleDefaults, bootDrillDefaults,
  ransomwareDefaults, transferBudgetDefaults, bootstrapExportDefaults,
} from './app-settings.js';

const featureMethods = [
  coreMethods, dashboardMethods, pairsMethods, jobsMethods, systemMethods, recoveryMethods, settingsMethods,
];

window.app = function app() {
  const component = {
    page: 'dashboard',
    remoteSetup: {
      show: false, busy: false, name: '', provider: '', providers: [],
      express: true, driveMode: 'oauth', scope: 'drive', rootFolderId: '',
      clientId: '', clientSecret: '', teamDrive: false,
      serviceAccountJson: '', serviceAccountResult: null,
      session: null, answer: '', error: '',
    },
    pages: ['dashboard', 'pairs', 'definitions', 'runs', 'doctor', 'settings', 'recovery'],
    settingsTabs: ['general', 'scheduler', 'security', 'filters', 'account', 'pbs'],
    navOpen: false,
    liveMessage: '',
    online: navigator.onLine,
    connectionState: navigator.onLine ? 'checking' : 'offline',
    connectionMessage: navigator.onLine ? 'Verbindung wird geprüft' : 'Netzwerkverbindung ist offline',
    theme: 'system',
    density: 'comfortable',
    lastUpdated: null,
    requestTimeoutMs: 30000,
    refreshing: false,
    polling: { active: false, generation: 0, refreshTimer: null, activityTimer: null },
    configLoading: true,
    configLoaded: false,
    configError: '',
    pending: {
      backup: false, plan: false, quick: false, pbs: false, pbsCancel: false,
      save: false, validate: false, filter: false, password: false, definition: false, retry: false,
      bootDrill: false, bitrot: false,
    },
    lockdownState: { active: false },
    lockdownDialog: { show: false, reason: '', password: '', error: '', submitting: false },
    lockdownSteps: [],
    currentPasswordDialog: { show: false, password: '', error: '' },
    confirmationDialog: {
      show: false,
      title: 'Aktion bestätigen',
      message: '',
      confirmLabel: 'Bestätigen',
      tone: 'danger',
    },

    overview: { loading: false, data: null },
    status: { backup: null, check: null, quicksync: null, restoretest: null, pbs: null, bitrotscrub: null, bootdrill: null },
    progress: null,
    sse: null,
    storageLoading: false,
    storageState: 'loading',
    storageError: '',
    storageMeasurement: { state: 'loading', total: 0, loaded: 0, failed: 0, stale: 0 },
    recentJobs: [],
    jobs: {
      loading: false, items: [], total: 0, offset: 0, limit: 25,
      kind: '', status: '', q: '', error: '',
    },

    config: {
      web: {}, paths: {},
      backup: {
        pairs: [], jobs: [], rclone_args: [], tuning: {},
        timeout_hours: 4, max_runtime_hours: 0,
        versions_lifecycle: { ...lifecycleDefaults }, bitrot_scrub: { ...scrubDefaults },
        ransomware_guard: { ...ransomwareDefaults }, transfer_budget: { ...transferBudgetDefaults },
      },
      notifications: {},
      maintenance: {
        auto_prune: true, job_retention_days: 180, keep_latest_jobs: 500, log_retention_days: 90,
        bootstrap_export: { ...bootstrapExportDefaults },
      },
      pbs: {
        enabled: false, repository: '', password: '', fingerprint: '',
        namespace: '', backup_id: '', timeout_hours: 4,
        keep: { keep_last: 0, keep_daily: 7, keep_weekly: 4, keep_monthly: 6, keep_yearly: 0 },
        gc_throttle: { ...gcThrottleDefaults }, boot_drill: { ...bootDrillDefaults },
        targets: [],
      },
    },
    rcloneArgsText: '',
    allowedHostsText: '',
    vpnProbesText: '',
    browseRootsText: '',
    configDirty: false,
    configEditGeneration: 0,
    savedConfigSnapshot: null,
    configDiffOpen: false,
    filterTester: { pathsText: '', results: [], parseErrors: [], loading: false },
    hooksAvailable: [],
    pairSearch: '',
    pairFilter: 'all',
    newPairPreset: 'push-copy',
    pairWizard: {
      open: false, step: 1, preset: '', name: '', source: '', target: '',
      twoWay: false, mode: 'copy', allowDelete: false, maxDelete: 100, backupDir: '',
      requireMountpoint: false,
    },
    pairOpen: {},
    pairSelection: {},
    jobPathChoice: {},
    jobDefinitions: { loading: false, error: '' },
    jobTimeline: { loading: false, rows: [], conflicts: [] },
    settingsTab: 'general',
    testResults: {},
    configValidation: { loading: false, ok: null, warnings: [], errors: [], revisionMatches: true },
    scheduleEditor: { mode: 'daily', time: '03:00', intervalHours: 6, intervalMinutes: 30, weekday: '0' },
    schedulePreview: { loading: false, valid: null, error: '', nextRuns: [], timer: null },
    performancePreset: 'balanced',

    plan: { loading: false, data: null, dry_run: true, definitionId: null },
    doctor: { loading: false, data: null },
    copies: { loading: false, data: null },
    recovery: {
      loading: false, pair: '', points: [], pointId: '', path: '', items: [],
      selectedPaths: [], diff: null, staging: [], quarantine: [], error: '',
      maxTotalMb: 512, timeTravel: false,
      searchQuery: '', searchResults: [], searchLoading: false,
      handover: { password: '', passphrase: '', repeat: '', includePaths: false, downloaded: false },
    },
    storageComposition: { loading: {}, data: {}, errors: {} },
    maintenance: { logs: [], loading: false, prune: null, database: null, logQuery: '' },
    filterFile: { content: '', path: '', revision: '', loading: false, dirty: false },
    pwChange: { current: '', new: '', confirm: '' },
    snapshots: { loading: false, items: [], max: 30, restoreName: '', password: '', diff: { loading: false, data: null, error: '' } },
    pbs: { loading: false, status: null },
    pbsInsights: null,
    bootDrill: null,
    bitrot: { loading: false, error: false, data: null, baselines: {} },
    cloudQuota: { loading: false, error: false, data: null },
    ransomwareStatus: { loading: false, error: false, data: null },
    vpn: { loading: false, error: false, data: null },
    activeTag: '',
    conflictCounts: {},
    conflictDialog: { show: false, loading: false, pair: '', data: null, error: '', stepErrors: [], busy: false },
    versionsState: {},
    bandwidth: { loading: false, error: false, days: 90, heatmap: null, suggestions: null, max: 0 },
    preflight: { show: false, loading: false, kind: '', jobId: null, data: null, excluded: {}, error: '' },
    stagingDiffPaths: {},
    diffDialog: { show: false, loading: false, data: null, error: '', path: '' },
    crypt: emptyCrypt(),
    objectLock: null,
    push:{ loading: false, testing: false, status: null, error: '' },
    schedulerControl: { loading: false, paused: false, until: null, remaining_seconds: null, reason: '', enabled: true },
    schedulerPause: { minutes: 60, reason: 'Wartungsfenster' },
    audit: { loading: false, items: [], eventType: '', chain: null },
    bootstrapExport: { passphrase: '', repeat: '', includeSecrets: false, busy: false, verifyResult: null },

    quickModal: { show: false },
    quick: emptyQuick(),
    picker: {
      show: false, mode: null, idx: null, current: '', parent: null,
      kind: 'local', paths: { local: '', remote: '' }, isRoot: true,
      entries: [], loading: false, creating: false, newFolderName: '', search: '', error: '',
    },
    jobModal: {
      show: false, job: null, log: '', loading: false, logLoading: false,
      logSearch: '', logFilter: 'all', autoRefresh: true,
    },
    quickPromotion: null,
    commandPalette: { show: false, query: '', selectedIndex: 0 },
    toast: { show: false, msg: '', type: 'ok', timer: null },
  };
  for (const methods of featureMethods) {
    for (const name of Object.keys(methods)) {
      // Entwicklungsschutz: ein doppelter Name würde State oder eine andere Methode still überschreiben.
      if (name in component) console.error(`app(): Schlüssel "${name}" ist mehrfach definiert`);
    }
    Object.assign(component, methods);
  }
  return component;
};

// PWA: Service Worker best-effort registrieren (nur Secure Context / localhost).
// Fehler werden bewusst verschluckt – die App funktioniert ohne SW unverändert.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  });
}
