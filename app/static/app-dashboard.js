// Lagebild: Lockdown, Übersicht, Status, Speicher, Kopien, PBS, Boot-Drill.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.

export const dashboardMethods = {
  async loadLockdown() {
    const result = await this.api('GET', '/api/lockdown', undefined, { silent: true, requestKey: 'lockdown' });
    if (this.isStale(result)) return undefined;
    if (result) this.lockdownState = result;
    return !!result;
  },

  openLockdown() {
    this.lockdownDialog = { show: true, reason: '', password: '', error: '', submitting: false };
    this.openDialog('lockdownDialog');
  },

  closeLockdown() {
    if (!this.lockdownDialog.show || this.lockdownDialog.submitting) return;
    this.lockdownDialog = { show: false, reason: '', password: '', error: '', submitting: false };
    this.restoreDialogFocus();
  },

  async triggerLockdown() {
    const dialog = this.lockdownDialog;
    if (dialog.submitting || !dialog.password) return;
    dialog.submitting = true;
    dialog.error = '';
    // Der Server beendet Läufe und remountet Mounts; das kann ~30 s dauern.
    const result = await this.api('POST', '/api/lockdown', { password: dialog.password, reason: dialog.reason.trim() }, { silent: true, captureError: true, timeoutMs: 120000 });
    dialog.password = '';
    dialog.submitting = false;
    if (!result) return;
    if (result.__error) { dialog.error = this.errorText(result); return; }
    this.lockdownDialog.show = false;
    this.showToast('Lockdown ausgelöst · alle Sitzungen wurden abgemeldet', 'warn');
    setTimeout(() => { window.location = '/login'; }, 1500);
  },

  async releaseLockdown() {
    let message = 'Aktuelles Passwort eingeben, um den Lockdown aufzuheben.';
    for (;;) {
      const password = await this.requestCurrentPassword(message);
      if (!password) return;
      const result = await this.api('POST', '/api/lockdown/release', { password }, { silent: true, captureError: true });
      if (!result) return;
      if (!result.__error) {
        this.lockdownSteps = result.manual_steps || [];
        this.showToast(result.released ? 'Lockdown aufgehoben · Automatik läuft wieder' : 'Es war kein Lockdown aktiv');
        await Promise.all([this.loadLockdown(), this.loadSchedulerState(true)]);
        return;
      }
      message = this.errorText(result);
      // Falsches Passwort oder Sperrfrist: erneut fragen, alles andere melden.
      if (![403, 429].includes(result.status)) { this.showToast(message, 'err'); return; }
    }
  },

  async copyLockdownSteps() {
    try { await navigator.clipboard.writeText(this.lockdownSteps.join('\n')); this.showToast('Befehle kopiert'); }
    catch (_) { this.showToast('Befehle konnten nicht kopiert werden', 'err'); }
  },

  systemLevel() {
    const data = this.overview.data;
    if (!this.online) return 'error';
    if (!data) return 'pending';
    if ((data.alerts || []).some((a) => a.level === 'error')) return 'error';
    if ((data.pairs?.health || []).some((p) => ['error', 'failed', 'timeout', 'stale'].includes(p.last_status) || (p.error && !['ok', 'success', 'cancelled'].includes(p.last_status)))) return 'error';
    if (this.storagePairs().some((p) => p.restore_evidence?.state === 'failed')) return 'error';
    if (data.services?.scheduler?.configured_enabled === false || data.services?.scheduler?.control?.paused) return 'warn';
    if ((data.alerts || []).some((a) => ['warn', 'warning'].includes(a.level))) return 'warn';
    if (this.busy()) return 'running';
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
        if (this.page === 'runs') this.loadJobs(false);
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
    if (!(await this.requestConfirmation(
      'Laufendes PBS-Backup kontrolliert abbrechen?',
      { title: 'PBS-Backup abbrechen', confirmLabel: 'Backup abbrechen' },
    ))) return;
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
  async loadPbsInsights() {
    const [insights, drill] = await Promise.all([
      this.api('GET', '/api/pbs/insights', undefined, { silent: true, requestKey: 'pbs-insights', timeoutMs: 70000 }),
      this.config.pbs?.boot_drill?.enabled
        ? this.api('GET', '/api/pbs/boot-drill/status', undefined, { silent: true, requestKey: 'boot-drill' })
        : null,
    ]);
    if (insights && !insights.__stale) this.pbsInsights = insights;
    if (drill && !drill.__stale) this.bootDrill = drill;
  },

  pbsDedupText() {
    const factor = Number(this.pbsInsights?.dedup?.dedup_factor);
    return factor > 0 ? `${factor.toLocaleString('de-DE', { maximumFractionDigits: 1 })}×` : '—';
  },

  pbsVerifySummary() {
    const verify = this.pbsInsights?.verify;
    if (!verify || !verify.total_snapshots) {
      return { tone: 'neutral', label: 'Keine Snapshots', text: verify?.error || 'Noch keine Snapshots im Datastore' };
    }
    if (verify.failed > 0) {
      return { tone: 'error', label: `${verify.failed} Verify-Fehler`, text: `${verify.verified_ok}/${verify.total_snapshots} geprüft · ${verify.unverified || 0} ungeprüft` };
    }
    if (verify.stale > 0 || verify.unverified > 0) {
      return { tone: 'warn', label: `${verify.verified_ok}/${verify.total_snapshots} geprüft`, text: `${verify.unverified || 0} ungeprüft · ${verify.stale || 0} älter als 30 Tage` };
    }
    return { tone: 'ok', label: `${verify.verified_ok}/${verify.total_snapshots} bestätigt`, text: verify.newest_verify ? `Letztes Verify ${this.formatDateTime(verify.newest_verify)}` : 'Alle Snapshots geprüft' };
  },

  pbsMaintenance() {
    const maintenance = this.pbsInsights?.maintenance;
    if (!maintenance || (maintenance.error && !maintenance.active)) {
      return { tone: 'neutral', label: 'Unbekannt', text: maintenance?.error || 'Noch nicht geprüft' };
    }
    if (!maintenance.active) return { tone: 'ok', label: 'Keine Wartung', text: 'Kein GC oder Verify aktiv' };
    const types = [...new Set((maintenance.tasks || []).map((task) => (task.type === 'garbage_collection' ? 'Garbage Collection' : 'Verify')))].join(', ') || 'Wartung';
    const hold = this.pbsInsights?.gc_throttle?.enabled ? ' · rclone-Läufe werden zurückgestellt' : '';
    return { tone: 'warn', label: 'Wartung aktiv', text: types + hold };
  },

  bootDrillLast() {
    const last = this.bootDrill?.last;
    return last?.summary || last || null;
  },

  bootDrillBadge() {
    const last = this.bootDrillLast();
    if (this.bootDrill?.running) return { tone: 'running', label: 'Läuft' };
    if (!last) return { tone: 'neutral', label: 'Nie gelaufen' };
    return last.bootable ? { tone: 'ok', label: 'Bootfähig ✓' } : { tone: 'error', label: 'Nicht bootfähig !' };
  },

  scheduleNextText(state) {
    if (state?.due?.next_run) return this.formatDateTime(state.due.next_run);
    return state?.settings?.enabled ? 'Nur manuell' : 'Deaktiviert';
  },

  async runBootDrill() {
    if (this.pending.bootDrill || this.bootDrill?.running) return;
    this.pending.bootDrill = true;
    try {
      const result = await this.api('POST', '/api/pbs/boot-drill');
      if (result?.job_id) {
        this.bootDrill = { ...this.bootDrill, running: true };
        this.showToast(`Boot-Drill gestartet (Lauf #${result.job_id})`);
      }
    } finally {
      this.pending.bootDrill = false;
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
  async loadStorage(includeRemote = false, refreshSizes = false) {
    const hadPairs = this.storagePairs().length > 0;
    this.storageState = 'loading';
    this.storageError = '';
    const query = includeRemote
      ? `?include_remote=true&refresh_sizes=${refreshSizes ? 'true' : 'false'}`
      : '';
    const result = await this.api('GET', `/api/storage/overview${query}`, undefined, {
      silent: true,
      captureError: true,
      requestKey: 'storage-overview',
      timeoutMs: includeRemote ? 70000 : 30000,
    });
    if (this.isStale(result)) return undefined;
    if (result?.__error) {
      const detail = typeof result.detail === 'string'
        ? result.detail
        : (result.detail?.message || 'Storage-Übersicht konnte nicht geladen werden');
      this.storageError = detail;
      this.storageState = hadPairs ? 'stale' : 'failed';
      return null;
    }
    // storage_pairs auch außerhalb des Dashboards ablegen: overview.data ist auf
    // der Pairs-Seite noch null (loadOverview läuft nur dort). Alle Leser nutzen
    // optionales Chaining, ein leeres Objekt ist daher unkritisch.
    if (result?.pairs) {
      if (!this.overview.data) this.overview.data = {};
      const previous = new Map(this.storagePairs().map((pair) => [pair.name, pair]));
      this.overview.data.storage_pairs = includeRemote
        ? result.pairs
        : result.pairs.map((pair) => {
          const old = previous.get(pair.name) || {};
          return {
            ...pair,
            source_size: pair.source_size || old.source_size,
            target_size: pair.target_size || old.target_size,
            source_measurement: old.source_size ? old.source_measurement : pair.source_measurement,
            target_measurement: old.target_size ? old.target_measurement : pair.target_measurement,
          };
        });
      if (includeRemote || previous.size === 0) {
        this.storageMeasurement = result.measurement || {
          state: includeRemote ? 'loaded' : 'loading',
          total: result.pairs.length * 2,
          loaded: 0,
          failed: 0,
          stale: 0,
        };
      }
      const measuredState = ['loaded', 'partial', 'failed', 'stale'].includes(this.storageMeasurement.state)
        ? this.storageMeasurement.state
        : 'partial';
      this.storageState = includeRemote
        ? measuredState
        : (previous.size > 0 && measuredState !== 'loaded' ? measuredState : 'loaded');
      this.storageError = this.storageMeasurement.measurement_error || '';
    } else {
      this.storageError = 'Der Server hat keine Storage-Daten geliefert.';
      this.storageState = hadPairs ? 'stale' : 'failed';
    }
    return result;
  },

  storagePairs() { return this.overview.data?.storage_pairs || []; },

  copyFolder(entry, kind) {
    const values = [entry?.source, entry?.target, entry?.local, entry?.remote].filter(Boolean);
    const isLocal = (value) => String(value).startsWith('/');
    const match = values.find((value) => kind === 'local' ? isLocal(value) : !isLocal(value));
    return match || '—';
  },

  copySize(entry) {
    const sourceIsCloud = entry?.source && !String(entry.source).startsWith('/');
    const targetIsCloud = entry?.target && !String(entry.target).startsWith('/');
    if (sourceIsCloud) return entry.source_size || null;
    if (targetIsCloud) return entry.target_size || null;
    return entry.target_size || entry.source_size || null;
  },

  copySide(entry, kind) {
    const wantLocal = kind === 'local';
    const sourceIsLocal = entry?.source && String(entry.source).startsWith('/');
    const targetIsLocal = entry?.target && String(entry.target).startsWith('/');
    if (sourceIsLocal === wantLocal) return entry?.source_size || null;
    if (targetIsLocal === wantLocal) return entry?.target_size || null;
    return null;
  },

  copySideError(entry, kind) {
    const size = this.copySide(entry, kind);
    return size?.measurement_error || size?.error || '';
  },

  copyMetric(entry, kind) {
    const size = this.copySize(entry);
    if (!size || size.error || size.measurement_status === 'failed') return '—';
    if (kind === 'count') return size.count ?? '—';
    if (kind === 'bytes') return size.bytes === null || size.bytes === undefined ? '—' : this.formatBytes(size.bytes);
    if (kind === 'age') return size.measured_at ? this.formatTs(size.measured_at) : 'Noch nicht gemessen';
    return '—';
  },

  restoreEvidence(entry) {
    const evidence = entry?.restore_evidence;
    const state = evidence?.state || 'never';
    const labels = {
      passed: 'Stichprobe bestätigt', partial: 'Stichprobe unvollständig',
      stale: 'Nachweis veraltet', failed: 'Letzter Restore-Test fehlgeschlagen',
      never: 'Noch kein Restore-Nachweis',
    };
    const age = Number(evidence?.valid_until || 0) * 1000 - Date.now();
    const fresh = state === 'passed' && age > 0;
    const count = Number(evidence?.verified_files || 0);
    const sample = Number(evidence?.sample_size || 0);
    const effective = !fresh && state === 'passed' ? 'stale' : state;
    return { state: fresh ? 'passed' : effective, text: fresh ? `${count}/${sample} geprüft` : (labels[effective] || labels.never), title: labels[effective] || labels.never };
  },

  restoreEvidenceLabel(entry) { return this.restoreEvidence(entry).text; },

  copyDelta(entry) {
    const source = entry?.source_size;
    const target = entry?.target_size;
    if (!source || !target || !Number.isFinite(Number(source.bytes)) || !Number.isFinite(Number(target.bytes))) return null;
    const sourceBytes = Number(source.bytes);
    const targetBytes = Number(target.bytes);
    const max = Math.max(sourceBytes, targetBytes, 1);
    return { source: sourceBytes, target: targetBytes, coverage: Math.min(100, Math.round(targetBytes / Math.max(sourceBytes, 1) * 100)), sourceWidth: sourceBytes / max * 100, targetWidth: targetBytes / max * 100 };
  },

  copyPathTitle(value) { return String(value || ''); },

  async copyPath(value) {
    try { await navigator.clipboard.writeText(String(value || '')); this.showToast('Pfad kopiert'); }
    catch (_) { this.showToast('Pfad konnte nicht kopiert werden', 'err'); }
  },

  async loadObjectLock() {
    // Backend cached 1 h; daher nur beim ersten Lagebild und bei expliziter Größenmessung.
    const result = await this.api('GET', '/api/storage/object-lock', undefined, { silent: true, requestKey: 'object-lock', timeoutMs: 70000 });
    if (result && !result.__stale) this.objectLock = result;
  },

  objectLockBadge(entry) {
    const cloud = this.copyFolder(entry, 'cloud');
    const [remote, ...rest] = cloud.split(':');
    const bucket = rest.join(':').replace(/^\/+/, '').split('/')[0];
    if (!rest.length || !bucket) return { tone: 'neutral', label: 'n/a', title: 'Kein S3-Ziel' };
    if (!this.objectLock) return { tone: 'neutral', label: '—', title: 'Noch nicht geprüft' };
    // Wie s3_object_lock._targets: Optionen nach Komma gehören nicht zum Remote-Namen.
    const match = (this.objectLock.buckets || []).find((item) => item.remote === remote.split(',')[0] && item.bucket === bucket);
    if (!match) return { tone: 'neutral', label: this.objectLock.error ? '—' : 'n/a', title: this.objectLock.error || 'Kein S3-Ziel' };
    const tone = { immutable: 'ok', unprotected: 'warn' }[match.level] || 'neutral';
    return { tone, label: match.level === 'unknown' ? '—' : match.label, title: match.error || match.hint || match.label };
  },

  async loadPairSizes() {
    if (this.storageLoading) return;
    this.storageLoading = true;
    this.loadObjectLock();
    try {
      const result = await this.loadStorage(true, true);
      if (result?.pairs) {
        const summary = result.measurement || this.storageMeasurement;
        const message = `${summary.loaded || 0}/${summary.total || 0} Messungen erfolgreich`;
        this.showToast(message, summary.state === 'loaded' ? 'ok' : (summary.state === 'failed' ? 'err' : 'warn'));
      } else {
        this.showToast(this.storageError || 'Größen konnten nicht berechnet werden', 'err');
      }
    } finally {
      this.storageLoading = false;
    }
  },

  async measureSinglePairSize(pairName, refresh = true) {
    if (!pairName) return null;
    const params = new URLSearchParams({
      pair: pairName,
      include_remote: 'true',
      refresh: refresh ? 'true' : 'false',
    });
    const result = await this.api('GET', `/api/storage/pair-size?${params}`, undefined, {
      silent: true,
      captureError: true,
      requestKey: `pair-size-${pairName}`,
      timeoutMs: 65000,
    });
    if (this.isStale(result)) return null;
    if (result?.__error || !result?.pair) {
      this.showToast(this.errorText(result), 'err');
      return null;
    }
    if (!this.overview.data) this.overview.data = {};
    const existing = this.storagePairs();
    const idx = existing.findIndex((item) => item.name === pairName);
    if (idx >= 0) {
      existing.splice(idx, 1, { ...existing[idx], ...result.pair });
    } else {
      existing.push(result.pair);
    }
    this.overview.data.storage_pairs = [...existing];
    this.showToast(`Größe für ${pairName} aktualisiert`);
    return result.pair;
  },

  storageStatusText() {
    const summary = this.storageMeasurement || {};
    if (this.storageState === 'loading') return 'Ordnergrößen werden ermittelt…';
    if (this.storageState === 'failed') return this.storageError || 'Keine Größenmessung war erfolgreich.';
    if (this.storageState === 'stale') return this.storageError || 'Es werden veraltete Messwerte angezeigt.';
    if (this.storageState === 'partial') {
      return `${summary.loaded || 0}/${summary.total || 0} Messungen erfolgreich; ${summary.failed || 0} fehlgeschlagen, ${summary.stale || 0} veraltet.`;
    }
    return '';
  },

  pairSize(name) {
    return this.storagePairs().find((entry) => entry.name === name) || null;
  },

  pairSizeText(name, side) {
    const entry = this.pairSize(name);
    if (!entry) return '';
    const size = entry[`${side}_size`];
    if (!size) return '—';
    if (size.error) return 'nicht ermittelbar';
    const count = size.count ?? null;
    const files = count === null ? '? Dateien' : `${count} ${count === 1 ? 'Datei' : 'Dateien'}`;
    return `${files} · ${this.formatBytes(size.bytes || 0)}`;
  },

  pairSizeAge(name, side) {
    const size = this.pairSize(name)?.[`${side}_size`];
    if (!size) return '';
    if (size.measurement_status === 'failed') return 'Messung fehlgeschlagen';
    if (!size.measured_at) return '';
    const prefix = size.measurement_status === 'stale' ? 'Veraltet' : 'Gemessen';
    return `${prefix} ${this.formatTs(size.measured_at)}`;
  },

  pairSizeError(name, side) {
    const size = this.pairSize(name)?.[`${side}_size`];
    return size?.measurement_error || size?.error || '';
  },

  async loadCopies() {
    this.copies.loading = true;
    const result = await this.api('GET', '/api/diagnostics/copies', undefined, { requestKey: 'copies' });
    if (this.isStale(result)) return;
    if (result) this.copies.data = result;
    this.copies.loading = false;
  },

  async loadStorageComposition(pair, side = 'source', refresh = false) {
    const key = `${pair}:${side}`;
    this.storageComposition.loading[key] = true;
    this.storageComposition.errors[key] = '';
    const params = new URLSearchParams({ pair, side, refresh: refresh ? 'true' : 'false' });
    const result = await this.api('GET', `/api/storage/composition?${params}`, undefined, { silent: true, captureError: true, requestKey: `composition-${key}`, timeoutMs: 70000 });
    if (this.isStale(result)) return;
    if (result?.__error) {
      const detail = result.detail;
      this.storageComposition.errors[key] = typeof detail === 'string' ? detail : (detail?.message || 'Zusammensetzung konnte nicht geladen werden.');
    } else if (result) this.storageComposition.data[key] = result;
    this.storageComposition.loading[key] = false;
  },

  compositionFor(pair, side) { return this.storageComposition.data[`${pair}:${side}`] || null; },
  compositionLoading(pair, side) { return Boolean(this.storageComposition.loading[`${pair}:${side}`]); },
  compositionError(pair, side) { return this.storageComposition.errors[`${pair}:${side}`] || ''; },
  compositionBuckets(pair, side) {
    return (this.compositionFor(pair, side)?.categories || []).filter((item) => Number(item.bytes) > 0).slice(0, 8);
  },
  compositionPercent(bucket, pair, side) {
    const total = Number(this.compositionFor(pair, side)?.bytes || 0);
    return total > 0 ? Math.max(0, Math.min(100, Number(bucket.bytes || 0) / total * 100)) : 0;
  },
  copyMatrixSummary() {
    const rows = this.copies.data?.sources || [];
    return { total: rows.length, protected: rows.filter((row) => row.level === 'ok').length, atRisk: rows.filter((row) => row.level !== 'ok') };
  },

  async runRestoreTest(pairName = '') {
    const scope = pairName ? `Pair „${pairName}“` : 'alle aktiven Pairs';
    if (!(await this.requestConfirmation(
      `Restore-Drill für ${scope} starten?\n\nEine Stichprobe wird vom Ziel zurückgeholt und per Prüfsumme mit der Quelle verglichen. Das erzeugt Egress-Kosten beim Anbieter. Die zurückgeholten Dateien werden nach dem Vergleich gelöscht.`,
      { title: 'Restore-Drill starten', confirmLabel: 'Restore-Drill starten' },
    ))) return;
    const query = pairName ? `?pairs=${encodeURIComponent(pairName)}` : '';
    const result = await this.api('POST', `/api/jobs/backup/restore-test${query}`);
    if (result?.ok) {
      this.showToast('Restore-Drill gestartet', 'ok');
      this.loadJobs(true);
      this.loadOverview(true);
    }
  },

  copyLevelLabel(level) {
    return { ok: 'Ausreichend', warn: 'Prüfen', error: 'Kritisch' }[level] || level;
  },
};
