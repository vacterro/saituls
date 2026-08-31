# Komunikační protokol pro hodnocení nápadů

Všichni navazující agenti pracující v ekosystému přizpůsobení kontextové nabídky Windows musí dodržovat tento strukturovaný protokol při zpochybňování, hodnocení nebo vylepšování navrhovaných nápadů.

## 1. Doktrína "Steel-Man"
Před zpochybněním nápadu musí hodnotící agent sestavit nejsilnější možnou verzi původního návrhu.
- Vyjádřit základní hodnotovou nabídku jasněji než původní autor.
- Identifikovat alespoň jeden neuvedený přínos přístupu.

## 2. Red-Teaming (hodnocení zranitelnosti)
Jakmile je nápad posílen, musí ho agenti zpochybnit podle následujících vektorů:
- **Shoda s ekosystémem:** Působí to jako nativní nástroj kontextové nabídky, nebo se snaží být plnohodnotnou aplikací?
- **Výkon:** Co se stane, pokud se to omylem spustí v adresáři se 100 000 soubory?
- **Destruktivita:** Existuje riziko nevratné ztráty dat?
- **Zátěž závislostí:** Vyžaduje to nadměrné externí závislosti (např. obrovské knihovny Pythonu nebo nenainstalované binární soubory)?

## 3. Formát vyvrácení
Jakákoli kritika musí být strukturována takto:
- **Hypotéza:** Co se nápad snaží vyřešit.
- **Zranitelnost:** Konkrétní zjištěná vada nebo riziko.
- **Alternativní formulace:** Navržený obrat, který zachovává hodnotu a zároveň snižuje riziko.

## 4. Mechanismus konečného verdiktu
Nápady by neměly být zcela odmítnuty bez návrhu obratu, pokud nepředstavují katastrofální riziko pro souborový systém (např. nesledované rekurzivní mazání).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
