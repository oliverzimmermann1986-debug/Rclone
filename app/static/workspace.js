/* Organize the existing console before Alpine initializes. Original controls and
 * bindings stay on their original nodes; the server remains authoritative. */
(() => {
  'use strict';

  if (typeof window.app !== 'function' || document.body.dataset.workspaceReady) return;
  const originalApp = window.app;
  const pageGroups = new Map();
  const defaults = {};
  const page = (name) => Array.from(document.querySelectorAll('main section[x-show]'))
    .find((section) => section.getAttribute('x-show').replace(/\s/g, '') === `page==='${name}'`);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const headingText = (node) => node.querySelector('h3')?.textContent.trim() || '';
  const isNotice = (node) => node.matches('.alert, .alert-stack, .inline-error, .config-check, .loading-state, .skeleton-grid');

  function groupPage(name, definitions, classify, always = new Set()) {
    const section = page(name);
    if (!section) return;
    const heading = Array.from(section.children).find((node) => node.classList.contains('page-heading'));
    if (!heading) return;
    const nodes = Array.from(section.children).filter((node) => node !== heading && !always.has(node) && !isNotice(node));
    const groups = definitions.map(([id, label, description]) => ({ id, label, description, nodes: [] }));
    for (const node of nodes) {
      const group = groups.find((candidate) => candidate.id === classify(node)) || groups[0];
      group.nodes.push(node);
    }
    const populated = groups.filter((group) => group.nodes.length);
    if (populated.length < 2) return;
    defaults[name] = populated[0].id;
    pageGroups.set(name, populated);
    section.classList.add('workspace-page');
    section.dataset.workspacePage = name;
    const tabs = element('nav', 'workspace-tabs');
    tabs.setAttribute('aria-label', `${heading.querySelector('h2')?.textContent || name}: Bereiche`);
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('x-on:keydown', `workspaceTabKey($event, '${name}')`);
    for (const group of populated) {
      const selected = `workspaceViews.${name} === '${group.id}'`;
      const button = element('button', 'workspace-tab');
      button.type = 'button';
      button.id = `workspace-tab-${name}-${group.id}`;
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-controls', `workspace-panel-${name}-${group.id}`);
      button.setAttribute(':aria-selected', selected);
      button.setAttribute(':tabindex', `${selected} ? 0 : -1`);
      button.setAttribute('x-on:click', `workspaceSelect('${name}', '${group.id}')`);
      button.append(element('span', '', group.label));
      if (name === 'recovery' && group.id === 'quarantine') {
        const count = element('span', 'workspace-tab-count');
        count.setAttribute('x-show', 'recovery.quarantine.length');
        count.setAttribute('x-text', 'recovery.quarantine.length');
        count.setAttribute('aria-label', 'aktive Quarantänen');
        button.append(count);
      }
      tabs.append(button);
      const panel = element('div', 'workspace-view');
      panel.id = `workspace-panel-${name}-${group.id}`;
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', button.id);
      panel.setAttribute('tabindex', '0');
      panel.setAttribute('x-show', selected);
      panel.setAttribute('x-cloak', '');
      panel.append(element('p', 'workspace-view-intro', group.description));
      for (const node of group.nodes) panel.append(node);
      section.append(panel);
    }
    heading.after(tabs);
  }

  function organizeDashboard() {
    const section = page('dashboard');
    if (!section) return;
    const grid = section.querySelector(':scope > .dashboard-grid');
    if (grid) {
      for (const child of Array.from(grid.children)) grid.before(child);
      grid.remove();
    }
    const always = new Set();
    const live = section.querySelector('.control-panel .live-progress');
    if (live) {
      const progress = element('article', 'panel workspace-live');
      progress.setAttribute('x-show', 'rcloneBusy() || progress?.running');
      progress.setAttribute('x-cloak', '');
      progress.append(live);
      const alerts = section.querySelector(':scope > .alert-stack');
      if (alerts) alerts.after(progress);
      else section.append(progress);
      always.add(progress);
    }
    const attention = element('div', 'workspace-attention');
    attention.setAttribute('x-show', 'workspaceIssues().length');
    attention.setAttribute('x-cloak', '');
    attention.setAttribute('role', 'status');
    const copy = element('div');
    copy.append(element('strong', '', 'Das braucht Aufmerksamkeit'));
    const list = element('ul');
    const template = element('template');
    template.setAttribute('x-for', '(issue, issueIndex) in workspaceIssues()');
    template.setAttribute(':key', 'issueIndex');
    const line = element('li');
    line.setAttribute('x-text', 'issue');
    template.content.append(line);
    list.append(template);
    copy.append(list);
    const button = element('button', 'button secondary small', 'Datenwege prüfen');
    button.type = 'button';
    button.setAttribute('x-on:click', "workspaceSelect('dashboard', 'paths')");
    attention.append(copy, button);
    section.querySelector(':scope > .page-heading')?.after(attention);
    always.add(attention);
    const scheduler = section.querySelector(':scope > .scheduler-control-strip');
    if (scheduler) always.add(scheduler);
    groupPage('dashboard', [
      ['overview', 'Überblick', 'Schutzstatus, Sicherungskopien und die letzten Läufe.'],
      ['paths', 'Datenweg-Status', 'Ergebnis und nächste Schritte je Datenweg.'],
      ['control', 'Manuell ausführen', 'Vor einem produktiven Lauf zuerst den Plan oder einen Probelauf prüfen.'],
      ['resources', 'Serverdaten', 'Ressourcen und technische Informationen dieses Servers.'],
    ], (node) => {
      if (node.classList.contains('control-panel') || node.querySelector('#pbs-targets-heading')) return 'control';
      if (node.classList.contains('system-panel')) return 'resources';
      if (node.querySelector('.health-grid')) return 'paths';
      return 'overview';
    }, always);
  }

  function organizeSystem() {
    groupPage('doctor', [
      ['health', 'Zustand', 'Dienste und Schutzregeln prüfen.'],
      ['copies', 'Kopien & Schutz', 'Sicherungsziele und offene Schutzlücken je Datenbestand.'],
      ['maintenance', 'Wartung', 'Logs, Datenbank und geschützte Konfigurationsstände verwalten.'],
      ['audit', 'Aktivitäten', 'Bedien- und Konfigurationsaktionen nachvollziehen.'],
    ], (node) => {
      if (node.classList.contains('audit-panel')) return 'audit';
      if (node.classList.contains('recovery-panel') || /Logs und Datenbank/.test(headingText(node))) return 'maintenance';
      if (node.querySelector('.copy-matrix, .copy-table') || /Kopien je Datenbestand/.test(headingText(node))) return 'copies';
      return 'health';
    });
  }

  function organizeRecovery() {
    const section = page('recovery');
    if (!section) return;
    const secondary = section.querySelector(':scope > .recovery-secondary-grid');
    if (secondary) {
      for (const child of Array.from(secondary.children)) secondary.before(child);
      secondary.remove();
    }
    groupPage('recovery', [
      ['restore', 'Dateien zurückholen', 'Stand und Dateien auswählen. Wiederherstellungen bleiben zuerst im Staging.'],
      ['staging', 'Zurückgeholte Dateien', 'Ergebnis, Prüfstatus und Ablageort deiner Wiederherstellungen.'],
      ['quarantine', 'Quarantäne', 'Unerwartete Quellenänderungen prüfen, bevor produktive Läufe wieder freigegeben werden.'],
      ['handover', 'Notfallpaket', 'Eine verschlüsselte Übergabeakte für erreichbare Vault-Zielkopien erstellen.'],
    ], (node) => {
      const title = headingText(node);
      if (/Staging-Bereich/.test(title)) return 'staging';
      if (/Quarantäne/.test(title)) return 'quarantine';
      if (node.classList.contains('handover-panel') || /Notfallpaket/.test(title)) return 'handover';
      // Unknown newer restore-plan controls deliberately remain in the default view.
      return 'restore';
    });
    const warning = element('div', 'workspace-attention workspace-quarantine-notice');
    warning.setAttribute('x-show', 'recovery.quarantine.length && workspaceViews.recovery !== \'quarantine\'');
    warning.setAttribute('x-cloak', '');
    warning.setAttribute('role', 'status');
    const message = element('strong');
    message.setAttribute('x-text', "recovery.quarantine.length + ' aktive Quarantäne(n): Quellenänderungen müssen geprüft werden.'");
    const action = element('button', 'button secondary small', 'Quarantäne prüfen');
    action.type = 'button';
    action.setAttribute('x-on:click', "workspaceSelect('recovery', 'quarantine')");
    warning.append(message, action);
    section.querySelector(':scope > .workspace-tabs')?.after(warning);
  }

  function disclosure(node, title, description, openExpression) {
    const details = element('details', 'workspace-disclosure');
    const summary = element('summary');
    const label = element('span');
    label.append(element('strong', '', title));
    if (description) label.append(element('small', '', description));
    summary.append(label);
    details.append(summary);
    if (openExpression) details.setAttribute(':open', openExpression);
    node.before(details);
    details.append(node);
    return details;
  }

  function organizeEditors() {
    const jobs = page('definitions');
    const template = jobs?.querySelector('.definition-list > template');
    if (template) {
      const root = template.content;
      const assignments = root.querySelector('.definition-assignments');
      if (assignments) {
        const details = disclosure(assignments, 'Datenwege & Ausführung', '', '!job.id');
        const count = element('small');
        count.setAttribute('x-text', "job.data_path_ids.length + ' Datenweg(e) zugewiesen'");
        details.querySelector('summary > span').append(count);
        const options = root.querySelector('.definition-options');
        if (options) details.append(options.closest('details') || options);
      }
    }
    const pairs = page('pairs');
    const pairTemplate = pairs?.querySelector('.pair-card-list > template');
    if (pairTemplate) {
      const root = pairTemplate.content;
      const actions = root.querySelector('.pair-quick-actions');
      if (actions) {
        const buttons = Array.from(actions.children).filter((node) => node.tagName === 'BUTTON');
        if (buttons.length > 2) {
          const details = element('details', 'workspace-action-menu');
          details.append(element('summary', '', 'Weitere Aktionen'));
          const list = element('div', 'workspace-action-menu-items');
          for (const button of buttons.slice(1)) list.append(button);
          details.append(list);
          buttons[0].after(details);
        }
      }
    }
    const scheduler = document.querySelector('#settings-panel-scheduler .scheduler-main');
    if (scheduler) {
      for (const section of Array.from(scheduler.children)) {
        if (!section.matches('.settings-section') || section.matches('.scheduler-operation-section, .schedule-routing, .schedule-section')) continue;
        const title = headingText(section);
        if (title) disclosure(section, title, 'Bei Bedarf öffnen und anpassen');
      }
    }
    const general = document.getElementById('settings-panel-general');
    const advancedRoots = general?.querySelector('textarea[x-model="allowedHostsText"]')?.closest('.form-grid');
    if (advancedRoots) disclosure(advancedRoots, 'Hosts & Ordnerfreigaben', 'Zugriff und erlaubte Dateipfade des Servers');
  }

  organizeDashboard();
  organizeSystem();
  organizeRecovery();
  organizeEditors();
  // Capture adds a view destination without replacing any original action.
  const copiesLink = page('dashboard')?.querySelector('.copy-matrix-summary button');
  if (copiesLink?.getAttribute('@click')?.includes("navigate('doctor')")) {
    copiesLink.setAttribute('x-on:click.capture', "workspaceDestination = { page: 'doctor', view: 'copies' }");
  }
  for (const button of page('doctor')?.querySelectorAll('.page-heading button') || []) {
    if (button.getAttribute('@click')?.includes('loadDoctor()')) {
      button.setAttribute('x-on:click.capture', "workspaceSelect('doctor', 'health')");
    }
  }

  window.app = function workspaceApp(...args) {
    const model = originalApp.apply(this, args);
    const navigate = model.navigate;
    const init = model.init;
    return Object.assign(model, {
      workspaceViews: { ...defaults },
      workspaceDestination: null,
      async init(...initArgs) {
        this.$watch('recovery.diff', (value) => {
          if (value && this.page === 'recovery') this.workspaceSelect('recovery', 'restore');
        });
        return init.apply(this, initArgs);
      },
      async navigate(next, ...navigateArgs) {
        const previous = this.page;
        const destination = this.workspaceDestination?.page === next ? this.workspaceDestination : null;
        this.workspaceDestination = null;
        const result = await navigate.call(this, next, ...navigateArgs);
        if (this.page !== previous && defaults[this.page]) this.workspaceViews[this.page] = defaults[this.page];
        if (this.page === next && destination) this.workspaceSelect(next, destination.view);
        return result;
      },
      workspaceSelect(name, id) {
        const group = pageGroups.get(name)?.find((item) => item.id === id);
        if (!group) return;
        // x-show can move focus to body while hiding the old view. Remember
        // where focus was before the reactive visibility change happens.
        const previousPanel = document.activeElement?.closest('.workspace-view');
        const restoreFocus = previousPanel && previousPanel.id !== `workspace-panel-${name}-${id}`;
        this.workspaceViews[name] = id;
        this.$nextTick(() => {
          if (restoreFocus) {
            document.getElementById(`workspace-tab-${name}-${id}`)?.focus();
          }
        });
      },
      workspaceTabKey(event, name) {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const groups = pageGroups.get(name) || [];
        if (!groups.length) return;
        const current = groups.findIndex((group) => group.id === this.workspaceViews[name]);
        let index = current;
        if (event.key === 'Home') index = 0;
        else if (event.key === 'End') index = groups.length - 1;
        else index = (current + (event.key === 'ArrowRight' ? 1 : -1) + groups.length) % groups.length;
        event.preventDefault();
        this.workspaceSelect(name, groups[index].id);
        document.getElementById(`workspace-tab-${name}-${groups[index].id}`)?.focus();
      },
      workspaceIssues() {
        const issues = [];
        for (const health of this.overview.data?.pairs?.health || []) {
          if (health.error || ['error', 'failed', 'timeout', 'stale', 'skipped'].includes(health.last_status)) {
            issues.push(`${health.name}: ${health.error || this.statusLabel(health.last_status)}`);
          }
        }
        for (const pair of this.storagePairs?.() || []) {
          if (pair.restore_evidence?.state === 'failed' && !issues.some((issue) => issue.startsWith(`${pair.name}:`))) {
            issues.push(`${pair.name}: Letzter Restore-Test fehlgeschlagen`);
          }
        }
        return issues;
      },
    });
  };
  document.body.dataset.workspaceReady = 'true';
})();
