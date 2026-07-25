function app() {
  const emptyQuick = () => ({
    remote: '', local: '', direction: 'bisync', mode: 'bisync', dry_run: true,
    allow_delete: false, max_delete: 100, new_target: false,
  });
  const requestControllers = new Map();
  const requestRevisions = new Map();
  const dialogFocusStack = [];
  const staleResponse = Object.freeze({ __stale: true });
  let currentPasswordResolver = null;
  const ui = window.RcloneUI;
  const safeStoredValue = ui.storedChoice;

  return {
    page: 'dashboard',
    pages: ['dashboard', 'pairs', 'jobs', 'doctor', 'settings'],
    settingsTabs: ['general', 'scheduler', 'security', 'notifications', 'filters', 'account', 'pbs'],
    navOpen: false,
    online: navigator.onLine,
    connectionState: navigator.onLine ? 'checking' : 'offline',
    connectionMessage: navigator.onLine ? 'Verbindung wird geprüft' : 'Netzwerkverbindung ist offline',
    theme: 'system',
    density: 'comfortable',
    lastUpdated: null,
    requestTimeoutMs: 30000,
    refreshing: false,
    polling: { active: false, refreshTimer: null, activityTimer: null },
    configLoading: true,
    configLoaded: false,
    configError: '',
    pending: {
      backup: false, plan: false, quick: false, pbs: false, pbsCancel: false,
      save: false, validate: false, filter: false, password: false,
    },
    currentPasswordDialog: { show: false, password: '', error: '' },

    overview: { loading: false, data: null },
    status: { backup: null, check: null, quicksync: null, pbs: null },
    progress: null,
    sse: null,
    recentJobs: [],
    jobs: {
      loading: false, items: [], total: 0, offset: 0, limit: 25,
      kind: '', status: '', q: '', error: '',
    },

    config: {
      web: {}, paths: {},
      backup: { pairs: [], rclone_args: [], tuning: {} },
      notifications: { webhooks: [] },
      maintenance: {},
      pbs: {
        enabled: false, repository: '', password: '', fingerprint: '',
        namespace: '', backup_id: '', timeout_hours: 4,
        keep: { keep_last: 0, keep_daily: 7, keep_weekly: 4, keep_monthly: 6, keep_yearly: 0 },
        targets: [],
      },
    },
    rcloneArgsText: '',
    allowedHostsText: '',
    browseRootsText: '',
    configDirty: false,
    pairSearch: '',
    pairFilter: 'all',
    newPairPreset: 'push-copy',
    pairOpen: {},
    settingsTab: 'general',
    testResults: {},
    configValidation: { loading: false, ok: null, warnings: [], errors: [], revisionMatches: true },
    scheduleEditor: { mode: 'daily', time: '03:00', intervalHours: 6, intervalMinutes: 30, weekday: '0' },
    schedulePreview: { loading: false, valid: null, error: '', nextRuns: [], timer: null },
    performancePreset: 'balanced',

    plan: { loading: false, data: null, dry_run: true },
    doctor: { loading: false, data: null },
    maintenance: { logs: [], loading: false, prune: null, database: null, logQuery: '' },
    filterFile: { content: '', path: '', revision: '', loading: false, dirty: false },
    pwChange: { current: '', new: '', confirm: '' },
    snapshots: { loading: false, items: [], max: 30, restoreName: '', password: '' },
    pbs: { loading: false, status: null },
    schedulerControl: { loading: false, paused: false, until: null, remaining_seconds: null, reason: '', enabled: true },
    schedulerPause: { minutes: 60, reason: 'Wartungsfenster' },
    audit: { loading: false, items: [], eventType: '' },

    quickModal: { show: false },
    quick: emptyQuick(),
    picker: {
      show: false, mode: null, idx: null, current: '', parent: null,
      entries: [], loading: false, search: '', error: '',
    },
    jobModal: {
      show: false, job: null, log: '', loading: false, logLoading: false,
      logSearch: '', autoRefresh: true,
    },
    toast: { show: false, msg: '', type: 'ok', timer: null },

    async init() {
      this.theme = safeStoredValue('rclone-sync-theme', ['system', 'dark', 'light'], 'system');
      this.density = safeStoredValue('rclone-sync-density', ['comfortable', 'compact'], ui.prefersCompact() ? 'compact' : 'comfortable');
      this.settingsTab = safeStoredValue('rclone-sync-settings-tab', this.settingsTabs, 'general');
      this.pairFilter = safeStoredValue(
        'rclone-sync-pair-filter',
        ['all', 'enabled', 'disabled', 'issues', 'destructive'],
        'all',
      );
      this.jobs.kind = safeStoredValue('rclone-sync-job-kind', ['', 'backup', 'check', 'quicksync', 'pbs'], '');
      this.jobs.status = safeStoredValue(
        'rclone-sync-job-status',
        ['', 'running', 'ok', 'error', 'skipped', 'cancelled', 'stale'],
        '',
      );
      this.applyTheme();
      this.applyDensity();
      const store = ui.store;
      this.$watch('density', (value) => { store('rclone-sync-density', value); this.applyDensity(); });
      this.$watch('settingsTab', (value) => store('rclone-sync-settings-tab', value));
      this.$watch('pairFilter', (value) => store('rclone-sync-pair-filter', value));
      this.$watch('jobs.kind', (value) => store('rclone-sync-job-kind', value));
      this.$watch('jobs.status', (value) => store('rclone-sync-job-status', value));
      window.addEventListener('keydown', (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
          event.preventDefault();
          if (this.settingsTab === 'filters' && this.filterFile.dirty) this.saveFilterFile();
          else if (this.configDirty) this.saveConfig();
        }
        if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
          event.preventDefault();
          const target = this.page === 'jobs' ? document.getElementById('job-search') : document.getElementById('pair-search');
          target?.focus();
        }
      });
      const hashPage = window.location.hash.replace('#', '');
      this.page = this.pages.includes(hashPage) ? hashPage : 'dashboard';
      const applyHistoryPage = () => {
        const next = window.location.hash.replace('#', '');
        if (this.pages.includes(next) && next !== this.page) this.navigate(next, false);
      };
      window.addEventListener('hashchange', applyHistoryPage);
      window.addEventListener('popstate', applyHistoryPage);
      window.addEventListener('online', () => {
        this.setConnectionState('checking', 'Verbindung wird erneut geprüft');
        this.refreshAll(true);
      });
      window.addEventListener('offline', () => {
        this.setConnectionState('offline', 'Netzwerkverbindung ist offline');
      });
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) this.stopPolling();
        else {
          this.startPolling();
          this.refreshAll(false);
        }
      });
      window.addEventListener('beforeunload', (event) => {
        if (this.configDirty || this.filterFile.dirty) {
          event.preventDefault();
          event.returnValue = '';
        }
      });
      await this.loadConfig(true);
      await this.refreshAll(false);
      if (this.page === 'dashboard') await this.loadStorage(false);
      else this.loadPage(this.page, true);
      this.startPolling();
    },

    async refreshAll(showToast = false) {
      if (this.refreshing) return false;
      this.refreshing = true;
      try {
        const tasks = [this.refreshStatus(true)];
        // Die teure Übersicht ist nur auf dem Dashboard sichtbar. Andere
        // Bereiche aktualisieren ausschließlich die dort benötigten Daten.
        if (this.page === 'dashboard') {
          tasks.push(this.loadOverview(true));
          tasks.push(this.loadRecent(true), this.loadSchedulerState(true));
          if (this.config.pbs?.enabled) tasks.push(this.loadPbsStatus(true));
        } else if (this.page === 'doctor' || this.page === 'settings') {
          tasks.push(this.loadSchedulerState(true));
        }
        const results = await Promise.all(tasks);
        if (this.rcloneBusy() || this.progress?.running) await this.loadProgress(true);
        this.lastUpdated = Date.now();
        const failed = results.filter((result) => result === false).length;
        if (failed) {
          this.setConnectionState('degraded', `${failed} Bereich(e) konnten nicht aktualisiert werden`);
          if (showToast) this.showToast('Ansicht nur teilweise aktualisiert', 'err');
        } else if (results.some(Boolean)) {
          this.setConnectionState('online', 'Mit dem Server verbunden');
          if (showToast) this.showToast('Ansicht aktualisiert');
        }
        return failed === 0;
      } finally {
        this.refreshing = false;
      }
    },

    startSseProgress() {
      if (!window.EventSource || this.sse) return;
      try {
        this.sse = new EventSource('/api/jobs/progress/stream');
        this.sse.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data && !data.error) this.progress = data;
          } catch (_) { /* ignore malformed frame */ }
        };
        this.sse.onerror = () => {
          // Verbindung abbrechen; Polling bleibt aktiver Fallback.
          this.stopSseProgress();
        };
      } catch (_) {
        this.sse = null;
      }
    },

    stopSseProgress() {
      if (this.sse) {
        try { this.sse.close(); } catch (_) { /* ignore */ }
        this.sse = null;
      }
    },

    stopPolling() {
      this.polling.active = false;
      if (this.polling.refreshTimer) window.clearTimeout(this.polling.refreshTimer);
      if (this.polling.activityTimer) window.clearTimeout(this.polling.activityTimer);
      this.polling.refreshTimer = null;
      this.polling.activityTimer = null;
      this.stopSseProgress();
    },

    startPolling() {
      if (this.polling.active || document.hidden) return;
      this.polling.active = true;
      this.startSseProgress();
      const scheduleRefresh = () => {
        if (!this.polling.active) return;
        this.polling.refreshTimer = window.setTimeout(refreshLoop, 30000);
      };
      const scheduleActivity = () => {
        if (!this.polling.active) return;
        const busy = this.rcloneBusy() || this.progress?.running
          || this.pbs.status?.running
          || (this.jobModal.show && this.jobModal.autoRefresh && this.jobModal.job?.status === 'running');
        this.polling.activityTimer = window.setTimeout(activityLoop, busy ? 2000 : 10000);
      };
      const refreshLoop = async () => {
        try {
          if (this.polling.active && !document.hidden) await this.refreshAll(false);
        } finally {
          scheduleRefresh();
        }
      };
      const activityLoop = async () => {
        try {
          if (this.polling.active && !document.hidden) {
            if (this.rcloneBusy() || this.progress?.running) await this.loadProgress(true);
            if (this.status?.pbs || this.pbs.status?.running) await this.loadPbsStatus(true);
            if (this.jobModal.show && this.jobModal.autoRefresh && this.jobModal.job?.status === 'running') {
              await this.refreshJobModal();
            }
          }
        } finally {
          scheduleActivity();
        }
      };
      scheduleRefresh();
      scheduleActivity();
    },

    navigate(next, updateHash = true) {
      if (!this.pages.includes(next)) return;
      if ((this.configDirty || this.filterFile.dirty) && ['pairs', 'settings'].includes(this.page) && next !== this.page) {
        if (!confirm('Es gibt ungespeicherte Änderungen. Seite trotzdem wechseln?')) return;
      }
      this.page = next;
      this.navOpen = false;
      if (updateHash && window.location.hash !== `#${next}`) history.pushState(null, '', `#${next}`);
      this.loadPage(next);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    },

    loadPage(page, configAlreadyLoaded = false) {
      if (page === 'dashboard') {
        this.loadOverview(true); this.loadRecent(true); this.loadStorage(false);
        if (!configAlreadyLoaded && !this.configLoaded) this.loadConfig(true);
        if (this.config?.pbs?.enabled) this.loadPbsStatus();
      } else if (page === 'pairs') {
        if (!configAlreadyLoaded) this.loadConfig();
      } else if (page === 'jobs') {
        this.loadJobs(true);
      } else if (page === 'doctor') {
        this.loadOverview(true); if (!configAlreadyLoaded) this.loadConfig(); this.loadDoctor(); this.loadLogs(); this.loadDatabaseStatus(); this.loadSnapshots(); this.loadAudit(); this.loadSchedulerState(true);
      } else if (page === 'settings') {
        if (!configAlreadyLoaded) this.loadConfig(); this.loadFilterFile(); this.loadSchedulerState(true);
      }
    },

    pageTitle() {
      return ({ dashboard: 'Übersicht', pairs: 'Sync-Paare', jobs: 'Jobhistorie', doctor: 'System & Diagnose', settings: 'Einstellungen' })[this.page] || 'rclone-sync';
    },

    cookie(name) {
      const prefix = encodeURIComponent(name) + '=';
      for (const part of document.cookie.split(';')) {
        const value = part.trim();
        if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
      }
      return '';
    },

    setConnectionState(state, message = '') {
      this.connectionState = state;
      this.connectionMessage = message || ({
        online: 'Mit dem Server verbunden',
        checking: 'Verbindung wird geprüft',
        degraded: 'Server nur teilweise erreichbar',
        offline: 'Keine Verbindung zum Server',
      })[state] || '';
      this.online = state === 'online';
    },

    connectionLabel() {
      return ({
        online: 'Verbunden',
        checking: 'Prüfe',
        degraded: 'Eingeschränkt',
        offline: 'Offline',
      })[this.connectionState] || 'Unbekannt';
    },

    isStale(result) {
      return Boolean(result?.__stale);
    },

    async api(method, url, body, options = {}) {
      const requestKey = options.requestKey || '';
      let revision = 0;
      if (requestKey) {
        requestControllers.get(requestKey)?.abort();
        revision = (requestRevisions.get(requestKey) || 0) + 1;
        requestRevisions.set(requestKey, revision);
      }
      const controller = new AbortController();
      if (requestKey) requestControllers.set(requestKey, controller);
      let timedOut = false;
      try {
        const upper = String(method || 'GET').toUpperCase();
        const opts = { method: upper, credentials: 'include', headers: {} };
        if (!['GET', 'HEAD', 'OPTIONS'].includes(upper)) {
          opts.headers['X-CSRF-Token'] = this.cookie('rclone_sync_csrf');
        }
        if (body !== undefined) {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = JSON.stringify(body);
        }
        opts.signal = controller.signal;
        const timeout = setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, options.timeoutMs || this.requestTimeoutMs);
        let response;
        try { response = await fetch(url, opts); } finally { clearTimeout(timeout); }
        if (requestKey && requestRevisions.get(requestKey) !== revision) return staleResponse;
        if (!response.ok) {
          if (response.status === 401) { window.location = '/login'; return null; }
          const err = await response.json().catch(() => ({}));
          if (requestKey && requestRevisions.get(requestKey) !== revision) return staleResponse;
          const rawDetail = err.detail || response.statusText;
          let detail = rawDetail;
          if (typeof detail === 'object') {
            const parts = [detail.message, ...(detail.errors || [])].filter(Boolean);
            detail = parts.join(' · ') || JSON.stringify(detail);
          }
          if (response.status >= 500) {
            this.setConnectionState('degraded', `Serverfehler ${response.status}: ${detail}`);
          } else {
            this.setConnectionState('online', 'Server erreichbar');
          }
          if (options.captureError) return { __error: true, status: response.status, detail: rawDetail };
          if (!options.silent) {
            if (response.status === 409 && (url === '/api/config' || url === '/api/config/filter-file')) {
              this.showToast('Parallel geändert: Bitte neu laden und Änderungen erneut prüfen.', 'err');
            } else {
              this.showToast(`Fehler: ${detail}`, 'err');
            }
          }
          return null;
        }
        const result = response.headers.get('content-type')?.includes('json') ? await response.json() : await response.text();
        if (requestKey && requestRevisions.get(requestKey) !== revision) return staleResponse;
        this.setConnectionState('online', 'Mit dem Server verbunden');
        return result;
      } catch (error) {
        const superseded = requestKey && requestRevisions.get(requestKey) !== revision;
        if (superseded) return staleResponse;
        if (error.name === 'AbortError' && !timedOut) return staleResponse;
        const offline = !navigator.onLine;
        const detail = timedOut
          ? 'Zeitüberschreitung bei der Serveranfrage'
          : (offline ? 'Netzwerkverbindung ist offline' : `Server nicht erreichbar: ${error.message}`);
        this.setConnectionState(
          offline ? 'offline' : 'degraded',
          detail,
        );
        if (options.captureError) return { __error: true, status: 0, detail };
        if (!options.silent) {
          this.showToast(timedOut ? 'Zeitüberschreitung bei der Anfrage' : `Netzwerkfehler: ${error.message}`, 'err');
        }
        return null;
      } finally {
        if (requestKey && requestControllers.get(requestKey) === controller) {
          requestControllers.delete(requestKey);
        }
      }
    },

    showToast(msg, type = 'ok') {
      if (this.toast.timer) clearTimeout(this.toast.timer);
      this.toast = { show: true, msg, type, timer: null };
      this.toast.timer = setTimeout(() => { this.toast.show = false; }, 4000);
    },

    applyDensity() {
      document.documentElement.dataset.density = this.density;
    },

    toggleDensity() {
      this.density = this.density === 'compact' ? 'comfortable' : 'compact';
      this.showToast(this.density === 'compact' ? 'Kompakte Ansicht aktiviert' : 'Komfortable Ansicht aktiviert');
    },

    dismissToast() {
      if (this.toast.timer) clearTimeout(this.toast.timer);
      this.toast.show = false;
    },

    setTheme(theme) {
      this.theme = theme;
      try { localStorage.setItem('rclone-sync-theme', theme); } catch (_) { /* optional */ }
      this.applyTheme();
    },

    cycleTheme() {
      const values = ['system', 'dark', 'light'];
      this.setTheme(values[(values.indexOf(this.theme) + 1) % values.length]);
    },

    applyTheme() {
      document.documentElement.dataset.theme = this.theme;
    },

    themeLabel() {
      return ({ system: 'System', dark: 'Dunkel', light: 'Hell' })[this.theme] || 'System';
    },

    busy() {
      return Boolean(
        this.pending.backup || this.pending.quick || this.pending.pbs ||
        this.status?.backup || this.status?.check || this.status?.quicksync || this.status?.pbs,
      );
    },

    rcloneBusy() {
      return Boolean(
        this.pending.backup || this.pending.quick ||
        this.status?.backup || this.status?.check || this.status?.quicksync,
      );
    },

    runningJob() {
      return this.status?.backup || this.status?.check || this.status?.quicksync || this.status?.pbs || null;
    },

    runningKind() {
      if (this.pending.pbs || this.status?.pbs) return 'PBS-Backup';
      if (this.status?.backup) return 'Backup';
      if (this.status?.check) return 'Check';
      if (this.status?.quicksync) return 'Quick-Sync';
      if (this.pending.backup) return 'Backup';
      if (this.pending.quick) return 'Quick-Sync';
      return '';
    },

    systemLevel() {
      const data = this.overview.data;
      if (!this.online) return 'error';
      if (!data) return 'pending';
      if (this.busy()) return 'running';
      if ((data.alerts || []).some((a) => a.level === 'error')) return 'error';
      if ((data.alerts || []).some((a) => a.level === 'warn')) return 'warn';
      return 'ok';
    },

    systemLabel() {
      const level = this.systemLevel();
      return ({ error: 'Handlungsbedarf', warn: 'Hinweise', running: `${this.runningKind()} läuft`, pending: 'Lädt', ok: 'Betrieb normal' })[level];
    },

    async loadOverview(silent = false) {
      this.overview.loading = !silent;
      const result = await this.api('GET', '/api/diagnostics/overview', undefined, { silent, requestKey: 'overview' });
      if (this.isStale(result)) return undefined;
      if (result) this.overview.data = result;
      this.overview.loading = false;
      return !!result;
    },

    async loadSchedulerState(silent = false) {
      this.schedulerControl.loading = true;
      const result = await this.api('GET', '/api/jobs/scheduler/state', undefined, { silent, requestKey: 'scheduler-state' });
      if (this.isStale(result)) return undefined;
      if (result) this.schedulerControl = { ...this.schedulerControl, ...result, loading: false };
      else this.schedulerControl.loading = false;
      return !!result;
    },

    async pauseScheduler(minutes = null, until = null) {
      const reason = String(this.schedulerPause.reason || 'Wartungsfenster').trim();
      const body = { reason };
      if (until) body.until = until;
      else body.minutes = Number(minutes || this.schedulerPause.minutes || 60);
      const result = await this.api('POST', '/api/jobs/scheduler/pause', body);
      if (result) {
        this.schedulerControl = { ...this.schedulerControl, ...result, loading: false };
        this.showToast('Automatische Zeitpläne pausiert', 'warn');
        this.loadOverview(true); this.loadAudit();
      }
    },

    pauseSchedulerUntilTomorrow() {
      const target = new Date();
      target.setDate(target.getDate() + 1);
      target.setHours(6, 0, 0, 0);
      this.pauseScheduler(null, target.getTime() / 1000);
    },

    async resumeScheduler() {
      const result = await this.api('POST', '/api/jobs/scheduler/resume', {});
      if (result) {
        this.schedulerControl = { ...this.schedulerControl, ...result, loading: false };
        this.showToast('Automatische Zeitpläne fortgesetzt');
        this.loadOverview(true); this.loadAudit();
      }
    },

    schedulerControlLabel() {
      if (this.config.backup?.enabled === false || !this.schedulerControl.enabled) return 'Automatik deaktiviert';
      if (!this.schedulerControl.paused) return 'Automatik aktiv';
      return this.schedulerControl.until ? `Pausiert bis ${this.formatDateTime(this.schedulerControl.until)}` : 'Pausiert';
    },

    async refreshStatus(silent = false) {
      const result = await this.api('GET', '/api/jobs/status/current', undefined, { silent, requestKey: 'current-status' });
      if (this.isStale(result)) return undefined;
      if (result) this.status = { ...this.status, ...result };
      return !!result;
    },

    async loadRecent(silent = false) {
      const result = await this.api('GET', '/api/jobs/list?limit=8', undefined, { silent, requestKey: 'recent-jobs' });
      if (this.isStale(result)) return undefined;
      if (result) this.recentJobs = result;
      return !!result;
    },

    async loadJobs(reset = false) {
      if (reset) this.jobs.offset = 0;
      this.jobs.loading = true;
      const params = new URLSearchParams({ limit: this.jobs.limit, offset: this.jobs.offset });
      if (this.jobs.kind) params.set('kind', this.jobs.kind);
      if (this.jobs.status) params.set('status', this.jobs.status);
      if (this.jobs.q.trim()) params.set('q', this.jobs.q.trim());
      this.jobs.error = '';
      const result = await this.api('GET', `/api/jobs/search?${params}`, undefined, { requestKey: 'jobs' });
      if (this.isStale(result)) return;
      if (result) {
        this.jobs.items = result.items || [];
        this.jobs.total = result.total || 0;
      } else {
        this.jobs.error = 'Jobhistorie konnte nicht geladen werden.';
      }
      this.jobs.loading = false;
    },

    jobPage() { return Math.floor(this.jobs.offset / this.jobs.limit) + 1; },
    jobPages() { return Math.max(1, Math.ceil(this.jobs.total / this.jobs.limit)); },
    nextJobs() { if (this.jobs.offset + this.jobs.limit < this.jobs.total) { this.jobs.offset += this.jobs.limit; this.loadJobs(); } },
    prevJobs() { if (this.jobs.offset > 0) { this.jobs.offset = Math.max(0, this.jobs.offset - this.jobs.limit); this.loadJobs(); } },

    downloadJobsCsv() {
      const params = new URLSearchParams({ limit: '10000' });
      if (this.jobs.kind) params.set('kind', this.jobs.kind);
      if (this.jobs.status) params.set('status', this.jobs.status);
      if (this.jobs.q.trim()) params.set('q', this.jobs.q.trim());
      window.location.assign(`/api/jobs/export.csv?${params}`);
    },

    async loadProgress(silent = false) {
      const result = await this.api('GET', '/api/jobs/backup/progress', undefined, { silent, requestKey: 'backup-progress' });
      if (this.isStale(result)) return null;
      if (result) {
        this.progress = result;
        if (!result.running) {
          await Promise.all([this.refreshStatus(true), this.loadRecent(true), this.loadOverview(true)]);
        }
      }
    },

    async ensureConfigSavedForRun() {
      if (!this.configDirty) return true;
      if (!confirm('Die Konfiguration enthält ungespeicherte Änderungen. Vor dem Start speichern?')) return false;
      return await this.saveConfig();
    },

    async runBackup(dryRun) {
      if (this.pending.backup) return;
      if (!(await this.ensureConfigSavedForRun())) return;
      if (!dryRun && !confirm('Produktiven Lauf für alle aktiven Pairs starten? Prüfe vorher möglichst den Plan oder einen Dry-Run.')) return;
      this.pending.backup = true;
      try {
        const result = await this.api('POST', `/api/jobs/backup/run?dry_run=${dryRun}`);
        if (result?.ok) {
          this.showToast(dryRun ? 'Dry-Run gestartet' : 'Backup gestartet');
          setTimeout(() => { this.refreshStatus(true); this.loadProgress(true); }, 400);
        }
      } finally {
        this.pending.backup = false;
      }
    },

    async cancelBackup() {
      if (!confirm('Laufenden Job wirklich abbrechen? Bereits übertragene Änderungen bleiben bestehen.')) return;
      const result = await this.api('POST', '/api/jobs/backup/cancel');
      if (result?.ok) this.showToast('Abbruchsignal gesendet');
      else if (result) this.showToast(result.error || 'Kein laufender Job', 'err');
    },

    async runSinglePair(name, dryRun = true) {
      if (!name || this.pending.backup) return;
      if (!(await this.ensureConfigSavedForRun())) return;
      if (!dryRun && !confirm(`Pair „${name}“ produktiv starten?`)) return;
      this.pending.backup = true;
      try {
        const result = await this.api('POST', `/api/jobs/backup/run-pair/${encodeURIComponent(name)}?dry_run=${dryRun}`);
        if (result?.ok) {
          this.showToast(dryRun ? `Dry-Run für „${name}“ gestartet` : `„${name}“ gestartet`);
          setTimeout(() => { this.refreshStatus(true); this.loadProgress(true); }, 400);
        }
      } finally {
        this.pending.backup = false;
      }
    },

    async checkPair(name) {
      if (!name || this.pending.backup) return;
      if (!(await this.ensureConfigSavedForRun())) return;
      this.pending.backup = true;
      try {
        const result = await this.api('POST', `/api/jobs/backup/check/${encodeURIComponent(name)}`);
        if (result?.ok) {
          this.showToast(`Read-only Check für „${name}“ gestartet`);
          setTimeout(() => { this.refreshStatus(true); this.loadJobs(true); }, 400);
        }
      } finally {
        this.pending.backup = false;
      }
    },

    async loadPlan(dryRun = true) {
      if (this.pending.plan) return;
      if (!(await this.ensureConfigSavedForRun())) return;
      this.pending.plan = true;
      this.plan.loading = true;
      this.plan.dry_run = dryRun;
      const result = await this.api('GET', `/api/jobs/backup/plan?dry_run=${dryRun}`, undefined, { requestKey: 'backup-plan' });
      if (!this.isStale(result) && result) {
        this.plan.data = result;
        this.openDialog('planDialog');
      }
      this.plan.loading = false;
      this.pending.plan = false;
    },

    openQuick() {
      this.quick = emptyQuick();
      this.quickModal.show = true;
      this.openDialog('quickDialog');
    },

    async runQuickSync() {
      if (this.pending.quick) return;
      if (!this.quick.remote || !this.quick.local) {
        this.showToast('Remote und lokaler Pfad müssen gesetzt sein', 'err'); return;
      }
      if (['sync', 'bisync'].includes(this.quick.mode) && !this.quick.dry_run) {
        if (!this.quick.allow_delete || this.quick.max_delete === null || this.quick.max_delete === '') {
          this.showToast('Produktiver Sync benötigt Löschfreigabe und Löschlimit', 'err'); return;
        }
        if (!confirm(`${this.quick.mode.toUpperCase()} kann bis zu ${this.quick.max_delete} Einträge löschen. Wirklich starten?`)) return;
      }
      const payload = { ...this.quick, min_local_files: this.quick.new_target ? 0 : 1 };
      delete payload.new_target;
      this.pending.quick = true;
      try {
        const result = await this.api('POST', '/api/jobs/backup/quick', payload);
        if (result?.ok) {
          this.showToast('Quick-Sync gestartet');
          this.closeQuick();
          setTimeout(() => { this.refreshStatus(true); this.loadProgress(true); }, 400);
        }
      } finally {
        this.pending.quick = false;
      }
    },

    async showJob(job) {
      if (!job?.id) return;
      const jobId = job.id;
      this.jobModal = { show: true, job: job || null, log: '', loading: true, logLoading: true, logSearch: '', autoRefresh: true };
      this.openDialog('jobDialog');
      const detail = await this.api('GET', `/api/jobs/${jobId}`, undefined, { requestKey: 'job-detail' });
      if (this.isStale(detail) || !this.jobModal.show || this.jobModal.job?.id !== jobId) return;
      if (detail) this.jobModal.job = detail;
      await this.loadJobLog(false, jobId);
      this.jobModal.loading = false;
    },

    async loadJobLog(silent = false, requestedJobId = null) {
      const jobId = requestedJobId || this.jobModal.job?.id;
      if (!jobId) return;
      this.jobModal.logLoading = !silent;
      const result = await this.api('GET', `/api/jobs/${jobId}/log?tail=5000`, undefined, { silent, requestKey: 'job-log' });
      if (this.isStale(result) || !this.jobModal.show || this.jobModal.job?.id !== jobId) return;
      if (result) this.jobModal.log = result.log || '';
      this.jobModal.logLoading = false;
    },

    async refreshJobModal() {
      const jobId = this.jobModal.job?.id;
      if (!jobId) return;
      const detail = await this.api('GET', `/api/jobs/${jobId}`, undefined, { silent: true, requestKey: 'job-detail' });
      if (this.isStale(detail) || !this.jobModal.show || this.jobModal.job?.id !== jobId) return;
      if (detail) this.jobModal.job = detail;
      await this.loadJobLog(true, jobId);
      if (detail?.status !== 'running') {
        this.loadJobs(false); this.loadRecent(true); this.loadOverview(true);
      }
    },

    filteredLog() {
      const text = this.jobModal.log || '';
      const needle = this.jobModal.logSearch.trim().toLowerCase();
      if (!needle) return text;
      return text.split('\n').filter((line) => line.toLowerCase().includes(needle)).join('\n');
    },

    async copyLog() {
      try {
        await navigator.clipboard.writeText(this.filteredLog());
        this.showToast('Log kopiert');
      } catch (_) {
        this.showToast('Log konnte nicht kopiert werden', 'err');
      }
    },

    downloadJobLog() {
      if (this.jobModal.job?.id) window.location.assign(`/api/jobs/${this.jobModal.job.id}/log/download`);
    },

    async cleanupFailed() {
      if (!confirm('Fehlgeschlagene, abgebrochene und verwaiste Jobs aus der Datenbank löschen? Die Logdateien bleiben bestehen.')) return;
      const result = await this.api('POST', '/api/jobs/cleanup-failed');
      if (result?.ok) {
        this.showToast(`${result.deleted} Jobs gelöscht`);
        this.loadJobs(true); this.loadOverview(true);
      }
    },

    async loadPbsStatus(silent = false) {
      this.pbs.loading = true;
      const wasRunning = Boolean(this.pbs.status?.running || this.status?.pbs);
      const result = await this.api('GET', '/api/pbs/status', undefined, { silent, requestKey: 'pbs-status' });
      if (this.isStale(result)) return undefined;
      this.pbs.loading = false;
      if (result) {
        this.pbs.status = result;
        this.status.pbs = result.running
          ? (result.running_job || { kind: 'pbs', status: 'running', started_at: Date.now() / 1000 })
          : null;
        if (wasRunning && !result.running) {
          this.loadRecent(true);
          this.loadOverview(true);
          if (this.page === 'jobs') this.loadJobs(false);
          this.showToast('PBS-Backup abgeschlossen');
        }
      }
      return Boolean(result);
    },
    async runPbs(target = null) {
      if (this.pending.pbs || this.pbs.status?.running) return;
      this.pending.pbs = true;
      try {
        const result = await this.api('POST', '/api/pbs/run', target ? { target } : {});
        if (result?.ok) {
          this.status.pbs = { id: result.job_id, kind: 'pbs', status: 'running', started_at: Date.now() / 1000 };
          this.showToast(`PBS-Backup gestartet (${(result.targets || []).join(', ')})`);
          setTimeout(() => { this.loadPbsStatus(); this.loadJobs(); }, 500);
        }
      } finally {
        this.pending.pbs = false;
      }
    },
    async cancelPbs() {
      if (this.pending.pbsCancel || !this.pbs.status?.running) return;
      if (!confirm('Laufendes PBS-Backup kontrolliert abbrechen?')) return;
      this.pending.pbsCancel = true;
      try {
        const result = await this.api('POST', '/api/pbs/cancel');
        if (result?.ok) {
          this.showToast('PBS-Abbruch angefordert', 'warn');
          setTimeout(() => this.loadPbsStatus(), 400);
        }
      } finally {
        this.pending.pbsCancel = false;
      }
    },
    addPbsTarget() {
      this.config.pbs ||= { enabled: false, targets: [], keep: {} };
      this.config.pbs.targets ||= [];
      this.config.pbs.targets.push({ name: '', paths: [], pathsText: '', schedule: 'manual', namespace: '', backup_id: '' });
      this.markConfigDirty();
    },
    removePbsTarget(index) {
      this.config.pbs.targets.splice(index, 1);
      this.markConfigDirty();
    },
    syncPbsTargets() {
      for (const target of (this.config.pbs?.targets || [])) {
        target.paths = (target.pathsText || '').split('\n').map(v => v.trim()).filter(Boolean);
      }
    },
    async loadConfig(silent = false) {
      this.configLoading = true;
      this.configError = '';
      const result = await this.api('GET', '/api/config', undefined, { silent, requestKey: 'config' });
      if (this.isStale(result)) return null;
      if (!result) {
        this.configLoading = false;
        this.configError = 'Konfiguration konnte nicht geladen werden. Bearbeitung bleibt gesperrt.';
        return false;
      }
      result.web ||= {};
      result.paths ||= {};
      result.web.allowed_hosts ||= ['*'];
      result.web.local_browse_roots ||= ['/mnt', '/media', '/srv', '/opt/rclone-sync/data'];
      result.web.secure_cookie ??= false;
      result.web.session_max_age_seconds ??= 604800;
      result.web.hsts_seconds ??= 0;
      result.backup ||= {};
      result.backup.pairs ||= [];
      result.backup.enabled ??= true;
      result.backup.rclone_args ||= [];
      result.backup.tuning ||= {};
      result.backup.tuning.transfers ??= 4;
      result.backup.tuning.checkers ??= 8;
      result.backup.tuning.retries ??= 3;
      result.backup.tuning.low_level_retries ??= 10;
      result.backup.tuning.stats_interval ||= '10s';
      result.backup.tuning.fast_list ??= false;
      result.backup.tuning.max_delete ??= 500;
      result.backup.require_delete_confirmation ??= true;
      result.backup.require_max_delete_for_sync ??= true;
      result.backup.allow_unsafe_rclone_args ??= false;
      result.backup.timezone ||= 'Europe/Berlin';
      result.backup.default_schedule ||= '0 3 * * *';
      result.backup.max_parallel ??= 2;
      result.backup.timeout_hours ??= 4;
      result.backup.scheduler_grace_minutes ??= 15;
      result.backup.scheduler_retry_minutes ??= 60;
      result.notifications ||= { webhooks: [] };
      result.notifications.allow_http ??= false;
      result.notifications.allow_private_targets ??= false;
      result.notifications.webhooks ||= [];
      result.maintenance ||= { auto_prune: true, job_retention_days: 180, keep_latest_jobs: 500, log_retention_days: 90 };
      for (const pair of (result.backup?.pairs || [])) {
        pair.two_way = pair.direction === 'bisync';
        if (pair.direction === 'pull') { pair.source = pair.remote; pair.target = pair.local; }
        else { pair.source = pair.local; pair.target = pair.remote; }
      }
      result.pbs ||= {};
      result.pbs.enabled ??= false;
      result.pbs.repository ||= '';
      result.pbs.password ||= '';
      result.pbs.fingerprint ||= '';
      result.pbs.namespace ||= '';
      result.pbs.backup_id ||= '';
      result.pbs.timeout_hours ??= 4;
      result.pbs.keep ||= {};
      result.pbs.keep.keep_last ??= 0;
      result.pbs.keep.keep_daily ??= 7;
      result.pbs.keep.keep_weekly ??= 4;
      result.pbs.keep.keep_monthly ??= 6;
      result.pbs.keep.keep_yearly ??= 0;
      result.pbs.targets ||= [];
      for (const target of result.pbs.targets) {
        target.pathsText = (target.paths || []).join('\n');
        target.schedule ||= 'manual';
      }
      for (const pair of result.backup.pairs) this.normalizePair(pair);
      for (const hook of result.notifications.webhooks) if (hook.enabled === undefined) hook.enabled = true;
      this.config = result;
      this.rcloneArgsText = (result.backup.rclone_args || []).join('\n');
      this.allowedHostsText = (result.web.allowed_hosts || []).join('\n');
      this.browseRootsText = (result.web.local_browse_roots || []).join('\n');
      this.syncScheduleEditorFromCron();
      this.performancePreset = this.detectPerformancePreset();
      this.configDirty = false;
      this.configLoaded = true;
      this.configLoading = false;
      this.configError = '';
      this.configValidation = { loading: false, ok: null, warnings: [], errors: [], revisionMatches: true };
      if (this.page === 'settings' && this.settingsTab === 'scheduler') this.refreshSchedulePreview();
      return true;
    },

    normalizePair(pair) {
      if (pair.enabled === undefined) pair.enabled = true;
      if (pair.schedule === undefined || pair.schedule === null) pair.schedule = 'manual';
      pair.direction ||= 'bisync';
      pair.mode ||= pair.direction === 'bisync' ? 'bisync' : 'copy';
      if (pair.min_local_files === undefined) pair.min_local_files = 1;
      if (pair.min_remote_files === undefined) pair.min_remote_files = 0;
      if (pair.allow_empty_remote_target === undefined) pair.allow_empty_remote_target = false;
      if (pair.min_free_gb === undefined) pair.min_free_gb = 0;
      if (pair.max_success_age_hours === undefined) pair.max_success_age_hours = 0;
      if (pair.allow_delete === undefined) pair.allow_delete = false;
      if (pair.require_mountpoint === undefined) pair.require_mountpoint = false;
      pair.mountpoint ||= '';
      pair.sentinel_file ||= '';
      pair.exclude ||= '';
      pair.include ||= '';
      pair.filter ||= '';
      pair.transfers ??= '';
      pair.checkers ??= '';
      pair.max_delete ??= 100;
      if (Array.isArray(pair.rclone_args)) pair.rclone_args = pair.rclone_args.join('\n');
      else pair.rclone_args ||= '';
    },

    configPayload() {
      const draft = JSON.parse(JSON.stringify(this.config));
      draft.backup ||= {}; draft.web ||= {};
      for (const key of ['password', 'password_hash', 'secret_key', 'session_version']) {
        delete draft.web[key];
      }
      draft.backup.rclone_args = this.rcloneArgsText.split('\n').map((value) => value.trim()).filter(Boolean);
      draft.web.allowed_hosts = this.allowedHostsText.split('\n').map((value) => value.trim()).filter(Boolean);
      draft.web.local_browse_roots = this.browseRootsText.split('\n').map((value) => value.trim()).filter(Boolean);
      for (const target of (draft.pbs?.targets || [])) delete target.pathsText;
      for (const pair of (draft.backup?.pairs || [])) {
        const source = (pair.source ?? pair.local ?? '').trim();
        const target = (pair.target ?? pair.remote ?? '').trim();
        const isLocal = (value) => value.startsWith('/');
        if (pair.two_way) {
          pair.direction = 'bisync'; pair.mode = 'bisync';
          // Gemischtes Bisync: Cloud-Seite ins remote-Feld, lokale Seite in local (Mount-Schutz).
          if (isLocal(source) && !isLocal(target)) { pair.remote = target; pair.local = source; }
          else if (!isLocal(source) && isLocal(target)) { pair.remote = source; pair.local = target; }
          else { pair.remote = source; pair.local = target; }
        } else if (isLocal(target) && !isLocal(source)) {
          // Cloud/anderes → lokales Ziel: pull, damit Mount-Schutz das Ziel bewacht.
          pair.direction = 'pull'; pair.remote = source; pair.local = target;
        } else {
          // Lokale/Cloud-Quelle → beliebiges Ziel: push, Mount-Schutz bewacht die Quelle.
          pair.direction = 'push'; pair.remote = target; pair.local = source;
        }
        if (pair.two_way === false && pair.mode === 'bisync') pair.mode = 'copy';
        delete pair.source; delete pair.target; delete pair.two_way;
      }
      return draft;
    },

    syncScheduleEditorFromCron() {
      const expression = String(this.config.backup?.default_schedule || 'manual').trim().toLowerCase();
      const editor = { mode: 'custom', time: '03:00', intervalHours: 6, intervalMinutes: 30, weekday: '0' };
      if (['', 'manual', 'off', 'disabled', 'none'].includes(expression)) {
        editor.mode = 'manual';
      } else {
        const parts = expression.split(/\s+/);
        if (parts.length === 5) {
          const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
          const pad = (value) => String(value).padStart(2, '0');
          if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
            editor.mode = 'daily'; editor.time = `${pad(hour)}:${pad(minute)}`;
          } else if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '1-5') {
            editor.mode = 'weekdays'; editor.time = `${pad(hour)}:${pad(minute)}`;
          } else if (/^\d+$/.test(minute) && /^\d+$/.test(hour) && dayOfMonth === '*' && month === '*' && /^[0-6]$/.test(dayOfWeek)) {
            editor.mode = 'weekly'; editor.time = `${pad(hour)}:${pad(minute)}`; editor.weekday = dayOfWeek;
          } else if (minute === '0' && /^\*\/\d+$/.test(hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
            editor.mode = 'hours'; editor.intervalHours = Math.max(1, Math.min(23, Number(hour.slice(2)) || 6));
          } else if (/^\*\/\d+$/.test(minute) && hour === '*' && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
            editor.mode = 'minutes'; editor.intervalMinutes = Math.max(5, Math.min(59, Number(minute.slice(2)) || 30));
          }
        }
      }
      this.scheduleEditor = editor;
    },

    applyScheduleMode(mode) {
      this.scheduleEditor.mode = mode;
      this.updateScheduleFromEditor();
    },

    updateScheduleFromEditor() {
      const editor = this.scheduleEditor;
      const [hourRaw, minuteRaw] = String(editor.time || '03:00').split(':');
      const hour = Math.max(0, Math.min(23, Number(hourRaw) || 0));
      const minute = Math.max(0, Math.min(59, Number(minuteRaw) || 0));
      let expression = this.config.backup.default_schedule || 'manual';
      if (editor.mode === 'manual') expression = 'manual';
      else if (editor.mode === 'daily') expression = `${minute} ${hour} * * *`;
      else if (editor.mode === 'weekdays') expression = `${minute} ${hour} * * 1-5`;
      else if (editor.mode === 'weekly') expression = `${minute} ${hour} * * ${editor.weekday || '0'}`;
      else if (editor.mode === 'hours') expression = `0 */${Math.max(1, Math.min(23, Number(editor.intervalHours) || 6))} * * *`;
      else if (editor.mode === 'minutes') expression = `*/${Math.max(5, Math.min(59, Number(editor.intervalMinutes) || 30))} * * * *`;
      this.config.backup.default_schedule = expression;
      this.markConfigDirty();
      this.refreshSchedulePreview();
    },

    onCustomScheduleInput() {
      this.scheduleEditor.mode = 'custom';
      this.markConfigDirty();
      this.refreshSchedulePreview();
    },

    refreshSchedulePreview() {
      if (this.schedulePreview.timer) clearTimeout(this.schedulePreview.timer);
      this.schedulePreview.timer = setTimeout(() => this.loadSchedulePreview(), 280);
    },

    async loadSchedulePreview() {
      const expression = String(this.config.backup?.default_schedule || 'manual').trim();
      const timezone = String(this.config.backup?.timezone || 'Europe/Berlin').trim();
      this.schedulePreview = { ...this.schedulePreview, loading: true, valid: null, error: '', nextRuns: [] };
      const result = await this.api(
        'POST',
        '/api/config/schedule-preview',
        { expression, timezone, count: 5 },
        { captureError: true, silent: true, requestKey: 'schedule-preview' },
      );
      if (this.isStale(result)) return;
      if (result?.__error) {
        const raw = result.detail;
        const error = typeof raw === 'string' ? raw : (raw?.message || raw?.detail || 'Zeitplan ungültig');
        this.schedulePreview = { ...this.schedulePreview, loading: false, valid: false, error: String(error), nextRuns: [] };
        return;
      }
      this.schedulePreview = { ...this.schedulePreview, loading: false, valid: true, error: '', nextRuns: result?.next_runs || [], enabled: result?.enabled !== false };
    },

    scheduleModeDescription() {
      const descriptions = {
        manual: 'Automatische Läufe sind ausgeschaltet. Jobs werden ausschließlich manuell gestartet.',
        daily: 'Ein Lauf pro Tag zur ausgewählten Uhrzeit.',
        weekdays: 'Montag bis Freitag zur ausgewählten Uhrzeit.',
        weekly: 'Ein Lauf pro Woche am ausgewählten Wochentag.',
        hours: 'Regelmäßiger Lauf in einem festen Stundenabstand.',
        minutes: 'Häufige Ausführung in einem festen Minutenabstand.',
        custom: 'Freie Cron-Angabe für besondere Zeitpläne.',
      };
      return descriptions[this.scheduleEditor.mode] || descriptions.custom;
    },

    scheduleNextLabel(run, index) {
      if (!run?.iso) return '—';
      const date = new Date(run.iso);
      const now = new Date();
      const tomorrow = new Date(now); tomorrow.setDate(tomorrow.getDate() + 1);
      let day = date.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' });
      if (date.toDateString() === now.toDateString()) day = 'Heute';
      else if (date.toDateString() === tomorrow.toDateString()) day = 'Morgen';
      const time = date.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
      return `${index + 1}. ${day}, ${time}`;
    },

    setPerformancePreset(preset) {
      const tuning = this.config.backup.tuning ||= {};
      const values = {
        gentle: { transfers: 2, checkers: 4, retries: 3, fast_list: false },
        balanced: { transfers: 4, checkers: 8, retries: 3, fast_list: false },
        fast: { transfers: 8, checkers: 16, retries: 3, fast_list: true },
      };
      if (values[preset]) Object.assign(tuning, values[preset]);
      this.performancePreset = preset;
      this.markConfigDirty();
    },

    detectPerformancePreset() {
      const tuning = this.config.backup?.tuning || {};
      const same = (value, transfers, checkers, retries, fastList) =>
        Number(value.transfers) === transfers && Number(value.checkers) === checkers && Number(value.retries) === retries && Boolean(value.fast_list) === fastList;
      if (same(tuning, 2, 4, 3, false)) return 'gentle';
      if (same(tuning, 4, 8, 3, false)) return 'balanced';
      if (same(tuning, 8, 16, 3, true)) return 'fast';
      return 'custom';
    },

    markPerformanceCustom() {
      this.performancePreset = this.detectPerformancePreset();
      this.markConfigDirty();
    },

    performanceHint() {
      if (this.performancePreset === 'gentle') return 'Geringe CPU-, RAM- und API-Last. Empfohlen für kleine LXC-Gäste und langsame Remotes.';
      if (this.performancePreset === 'fast') return 'Hoher Durchsatz, aber deutlich mehr RAM, CPU und gleichzeitige Remote-Anfragen.';
      if (this.performancePreset === 'custom') return 'Individuelle Werte. Bei Timeouts oder API-Limits Transfers und Checkers reduzieren.';
      return 'Guter Standard für die meisten Proxmox-LXC mit 2–4 vCPU und mindestens 2 GB RAM.';
    },

    schedulerRiskLevel() {
      const parallel = Number(this.config.backup?.max_parallel || 1);
      const transfers = Number(this.config.backup?.tuning?.transfers || 1);
      const checkers = Number(this.config.backup?.tuning?.checkers || 1);
      const pressure = parallel * (transfers + checkers);
      if (pressure >= 64) return 'high';
      if (pressure >= 32) return 'medium';
      return 'low';
    },

    schedulerRiskText() {
      const level = this.schedulerRiskLevel();
      if (level === 'high') return 'Hohe Parallelität: Für kleine LXC wahrscheinlich zu aggressiv.';
      if (level === 'medium') return 'Mittlere Last: Ressourcen und Remote-API-Limits beobachten.';
      return 'Moderate Last: für typische Proxmox-Gäste gut geeignet.';
    },

    async validateConfigDraft() {
      if (this.pending.validate || this.configLoading || !this.configLoaded) return false;
      this.pending.validate = true;
      this.configValidation = { loading: true, ok: null, warnings: [], errors: [], revisionMatches: true };
      const result = await this.api('POST', '/api/config/validate', { config: this.configPayload() }, { captureError: true, silent: true });
      if (result?.__error) {
        const detail = result.detail;
        const errors = Array.isArray(detail?.errors) ? detail.errors : [detail?.message || String(detail || 'Validierung fehlgeschlagen')];
        this.configValidation = { loading: false, ok: false, warnings: [], errors, revisionMatches: result.status !== 409 };
        this.showToast(`${errors.length} Konfigurationsfehler gefunden`, 'err');
        this.pending.validate = false;
        return false;
      }
      if (!result) {
        this.configValidation = { loading: false, ok: false, warnings: [], errors: ['Validierung konnte nicht ausgeführt werden'], revisionMatches: true };
        this.pending.validate = false;
        return false;
      }
      this.configValidation = { loading: false, ok: true, warnings: result?.warnings || [], errors: [], revisionMatches: result?.revision_matches !== false };
      this.showToast(result?.warnings?.length ? `Gültig mit ${result.warnings.length} Hinweis(en)` : 'Konfiguration ist gültig', result?.warnings?.length ? 'warn' : 'ok');
      this.pending.validate = false;
      return true;
    },

    async saveConfig() {
      if (this.pending.save || this.configLoading || !this.configLoaded) return false;
      this.pending.save = true;
      this.syncPbsTargets();
      try {
        const payload = { config: this.configPayload() };
        let result = await this.api('PUT', '/api/config', payload, { captureError: true, silent: true });
        if (result?.__error && result.status === 403) {
          const currentPassword = await this.requestCurrentPassword(
            typeof result.detail === 'string'
              ? result.detail
              : 'Diese sicherheitsrelevante Änderung benötigt dein aktuelles Passwort.',
          );
          if (!currentPassword) return false;
          try {
            result = await this.api(
              'PUT',
              '/api/config',
              { ...payload, current_password: currentPassword },
              { captureError: true, silent: true },
            );
          } finally {
            this.currentPasswordDialog.password = '';
          }
        }
        if (result?.__error) {
          const detail = typeof result.detail === 'string'
            ? result.detail
            : (result.detail?.message || 'Einstellungen konnten nicht gespeichert werden');
          this.showToast(detail, 'err');
          return false;
        }
        if (!result?.ok) return false;
        this.config = result.config || this.config;
        this.configDirty = false;
        if (result.warnings?.length) this.showToast(`Gespeichert: ${result.warnings.join(' · ')}`, 'warn');
        else this.showToast('Einstellungen gespeichert');
        await this.loadConfig();
        this.loadOverview(true);
        return true;
      } finally {
        this.pending.save = false;
      }
    },

    syncPairDirection(pair) {
      if (pair.two_way === 'true') pair.two_way = true;
      if (pair.two_way === 'false') pair.two_way = false;
      if (pair.two_way) pair.mode = 'bisync';
      else if (pair.mode === 'bisync') pair.mode = 'copy';
      this.markConfigDirty();
    },
    convertPairToPbs(idx) {
      const pair = this.config.backup.pairs[idx];
      this.config.pbs ||= { enabled: false, targets: [], keep: {} };
      this.config.pbs.targets ||= [];
      const paths = (pair.source || '').startsWith('/') ? [pair.source] : [];
      this.config.pbs.targets.push({
        name: pair.name || '', paths, pathsText: paths.join('\n'),
        schedule: pair.schedule || 'manual', namespace: '', backup_id: '',
      });
      this.config.backup.pairs.splice(idx, 1);
      delete this.pairOpen[idx];
      this.markConfigDirty();
      this.showToast(this.config.pbs?.enabled ? 'In PBS-Backup umgewandelt – unten prüfen und speichern' : 'PBS-Backup angelegt – Verbindung unter Einstellungen → Proxmox Backup fehlt noch', this.config.pbs?.enabled ? 'ok' : 'warn');
    },
    addPair(preset = this.newPairPreset || 'push-copy') {
      if (preset === 'pbs') {
        this.addPbsTarget();
        const idx = this.config.pbs.targets.length - 1;
        this.showToast(this.config.pbs?.enabled ? 'PBS-Backup angelegt – Name und Pfade setzen' : 'PBS-Backup angelegt – Verbindung unter Einstellungen → Proxmox Backup einrichten', this.config.pbs?.enabled ? 'ok' : 'warn');
        this.$nextTick(() => document.querySelectorAll('[x-for]')); // Layout aktualisiert sich über Alpine
        return;
      }
      const templates = {
        'push-copy': { direction: 'push', mode: 'copy' },
        'pull-copy': { direction: 'pull', mode: 'copy' },
        'bisync': { direction: 'bisync', mode: 'bisync' },
        'push-sync': { direction: 'push', mode: 'sync' },
      };
      const selected = templates[preset] || templates['push-copy'];
      const pair = {
        name: '', remote: '', local: '', source: '', target: '', schedule: 'manual', enabled: false,
        direction: selected.direction, mode: selected.mode, two_way: selected.direction === 'bisync', min_local_files: 1,
        exclude: '.DS_Store\nThumbs.db', include: '', filter: '', rclone_args: '',
        transfers: '', checkers: '', max_delete: 100, allow_delete: false,
        min_remote_files: 0, allow_empty_remote_target: false, min_free_gb: 0, max_success_age_hours: 0, require_mountpoint: false,
        mountpoint: '', sentinel_file: '',
      };
      this.config.backup.pairs.push(pair);
      const idx = this.config.backup.pairs.length - 1;
      this.pairOpen[idx] = true;
      this.configDirty = true;
      this.showToast(selected.mode === 'copy' ? 'Sichere Copy-Vorlage angelegt' : 'Deaktivierte Vorlage angelegt – Löschschutz prüfen', selected.mode === 'copy' ? 'ok' : 'warn');
      this.$nextTick(() => document.getElementById(`pair-${idx}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
    },

    clonePair(idx) {
      const source = this.config.backup.pairs[idx] || {};
      const clone = JSON.parse(JSON.stringify(source));
      delete clone.id;
      clone.name = `${clone.name || 'Pair'}_Kopie`;
      clone.enabled = false;
      clone.schedule = 'manual';
      this.config.backup.pairs.splice(idx + 1, 0, clone);
      this.configDirty = true;
      this.pairOpen[idx + 1] = true;
      this.showToast('Pair als deaktivierte Kopie angelegt');
    },

    removePair(idx) {
      const pair = this.config.backup.pairs[idx];
      if (!confirm(`Pair „${pair?.name || 'ohne Namen'}“ aus der Konfiguration entfernen?`)) return;
      this.config.backup.pairs.splice(idx, 1);
      this.configDirty = true;
    },

    movePair(idx, delta) {
      const target = idx + delta;
      if (target < 0 || target >= this.config.backup.pairs.length) return;
      const [pair] = this.config.backup.pairs.splice(idx, 1);
      this.config.backup.pairs.splice(target, 0, pair);
      this.configDirty = true;
    },

    visiblePairCount() {
      return (this.config.backup.pairs || []).filter((pair) => this.pairVisible(pair)).length;
    },

    setAllPairsOpen(open) {
      const next = { ...this.pairOpen };
      (this.config.backup.pairs || []).forEach((pair, idx) => {
        if (this.pairVisible(pair)) next[idx] = Boolean(open);
      });
      this.pairOpen = next;
    },

    pairVisible(pair) {
      const needle = this.pairSearch.trim().toLowerCase();
      if (needle && !`${pair.name || ''} ${pair.remote || ''} ${pair.local || ''} ${pair.source || ''} ${pair.target || ''}`.toLowerCase().includes(needle)) return false;
      if (this.pairFilter === 'enabled' && !pair.enabled) return false;
      if (this.pairFilter === 'disabled' && pair.enabled) return false;
      if (this.pairFilter === 'destructive' && !(pair.direction === 'bisync' || pair.mode === 'sync')) return false;
      if (this.pairFilter === 'issues' && this.pairIssues(pair).length === 0 && !this.pairRuntimeIssue(pair)) return false;
      return true;
    },

    pairIssues(pair) {
      const issues = [];
      if (!String(pair.name || '').trim()) issues.push('Name fehlt');
      const src = String(pair.source ?? pair.local ?? '').trim();
      const dst = String(pair.target ?? pair.remote ?? '').trim();
      if (!src) issues.push('Quelle fehlt');
      else if (!src.startsWith('/') && !src.includes(':')) issues.push('Quelle ungültig (lokaler Pfad oder remote:/pfad)');
      if (!dst) issues.push('Ziel fehlt');
      else if (!dst.startsWith('/') && !dst.includes(':')) issues.push('Ziel ungültig (lokaler Pfad oder remote:/pfad)');
      if (src && dst && src.replace(/\/+$/, '') === dst.replace(/\/+$/, '')) issues.push('Quelle und Ziel identisch');
      const effectiveSchedule = String(pair.schedule || this.config.backup?.default_schedule || 'manual').trim().toLowerCase();
      if (pair.enabled && ['off', 'disabled', 'none'].includes(effectiveSchedule)) issues.push('Zeitplan deaktiviert');
      const destructive = pair.two_way || pair.direction === 'bisync' || pair.mode === 'sync';
      if (destructive && pair.enabled && !pair.allow_delete) issues.push('Löschen nicht freigegeben');
      if (destructive && pair.enabled && (pair.max_delete === '' || pair.max_delete === null || pair.max_delete === undefined)) issues.push('Löschlimit fehlt');
      if (pair.require_mountpoint && !String(pair.mountpoint || '').trim()) issues.push('Mountpoint fehlt');
      return issues;
    },

    pairScheduleLabel(pair) {
      const own = String(pair?.schedule || '').trim();
      if (!own) return `Standard · ${this.humanCron(this.config.backup?.default_schedule || 'manual')}`;
      return this.humanCron(own);
    },

    pairRuntimeIssue(pair) {
      const health = this.pairLastRun(pair);
      if (health?.last_status === 'stale') return health.error || 'Letzter Lauf blieb unvollständig';
      if (health?.last_status === 'error') return health.error || 'Letzter Lauf fehlgeschlagen';
      if (health?.overdue) {
        if (!health.last_success) return `Noch kein erfolgreicher Lauf (Frist ${health.max_success_age_hours} Std.)`;
        return `Letzter Erfolg ist ${Math.round(health.success_age_hours || 0)} Std. alt`;
      }
      return '';
    },

    pairRuntimeIssueLevel(pair) {
      const health = this.pairLastRun(pair);
      return health?.last_status === 'error' || health?.last_status === 'stale' ? 'error' : 'warn';
    },

    pairStatus(pair) {
      const health = this.pairLastRun(pair);
      if (!pair.enabled) return 'disabled';
      if (health?.last_status === 'error' || health?.last_status === 'stale') return 'error';
      if (health?.overdue || this.pairIssues(pair).length) return 'warn';
      if (health?.last_status === 'ok') return 'ok';
      return 'pending';
    },

    pairLastRun(pair) {
      return (this.overview.data?.pairs?.health || []).find((item) => item.name === pair.name) || null;
    },

    async testRclone() {
      this.testResults._global = { loading: true };
      const result = await this.api('POST', '/api/test/rclone', {});
      this.testResults._global = result || { ok: false, error: 'Test fehlgeschlagen' };
    },

    async testPair(idx) {
      this.testResults[idx] = { loading: true };
      const result = await this.api('POST', '/api/test/rclone', { pair: this.config.backup.pairs[idx] });
      this.testResults[idx] = result || { ok: false, error: 'Test fehlgeschlagen' };
    },

    async loadStorage(includeRemote = false) {
      const result = await this.api('GET', `/api/storage/overview${includeRemote ? '?include_remote=true' : ''}`, undefined, { silent: !includeRemote, timeoutMs: includeRemote ? 120000 : 30000 });
      if (result?.pairs && this.overview.data) this.overview.data.storage_pairs = result.pairs;
      return result;
    },

    storagePairs() { return this.overview.data?.storage_pairs || []; },

    openPicker(mode, idx) {
      this.picker = { show: true, mode, idx, current: '', parent: null, entries: [], loading: true, search: '', error: '' };
      this.openDialog('pickerDialog');
      this.loadPicker('');
    },

    openQuickPicker(mode) { this.openPicker(mode, -1); },
    openPbsPicker(index) { this.openPicker('pbs-target', index); },

    async loadPicker(path) {
      if (path === 'pbs:') { this.pickPath('pbs:'); return; }
      this.picker.loading = true;
      this.picker.error = '';
      this.picker.current = path;
      const endpoint = this.picker.mode.endsWith('-remote') || this.picker.mode === 'remote' ? '/api/browse/rclone' : '/api/browse/local';
      // 'remote-local': lokaler Browser, Auswahl landet im Remote-Feld (lokal→lokal-Sync)
      const result = await this.api(
        'GET',
        endpoint + (path ? `?path=${encodeURIComponent(path)}` : ''),
        undefined,
        { requestKey: 'picker' },
      );
      if (this.isStale(result)) return;
      if (result) {
        this.picker.parent = result.parent;
        this.picker.entries = result.entries || [];
        this.picker.current = result.path || path || '';
        this.picker.error = result.error || '';
        const cloudRoot = (this.picker.mode.endsWith('-remote') || this.picker.mode === 'remote') && !this.picker.current;
        if (cloudRoot && this.config?.pbs?.enabled) {
          this.picker.entries = [
            { name: 'Proxmox Backup Server', path: 'pbs:', pbs: true },
            ...this.picker.entries,
          ];
        }
      } else {
        this.picker.entries = [];
        this.picker.error = 'Verzeichnis konnte nicht geladen werden.';
      }
      this.picker.loading = false;
    },

    pickerEntries() {
      const needle = this.picker.search.trim().toLowerCase();
      return needle ? this.picker.entries.filter((entry) => String(entry.name || '').toLowerCase().includes(needle)) : this.picker.entries;
    },

    pickPath(path) {
      const { mode, idx } = this.picker;
      if (path === 'pbs:') {
        this.closePicker();
        if (mode.startsWith('target-') && idx >= 0) { this.convertPairToPbs(idx); return; }
        this.showToast('PBS ist als Quelle nicht syncbar: Der Datastore ist ein deduplizierter Chunk-Store, einzelne Container/VMs lassen sich daraus nicht als Dateien herauskopieren. Für Cloud-Replikation den PBS-eigenen S3-Sync nutzen oder vzdump-Dateien als Quelle syncen.', 'warn');
        return;
      }
      if (mode === 'pbs-target') {
        const target = this.config.pbs?.targets?.[idx];
        if (target) {
          const lines = (target.pathsText || '').split('\n').map(v => v.trim()).filter(Boolean);
          if (!lines.includes(path)) lines.push(path);
          target.pathsText = lines.join('\n');
          this.markConfigDirty();
        }
        this.closePicker();
        this.showToast(`Pfad hinzugefügt: ${path}`);
        return;
      }
      if (idx === -1) {
        if (mode === 'remote' || mode === 'remote-local') this.quick.remote = path;
        else this.quick.local = path;
      } else if (mode.startsWith('source-') || mode.startsWith('target-')) {
        const pair = this.config.backup.pairs[idx];
        if (pair) {
          pair[mode.startsWith('source-') ? 'source' : 'target'] = path;
          this.syncPairDirection(pair);
        }
      } else {
        if (mode === 'remote' || mode === 'remote-local') this.config.backup.pairs[idx].remote = path;
        else this.config.backup.pairs[idx].local = path;
        this.configDirty = true;
      }
      this.closePicker();
      this.showToast(`Pfad gesetzt: ${path}`);
    },

    async loadDoctor() {
      this.doctor.loading = true;
      const result = await this.api('GET', '/api/diagnostics/doctor', undefined, { timeoutMs: 120000, requestKey: 'doctor' });
      if (this.isStale(result)) return;
      if (result) this.doctor.data = result;
      this.doctor.loading = false;
    },

    doctorCounts() {
      const data = this.doctor.data;
      const all = [...(data?.checks || []), ...(data?.pairs || []).flatMap((pair) => pair.checks || [])];
      return {
        ok: all.filter((item) => item.level === 'ok').length,
        warn: all.filter((item) => item.level === 'warn').length,
        error: all.filter((item) => item.level === 'error').length,
      };
    },

    async loadLogs() {
      this.maintenance.loading = true;
      const query = this.maintenance.logQuery ? `&query=${encodeURIComponent(this.maintenance.logQuery)}` : '';
      const result = await this.api('GET', `/api/maintenance/logs?limit=200${query}`, undefined, { requestKey: 'maintenance-logs' });
      if (this.isStale(result)) return;
      if (result?.logs) this.maintenance.logs = result.logs;
      this.maintenance.loading = false;
    },

    async pruneLogs(dryRun = true) {
      const days = Number(this.config.maintenance?.log_retention_days || 90);
      const result = await this.api('POST', `/api/maintenance/logs/prune?days=${days}&dry_run=${dryRun}`);
      if (result) {
        this.maintenance.prune = result;
        this.showToast(dryRun ? `${result.matched} alte Logs gefunden` : `${result.deleted} alte Logs gelöscht`);
        this.loadLogs();
      }
    },

    async loadDatabaseStatus() {
      const result = await this.api('GET', '/api/maintenance/database', undefined, { requestKey: 'database-status' });
      if (this.isStale(result)) return;
      if (result) this.maintenance.database = result;
    },

    async pruneDatabase() {
      const cfg = this.config.maintenance || {};
      const days = cfg.job_retention_days || 180;
      const keep = cfg.keep_latest_jobs || 500;
      const result = await this.api('POST', `/api/maintenance/database/prune?days=${days}&keep_latest=${keep}`);
      if (result) {
        this.maintenance.database = result;
        this.showToast(`${result.deleted_jobs} alte Jobs gelöscht`);
        this.loadJobs(true); this.loadOverview(true);
      }
    },

    async loadSnapshots() {
      this.snapshots.loading = true;
      const result = await this.api('GET', '/api/maintenance/config/snapshots', undefined, { silent: true, requestKey: 'snapshots' });
      if (this.isStale(result)) return;
      if (result?.snapshots) {
        this.snapshots.items = result.snapshots;
        this.snapshots.max = result.max_snapshots || 30;
      }
      this.snapshots.loading = false;
    },

    async createSnapshot() {
      const result = await this.api('POST', '/api/maintenance/config/snapshots');
      if (result?.ok) {
        this.showToast(`Snapshot erstellt: ${result.snapshot.name}`);
        await this.loadSnapshots();
      }
    },

    async restoreSnapshot() {
      if (!this.snapshots.restoreName || !this.snapshots.password) {
        this.showToast('Snapshot und aktuelles Passwort erforderlich', 'err'); return;
      }
      if (!confirm('Diesen Snapshot wiederherstellen? Aktuelle Zugangsdaten bleiben erhalten; alle Sitzungen werden beendet.')) return;
      const selected = this.snapshots.items.find((item) => item.name === this.snapshots.restoreName);
      const result = await this.api('POST', '/api/maintenance/config/snapshots/restore', {
        name: this.snapshots.restoreName,
        current_password: this.snapshots.password,
        expected_revision: this.config._revision,
        sha256: selected?.sha256 || null,
      });
      if (result?.ok) {
        this.showToast('Snapshot wiederhergestellt – erneute Anmeldung erforderlich');
        setTimeout(() => { window.location = '/login'; }, 900);
      }
    },

    downloadSupportBundle() { window.location.assign('/api/maintenance/support-bundle'); },
    downloadRedactedConfig() { window.location.assign('/api/maintenance/config/export'); },

    async loadAudit() {
      this.audit.loading = true;
      const suffix = this.audit.eventType ? `?limit=100&event_type=${encodeURIComponent(this.audit.eventType)}` : '?limit=100';
      const result = await this.api('GET', `/api/maintenance/audit${suffix}`, undefined, { silent: true, requestKey: 'audit' });
      if (this.isStale(result)) return;
      if (result?.events) this.audit.items = result.events;
      this.audit.loading = false;
    },

    auditLabel(type) {
      return ({
        scheduler_paused: 'Scheduler pausiert', scheduler_resumed: 'Scheduler fortgesetzt',
        config_saved: 'Konfiguration gespeichert', filter_saved: 'Filter gespeichert',
        password_changed: 'Passwort geändert', config_snapshot_created: 'Snapshot erstellt',
        config_snapshot_restored: 'Snapshot wiederhergestellt', backup_requested: 'Backup angefordert',
        check_requested: 'Check angefordert', quicksync_requested: 'Quick-Sync angefordert',
      })[type] || type;
    },

    async loadFilterFile() {
      this.filterFile.loading = true;
      const result = await this.api('GET', '/api/config/filter-file', undefined, { requestKey: 'filter-file' });
      if (this.isStale(result)) return;
      if (result) {
        this.filterFile.content = result.content || '';
        this.filterFile.path = result.path || '';
        this.filterFile.revision = result.revision || '';
        this.filterFile.dirty = false;
      }
      this.filterFile.loading = false;
    },

    async saveFilterFile() {
      if (this.pending.filter) return false;
      this.pending.filter = true;
      try {
        const result = await this.api('PUT', '/api/config/filter-file', { content: this.filterFile.content, revision: this.filterFile.revision });
        if (result?.ok) {
          this.filterFile.revision = result.revision || this.filterFile.revision;
          this.filterFile.dirty = false;
          this.showToast(`Filter gespeichert (${result.bytes} B)`);
          return true;
        }
        return false;
      } finally {
        this.pending.filter = false;
      }
    },

    addWebhook() {
      this.config.notifications ||= { webhooks: [] };
      this.config.notifications.webhooks ||= [];
      this.config.notifications.webhooks.push({
        id: window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`,
        enabled: true, type: 'discord', url: '', events: ['sync_error', 'mount_check_failed'],
      });
      this.configDirty = true;
    },

    toggleHookEvent(hook, event) {
      hook.events ||= [];
      const idx = hook.events.indexOf(event);
      if (idx >= 0) hook.events.splice(idx, 1);
      else hook.events.push(event);
      this.configDirty = true;
    },

    async testWebhook(idx) {
      if (this.configDirty && !(await this.saveConfig())) return;
      const hook = this.config.notifications.webhooks[idx];
      const result = await this.api('POST', '/api/config/test-webhook', { index: idx, id: hook?.id, event: 'sync_ok' });
      if (result?.ok) this.showToast('Webhook-Test gesendet');
    },

    async changePassword() {
      if (this.pending.password) return;
      if (this.pwChange.new !== this.pwChange.confirm) { this.showToast('Passwortwiederholung stimmt nicht überein', 'err'); return; }
      if (this.pwChange.new.length < 12) { this.showToast('Mindestens 12 Zeichen erforderlich', 'err'); return; }
      this.pending.password = true;
      try {
        const result = await this.api('POST', '/api/config/change-password', { current_password: this.pwChange.current, new_password: this.pwChange.new });
        if (result?.ok) {
          this.showToast('Passwort geändert – bitte neu anmelden');
          setTimeout(() => { window.location = '/login'; }, 800);
        }
      } finally {
        this.pending.password = false;
      }
    },

    async logout() {
      if ((this.configDirty || this.filterFile.dirty) && !confirm('Ungespeicherte Änderungen verwerfen und abmelden?')) return;
      const result = await this.api('POST', '/logout');
      if (result !== null) window.location = '/login';
    },

    focusableElements(dialog) {
      if (!dialog) return [];
      return [...dialog.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
        'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )].filter((element) => !element.hidden && element.offsetParent !== null);
    },

    openDialog(refName) {
      dialogFocusStack.push(document.activeElement);
      this.$nextTick(() => {
        const dialog = this.$refs[refName];
        const target = dialog?.querySelector('[data-dialog-initial-focus]') || this.focusableElements(dialog)[0] || dialog;
        target?.focus();
      });
    },

    restoreDialogFocus() {
      const target = dialogFocusStack.pop();
      this.$nextTick(() => {
        if (target?.isConnected) target.focus();
      });
    },

    activeDialog() {
      if (this.currentPasswordDialog.show) return this.$refs.currentPasswordDialog;
      if (this.jobModal.show) return this.$refs.jobDialog;
      if (this.picker.show) return this.$refs.pickerDialog;
      if (this.quickModal.show) return this.$refs.quickDialog;
      if (this.plan.data) return this.$refs.planDialog;
      return null;
    },

    requestCurrentPassword(message = '') {
      if (currentPasswordResolver) currentPasswordResolver(null);
      this.currentPasswordDialog = { show: true, password: '', error: message };
      this.openDialog('currentPasswordDialog');
      return new Promise((resolve) => {
        currentPasswordResolver = resolve;
      });
    },

    submitCurrentPassword() {
      const password = String(this.currentPasswordDialog.password || '');
      if (!password) {
        this.currentPasswordDialog.error = 'Aktuelles Passwort ist erforderlich.';
        return;
      }
      const resolve = currentPasswordResolver;
      currentPasswordResolver = null;
      this.currentPasswordDialog.show = false;
      this.currentPasswordDialog.password = '';
      this.restoreDialogFocus();
      resolve?.(password);
    },

    cancelCurrentPassword() {
      const resolve = currentPasswordResolver;
      currentPasswordResolver = null;
      this.currentPasswordDialog = { show: false, password: '', error: '' };
      this.restoreDialogFocus();
      resolve?.(null);
    },

    trapDialogFocus(event) {
      const dialog = this.activeDialog();
      if (!dialog) return;
      const focusable = this.focusableElements(dialog);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },

    closePlan() {
      if (!this.plan.data) return;
      this.plan.data = null;
      this.restoreDialogFocus();
    },

    closeQuick() {
      if (!this.quickModal.show) return;
      this.quickModal.show = false;
      this.restoreDialogFocus();
    },

    closePicker() {
      if (!this.picker.show) return;
      this.picker.show = false;
      requestControllers.get('picker')?.abort();
      this.restoreDialogFocus();
    },

    closeJob() {
      if (!this.jobModal.show) return;
      this.jobModal.show = false;
      requestControllers.get('job-detail')?.abort();
      requestControllers.get('job-log')?.abort();
      this.restoreDialogFocus();
    },

    closeOverlays() {
      if (this.currentPasswordDialog.show) this.cancelCurrentPassword();
      else if (this.jobModal.show) this.closeJob();
      else if (this.picker.show) this.closePicker();
      else if (this.quickModal.show) this.closeQuick();
      else if (this.plan.data) this.closePlan();
      else this.navOpen = false;
    },

    selectSettingsTab(tab, focus = false) {
      if (!this.settingsTabs.includes(tab)) return;
      this.settingsTab = tab;
      if (tab === 'scheduler') this.refreshSchedulePreview();
      if (tab === 'pbs') this.loadPbsStatus();
      if (focus) {
        this.$nextTick(() => document.getElementById(`settings-tab-${tab}`)?.focus());
      }
    },

    moveSettingsTab(delta) {
      const current = Math.max(0, this.settingsTabs.indexOf(this.settingsTab));
      const next = (current + delta + this.settingsTabs.length) % this.settingsTabs.length;
      this.selectSettingsTab(this.settingsTabs[next], true);
    },

    markConfigDirty() {
      this.configDirty = true;
      if (this.configValidation.ok !== null) this.configValidation.ok = null;
    },

    formatBytes(value) {
      if (value === null || value === undefined || value === '') return '—';
      let bytes = Number(value);
      if (!Number.isFinite(bytes)) return String(value);
      if (bytes < 1024) return `${bytes} B`;
      const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
      let index = -1;
      do { bytes /= 1024; index += 1; } while (bytes >= 1024 && index < units.length - 1);
      return `${bytes.toFixed(bytes >= 100 ? 0 : 1)} ${units[index]}`;
    },

    formatTs(value) {
      if (!value) return 'Noch nie';
      const date = new Date(Number(value) * 1000);
      const now = new Date();
      const diff = Math.max(0, (now - date) / 1000);
      const time = date.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
      if (diff < 60) return 'gerade eben';
      if (diff < 3600) return `vor ${Math.round(diff / 60)} Min.`;
      if (date.toDateString() === now.toDateString()) return `heute ${time}`;
      const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
      if (date.toDateString() === yesterday.toDateString()) return `gestern ${time}`;
      return `${date.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: '2-digit' })} ${time}`;
    },

    formatDateTime(value) {
      if (!value) return '—';
      return new Date(Number(value) * 1000).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' });
    },

    formatDur(seconds) {
      if (seconds === null || seconds === undefined) return '—';
      let value = Math.max(0, Math.round(Number(seconds)));
      const days = Math.floor(value / 86400); value %= 86400;
      const hours = Math.floor(value / 3600); value %= 3600;
      const minutes = Math.floor(value / 60); const secs = value % 60;
      if (days) return `${days}d ${hours}h`;
      if (hours) return `${hours}h ${minutes}m`;
      if (minutes) return `${minutes}m ${secs}s`;
      return `${secs}s`;
    },

    formatUptime(seconds) {
      if (!seconds) return '—';
      const days = Math.floor(seconds / 86400);
      const hours = Math.floor((seconds % 86400) / 3600);
      return days ? `${days} Tage, ${hours} Std.` : `${hours} Std.`;
    },

    humanCron(expr) {
      if (!expr) return 'Manuell';
      const value = expr.trim().toLowerCase();
      if (['manual', 'off', 'disabled', 'none', ''].includes(value)) return 'Nur manuell';
      const parts = value.split(/\s+/);
      if (parts.length !== 5) return 'Cron ungültig';
      const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
      const days = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
      if (/^\*\/\d+$/.test(minute) && hour === '*' && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
        return `Alle ${minute.slice(2)} Minuten`;
      }
      if (minute === '0' && /^\*\/\d+$/.test(hour) && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
        return `Alle ${hour.slice(2)} Stunden`;
      }
      let time;
      if (minute.startsWith('*/')) time = `alle ${minute.slice(2)} Minuten`;
      else if (hour === '*') time = `stündlich :${minute.padStart(2, '0')}`;
      else if (hour.startsWith('*/')) time = `alle ${hour.slice(2)} Stunden`;
      else time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')} Uhr`;
      let day = 'Täglich';
      if (dayOfWeek !== '*') {
        if (dayOfWeek === '1-5') day = 'Montag bis Freitag';
        if (dayOfWeek.includes('-')) {
          const [a, b] = dayOfWeek.split('-').map(Number);
          day = `${days[a] || a} bis ${days[b] || b}`;
        } else if (dayOfWeek.includes(',')) day = dayOfWeek.split(',').map((n) => days[Number(n)] || n).join(', ');
        else day = days[Number(dayOfWeek)] || dayOfWeek;
      } else if (dayOfMonth !== '*') day = `Am ${dayOfMonth}.`;
      else if (month !== '*') day = `Monat ${month}`;
      return `${day} um ${time}`;
    },

    statusLabel(status) {
      return ({ running: 'Läuft', ok: 'Erfolgreich', error: 'Fehler', skipped: 'Übersprungen', cancelled: 'Abgebrochen', stale: 'Verwaist', pending: 'Ausstehend', done: 'Fertig', disabled: 'Deaktiviert', warn: 'Prüfen' })[status] || status || 'Unbekannt';
    },

    kindLabel(kind) {
      return ({ backup: 'Backup', check: 'Check', quicksync: 'Quick-Sync', pbs: 'PBS-Backup' })[kind] || kind || 'Job';
    },

    directionLabel(pair) {
      const shorten = (value) => {
        const text = String(value || '?');
        return text.length > 28 ? `${text.slice(0, 13)}…${text.slice(-13)}` : text;
      };
      if (pair.two_way || pair.direction === 'bisync') return `${shorten(pair.source ?? pair.remote)} ⇄ ${shorten(pair.target ?? pair.local)}`;
      const mode = pair.mode === 'sync' ? 'Mirror' : 'Copy';
      const src = pair.source ?? (pair.direction === 'pull' ? pair.remote : pair.local);
      const dst = pair.target ?? (pair.direction === 'pull' ? pair.local : pair.remote);
      return `${shorten(src)} → ${shorten(dst)} · ${mode}`;
    },

    summaryShort(summary) {
      if (!summary) return 'Keine Zusammenfassung';
      if (summary.ok_count !== undefined) return `${summary.ok_count}/${summary.total_pairs} Paare${summary.dry_run ? ' · Dry-Run' : ''}`;
      if (summary.pair && summary.command) return `Check ${summary.pair}`;
      if (summary.error) return String(summary.error).substring(0, 120);
      if (summary.remote && summary.local) return `${summary.direction || ''} ${summary.mode || ''}`.trim();
      return 'Details verfügbar';
    },

    prettyJson(value) {
      return JSON.stringify(value || {}, null, 2);
    },
  };
}

// PWA: Service Worker best-effort registrieren (nur Secure Context / localhost).
// Fehler werden bewusst verschluckt – die App funktioniert ohne SW unverändert.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
  });
}
