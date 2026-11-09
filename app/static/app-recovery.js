// Wiederherstellung: Punkte, Auswahl, Staging, Diff, Quarantäne, Übergabe.
// Teil der Alpine-Komponente app(); Methoden laufen mit this = Komponente.
import { requestControllers } from './app-core.js';

export const recoveryMethods = {
  async loadRecovery() {
    this.recovery.loading = true;
    this.recovery.error = '';
    const configured = (this.config.backup?.pairs || []).filter((pair) => pair.enabled !== false && pair.name);
    if (!this.recovery.pair || !configured.some((pair) => pair.name === this.recovery.pair)) this.recovery.pair = configured[0]?.name || '';
    const tasks = [
      this.api('GET', '/api/recovery/staging', undefined, { silent: true, captureError: true, requestKey: 'recovery-staging' }),
      this.api('GET', '/api/recovery/quarantine', undefined, { silent: true, captureError: true, requestKey: 'recovery-quarantine' }),
    ];
    if (this.recovery.pair) tasks.push(this.api('GET', `/api/recovery/points?identity=${encodeURIComponent(this.recovery.pair)}`, undefined, { silent: true, captureError: true, requestKey: 'recovery-points' }));
    const [staging, quarantine, points] = await Promise.all(tasks);
    if (staging?.items) this.recovery.staging = staging.items;
    if (quarantine?.items) this.recovery.quarantine = quarantine.items;
    if (points?.points) {
      this.recovery.points = points.points;
      if (!this.recovery.points.some((point) => point.id === this.recovery.pointId)) this.recovery.pointId = this.recovery.points[0]?.id || '';
    }
    const failures = [staging, quarantine, points].filter((result) => result?.__error);
    if (failures.length) this.recovery.error = failures.map((result) => typeof result.detail === 'string' ? result.detail : result.detail?.message || 'Recovery-Daten konnten nicht vollständig geladen werden.').join(' ');
    this.recovery.loading = false;
    if (this.recovery.pair && this.recovery.pointId) await this.loadRecoveryBrowse('');
  },

  async selectRecoveryPair(name) {
    this.recovery.pair = name;
    this.recovery.pointId = '';
    this.recovery.path = '';
    this.recovery.selectedPaths = [];
    this.recovery.diff = null;
    await this.loadRecovery();
  },

  async loadRecoveryPoints() {
    if (!this.recovery.pair) return;
    const result = await this.api('GET', `/api/recovery/points?identity=${encodeURIComponent(this.recovery.pair)}`, undefined, { silent: true, captureError: true, requestKey: 'recovery-points' });
    if (this.isStale(result)) return;
    if (result?.points) {
      this.recovery.points = result.points;
      if (!result.points.some((point) => point.id === this.recovery.pointId)) this.recovery.pointId = result.points[0]?.id || '';
    }
  },

  async loadRecoveryBrowse(path = '') {
    if (!this.recovery.pair || !this.recovery.pointId) return;
    this.recovery.path = path;
    this.recovery.loading = true;
    const params = new URLSearchParams({ identity: this.recovery.pair, path });
    let endpoint;
    if (this.recovery.timeTravel && this.recovery.pointId !== 'current' && !this.recovery.pointId.startsWith('full-')) {
      params.set('point_id', this.recovery.pointId);
      endpoint = `/api/recovery/timetravel/browse?${params}`;
    } else {
      endpoint = this.recovery.pointId === 'current' ? `/api/recovery/browse?${params}` : `/api/recovery/points/${encodeURIComponent(this.recovery.pointId)}/browse?${params}`;
    }
    const result = await this.api('GET', endpoint, undefined, { silent: true, captureError: true, requestKey: 'recovery-browse', timeoutMs: 70000 });
    if (this.isStale(result)) return;
    if (result?.__error) this.recovery.error = typeof result.detail === 'string' ? result.detail : (result.detail?.message || 'Dateiliste konnte nicht geladen werden.');
    else if (result) { this.recovery.items = result.items || []; this.recovery.error = ''; }
    this.recovery.loading = false;
  },

  async loadRecoveryDiff() {
    if (!this.recovery.pair || !this.recovery.pointId || this.recovery.pointId === 'current') {
      this.recovery.diff = { changes: [], total: 0, message: 'Wähle einen gespeicherten Wiederherstellungspunkt.' };
      return;
    }
    const params = new URLSearchParams({ identity: this.recovery.pair, from_point: this.recovery.pointId, to_point: 'current' });
    const result = await this.api('GET', `/api/recovery/diff?${params}`, undefined, { silent: true, captureError: true, requestKey: 'recovery-diff', timeoutMs: 70000 });
    if (this.isStale(result)) return;
    if (result?.__error) { this.recovery.error = typeof result.detail === 'string' ? result.detail : (result.detail?.message || 'Bestände konnten nicht verglichen werden.'); return; }
    this.recovery.diff = result;
  },

  toggleRecoveryPath(path, checked) {
    const selected = new Set(this.recovery.selectedPaths);
    if (checked) selected.add(path); else selected.delete(path);
    this.recovery.selectedPaths = [...selected].slice(0, 100);
  },

  async startSelectiveRestore() {
    const selected = this.recovery.selectedPaths;
    if (!selected.length || !this.recovery.pair) return;
    if (!(await this.requestConfirmation(`Es werden ${selected.length} ausgewählte Einträge in das geschützte Staging zurückgeholt. Die produktiven Quellordner werden nicht überschrieben.`, { title: 'Wiederherstellung ins Staging starten', confirmLabel: 'Ins Staging wiederherstellen' }))) return;
    const result = await this.api('POST', '/api/recovery/restore', { identity: this.recovery.pair, paths: selected, point_id: this.recovery.pointId || 'current', max_total_mb: Number(this.recovery.maxTotalMb) || 512 });
    if (result?.ok) { this.showToast(`Recovery-Lauf #${result.job_id} gestartet`); this.recovery.selectedPaths = []; this.loadJobs(true); }
  },

  async startFullSnapshot() {
    if (!this.recovery.pair) return;
    if (!(await this.requestConfirmation('Der aktuelle Sicherungsbestand wird in einen vollständigen, lokalen Recovery-Punkt kopiert. Der Vorgang kann Speicherplatz und Zeit beanspruchen.', { title: 'Recovery-Punkt erstellen', confirmLabel: 'Recovery-Punkt erstellen' }))) return;
    const result = await this.api('POST', '/api/recovery/snapshots', { identity: this.recovery.pair, max_total_mb: Math.min(51200, Math.max(1, Number(this.recovery.maxTotalMb) || 5120)) });
    if (result?.ok) { this.showToast(`Recovery-Punkt wird erstellt (Lauf #${result.job_id})`); this.loadJobs(true); }
  },

  async startFullStagedRestore(pointId) {
    if (!this.recovery.pair || !pointId) return;
    if (!(await this.requestConfirmation('Der vollständige Stand wird geprüft und ausschließlich in ein isoliertes Staging-Verzeichnis wiederhergestellt. Die produktiven Datenwege bleiben unberührt.', { title: 'Vollständigen Stand ins Staging laden', confirmLabel: 'Ins Staging wiederherstellen' }))) return;
    const point = this.recovery.points.find((p) => p.id === pointId);
    const limit = Math.min(51200, Math.max(1, Math.ceil(Number(point?.total_bytes || 0) / 1048576) || Number(this.recovery.maxTotalMb) || 5120));
    const result = await this.api('POST', `/api/recovery/points/${encodeURIComponent(pointId)}/restore`, { identity: this.recovery.pair, max_total_mb: limit });
    if (result?.ok) { this.showToast(`Vollständige Recovery läuft (Job #${result.job_id})`); this.loadJobs(true); }
  },

  async removeRecoveryStaging(item) {
    if (!(await this.requestConfirmation('Die zurückgeholten Dateien in diesem Staging werden gelöscht. Prüfe vorher, ob du sie noch benötigst.', { title: 'Staging entfernen?', confirmLabel: 'Staging entfernen' }))) return;
    const password = await this.requestCurrentPassword('Aktuelles Passwort zur Löschung eingeben.');
    if (!password) return;
    const result = await this.api('DELETE', `/api/recovery/staging/${encodeURIComponent(item.id)}`, { current_password: password });
    if (result?.ok) await this.loadRecovery();
  },

  downloadRecoveryStaging(item) {
    if (!item?.id) return;
    window.location.assign(`/api/recovery/staging/${encodeURIComponent(item.id)}/download`);
  },

  async searchRecoveryFiles() {
    const q = String(this.recovery.searchQuery || '').trim();
    if (q.length < 2 || !this.recovery.pair) {
      this.recovery.searchResults = [];
      return;
    }
    this.recovery.searchLoading = true;
    const params = new URLSearchParams({ identity: this.recovery.pair, q, limit: '50' });
    const result = await this.api('GET', `/api/recovery/search?${params}`, undefined, {
      silent: true,
      captureError: true,
      requestKey: 'recovery-search',
      timeoutMs: 65000,
    });
    this.recovery.searchLoading = false;
    if (this.isStale(result)) return;
    if (!result || result.__error) {
      this.showToast(this.errorText(result), 'err');
      return;
    }
    this.recovery.searchResults = result.results || [];
  },

  async selectRecoverySearchResult(hit) {
    if (!hit?.point_id || !hit?.path) return;
    this.recovery.pointId = hit.point_id;
    const parent = hit.path.includes('/') ? hit.path.slice(0, hit.path.lastIndexOf('/')) : '';
    await this.loadRecoveryBrowse(parent);
    if (!this.recovery.selectedPaths.includes(hit.path)) {
      this.toggleRecoveryPath(hit.path, true);
    }
  },

  async openTimeTravelDiff(item) {
    if (!item?.path || !this.recovery.pair) return;
    const fromPoint = item.source_point || this.recovery.pointId || 'current';
    this.diffDialog = { show: true, loading: true, data: null, error: '', path: item.path };
    this.openDialog('diffDialog');
    const params = new URLSearchParams({ identity: this.recovery.pair, path: item.path, from_point: fromPoint, to_point: 'current' });
    const result = await this.api('GET', `/api/recovery/timetravel/diff?${params}`, undefined, { silent: true, captureError: true, requestKey: 'staging-diff' });
    if (this.isStale(result) || !this.diffDialog.show) return;
    this.diffDialog.loading = false;
    if (!result || result.__error) { this.diffDialog.error = result ? this.errorText(result) : ''; return; }
    this.diffDialog.data = result;
  },

  async openStagingDiff(item) {
    const path = String(this.stagingDiffPaths[item.id] || '').trim();
    if (!path) return;
    this.diffDialog = { show: true, loading: true, data: null, error: '', path };
    this.openDialog('diffDialog');
    const result = await this.api('GET', `/api/recovery/staging/${encodeURIComponent(item.id)}/diff?path=${encodeURIComponent(path)}`, undefined, { silent: true, captureError: true, requestKey: 'staging-diff' });
    if (this.isStale(result) || !this.diffDialog.show) return;
    this.diffDialog.loading = false;
    if (!result || result.__error) { this.diffDialog.error = result ? this.errorText(result) : ''; return; }
    this.diffDialog.data = result;
  },

  closeDiff() {
    if (!this.diffDialog.show) return;
    requestControllers.get('staging-diff')?.abort();
    this.diffDialog = { show: false, loading: false, data: null, error: '', path: '' };
    this.restoreDialogFocus();
  },

  async acknowledgeQuarantine(item) {
    if (!(await this.requestConfirmation(`Quarantäne für „${item.pair}“ aufheben? Dateirückgang: ${item.file_drop_percent ?? '–'} %, Größenrückgang: ${item.size_drop_percent ?? '–'} %. Prüfe Mount, Quellpfad und Bestand außerhalb der Anwendung. Die Freigabe erlaubt nachfolgende produktive Läufe einschließlich Löschungen.`, { title: 'Quarantäne wirklich aufheben?', confirmLabel: 'Geprüfte Quelle freigeben' }))) return;
    const password = await this.requestCurrentPassword('Aktuelles Passwort zur Freigabe der Quarantäne eingeben.');
    if (!password) return;
    const result = await this.api('POST', `/api/recovery/quarantine/${encodeURIComponent(item.pair)}/acknowledge`, { current_password: password });
    if (result?.ok) { this.showToast('Quarantäne bestätigt'); await this.loadRecovery(); }
  },

  async downloadRecoveryHandover() {
    const handover = this.recovery.handover;
    if (handover.passphrase.length < 12 || !handover.password) { this.showToast('Aktuelles Passwort und Passphrase mit mindestens 12 Zeichen eingeben.', 'err'); return; }
    if (handover.passphrase !== handover.repeat) { this.showToast('Die Passphrasen stimmen nicht überein.', 'err'); return; }
    if (location.protocol === 'http:' && !(await this.requestConfirmation('Diese Verbindung ist unverschlüsselt. Serverpasswort und Paket-Passphrase werden im Klartext an den Server übertragen. Verwende nach Möglichkeit HTTPS.', { title: 'Unverschlüsselte Verbindung', confirmLabel: 'Bewusst über HTTP fortfahren' }))) return;
    try {
      const response = await fetch('/api/recovery/handover', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': this.cookie('rclone_sync_csrf') },
        body: JSON.stringify({ current_password: handover.password, passphrase: handover.passphrase, include_paths: handover.includePaths }),
      });
      if (!response.ok) {
        let message = `Serverfehler (HTTP ${response.status})`;
        try { const payload = await response.json(); if (typeof payload.detail === 'string') message = payload.detail; } catch (_) {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = 'recovery-handover.encrypted.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      handover.downloaded = true;
      handover.password = '';
      this.showToast('Verschlüsseltes Übergabepaket heruntergeladen');
    } catch (error) { this.showToast(error.message || 'Übergabepaket konnte nicht erstellt werden.', 'err'); }
  },

  printHandoverKey() {
    if (!this.recovery.handover.passphrase) { this.showToast('Zuerst eine Passphrase eingeben.', 'err'); return; }
    const printWindow = window.open('', '_blank', 'width=640,height=520');
    if (!printWindow) { this.showToast('Druckfenster wurde vom Browser blockiert.', 'err'); return; }
    printWindow.opener = null;
    const escaped = this.recovery.handover.passphrase.replace(/[&<>"']/g, (char) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));
    printWindow.document.write(`<!doctype html><meta charset="utf-8"><title>Passphrase für Sicherpfad-Notfallpaket</title><h1>Passphrase für das Notfallpaket</h1><p>Getrennt vom verschlüsselten Paket verwahren und nur der bevollmächtigten Person übergeben.</p><pre>${escaped}</pre>`);
    printWindow.document.close();
    printWindow.focus();
    printWindow.print();
  },

  recoveryBreadcrumbs() {
    const parts = String(this.recovery.path || '').split('/').filter(Boolean);
    return [{ label: 'Wurzel', path: '' }, ...parts.map((part, index) => ({ label: part, path: parts.slice(0, index + 1).join('/') }))];
  },
};
