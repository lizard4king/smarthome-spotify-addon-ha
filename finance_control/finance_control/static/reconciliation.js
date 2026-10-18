'use strict';
// Display only: the server selects the dated control and calculates the difference.
window.financeReconciliation = (parent, comparison, {action=true}={}) => {
  if (!comparison) return;
  const add = (tag, text, className='') => {
    const node = document.createElement(tag); node.textContent = text; node.className = className;
    box.append(node); return node;
  };
  const box = document.createElement('span');
  box.className = `balance-reconciliation balance-reconciliation-${['matched','different','missing'].includes(comparison.status) ? comparison.status : 'missing'}`;
  const date = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value.split('-').reverse().join('.') : 'unbekannt';
  const money = (value, signed=false) => value === null || value === undefined || !/^[+-]?\d+(?:\.\d{1,2})?$/.test(String(value)) ? '—'
    : new Intl.NumberFormat('de-DE',{style:'currency',currency:comparison.currency || 'EUR',signDisplay:signed ? 'always' : 'auto'}).format(Number(value));
  const reasons = {
    paypal_transaction_date_not_booking_date:'PayPal-Buchungsdatum fehlt.',
    no_control:'Kein belegter Vergleichsstand vorhanden.',
    conflicting_controls:'Widersprüchliche Vergleichsstände. Bitte die Belege prüfen.',
    currency_conflict:'Währungen stimmen nicht überein. Vergleich nicht möglich.',
    control_predates_opening:'Der Vergleichsstand liegt vor dem Eröffnungssaldo.',
    invalid_ledger:'Der berechnete Stand ist nicht verfügbar.',
    invalid_control:'Der belegte Vergleichsstand ist ungültig.',
  };
  const status = comparison.status;
  add('strong', status === 'matched' ? 'Rechnerisch passend' : status === 'different'
    ? `Differenz ${money(comparison.difference, true)}` : 'Vergleich fehlt');
  if (comparison.comparison_date) add('span', `Vergleich am ${date(comparison.comparison_date)}`);
  if (status === 'missing') add('span', reasons[comparison.reason] || 'Kein belastbarer Vergleich möglich.');
  else {
    const source = {bank:'Bankstand',statement:'Belegstand',finanzguru_file:'Dateistand'}[comparison.source] || 'Belegter Stand';
    add('span', `${source}: ${money(comparison.control_amount)} · Berechnet: ${money(comparison.ledger_amount)}`);
    add('span', 'Differenz = belegter Stand minus berechneter Stand.', 'muted');
  }
  const warnings = new Set(comparison.warnings || []);
  if (warnings.has('older_control')) add('span', `Älterer Vergleichsstand; angefragter Stichtag ${date(comparison.requested_as_of)}.`, 'balance-reconciliation-warning');
  if (warnings.has('bank_balance_not_day_end') || warnings.has('file_balance_not_day_end') || comparison.comparison_basis === 'intraday')
    add('span', 'Kein bestätigter Tagesendstand; Abweichungen innerhalb des Tages sind möglich.', 'balance-reconciliation-warning');
  // A matching number alone never proves that all bookings were imported.
  add('span', 'Vollständigkeit der Buchungen nicht bestätigt.', 'balance-reconciliation-warning');
  if (status === 'different') {
    const button = add(action ? 'button' : 'span', 'Buchungen prüfen', 'balance-reconciliation-action');
    if (action) {
      button.type = 'button';
      button.addEventListener('click', event => {event.preventDefault(); event.stopPropagation(); window.financeDashboardSelectAccount(comparison.account_id);});
    }
  }
  parent.append(box);
  return box;
};
