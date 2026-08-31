# Komunikacijski protokol za procjenu ideja

Svi kasniji agenti koji rade u ekosustavu prilagodbe kontekstnog izbornika sustava Windows moraju se pridržavati ovog strukturiranog protokola kada osporavaju, procjenjuju ili usavršavaju predložene ideje.

## 1. Doktrina "Steel-Man"
Prije osporavanja ideje, agent koji procjenjuje mora izgraditi najjaču moguću verziju izvornog prijedloga.
- Artikulirati središnju vrijednosnu ponudu jasnije od izvornog autora.
- Identificirati barem jednu neizrečenu korist pristupa.

## 2. Red-Teaming (procjena ranjivosti)
Nakon što je ideja ojačana, agenti je moraju osporiti duž sljedećih vektora:
- **Uklapanje u ekosustav:** Osjeća li se ovo kao izvorni alat kontekstnog izbornika ili pokušava biti punopravna aplikacija?
- **Performanse:** Što se događa ako se slučajno pokrene na mapi sa 100 000 datoteka?
- **Destruktivnost:** Postoji li rizik od nepovratnog gubitka podataka?
- **Teret ovisnosti:** Zahtijeva li prekomjerne vanjske ovisnosti (npr. ogromne Python biblioteke ili neinstalirane binarne datoteke)?

## 3. Format opovrgavanja
Svaka kritika mora biti strukturirana na sljedeći način:
- **Hipoteza:** Što ideja želi riješiti.
- **Ranjivost:** Specifični utvrđeni nedostatak ili rizik.
- **Alternativna formulacija:** Predloženi zaokret koji zadržava vrijednost dok ublažava rizik.

## 4. Mehanizam konačne presude
Ideje se ne smiju u potpunosti odbaciti bez prijedloga zaokreta, osim ako predstavljaju katastrofalan rizik za datotečni sustav (npr. nepratno rekurzivno brisanje).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
