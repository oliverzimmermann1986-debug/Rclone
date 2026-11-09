// Jobs: Filter, Scheduler-Pause, Definitionen, Bandbreite, Läufe, Job-Detail, Quick-Sync, Preflight.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.
import { requestControllers } from './app-core.js';

export const emptyQuick = () => ({
  remote: '', local: '', direction: 'bisync', mode: 'bisync', dry_run: true,
  allow_delete: false, max_delete: 100, new_target: false,
});

export const jobsMethods = {
  setJobFilter(kind, status) {
    this.jobs.kind = kind;
    this.jobs.status = status;
    this.loadJobs(true);
  },

  canCancelCurrentJob() {
    return Boolean(this.rcloneBusy() || this.progress?.running || this.pbs.status?.running);
  },

  cancelCurrentJob() {
    if (this.pbs.status?.running && !this.rcloneBusy()) return this.cancelPbs();
    return this.cancelBackup();
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
      this.jobs.error = 'Läufe konnten nicht geladen werden.';
    }
    this.jobs.loading = false;
  },

  normalizeJobDefinition(job) {
    if (job.enabled === undefined) job.enabled = true;
    job.name ||= '';
    job.data_path_ids ||= [];
    job.schedule ||= 'manual';
    job.execution_mode = job.execution_mode === 'parallel' ? 'parallel' : 'sequential';
    job.max_parallel = job.execution_mode === 'sequential'
      ? 1
      : Math.max(1, Number(job.max_parallel) || 2);
    job.retry_minutes = Math.max(1, Number(job.retry_minutes) || 60);
    return job;
  },

  async loadJobDefinitions(silent = false) {
    this.jobDefinitions = { loading: true, error: '' };
    const result = await this.api('GET', '/api/jobs/definitions', undefined, {
      silent,
      requestKey: 'job-definitions',
    });
    if (this.isStale(result)) return;
    if (!result) {
      this.jobDefinitions = { loading: false, error: 'Jobdefinitionen konnten nicht geladen werden.' };
      return;
    }
    if (!this.configDirty) {
      this.config.backup ||= {};
      this.config.backup.jobs = result.map((job) => this.normalizeJobDefinition({ ...job }));
    }
    this.jobDefinitions = { loading: false, error: '' };
    this.loadJobScheduleTimeline();
  },

  async loadJobScheduleTimeline() {
    const jobs = (this.config.backup?.jobs || []).filter((job) => job.enabled && job.schedule && job.schedule !== 'manual');
    this.jobTimeline.loading = true;
    const timezone = String(this.config.backup?.timezone || 'Europe/Berlin');
    const now = Date.now() / 1000;
    const horizon = now + 24 * 3600;
    const [durationsResp, ...previews] = await Promise.all([
      this.api('GET', '/api/jobs/definitions/durations', undefined, { silent: true, requestKey: 'job-durations' }),
      ...jobs.map(async (job) => {
        const result = await this.api('POST', '/api/config/schedule-preview', { expression: job.schedule, timezone, count: 10 }, { silent: true, captureError: true, requestKey: `timeline-${job.id || job.name}` });
        if (this.isStale(result) || !result || result.__error) return { job, runs: [], error: result?.detail?.message || result?.detail || '' };
        return { job, runs: (result.next_runs || []).filter((run) => Number(run.timestamp) >= now && Number(run.timestamp) < horizon) };
      }),
    ]);
    const durationsMap = (!this.isStale(durationsResp) && durationsResp?.durations) ? durationsResp.durations : {};
    const rows = previews.map(({ job, runs, error }) => {
      const durInfo = durationsMap[job.id] || {};
      const avgDurationSec = Number(durInfo.avg_duration_seconds) || 0;
      const widthPct = avgDurationSec > 0
        ? Math.max(1.2, Math.min(35, (avgDurationSec / (24 * 3600)) * 100))
        : 1.2;
      return {
        id: job.id || job.name,
        name: job.name || 'Unbenannter Job',
        avgDurationSec,
        error: String(error || ''),
        runs: runs.map((run) => ({
          ...run,
          minute: new Date(Number(run.timestamp) * 1000).getMinutes(),
          hour: new Date(Number(run.timestamp) * 1000).getHours(),
          position: Math.max(0, Math.min(100, (Number(run.timestamp) - now) / (24 * 3600) * 100)),
          widthPercent: widthPct,
          avgDurationSec,
        })),
      };
    });
    const groups = new Map();
    for (const row of rows) for (const run of row.runs) {
      const key = new Date(Number(run.timestamp) * 1000).toISOString().slice(0, 16);
      groups.set(key, [...(groups.get(key) || []), row.name]);
    }
    this.jobTimeline = {
      loading: false,
      rows,
      conflicts: [...groups.entries()].filter(([, names]) => names.length > 1).map(([at, names]) => ({ at, names })),
    };
  },

  timelineHourPosition(hour) { return hour / 24 * 100; },

  async loadBandwidth() {
    this.bandwidth.loading = true;
    const [heatmap, suggestions] = await Promise.all([
      this.api('GET', `/api/jobs/bandwidth/heatmap?days=${this.bandwidth.days}`, undefined, { silent: true, requestKey: 'bandwidth-heatmap' }),
      this.api('GET', '/api/jobs/bandwidth/suggestions', undefined, { silent: true, requestKey: 'bandwidth-suggestions' }),
    ]);
    if (this.isStale(heatmap)) return;
    this.bandwidth.error = !heatmap;
    if (heatmap) {
      this.bandwidth.heatmap = heatmap;
      this.bandwidth.max = Math.max(0, ...heatmap.cells.flat().filter((value) => value !== null));
    }
    if (suggestions && !this.isStale(suggestions)) this.bandwidth.suggestions = suggestions;
    this.bandwidth.loading = false;
  },

  heatTitle(day, hour) {
    const value = this.bandwidth.heatmap?.cells?.[day]?.[hour];
    const samples = this.bandwidth.heatmap?.samples?.[day]?.[hour] || 0;
    const slot = `${['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'][day]}, ${String(hour).padStart(2, '0')}:00 Uhr`;
    if (value === null || value === undefined) return `${slot}: keine Messung`;
    return `${slot}: ${this.formatBytes(value)}/s · ${samples} ${samples === 1 ? 'Messung' : 'Messungen'}`;
  },

  suggestionApplied(item) {
    return (this.config.backup?.jobs || []).find((job) => job.id === item.definition_id)?.schedule === item.proposed_schedule;
  },

  applyScheduleSuggestion(item) {
    const job = (this.config.backup?.jobs || []).find((entry) => entry.id === item.definition_id);
    if (!job) { this.showToast('Job nicht mehr vorhanden – Vorschläge aktualisieren', 'warn'); return; }
    job.schedule = item.proposed_schedule;
    this.markConfigDirty();
    this.loadJobScheduleTimeline();
    this.showToast('Zeitplan übernommen – noch speichern');
  },

  addJobDefinition() {
    this.config.backup ||= {};
    this.config.backup.jobs ||= [];
    this.config.backup.jobs.push(this.normalizeJobDefinition({
      name: '', enabled: false, data_path_ids: [], schedule: 'manual',
      execution_mode: 'sequential', max_parallel: 1, retry_minutes: 60,
    }));
    this.markConfigDirty();
  },

  async removeJobDefinition(idx) {
    const job = this.config.backup.jobs[idx];
    const confirmed = await this.requestConfirmation(
      `Job „${job?.name || 'ohne Namen'}“ entfernen? Die bisherigen Läufe bleiben erhalten.`,
      { title: 'Job entfernen', confirmLabel: 'Job entfernen' },
    );
    if (!confirmed) return;
    this.config.backup.jobs.splice(idx, 1);
    delete this.jobPathChoice[idx];
    this.markConfigDirty();
  },

  dataPathName(pathId) {
    const pair = (this.config.backup?.pairs || []).find((item) => item.id === pathId);
    return pair?.name || pathId || 'Unbekannter Datenweg';
  },

  availableDataPaths(job) {
    const assigned = new Set(job?.data_path_ids || []);
    return (this.config.backup?.pairs || []).filter((pair) => pair.id && !assigned.has(pair.id));
  },

  toggleJobPathAssignment(job, pathId, checked) {
    const ids = job.data_path_ids || (job.data_path_ids = []);
    const existing = ids.indexOf(pathId);
    if (checked && existing < 0) ids.push(pathId);
    else if (!checked && existing >= 0) ids.splice(existing, 1);
    this.markConfigDirty();
  },

  addJobPath(job, jobIdx) {
    const pathId = this.jobPathChoice[jobIdx];
    if (!pathId || job.data_path_ids.includes(pathId)) return;
    job.data_path_ids.push(pathId);
    this.jobPathChoice[jobIdx] = '';
    this.markConfigDirty();
  },

  removeJobPath(job, pathIdx) {
    job.data_path_ids.splice(pathIdx, 1);
    this.markConfigDirty();
  },

  moveJobPath(job, pathIdx, delta) {
    const target = pathIdx + delta;
    if (target < 0 || target >= job.data_path_ids.length) return;
    const [pathId] = job.data_path_ids.splice(pathIdx, 1);
    job.data_path_ids.splice(target, 0, pathId);
    this.markConfigDirty();
  },

  async planJobDefinition(job, dryRun = true) {
    if (!job?.id || this.configDirty || this.pending.definition) {
      this.showToast('Job zuerst speichern, dann planen.', 'warn');
      return;
    }
    this.pending.definition = true;
    this.plan = { loading: true, data: null, dry_run: dryRun, definitionId: job.id };
    try {
      const result = await this.api(
        'GET',
        `/api/jobs/definitions/${encodeURIComponent(job.id)}/plan?dry_run=${dryRun}`,
        undefined,
        { requestKey: 'definition-plan' },
      );
      if (!this.isStale(result) && result) {
        this.plan = { loading: false, data: result, dry_run: dryRun, definitionId: job.id };
        this.openDialog('planDialog');
      }
    } finally {
      this.plan.loading = false;
      this.pending.definition = false;
    }
  },

  async runJobDefinition(job, dryRun = true) {
    if (!job?.id || this.configDirty || this.pending.definition) {
      this.showToast('Job zuerst speichern, dann starten.', 'warn');
      return;
    }
    if (!dryRun && !(await this.requestConfirmation(
      `Job „${job.name}“ jetzt produktiv starten?`,
      { title: 'Produktiven Job starten', confirmLabel: 'Produktiv starten' },
    ))) return;
    this.pending.definition = true;
    try {
      const result = await this.api(
        'POST',
        `/api/jobs/definitions/${encodeURIComponent(job.id)}/run?dry_run=${dryRun}`,
      );
      if (result?.ok) {
        this.showToast(dryRun ? `Dry-Run für „${job.name}“ gestartet` : `„${job.name}“ gestartet`);
        setTimeout(() => { this.refreshStatus(true); this.loadJobs(true); }, 400);
      }
    } finally {
      this.pending.definition = false;
    }
  },

  startPlannedDryRun() {
    const definitionId = this.plan.definitionId;
    this.closePlan();
    if (!definitionId) {
      this.runBackup(true);
      return;
    }
    const job = (this.config.backup?.jobs || []).find((item) => item.id === definitionId);
    if (job) this.runJobDefinition(job, true);
    else this.showToast('Die geplante Jobdefinition ist nicht mehr verfügbar.', 'warn');
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
    if (!(await this.requestConfirmation(
      'Die Konfiguration enthält ungespeicherte Änderungen. Vor dem Start speichern?',
      { title: 'Vor dem Start speichern', confirmLabel: 'Speichern und fortfahren', tone: 'primary' },
    ))) return false;
    return await this.saveConfig();
  },

  async runBackup(dryRun, pairs = null) {
    if (this.pending.backup) return;
    if (!(await this.ensureConfigSavedForRun())) return;
    if (!dryRun && !(await this.requestConfirmation(
      `Produktiven Lauf für ${pairs?.join(', ') || 'alle aktiven Pairs'} starten? Prüfe vorher möglichst den Plan oder einen Dry-Run.`,
      { title: 'Produktiven Lauf starten', confirmLabel: 'Produktiv starten' },
    ))) return;
    this.pending.backup = true;
    try {
      const scope = pairs ? `&pairs=${encodeURIComponent(pairs.join(','))}` : '';
      const result = await this.api('POST', `/api/jobs/backup/run?dry_run=${dryRun}${scope}`);
      if (result?.ok) {
        this.showToast(dryRun ? 'Dry-Run gestartet' : 'Backup gestartet');
        setTimeout(() => { this.refreshStatus(true); this.loadProgress(true); }, 400);
      }
    } finally {
      this.pending.backup = false;
    }
  },

  async cancelBackup() {
    if (!(await this.requestConfirmation(
      'Laufenden Job wirklich abbrechen? Bereits übertragene Änderungen bleiben bestehen.',
      { title: 'Lauf abbrechen', confirmLabel: 'Job abbrechen' },
    ))) return;
    const result = await this.api('POST', '/api/jobs/backup/cancel');
    if (result?.ok) this.showToast('Abbruchsignal gesendet');
    else if (result) this.showToast(result.error || 'Kein laufender Job', 'err');
  },

  async runSinglePair(name, dryRun = true) {
    if (!name || this.pending.backup) return;
    if (!(await this.ensureConfigSavedForRun())) return;
    if (!dryRun && !(await this.requestConfirmation(
      `Pair „${name}“ produktiv starten?`,
      { title: 'Datenweg produktiv starten', confirmLabel: 'Produktiv starten' },
    ))) return;
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
    this.plan.definitionId = null;
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
      if (!(await this.requestConfirmation(
        `${this.quick.mode.toUpperCase()} kann bis zu ${this.quick.max_delete} Einträge löschen. Wirklich starten?`,
        { title: 'Löschenden Lauf starten', confirmLabel: 'Trotzdem starten' },
      ))) return;
    }
    const payload = { ...this.quick, min_local_files: this.quick.new_target ? 0 : 1 };
    delete payload.new_target;
    const promotion = { ...this.quick };
    this.pending.quick = true;
    try {
      const result = await this.api('POST', '/api/jobs/backup/quick', payload);
      if (result?.ok) {
        if (result.job_id) this.quickPromotion = { jobId: result.job_id, draft: promotion };
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
    this.jobModal = { show: true, job: job || null, log: '', loading: true, logLoading: true, logSearch: '', logFilter: 'all', autoRefresh: true };
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

  setJobLogFilter(mode) {
    this.jobModal.logFilter = mode || 'all';
  },

  filteredLog() {
    const text = this.jobModal.log || '';
    const mode = this.jobModal.logFilter || 'all';
    const needle = this.jobModal.logSearch.trim().toLowerCase();
    if (!needle && mode === 'all') return text;
    return text.split('\n').filter((line) => {
      if (mode === 'errors' && !/\b(ERROR|FATAL|WARN|WARNING|NOTICE)\b/i.test(line)) return false;
      if (mode === 'transfers' && !/\b(Copied|Deleted|Moved|Renamed|Transferred)\b/i.test(line)) return false;
      if (needle && !line.toLowerCase().includes(needle)) return false;
      return true;
    }).join('\n');
  },

  filteredLogLines() {
    return this.filteredLog().split('\n').map((text) => {
      const match = text.match(/\b(DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|FATAL)\b/i);
      const value = (match?.[1] || '').toUpperCase();
      const level = ['ERROR', 'FATAL'].includes(value) ? 'error'
        : (['WARNING', 'WARN', 'NOTICE'].includes(value) ? 'warn'
          : (value === 'INFO' ? 'info' : (value === 'DEBUG' ? 'debug' : '')));
      return { text, level };
    });
  },

  jumpToLogError() {
    const line = document.querySelector('.live-log-line.error');
    if (line) line.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    else this.showToast('Im geladenen Log wurde kein Fehler gefunden.', 'warn');
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

  canRetryJob(job = this.jobModal.job) {
    return job?.kind === 'backup'
      && ['error', 'cancelled', 'stale'].includes(job?.status)
      && Boolean(job?.definition_id)
      && Boolean(job?.config_revision);
  },

  async retryJob(job = this.jobModal.job) {
    if (!this.canRetryJob(job) || this.pending.retry) return;
    if (!(await this.requestConfirmation(
      'Job erneut starten?\n\nDer Server startet nur dieselbe Jobdefinition, wenn sich die Konfiguration seit diesem Lauf nicht geändert hat. Andernfalls wird der Start sicher abgelehnt.',
      { title: 'Job erneut starten', confirmLabel: 'Job starten', tone: 'primary' },
    ))) return;
    this.pending.retry = true;
    try {
      const result = await this.api('POST', `/api/jobs/${job.id}/retry?dry_run=false`);
      if (!result?.ok) return;
      this.showToast(`Job #${result.job_id} wurde erneut gestartet`);
      this.closeJob();
      await Promise.all([this.refreshStatus(true), this.loadJobs(false), this.loadOverview(true)]);
      if (result.job_id) this.showJob({ id: result.job_id });
    } finally {
      this.pending.retry = false;
    }
  },

  async openPreflight(job) {
    if (!job?.id) return;
    this.preflight = { show: true, loading: true, kind: job.kind, jobId: job.id, data: null, excluded: {}, error: '' };
    this.openDialog('preflightDialog');
    const result = await this.api('GET', `/api/jobs/${job.id}/preflight`, undefined, { silent: true, captureError: true, requestKey: 'preflight', timeoutMs: 60000 });
    if (this.isStale(result) || this.preflight.jobId !== job.id) return;
    this.preflight.loading = false;
    if (!result || result.__error) { this.preflight.error = result ? this.errorText(result) : ''; return; }
    const groups = [['copies', 'Kopie', 'ok'], ['deletes', 'Löschen', 'error'], ['moves', 'Verschieben', 'warn'], ['other', 'Sonstiges', 'neutral']];
    const excluded = {};
    for (const pair of result.pairs || []) {
      pair.rows = groups.flatMap(([key, label, tone]) => (pair.matrix?.[key] || []).map((row) => ({
        path: row.path, key, label, tone,
        detail: key === 'moves' ? `→ ${row.to || '—'}` : (key === 'other' ? row.action : this.formatBytes(row.size_bytes)),
      })));
      excluded[pair.name] = {};
    }
    this.preflight.excluded = excluded;
    this.preflight.data = result;
  },

  preflightExcludes() {
    const excludes = {};
    for (const [name, paths] of Object.entries(this.preflight.excluded)) {
      const selected = Object.keys(paths).filter((path) => paths[path]);
      if (selected.length) excludes[name] = selected;
    }
    return excludes;
  },

  preflightExcludedCount() {
    return Object.values(this.preflightExcludes()).reduce((sum, paths) => sum + paths.length, 0);
  },

  async startPreflightRun() {
    const state = this.preflight;
    const names = (state.data?.pairs || []).map((pair) => pair.name).filter(Boolean);
    if (!names.length || this.pending.backup) return;
    const skipped = this.preflightExcludedCount();
    if (!(await this.requestConfirmation(
      `Produktiven Lauf für ${names.join(', ')} starten?${skipped ? `\n\n${skipped} abgewählte Pfade werden ausgenommen.` : ''}`,
      { title: 'Produktiven Lauf starten', confirmLabel: 'Produktiv starten' },
    ))) return;
    this.pending.backup = true;
    state.error = '';
    try {
      const result = await this.api(
        'POST',
        `/api/jobs/backup/run?dry_run=false&pairs=${encodeURIComponent(names.join(','))}`,
        { exclude_paths: this.preflightExcludes() },
        { silent: true, captureError: true },
      );
      if (!result) return;
      if (result.__error) { state.error = this.errorText(result); return; }
      if (result.ok) {
        this.showToast('Backup gestartet');
        this.closePreflight();
        this.closeJob();
        setTimeout(() => { this.refreshStatus(true); this.loadProgress(true); }, 400);
      }
    } finally {
      this.pending.backup = false;
    }
  },

  closePreflight() {
    if (!this.preflight.show) return;
    requestControllers.get('preflight')?.abort();
    this.preflight = { show: false, loading: false, kind: '', jobId: null, data: null, excluded: {}, error: '' };
    this.restoreDialogFocus();
  },

  async cleanupFailed() {
    if (!(await this.requestConfirmation(
      'Fehlgeschlagene, abgebrochene und verwaiste Jobs aus der Datenbank löschen? Die Logdateien bleiben bestehen.',
      { title: 'Fehlgeschlagene Jobs bereinigen', confirmLabel: 'Jobs löschen' },
    ))) return;
    const result = await this.api('POST', '/api/jobs/cleanup-failed');
    if (result?.ok) {
      this.showToast(`${result.deleted} Jobs gelöscht`);
      this.loadJobs(true); this.loadOverview(true);
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

  closeJob() {
    if (!this.jobModal.show) return;
    this.jobModal.show = false;
    requestControllers.get('job-detail')?.abort();
    requestControllers.get('job-log')?.abort();
    this.restoreDialogFocus();
  },
};
