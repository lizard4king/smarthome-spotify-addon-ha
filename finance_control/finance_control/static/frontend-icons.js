'use strict';
// Local, fixed Lucide nodes copied from dashboard.js; no remote dependency.
(() => {
  const iconPaths = {
    account: 'wallet', savings: 'piggy-bank', cash: 'banknote', card: 'credit-card',
    depot: 'chart-no-axes-combined', book: 'book-open', energy: 'zap',
    transfer: 'arrow-left-right', income: 'arrow-down-to-line', category: 'tag',
  };
  const accountIcon = kind => ({SAVINGS:'savings',CASH:'cash',CREDIT_CARD:'card',DEPOT:'depot',INVESTMENT:'depot'})[String(kind || '').toUpperCase()] || 'account';
  // Match bank words only in public-facing metadata. Account IDs are private identifiers.
  const bankBrand = account => {
    if (String(account.kind || '').toUpperCase() === 'CASH') return null;
    for (const value of [account.institution, account.display_name]) {
      if (typeof value !== 'string') continue;
      const label = value.toLocaleLowerCase('de-DE');
      if (/(^|[^\p{L}])postbank(?=$|[^\p{L}])/u.test(label)) return 'postbank';
      if (/(^|[^\p{L}])(?:naspa|sparkasse)(?=$|[^\p{L}])/u.test(label)) return 'sparkasse';
      if (/(^|[^\p{L}])ing(?=$|[^\p{L}])/u.test(label)) return 'ing';
      if (/(^|[^\p{L}])paypal(?=$|[^\p{L}])/u.test(label)) return 'paypal';
    }
    return null;
  };
  const categoryIcon = name => {
    const value = String(name || '').toLocaleLowerCase('de-DE');
    const words = value.split(/[^\p{L}\p{N}]+/u).filter(Boolean);
    const has = (...prefixes) => words.some(word => prefixes.some(prefix => word.startsWith(prefix)));
    const contains = part => words.some(word => word.includes(part));
    if (contains('umbuch') || has('transfer')) return 'transfer';
    if (has('bücher', 'buch', 'lese')) return 'book';
    if (has('lebensmittel', 'supermarkt', 'nahrung')) return 'shopping-basket';
    if (has('einkauf', 'shopping', 'kleidung')) return 'shopping-bag';
    if (has('haustier', 'tierbedarf', 'tierarzt', 'hund', 'katze', 'hunde', 'katzen')) return 'paw-print';
    if (has('wohnen', 'miete', 'wohnung', 'haus')) return 'house';
    if (has('gastronomie', 'restaurant', 'essen', 'café', 'cafe')) return 'utensils';
    if (contains('energie') || has('strom', 'gas', 'heizung')) return 'energy';
    if (has('öpnv', 'oepnv', 'öffis', 'oeffis', 'bus', 'tram')) return 'bus-front';
    if (has('bahn', 'zug', 'eisenbahn')) return 'train-front';
    if (has('mobilität', 'mobilitaet', 'auto', 'verkehr', 'transport')) return 'car-front';
    if (has('gesundheit', 'arzt', 'apotheke')) return 'heart-pulse';
    if (has('kinder', 'kind', 'baby')) return 'baby';
    if (has('versicherung')) return 'shield-check';
    if (has('telefon', 'internet', 'mobilfunk')) return 'wifi';
    if (has('abo', 'abonnement')) return 'repeat-2';
    if (has('urlaub', 'reise')) return 'plane';
    if (has('freizeit', 'kultur', 'kino')) return 'ticket';
    if (contains('steuer') || has('abgabe')) return 'receipt-text';
    if (contains('einkommen') || has('gehalt', 'lohn')) return 'income';
    if (has('bargeld', 'barabhebung')) return 'cash';
    return 'category';
  };
  // Fixed Lucide SVG nodes: https://github.com/lucide-icons/lucide/tree/main/icons
  const lucideNodes = {
    "wallet": [["path",{"d":"M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"}],["path",{"d":"M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"}]],
    "piggy-bank": [["path",{"d":"M11 17h3v2a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1v-3a3.16 3.16 0 0 0 2-2h1a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1h-1a5 5 0 0 0-2-4V3a4 4 0 0 0-3.2 1.6l-.3.4H11a6 6 0 0 0-6 6v1a5 5 0 0 0 2 4v3a1 1 0 0 0 1 1h2a1 1 0 0 0 1-1z"}],["path",{"d":"M16 10h.01"}],["path",{"d":"M2 8v1a2 2 0 0 0 2 2h1"}]],
    "banknote": [["rect",{"width":"20","height":"12","x":"2","y":"6","rx":"2"}],["circle",{"cx":"12","cy":"12","r":"2"}],["path",{"d":"M6 12h.01M18 12h.01"}]],
    "credit-card": [["rect",{"width":"20","height":"14","x":"2","y":"5","rx":"2"}],["line",{"x1":"2","x2":"22","y1":"10","y2":"10"}],["path",{"d":"M6 14h2"}]],
    "chart-no-axes-combined": [["path",{"d":"M12 16v5"}],["path",{"d":"M16 14.639V21"}],["path",{"d":"M20 10.656V21"}],["path",{"d":"m22 3-8.646 8.646a.5.5 0 0 1-.708 0L9.354 8.354a.5.5 0 0 0-.707 0L2 15"}],["path",{"d":"M4 18.463V21"}],["path",{"d":"M8 14.656V21"}]],
    "book-open": [["path",{"d":"M12 5v16"}],["path",{"d":"M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z"}]],
    "zap": [["path",{"d":"M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z"}]],
    "arrow-left-right": [["path",{"d":"M8 3 4 7l4 4"}],["path",{"d":"M4 7h16"}],["path",{"d":"m16 21 4-4-4-4"}],["path",{"d":"M20 17H4"}]],
    "arrow-down-to-line": [["path",{"d":"M12 17V3"}],["path",{"d":"m6 11 6 6 6-6"}],["path",{"d":"M19 21H5"}]],
    "tag": [["path",{"d":"M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"}],["circle",{"cx":"7.5","cy":"7.5","r":".5","fill":"currentColor"}]],
    "shopping-basket": [["path",{"d":"m15 11-1 9"}],["path",{"d":"m19 11-4-7"}],["path",{"d":"M2 11h20"}],["path",{"d":"m3.5 11 1.6 7.4a2 2 0 0 0 2 1.6h9.8a2 2 0 0 0 2-1.6l1.7-7.4"}],["path",{"d":"M4.5 15.5h15"}],["path",{"d":"m5 11 4-7"}],["path",{"d":"m9 11 1 9"}]],
    "shopping-bag": [["path",{"d":"M16 10a4 4 0 0 1-8 0"}],["path",{"d":"M3.103 6.034h17.794"}],["path",{"d":"M3.4 5.467a2 2 0 0 0-.4 1.2V20a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6.667a2 2 0 0 0-.4-1.2l-2-2.667A2 2 0 0 0 17 2H7a2 2 0 0 0-1.6.8z"}]],
    "house": [["path",{"d":"M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"}],["path",{"d":"M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"}]],
    "car-front": [["path",{"d":"m21 8-2 2-1.5-3.7A2 2 0 0 0 15.646 5H8.4a2 2 0 0 0-1.903 1.257L5 10 3 8"}],["path",{"d":"M7 14h.01"}],["path",{"d":"M17 14h.01"}],["rect",{"width":"18","height":"8","x":"3","y":"10","rx":"2"}],["path",{"d":"M5 18v2"}],["path",{"d":"M19 18v2"}]],
    "utensils": [["path",{"d":"M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"}],["path",{"d":"M7 2v20"}],["path",{"d":"M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"}]],
    "heart-pulse": [["path",{"d":"M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"}],["path",{"d":"M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"}]],
    "baby": [["path",{"d":"M10 16c.5.3 1.2.5 2 .5s1.5-.2 2-.5"}],["path",{"d":"M15 12h.01"}],["path",{"d":"M19.38 6.813A9 9 0 0 1 20.8 10.2a2 2 0 0 1 0 3.6 9 9 0 0 1-17.6 0 2 2 0 0 1 0-3.6A9 9 0 0 1 12 3c2 0 3.5 1.1 3.5 2.5s-.9 2.5-2 2.5c-.8 0-1.5-.4-1.5-1"}],["path",{"d":"M9 12h.01"}]],
    "shield-check": [["path",{"d":"M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"}],["path",{"d":"m9 12 2 2 4-4"}]],
    "wifi": [["path",{"d":"M12 20h.01"}],["path",{"d":"M2 8.82a15 15 0 0 1 20 0"}],["path",{"d":"M5 12.859a10 10 0 0 1 14 0"}],["path",{"d":"M8.5 16.429a5 5 0 0 1 7 0"}]],
    "repeat-2": [["path",{"d":"m2 9 3-3 3 3"}],["path",{"d":"M13 18H7a2 2 0 0 1-2-2V6"}],["path",{"d":"m22 15-3 3-3-3"}],["path",{"d":"M11 6h6a2 2 0 0 1 2 2v10"}]],
    "ticket": [["path",{"d":"M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"}],["path",{"d":"M13 5v2"}],["path",{"d":"M13 17v2"}],["path",{"d":"M13 11v2"}]],
    "plane": [["path",{"d":"M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"}]],
    "receipt-text": [["path",{"d":"M13 16H8"}],["path",{"d":"M14 8H8"}],["path",{"d":"M16 12H8"}],["path",{"d":"M4 3a1 1 0 0 1 1-1 1.3 1.3 0 0 1 .7.2l.933.6a1.3 1.3 0 0 0 1.4 0l.934-.6a1.3 1.3 0 0 1 1.4 0l.933.6a1.3 1.3 0 0 0 1.4 0l.933-.6a1.3 1.3 0 0 1 1.4 0l.934.6a1.3 1.3 0 0 0 1.4 0l.933-.6A1.3 1.3 0 0 1 19 2a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1 1.3 1.3 0 0 1-.7-.2l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.934.6a1.3 1.3 0 0 1-1.4 0l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-1.4 0l-.934-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-.7.2 1 1 0 0 1-1-1z"}]],
  };
  lucideNodes['paw-print'] = [["circle",{"cx":"11","cy":"4","r":"2"}],["circle",{"cx":"18","cy":"8","r":"2"}],["circle",{"cx":"20","cy":"16","r":"2"}],["path",{"d":"M9 10a5 5 0 0 1 5 5v3.5a3.5 3.5 0 0 1-6.84 1.045Q6.52 17.48 4.46 16.84A3.5 3.5 0 0 1 5.5 10Z"}]];
  lucideNodes['bus-front'] = [["path",{"d":"M4 6 2 7"}],["path",{"d":"M10 6h4"}],["path",{"d":"m22 7-2-1"}],["rect",{"width":"16","height":"16","x":"4","y":"3","rx":"2"}],["path",{"d":"M4 11h16"}],["path",{"d":"M8 15h.01"}],["path",{"d":"M16 15h.01"}],["path",{"d":"M6 19v2"}],["path",{"d":"M18 21v-2"}]];
  lucideNodes['train-front'] = [["path",{"d":"M8 3.1V7a4 4 0 0 0 8 0V3.1"}],["path",{"d":"m9 15-1-1"}],["path",{"d":"m15 15 1-1"}],["path",{"d":"M9 19c-2.8 0-5-2.2-5-5v-4a8 8 0 0 1 16 0v4c0 2.8-2.2 5-5 5Z"}],["path",{"d":"m8 19-2 3"}],["path",{"d":"m16 19 2 3"}]];

  Object.assign(lucideNodes, {
    'chevron-right': [['path',{d:'m9 18 6-6-6-6'}]],
    'chevron-down': [['path',{d:'m6 9 6 6 6-6'}]],
    'arrow-up-right': [['path',{d:'M7 7h10v10M7 17 17 7'}]],
    'arrow-left': [['path',{d:'m12 19-7-7 7-7M5 12h14'}]],
    'search': [['circle',{cx:'11',cy:'11',r:'8'}],['path',{d:'m21 21-4.3-4.3'}]],
    'sliders-horizontal': [['path',{d:'M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4'}]],
    'x': [['path',{d:'M18 6 6 18M6 6l12 12'}]],
    'check': [['path',{d:'m20 6-11 11-5-5'}]],
    'rotate-ccw': [['path',{d:'M3 11a9 9 0 1 1 2.7 6.4M3 3v8h8'}]],
    'refresh-cw': [['path',{d:'M3 7v5h5M21 17v-5h-5M5 16a8 8 0 0 0 13 3l3-7M3 12l3-7a8 8 0 0 1 13 3'}]],
    'calendar-days': [['rect',{x:'3',y:'5',width:'18',height:'16',rx:'2'}],['path',{d:'M16 3v4M8 3v4M3 11h18M8 15h.01M12 15h.01M16 15h.01M8 18h.01M12 18h.01'}]],
    'ellipsis': [['circle',{cx:'5',cy:'12',r:'1'}],['circle',{cx:'12',cy:'12',r:'1'}],['circle',{cx:'19',cy:'12',r:'1'}]],
    'sun': [['circle',{cx:'12',cy:'12',r:'4'}],['path',{d:'M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5'}]],
    'log-out': [['path',{d:'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9'}]],
  });
  const escapeAttribute=value=>String(value).replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  function icon(name){
    const key=iconPaths[name]||name, nodes=lucideNodes[key]||lucideNodes.tag;
    return `<svg class="fc-icon" data-icon="${escapeAttribute(key)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${nodes.map(([tag,attrs])=>`<${tag} ${Object.entries(attrs).map(([key,value])=>`${key}="${escapeAttribute(value)}"`).join(' ')}></${tag}>`).join('')}</svg>`;
  }
  const tile=name=>`<span class="tile pictogram" aria-hidden="true">${icon(name)}</span>`;
  function accountTile(account){
    const brand=bankBrand(account),source={postbank:'/bank-postbank.svg',sparkasse:'/bank-sparkasse.png',ing:'/bank-ing.svg',paypal:'/bank-paypal.png'}[brand];
    return source?`<span class="tile logo bank-logo" aria-hidden="true"><img src="${source}" alt="" loading="lazy"></span>`:tile(accountIcon(account.kind));
  }
  window.financeFrontendIcons=Object.freeze({icon,tile,accountTile,bankBrand,categoryIcon});
})();
