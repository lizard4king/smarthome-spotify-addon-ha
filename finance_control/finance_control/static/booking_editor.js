'use strict';

// This dialog owns its selection. It never changes the classification workspace.
let bookingEditorSession = null;
function bookingEditorNode(tag, text = '', className = '') {
  const node = document.createElement(tag);
  const icon = text.startsWith('📄 ') ? 'document' : text.startsWith('🏷 ') ? 'category' : text.startsWith('🔎 ') ? 'source' : null;
  node.textContent = icon ? text.slice(3) : text;
  if (icon) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false'); svg.setAttribute('class', 'booking-editor-icon');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', {document: 'M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 16h7', category: 'M3 3h8l10 10-8 8L3 11z M7 7h.01', source: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6'}[icon]);
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'currentColor'); path.setAttribute('stroke-width', '1.7'); svg.append(path); node.prepend(svg);
  }
  if (className) node.className = className;
  return node;
}
function bookingEditorKey(tx) { return {account_id: tx.account_id, external_id: tx.external_id}; }
function bookingEditorActive(session) { return bookingEditorSession === session && session.dialog.open; }
function bookingEditorApprovalSignature(value, type) {
  const fields = type === 'transaction' ? ['account_id', 'external_id', 'revision', 'amount', 'currency', 'is_transfer', 'transfer_id', 'category', 'confirmed'] :
    ['id', 'revision', 'kind', 'status', 'amount', 'currency', 'document_date', 'vendor', 'title', 'source_reference', 'voucher_amount'];
  return JSON.stringify([fields.map(field => value[field] ?? null), (value.links || []).map(link =>
    [link.id ?? null, link.account_id ?? null, link.external_id ?? null, link.allocated_amount ?? null, link.allocation_type ?? null,
      link.title ?? null, link.kind ?? null, link.vendor ?? null]).sort()]);
}
function bookingEditorMessage(session, text, error = false) {
  session.status.textContent = text;
  session.status.className = error ? 'error' : 'muted';
}
function bookingEditorButton(session, box, text, handler) {
  const control = bookingEditorNode('button', text);
  control.type = 'button';
  control.addEventListener('click', () => { void handler(); });
  box.append(control);
  return control;
}
function bookingEditorBusy(session, busy) {
  session.busy = busy;
  for (const control of session.dialog.querySelectorAll('button,input,select')) {
    if (busy) { control.dataset.wasDisabled = String(control.disabled); control.disabled = true; }
    else if (control.dataset.wasDisabled !== undefined) {
      control.disabled = control.dataset.wasDisabled === 'true'; delete control.dataset.wasDisabled;
    }
  }
  session.dialog.setAttribute('aria-busy', String(busy));
}
async function bookingEditorReload(session) {
  const result = await api('/api/classification-transaction-get', session.key);
  if (!bookingEditorActive(session)) return;
  session.tx = result.transaction;
  bookingEditorRenderTransaction(session);
}
async function bookingEditorMutate(session, kind, route, payload) {
  if (!bookingEditorActive(session) || session.busy) return;
  bookingEditorBusy(session, true);
  bookingEditorMessage(session, 'Änderung wird gespeichert …');
  let saved = false;
  let refreshWarning = '';
  try {
    if (kind !== 'category' && kind !== 'document') {
      const [freshTransaction, freshDocument] = await Promise.all([api('/api/classification-transaction-get', session.key),
        api('/api/classification-document-get', {id: payload.document_id})]);
      let freshRest = null;
      if (kind !== 'unlink' && freshDocument.document.kind === 'invoice') {
        // Refresh the backend's remaining-payment/refund view; allocation validation
        // still happens atomically on the server, without browser-side arithmetic.
        const rest = await api('/api/classification-document-matches', {document_id: payload.document_id, max_days: 45, page: 0});
        freshRest = JSON.stringify([rest.remaining_payment, rest.remaining_refund]);
      }
      const changed = bookingEditorApprovalSignature(session.tx, 'transaction') !== bookingEditorApprovalSignature(freshTransaction.transaction, 'transaction') ||
        (session.document?.id === payload.document_id && bookingEditorApprovalSignature(session.document, 'document') !== bookingEditorApprovalSignature(freshDocument.document, 'document')) ||
        (kind !== 'unlink' && freshRest !== session.documentRest);
      if (changed) {
        session.tx = freshTransaction.transaction; bookingEditorRenderTransaction(session);
        await bookingEditorSelectDocument(session, payload.document_id, true);
        bookingEditorMessage(session, 'Buchung, Beleg oder offener Rest wurden inzwischen geändert. Prüfe die neu geladenen Daten und bestätige erneut.', true);
        return;
      }
    }
    await api(route, payload);
    saved = true;
    // Notify the caller immediately after a successful write, even if reloading fails.
    if (typeof session.onSaved === 'function') {
      try { await session.onSaved({...session.key, kind}); }
      catch (error) { refreshWarning = error.message; }
    }
    await bookingEditorReload(session);
    if (bookingEditorActive(session) && session.document) await bookingEditorSelectDocument(session, session.document.id, true);
    if (bookingEditorActive(session)) await bookingEditorSuggestions(session);
    if (bookingEditorActive(session)) bookingEditorMessage(session, refreshWarning ?
      'Gespeichert. Der Monatsvergleich konnte nicht aktualisiert werden: ' + refreshWarning :
      'Gespeichert. Du kannst diese Buchung weiter bearbeiten.', Boolean(refreshWarning));
  } catch (error) {
    if (!bookingEditorActive(session)) return;
    // Do not retry a stale write automatically: display the current authoritative choice.
    if (!saved && /stale_revision|zwischenzeitlich|veraltet|inzwischen geändert/i.test(error.message)) {
      if (kind === 'document') {
        await bookingEditorSelectDocument(session, payload.id, true);
        bookingEditorMessage(session, 'Der Beleg wurde inzwischen geändert. Prüfe die neu geladenen Belegdaten und bestätige sie erneut.', true);
        return;
      }
      let reloaded = true;
      try { await bookingEditorReload(session); } catch (_) {
        reloaded = false; session.categorySave.disabled = true; session.categorySave.dataset.wasDisabled = 'true';
      }
      bookingEditorMessage(session, reloaded ?
        'Die Buchung wurde inzwischen geändert. Prüfe die neu geladenen Daten und wähle die Kategorie erneut.' :
        'Die Buchung wurde inzwischen geändert. Aktuelle Daten konnten nicht geladen werden. Schließe den Dialog und öffne die Buchung erneut.', true);
    } else bookingEditorMessage(session, `${saved ? 'Gespeichert, aber die Ansicht konnte nicht vollständig aktualisiert werden. ' : 'Nicht gespeichert. '}${error.message}`, true);
  } finally {
    if (bookingEditorActive(session)) { bookingEditorBusy(session, false); bookingEditorUpdateDocumentActions(session); }
  }
}
async function bookingEditorSuggestions(session) {
  const epoch = ++session.matchEpoch;
  session.matches.textContent = 'Belegvorschläge werden geladen …';
  try {
    const result = await api('/api/classification-matches', {...session.key, max_days: 30});
    if (!bookingEditorActive(session) || epoch !== session.matchEpoch) return;
    session.matches.replaceChildren();
    session.matches.append(bookingEditorNode('p', 'Vorschläge aus Empfänger, Betrag und Datum; kein Nachweis der Zusammengehörigkeit. Prüfe jeden Beleg anhand seiner Quelle.', 'muted'));
    const render = (match, prefix = '') => {
      const row = bookingEditorNode('article', '', 'booking-editor-document');
      row.append(bookingEditorNode('p', `${prefix}#${match.document_id} · ${match.document_date || 'Datum offen'} · ${match.vendor || ''} · ${match.title} · Vorschlagsbetrag ${match.allocated_amount || match.remaining_amount || 'offen'} EUR`));
      const control = bookingEditorButton(session, row, '📄 Vorgeschlagenen Beleg prüfen', () => bookingEditorSelectDocument(session, match.document_id));
      if (session.busy) { control.dataset.wasDisabled = 'false'; control.disabled = true; }
      session.matches.append(row);
    };
    for (const match of result.suggestions || []) render(match);
    for (const group of result.suggestion_groups || []) {
      session.matches.append(bookingEditorNode('p', `Sammelvorschlag: ${group.documents.length} Belege · zusammen ${group.allocated_amount} EUR. Jede Zuordnung einzeln bestätigen.`, 'notice'));
      for (const match of group.documents) render(match, 'Sammelvorschlag · ');
    }
    if (!result.suggestions?.length && !result.suggestion_groups?.length) session.matches.append(bookingEditorNode('p', 'Keine passenden Belegvorschläge. Du kannst den Belegbestand durchsuchen.'));
  } catch (error) {
    if (bookingEditorActive(session) && epoch === session.matchEpoch) session.matches.textContent = 'Belegvorschläge konnten nicht geladen werden: ' + error.message;
  }
}
function bookingEditorRenderTransaction(session) {
  const tx = session.tx;
  session.summary.replaceChildren();
  session.summary.append(bookingEditorNode('strong', tx.counterparty || 'Keine Gegenpartei'),
    bookingEditorNode('p', `${tx.date} · ${tx.account_label || tx.account_id} · ${tx.amount} ${tx.currency || ''}`),
    bookingEditorNode('p', tx.description || 'Kein Verwendungszweck vorhanden.'));
  if (tx.source_context_complete === false) session.summary.append(bookingEditorNode('p', 'Quelldetails fehlen.', 'notice'));
  session.category.replaceChildren(bookingEditorNode('option', 'Kategorie wählen'));
  session.category.firstChild.value = '';
  for (const parent of session.catalog.parents || []) {
    if (parent.transaction_type !== tx.direction) continue;
    const group = bookingEditorNode('optgroup'); group.label = parent.label;
    for (const category of session.catalog.categories || []) {
      if (category.transaction_type !== tx.direction || category.parent_id !== parent.id) continue;
      const option = bookingEditorNode('option', category.label); option.value = category.id; group.append(option);
    }
    if (group.children.length) session.category.append(group);
  }
  session.category.value = tx.category || '';
  session.category.disabled = Boolean(tx.is_transfer);
  session.categorySave.disabled = Boolean(tx.is_transfer);
  session.categoryNote.textContent = tx.is_transfer ? (tx.transfer_display || 'Umbuchung: Kategorie bleibt unverändert.') :
    tx.confirmed ? 'Kategorie ist geprüft. Änderungen gelten nur für diese Buchung.' : 'Kategorie ist noch offen. Bestätigung gilt nur für diese Buchung.';
  session.links.replaceChildren();
  if (!(tx.links || []).length) session.links.append(bookingEditorNode('p', 'Noch keine Belege zugeordnet.', 'muted'));
  for (const link of tx.links || []) {
    const row = bookingEditorNode('article', '', 'booking-editor-document');
    const type = link.allocation_type === 'refund' ? 'Erstattung' : link.allocation_type === 'evidence' ? 'Zahlungsnachweis' : 'Zahlung';
    row.append(bookingEditorNode('p', `#${link.id} · ${link.title} · ${type} · ${link.allocated_amount ?? 'Vollbetrag'}`));
    bookingEditorButton(session, row, '📄 Beleg und Quelle ansehen', () => bookingEditorSelectDocument(session, link.id));
    bookingEditorButton(session, row, 'Diese Zuordnung lösen', () => bookingEditorMutate(session, 'unlink', '/api/classification-unlink',
      {...session.key, document_id: link.id, confirmed: true}));
    session.links.append(row);
  }
  if (session.busy) {
    // Newly rendered controls must also remain locked until the mutation completes.
    for (const control of session.dialog.querySelectorAll('button,input,select')) {
      if (control.dataset.wasDisabled === undefined) { control.dataset.wasDisabled = String(control.disabled); control.disabled = true; }
    }
    session.category.dataset.wasDisabled = String(Boolean(tx.is_transfer));
    session.categorySave.dataset.wasDisabled = String(Boolean(tx.is_transfer));
    session.category.disabled = true; session.categorySave.disabled = true;
  }
}
async function bookingEditorDocuments(session, page = 0) {
  if (!bookingEditorActive(session) || session.busy || !session.tx) return;
  const epoch = ++session.searchEpoch;
  session.documents.textContent = 'Belege werden geladen …';
  session.previous.disabled = true; session.next.disabled = true;
  try {
    const data = await api('/api/classification-documents', {page, query: session.search.value});
    if (!bookingEditorActive(session) || epoch !== session.searchEpoch) return;
    session.page = data.page;
    session.documents.replaceChildren();
    for (const doc of data.documents || []) {
      const row = bookingEditorNode('article', '', 'booking-editor-document');
      row.append(bookingEditorNode('p', `#${doc.id} · ${doc.document_date || 'Datum offen'} · ${doc.vendor || ''} · ${doc.title} · ${doc.amount ?? 'Betrag offen'} ${doc.currency || ''} · ${doc.status === 'confirmed' ? 'bestätigt' : 'ungeprüft'}`));
      bookingEditorButton(session, row, '📄 Beleg prüfen', async () => {
        session.searchBox.open = false;
        await bookingEditorSelectDocument(session, doc.id);
        if (bookingEditorActive(session)) session.documentTitle.scrollIntoView?.({block: 'nearest'});
      });
      session.documents.append(row);
    }
    if (!data.documents?.length) session.documents.textContent = 'Keine Belege für diese Suche.';
    session.pageLabel.textContent = `Seite ${data.page + 1} / ${Math.max(1, data.pages)} · ${data.total} Belege`;
    session.previous.disabled = data.page === 0; session.next.disabled = data.page + 1 >= data.pages;
  } catch (error) {
    if (bookingEditorActive(session) && epoch === session.searchEpoch) {
      session.documents.textContent = 'Belege konnten nicht geladen werden.';
      bookingEditorMessage(session, error.message, true);
    }
  }
}
function bookingEditorUpdateDocumentActions(session) {
  // Association buttons deliberately defer amount/currency/direction acceptance to
  // the atomic backend rules. The browser must not maintain a second money model;
  // contracts, partial payments and refunds do not share simple equality rules.
  const doc = session.document, tx = session.tx;
  session.documentSave.disabled = session.busy || !doc;
  if (!doc || !tx) return;
  const linked = (tx.links || []).some(link => link.id === doc.id);
  session.link.disabled = Boolean(session.busy || !session.documentReady || linked || doc.status !== 'confirmed' || (tx.is_transfer && doc.kind !== 'contract'));
  session.allocate.disabled = Boolean(session.busy || !session.documentReady || linked || tx.is_transfer || doc.status !== 'confirmed' || doc.kind !== 'invoice');
  session.documentNote.textContent = linked ? 'Dieser Beleg ist bereits zugeordnet.' : doc.status !== 'confirmed' ?
    'Dieser Beleg ist ungeprüft. Prüfe die Quelle und bestätige seine Daten hier im Dialog.' : tx.is_transfer && doc.kind !== 'contract' ?
    'Umbuchungen können nur durch bestätigte Verträge belegt werden.' :
    'Prüfe die Quelle. Vollzuordnung und Teilbetrag werden beim Speichern auf Betrag, Währung und verfügbare Restsumme geprüft.';
}
async function bookingEditorSelectDocument(session, id, duringMutation = false) {
  if (!bookingEditorActive(session) || !session.tx || (session.busy && !duringMutation)) return;
  const epoch = ++session.documentEpoch;
  session.document = null; session.detail.hidden = false; session.documentTitle.textContent = 'Beleg wird geladen …';
  session.documentRest = null; session.documentReady = false; session.restNote.textContent = '';
  session.documentForm.hidden = true; session.documentSave.disabled = true;
  session.source.textContent = ''; session.sourceReference.textContent = ''; session.documentNote.textContent = ''; session.link.disabled = true; session.allocate.disabled = true;
  try {
    const result = await api('/api/classification-document-get', {id: Number(id)});
    if (!bookingEditorActive(session) || epoch !== session.documentEpoch) return;
    const doc = result.document; session.document = doc;
    session.documentForm.hidden = false;
    session.documentTitle.textContent = `#${doc.id} · ${doc.vendor || ''} · ${doc.title} · ${doc.document_date || 'Datum offen'} · ${doc.amount ?? 'Betrag offen'} ${doc.currency || ''}`;
    session.sourceReference.textContent = `Quelle: ${doc.source_reference || 'Keine Referenz'}${doc.warnings?.length ? ' · Hinweise: ' + doc.warnings.join(', ') : ''}`;
    for (const field of ['vendor', 'title', 'document_date', 'amount']) session.documentFields[field].value = doc[field] ?? '';
    session.documentFields.kind.value = doc.kind === 'invoice' ? 'Rechnung' : 'Vertrag';
    session.documentFields.currency.value = doc.currency || 'EUR';
    const linkedInvoice = doc.kind === 'invoice' && Boolean(doc.links?.length);
    session.documentFields.amount.readOnly = linkedInvoice; session.documentFields.document_date.readOnly = linkedInvoice;
    session.documentEditNote.textContent = linkedInvoice ? 'Betrag und Datum sind wegen vorhandener Zuordnungen gesperrt. Anbieter und Titel lassen sich korrigieren.' : 'Prüfe Anbieter, Titel, Datum und Betrag anhand der Quelle. Die Bestätigung ordnet noch keine Buchung zu.';
    session.documentSave.textContent = doc.status === 'confirmed' ? 'Belegdaten korrigieren und bestätigen' : 'Belegdaten bestätigen';
    session.documentSave.disabled = session.busy;
    if (session.busy) session.documentSave.dataset.wasDisabled = 'false';
    session.allocationType.textContent = Number(session.tx.amount) < 0 ? 'Zahlung' : 'Erstattung';
    session.allocation.value = ''; // Explicit entry avoids deriving financial allocation rules in the browser.
    if (doc.kind === 'invoice') {
      const rest = await api('/api/classification-document-matches', {document_id: doc.id, max_days: 45, page: 0});
      if (!bookingEditorActive(session) || epoch !== session.documentEpoch) return;
      session.documentRest = JSON.stringify([rest.remaining_payment, rest.remaining_refund]);
      session.restNote.textContent = `${Number(doc.voucher_amount)>0?`Gutschein separat: ${doc.voucher_amount} EUR · `:''}Offener Zahlungsrest ${rest.remaining_payment ?? 'offen'} EUR · offener Erstattungsrest ${rest.remaining_refund ?? 'offen'} EUR`;
    }
    session.documentReady = true; bookingEditorUpdateDocumentActions(session);
    session.source.textContent = 'Quelltext wird geladen …';
    try {
      const source = await api('/api/classification-document-source', {id: doc.id});
      if (bookingEditorActive(session) && epoch === session.documentEpoch) session.source.textContent = source.text;
    } catch (_) {
      if (bookingEditorActive(session) && epoch === session.documentEpoch) session.source.textContent = 'Kein lokal hinterlegter Quelltext. Prüfe den vorhandenen Originalbeleg.';
    }
  } catch (error) {
    if (bookingEditorActive(session) && epoch === session.documentEpoch) { session.documentTitle.textContent = 'Beleg konnte nicht geladen werden.'; bookingEditorMessage(session, error.message, true); }
  }
}
async function bookingEditorOpen(key, {onSaved} = {}) {
  if (!key || typeof key.account_id !== 'string' || !key.account_id || typeof key.external_id !== 'string' || !key.external_id) throw new Error('Eine vorhandene Buchung auswählen.');
  if (bookingEditorSession?.busy) return false;
  if (bookingEditorSession) bookingEditorSession.dialog.close();
  const dialog = bookingEditorNode('dialog', '', 'booking-editor');
  const session = {dialog, key: bookingEditorKey(key), onSaved, busy: false, searchEpoch: 0, documentEpoch: 0, matchEpoch: 0, page: 0};
  bookingEditorSession = session;
  const opener = document.activeElement;
  const header = bookingEditorNode('div', '', 'booking-editor-header');
  const title = bookingEditorNode('h2', 'Buchung bearbeiten'); title.id = 'booking-editor-title';
  dialog.setAttribute('aria-labelledby', title.id); header.append(title);
  const close = bookingEditorButton(session, header, 'Schließen', async () => { if (!session.busy) dialog.close(); });
  dialog.append(header);
  session.status = bookingEditorNode('p', 'Buchung wird geladen …'); session.status.setAttribute('role', 'status'); dialog.append(session.status);
  session.summary = bookingEditorNode('section', '', 'booking-editor-summary'); dialog.append(session.summary);
  const categoryBox = bookingEditorNode('section'); categoryBox.append(bookingEditorNode('h3', '🏷 Kategorie'));
  const categoryLabel = bookingEditorNode('label', 'Eigene Kategorie'); session.category = bookingEditorNode('select'); categoryLabel.append(session.category); categoryBox.append(categoryLabel);
  session.categorySave = bookingEditorButton(session, categoryBox, 'Nur diese Buchung bestätigen', async () => {
    if (!session.tx || session.tx.is_transfer || session.busy) return;
    if (!session.category.value) { bookingEditorMessage(session, 'Bitte eine Kategorie wählen.', true); return; }
    await bookingEditorMutate(session, 'category', '/api/classification-save', {...session.key, category: session.category.value, revision: session.tx.revision});
  });
  session.categorySave.disabled = true;
  session.categoryNote = bookingEditorNode('p', '', 'muted'); categoryBox.append(session.categoryNote); dialog.append(categoryBox);
  const receipts = bookingEditorNode('section'); receipts.append(bookingEditorNode('h3', '📄 Zugeordnete Belege'));
  session.links = bookingEditorNode('div'); receipts.append(session.links); dialog.append(receipts);
  const matchSection = bookingEditorNode('section'); matchSection.append(bookingEditorNode('h3', '📄 Belegvorschläge'));
  session.matches = bookingEditorNode('div'); matchSection.append(session.matches); dialog.append(matchSection);
  const searchBox = bookingEditorNode('details', '', 'booking-editor-search'); session.searchBox = searchBox;
  searchBox.append(bookingEditorNode('summary', '📄 Beleg suchen und zuordnen'));
  const searchForm = bookingEditorNode('form', '', 'booking-editor-actions');
  const searchLabel = bookingEditorNode('label', 'Anbieter oder Titel'); session.search = bookingEditorNode('input'); session.search.type = 'search'; searchLabel.append(session.search); searchForm.append(searchLabel);
  const searchButton = bookingEditorNode('button', 'Suchen'); session.searchButton = searchButton; searchButton.type = 'submit'; searchButton.disabled = true; searchForm.append(searchButton);
  searchForm.addEventListener('submit', event => { event.preventDefault(); void bookingEditorDocuments(session, 0); }); searchBox.append(searchForm);
  session.documents = bookingEditorNode('div'); searchBox.append(session.documents);
  const pages = bookingEditorNode('div', '', 'booking-editor-actions');
  session.previous = bookingEditorButton(session, pages, 'Vorherige Seite', () => bookingEditorDocuments(session, session.page - 1));
  session.pageLabel = bookingEditorNode('span'); pages.append(session.pageLabel);
  session.next = bookingEditorButton(session, pages, 'Nächste Seite', () => bookingEditorDocuments(session, session.page + 1));
  session.previous.disabled = true; session.next.disabled = true;
  searchBox.append(pages); dialog.append(searchBox);
  session.detail = bookingEditorNode('section', '', 'booking-editor-detail'); session.detail.hidden = true;
  session.documentTitle = bookingEditorNode('h3'); session.sourceReference = bookingEditorNode('p'); session.source = bookingEditorNode('pre'); session.source.setAttribute('tabindex', '0');
  const sourceDetails = bookingEditorNode('details'); sourceDetails.append(bookingEditorNode('summary', '🔎 Quelle ansehen'), session.source);
  session.documentNote = bookingEditorNode('p', '', 'notice'); session.restNote = bookingEditorNode('p', '', 'muted'); session.detail.append(session.documentTitle, session.sourceReference, sourceDetails, session.documentNote, session.restNote);
  const documentForm = bookingEditorNode('form', '', 'booking-editor-fields'); session.documentForm = documentForm; session.documentFields = {};
  for (const [field, label, type] of [['kind', 'Art', 'text'], ['vendor', 'Anbieter', 'text'], ['title', 'Titel', 'text'], ['document_date', 'Belegdatum', 'date'], ['amount', 'Betrag', 'text'], ['currency', 'Währung', 'text']]) {
    const wrapper = bookingEditorNode('label', label), input = bookingEditorNode('input'); input.type = type;
    if (field === 'kind' || field === 'currency') input.readOnly = true;
    if (field === 'amount') input.inputMode = 'decimal';
    session.documentFields[field] = input; wrapper.append(input); documentForm.append(wrapper);
  }
  session.documentEditNote = bookingEditorNode('p', '', 'muted'); documentForm.append(session.documentEditNote);
  session.documentSave = bookingEditorNode('button', 'Belegdaten bestätigen'); session.documentSave.type = 'submit'; session.documentSave.disabled = true; documentForm.append(session.documentSave);
  documentForm.addEventListener('submit', event => {
    event.preventDefault(); if (!session.document || session.busy) return;
    const fields = session.documentFields, value = fields.amount.value.trim().replace(',', '.');
    void bookingEditorMutate(session, 'document', '/api/classification-document-save', {
      id: session.document.id, revision: session.document.revision, confirmed: true, status: 'confirmed',
      vendor: fields.vendor.value, title: fields.title.value, document_date: fields.document_date.value || null,
      amount: value || null, currency: value ? 'EUR' : null, source_reference: session.document.source_reference,
    });
  });
  session.detail.append(documentForm);
  session.link = bookingEditorButton(session, session.detail, 'Zuordnung des Vollbetrags bestätigen', async () => {
    if (!session.document || session.link.disabled) return;
    await bookingEditorMutate(session, 'link', '/api/classification-link', {...session.key, document_id: session.document.id, confirmed: true});
  });
  const allocationLabel = bookingEditorNode('label', 'Zuordnungsbetrag in EUR'); session.allocation = bookingEditorNode('input'); session.allocation.type = 'text'; session.allocation.inputMode = 'decimal'; allocationLabel.append(session.allocation);
  session.allocationType = bookingEditorNode('span'); allocationLabel.append(session.allocationType); session.detail.append(allocationLabel);
  session.allocate = bookingEditorButton(session, session.detail, 'Diesen Teilbetrag bestätigen', async () => {
    if (!session.document || session.allocate.disabled) return;
    const value = session.allocation.value.trim().replace(',', '.');
    if (!/^\d+(?:\.\d{1,2})?$/.test(value) || Number(value) <= 0) { bookingEditorMessage(session, 'Bitte einen positiven EUR-Betrag mit höchstens zwei Nachkommastellen eingeben.', true); return; }
    await bookingEditorMutate(session, 'allocate', '/api/classification-allocate', {...session.key, document_id: session.document.id, allocated_amount: value,
      allocation_type: Number(session.tx.amount) < 0 ? 'payment' : 'refund', confirmed: true});
  });
  dialog.append(session.detail);
  dialog.addEventListener('cancel', event => { if (session.busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { if (bookingEditorSession === session) bookingEditorSession = null; dialog.remove(); if (opener?.isConnected) opener.focus(); });
  document.body.append(dialog); dialog.showModal(); close.focus();
  try {
    const [result, catalog] = await Promise.all([api('/api/classification-transaction-get', session.key), api('/api/classification-catalog', {})]);
    if (!bookingEditorActive(session)) return false;
    session.tx = result.transaction; session.catalog = catalog; bookingEditorRenderTransaction(session);
    session.searchButton.disabled = false;
    bookingEditorMessage(session, 'Originalbuchung bleibt unverändert. Du bearbeitest Kategorie und Belegzuordnungen.');
    await Promise.all([bookingEditorSuggestions(session), bookingEditorDocuments(session)]);
  } catch (error) { if (bookingEditorActive(session)) bookingEditorMessage(session, error.message, true); }
  return bookingEditorActive(session);
}
