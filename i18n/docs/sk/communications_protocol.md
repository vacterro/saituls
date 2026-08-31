# Komunikačný protokol na hodnotenie nápadov

Všetci nadväzujúci agenti pracujúci v ekosystéme prispôsobenia kontextovej ponuky systému Windows musia dodržiavať tento štruktúrovaný protokol pri spochybňovaní, hodnotení alebo vylepšovaní navrhovaných nápadov.

## 1. Doktrína "Steel-Man"
Pred spochybnením nápadu musí hodnotiaci agent zostaviť najsilnejšiu možnú verziu pôvodného návrhu.
- Vyjadriť základnú hodnotovú ponuku jasnejšie ako pôvodný autor.
- Identifikovať aspoň jeden neuvedený prínos prístupu.

## 2. Red-Teaming (hodnotenie zraniteľnosti)
Keď je nápad posilnený, agenti ho musia spochybniť podľa nasledujúcich vektorov:
- **Zhoda s ekosystémom:** Pôsobí to ako natívny nástroj kontextovej ponuky alebo sa snaží byť plnohodnotnou aplikáciou?
- **Výkon:** Čo sa stane, ak sa to omylom spustí v adresári so 100 000 súbormi?
- **Deštruktívnosť:** Existuje riziko nezvratnej straty údajov?
- **Záťaž závislostí:** Vyžaduje to nadmerné externé závislosti (napr. obrovské knižnice Pythonu alebo nenainštalované binárne súbory)?

## 3. Formát vyvrátenia
Akákoľvek kritika musí byť štruktúrovaná takto:
- **Hypotéza:** Čo sa nápad snaží vyriešiť.
- **Zraniteľnosť:** Konkrétna zistená chyba alebo riziko.
- **Alternatívna formulácia:** Navrhnutý obrat, ktorý zachováva hodnotu a zároveň znižuje riziko.

## 4. Mechanizmus konečného verdiktu
Nápady by sa nemali úplne odmietnuť bez návrhu obratu, pokiaľ nepredstavujú katastrofálne riziko pre súborový systém (napr. nesledované rekurzívne mazanie).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
