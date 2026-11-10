// System: Doctor, Push, VPN, Bitrot, Wartung, Snapshots, Audit.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.

export const systemMethods = {
  async loadSystemBundle() {
    const result = await this.api('GET', '/api/diagnostics/system-bundle', undefined, { silent: true, requestKey: 'system-bundle' });
    if (this.isStale(result)) return;
    if (!result) {
      this.loadOverview(true); this.loadLogs(); this.loadDatabaseStatus(); this.loadSnapshots();
      this.loadAudit(); this.loadCopies(); this.loadSchedulerState(true); this.loadPushStatus(true);
      this.loadBitrot(); this.loadRansomwareStatus();
      return;
    }
    if (result.overview) this.overview.data = result.overview;
    if (result.copies) this.copies.data = result.copies;
    if (result.logs?.logs) this.maintenance.logs = result.logs.logs;
    if (result.database) this.maintenance.database = result.database;
    if (result.snapshots?.snapshots) {
      this.snapshots.items = result.snapshots.snapshots;
      this.snapshots.max = result.snapshots.max_snapshots || 30;
    }
    if (result.audit?.events) this.audit.items = result.audit.events;
    if (result.audit?.chain) this.audit.chain = result.audit.chain;
    if (result.scheduler) this.schedulerControl = { ...this.schedulerControl, ...result.scheduler, loading: false };
    if (result.push) {
      this.push.status = result.push;
      this.push.error = '';
    }
    if (result.bitrot) {
      this.bitrot.data = result.bitrot;
      this.bitrot.error = false;
    }
    if (result.ransomware) {
      this.ransomwareStatus.data = result.ransomware;
      this.ransomwareStatus.error = false;
    }
  },

  async loadDoctor() {
    this.doctor.loading = true;
    const result = await this.api('GET', '/api/diagnostics/doctor', undefined, { timeoutMs: 120000, requestKey: 'doctor' });
    if (this.isStale(result)) return;
    if (result) this.doctor.data = result;
    this.doctor.loading = false;
  },

  async loadPushStatus(silent = false) {
    this.push.loading = true;
    const result = await this.api('GET', '/api/push/status', undefined, { silent, requestKey: 'push-status' });
    if (this.isStale(result)) return;
    if (result) {
      this.push.status = result;
      this.push.error = '';
    } else if (!silent) {
      this.push.error = 'Push-Status konnte nicht geladen werden.';
    }
    this.push.loading = false;
  },

  async testPushNotification() {
    this.push.testing = true;
    const result = await this.api('POST', '/api/push/test');
    this.push.testing = false;
    if (!result?.ok) return;
    this.showToast('Testmitteilung wurde über APNs zugestellt');
    await this.loadPushStatus(true);
  },

  async loadVpn(refresh = false) {
    this.vpn.loading = true;
    const result = await this.api('GET', `/api/diagnostics/vpn${refresh ? '?refresh=1' : ''}`, undefined, { silent: !refresh, requestKey: 'vpn', timeoutMs: 60000 });
    if (this.isStale(result)) return;
    this.vpn.error = !result;
    if (result) this.vpn.data = result;
    this.vpn.loading = false;
  },

  wireguardPeers() {
    return (this.vpn.data?.wireguard?.interfaces || []).flatMap((iface) => (iface.peers || []).map((peer) => ({ ...peer, iface: iface.name })));
  },

  handshakeState(age) {
    if (age === null || age === undefined) return { tone: 'error', label: 'Nie' };
    return age <= 180
      ? { tone: 'ok', label: `vor ${this.formatDur(age)}` }
      : { tone: 'warn', label: `Veraltet · vor ${this.formatDur(age)}` };
  },

  async loadCloudQuota(refresh = false) {
    this.cloudQuota.loading = true;
    const result = await this.api('GET', `/api/storage/quota${refresh ? '?refresh=true' : ''}`, undefined, { silent: !refresh, requestKey: 'cloud-quota', timeoutMs: 45000 });
    if (this.isStale(result)) return;
    this.cloudQuota.error = !result;
    if (result) this.cloudQuota.data = result;
    this.cloudQuota.loading = false;
  },

  async loadRansomwareStatus() {
    this.ransomwareStatus.loading = true;
    const result = await this.api('GET', '/api/security/ransomware-status', undefined, { silent: true, requestKey: 'ransomware-status' });
    if (this.isStale(result)) return;
    this.ransomwareStatus.error = !result;
    if (result) this.ransomwareStatus.data = result;
    this.ransomwareStatus.loading = false;
  },

  async initPairCanary(pairName, filename = '') {
    if (!pairName) return;
    const body = filename ? { filename } : {};
    let result = await this.api('POST', `/api/security/canary/${encodeURIComponent(pairName)}/init`, body, { silent: true, captureError: true });
    if (result?.__error && result.status === 403) {
      const password = await this.requestCurrentPassword('Aktuelles Passwort zum Überschreiben der bestehenden Canary-Datei eingeben.');
      if (!password) return;
      result = await this.api('POST', `/api/security/canary/${encodeURIComponent(pairName)}/init`, { ...body, current_password: password });
    }
    if (result?.ok) {
      this.showToast(`Canary-Datei für ${pairName} angelegt (${result.canary_file})`);
      await this.loadRansomwareStatus();
    } else if (result?.__error) {
      this.showToast(this.errorText(result), 'err');
    }
  },

  async loadBitrot() {
    this.bitrot.loading = true;
    const result = await this.api('GET', '/api/jobs/bitrot-scrub/status', undefined, { silent: true, requestKey: 'bitrot-status' });
    if (this.isStale(result)) return;
    this.bitrot.error = !result;
    if (result) this.bitrot.data = result;
    this.bitrot.loading = false;
  },

  bitrotPairs() {
    return (this.config.backup?.pairs || []).filter((pair) => pair.enabled && pair.name && this.copyFolder(pair, 'cloud') !== '—');
  },

  bitrotMismatches(summary) {
    return (summary?.pairs || []).flatMap((pair) => (pair.mismatches || []).map((item) => ({ ...item, pair: pair.name })));
  },

  bitrotSettingsText() {
    const s = this.bitrot.data?.settings;
    if (!s) return '';
    return `Stichprobe ${s.sample_percent} % der fälligen Dateien · höchstens ${s.max_files} Dateien und ${s.max_total_mb} MB je Lauf · geprüfte Dateien sind nach ${s.rotation_days} Tagen wieder fällig`;
  },

  bitrotBaselineText(name) {
    const base = this.bitrot.baselines[name];
    if (!base) return 'Noch nicht abgefragt';
    if (base.error) return base.error;
    if (!base.rows) return 'Noch keine Prüfsummen erfasst';
    return `${Number(base.rows).toLocaleString('de-DE')} Prüfsummen · erfasst seit ${this.formatDateTime(base.oldest_recorded_at)} · älteste Bestätigung ${this.formatDateTime(base.oldest_verified_at)}`;
  },

  async runBitrotScrub() {
    if (this.pending.bitrot) return;
    if (!(await this.requestConfirmation(
      'Bitrot-Scrub für alle aktiven Cloud-Datenwege starten?\n\nEine Stichprobe wird aus der Cloud gelesen und per SHA-256 mit der gespeicherten Baseline verglichen. Das erzeugt Abruf- und Egress-Kosten beim Anbieter. Inhalte werden nicht gespeichert.',
      { title: 'Bitrot-Scrub starten', confirmLabel: 'Scrub starten', tone: 'primary' },
    ))) return;
    this.pending.bitrot = true;
    try {
      const result = await this.api('POST', '/api/jobs/bitrot-scrub', { pairs: null });
      if (result?.ok) {
        this.showToast(`Bitrot-Scrub gestartet (Lauf #${result.job_id})`);
        this.loadBitrot();
      }
    } finally {
      this.pending.bitrot = false;
    }
  },

  async loadBitrotBaseline(name) {
    const result = await this.api('GET', `/api/jobs/bitrot-scrub/baseline?pair=${encodeURIComponent(name)}`, undefined, { silent: true, captureError: true });
    if (result) this.bitrot.baselines[name] = result.__error ? { error: this.errorText(result) } : result;
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
    if (!dryRun && !(await this.requestConfirmation(
      'Alte Logs wirklich löschen?',
      { title: 'Alte Logs löschen', confirmLabel: 'Logs löschen' },
    ))) return;
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
    if (!(await this.requestConfirmation(
      'Alte Jobhistorie gemäß Aufbewahrung löschen?',
      { title: 'Datenbank aufräumen', confirmLabel: 'Jobhistorie löschen' },
    ))) return;
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

  async loadSnapshotDiff() {
    const name = this.snapshots.restoreName;
    if (!name) { this.snapshots.diff = { loading: false, data: null, error: '' }; return; }
    this.snapshots.diff = { loading: true, data: null, error: '' };
    const result = await this.api('GET', `/api/maintenance/config/snapshots/${encodeURIComponent(name)}/diff`, undefined, { silent: true, captureError: true, requestKey: 'snapshot-diff' });
    if (this.isStale(result)) return;
    if (result?.__error) {
      const detail = result.detail;
      this.snapshots.diff = { loading: false, data: null, error: typeof detail === 'string' ? detail : (detail?.message || 'Snapshot-Unterschiede konnten nicht geladen werden.') };
      return;
    }
    this.snapshots.diff = { loading: false, data: result || null, error: '' };
  },

  async createSnapshot() {
    const result = await this.api('POST', '/api/maintenance/config/snapshots');
    if (result?.ok) {
      this.showToast(`Snapshot erstellt: ${result.snapshot.name}`);
      await this.loadSnapshots();
    }
  },

  async restoreSnapshot() {
    if (!this.snapshots.restoreName) {
      this.showToast('Bitte zuerst einen Snapshot auswählen', 'err'); return;
    }
    let currentPassword = this.snapshots.password;
    if (!currentPassword) {
      currentPassword = await this.requestCurrentPassword('Aktuelles Passwort oder Passkey zur Wiederherstellung des Snapshots bestätigen.');
      if (!currentPassword) return;
    }
    if (!(await this.requestConfirmation(
      'Diesen Snapshot wiederherstellen? Aktuelle Zugangsdaten bleiben erhalten; alle Sitzungen werden beendet.',
      { title: 'Snapshot wiederherstellen', confirmLabel: 'Wiederherstellen' },
    ))) return;
    const selected = this.snapshots.items.find((item) => item.name === this.snapshots.restoreName);
    const result = await this.api('POST', '/api/maintenance/config/snapshots/restore', {
      name: this.snapshots.restoreName,
      current_password: currentPassword,
      expected_revision: this.config._revision,
      sha256: selected?.sha256 || null,
    });
    if (result?.ok) {
      this.showToast('Snapshot wiederhergestellt – erneute Anmeldung erforderlich');
      setTimeout(() => { window.location = '/login'; }, 900);
    }
  },

  async downloadBootstrapBundle() {
    const be = this.bootstrapExport;
    if (be.busy) return;
    if (!be.passphrase || be.passphrase.length < 12) {
      this.showToast('Bootstrap-Passphrase muss mindestens 12 Zeichen lang sein.', 'err');
      return;
    }
    if (be.passphrase !== be.repeat) {
      this.showToast('Passphrasen stimmen nicht überein.', 'err');
      return;
    }
    const currentPassword = await this.requestCurrentPassword(
      'Aktuelles Passwort oder Passkey bestätigen, um das verschlüsselte Bootstrap-Paket zu exportieren.',
    );
    if (!currentPassword) return;
    be.busy = true;
    try {
      const result = await this.api(
        'POST',
        '/api/config/bootstrap-export',
        {
          current_password: currentPassword,
          passphrase: be.passphrase,
          include_secrets: Boolean(be.includeSecrets),
        },
        { silent: true, captureError: true },
      );
      if (!result || result.__error || !result.bundle) {
        this.showToast(this.errorText(result), 'err');
        return;
      }
      const blob = new Blob([JSON.stringify(result.bundle, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'sicherpfad-bootstrap.encrypted.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      be.passphrase = '';
      be.repeat = '';
      this.showToast('Verschlüsseltes Bootstrap-Paket heruntergeladen');
      this.loadAudit();
    } finally {
      be.busy = false;
    }
  },

  async verifyBootstrapBundle(event) {
    const file = event?.target?.files?.[0];
    if (!file) return;
    const be = this.bootstrapExport;
    if (!be.passphrase) {
      this.showToast('Bitte zuerst die Passphrase des Bootstrap-Pakets eingeben.', 'warn');
      event.target.value = '';
      return;
    }
    be.busy = true;
    be.verifyResult = null;
    try {
      const text = await file.text();
      let bundle;
      try {
        bundle = JSON.parse(text);
      } catch (_) {
        this.showToast('Ausgewählte Datei ist kein gültiges JSON-Paket.', 'err');
        return;
      }
      const result = await this.api(
        'POST',
        '/api/config/bootstrap-verify',
        { bundle, passphrase: be.passphrase },
        { silent: true, captureError: true },
      );
      if (!result || result.__error) {
        this.showToast(this.errorText(result), 'err');
        return;
      }
      be.verifyResult = result;
      this.showToast(`Bootstrap-Paket erfolgreich entschlüsselt (${result.pairs_count} Datenwege)`);
    } finally {
      be.busy = false;
      event.target.value = '';
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
    if (result?.chain) this.audit.chain = result.chain;
    this.audit.loading = false;
  },

  auditLabel(type) {
    return ({
      scheduler_paused: 'Scheduler pausiert', scheduler_resumed: 'Scheduler fortgesetzt',
      config_saved: 'Konfiguration gespeichert', filter_saved: 'Filter gespeichert',
      password_changed: 'Passwort geändert', config_snapshot_created: 'Snapshot erstellt',
      config_snapshot_restored: 'Snapshot wiederhergestellt', backup_requested: 'Backup angefordert',
      check_requested: 'Check angefordert', quicksync_requested: 'Quick-Sync angefordert',
      directory_created: 'Ordner angelegt', bootstrap_bundle_exported: 'Bootstrap-Paket exportiert',
      canary_initialized: 'Canary-Datei angelegt', webauthn_reauth: 'Passkey-Bestätigung',
      webauthn_registered: 'Passkey registriert', webauthn_removed: 'Passkey entfernt',
      lockdown_triggered: 'Lockdown ausgelöst', lockdown_released: 'Lockdown aufgehoben',
    })[type] || type;
  },
};
