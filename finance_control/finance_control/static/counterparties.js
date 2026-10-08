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
    {id: 'devk', label: 'DEVK', src: '/brand-devk.png', aliases: ['DEVK Versicherungen', 'DEVK']},
    {id: 'cineplex', label: 'Cineplex', src: '/brand-cineplex.svg', aliases: ['Cineplex Deutschland', 'Cineplex']},
    {id: 'schaefer-baecker', label: 'Schäfer Dein Bäcker', src: '/brand-schaefer-dein-baecker.jpg', aliases: ['Schäfer Dein Bäcker', 'Schaefer Dein Baecker']},
    {id: 'contipark', label: 'Contipark', src: '/brand-contipark.png', aliases: ['Contipark']},
    {id: 'sumup', label: 'SumUp', src: '/brand-sumup.svg', aliases: ['SumUp']},
    {id: 'esso', label: 'Esso', src: '/brand-esso.ico', aliases: ['Esso Deutschland', 'Esso']},
    {id: 'chin-thai-limburg', label: 'Chin Thai Limburg', src: '/brand-chin-thai-limburg.png', aliases: ['CHIN*THAI', 'Chin Thai Limburg', 'Chin Thai', 'ChinThai Limburg']},
    {id: 'pvs-dental', label: 'PVS Dental', src: '/brand-pvs-dental.png', aliases: ['PVS-Dental', 'PVS Dental']},
    {id: 'dkv', label: 'DKV', src: '/brand-dkv.png', aliases: ['DKV Deutsche Krankenversicherung', 'DKV Krankenversicherung']},
    {id: 'targobank', label: 'TARGOBANK', src: '/brand-targobank.png', aliases: ['TARGOBANK', 'Targo Bank']},
    {id: 'revolut', label: 'Revolut', src: '/brand-revolut.svg', aliases: ['Revolut']},
    {id: 'klarna', label: 'Klarna', src: '/brand-klarna.ico', aliases: ['Klarna']},
    {id: 'payone', label: 'PAYONE', src: '/brand-payone.png', aliases: ['PAYONE']},
    {id: 'adyen', label: 'Adyen', src: '/brand-adyen.svg', aliases: ['Adyen']},
    {id: 'jet', label: 'JET', src: '/brand-jet.png', aliases: ['JET Tankstelle', 'JET Tankstellen']},
    {id: 'deutsche-bank', label: 'Deutsche Bank', src: '/brand-deutsche-bank.svg', aliases: ['Deutsche Bank AG', 'Deutsche Bank']},
    {id: 'audible', label: 'Audible', src: '/brand-audible.svg', aliases: ['Audible']},
    {id: 'adidas', label: 'Adidas', src: '/brand-adidas.svg', aliases: ['Adidas']},
    {id: 'avast', label: 'Avast', src: '/brand-avast.svg', aliases: ['Avast']},
    {id: 'deutschepost', label: 'Deutsche Post', src: '/brand-deutschepost.svg', aliases: ['Deutsche Post']},
    {id: 'expedia', label: 'Expedia', src: '/brand-expedia.svg', aliases: ['Expedia']},
    {id: 'globus', label: 'Globus', src: '/brand-globus.svg', aliases: ['Globus Markthalle', 'Globus SB-Warenhaus']},
    {id: 'handm', label: 'H&M', src: '/brand-handm.svg', aliases: ['H&M']},
    {id: 'hilton', label: 'Hilton', src: '/brand-hilton.svg', aliases: ['Hilton Hotel', 'Hilton Worldwide']},
    {id: 'hiltonhotelsandresorts', label: 'Hilton Hotels & Resorts', src: '/brand-hiltonhotelsandresorts.svg', aliases: ['Hilton Hotels & Resorts']},
    {id: 'hp', label: 'HP', src: '/brand-hp.svg', aliases: ['HP Inc.', 'HP Deutschland', 'HP Store']},
    {id: 'jetbrains', label: 'JetBrains', src: '/brand-jetbrains.svg', aliases: ['JetBrains']},
    {id: 'kleinanzeigen', label: 'Kleinanzeigen', src: '/brand-kleinanzeigen.svg', aliases: ['eBay Kleinanzeigen', 'Kleinanzeigen']},
    {id: 'samsung', label: 'Samsung', src: '/brand-samsung.svg', aliases: ['Samsung']},
    {id: 'stripe', label: 'Stripe', src: '/brand-stripe.svg', aliases: ['Stripe']},
    {id: 'trustedshops', label: 'Trusted Shops', src: '/brand-trustedshops.svg', aliases: ['Trusted Shops']},
    {id: 'vinted', label: 'Vinted', src: '/brand-vinted.svg', aliases: ['Vinted']},
    {id: 'muehlenbaeckerei', label: 'Mühlenbäckerei Rudolf Jung', src: '/brand-local-muehlenbaeckerei.png', aliases: ['Mühlenbäckerei Rudolf Jung', 'Die Mühlenbäcker', '48 Muehlenbaeckerei Jung']},
    {id: 'lohners', label: 'Die Lohner’s', src: '/brand-local-lohners.png', aliases: ['Die Lohner’s', "Die Lohner's", 'Achim Lohner']},
    {id: 'ruch', label: 'Feinbäckerei Ruch', src: '/brand-local-ruch.png', aliases: ['Feinbäckerei Ruch', 'Feinbaeckerei Ruch Gmb']},
    {id: 'thiele', label: 'Feinbäckerei Thiele', src: '/brand-local-thiele.png', aliases: ['Feinbäckerei Thiele']},
    {id: 'grobe', label: 'Bäckermeister Grobe', src: '/brand-local-grobe.png', aliases: ['Bäckermeister Grobe']},
    {id: 'extrablatt', label: 'Café Extrablatt', src: '/brand-local-extrablatt.png', aliases: ['Café Extrablatt', 'Cafe Extrablatt']},
    {id: 'timberjacks', label: 'Timberjacks', src: '/brand-local-timberjacks.png', aliases: ['Timberjacks']},
    {id: 'auto-bach', label: 'Auto Bach', src: '/brand-local-auto-bach.png', aliases: ['Auto Bach']},
    {id: 'emser-therme', label: 'Emser Therme', src: '/brand-local-emser-therme.png', aliases: ['Emser Therme']},
    {id: 'bad-kreuznach-stadtwerke', label: 'Kreuznacher Stadtwerke', src: '/brand-local-bad-kreuznach-stadtwerke.png', aliases: ['Bad Kreuznacher Stadtwerke', 'Kreuznacher Stadtwerke', 'Stadtwerke GmbH Bad Kreuznach']},
    {id: 'goettingen-stadtwerke', label: 'Stadtwerke Göttingen', src: '/brand-local-goettingen-stadtwerke.png', aliases: ['Göttinger Stadtwerke', 'Stadtwerke Göttingen']},
    {id: 'ruedesheim-seilbahn', label: 'Rüdesheimer Seilbahn', src: '/brand-local-ruedesheim-seilbahn.png', aliases: ['Rüdesheimer Seilbahn', 'Seilbahn Rüdesheim']},
    {id: 'taunus-wunderland', label: 'Taunus Wunderland', src: '/brand-local-taunus-wunderland.png', aliases: ['Taunus Wunderland']},
    {id: 'phantasialand', label: 'Phantasialand', src: '/brand-local-phantasialand.png', aliases: ['Phantasialand']},
    {id: 'phaeno', label: 'phaeno Wolfsburg', src: '/brand-local-phaeno.png', aliases: ['phaeno Wolfsburg', 'phaeno']},
    {id: 'bowlhouse-limburg', label: 'Bowlhouse Limburg', src: '/brand-local-bowlhouse-limburg.png', aliases: ['Bowlhouse Limburg']},
    {id: 'fitseveneleven', label: 'Fit Seven Eleven', src: '/brand-local-fitseveneleven.jpg', aliases: ['Fit Seven Eleven', 'Fitseveneleven']},
    {id: 'oneandone', label: '1&1', src: '/brand-oneandone.png', aliases: ['1&1']},
    {id: 'gmx', label: 'GMX', src: '/brand-gmx.ico', aliases: ['GMX']},
    {id: 'webde', label: 'WEB.DE', src: '/brand-webde.ico', aliases: ['WEB.DE']},
    {id: 'arag', label: 'ARAG', src: '/brand-arag.svg', aliases: ['ARAG']},
    {id: 'eni', label: 'Eni', src: '/brand-eni.ico', aliases: ['Eni Tankstelle', 'Eni Deutschland']},
    {id: 'alternate', label: 'ALTERNATE', src: '/brand-alternate.png', aliases: ['ALTERNATE']},
    {id: 'avia', label: 'AVIA', src: '/brand-avia.ico', aliases: ['AVIA Tankstelle', 'AVIA Deutschland']},
    {id: 'bhw', label: 'BHW', src: '/brand-bhw.svg', aliases: ['BHW Bausparkasse', 'BHW Bauspar']},
    {id: 'bnp-paribas', label: 'BNP Paribas', src: '/brand-bnp-paribas.png', aliases: ['BNP Paribas']},
    {id: 'cardif', label: 'Cardif', src: '/brand-cardif.png', aliases: ['Cardif Versicherung', 'BNP Paribas Cardif']},
    {id: 'consors-finanz', label: 'Consors Finanz', src: '/brand-consors-finanz.png', aliases: ['Consors Finanz']},
    {id: 'bett1', label: 'bett1', src: '/brand-bett1.ico', aliases: ['bett1']},
    {id: 'boc', label: 'B.O.C.', src: '/brand-boc.jpg', aliases: ['B.O.C. Fahrrad', 'B.O.C. Bike']},
    {id: 'blume2000', label: 'BLUME2000', src: '/brand-blume2000.ico', aliases: ['BLUME2000']},
    {id: 'buhl', label: 'Buhl', src: '/brand-buhl.svg', aliases: ['Buhl Data', 'Buhl Software']},
    {id: 'cinemaxx', label: 'CinemaxX', src: '/brand-cinemaxx.png', aliases: ['CinemaxX']},
    {id: 'congstar', label: 'congstar', src: '/brand-congstar.png', aliases: ['congstar']},
    {id: 'dak', label: 'DAK', src: '/brand-dak.ico', aliases: ['DAK Gesundheit', 'DAK Krankenkasse']},
    {id: 'debeka', label: 'Debeka', src: '/brand-debeka.svg', aliases: ['Debeka']},
    {id: 'dehner', label: 'Dehner', src: '/brand-dehner.png', aliases: ['Dehner Gartencenter', 'Dehner Garten']},
    {id: 'drillisch', label: 'Drillisch', src: '/brand-drillisch.ico', aliases: ['Drillisch Online', 'Drillisch AG', 'Drillisch Telecom']},
    {id: 'ergo', label: 'ERGO', src: '/brand-ergo.ico', aliases: ['ERGO Versicherung', 'ERGO Group', 'ERGO Krankenversicherung AG']},
    {id: 'easypark', label: 'EasyPark', src: '/brand-easypark.png', aliases: ['EasyPark']},
    {id: 'ernstings-family', label: 'Ernsting\'s family', src: '/brand-ernstings-family.png', aliases: ['Ernsting\'s family', 'Ernstings family']},
    {id: 'fleurop', label: 'Fleurop', src: '/brand-fleurop.svg', aliases: ['Fleurop']},
    {id: 'floraprima', label: 'Floraprima', src: '/brand-floraprima.png', aliases: ['Floraprima']},
    {id: 'fraport', label: 'Fraport', src: '/brand-fraport.png', aliases: ['Fraport']},
    {id: 'hem', label: 'HEM', src: '/brand-hem.png', aliases: ['HEM Tankstelle', 'HEM Tankstellen']},
    {id: 'tamoil', label: 'Tamoil', src: '/brand-tamoil.ico', aliases: ['Tamoil']},
    {id: 'huk-coburg', label: 'HUK-Coburg', src: '/brand-huk-coburg.ico', aliases: ['HUK-Coburg']},
    {id: 'huk24', label: 'HUK24', src: '/brand-huk24.ico', aliases: ['HUK24']},
    {id: 'hannoversche', label: 'Hannoversche', src: '/brand-hannoversche.png', aliases: ['Hannoversche Lebensversicherung']},
    {id: 'heide-park', label: 'Heide Park', src: '/brand-heide-park.png', aliases: ['Heide Park']},
    {id: 'jamara', label: 'JAMARA', src: '/brand-jamara.png', aliases: ['JAMARA']},
    {id: 'jobrad', label: 'JobRad', src: '/brand-jobrad.ico', aliases: ['JobRad']},
    {id: 'mcfit', label: 'McFIT', src: '/brand-mcfit.png', aliases: ['McFIT']},
    {id: 'mcpaper', label: 'McPaper', src: '/brand-mcpaper.jpg', aliases: ['McPaper']},
    {id: 'mercure', label: 'Mercure', src: '/brand-mercure.svg', aliases: ['Mercure Hotel', 'Hotel Mercure']},
    {id: 'accor', label: 'Accor', src: '/brand-accor.ico', aliases: ['Accor Hotels', 'Accor SA']},
    {id: 'mixmarkt', label: 'Mix Markt', src: '/brand-mixmarkt.ico', aliases: ['Mix Markt']},
    {id: 'multisafepay', label: 'MultiSafepay', src: '/brand-multisafepay.ico', aliases: ['MultiSafepay']},
    {id: 'nanu-nana', label: 'Nanu-Nana', src: '/brand-nanu-nana.png', aliases: ['Nanu-Nana']},
    {id: 'nordsee', label: 'NORDSEE', src: '/brand-nordsee.png', aliases: ['NORDSEE Restaurant', 'NORDSEE GmbH']},
    {id: 'norma', label: 'NORMA', src: '/brand-norma.ico', aliases: ['NORMA Lebensmittel', 'NORMA Markt']},
    {id: 'norton', label: 'Norton', src: '/brand-norton.png', aliases: ['Norton Antivirus', 'Norton Security', 'NortonLifeLock']},
    {id: 'otelo', label: 'otelo', src: '/brand-otelo.png', aliases: ['otelo']},
    {id: 'parkster', label: 'Parkster', src: '/brand-parkster.webp', aliases: ['Parkster']},
    {id: 'paybyphone', label: 'PayByPhone', src: '/brand-paybyphone.ico', aliases: ['PayByPhone']},
    {id: 'poco', label: 'POCO', src: '/brand-poco.png', aliases: ['POCO Einrichtungsmärkte', 'POCO Möbel']},
    {id: 'qpark', label: 'Q-Park', src: '/brand-qpark.ico', aliases: ['Q-Park']},
    {id: 'rabot', label: 'RABOT', src: '/brand-rabot.png', aliases: ['RABOT Energy']},
    {id: 'raisin', label: 'Raisin', src: '/brand-raisin.png', aliases: ['Raisin Bank', 'Raisin GmbH']},
    {id: 'ratepay', label: 'Ratepay', src: '/brand-ratepay.svg', aliases: ['Ratepay']},
    {id: 'refurbed', label: 'refurbed', src: '/brand-refurbed.ico', aliases: ['refurbed']},
    {id: 'reservix', label: 'Reservix', src: '/brand-reservix.ico', aliases: ['Reservix']},
    {id: 'schoeffel', label: 'Schöffel', src: '/brand-schoeffel.ico', aliases: ['Schöffel Sportbekleidung', 'Schöffel GmbH']},
    {id: 'lowa', label: 'LOWA', src: '/brand-lowa.png', aliases: ['LOWA Sportschuhe', 'LOWA Boots']},
    {id: 'simmel', label: 'Simmel', src: '/brand-simmel.png', aliases: ['Simmel Supermarkt', 'Simmel Markt']},
    {id: 'stiftung-warentest', label: 'Stiftung Warentest', src: '/brand-stiftung-warentest.ico', aliases: ['Stiftung Warentest']},
    {id: 'tedi', label: 'TEDi', src: '/brand-tedi.png', aliases: ['TEDi']},
    {id: 'tfbank', label: 'TF Bank', src: '/brand-tfbank.ico', aliases: ['TF Bank']},
    {id: 'toom', label: 'toom', src: '/brand-toom.ico', aliases: ['toom Baumarkt', 'toom']},
    {id: 'tamaris', label: 'Tamaris', src: '/brand-tamaris.ico', aliases: ['Tamaris']},
    {id: 'tchibo', label: 'Tchibo', src: '/brand-tchibo.ico', aliases: ['Tchibo']},
    {id: 'tk', label: 'Techniker Krankenkasse', src: '/brand-tk.ico', aliases: ['Techniker Krankenkasse', 'TK Krankenkasse']},
    {id: 'tegut', label: 'tegut', src: '/brand-tegut.svg', aliases: ['tegut']},
    {id: 'ubigi', label: 'Ubigi', src: '/brand-ubigi.ico', aliases: ['Ubigi']},
    {id: 'vattenfall', label: 'Vattenfall', src: '/brand-vattenfall.ico', aliases: ['Vattenfall']},
    {id: 'vr', label: 'Volksbanken Raiffeisenbanken', src: '/brand-vr.svg', aliases: ['Volksbanken Raiffeisenbanken']},
    {id: 'amedes', label: 'amedes', src: '/brand-amedes.svg', aliases: ['amedes']},
    {id: 'klarmobil', label: 'klarmobil', src: '/brand-klarmobil.ico', aliases: ['klarmobil']},
    {id: 'united-domains', label: 'united-domains', src: '/brand-united-domains.svg', aliases: ['united-domains']},
    {id: 'winsim', label: 'winSIM', src: '/brand-winsim.ico', aliases: ['winSIM']},
    {id: '2theloo', label: '2theloo', src: '/brand-local-2theloo.png', aliases: ['2theloo']},
    {id: 'alex-gastronomie', label: 'ALEX Gastronomie', src: '/brand-local-alex.svg', aliases: ['ALEX Gastronomie', 'ALEX Restaurant']},
    {id: 'bolero-restaurants', label: 'Bolero Restaurants', src: '/brand-local-bolero.ico', aliases: ['Bolero Restaurants', 'Bolero Restaurant']},
    {id: 'deutsche-kautionskasse', label: 'Deutsche Kautionskasse', src: '/brand-local-deutsche-kautionskasse.png', aliases: ['Deutsche Kautionskasse']},
    {id: 'dpv', label: 'Deutscher Pressevertrieb', src: '/brand-local-dpv.png', aliases: ['DPV Deutscher Pressevertrieb', 'Deutscher Pressevertrieb']},
    {id: 'europabad', label: 'Europabad Karlsruhe', src: '/brand-local-europabad.svg', aliases: ['Europabad Karlsruhe']},
    {id: 'gym100-limburg', label: 'Gym100 Limburg', src: '/brand-local-gym100-limburg.svg', aliases: ['Gym100 Limburg', 'Gym 100 GmbH + Co. KG']},
    {id: 'habakuk-limburg', label: 'Habakuk Limburg', src: '/brand-local-habakuk.png', aliases: ['Habakuk Limburg']},
    {id: 'hinnerbaecker', label: 'Hinnerbäcker', src: '/brand-local-hinnerbaecker.png', aliases: ['Hinnerbäcker', 'Hinnerbaecker']},
    {id: 'imo-autopflege', label: 'IMO Autopflege', src: '/brand-local-imo.png', aliases: ['IMO Autopflege']},
    {id: 'jumpnfun-limburg', label: 'Jump’n Fun Arena Limburg', src: '/brand-local-jumpnfun-arena.png', aliases: ['Jump’n Fun Arena Limburg', "Jump'n Fun Arena Limburg"]},
    {id: 'minera-kraftstoffe', label: 'Minera Kraftstoffe', src: '/brand-local-minera.svg', aliases: ['Minera Kraftstoffe']},
    {id: 'myers-goettingen', label: 'Café Myer’s Göttingen', src: '/brand-local-myers.jpg', aliases: ['Café Myer’s Göttingen', 'Cafe Myers Göttingen']},
    {id: 'neusehland', label: 'Neusehland', src: '/brand-local-neusehland.svg', aliases: ['Neusehland']},
    {id: 'photolini', label: 'Photolini', src: '/brand-local-photolini.png', aliases: ['Photolini']},
    {id: 'rhein-main-therme', label: 'Rhein-Main-Therme', src: '/brand-local-rhein-main-therme.svg', aliases: ['Rhein-Main-Therme']},
    {id: 'serways', label: 'Serways', src: '/brand-local-serways.png', aliases: ['Serways']},
    {id: 'ssp-deutschland', label: 'SSP Deutschland', src: '/brand-local-ssp.svg', aliases: ['SSP Deutschland']},
    {id: 'tank-rast', label: 'Tank & Rast', src: '/brand-local-tank-rast.png', aliases: ['Tank & Rast']},
    {id: 'tournesol-idstein', label: 'Tournesol Idstein', src: '/brand-local-tournesol.svg', aliases: ['Tournesol Idstein']},
    {id: 'zeiss', label: 'ZEISS', src: '/brand-local-zeiss.png', aliases: ['Carl Zeiss', 'ZEISS AG', 'ZEISS Vision']},
    {id: 'soonwald-gruenewald', label: 'Soonwald-Bäckerei Grünewald', src: '/brand-local-soonwald.png', aliases: ['Soonwald-Bäckerei Grünewald GmbH', 'Bäckerei Grünewald', 'Soonwaldbaeckerei Grue']},
    {id: 'apotheke-werkstadt', label: 'Apotheke in der WERKStadt', src: '/brand-local-apotheke-werkstadt.jpg', aliases: ['Apotheke in der WERKStadt']},
    {id: 'aartal-apotheke', label: 'Aartal-Apotheke', src: '/brand-local-aartal-apotheke.png', aliases: ['Aartal-Apotheke']},
    {id: 'unica-doener', label: 'Unica Döner Limburg', src: '/brand-local-unica-doener.png', aliases: ['Unica Döner Limburg', 'Unica Doener Limburg']},
    {id: 'bereket-center', label: 'Bereket Center', src: '/brand-local-bereket-center.png', aliases: ['Bereket Center GmbH & Co. KG', 'Bereket Center', 'Berekt Center GmbH u Co KG']},
    {id: 'dorea', label: 'DOREA FAMILIE', src: '/brand-local-dorea.svg', aliases: ['DOREA FAMILIE', 'DOREA GmbH', 'DOREA Gamma Beteiligungsgesellschaft mbH']},
    {id: 'baeckerei-moos', label: 'Bäckerei Moos', src: '/brand-local-moos.png', aliases: ['Bäckerei Moos GmbH', 'Bäckerei Moos']},
    {id: 'baerentreff', label: 'Bären-Treff', src: '/brand-local-baerentreff.svg', aliases: ['Bären-Treff', 'Bärentreff']},
    {id: 'adobe', label: 'Adobe', src: '/brand-adobe.ico', aliases: ['Adobe']},
    {id: 'openai', label: 'OpenAI', src: '/brand-openai.svg', aliases: ['OpenAI', 'ChatGPT']},
    {id: 'lime', label: 'Lime', src: '/brand-lime.webp', aliases: ['Lime Scooter', 'Lime Micromobility']},
    {id: 'galeria', label: 'GALERIA', src: '/brand-galeria.ico', aliases: ['GALERIA Kaufhof', 'GALERIA Karstadt', 'GALERIA Warenhaus']},
    {id: 'enbw', label: 'EnBW', src: '/brand-enbw.ico', aliases: ['EnBW']},
    {id: 'disneyplus', label: 'Disney+', src: '/brand-disneyplus.png', aliases: ['Disney+']},
    {id: 'douglas-group', label: 'DOUGLAS GROUP', src: '/brand-douglas.svg', aliases: ['DOUGLAS GROUP']},
    {id: 'netto-marken-discount', label: 'Netto Marken-Discount', src: '/brand-netto-marken-discount.webp', aliases: ['Netto Marken-Discount', 'Netto Markendiscount']},
    {id: 'temu', label: 'Temu', src: '/brand-temu.ico', aliases: ['Temu']},
    {id: 'shop-apotheke', label: 'Shop Apotheke', src: '/brand-shop-apotheke.png', aliases: ['Shop Apotheke', 'Shop-Apotheke']},
    {id: 'totalenergies', label: 'TotalEnergies', src: '/brand-totalenergies.ico', aliases: ['TotalEnergies']},
    {id: 'intersport', label: 'INTERSPORT', src: '/brand-intersport.svg', aliases: ['INTERSPORT']},
    {id: 'beitragsservice', label: 'ARD ZDF Deutschlandradio Beitragsservice', src: '/brand-beitragsservice.ico', aliases: ['ARD ZDF Deutschlandradio Beitragsservice', 'Beitragsservice', 'Rundfunkbeitrag']},
    {id: 'zattoo', label: 'Zattoo', src: '/brand-zattoo.svg', aliases: ['Zattoo']},
    {id: 'arbeitsagentur', label: 'Bundesagentur für Arbeit', src: '/brand-arbeitsagentur.png', aliases: ['Bundesagentur für Arbeit', 'Agentur für Arbeit', 'Arbeitsagentur']},
    {id: 'babbel', label: 'Babbel', src: '/brand-babbel.svg', aliases: ['Babbel']},
    {id: 'bauhaus', label: 'BAUHAUS', src: '/brand-bauhaus.jpg', aliases: ['BAUHAUS Baumarkt', 'BAUHAUS Fachcentrum', 'BAUHAUS Deutschland']},
    {id: 'bolt', label: 'Bolt', src: '/brand-bolt.svg', aliases: ['Bolt Scooter', 'Bolt Mobility']},
    {id: 'c-and-a', label: 'C&A', src: '/brand-c-and-a.svg', aliases: ['C&A']},
    {id: 'cewe', label: 'CEWE', src: '/brand-cewe.png', aliases: ['CEWE']},
    {id: 'check24', label: 'CHECK24', src: '/brand-check24.png', aliases: ['CHECK24']},
    {id: 'decathlon', label: 'Decathlon', src: '/brand-decathlon.ico', aliases: ['Decathlon']},
    {id: 'eventim', label: 'Eventim', src: '/brand-eventim.ico', aliases: ['Eventim', 'CTS Eventim']},
    {id: 'fressnapf', label: 'Fressnapf', src: '/brand-fressnapf.png', aliases: ['Fressnapf']},
    {id: 'holiday-inn', label: 'Holiday Inn', src: '/brand-holiday-inn.png', aliases: ['Holiday Inn']},
    {id: 'home24', label: 'Home24', src: '/brand-home24.ico', aliases: ['Home24']},
    {id: 'immoscout24', label: 'ImmoScout24', src: '/brand-immoscout24.png', aliases: ['ImmoScout24', 'ImmobilienScout24']},
    {id: 'lightricks', label: 'Lightricks', src: '/brand-lightricks.png', aliases: ['Lightricks']},
    {id: 'microsoft', label: 'Microsoft', src: '/brand-microsoft.ico', aliases: ['Microsoft']},
    {id: 'aramark', label: 'Aramark', src: '/brand-aramark.png', aliases: ['Aramark']},
    {id: 'thomann', label: 'Thomann', src: '/brand-thomann.png', aliases: ['Thomann Musikhaus', 'Musikhaus Thomann']},
    {id: 'thalia-buecher', label: 'Thalia Bücher', src: '/brand-thalia.png', aliases: ['Thalia Bücher', 'Thalia Buchhandlung', 'Thalia Buchhandlung GmbH']},
  ];

  // Unicode letters and numbers define merchant token boundaries; underscores and
  // punctuation delimit names, while longer words never match short brands.
  function escaped(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+');
  }

  const intermediaryIds = new Set(['paypal', 'sumup', 'klarna', 'adyen', 'payone',
    'stripe', 'ratepay', 'multisafepay']);
  const aliasMatchers = catalog.flatMap(brand => brand.aliases.map(alias => ({
    brand,
    pattern: new RegExp(`(?<![\\p{L}\\p{N}])${escaped(alias)}(?![\\p{L}\\p{N}])`, 'iu'),
  })));

  function findBrands(value, {ignoreIntermediaries = false} = {}) {
    if (typeof value !== 'string' || !value.trim()) return [];
    const matches = new Map();
    for (const {brand, pattern} of aliasMatchers) {
      if (ignoreIntermediaries && intermediaryIds.has(brand.id)) continue;
      const match = pattern.exec(value);
      if (!match) continue;
      const current = matches.get(brand.id);
      if (!current || match.index < current.index || (match.index === current.index && match[0].length > current.length)) {
        matches.set(brand.id, {brand, index: match.index, length: match[0].length});
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
  const intermediaryPattern = /(?<![\p{L}\p{N}])(?:PayPal|SumUp|Klarna|Adyen|PAYONE|Stripe|Ratepay|MultiSafepay)(?![\p{L}\p{N}])/giu;
  const leadingIntermediary = /^\s*(?:PayPal|SumUp|Klarna|Adyen|PAYONE|Stripe|Ratepay|MultiSafepay)(?![\p{L}\p{N}])/iu;
  const cardSettlement = /(?<![\p{L}\p{N}])(?:Kreditkarten|Karten)abrechnung(?![\p{L}\p{N}])/iu;
  // IBAN lengths are defined per country. A full leading IBAN is stripped only
  // for matching; the original counterparty remains visible in the booking.
  const ibanLengths = {AT: 20, BE: 16, CH: 21, DE: 22, ES: 24, FR: 27,
    GB: 22, IT: 27, LU: 20, NL: 18, PL: 28, PT: 25};
  const numericIbanCountries = new Set(['AT', 'BE', 'DE', 'ES', 'PL', 'PT']);

  function validIban(compact) {
    let remainder = 0;
    for (const character of compact.slice(4) + compact.slice(0, 4)) {
      const digits = /[A-Z]/u.test(character) ? String(character.charCodeAt(0) - 55) : character;
      for (const digit of digits) remainder = (remainder * 10 + Number(digit)) % 97;
    }
    return remainder === 1;
  }

  function withoutLeadingIban(value) {
    if (typeof value !== 'string') return '';
    const start = /^\s*([A-Z]{2})\d{2}/iu.exec(value);
    const length = start && ibanLengths[start[1].toUpperCase()];
    if (!length) return value;
    let position = start.index + start[0].length;
    let compact = start[0].trim().replace(/\s/gu, '').toUpperCase();
    while (compact.length < length && position < value.length) {
      const character = value[position];
      if (/\s/u.test(character)) {
        position += 1;
        continue;
      }
      if (!/[A-Z0-9]/iu.test(character)) return value;
      compact += character.toUpperCase();
      position += 1;
    }
    const numericBban = numericIbanCountries.has(start[1].toUpperCase());
    const nextCharacter = value[position] || '';
    if (compact.length !== length
        || (numericBban && !/^\d+$/u.test(compact.slice(4)))
        || !validIban(compact)
        || (numericBban ? /\p{N}/u : /[\p{L}\p{N}]/u).test(nextCharacter)) return value;
    const remainder = value.slice(position).replace(/^[\s,;:/–—-]+/u, '').trim();
    return remainder || value;
  }

  function isGeneric(value) {
    const normalized = (typeof value === 'string' ? value : '').trim().toLocaleLowerCase('de-DE')
      .replace(/\s+/g, ' ');
    return genericCounterparties.has(normalized);
  }

  function paymentProcessor(value) {
    if (typeof value !== 'string') return null;
    intermediaryPattern.lastIndex = 0;
    const match = intermediaryPattern.exec(value);
    return match ? match[0].toLocaleLowerCase('de-DE') : null;
  }

  function intermediaryParty(party, purpose) {
    return leadingIntermediary.test(party)
      || (/^\s*Deutsche Bank(?![\p{L}\p{N}])/iu.test(party)
        && cardSettlement.test(`${party} ${purpose}`));
  }

  function identify({counterparty = '', description = ''} = {}) {
    const party = withoutLeadingIban(counterparty);
    const purpose = typeof description === 'string' ? description : '';
    const exactParty = party.trim().toLocaleLowerCase('de-DE').replace(/\s+/gu, ' ');
    const exactOnly = {shell: 'shell', aral: 'aral', globus: 'globus', rabot: 'rabot',
      lime: 'lime', galeria: 'galeria', bolt: 'bolt', bauhaus: 'bauhaus',
      thomann: 'thomann'};
    const exactId = Object.hasOwn(exactOnly, exactParty) ? exactOnly[exactParty] : null;
    const exactBrand = exactId && catalog.find(brand => brand.id === exactId);
    if (exactBrand) return {id: exactBrand.id, label: exactBrand.label, src: exactBrand.src};
    const processor = paymentProcessor(party);
    const bankSettlement = /^\s*Deutsche Bank(?![\p{L}\p{N}])/iu.test(party)
      && cardSettlement.test(`${party} ${purpose}`);
    const intermediary = intermediaryParty(party, purpose);
    const ordinaryPartyBrands = findBrands(party, {ignoreIntermediaries: true})
      .filter(candidate => !bankSettlement || candidate.brand.id !== 'deutsche-bank');
    if (ordinaryPartyBrands.length > 1) return null;
    if (!intermediary && ordinaryPartyBrands.length === 1) {
      const direct = ordinaryPartyBrands[0].brand;
      return {id: direct.id, label: direct.label, src: direct.src};
    }
    const canUseDescription = isGeneric(party) || intermediary;
    if (!canUseDescription) return null;

    const purposeProcessor = paymentProcessor(purpose);
    const purposeBrands = findBrands(purpose, {ignoreIntermediaries: true})
      .filter(candidate => !bankSettlement || candidate.brand.id !== 'deutsche-bank');
    const merchants = new Map([...ordinaryPartyBrands, ...purposeBrands]
      .map(candidate => [candidate.brand.id, candidate.brand]));
    if (merchants.size > 1) return null;
    if (merchants.size === 1) {
      const merchant = [...merchants.values()][0];
      return {id: merchant.id, label: merchant.label, src: merchant.src};
    }

    intermediaryPattern.lastIndex = 0;
    const processors = new Set([...party.matchAll(intermediaryPattern), ...purpose.matchAll(intermediaryPattern)]
      .map(match => match[0].toLocaleLowerCase('de-DE')));
    if (processors.size > 1) return null;
    const fallback = processor || purposeProcessor
      || (intermediary && /^\s*Deutsche Bank(?![\p{L}\p{N}])/iu.test(party) ? 'deutsche bank' : null);
    const fallbackIds = {paypal: 'paypal', sumup: 'sumup', klarna: 'klarna',
      adyen: 'adyen', payone: 'payone', stripe: 'stripe', ratepay: 'ratepay',
      multisafepay: 'multisafepay', 'deutsche bank': 'deutsche-bank'};
    const fallbackBrand = catalog.find(brand => brand.id === fallbackIds[fallback]);
    if (fallbackBrand) {
      return {id: fallbackBrand.id, label: fallbackBrand.label, src: fallbackBrand.src};
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

  function safePictogram(node) {
    const svgNamespace = 'http://www.w3.org/2000/svg';
    if (!(node instanceof SVGElement) || node.namespaceURI !== svgNamespace
        || node.localName !== 'svg') return null;
    const shapes = new Set(['svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g']);
    const attributes = new Set(['class', 'data-icon', 'viewBox', 'fill', 'stroke',
      'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'aria-hidden', 'focusable',
      'width', 'height', 'd', 'cx', 'cy', 'r', 'x', 'y', 'rx', 'x1', 'x2', 'y1', 'y2',
      'points']);
    for (const part of [node, ...node.querySelectorAll('*')]) {
      if (part.namespaceURI !== svgNamespace || !shapes.has(part.localName)) return null;
      for (const attribute of part.attributes) {
        if (!attributes.has(attribute.name) || /url\s*\(|javascript:/iu.test(attribute.value)) return null;
      }
    }
    const pictogram = node.cloneNode(true);
    pictogram.setAttribute('aria-hidden', 'true');
    pictogram.setAttribute('focusable', 'false');
    return pictogram;
  }

  function decorate(element, item = {}, {fallbackIcon} = {}) {
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
      if (identity.id === 'gym100-limburg') image.classList.add('fc-counterparty-logo--dark');
      image.src = identity.src;
      image.alt = '';
      image.setAttribute('aria-hidden', 'true');
      image.loading = 'lazy';
      image.decoding = 'async';
      image.draggable = false;
      content.append(image);
    } else {
      let pictogram = null;
      if (typeof fallbackIcon === 'function') {
        try {
          pictogram = safePictogram(fallbackIcon(item));
        } catch (_) {
          pictogram = null;
        }
      }
      const badge = document.createElement('span');
      badge.setAttribute('aria-hidden', 'true');
      badge.className = pictogram ? 'fc-counterparty-pictogram' : 'fc-counterparty-initials';
      if (pictogram) badge.append(pictogram);
      else badge.textContent = initials(displayText);
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
