function app() {
  return {
    page: 'dashboard',
    status: { backup: null },
    progress: null,
    recentJobs: [],
    jobs: [],
    config: { web: {}, paths: {}, backup: { pairs: [], rclone_args: [] } },
    rcloneArgsText: '',
    testResults: {},
    logModal: { show: false, id: null, text: '' },
    picker: {
      show: false, mode: null, idx: null,
      current: '', parent: null, entries: [], loading: false,
    },
    pwChange: { current: '', new: '', confirm: '' },
    toast: { show: false, msg: '', type: 'ok' },

    init() {
      this.refreshStatus();
      this.loadRecent();
      setInterval(() => this.refreshStatus(), 3000);
      setInterval(() => {
        if (this.status.backup) this.loadProgress();
        else if (this.progress?.running) this.loadProgress();
      }, 2000);
    },

    async api(method, url, body) {
      try {
        const opts = { method, credentials: 'include', headers: {} };
        if (body !== undefined) {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        if (!r.ok) {
          if (r.status === 401) { window.location = '/login'; return null; }
          const err = await r.json().catch(() => ({}));
          this.showToast('Fehler: ' + (err.detail || r.statusText), 'err');
          return null;
        }
        return r.headers.get('content-type')?.includes('json') ? await r.json() : await r.text();
      } catch (e) {
        this.showToast('Netzwerkfehler: ' + e.message, 'err');
        return null;
      }
    },

    showToast(msg, type = 'ok') {
      this.toast = { show: true, msg, type };
      setTimeout(() => this.toast.show = false, 3500);
    },

    async refreshStatus() {
      const r = await this.api('GET', '/api/jobs/status/current');
      if (r) this.status = r;
    },

    async loadRecent() {
      const r = await this.api('GET', '/api/jobs/list?limit=8');
      if (r) this.recentJobs = r;
    },

    async loadJobs() {
      const r = await this.api('GET', '/api/jobs/list?limit=50');
      if (r) this.jobs = r;
    },

    async loadProgress() {
      const r = await this.api('GET', '/api/jobs/backup/progress');
      if (r) this.progress = r;
    },

    async runBackup(dryRun) {
      const r = await this.api('POST', `/api/jobs/backup/run?dry_run=${dryRun}`);
      if (r?.ok) {
        this.showToast(dryRun ? 'Dry-Run gestartet' : 'Backup gestartet');
        setTimeout(() => { this.refreshStatus(); this.loadProgress(); }, 500);
      }
    },

    async cancelBackup() {
      if (!confirm('Backup abbrechen?')) return;
      const r = await this.api('POST', '/api/jobs/backup/cancel');
      if (r?.ok) this.showToast('Cancel-Signal gesendet');
    },

    async runSinglePair(name) {
      if (!confirm(`Nur Paar "${name}" syncen?`)) return;
      const r = await this.api('POST', `/api/jobs/backup/run-pair/${encodeURIComponent(name)}`);
      if (r?.ok) this.showToast(`"${name}" gestartet`);
    },

    async showLog(jobId) {
      this.logModal = { show: true, id: jobId, text: 'lädt…' };
      const r = await this.api('GET', `/api/jobs/${jobId}/log?tail=2000`);
      this.logModal.text = r?.log || '<leer>';
    },

    async cleanupFailed() {
      if (!confirm('Alle fehlgeschlagenen Jobs aus DB löschen?')) return;
      const r = await this.api('POST', '/api/jobs/cleanup-failed');
      if (r?.ok) {
        this.showToast(`${r.deleted} gelöscht`);
        this.loadJobs();
      }
    },

    async loadConfig() {
      const r = await this.api('GET', '/api/config');
      if (!r) return;
      // Defaults setzen damit Bindings nicht crashen
      r.web ||= {};
      r.paths ||= {};
      r.backup ||= {};
      r.backup.pairs ||= [];
      r.backup.rclone_args ||= [];
      this.config = r;
      this.rcloneArgsText = (r.backup.rclone_args || []).join('\n');
    },

    async saveConfig() {
      // rcloneArgsText → array
      this.config.backup.rclone_args = this.rcloneArgsText
        .split('\n').map(s => s.trim()).filter(Boolean);
      const r = await this.api('PUT', '/api/config', { config: this.config });
      if (r?.ok) this.showToast('✓ gespeichert');
    },

    addPair() {
      this.config.backup.pairs.push({ name: '', remote: '', local: '', schedule: '' });
    },

    async testRclone() {
      this.testResults._global = { loading: true };
      const r = await this.api('POST', '/api/test/rclone', {});
      this.testResults._global = r;
    },

    async testPair(idx) {
      this.testResults[idx] = { loading: true };
      const r = await this.api('POST', '/api/test/rclone', { pair_index: idx });
      this.testResults[idx] = r;
    },

    // ─── Folder-Picker ──────────────────────────────────────────────────
    openPicker(mode, idx) {
      // mode: 'remote' (rclone) | 'local' (FS)
      this.picker = {
        show: true, mode, idx,
        current: '', parent: null, entries: [], loading: true,
      };
      this.loadPicker('');
    },
    async loadPicker(path) {
      this.picker.loading = true;
      this.picker.current = path;
      const endpoint = this.picker.mode === 'remote' ? '/api/browse/rclone' : '/api/browse/local';
      const r = await this.api('GET', endpoint + (path ? '?path=' + encodeURIComponent(path) : ''));
      if (r) {
        this.picker.parent = r.parent;
        this.picker.entries = r.entries || [];
        this.picker.current = r.path || path || '';
      }
      this.picker.loading = false;
    },
    pickPath(path) {
      const { mode, idx } = this.picker;
      if (mode === 'remote') this.config.backup.pairs[idx].remote = path;
      else this.config.backup.pairs[idx].local = path;
      this.picker.show = false;
      this.showToast(`Pfad gesetzt: ${path}`);
    },

    // ─── Passwort ändern ────────────────────────────────────────────────
    async changePassword() {
      if (this.pwChange.new !== this.pwChange.confirm) {
        this.showToast('Wiederholung passt nicht', 'err'); return;
      }
      if (this.pwChange.new.length < 8) {
        this.showToast('Min. 8 Zeichen', 'err'); return;
      }
      const r = await this.api('POST', '/api/config/change-password', {
        current_password: this.pwChange.current,
        new_password: this.pwChange.new,
      });
      if (r?.ok) {
        this.showToast('✓ Passwort geändert');
        this.pwChange = { current: '', new: '', confirm: '' };
      }
    },

    formatTs(t) {
      if (!t) return '—';
      return new Date(t * 1000).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' });
    },
    formatDur(s) {
      if (!s) return '—';
      s = Math.round(s);
      if (s < 60) return s + 's';
      const m = Math.floor(s / 60);
      if (m < 60) return m + 'm ' + (s % 60) + 's';
      return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    },
    summaryShort(s) {
      if (!s) return '—';
      if (s.ok_count !== undefined) return `${s.ok_count}/${s.total_pairs} Paare${s.dry_run ? ' (dry)' : ''}`;
      if (s.error) return '✗ ' + s.error.substring(0, 80);
      return JSON.stringify(s).substring(0, 80);
    },
  };
}
