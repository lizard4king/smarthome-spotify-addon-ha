'use strict';

// Read-only evidence view. All source fields are inserted as text nodes.
(function () {
  let page = 0;
  let requestNumber = 0;
  let initialized = false;

  const byId = id => document.getElementById(id);
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  };
  const known = value => value !== null && value !== undefined && String(value) !== '';

  function dateLabel(value) {
    if (!known(value)) return 'Datum offen';
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value));
    if (!match) return String(value);
    const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
    if (date.getUTCFullYear() !== Number(match[1]) || date.getUTCMonth() + 1 !== Number(match[2]) || date.getUTCDate() !== Number(match[3])) return String(value);
    return `${match[3]}.${match[2]}.${match[1]}`;
  }

  function money(value, currency) {
    if (!known(value)) return 'Wert offen';
    const amount = Number(value);
    const number = Number.isFinite(amount)
      ? new Intl.NumberFormat('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(amount)
      : String(value);
    return known(currency) ? `${number} ${currency}` : `${number} · Währung offen`;
  }

  const typeLabel = kind => ({receipt:'Kassenbon', invoice:'Rechnung'})[kind] || (known(kind) ? String(kind) : 'Belegart offen');
  const statusLabel = status => ({unreviewed:'Noch ungeprüft', confirmed:'Bestätigt'})[status] || (known(status) ? String(status) : 'Prüfstatus offen');
  const sourceLabel = source => ({bonsy:'Bonsy', amazon:'Amazon', document:'Dokument'})[source] || (known(source) ? String(source) : 'Quelle offen');
  const allocationLabel = type => ({payment:'Zahlung', refund:'Erstattung', evidence:'Zahlungsnachweis'})[type] || (known(type) ? String(type) : 'Art offen');

  function field(label, value) {
    const line = node('span', 'fp-field');
    line.append(node('span', 'fp-field-label', `${label}: `), node('span', '', value));
    return line;
  }

  function merchant(row) {
    const box = node('span', 'fp-merchant');
    // Receipt titles are not bank descriptions and do not establish the merchant.
    // Keep the purchase-specific missing-merchant label while reusing its badge.
    const vendor = known(row.vendor) ? String(row.vendor) : 'Händler offen';
    if (window.financeCounterparties?.decorate) {
      window.financeCounterparties.decorate(box, {counterparty: vendor, description: ''});
    } else box.append(node('span', '', vendor));
    return box;
  }

  function detail(row) {
    const details = node('details', 'fp-details');
    details.append(node('summary', '', 'Details und Artikel ansehen'));
    const body = node('div', 'fp-details-body');
    body.append(field('Belegart', typeLabel(row.kind)), field('Quelle', sourceLabel(row.source)));
    const items = node('div', 'fp-items');
    items.append(node('h4', '', 'Artikelpositionen'));
    if (Array.isArray(row.items) && row.items.length) {
      const list = node('ul', 'fp-item-list');
      for (const item of row.items) {
        const entry = node('li', 'fp-item');
        entry.append(node('span', 'fp-item-description', known(item.description) ? item.description : 'Beschreibung offen'));
        const quantity = known(item.quantity) ? `${item.quantity}${known(item.unit) ? ` ${item.unit}` : ''}` : 'Menge offen';
        entry.append(node('span', 'fp-item-meta', `${quantity} · ${money(item.amount, item.currency)}`));
        list.append(entry);
      }
      items.append(list);
    } else if (!known(row.items_note)) items.append(node('p', 'fp-muted', 'Keine Artikelpositionen erfasst.'));
    if (known(row.items_note)) items.append(node('p', 'fp-note', row.items_note));
    body.append(items);

    const links = node('div', 'fp-links');
    links.append(node('h4', '', 'Zahlungszuordnungen'));
    if (Array.isArray(row.links) && row.links.length) {
      const list = node('ul', 'fp-link-list');
      for (const link of row.links) {
        const accountId = known(link.account_id) ? String(link.account_id) : 'Konto offen';
        const account = typeof accountDisplayById === 'function' && known(link.account_id)
          ? accountDisplayById(link.account_id) : accountId;
        const externalId = known(link.external_id) ? ` · ${link.external_id}` : '';
        list.append(node('li', '', `${account}${externalId} · ${allocationLabel(link.allocation_type)} · ${money(link.allocated_amount, row.currency)}`));
      }
      links.append(list);
    } else links.append(node('p', 'fp-muted', 'Keine Zahlung zugeordnet.'));
    body.append(links);
    if (known(row.id)) {
      const edit = node('button', 'secondary fp-edit', 'Beleg bearbeiten');
      edit.type = 'button';
      edit.addEventListener('click', () => document.dispatchEvent(new CustomEvent('finance-purchase-edit', {detail: {id: row.id}})));
      body.append(edit);
    }
    details.append(body);
    return details;
  }

  function rowHeading(row) {
    const box = node('div', 'fp-heading');
    box.append(merchant(row));
    if (known(row.title)) box.append(node('span', 'fp-title', row.title));
    else box.append(node('span', 'fp-muted', 'Titel offen'));
    return box;
  }

  function status(row) {
    const box = node('div', 'fp-status');
    box.append(node('span', '', statusLabel(row.status)));
    const count = Array.isArray(row.links) ? row.links.length : 0;
    box.append(node('span', 'fp-muted', count ? `${count} Zuordnung${count === 1 ? '' : 'en'}` : 'Keine Zuordnung'));
    return box;
  }

  function renderRow(row) {
    const tr = node('tr');
    const cells = [dateLabel(row.date), rowHeading(row), money(row.amount, row.currency), status(row), detail(row)];
    for (const content of cells) {
      const td = node('td');
      td.append(content instanceof Node ? content : document.createTextNode(content));
      tr.append(td);
    }
    return tr;
  }

  function renderCard(row) {
    const card = node('article', 'fp-card');
    const head = node('div', 'fp-card-head');
    head.append(rowHeading(row), node('strong', 'fp-amount', money(row.amount, row.currency)));
    card.append(head, node('p', 'fp-card-meta', dateLabel(row.date)));
    card.append(status(row), detail(row));
    return card;
  }

  function render(data) {
    const rows = Array.isArray(data?.rows) ? data.rows : [];
    const table = byId('fp-table-body');
    const cards = byId('fp-cards');
    table.replaceChildren();
    cards.replaceChildren();
    for (const row of rows) {
      table.append(renderRow(row));
      cards.append(renderCard(row));
    }
    const total = Number.isInteger(data?.total) && data.total >= 0 ? data.total : rows.length;
    const pages = Number.isInteger(data?.pages) && data.pages >= 0 ? data.pages : 0;
    page = Number.isInteger(data?.page) && data.page >= 0 ? data.page : page;
    byId('fp-count').textContent = total ? `${total} Beleg${total === 1 ? '' : 'e'} im gewählten Zeitraum` : 'Keine Belege für diese Filter.';
    byId('fp-page').textContent = pages ? `Seite ${page + 1} von ${pages}` : 'Keine Seite';
    byId('fp-prev').disabled = page <= 0;
    byId('fp-next').disabled = pages === 0 || page + 1 >= pages;
    byId('fp-empty').hidden = rows.length > 0;
    byId('fp-table').hidden = rows.length === 0;
    byId('fp-cards').hidden = rows.length === 0;
  }

  function filters() {
    return {
      query: byId('fp-query').value,
      date_from: byId('fp-from').value || null,
      date_to: byId('fp-to').value || null,
      kind: byId('fp-kind').value || 'all',
      page,
      order: byId('fp-order').value,
    };
  }

  async function load() {
    initialize();
    const current = ++requestNumber;
    const statusNode = byId('fp-load-status');
    statusNode.textContent = 'Belege werden geladen …';
    statusNode.className = 'fp-load-status';
    try {
      const data = await api('/api/purchases', filters());
      if (current !== requestNumber) return;
      render(data);
      statusNode.textContent = '';
    } catch (error) {
      if (current !== requestNumber) return;
      render({rows: [], total: 0, page: 0, pages: 0});
      statusNode.textContent = `Belege konnten nicht geladen werden: ${error.message}`;
      statusNode.className = 'fp-load-status error';
    }
  }

  function initialize() {
    if (initialized) return;
    const root = byId('purchases-root');
    if (!root) return;
    initialized = true;
    root.classList.add('fp-root');
    const intro = node('p', 'fp-notice', 'Belegwerte sind Nachweise zu Einkäufen und keine zusätzlichen Kontoabflüsse. Ob ein Einkauf geplant war, ergibt sich nicht automatisch aus dem Beleg.');
    const form = node('form', 'fp-filters');
    form.id = 'fp-filters';
    const label = (caption, control) => {
      const wrapper = node('label', 'fp-filter');
      wrapper.append(node('span', '', caption), control);
      return wrapper;
    };
    const query = node('input'); query.id = 'fp-query'; query.type = 'search'; query.placeholder = 'Händler, Titel oder Artikel';
    const from = node('input'); from.id = 'fp-from'; from.type = 'date';
    const to = node('input'); to.id = 'fp-to'; to.type = 'date';
    const month = /^\d{4}-\d{2}/.exec(typeof state !== 'undefined' ? state?.as_of || '' : '');
    if (month) {
      const [year, part] = month[0].split('-').map(Number);
      const lastDay = new Date(Date.UTC(year, part, 0)).getUTCDate();
      from.value = `${month[0]}-01`;
      to.value = `${month[0]}-${String(lastDay).padStart(2, '0')}`;
    }
    const kind = node('select'); kind.id = 'fp-kind';
    for (const [value, text] of [['', 'Alle Belegarten'], ['receipt', 'Kassenbons'], ['invoice', 'Rechnungen']]) kind.append(new Option(text, value));
    const order = node('select'); order.id = 'fp-order';
    for (const [value, text] of [['newest', 'Neueste zuerst'], ['oldest', 'Älteste zuerst']]) order.append(new Option(text, value));
    const search = node('button', '', 'Suchen'); search.type = 'submit';
    form.append(label('Suche', query), label('Von', from), label('Bis', to), label('Belegart', kind), label('Sortierung', order), search);
    const count = node('p', 'fp-count'); count.id = 'fp-count';
    const statusNode = node('p', 'fp-load-status'); statusNode.id = 'fp-load-status'; statusNode.setAttribute('role', 'status');
    const table = node('table', 'fp-table'); table.id = 'fp-table';
    const head = node('thead'); const headings = node('tr');
    for (const title of ['Datum', 'Händler / Einkauf', 'Belegwert', 'Prüfstatus / Zuordnung', 'Details']) {
      const th = node('th', '', title); th.scope = 'col'; headings.append(th);
    }
    head.append(headings);
    const body = node('tbody'); body.id = 'fp-table-body'; table.append(head, body);
    const cards = node('div', 'fp-cards'); cards.id = 'fp-cards';
    const empty = node('p', 'fp-empty', 'Keine Belege für diese Filter.'); empty.id = 'fp-empty';
    const navigation = node('div', 'fp-pagination');
    const previous = node('button', 'secondary', 'Vorige Seite'); previous.id = 'fp-prev'; previous.type = 'button';
    const pageLabel = node('span'); pageLabel.id = 'fp-page';
    const next = node('button', 'secondary', 'Nächste Seite'); next.id = 'fp-next'; next.type = 'button';
    navigation.append(previous, pageLabel, next);
    root.append(intro, form, count, statusNode, table, cards, empty, navigation);
    form.addEventListener('submit', event => {event.preventDefault(); page = 0; void load();});
    for (const control of [from, to, kind, order]) control.addEventListener('change', () => {page = 0; void load();});
    previous.addEventListener('click', () => {if (page > 0) {page -= 1; void load();}});
    next.addEventListener('click', () => {page += 1; void load();});
  }

  window.financePurchasesLoad = load;
  window.financePurchasesRender = data => {initialize(); render(data);};
})();
