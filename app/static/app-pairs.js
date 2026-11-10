// Datenwege: Paare, Tags, Konflikte, Versionsordner, Remote-Setup, Crypt, Picker.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.
import { requestControllers } from './app-core.js';

// Schlüssel liegen nur hier im Komponenten-State und werden beim Schließen verworfen.
export const emptyCrypt = () => ({
  show: false, step: 1, busy: false, error: '', candidates: [], name: '', base_remote: '', base_path: '',
  filename_encryption: 'standard', directory_name_encryption: true, password: '', password2: '',
  result: null, saved: false, verify: null,
});

export const pairsMethods = {
  normalizePair(pair) {
    if (pair.enabled === undefined) pair.enabled = true;
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
    if (pair.sqlite_backup === undefined) pair.sqlite_backup = false;
    pair.canary_file ||= '';
    pair.backup_dir ||= '';
    pair.backup_dir1 ||= '';
    pair.backup_dir2 ||= '';
    pair.pre_hook ||= '';
    pair.post_hook ||= '';
    pair.exclude ||= '';
    pair.include ||= '';
    pair.filter ||= '';
    pair.transfers ??= '';
    pair.checkers ??= '';
    pair.max_delete ??= 100;
    if (Array.isArray(pair.rclone_args)) pair.rclone_args = pair.rclone_args.join('\n');
    else pair.rclone_args ||= '';
  },

  togglePairSelection(name) {
    if (!name) return;
    if (this.pairSelection[name]) delete this.pairSelection[name];
    else this.pairSelection[name] = true;
  },

  clearPairSelection() {
    this.pairSelection = {};
  },

  selectedPairNames() {
    return Object.keys(this.pairSelection).filter((name) => this.pairSelection[name]);
  },

  selectedPairCount() {
    return this.selectedPairNames().length;
  },

  async bulkPairAction(action) {
    const names = this.selectedPairNames();
    if (!names.length) return;
    if (this.configDirty) {
      this.showToast('Bitte zuerst ungespeicherte Änderungen speichern', 'warn');
      return;
    }
    const revision = this.config?._revision;
    if (!revision) {
      this.showToast('Konfiguration nicht geladen', 'err');
      return;
    }
    const result = await this.api(
      'POST', '/api/config/pairs/bulk',
      { names, action, revision },
      { captureError: true, silent: true },
    );
    if (result?.__error) {
      const detail = typeof result.detail === 'string'
        ? result.detail
        : (result.detail?.message || 'Bulk-Aktion fehlgeschlagen');
      this.showToast(detail, 'err');
      if (result.status === 409) await this.loadConfig();
      return;
    }
    if (!result?.ok) return;
    this.config = result.config || this.config;
    this.clearPairSelection();
    const verb = action === 'enable' ? 'aktiviert' : 'deaktiviert';
    this.showToast(`${result.changed} Pair(e) ${verb}`);
    await this.loadConfig();
    this.loadOverview(true);
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
      schedule: 'manual', namespace: '', backup_id: '',
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
      name: '', remote: '', local: '', source: '', target: '', enabled: false,
      direction: selected.direction, mode: selected.mode, two_way: selected.direction === 'bisync', min_local_files: 1,
      exclude: '.DS_Store\nThumbs.db', include: '', filter: '', rclone_args: '',
      transfers: '', checkers: '', max_delete: 100, allow_delete: false,
      min_remote_files: 0, allow_empty_remote_target: false, min_free_gb: 0, max_success_age_hours: 48, require_mountpoint: false,
      mountpoint: '', sentinel_file: '',
      backup_dir: '', backup_dir1: '', backup_dir2: '',
      pre_hook: '', post_hook: '',
    };
    this.config.backup.pairs.push(pair);
    const idx = this.config.backup.pairs.length - 1;
    this.pairOpen[idx] = true;
    this.markConfigDirty();
    this.showToast(selected.mode === 'copy' ? 'Sichere Copy-Vorlage angelegt' : 'Deaktivierte Vorlage angelegt – Löschschutz prüfen', selected.mode === 'copy' ? 'ok' : 'warn');
    this.$nextTick(() => document.getElementById(`pair-${idx}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
  },

  // rclone bricht ab, wenn --backup-dir im Ziel liegt. Der Vorschlag setzt die
  // Versionsablage daher als Geschwister neben das Ziel.
  suggestedBackupDir(path) {
    const value = String(path || '').trim().replace(/\/+$/, '');
    if (!value) return '';
    return `${value}-versions/{date}`;
  },

  applyBackupDirSuggestion(pair) {
    if (pair.direction === 'bisync') {
      pair.backup_dir1 ||= this.suggestedBackupDir(pair.local);
      pair.backup_dir2 ||= this.suggestedBackupDir(pair.remote);
    } else {
      const target = pair.direction === 'pull' ? pair.local : pair.remote;
      pair.backup_dir = this.suggestedBackupDir(target);
    }
    this.markConfigDirty();
  },

  backupDirMissing(pair) {
    const destructive = pair.two_way || pair.direction === 'bisync' || pair.mode === 'sync';
    if (!destructive) return false;
    return !(pair.backup_dir || pair.backup_dir1 || pair.backup_dir2);
  },

  clonePair(idx) {
    const source = this.config.backup.pairs[idx] || {};
    const clone = JSON.parse(JSON.stringify(source));
    delete clone.id;
    clone.name = `${clone.name || 'Pair'}_Kopie`;
    clone.enabled = false;
    this.config.backup.pairs.splice(idx + 1, 0, clone);
    this.markConfigDirty();
    this.pairOpen[idx + 1] = true;
    this.showToast('Pair als deaktivierte Kopie angelegt');
  },

  async removePair(idx) {
    const pair = this.config.backup.pairs[idx];
    if (!(await this.requestConfirmation(
      `Pair „${pair?.name || 'ohne Namen'}“ aus der Konfiguration entfernen?`,
      { title: 'Datenweg entfernen', confirmLabel: 'Datenweg entfernen' },
    ))) return;
    this.config.backup.pairs.splice(idx, 1);
    this.markConfigDirty();
  },

  movePair(idx, delta) {
    const target = idx + delta;
    if (target < 0 || target >= this.config.backup.pairs.length) return;
    const [pair] = this.config.backup.pairs.splice(idx, 1);
    this.config.backup.pairs.splice(target, 0, pair);
    this.markConfigDirty();
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
    if (this.activeTag && !(pair.tags || []).includes(this.activeTag)) return false;
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
    const destructive = pair.two_way || pair.direction === 'bisync' || pair.mode === 'sync';
    if (destructive && pair.enabled && !pair.allow_delete) issues.push('Löschen nicht freigegeben');
    if (destructive && pair.enabled && (pair.max_delete === '' || pair.max_delete === null || pair.max_delete === undefined || Number(pair.max_delete) < 0)) issues.push('Löschlimit fehlt');
    if (pair.require_mountpoint && !String(pair.mountpoint || '').trim()) issues.push('Mountpoint fehlt');
    const tags = pair.tags || [];
    if (tags.length > 10 || tags.some((tag) => !/^[a-z0-9][a-z0-9_-]{0,23}$/.test(tag))) issues.push('Tags ungültig (a–z, 0–9, _ und -, höchstens 10)');
    return issues;
  },

  pairJobLabel(pair) {
    const count = (this.config.backup?.jobs || []).filter((job) =>
      (job.data_path_ids || []).includes(pair?.id),
    ).length;
    if (!count) return 'Keinem Job zugewiesen';
    return count === 1 ? '1 Job' : `${count} Jobs`;
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
    if ((health?.last_status === 'skipped' && health?.error) || health?.overdue || this.pairIssues(pair).length) return 'warn';
    if (health?.last_status === 'ok') return 'ok';
    return 'pending';
  },

  pairLastRun(pair) {
    return (this.overview.data?.pairs?.health || []).find((item) => item.name === pair.name) || null;
  },

  pairTags() {
    return [...new Set((this.config.backup?.pairs || []).flatMap((pair) => pair.tags || []))].sort();
  },

  tagPairNames() {
    return (this.config.backup?.pairs || []).filter((pair) => pair.enabled && pair.name && (pair.tags || []).includes(this.activeTag)).map((pair) => pair.name);
  },

  setPairTags(pair, text) {
    pair.tags = [...new Set(String(text || '').split(',').map((tag) => tag.trim().toLowerCase()).filter(Boolean))];
    this.markConfigDirty();
  },

  async openConflicts(name) {
    if (!name) return;
    this.conflictDialog = { show: true, loading: true, pair: name, data: null, error: '', stepErrors: [], busy: false };
    this.openDialog('conflictDialog');
    const result = await this.api('GET', `/api/pairs/${encodeURIComponent(name)}/conflicts`, undefined, { silent: true, captureError: true, requestKey: 'conflicts', timeoutMs: 120000 });
    if (this.isStale(result) || this.conflictDialog.pair !== name) return;
    this.conflictDialog.loading = false;
    if (!result || result.__error) { this.conflictDialog.error = result ? this.errorText(result) : ''; return; }
    this.conflictDialog.data = result;
    this.conflictCounts[name] = result.groups.length;
  },

  closeConflicts() {
    if (!this.conflictDialog.show || this.conflictDialog.busy) return;
    requestControllers.get('conflicts')?.abort();
    this.conflictDialog.show = false;
    this.restoreDialogFocus();
  },

  conflictFileText(file) {
    if (!file) return 'Nicht vorhanden';
    return `${this.formatBytes(file.size)} · ${file.modtime ? this.formatDateTime(Date.parse(file.modtime) / 1000) : '—'}`;
  },

  async resolveConflict(group, choice) {
    const dialog = this.conflictDialog;
    if (dialog.busy) return;
    const url = `/api/pairs/${encodeURIComponent(dialog.pair)}/conflicts/resolve`;
    const send = (password) => this.api('POST', url, { base_path: group.base_path, choice, ...(password ? { password } : {}) }, { silent: true, captureError: true });
    dialog.busy = true;
    dialog.error = '';
    dialog.stepErrors = [];
    try {
      const result = choice === 'keep_both'
        ? await send()
        : await this.withCurrentPassword(`Die Konfliktkopien von „${group.base_path}“ werden gelöscht. Aktuelles Passwort zur Bestätigung eingeben.`, send);
      if (!result) return;
      if (result.__error) { dialog.error = this.errorText(result); return; }
      dialog.stepErrors = (result.steps || []).filter((step) => step.error).map((step) => step.error);
      if (result.completed) {
        dialog.data.groups = dialog.data.groups.filter((item) => item.base_path !== group.base_path);
        this.conflictCounts[dialog.pair] = dialog.data.groups.length;
        this.showToast(`Konflikt „${group.base_path}“ aufgelöst`);
      }
    } finally {
      dialog.busy = false;
    }
  },

  toggleLifecycleOverride(pair, enabled) {
    if (enabled) pair.versions_lifecycle = { ...this.config.backup.versions_lifecycle };
    else delete pair.versions_lifecycle;
    this.markConfigDirty();
  },

  async loadVersionsPreview(name) {
    if (!name) return;
    this.versionsState[name] = { loading: true, data: null, error: '' };
    const result = await this.api('GET', `/api/jobs/versions/preview?pair=${encodeURIComponent(name)}`, undefined, { silent: true, captureError: true, requestKey: `versions-${name}`, timeoutMs: 120000 });
    if (this.isStale(result)) return;
    this.versionsState[name] = result?.__error
      ? { loading: false, data: null, error: this.errorText(result) }
      : { loading: false, data: result, error: '' };
  },

  versionsPurgeCount(name) {
    return (this.versionsState[name]?.data?.roots || []).reduce((sum, root) => sum + (root.purge?.length || 0), 0);
  },

  // Backend liefert merge je Wurzel als [{into, from}]; die Planform {vertreter: [jüngere]} wird ebenso gelesen.
  versionsMerges(root) {
    const merge = root?.merge || [];
    return Array.isArray(merge) ? merge : Object.entries(merge).flatMap(([into, younger]) => younger.map((from) => ({ into, from })));
  },

  versionsMergeGroups(root) {
    const groups = new Map();
    for (const item of this.versionsMerges(root)) groups.set(item.into, [...(groups.get(item.into) || []), item.from]);
    return [...groups].map(([into, from]) => ({ into, from }));
  },

  versionsMergeCount(name) {
    return (this.versionsState[name]?.data?.roots || []).reduce((sum, root) => sum + this.versionsMerges(root).length, 0);
  },

  versionsSummary(name) {
    const data = this.versionsState[name]?.data;
    if (!data?.dry_run || !(data.roots || []).length) return '';
    return `Vorschau: ${this.versionsMergeCount(name)} zusammenführen · ${this.versionsPurgeCount(name)} löschen`;
  },

  async pruneVersions(name) {
    const state = this.versionsState[name];
    const count = this.versionsPurgeCount(name);
    const merges = this.versionsMergeCount(name);
    if (!(count + merges) || state.loading) return;
    state.loading = true;
    state.error = '';
    const plan = [
      merges && `${merges} Versionsordner werden in ihren älteren Vertreter zusammengeführt`,
      count && `${count} Versionsordner werden endgültig gelöscht`,
    ].filter(Boolean).join(', ');
    try {
      const result = await this.withCurrentPassword(
        `„${name}“: ${plan}. Aktuelles Passwort zur Bestätigung eingeben.`,
        (password) => this.api('POST', '/api/jobs/versions/prune', { pair: name, password }, { silent: true, captureError: true, timeoutMs: 300000 }),
      );
      if (!result) return;
      if (result.__error) { state.error = this.errorText(result); return; }
      state.data = result;
      const purged = (result.roots || []).reduce((sum, root) => sum + (root.purged?.length || 0), 0);
      const failed = (result.roots || []).reduce((sum, root) => sum + (root.failed?.length || 0), 0);
      const merged = (result.roots || []).reduce((sum, root) => sum + (root.merged || []).filter((item) => item.ok).length, 0);
      const outcome = `${merged} Versionsordner zusammengeführt, ${purged} gelöscht${failed ? `, ${failed} fehlgeschlagen` : ''}`;
      this.showToast(result.aborted ? `Ausdünnen wegen Lockdown abgebrochen: ${outcome}` : outcome, failed || result.aborted ? 'warn' : 'ok');
    } finally {
      state.loading = false;
    }
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

  async openRemoteSetup(presetProvider = '') {
    const suggestedNames = { drive: 'gdrive', pcloud: 'pcloud', onedrive: 'onedrive', webdav: 'nextcloud' };
    this.remoteSetup = {
      show: true,
      busy: true,
      name: suggestedNames[presetProvider] || '',
      provider: presetProvider || '',
      providers: [],
      express: true,
      driveMode: 'oauth',
      scope: 'drive',
      rootFolderId: '',
      clientId: '',
      clientSecret: '',
      teamDrive: false,
      serviceAccountJson: '',
      serviceAccountResult: null,
      session: null,
      answer: '',
      error: '',
    };
    this.openDialog('remoteSetupDialog');
    const data = await this.api('GET', '/api/remotes/providers', undefined, { captureError: true });
    this.remoteSetup.busy = false;
    if (!data || data.__error) { this.remoteSetup.error = data?.detail || 'Anbieter konnten nicht geladen werden.'; return; }
    this.remoteSetup.providers = data.providers;
  },

  selectRemotePreset(provider, suggestedName = '') {
    this.remoteSetup.provider = provider;
    if (!this.remoteSetup.name.trim() && suggestedName) {
      this.remoteSetup.name = suggestedName;
    }
    this.remoteSetup.error = '';
  },

  async loadDriveServiceAccountFile(event) {
    const file = event?.target?.files?.[0];
    if (!file) return;
    try {
      this.remoteSetup.serviceAccountJson = await file.text();
      this.remoteSetup.error = '';
    } catch (_) {
      this.remoteSetup.error = 'Service-Account-Datei konnte nicht gelesen werden.';
    } finally {
      event.target.value = '';
    }
  },

  async copyRemoteAuthCommand() {
    const cmd = this.remoteSetup.session?.option?.authorize_command;
    if (!cmd) return;
    try {
      await navigator.clipboard.writeText(cmd);
      this.showToast('rclone-Befehl in die Zwischenablage kopiert');
    } catch (_) {
      this.showToast('Kopieren nicht möglich – bitte Befehl manuell markieren', 'warn');
    }
  },

  async submitRemoteSetup() {
    const state = this.remoteSetup;
    if (state.busy) return;
    state.busy = true;
    state.error = '';
    const session = state.session;
    if (!session && state.provider === 'drive' && state.driveMode === 'service_account') {
      const result = await this.api(
        'POST',
        '/api/remotes/google-drive/service-account',
        {
          name: state.name.trim(),
          service_account_json: state.serviceAccountJson,
          scope: state.scope || 'drive',
          root_folder_id: (state.rootFolderId || '').trim(),
        },
        { captureError: true, timeoutMs: 70000 },
      );
      state.busy = false;
      if (!result || result.__error) {
        state.error = result?.detail || 'Service-Account-Einrichtung fehlgeschlagen.';
        return;
      }
      await this.closeRemoteSetup();
      this.showToast(`Google Drive „${result.name}“ eingerichtet (Service-Account: ${result.client_email})`, 'ok');
      if (this.picker.show && this.picker.kind === 'remote') await this.loadPicker('');
      return;
    }
    const url = !session ? '/api/remotes/setup' : `/api/remotes/setup/${session.id}/${session.ready ? 'save' : 'answer'}`;
    const body = !session
      ? {
        name: state.name.trim(),
        provider: state.provider,
        express: Boolean(state.express),
        scope: state.scope || 'drive',
        client_id: (state.clientId || '').trim(),
        client_secret: (state.clientSecret || '').trim() || null,
        root_folder_id: (state.rootFolderId || '').trim(),
        team_drive: Boolean(state.teamDrive),
      }
      : session.ready ? {} : { step: session.step, answer: state.answer || session.option.default || '' };
    const result = await this.api('POST', url, body, { captureError: true, timeoutMs: 70000 });
    if (!result || result.__error) {
      state.busy = false;
      state.error = result?.detail || 'Einrichtung fehlgeschlagen. Bitte erneut versuchen.';
      return;
    }
    if (session?.ready) {
      state.busy = false;
      state.session = null;
      await this.closeRemoteSetup();
      this.showToast(`Remote ${result.name} ist angelegt.`, 'ok');
      if (this.picker.show && this.picker.kind === 'remote') await this.loadPicker('');
    } else if (result.ready && state.express) {
      const saved = await this.api('POST', `/api/remotes/setup/${result.id}/save`, {}, { captureError: true, timeoutMs: 70000 });
      state.busy = false;
      if (!saved || saved.__error) {
        state.session = result;
        state.error = saved?.detail || 'Speichern fehlgeschlagen.';
        return;
      }
      state.session = null;
      await this.closeRemoteSetup();
      this.showToast(`Remote ${saved.name} ist angelegt.`, 'ok');
      if (this.picker.show && this.picker.kind === 'remote') await this.loadPicker('');
    } else {
      state.busy = false;
      state.session = result;
      state.answer = '';
      state.error = result.error || '';
      this.$nextTick(() => this.$refs.remoteAnswer?.focus());
    }
  },

  async closeRemoteSetup() {
    if (this.remoteSetup.busy) return;
    const id = this.remoteSetup.session?.id;
    this.remoteSetup.show = false;
    this.remoteSetup.answer = '';
    this.remoteSetup.session = null;
    this.restoreDialogFocus();
    if (id) await this.api('DELETE', `/api/remotes/setup/${id}`, undefined, { silent: true });
  },

  async openCrypt() {
    this.crypt = { ...emptyCrypt(), show: true, busy: true };
    this.openDialog('cryptDialog');
    const result = await this.api('GET', '/api/remotes/crypt/candidates', undefined, { silent: true, captureError: true });
    this.crypt.busy = false;
    if (!result || result.__error) { this.crypt.error = result ? this.errorText(result) : ''; return; }
    this.crypt.candidates = result.candidates || [];
  },

  async cryptNext() {
    const state = this.crypt;
    if (state.busy) return;
    state.error = '';
    if (state.step === 1) {
      if (!/^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/.test(state.name)) { state.error = 'Remote-Name: 2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche.'; return; }
      if (!state.base_remote) { state.error = 'Bitte ein Basis-Remote auswählen.'; return; }
      state.step = 2;
      return;
    }
    if ([state.password, state.password2].some((key) => key && key.length < 16)) { state.error = 'Eigene Schlüssel brauchen mindestens 16 Zeichen.'; return; }
    state.busy = true;
    const result = await this.api('POST', '/api/remotes/crypt', {
      name: state.name, base_remote: state.base_remote, base_path: state.base_path.trim(),
      filename_encryption: state.filename_encryption, directory_name_encryption: state.directory_name_encryption,
      ...(state.password ? { password: state.password } : {}), ...(state.password2 ? { password2: state.password2 } : {}),
    }, { silent: true, captureError: true, timeoutMs: 70000 });
    state.busy = false;
    if (!result) return;
    if (result.__error) { state.error = this.errorText(result); return; }
    state.password = '';
    state.password2 = '';
    state.result = result;
    state.step = 3;
    this.$nextTick(() => this.$refs.cryptResult?.focus());
  },

  async copyCryptKey(key) {
    try { await navigator.clipboard.writeText(this.crypt.result?.keys?.[key] || ''); this.showToast('Schlüssel kopiert'); }
    catch (_) { this.showToast('Kopieren nicht möglich – Schlüssel bitte manuell markieren', 'err'); }
  },

  async verifyCrypt() {
    const state = this.crypt;
    if (!state.result || state.busy) return;
    state.busy = true;
    const result = await this.api('POST', `/api/remotes/crypt/${encodeURIComponent(state.result.name)}/verify`, undefined, { silent: true, captureError: true, timeoutMs: 70000 });
    state.busy = false;
    if (result) state.verify = result.__error ? { ok: false, text: this.errorText(result) } : { ok: Boolean(result.ok), text: result.ok ? 'Lesbar' : 'Nicht lesbar' };
  },

  async closeCrypt() {
    if (!this.crypt.show || this.crypt.busy) return;
    if (this.crypt.result && !this.crypt.saved && !(await this.requestConfirmation(
      'Die Schlüssel werden nur jetzt angezeigt. Geht die Serverkonfiguration verloren, sind die verschlüsselten Daten ohne sie unwiederbringlich verloren. Trotzdem schließen?',
      { title: 'Schlüssel noch nicht gesichert', confirmLabel: 'Trotzdem schließen' },
    ))) return;
    this.crypt = emptyCrypt();
    this.restoreDialogFocus();
  },

  openPicker(mode, idx) {
    const kind = this.pickerKindForMode(mode);
    this.picker = {
      show: true, mode, idx, kind, current: '', parent: null,
      paths: { local: '', remote: '' }, isRoot: true,
      entries: [], loading: true, creating: false, newFolderName: '', search: '', error: '',
    };
    this.openDialog('pickerDialog');
    this.loadPicker('');
  },

  openQuickPicker(mode) { this.openPicker(mode, -1); },
  openPbsPicker(index) { this.openPicker('pbs-target', index); },

  pickerKindForMode(mode = this.picker.mode) {
    return mode?.endsWith('-remote') || mode === 'remote' ? 'remote' : 'local';
  },

  pickerCanSwitchKind() {
    return this.picker.mode?.startsWith('target-') || ['remote', 'remote-local'].includes(this.picker.mode);
  },

  switchPickerKind(kind) {
    if (!this.pickerCanSwitchKind() || !['local', 'remote'].includes(kind) || this.picker.kind === kind) return;
    if (this.picker.mode?.startsWith('target-')) this.picker.mode = `target-${kind}`;
    else this.picker.mode = kind === 'remote' ? 'remote' : 'remote-local';
    this.picker.kind = kind;
    this.picker.search = '';
    this.picker.error = '';
    this.loadPicker(this.picker.paths[kind] || '');
  },

  async loadPicker(path) {
    if (path === 'pbs:') { this.pickPath('pbs:'); return; }
    this.picker.loading = true;
    this.picker.error = '';
    this.picker.current = path;
    const endpoint = this.picker.kind === 'remote' ? '/api/browse/rclone' : '/api/browse/local';
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
      this.picker.paths[this.picker.kind] = this.picker.current;
      this.picker.isRoot = result.is_root === true;
      this.picker.error = result.error || '';
      const cloudRoot = this.picker.kind === 'remote' && !this.picker.current;
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

  canCreatePickerFolder() {
    return !this.picker.loading && !this.picker.error
      && Boolean(this.picker.current) && this.picker.current !== '/' && !this.picker.isRoot;
  },

  async createPickerFolder() {
    const name = String(this.picker.newFolderName || '').trim();
    if (!this.canCreatePickerFolder()) {
      this.showToast('Öffne zuerst einen lokalen oder Cloud-Ordner.', 'err'); return;
    }
    if (!name || name === '.' || name === '..' || name.startsWith('.') || /[\\/\r\n]/.test(name)) {
      this.showToast('Bitte einen einfachen, sichtbaren Ordnernamen eingeben.', 'err'); return;
    }
    this.picker.creating = true;
    try {
      const result = await this.api('POST', '/api/browse/directory', {
        kind: this.picker.kind,
        parent: this.picker.current,
        name,
      }, { requestKey: 'picker-create' });
      if (this.isStale(result)) return;
      if (result?.ok) {
        this.picker.newFolderName = '';
        await this.loadPicker(result.path);
        this.showToast(`Ordner angelegt: ${result.path}`);
      }
    } finally {
      this.picker.creating = false;
    }
  },

  pickerEntries() {
    const needle = this.picker.search.trim().toLowerCase();
    return needle ? this.picker.entries.filter((entry) => String(entry.name || '').toLowerCase().includes(needle)) : this.picker.entries;
  },

  pickerBreadcrumbs() {
    const value = String(this.picker.current || '');
    if (!value) return [{ label: 'Remotes', path: '' }];
    const remote = value.match(/^([^/:]+):(.*)$/);
    if (remote) {
      const root = `${remote[1]}:`;
      const segments = remote[2].split('/').filter(Boolean);
      return [{ label: remote[1], path: root }, ...segments.map((segment, index) => ({
        label: segment,
        path: `${root}${segments.slice(0, index + 1).join('/')}`,
      }))];
    }
    const absolute = value.startsWith('/');
    const segments = value.split('/').filter(Boolean);
    return [{ label: absolute ? '/' : 'Ordner', path: absolute ? '/' : '' }, ...segments.map((segment, index) => ({
      label: segment,
      path: `${absolute ? '/' : ''}${segments.slice(0, index + 1).join('/')}`,
    }))];
  },

  swapQuickPaths() {
    [this.quick.remote, this.quick.local] = [this.quick.local, this.quick.remote];
    const direction = this.quick.direction === 'push' ? 'pull' : this.quick.direction === 'pull' ? 'push' : 'bisync';
    this.quick.direction = direction;
    this.quick.mode = direction === 'bisync' ? 'bisync' : (this.quick.mode === 'bisync' ? 'copy' : this.quick.mode);
  },

  promoteQuickSync() {
    const promotion = this.quickPromotion;
    if (!promotion?.draft || this.configDirty) {
      this.showToast(this.configDirty ? 'Speichere oder verwerfe zuerst die offenen Änderungen.' : 'Quick-Sync-Datenweg nicht verfügbar.', 'warn');
      return;
    }
    const draft = promotion.draft;
    const existingNames = new Set((this.config.backup.pairs || []).map((item) => String(item.name || '').toLowerCase()));
    const nameBase = `Quick-Sync ${new Date().toLocaleDateString('de-DE')}`;
    let name = nameBase;
    let suffix = 2;
    while (existingNames.has(name.toLowerCase())) name = `${nameBase} (${suffix++})`;
    const pair = {
      name,
      local: draft.local, remote: draft.remote,
      source: draft.direction === 'push' ? draft.local : draft.remote,
      target: draft.direction === 'push' ? draft.remote : draft.local,
      direction: draft.direction, mode: draft.mode, two_way: draft.direction === 'bisync',
      enabled: false, min_local_files: draft.new_target ? 0 : 1,
      exclude: '.DS_Store\nThumbs.db', include: '', filter: '', rclone_args: '',
      transfers: '', checkers: '', max_delete: draft.max_delete, allow_delete: false,
      min_remote_files: 0, allow_empty_remote_target: false, min_free_gb: 0,
      max_success_age_hours: 48, require_mountpoint: false, mountpoint: '', sentinel_file: '',
      backup_dir: '', backup_dir1: '', backup_dir2: '',
    };
    this.config.backup.pairs.push(pair);
    this.markConfigDirty();
    this.quickPromotion = null;
    this.closeJob();
    this.navigate('pairs');
    this.showToast('Deaktivierter Entwurf angelegt. Pfade und Schutzregeln vor dem Speichern prüfen.', 'warn');
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
      this.markConfigDirty();
    }
    this.closePicker();
    this.showToast(`Pfad gesetzt: ${path}`);
  },

  closePicker() {
    if (!this.picker.show) return;
    this.picker.show = false;
    requestControllers.get('picker')?.abort();
    requestControllers.get('picker-create')?.abort();
    this.restoreDialogFocus();
  },

  async loadAvailableHooks() {
    const result = await this.api('GET', '/api/config/hooks', undefined, { silent: true, requestKey: 'config-hooks' });
    if (this.isStale(result)) return;
    if (result?.hooks) this.hooksAvailable = result.hooks;
  },

  openPairWizard() {
    this.pairWizard = {
      open: true,
      step: 1,
      preset: '',
      name: '',
      source: '',
      target: '',
      twoWay: false,
      mode: 'copy',
      allowDelete: false,
      maxDelete: 100,
      backupDir: '',
      requireMountpoint: false,
    };
  },

  applyPairWizardPreset(presetId) {
    const presets = {
      family_photos: {
        preset: 'family_photos',
        twoWay: false,
        mode: 'copy',
        allowDelete: false,
        maxDelete: 50,
        requireMountpoint: true,
      },
      documents: {
        preset: 'documents',
        twoWay: false,
        mode: 'sync',
        allowDelete: true,
        maxDelete: 100,
        requireMountpoint: false,
      },
      archive: {
        preset: 'archive',
        twoWay: false,
        mode: 'copy',
        allowDelete: false,
        maxDelete: 25,
        requireMountpoint: true,
      },
      critical_data: {
        preset: 'critical_data',
        twoWay: false,
        mode: 'sync',
        allowDelete: true,
        maxDelete: 50,
        requireMountpoint: true,
      },
    };
    const chosen = presets[presetId];
    if (!chosen) return;
    Object.assign(this.pairWizard, chosen);
  },

  closePairWizard() {
    this.pairWizard.open = false;
  },

  nextPairWizardStep() {
    if (this.pairWizard.step === 1 && (!this.pairWizard.name.trim() || !this.pairWizard.source.trim())) {
      this.showToast('Bitte Name und Quellpfad angeben.', 'warn');
      return;
    }
    if (this.pairWizard.step === 2 && !this.pairWizard.target.trim()) {
      this.showToast('Bitte einen Zielpfad angeben.', 'warn');
      return;
    }
    if (this.pairWizard.step === 2 && !this.pairWizard.backupDir) {
      this.pairWizard.backupDir = this.suggestedBackupDir(this.pairWizard.target);
    }
    if (this.pairWizard.step < 4) this.pairWizard.step += 1;
  },

  prevPairWizardStep() {
    if (this.pairWizard.step > 1) this.pairWizard.step -= 1;
  },

  applyPairWizard() {
    const w = this.pairWizard;
    const isBisync = Boolean(w.twoWay);
    const direction = isBisync ? 'bisync' : (w.target.startsWith('/') && !w.source.startsWith('/') ? 'pull' : 'push');
    const mode = isBisync ? 'bisync' : (w.mode === 'sync' ? 'sync' : 'copy');
    const pair = {
      name: w.name.trim(),
      source: w.source.trim(),
      target: w.target.trim(),
      local: direction === 'pull' ? w.target.trim() : w.source.trim(),
      remote: direction === 'pull' ? w.source.trim() : w.target.trim(),
      enabled: false,
      direction,
      mode,
      two_way: isBisync,
      min_local_files: 1,
      exclude: '.DS_Store\nThumbs.db',
      include: '',
      filter: '',
      rclone_args: '',
      transfers: '',
      checkers: '',
      max_delete: Number(w.maxDelete) || 100,
      allow_delete: Boolean(w.allowDelete),
      min_remote_files: 0,
      allow_empty_remote_target: false,
      min_free_gb: 0,
      max_success_age_hours: 48,
      require_mountpoint: Boolean(w.requireMountpoint),
      mountpoint: '',
      sentinel_file: '',
      backup_dir: isBisync ? '' : (w.backupDir || ''),
      backup_dir1: isBisync ? this.suggestedBackupDir(w.source.trim()) : '',
      backup_dir2: isBisync ? this.suggestedBackupDir(w.target.trim()) : '',
      pre_hook: '',
      post_hook: '',
    };
    this.config.backup.pairs.push(pair);
    const idx = this.config.backup.pairs.length - 1;
    this.pairOpen[idx] = true;
    this.pairWizard.open = false;
    this.markConfigDirty();
    this.showToast(`Datenweg „${pair.name}“ über den Assistenten angelegt – vor dem Aktivieren prüfen`);
  },
};
