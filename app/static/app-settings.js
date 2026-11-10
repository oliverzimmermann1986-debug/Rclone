// Einstellungen: Konfiguration laden/prüfen/speichern, Zeitplan-Editor, Filterdatei, Konto.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.

// Gleiche Defaults wie config_validation.py, damit Bindungen vor dem Laden greifen.
export const lifecycleDefaults = { enabled: false, keep_daily: 7, keep_weekly: 4, keep_monthly: 12 };
export const scrubDefaults = { enabled: false, schedule: '0 4 * * *', sample_percent: 2, max_files: 200, max_total_mb: 256, max_scan_files: 20000, rotation_days: 30 };
export const gcThrottleDefaults = { enabled: true, poll_seconds: 60 };
export const bootDrillDefaults = { enabled: false, schedule: 'manual', group: '', archive: '', memory_mb: 1024, timeout_seconds: 60, scratch_dir: '' };
export const ransomwareDefaults = { enabled: true, max_suspect_files: 1, auto_lockdown: false };
export const transferBudgetDefaults = { enabled: false, monthly_egress_gb: 0, monthly_upload_gb: 0, warn_percent: 80, throttle_optional_jobs: true };
export const bootstrapExportDefaults = { enabled: false, target_dir: '' };

export const settingsMethods = {
  async loadConfig(silent = false) {
    const editGeneration = this.configEditGeneration;
    this.configLoading = true;
    this.configError = '';
    const result = await this.api('GET', '/api/config', undefined, { silent, requestKey: 'config' });
    if (this.isStale(result)) return null;
    if (!result) {
      this.configLoading = false;
      this.configError = 'Konfiguration konnte nicht geladen werden. Bearbeitung bleibt gesperrt.';
      return false;
    }
    if (this.configDirty && editGeneration !== this.configEditGeneration) {
      this.configLoading = false;
      return null;
    }
    result.web ||= {};
    result.paths ||= {};
    result.web.allowed_hosts ||= ['localhost', '127.0.0.1', '::1'];
    result.web.local_browse_roots ||= ['/mnt', '/media', '/srv', '/opt/rclone-sync/data'];
    result.web.secure_cookie ??= 'auto';
    result.web.session_max_age_seconds ??= 604800;
    result.web.hsts_seconds ??= 0;
    result.backup ||= {};
    result.backup.pairs ||= [];
    result.backup.jobs ||= [];
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
    result.backup.max_parallel ??= 2;
    result.backup.timeout_hours ??= 4;
    result.backup.max_runtime_hours ??= 0;
    result.backup.scheduler_grace_minutes ??= 15;
    result.backup.scheduler_retry_minutes ??= 60;
    result.notifications ||= {};
    delete result.notifications.webhooks;
    delete result.notifications.allow_http;
    delete result.notifications.allow_private_targets;
    delete result.notifications.timeout_seconds;
    delete result.notifications.max_parallel;
    result.maintenance ||= { auto_prune: true, job_retention_days: 180, keep_latest_jobs: 500, log_retention_days: 90 };
    result.maintenance.bootstrap_export = { ...bootstrapExportDefaults, ...result.maintenance.bootstrap_export };
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
    result.pbs.gc_throttle = { ...gcThrottleDefaults, ...result.pbs.gc_throttle };
    result.pbs.boot_drill = { ...bootDrillDefaults, ...result.pbs.boot_drill };
    result.backup.versions_lifecycle = { ...lifecycleDefaults, ...result.backup.versions_lifecycle };
    result.backup.bitrot_scrub = { ...scrubDefaults, ...result.backup.bitrot_scrub };
    result.backup.ransomware_guard = { ...ransomwareDefaults, ...result.backup.ransomware_guard };
    result.backup.transfer_budget = { ...transferBudgetDefaults, ...result.backup.transfer_budget };
    result.pbs.targets ||= [];
    for (const target of result.pbs.targets) {
      target.pathsText = (target.paths || []).join('\n');
      target.schedule ||= 'manual';
    }
    for (const pair of result.backup.pairs) this.normalizePair(pair);
    for (const job of result.backup.jobs) this.normalizeJobDefinition(job);
    this.config = result;
    this.rcloneArgsText = (result.backup.rclone_args || []).join('\n');
    this.allowedHostsText = (result.web.allowed_hosts || []).join('\n');
    this.browseRootsText = (result.web.local_browse_roots || []).join('\n');
    this.vpnProbesText = (result.backup.vpn_probes || []).join('\n');
    this.syncScheduleEditorFromCron();
    this.performancePreset = this.detectPerformancePreset();
    this.configDirty = false;
    this.configDiffOpen = false;
    this.configLoaded = true;
    this.configLoading = false;
    this.configError = '';
    this.savedConfigSnapshot = JSON.stringify(this.configPayload());
    this.loadAvailableHooks?.();
    this.configValidation = { loading: false, ok: null, warnings: [], errors: [], revisionMatches: true };
    if (this.page === 'settings' && this.settingsTab === 'scheduler') this.refreshSchedulePreview();
    return true;
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
    draft.backup.vpn_probes = this.vpnProbesText.split('\n').map((value) => value.trim()).filter(Boolean);
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
      delete pair.source; delete pair.target; delete pair.two_way; delete pair.schedule;
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
    const savedGeneration = this.configEditGeneration;
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
      const editedDuringSave = this.configEditGeneration !== savedGeneration;
      if (editedDuringSave) {
        if (result.config?._revision) this.config._revision = result.config._revision;
        this.configDirty = true;
        this.showToast('Gespeichert · neuere Änderungen sind noch ungespeichert', 'warn');
      } else {
        this.config = result.config || this.config;
        this.configDirty = false;
        if (result.warnings?.length) this.showToast(`Gespeichert: ${result.warnings.join(' · ')}`, 'warn');
        else this.showToast('Einstellungen gespeichert');
        await this.loadConfig();
      }
      this.loadOverview(true);
      return true;
    } finally {
      this.pending.save = false;
    }
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

  applyFilterPreset(name) {
    const presets = {
      os: ['- .DS_Store', '- Thumbs.db', '- desktop.ini', '- ._*'],
      dev: ['- node_modules/**', '- __pycache__/**', '- .venv/**', '- .git/**'],
      temp: ['- *.tmp', '- *.swp', '- ~$*'],
    };
    const incoming = presets[name];
    if (!incoming) return;
    const lines = String(this.filterFile.content || '').split(/\r?\n/);
    const existing = new Set(lines.map((line) => line.trim()).filter(Boolean));
    const additions = incoming.filter((line) => !existing.has(line));
    if (!additions.length) {
      this.showToast('Dieses Filter-Preset ist bereits enthalten.', 'warn');
      return;
    }
    const prefix = this.filterFile.content && !this.filterFile.content.endsWith('\n') ? '\n' : '';
    this.filterFile.content = `${this.filterFile.content || ''}${prefix}${additions.join('\n')}\n`;
    this.filterFile.dirty = true;
    this.showToast(`${additions.length} Filterregel(n) ergänzt`);
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
    if ((this.configDirty || this.filterFile.dirty) && !(await this.requestConfirmation(
      'Ungespeicherte Änderungen verwerfen und abmelden?',
      { title: 'Abmelden', confirmLabel: 'Verwerfen und abmelden' },
    ))) return;
    const result = await this.api('POST', '/logout');
    if (result !== null) window.location = '/login';
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
    this.configEditGeneration += 1;
    if (this.configValidation.ok !== null) this.configValidation.ok = null;
  },

  toggleConfigDiff() {
    this.configDiffOpen = !this.configDiffOpen;
  },

  configDiffEntries() {
    if (!this.savedConfigSnapshot || !this.configLoaded) return [];
    let beforeObj;
    try {
      beforeObj = JSON.parse(this.savedConfigSnapshot);
    } catch (_) {
      return [];
    }
    const afterObj = this.configPayload();
    const entries = [];
    const compareFlat = (section, left = {}, right = {}) => {
      const keys = new Set([...Object.keys(left || {}), ...Object.keys(right || {})]);
      for (const key of keys) {
        if (key === '_revision') continue;
        const lStr = JSON.stringify(left?.[key]);
        const rStr = JSON.stringify(right?.[key]);
        if (lStr !== rStr) {
          entries.push({
            path: `${section}.${key}`,
            before: typeof left?.[key] === 'object' ? JSON.stringify(left?.[key]) : String(left?.[key] ?? '—'),
            after: typeof right?.[key] === 'object' ? JSON.stringify(right?.[key]) : String(right?.[key] ?? '—'),
          });
        }
      }
    };
    compareFlat('web', beforeObj.web, afterObj.web);
    compareFlat('paths', beforeObj.paths, afterObj.paths);
    compareFlat('maintenance', beforeObj.maintenance, afterObj.maintenance);
    compareFlat('pbs', beforeObj.pbs, afterObj.pbs);
    const bBefore = { ...(beforeObj.backup || {}) };
    const bAfter = { ...(afterObj.backup || {}) };
    const pairsBefore = bBefore.pairs || [];
    const pairsAfter = bAfter.pairs || [];
    const jobsBefore = bBefore.jobs || [];
    const jobsAfter = bAfter.jobs || [];
    delete bBefore.pairs;
    delete bAfter.pairs;
    delete bBefore.jobs;
    delete bAfter.jobs;
    compareFlat('backup', bBefore, bAfter);
    const maxPairs = Math.max(pairsBefore.length, pairsAfter.length);
    for (let i = 0; i < maxPairs; i += 1) {
      const pLeft = pairsBefore[i];
      const pRight = pairsAfter[i];
      if (JSON.stringify(pLeft) !== JSON.stringify(pRight)) {
        const label = pRight?.name || pLeft?.name || `#${i + 1}`;
        entries.push({
          path: `Datenweg „${label}“`,
          before: pLeft ? `${pLeft.direction || ''} · ${pLeft.mode || ''} · ${pLeft.enabled ? 'aktiv' : 'aus'}` : '(neu)',
          after: pRight ? `${pRight.direction || ''} · ${pRight.mode || ''} · ${pRight.enabled ? 'aktiv' : 'aus'}` : '(entfernt)',
        });
      }
    }
    const maxJobs = Math.max(jobsBefore.length, jobsAfter.length);
    for (let i = 0; i < maxJobs; i += 1) {
      const jLeft = jobsBefore[i];
      const jRight = jobsAfter[i];
      if (JSON.stringify(jLeft) !== JSON.stringify(jRight)) {
        const label = jRight?.name || jLeft?.name || `#${i + 1}`;
        entries.push({
          path: `Jobdefinition „${label}“`,
          before: jLeft ? `${jLeft.schedule || 'manual'} · ${jLeft.enabled ? 'aktiv' : 'aus'}` : '(neu)',
          after: jRight ? `${jRight.schedule || 'manual'} · ${jRight.enabled ? 'aktiv' : 'aus'}` : '(entfernt)',
        });
      }
    }
    return entries;
  },

  async discardConfigChanges() {
    if (!this.configDirty) return;
    if (!(await this.requestConfirmation(
      'Alle ungespeicherten Konfigurationsänderungen verwerfen und den gespeicherten Stand neu laden?',
      { title: 'Änderungen verwerfen', confirmLabel: 'Verwerfen' },
    ))) return;
    this.configDirty = false;
    this.configDiffOpen = false;
    await this.loadConfig();
    this.showToast('Ungespeicherte Änderungen verworfen');
  },

  async runFilterTest() {
    const paths = String(this.filterTester.pathsText || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    if (!paths.length) {
      this.showToast('Bitte mindestens einen Beispielpfad eingeben.', 'warn');
      return;
    }
    this.filterTester.loading = true;
    const result = await this.api(
      'POST',
      '/api/config/filter-test',
      { rules: this.filterFile.content || '', paths },
      { silent: true, captureError: true, requestKey: 'filter-test' },
    );
    this.filterTester.loading = false;
    if (this.isStale(result)) return;
    if (!result || result.__error) {
      this.showToast(this.errorText(result), 'err');
      return;
    }
    this.filterTester.results = result.results || [];
    this.filterTester.parseErrors = result.parse_errors || [];
  },
};
