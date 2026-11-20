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

  function actionMenu(nodes, label, className = '') {
    const details = element('details', `workspace-action-menu console-action-menu ${className}`.trim());
    details.append(element('summary', '', label));
    const items = element('div', 'workspace-action-menu-items console-action-menu-items');
    for (const node of nodes) items.append(node);
    details.append(items);
    return details;
  }

  function sectionHeading(title, meta) {
    const heading = element('div', 'console-section-heading');
    heading.append(element('h3', '', title));
    if (meta) heading.append(meta);
    return heading;
  }

  function groupPage(name, definitions, classify, always = new Set()) {
    const section = page(name);
    if (!section) return;
    const heading = Array.from(section.children).find((node) => node.classList.contains('page-heading'));
    if (!heading) return;
    const nodes = Array.from(section.children).filter((node) => node !== heading && !always.has(node) && !isNotice(node));
    const groups = definitions.map(([id, label, description]) => ({ id, label, description, nodes: [] }));
    for (const node of nodes) {
      const explicitGroup = node.getAttribute('data-workspace-group');
      const targetId = explicitGroup || classify(node);
      const group = groups.find((candidate) => candidate.id === targetId) || groups[0];
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
      const panel = element('div', `workspace-view workspace-view-${name}-${group.id}`);
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
    const metrics = section.querySelector(':scope > .metric-grid');
    const lead = metrics?.firstElementChild;
    if (lead && metrics.children.length > 1) {
      lead.classList.add('overview-lead');
      metrics.classList.add('overview-facts');
      metrics.before(lead);
      always.add(lead);
    }
    section.querySelector('.copy-overview')?.classList.add('overview-primary');
    section.querySelector('.copy-matrix-summary')?.classList.add('overview-secondary');
    const live = section.querySelector('.control-panel .live-progress');
    if (live) {
      const progress = element('article', 'panel workspace-live overview-live');
      progress.setAttribute('x-show', 'rcloneBusy() || progress?.running');
      progress.setAttribute('x-cloak', '');
      progress.append(live);
      const alerts = section.querySelector(':scope > .alert-stack');
      if (alerts) alerts.after(progress);
      else section.append(progress);
      always.add(progress);
    }
    const attention = element('div', 'workspace-attention overview-attention');
    attention.setAttribute('x-show', 'workspaceIssues().length');
    attention.setAttribute('x-cloak', '');
    attention.setAttribute('role', 'status');
    const copy = element('div', 'workspace-issue-list');
    copy.append(element('strong', '', 'Das braucht Aufmerksamkeit'));
    const list = element('ul');
    const template = element('template');
    template.setAttribute('x-for', 'issue in workspaceIssues()');
    template.setAttribute(':key', 'issue.id');
    const line = element('li', 'workspace-issue');
    const message = element('span');
    message.setAttribute('x-text', 'issue.message');
    const action = element('button', 'button secondary small');
    action.type = 'button';
    action.setAttribute('x-text', 'issue.actionLabel');
    action.setAttribute(':aria-label', "issue.actionLabel + ': ' + issue.pair");
    action.setAttribute(':disabled', "issue.action === 'restore' && startBlocked()");
    action.setAttribute('x-on:click', 'workspaceIssueAction(issue)');
    line.append(message, action);
    template.content.append(line);
    list.append(template);
    copy.append(list);
    const button = element('button', 'button secondary small', 'Datenwege prüfen');
    button.type = 'button';
    button.setAttribute('x-on:click', "workspaceSelect('dashboard', 'paths')");
    attention.append(copy, button);
    section.querySelector(':scope > .page-heading')?.after(attention);
    always.add(attention);
    groupPage('dashboard', [
      ['overview', 'Überblick', 'Schutzstatus, Sicherungskopien und die letzten Läufe.'],
      ['paths', 'Datenweg-Status', 'Ergebnis und nächste Schritte je Datenweg.'],
      ['control', 'Ausführen', 'Hier kannst du Läufe starten und die Automatik pausieren.'],
      ['resources', 'Serverdaten', 'Ressourcen und technische Informationen dieses Servers.'],
    ], (node) => {
      if (node.classList.contains('control-panel') || node.querySelector('#pbs-targets-heading')) return 'control';
      if (node.classList.contains('system-panel')) return 'resources';
      if (node.querySelector('.health-grid')) return 'paths';
      return 'overview';
    }, always);
    // Keep warnings and active execution ahead of optional views, in the same
    // reading order on desktop and mobile. No state is inferred from layout.
    let anchor = section.querySelector(':scope > .page-heading');
    for (const node of [
      lead,
      section.querySelector(':scope > .skeleton-grid'),
      section.querySelector(':scope > .alert-stack'),
      attention,
      section.querySelector(':scope > .workspace-live'),
      section.querySelector(':scope > .workspace-tabs'),
    ].filter(Boolean)) {
      if (anchor) anchor.after(node);
      anchor = node;
    }
    const overview = section.querySelector('#workspace-panel-dashboard-overview');
    const copies = overview?.querySelector('.copy-overview');
    const summary = overview?.querySelector('.overview-facts');
    if (copies && summary) summary.after(copies);
    const matrix = overview?.querySelector('.copy-matrix-summary');
    const recent = overview?.querySelector('.job-list')?.closest('article');
    if (matrix && recent) {
      const followup = element('div', 'overview-followup');
      matrix.before(followup);
      recent.classList.add('overview-recent');
      followup.append(matrix, recent);
    }
  }

  function organizeSystem() {
    groupPage('doctor', [
      ['health', 'Zustand', 'Dienste und Schutzregeln prüfen.'],
      ['copies', 'Kopien & Schutz', 'Sicherungsziele und offene Schutzlücken je Datenbestand.'],
      ['maintenance', 'Wartung', 'Logs, Datenbank und geschützte Konfigurationsstände verwalten.'],
      ['audit', 'Aktivitäten', 'Bedien- und Konfigurationsaktionen nachvollziehen.'],
    ], (node) => {
      if (node.classList.contains('audit-panel')) return 'audit';
      if (node.classList.contains('recovery-panel') || node.classList.contains('maintenance-panel') || /Logs und Datenbank/.test(headingText(node))) return 'maintenance';
      if (node.classList.contains('copies-panel') || node.querySelector('.copy-matrix, .copy-table') || /Kopien je Datenbestand|Bitrot-Scrub/.test(headingText(node))) return 'copies';
      return 'health';
    });
    const health = document.getElementById('workspace-panel-doctor-health');
    const findings = health?.querySelector('article[data-workspace-group="health"][x-show="doctor.data"]');
    findings?.classList.add('system-findings');
    health?.querySelector('.metric-grid')?.classList.add('system-status-strip');
    if (findings) health.querySelector('.workspace-view-intro')?.after(findings);
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
      ['restore', 'Dateien zurückholen', 'Zurückgeholte Dateien landen zuerst in einem separaten Ordner.'],
      ['staging', 'Zurückgeholte Dateien', 'Ergebnis, Prüfstatus und Ablageort deiner Wiederherstellungen.'],
      ['quarantine', 'Quarantäne', 'Unerwartete Quellenänderungen prüfen, bevor produktive Läufe wieder freigegeben werden.'],
      ['handover', 'Notfallpaket', 'Eine verschlüsselte Übergabeakte für erreichbare Vault-Zielkopien erstellen.'],
    ], (node) => {
      const title = headingText(node);
      if (node.classList.contains('staging-panel') || /Staging-Bereich/.test(title)) return 'staging';
      if (node.classList.contains('quarantine-panel') || /Quarantäne/.test(title)) return 'quarantine';
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
      root.querySelector('.definition-row')?.classList.add('job-definition-record');
      const assignments = root.querySelector('.definition-assignments');
      if (assignments) {
        const pickerDetails = assignments.querySelector('details.assignment-picker');
        if (pickerDetails) {
          const flatPicker = element('div', 'assignment-picker');
          const pickerSummary = pickerDetails.querySelector(':scope > summary');
          if (pickerSummary) {
            const head = element('div', 'assignment-picker-head');
            while (pickerSummary.firstChild) head.append(pickerSummary.firstChild);
            flatPicker.append(head);
            pickerSummary.remove();
          }
          while (pickerDetails.firstChild) flatPicker.append(pickerDetails.firstChild);
          pickerDetails.replaceWith(flatPicker);
        }
        const details = disclosure(assignments, 'Datenwege & Ausführung', '', '!job.id');
        const count = element('small');
        count.setAttribute('x-text', "job.data_path_ids.length + ' Datenweg(e) zugewiesen'");
        details.querySelector('summary > span').append(count);
        const options = root.querySelector('.definition-options');
        if (options) {
          const optionsDetails = options.closest('details');
          if (optionsDetails && optionsDetails !== details) {
            const summaryText = optionsDetails.querySelector(':scope > summary')?.textContent.trim();
            if (options === optionsDetails) {
              const flatOptions = element('div', 'definition-options');
              if (summaryText) flatOptions.append(element('h4', 'subheading', summaryText));
              optionsDetails.querySelector(':scope > summary')?.remove();
              while (optionsDetails.firstChild) flatOptions.append(optionsDetails.firstChild);
              details.append(flatOptions);
              optionsDetails.remove();
            } else {
              if (summaryText && !options.querySelector('h4, .subheading')) {
                options.prepend(element('h4', 'subheading', summaryText));
              }
              details.append(options);
              optionsDetails.remove();
            }
          } else {
            details.append(options);
          }
        }
      }
    }
    const pairs = page('pairs');
    const pairTemplate = pairs?.querySelector('.pair-card-list > template');
    if (pairTemplate) {
      const root = pairTemplate.content;
      const card = root.querySelector('.pair-card');
      const top = root.querySelector('.pair-card-top');
      if (card && top) {
        card.classList.add('data-path-record');
        const summary = element('div', 'data-path-summary');
        top.after(summary);
        for (const selector of ['.pair-tags', '.pair-warning-row', '.pair-size-row', '.pair-quick-actions']) {
          const node = root.querySelector(selector);
          if (node) summary.append(node);
        }
        const composition = root.querySelector('.composition-detail');
        const footer = root.querySelector('.pair-footer-actions');
        if (composition && footer) footer.before(composition);
      }
      const actions = root.querySelector('.pair-quick-actions');
      if (actions) {
        const buttons = Array.from(actions.children).filter((node) => node.tagName === 'BUTTON');
        if (buttons.length > 2) {
          const details = actionMenu(buttons.slice(1), 'Weitere Aktionen');
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

  function organizeDataPaths() {
    const section = page('pairs');
    const list = section?.querySelector('.pair-card-list');
    const toolbar = section?.querySelector(':scope > .toolbar');
    if (!list || !toolbar) return;
    toolbar.classList.add('data-paths-toolbar');
    const meta = toolbar.querySelector('.toolbar-meta');
    const tools = [toolbar.querySelector('.pair-template-action'), toolbar.querySelector('.toolbar-actions')].filter(Boolean);
    if (tools.length) toolbar.append(actionMenu(tools, 'Ansicht & Vorlagen', 'data-paths-options'));
    const heading = sectionHeading('Deine Datenwege', meta);
    toolbar.before(heading);
    list.classList.add('records-list');
  }

  function organizeJobPlanning() {
    const section = page('definitions');
    const list = section?.querySelector('.definition-list');
    if (!list) return;
    const workbench = element('div', 'definitions-workbench records-section');
    const count = element('span', 'records-meta');
    count.setAttribute('x-text', "(config.backup.jobs || []).length + ' konfiguriert'");
    list.before(workbench);
    workbench.append(sectionHeading('Deine Jobs', count), list);
    const empty = section.querySelector(':scope > .empty-state');
    if (empty) workbench.append(empty);
    const timeline = section.querySelector('.schedule-timeline');
    timeline?.classList.add('definitions-schedule');
    const bandwidth = section.querySelector('[aria-labelledby="bandwidth-heading"]');
    if (bandwidth) {
      const details = disclosure(bandwidth, 'Bandbreite & Zeitfenster', 'Vergangene Übertragungen auswerten und Zeitplan-Vorschläge prüfen');
      details.classList.add('console-disclosure', 'definitions-analysis');
    }
  }

  function organizeHistory() {
    const section = page('runs');
    const toolbar = section?.querySelector('.jobs-toolbar');
    const results = section?.querySelector(':scope > article.panel.no-padding');
    if (!toolbar || !results) return;
    section.querySelector(':scope > .metric-grid')?.classList.add('runs-summary');
    const workbench = element('div', 'runs-workbench records-section');
    const heading = sectionHeading('Ausführungsverlauf', toolbar.querySelector('.toolbar-meta'));
    toolbar.before(workbench);
    workbench.append(heading, toolbar, results);
    const search = toolbar.querySelector('.job-search');
    const chips = toolbar.querySelector('.job-filter-chips');
    const primary = element('div', 'runs-filter-primary');
    if (search) primary.append(search);
    if (chips) primary.append(chips);
    const options = element('div', 'runs-filter-options');
    for (const select of Array.from(toolbar.children).filter((node) => node.tagName === 'SELECT')) options.append(select);
    toolbar.append(primary, options);
    results.classList.add('runs-results');
  }

  function prepareActionMenus(scope = document) {
    for (const menu of scope.querySelectorAll('.console-action-menu')) {
      menu.setAttribute('x-on:keydown.escape.stop', "$el.open = false; $el.querySelector('summary').focus()");
      menu.setAttribute('x-on:click.outside', '$el.open = false');
    }
    for (const template of scope.querySelectorAll('template')) prepareActionMenus(template.content);
  }

  organizeDashboard();
  organizeSystem();
  organizeRecovery();
  organizeEditors();
  organizeDataPaths();
  organizeJobPlanning();
  organizeHistory();
  prepareActionMenus();
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
          if (health.error || health.overdue || ['error', 'failed', 'timeout', 'stale', 'skipped'].includes(health.last_status)) {
            issues.push({
              id: `run:${health.history_key || health.name}`,
              pair: health.name,
              message: `${health.name}: ${health.error || (health.overdue ? 'Sicherung überfällig' : this.statusLabel(health.last_status))}`,
              action: health.job_id ? 'job' : 'pair',
              actionLabel: health.job_id ? 'Lauf ansehen' : 'Datenweg öffnen',
              jobId: health.job_id,
            });
          }
        }
        for (const pair of this.storagePairs?.() || []) {
          if (pair.enabled === false || this.config?.backup?.pairs?.some((entry) => entry.name === pair.name && entry.enabled === false)) continue;
          const evidence = pair.restore_evidence;
          if (!evidence || (evidence.state === 'passed' && evidence.valid !== false
            && Number(evidence.valid_until) * 1000 > Date.now())) continue;
          const messages = {
            failed: 'Letzter Restore-Test fehlgeschlagen',
            stale: 'Restore-Nachweis veraltet',
            partial: 'Restore-Stichprobe unvollständig',
            never: 'Noch kein Restore-Nachweis',
          };
          issues.push({
            id: `restore:${pair.name}`,
            pair: pair.name,
            message: `${pair.name}: ${messages[evidence.state] || 'Kein aktueller Restore-Nachweis'}`,
            action: 'restore',
            actionLabel: 'Restore-Test starten…',
          });
        }
        return issues;
      },
      async workspaceIssueAction(issue) {
        if (issue.action === 'restore') return this.runRestoreTest(issue.pair);
        if (issue.action === 'job') return this.showJob({ id: issue.jobId });
        await this.navigate('pairs');
        if (this.page !== 'pairs') return;
        const index = (this.config.backup?.pairs || []).findIndex((pair) => pair.name === issue.pair);
        if (index < 0) {
          this.showToast('Dieser Datenweg ist nicht mehr in der aktuellen Konfiguration vorhanden.', 'warn');
          return;
        }
        this.pairSearch = issue.pair;
        this.pairFilter = 'all';
        this.activeTag = '';
        this.pairOpen[index] = true;
        this.$nextTick(() => {
          document.getElementById(`pair-${index}`)?.querySelector('.pair-expand')?.focus();
        });
      },
    });
  };
  document.body.dataset.workspaceReady = 'true';
})();
