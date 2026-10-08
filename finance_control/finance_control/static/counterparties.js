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
    {id: 'edeka', label: 'EDEKA', src: '/brand-edeka.svg', aliases: ['EDEKA']},
    {id: 'penny', label: 'PENNY', src: '/brand-penny.svg', aliases: ['PENNY Markt', 'PENNY']},
    {id: 'rossmann', label: 'ROSSMANN', src: '/brand-rossmann.svg', aliases: ['ROSSMANN']},
    {id: 'mueller', label: 'Müller', src: '/brand-muller.svg', aliases: ['Müller Drogerie', 'Mueller Drogerie', 'Müller Handels', 'Mueller Handels']},
    {id: 'ikea', label: 'IKEA', src: '/brand-ikea.svg', aliases: ['IKEA']},
    {id: 'otto', label: 'OTTO', src: '/brand-otto.svg', aliases: ['OTTO Versand', 'OTTO GmbH', 'OTTO.de']},
    {id: 'zalando', label: 'Zalando', src: '/brand-zalando.svg', aliases: ['Zalando']},
    {id: 'ebay', label: 'eBay', src: '/brand-ebay.svg', aliases: ['eBay']},
    {id: 'saturn', label: 'SATURN', src: '/brand-saturn.svg', aliases: ['SATURN Elektro', 'SATURN Markt', 'SATURN.de']},
    {id: 'dhl', label: 'DHL', src: '/brand-dhl.svg', aliases: ['DHL']},
    {id: 'hermes', label: 'Hermes', src: '/brand-hermes.svg', aliases: ['Hermes Versand', 'Hermes Paket', 'Hermes Germany']},
    {id: 'fedex', label: 'FedEx', src: '/brand-fedex.svg', aliases: ['FedEx']},
    {id: 'ups', label: 'UPS', src: '/brand-ups.svg', aliases: ['UPS Paket', 'UPS Deutschland', 'United Parcel Service']},
    {id: 'deutschebahn', label: 'Deutsche Bahn', src: '/brand-deutschebahn.svg', aliases: ['Deutsche Bahn', 'DB Vertrieb', 'DB Fernverkehr', 'DB Regio']},
    {id: 'lufthansa', label: 'Lufthansa', src: '/brand-lufthansa.svg', aliases: ['Lufthansa']},
    {id: 'ryanair', label: 'Ryanair', src: '/brand-ryanair.svg', aliases: ['Ryanair']},
    {id: 'easyjet', label: 'easyJet', src: '/brand-easyjet.svg', aliases: ['easyJet']},
    {id: 'bookingdotcom', label: 'Booking.com', src: '/brand-bookingdotcom.svg', aliases: ['Booking.com']},
    {id: 'airbnb', label: 'Airbnb', src: '/brand-airbnb.svg', aliases: ['Airbnb']},
    {id: 'ubereats', label: 'Uber Eats', src: '/brand-ubereats.svg', aliases: ['Uber Eats']},
    {id: 'uber', label: 'Uber', src: '/brand-uber.svg', aliases: ['Uber']},
    {id: 'vodafone', label: 'Vodafone', src: '/brand-vodafone.svg', aliases: ['Vodafone']},
    {id: 'o2', label: 'O2', src: '/brand-o2.svg', aliases: ['O2 Telefónica', 'O2 Telefonica', 'O2 Germany']},
    {id: 'netflix', label: 'Netflix', src: '/brand-netflix.svg', aliases: ['Netflix']},
    {id: 'spotify', label: 'Spotify', src: '/brand-spotify.svg', aliases: ['Spotify']},
    {id: 'youtube', label: 'YouTube', src: '/brand-youtube.svg', aliases: ['YouTube']},
    {id: 'playstation', label: 'PlayStation', src: '/brand-playstation.svg', aliases: ['PlayStation']},
    {id: 'steam', label: 'Steam', src: '/brand-steam.svg', aliases: ['Steam Games', 'Steam Powered', 'Steam Store']},
    {id: 'apple', label: 'Apple', src: '/brand-apple.svg', aliases: ['Apple.com', 'Apple Services', 'Apple Distribution', 'Apple iTunes']},
    {id: 'dropbox', label: 'Dropbox', src: '/brand-dropbox.svg', aliases: ['Dropbox']},
    {id: 'github', label: 'GitHub', src: '/brand-github.svg', aliases: ['GitHub']},
    {id: 'mcdonalds', label: "McDonald's", src: '/brand-mcdonalds.svg', aliases: ["McDonald's", 'McDonalds']},
    {id: 'burgerking', label: 'Burger King', src: '/brand-burgerking.svg', aliases: ['Burger King']},
    {id: 'kfc', label: 'KFC', src: '/brand-kfc.svg', aliases: ['KFC']},
    {id: 'starbucks', label: 'Starbucks', src: '/brand-starbucks.svg', aliases: ['Starbucks']},
    {id: 'aral', label: 'Aral', src: '/brand-aral.svg', aliases: ['Aral Tankstelle', 'Aral AG']},
    {id: 'shell', label: 'Shell', src: '/brand-shell.svg', aliases: ['Shell Tankstelle', 'Shell Deutschland']},
    {id: 'bmw', label: 'BMW', src: '/brand-bmw.svg', aliases: ['BMW']},
    {id: 'volkswagen', label: 'Volkswagen', src: '/brand-volkswagen.svg', aliases: ['Volkswagen']},
    {id: 'tesla', label: 'Tesla', src: '/brand-tesla.svg', aliases: ['Tesla']},
    {id: 'n26', label: 'N26', src: '/brand-n26.svg', aliases: ['N26 Bank', 'N26 GmbH']},
    {id: 'commerzbank', label: 'Commerzbank', src: '/brand-commerzbank.svg', aliases: ['Commerzbank']},
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
    const candidates = [...matches.values()];
    // Specific compound names take precedence over a nested generic brand.
    return candidates.filter(candidate => !candidates.some(other =>
      other !== candidate && other.index <= candidate.index
      && other.index + other.length >= candidate.index + candidate.length
      && other.length > candidate.length));
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
