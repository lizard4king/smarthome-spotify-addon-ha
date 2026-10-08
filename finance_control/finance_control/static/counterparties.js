'use strict';

(() => {
  const catalog = [
    {id: 'postbank', label: 'Postbank', src: '/bank-postbank.svg', aliases: ['Postbank']},
    {id: 'sparkasse', label: 'Sparkasse', src: '/bank-sparkasse.png', aliases: ['Nassauische Sparkasse', 'Sparkasse', 'Sparkassen', 'NASPA']},
    {id: 'ing', label: 'ING', src: '/bank-ing.svg', aliases: ['ING-DiBa', 'ING']},
    {id: 'paypal', label: 'PayPal', src: '/bank-paypal.png', aliases: ['PayPal']},
    {id: 'kaufland', label: 'Kaufland', src: '/brand-kaufland.png', aliases: ['Kaufland']},
    {id: 'lidl', label: 'Lidl', src: '/brand-lidl.svg', aliases: ['Lidl']},
    {id: 'rewe', label: 'REWE', src: '/brand-rewe.ico', aliases: ['REWE']},
    {id: 'aldi-sued', label: 'ALDI SÜD', src: '/brand-aldi-sued.ico', aliases: ['ALDI SÜD', 'ALDI SUED']},
    {id: 'dm', label: 'dm', src: '/brand-dm.png', aliases: ['dm-drogerie markt', 'dm']},
    {id: 'obi', label: 'OBI', src: '/brand-obi.png', aliases: ['OBI']},
    {id: 'mediamarkt', label: 'MediaMarkt', src: '/brand-mediamarkt.png', aliases: ['MediaMarkt', 'Media Markt']},
    {id: 'google', label: 'Google', src: '/brand-google.ico', aliases: ['Google Play', 'GooglePlay', 'Google']},
    {id: 'amazon', label: 'Amazon', src: '/brand-amazon.ico', aliases: ['Amazon']},
  ];

  // Unicode letters and numbers define merchant token boundaries; underscores and
  // punctuation delimit names, while longer words never match short brands.
  function escaped(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+');
  }

  function findBrands(value, {ignorePayPal = false} = {}) {
    if (typeof value !== 'string' || !value.trim()) return [];
    const matches = new Map();
    for (const brand of catalog) {
      if (ignorePayPal && brand.id === 'paypal') continue;
      for (const alias of brand.aliases) {
        const pattern = new RegExp(`(?<![\\p{L}\\p{N}])${escaped(alias)}(?![\\p{L}\\p{N}])`, 'giu');
        const match = pattern.exec(value);
        if (!match) continue;
        const current = matches.get(brand.id);
        if (!current || match.index < current.index || (match.index === current.index && match[0].length > current.length)) {
          matches.set(brand.id, {brand, index: match.index, length: match[0].length});
        }
      }
    }
    return [...matches.values()];
  }

  const genericCounterparties = new Set([
    '', 'abrechnung karte', 'kreditkartenumsatz', 'abbuchung', 'kartenzahlung',
    'kartenumsatz', 'lastschrift', 'ec-karte', 'debitkarte',
  ]);
  const processorPattern = /(?<![\p{L}\p{N}])(?:PayPal|SumUp)(?![\p{L}\p{N}])/giu;

  function isGeneric(value) {
    const normalized = (typeof value === 'string' ? value : '').trim().toLocaleLowerCase('de-DE')
      .replace(/\s+/g, ' ');
    return genericCounterparties.has(normalized);
  }

  function paymentProcessor(value) {
    if (typeof value !== 'string') return null;
    processorPattern.lastIndex = 0;
    const match = processorPattern.exec(value);
    return match ? match[0].toLocaleLowerCase('de-DE') : null;
  }

  function identify({counterparty = '', description = ''} = {}) {
    const party = typeof counterparty === 'string' ? counterparty : '';
    const purpose = typeof description === 'string' ? description : '';
    const processor = paymentProcessor(party);
    const ordinaryPartyBrands = findBrands(party, {ignorePayPal: true});
    if (ordinaryPartyBrands.length > 1) return null;
    if (ordinaryPartyBrands.length === 1) {
      const direct = ordinaryPartyBrands[0].brand;
      return {id: direct.id, label: direct.label, src: direct.src};
    }
    const canUseDescription = isGeneric(party) || Boolean(processor);
    if (!canUseDescription) return null;

    const purposeProcessor = paymentProcessor(purpose);
    const purposeBrands = findBrands(purpose, {ignorePayPal: Boolean(purposeProcessor)});
    if (purposeBrands.length > 1) return null;
    if (purposeBrands.length === 1) {
      const merchant = purposeBrands[0].brand;
      return {id: merchant.id, label: merchant.label, src: merchant.src};
    }

    if (processor === 'paypal' || purposeProcessor === 'paypal') {
      const paypal = catalog.find(brand => brand.id === 'paypal');
      return {id: paypal.id, label: paypal.label, src: paypal.src};
    }
    return null;
  }

  function initials(value) {
    const words = (typeof value === 'string' ? value : '').trim().split(/\s+/u).filter(Boolean);
    const firstLetter = word => Array.from(word).find(character => /[\p{L}\p{N}]/u.test(character)) || '';
    const letters = words.length > 1
      ? `${firstLetter(words[0])}${firstLetter(words[1])}`
      : Array.from(words[0] || '').filter(character => /[\p{L}\p{N}]/u.test(character)).slice(0, 2).join('');
    const result = letters || '?';
    return (result.length === 1 ? result + result : result).toLocaleUpperCase('de-DE');
  }

  function decorate(element, item = {}) {
    if (!element || typeof element.replaceChildren !== 'function') return element;
    element.replaceChildren();
    const counterparty = typeof item.counterparty === 'string' ? item.counterparty : '';
    const description = typeof item.description === 'string' ? item.description : '';
    const displayText = counterparty.trim() || description.trim() || 'Ohne Gegenpartei';
    const identity = identify({counterparty, description});
    const content = document.createElement('span');
    content.className = 'fc-counterparty';

    if (identity) {
      const image = document.createElement('img');
      image.className = 'fc-counterparty-logo';
      image.src = identity.src;
      image.alt = '';
      image.setAttribute('aria-hidden', 'true');
      image.loading = 'lazy';
      image.decoding = 'async';
      image.draggable = false;
      content.append(image);
    } else {
      const badge = document.createElement('span');
      badge.className = 'fc-counterparty-initials';
      badge.setAttribute('aria-hidden', 'true');
      badge.textContent = initials(displayText);
      content.append(badge);
    }

    const text = document.createElement('span');
    text.className = 'fc-counterparty-text';
    text.textContent = displayText;
    content.append(text);
    element.append(content);
    return element;
  }

  window.financeCounterparties = {identify, decorate};
})();
