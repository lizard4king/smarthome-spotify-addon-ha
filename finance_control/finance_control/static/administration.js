'use strict';
(() => {
  const byId = id => document.getElementById(id);
  let administrationState = null;
  let pending = null;
  let busy = false;
  let stale = false;
  let loadSequence = 0;
  let credentialsRenderSequence = 0;
  const node = (tag, text, className = '') => {
    const element = document.createElement(tag);
    element.textContent = String(text ?? '');
    if (className) element.className = className;
    return element;
  };
  const status = (text, error = false) => {
    byId('adm-status').textContent = text;
    byId('adm-status').classList.toggle('error', error);
  };
  const mutationControls = () => document.querySelectorAll('#administration .adm-mutate');
  const lockMutations = () => mutationControls().forEach(button => { button.disabled = busy || stale || button.dataset.locked === 'true'; });
  const invalidateCredentialsUi = () => {
    credentialsRenderSequence += 1;
    byId('adm-server-credentials').replaceChildren();
  };
  const roleName = role => role === 'admin' ? 'Administrator' : 'Mitglied';
  const bankName = id => administrationState?.banks?.find(bank => bank.id === id)?.name || id;
  const bankLogo = id => {
    const source = administrationState?.banks?.find(bank => bank.id === id)?.logo;
    return /^\/bank-(?:postbank|ing)\.svg$|^\/bank-(?:sparkasse|paypal)\.png$/.test(source || '') ? source : null;
  };
  const addRow = (parent, title, detail, action, callback, disabled = false) => {
    const row = node('div', '', 'adm-row');
    const copy = node('div', '', 'adm-row-copy');
    copy.append(node('strong', title), node('small', detail));
    row.append(copy);
    if (action) {
      const button = node('button', action, 'secondary adm-mutate');
      button.type = 'button'; button.dataset.locked = String(disabled); button.disabled = disabled || busy || stale;
      button.addEventListener('click', callback);
      row.append(button);
    }
    parent.append(row);
    return row;
  };
  function confirmAction(text, action, payload) {
    if (busy || stale || !administrationState?.enabled) return;
    pending = {action, payload};
    byId('adm-dialog-text').textContent = text;
    byId('adm-dialog').showModal();
    byId('adm-cancel').focus();
  }
  function render() {
    const data = administrationState;
    const credentialsHost = byId('adm-server-credentials');
    const credentialsGeneration = ++credentialsRenderSequence;
    credentialsHost.replaceChildren();
    byId('adm-disabled').hidden = data?.enabled !== false;
    byId('adm-removed').hidden = true;
    byId('adm-content').hidden = !data?.enabled;
    if (!data?.enabled) return;
    byId('adm-profile-email').textContent = `${data.me.email} · ${roleName(data.me.role)}`;
    byId('adm-profile-form').elements.display_name.value = data.me.display_name || '';
    const admin = data.me.role === 'admin';
    byId('adm-admin').hidden = !admin;
    const activeAdmins = (data.users || []).filter(user => user.active && user.role === 'admin').length;
    byId('adm-remove-self').dataset.locked = String(admin && activeAdmins <= 1);
    const users = byId('adm-users'); users.replaceChildren();
    if (admin) for (const user of data.users || []) {
      const canRemove = user.active && (user.role !== 'admin' || activeAdmins > 1);
      addRow(users, user.display_name || user.email,
        `${user.email} · ${roleName(user.role)}${user.active ? '' : ' · Inaktiv'}`,
        user.active ? 'Entfernen' : 'Wieder freischalten',
        () => user.active
          ? confirmAction(`Benutzer ${user.email} entfernen? Der Finance-Control-Zugang wird entzogen.`,
            'user-remove', {id:user.id, revision:user.revision, confirmed:true})
          : confirmAction(`Benutzer ${user.email} als ${roleName(user.role)} wieder freischalten? Diese Person kann die gemeinsamen Haushaltsfinanzen sehen.${user.role === 'admin' ? ' Als Administrator kann sie auch Benutzer verwalten.' : ''} Cloudflare muss die Adresse ebenfalls erlauben.`,
            'user-restore', {id:user.id, revision:user.revision, confirmed:true}),
        user.active && !canRemove);
    }
    const bankSelect = byId('adm-bank-form').elements.bank_id;
    bankSelect.replaceChildren();
    for (const bank of data.banks || []) {
      const option = node('option', `${bank.name}${bank.bank_code ? ` · ${bank.bank_code}` : ''}`);
      option.value = bank.id; bankSelect.append(option);
    }
    const connections = byId('adm-connections'); connections.replaceChildren();
    for (const connection of data.connections || []) {
      const detail = `${bankName(connection.bank_id)} · ${connection.status === 'LOCAL_SETUP_REQUIRED' ? 'Zugang einrichten' : 'Status prüfen'}`;
      const row = addRow(connections, connection.label, detail, 'Entfernen',
        () => confirmAction(`Bankregistrierung ${connection.label} (${bankName(connection.bank_id)}) entfernen? Die deaktivierte Registrierung wird nicht mehr genutzt; die Buchungshistorie bleibt erhalten.`,
          'bank-remove', {id:connection.id, revision:connection.revision, confirmed:true}));
      const logo = bankLogo(connection.bank_id);
      if (logo) {
        const image = document.createElement('img');
        image.className = 'adm-bank-logo'; image.src = logo;
        image.alt = ''; image.setAttribute('aria-hidden', 'true');
        row.prepend(image);
      }
    }
    if (!data.connections?.length) connections.append(node('p', 'Noch keine Bank registriert.', 'muted'));
    if (window.financeServerBanking && data.connections?.length) {
      const scopedHost = document.createElement('div');
      credentialsHost.append(scopedHost);
      window.financeServerBanking.render(scopedHost, data.connections, () => {
        if (credentialsRenderSequence !== credentialsGeneration || !administrationState?.enabled || !scopedHost.isConnected) return;
        return load();
      });
    }
    lockMutations();
  }
  async function load() {
    const sequence = ++loadSequence;
    invalidateCredentialsUi();
    byId('adm-reload').disabled = true;
    status('Verwaltung wird geladen.');
    try {
      const result = await api('/api/administration');
      if (sequence !== loadSequence) return;
      administrationState = result;
      stale = false;
      render();
      status(result.enabled ? '' : 'Verwaltung ist ohne konfigurierte Cloudflare-Zugangskontrolle nicht verfügbar.');
    } catch (error) {
      if (sequence !== loadSequence) return;
      stale = true;
      lockMutations();
      status(error.message || 'Verwaltung konnte nicht geladen werden.', true);
    } finally {
      if (sequence === loadSequence) byId('adm-reload').disabled = false;
    }
  }
  async function commit() {
    if (!pending || busy || stale) return;
    const {action, payload} = pending;
    pending = null;
    busy = true;
    byId('adm-confirm').disabled = true;
    lockMutations();
    try {
      const result = await api(`/api/administration/${action}`, payload);
      byId('adm-dialog').close();
      if (result.removed) {
        administrationState = null;
        invalidateCredentialsUi();
        byId('adm-content').hidden = true;
        byId('adm-removed').hidden = false;
        status('Dein Zugang wurde entfernt.');
        return;
      }
      if (result.enabled === true) {
        administrationState = result;
        render();
      } else await load();
      status('Änderung gespeichert.');
    } catch (error) {
      byId('adm-dialog').close();
      stale = true;
      status(`${error.message || 'Änderung fehlgeschlagen.'} Bitte neu laden und den aktuellen Stand prüfen.`, true);
    } finally {
      busy = false;
      byId('adm-confirm').disabled = false;
      lockMutations();
    }
  }
  byId('adm-dialog').addEventListener('close', () => { pending = null; });
  byId('adm-confirm').addEventListener('click', commit);
  byId('adm-reload').addEventListener('click', load);
  byId('adm-remove-self').addEventListener('click', () => {
    const me = administrationState?.me;
    if (!me) return;
    confirmAction(`Deinen eigenen Finance-Control-Zugang ${me.email} entfernen? Danach ist diese Sitzung nicht mehr nutzbar.`,
      'user-remove', {id:me.id, revision:me.revision, confirmed:true});
  });
  byId('adm-profile-form').addEventListener('submit', event => {
    event.preventDefault();
    const name = event.currentTarget.elements.display_name.value.trim();
    if (!name || !administrationState?.me) return;
    confirmAction(`Deinen Anzeigenamen auf „${name}“ ändern?`, 'profile-save',
      {display_name:name, revision:administrationState.me.revision, confirmed:true});
  });
  byId('adm-user-form').addEventListener('submit', event => {
    event.preventDefault();
    if (administrationState?.me?.role !== 'admin') return;
    const form = event.currentTarget.elements;
    const email = form.email.value.trim();
    const displayName = form.display_name.value.trim();
    const role = form.role.value;
    if (!email || !displayName || !['member','admin'].includes(role)) return;
    confirmAction(`Benutzer ${email} als ${roleName(role)} hinzufügen? Diese Person kann die gemeinsamen Haushaltsfinanzen sehen.${role === 'admin' ? ' Als Administrator kann sie auch Benutzer verwalten.' : ''} Es wird keine E-Mail verschickt. Cloudflare muss die Adresse ebenfalls erlauben.`,
      'user-add', {email, display_name:displayName, role, confirmed:true});
  });
  byId('adm-bank-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!administrationState?.enabled) return;
    const form = event.currentTarget.elements;
    const bankId = form.bank_id.value;
    const label = form.label.value.trim();
    if (!bankId || !label) return;
    confirmAction(`Bank ${bankName(bankId)} als „${label}“ registrieren? Zugangsdaten können danach separat auf dem Server eingerichtet und Kontostände einmalig lesend abgerufen werden.`,
      'bank-add', {bank_id:bankId, label, confirmed:true});
  });
  window.administrationLoad = load;
})();
