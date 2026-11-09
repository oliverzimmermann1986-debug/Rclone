// Kern: Start, Polling, api(), Toast, Theme, Dialog-Fokus, Navigation, Anzeigeformate.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.

const DIALOG_FOCUS_RETRY_MS = 50;
export const requestControllers = new Map();
const requestRevisions = new Map();
const dialogFocusStack = [];
const staleResponse = Object.freeze({ __stale: true });
let currentPasswordResolver = null;
let confirmationResolver = null;
let navigationFocusReturn = null;
let dialogFocusGeneration = 0;
const ui = window.RcloneUI;
const safeStoredValue = ui.storedChoice;

export const coreMethods = {
  async init() {
    this.theme = safeStoredValue('rclone-sync-theme', ['system', 'dark', 'light'], 'system');
    this.density = safeStoredValue('rclone-sync-density', ['comfortable', 'compact'], ui.prefersCompact() ? 'compact' : 'comfortable');
    this.settingsTab = safeStoredValue('rclone-sync-settings-tab', this.settingsTabs, 'general');
    this.pairFilter = safeStoredValue(
      'rclone-sync-pair-filter',
      ['all', 'enabled', 'disabled', 'issues', 'destructive'],
      'all',
    );
    this.jobs.kind = safeStoredValue('rclone-sync-job-kind', ['', 'backup', 'check', 'quicksync', 'restoretest', 'pbs', 'bitrotscrub', 'bootdrill'], '');
    this.jobs.status = safeStoredValue(
      'rclone-sync-job-status',
      ['', 'running', 'ok', 'error', 'warning', 'skipped', 'cancelled', 'stale'],
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
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        this.openCommandPalette();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (this.settingsTab === 'filters' && this.filterFile.dirty) this.saveFilterFile();
        else if (this.configDirty) this.saveConfig();
      }
      if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
        event.preventDefault();
        const target = this.page === 'runs' ? document.getElementById('job-search') : document.getElementById('pair-search');
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
    if (this.page === 'dashboard') { this.loadObjectLock(); await this.loadStorage(false); await this.loadCopies(); }
    else this.loadPage(this.page, true);
    this.startPolling();
  },

  async refreshAll(showToast = false) {
    if (this.refreshing) return false;
    this.refreshing = true;
    try {
      const tasks = [this.refreshStatus(true), this.loadLockdown()];
      // Die teure Übersicht ist nur auf dem Dashboard sichtbar. Andere
      // Bereiche aktualisieren ausschließlich die dort benötigten Daten.
      if (this.page === 'dashboard') {
        tasks.push(this.loadOverview(true));
        tasks.push(this.loadRecent(true), this.loadSchedulerState(true), this.loadCopies());
        // Insights nicht abwarten: ein langsamer PBS-Host soll die Aktualisierung nicht blockieren.
        if (this.config.pbs?.enabled) { tasks.push(this.loadPbsStatus(true)); this.loadPbsInsights(); }
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
          if (data && !data.error) {
            this.progress = data;
            if (data.current_status && typeof data.current_status === 'object') {
              this.status = { ...this.status, ...data.current_status };
            }
            if (typeof data.lockdown_active === 'boolean') {
              this.lockdownState.active = data.lockdown_active;
            }
          }
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
    this.polling.generation += 1;
    if (this.polling.refreshTimer) window.clearTimeout(this.polling.refreshTimer);
    if (this.polling.activityTimer) window.clearTimeout(this.polling.activityTimer);
    this.polling.refreshTimer = null;
    this.polling.activityTimer = null;
    this.stopSseProgress();
  },

  startPolling() {
    if (this.polling.active || document.hidden) return;
    this.polling.active = true;
    this.polling.generation += 1;
    const generation = this.polling.generation;
    const isCurrent = () => this.polling.active && this.polling.generation === generation;
    this.startSseProgress();
    const scheduleRefresh = () => {
      if (!isCurrent()) return;
      this.polling.refreshTimer = window.setTimeout(refreshLoop, 30000);
    };
    const scheduleActivity = () => {
      if (!isCurrent()) return;
      const busy = this.rcloneBusy() || this.progress?.running
        || this.pbs.status?.running
        || (this.jobModal.show && this.jobModal.autoRefresh && this.jobModal.job?.status === 'running');
      this.polling.activityTimer = window.setTimeout(activityLoop, busy ? 2000 : 10000);
    };
    const refreshLoop = async () => {
      try {
        if (isCurrent() && !document.hidden) await this.refreshAll(false);
      } finally {
        scheduleRefresh();
      }
    };
    const activityLoop = async () => {
      try {
        if (isCurrent() && !document.hidden) {
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

  async navigate(next, updateHash = true) {
    if (!this.pages.includes(next)) return;
    if ((this.configDirty || this.filterFile.dirty) && ['pairs', 'definitions', 'settings'].includes(this.page) && next !== this.page) {
      const confirmed = await this.requestConfirmation(
        'Es gibt ungespeicherte Änderungen. Seite trotzdem wechseln?',
        { title: 'Ungespeicherte Änderungen', confirmLabel: 'Seite wechseln' },
      );
      if (!confirmed) return;
    }
    this.page = next;
    this.closeNavigation(false);
    if (updateHash && window.location.hash !== `#${next}`) history.pushState(null, '', `#${next}`);
    this.loadPage(next);
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    this.announce(`${this.pageTitle()} geöffnet`);
    this.$nextTick(() => document.getElementById('main-content')?.focus({ preventScroll: true }));
  },

  async loadPage(page, configAlreadyLoaded = false) {
    if (page === 'dashboard') {
      this.loadOverview(true); this.loadRecent(true); this.loadStorage(false);
      if (!configAlreadyLoaded && !this.configLoaded) this.loadConfig(true);
      if (this.config?.pbs?.enabled) { this.loadPbsStatus(); this.loadPbsInsights(); }
      if (!this.objectLock) this.loadObjectLock();
    } else if (page === 'pairs') {
      if (!configAlreadyLoaded) this.loadConfig();
    } else if (page === 'definitions') {
      if (!configAlreadyLoaded) this.loadConfig();
      this.loadJobDefinitions(true);
      this.loadBandwidth();
    } else if (page === 'runs') {
      this.loadJobs(true);
    } else if (page === 'doctor') {
      if (!configAlreadyLoaded) this.loadConfig();
      this.loadSystemBundle();
      this.loadDoctor(); this.loadVpn(); this.loadCloudQuota();
    } else if (page === 'recovery') {
      if (!configAlreadyLoaded) await this.loadConfig();
      this.loadRecovery();
    } else if (page === 'settings') {
      if (!configAlreadyLoaded) this.loadConfig(); this.loadFilterFile(); this.loadSchedulerState(true);
    }
  },

  pageTitle() {
    return ({ dashboard: 'Lagebild', pairs: 'Datenwege', definitions: 'Jobs', runs: 'Läufe', doctor: 'System', settings: 'Einstellungen', recovery: 'Wiederherstellung' })[this.page] || 'Sicherpfad';
  },

  openCommandPalette() {
    if (this.commandPalette.show) return;
    dialogFocusStack.push(document.activeElement);
    this.commandPalette = { show: true, query: '', selectedIndex: 0 };
    this.$nextTick(() => document.getElementById('command-palette-input')?.focus());
  },

  closeCommandPalette() {
    if (!this.commandPalette.show) return;
    this.commandPalette.show = false;
    this.commandPalette.query = '';
    this.commandPalette.selectedIndex = 0;
    this.restoreDialogFocus();
  },

  moveCommandSelection(delta) {
    const items = this.commandItems();
    if (!items.length) {
      this.commandPalette.selectedIndex = 0;
      return;
    }
    const current = Number(this.commandPalette.selectedIndex) || 0;
    this.commandPalette.selectedIndex = (current + delta + items.length) % items.length;
  },

  runSelectedCommand() {
    const items = this.commandItems();
    const index = Math.min(Math.max(0, Number(this.commandPalette.selectedIndex) || 0), Math.max(0, items.length - 1));
    this.runCommand(items[index]);
  },

  commandItems() {
    const query = this.commandPalette.query.trim().toLowerCase();
    const items = [
      { label: 'Gehe zu Lagebild', keywords: 'dashboard status', run: () => this.navigate('dashboard') },
      { label: 'Gehe zu Datenwegen', keywords: 'pairs datenwege', run: () => this.navigate('pairs') },
      { label: 'Gehe zu Jobs', keywords: 'jobs plan', run: () => this.navigate('definitions') },
      { label: 'Gehe zu Läufe', keywords: 'runs verlauf', run: () => this.navigate('runs') },
      { label: 'Gehe zu System', keywords: 'system diagnose', run: () => this.navigate('doctor') },
      { label: 'Gehe zu Wiederherstellung', keywords: 'recovery restore zeitreise staging', run: () => this.navigate('recovery') },
      { label: 'Gehe zu Einstellungen', keywords: 'settings konfiguration filter pbs', run: () => this.navigate('settings') },
      { label: 'Datenweg-Assistent starten', keywords: 'wizard assistent neuer datenweg', run: () => { this.navigate('pairs'); this.openPairWizard(); } },
      { label: 'Quick-Sync öffnen', keywords: 'quick sync kopieren', run: () => this.openQuick() },
      { label: 'Dry-Run starten', keywords: 'trockenlauf backup', run: () => this.runBackup(true) },
      { label: 'Notfall-Lockdown öffnen', keywords: 'lockdown notfall ransomware sperre', run: () => this.openLockdown() },
      { label: 'Support-Bundle herunterladen', keywords: 'support diagnose export', run: () => this.downloadSupportBundle() },
    ];
    const matches = this.config.backup?.pairs || [];
    for (const pair of matches) {
      if (pair?.name) items.push({ label: `Datenweg: ${pair.name}`, keywords: `${pair.name} ${pair.local || ''} ${pair.remote || ''}`, run: () => { this.navigate('pairs'); this.pairSearch = pair.name; } });
    }
    for (const job of (this.config.backup?.jobs || [])) {
      if (job?.name && job?.id) {
        items.push({
          label: `Job (Dry-Run): ${job.name}`,
          keywords: `job dry-run ${job.name}`,
          run: () => this.runJobDefinition(job, true),
        });
      }
    }
    for (const run of (this.recentJobs || [])) {
      if (run?.id && run?.status === 'error') {
        items.push({
          label: `Fehlerlauf öffnen: #${run.id}`,
          keywords: `fehler error run lauf ${run.id}`,
          run: () => this.showJob(run),
        });
      }
    }
    return query ? items.filter((item) => `${item.label} ${item.keywords}`.toLowerCase().includes(query)) : items;
  },

  runCommand(item) {
    if (!item) return;
    this.closeCommandPalette();
    item.run();
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
      this.status?.backup || this.status?.check || this.status?.quicksync || this.status?.restoretest || this.status?.pbs ||
      this.status?.bitrotscrub || this.status?.bootdrill,
    );
  },

  rcloneBusy() {
    return Boolean(
      this.pending.backup || this.pending.quick ||
      this.status?.backup || this.status?.check || this.status?.quicksync || this.status?.restoretest,
    );
  },

  runningJob() {
    return this.status?.backup || this.status?.check || this.status?.quicksync || this.status?.restoretest || this.status?.pbs ||
      this.status?.bitrotscrub || this.status?.bootdrill || null;
  },

  runningKind() {
    if (this.pending.pbs || this.status?.pbs) return 'PBS-Backup';
    if (this.status?.backup) return 'Backup';
    if (this.status?.check) return 'Check';
    if (this.status?.quicksync) return 'Quick-Sync';
    if (this.status?.restoretest) return 'Restore-Drill';
    if (this.status?.bitrotscrub) return 'Bitrot-Scrub';
    if (this.status?.bootdrill) return 'Boot-Drill';
    if (this.pending.backup) return 'Backup';
    if (this.pending.quick) return 'Quick-Sync';
    return '';
  },

  // Startsperre für alle Lauf-Buttons; busy() bleibt reine Laufanzeige.
  startBlocked() {
    return this.busy() || this.lockdownState.active;
  },

  errorText(result) {
    const detail = result?.detail;
    const text = typeof detail === 'string' ? detail : (detail?.message || 'Aktion fehlgeschlagen');
    return detail?.retry_after_seconds ? `${text} · erneut möglich in ${detail.retry_after_seconds} s` : text;
  },

  // Fragt erneut, solange der Server das Passwort ablehnt (403) oder bremst (429); null bei Abbruch.
  async withCurrentPassword(message, send) {
    for (;;) {
      const password = await this.requestCurrentPassword(message);
      if (!password) return null;
      const result = await send(password);
      if (!result?.__error || ![403, 429].includes(result.status)) return result;
      message = this.errorText(result);
    }
  },

  focusableElements(dialog) {
    if (!dialog) return [];
    return [...dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
      'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((element) => !element.hidden && element.offsetParent !== null);
  },

  openDialog(refName) {
    const generation = ++dialogFocusGeneration;
    dialogFocusStack.push(document.activeElement);
    this.$nextTick(() => {
      this.focusOpenedDialog(refName);
      window.setTimeout(() => {
        if (generation !== dialogFocusGeneration) return;
        this.focusOpenedDialog(refName);
      }, DIALOG_FOCUS_RETRY_MS);
    });
  },

  focusOpenedDialog(refName) {
    const dialog = this.$refs[refName];
    if (!this.visibleElement(dialog)) return false;
    const initialTarget = dialog.querySelector('[data-dialog-initial-focus]');
    if (initialTarget && !this.visibleElement(initialTarget)) return false;
    const target = initialTarget || this.focusableElements(dialog)[0] || dialog;
    target.focus({ preventScroll: true });
    return dialog.contains(document.activeElement);
  },

  enforceInitialDialogFocus(refName) {
    const dialog = this.$refs[refName];
    if (!this.visibleElement(dialog)) return false;
    const initialTarget = dialog.querySelector('[data-dialog-initial-focus]');
    if (!this.visibleElement(initialTarget)) return false;
    const focusIsOutside = !dialog.contains(document.activeElement);
    const focusIsNotInitial = document.activeElement !== initialTarget;
    if (focusIsOutside || focusIsNotInitial) initialTarget.focus({ preventScroll: true });
    return document.activeElement === initialTarget;
  },

  enforceActiveDialogFocus(event) {
    const dialog = this.activeDialog();
    if (!this.visibleElement(dialog)) return false;
    const eventTarget = event?.target;
    if (eventTarget?.nodeType && dialog.contains(eventTarget)) return true;
    const initialTarget = dialog.querySelector('[data-dialog-initial-focus]');
    const target = this.visibleElement(initialTarget)
      ? initialTarget
      : (this.focusableElements(dialog)[0] || dialog);
    if (!this.visibleElement(target)) return false;
    target.focus({ preventScroll: true });
    return dialog.contains(document.activeElement);
  },

  visibleElement(element) {
    if (!element?.isConnected || element.getClientRects().length === 0) return false;
    const style = window.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden';
  },

  restoreDialogFocus() {
    dialogFocusGeneration += 1;
    const target = dialogFocusStack.pop();
    this.$nextTick(() => {
      const focusTarget = target?.isConnected ? target : document.getElementById('main-content');
      focusTarget?.focus();
    });
  },

  activeDialog() {
    if (this.remoteSetup.show) return this.$refs.remoteSetupDialog;
    if (this.commandPalette.show) return this.$refs.commandPaletteDialog;
    if (this.confirmationDialog.show) return this.$refs.confirmationDialog;
    if (this.currentPasswordDialog.show) return this.$refs.currentPasswordDialog;
    if (this.lockdownDialog.show) return this.$refs.lockdownDialog;
    if (this.preflight.show) return this.$refs.preflightDialog;
    if (this.conflictDialog.show) return this.$refs.conflictDialog;
    if (this.diffDialog.show) return this.$refs.diffDialog;
    if (this.crypt.show) return this.$refs.cryptDialog;
    if (this.jobModal.show) return this.$refs.jobDialog;
    if (this.picker.show) return this.$refs.pickerDialog;
    if (this.quickModal.show) return this.$refs.quickDialog;
    if (this.plan.data) return this.$refs.planDialog;
    return null;
  },

  requestConfirmation(message, options = {}) {
    if (confirmationResolver) confirmationResolver(false);
    this.confirmationDialog = {
      show: true,
      title: options.title || 'Aktion bestätigen',
      message: String(message || ''),
      confirmLabel: options.confirmLabel || 'Bestätigen',
      tone: options.tone === 'primary' ? 'primary' : 'danger',
    };
    this.openDialog('confirmationDialog');
    return new Promise((resolve) => {
      confirmationResolver = resolve;
    });
  },

  respondConfirmation(confirmed) {
    if (!this.confirmationDialog.show) return;
    const resolve = confirmationResolver;
    confirmationResolver = null;
    this.confirmationDialog = {
      show: false,
      title: 'Aktion bestätigen',
      message: '',
      confirmLabel: 'Bestätigen',
      tone: 'danger',
    };
    this.restoreDialogFocus();
    resolve?.(Boolean(confirmed));
  },

  announce(message) {
    this.liveMessage = '';
    this.$nextTick(() => { this.liveMessage = String(message || ''); });
  },

  navigationIsModal() {
    return this.navOpen && Boolean(window.matchMedia?.('(max-width: 900px)').matches);
  },

  openNavigation() {
    if (this.navOpen) return;
    navigationFocusReturn = document.activeElement;
    this.navOpen = true;
    this.$nextTick(() => this.focusableElements(this.$refs.mainSidebar)[0]?.focus());
  },

  closeNavigation(restoreFocus = true) {
    if (!this.navOpen) return;
    this.navOpen = false;
    const target = navigationFocusReturn;
    navigationFocusReturn = null;
    if (restoreFocus) this.$nextTick(() => target?.isConnected && target.focus());
  },

  toggleNavigation() {
    if (this.navOpen) this.closeNavigation();
    else this.openNavigation();
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

  _b64urlToBytes(value) {
    const base64 = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
    const pad = base64.length % 4 ? '='.repeat(4 - (base64.length % 4)) : '';
    const binary = window.atob(base64 + pad);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  },

  _bytesToB64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.byteLength; i += 1) binary += String.fromCharCode(bytes[i]);
    return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  },

  async reauthWithWebAuthn() {
    if (!window.PublicKeyCredential || !navigator.credentials?.get) {
      this.currentPasswordDialog.error = 'WebAuthn wird in diesem Browser nicht unterstützt.';
      return;
    }
    this.currentPasswordDialog.error = '';
    const options = await this.api('POST', '/api/webauthn/reauth/options', {}, { silent: true, captureError: true });
    if (!options || options.__error) {
      this.currentPasswordDialog.error = this.errorText(options);
      return;
    }
    try {
      const publicKey = {
        ...options,
        challenge: this._b64urlToBytes(options.challenge),
        allowCredentials: (options.allowCredentials || []).map((cred) => ({
          ...cred,
          id: this._b64urlToBytes(cred.id),
        })),
      };
      const assertion = await navigator.credentials.get({ publicKey });
      if (!assertion) return;
      const credential = {
        id: assertion.id,
        rawId: this._bytesToB64url(assertion.rawId),
        type: assertion.type,
        response: {
          authenticatorData: this._bytesToB64url(assertion.response.authenticatorData),
          clientDataJSON: this._bytesToB64url(assertion.response.clientDataJSON),
          signature: this._bytesToB64url(assertion.response.signature),
          userHandle: assertion.response.userHandle ? this._bytesToB64url(assertion.response.userHandle) : null,
        },
      };
      const verified = await this.api('POST', '/api/webauthn/reauth/verify', { credential }, { silent: true, captureError: true });
      if (!verified || verified.__error || !verified.reauth_token) {
        this.currentPasswordDialog.error = this.errorText(verified);
        return;
      }
      const resolve = currentPasswordResolver;
      currentPasswordResolver = null;
      this.currentPasswordDialog.show = false;
      this.currentPasswordDialog.password = '';
      this.restoreDialogFocus();
      resolve?.(verified.reauth_token);
    } catch (err) {
      this.currentPasswordDialog.error = err?.message || 'Passkey-Bestätigung abgebrochen.';
    }
  },

  trapDialogFocus(event) {
    const container = this.activeDialog() || (this.navigationIsModal() ? this.$refs.mainSidebar : null);
    if (!container) return;
    const focusable = this.focusableElements(container);
    if (!focusable.length) {
      event.preventDefault();
      container.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!container.contains(document.activeElement)) {
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

  closeOverlays() {
    if (this.remoteSetup.show) this.closeRemoteSetup();
    else if (this.confirmationDialog.show) this.respondConfirmation(false);
    else if (this.currentPasswordDialog.show) this.cancelCurrentPassword();
    else if (this.lockdownDialog.show) this.closeLockdown();
    else if (this.preflight.show) this.closePreflight();
    else if (this.conflictDialog.show) this.closeConflicts();
    else if (this.diffDialog.show) this.closeDiff();
    else if (this.crypt.show) this.closeCrypt();
    else if (this.jobModal.show) this.closeJob();
    else if (this.picker.show) this.closePicker();
    else if (this.quickModal.show) this.closeQuick();
    else if (this.plan.data) this.closePlan();
    else this.closeNavigation();
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

  metricAvailable(value) {
    return value !== null && value !== undefined && Number.isFinite(Number(value));
  },

  metricPercent(value) {
    return this.metricAvailable(value) ? `${Math.round(Number(value))}%` : 'Nicht verfügbar';
  },

  metricWidth(value) {
    return this.metricAvailable(value) ? Math.min(100, Math.max(0, Number(value))) : 0;
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
    return ({ running: 'Läuft', ok: 'Erfolgreich', error: 'Fehler', warning: 'Mit Hinweis', skipped: 'Übersprungen', cancelled: 'Abgebrochen', stale: 'Verwaist', pending: 'Ausstehend', done: 'Fertig', disabled: 'Deaktiviert', warn: 'Prüfen' })[status] || status || 'Unbekannt';
  },

  kindLabel(kind) {
    return ({ backup: 'Backup', check: 'Check', quicksync: 'Quick-Sync', pbs: 'PBS-Backup', restoretest: 'Restore-Drill', bitrotscrub: 'Bitrot-Scrub', bootdrill: 'Boot-Drill' })[kind] || kind || 'Job';
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
    if (summary.kind === 'restoretest' && summary.outcome === 'partial') return 'Prüfumfang begrenzt · Details im Lauf';
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
