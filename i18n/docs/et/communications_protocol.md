# Suhtlusprotokoll ideede hindamiseks

Kõik hilisemad agendid, kes töötavad Windowsi kontekstimenüü kohandamise ökosüsteemis, peavad järgima seda struktureeritud protokolli, kui nad esitavad väljakutseid, hindavad või täpsustavad pakutud ideid.

## 1. "Steel-Man" õpetus
Enne idee vaidlustamist peab hindav agent koostama algse ettepaneku tugevaima võimaliku versiooni.
- Sõnasta põhiväärtuspakkumine selgemalt kui algne autor.
- Leia vähemalt üks väljaütlemata kasu lähenemisviisist.

## 2. Red-Teaming (haavatavuse hindamine)
Pärast tugevaima versiooni koostamist peavad agendid ideed vaidlustama järgmiste vektorite lõikes:
- **Ökosüsteemi sobivus:** Kas see mõjub nagu loomulik kontekstimenüü tööriist või üritab olla täisrakendus?
- **Jõudlus:** Mis juhtub, kui seda kogemata käivitada kataloogis, kus on 100 000 faili?
- **Hävituslikkus:** Kas on oht pöördumatuks andmekaduks?
- **Sõltuvuste koorem:** Kas see nõuab liigseid väliseid sõltuvusi (nt hiiglaslikke Pythoni teeke või installimata binaare)?

## 3. Vastulause vorming
Iga kriitika peab olema struktureeritud järgmiselt:
- **Hüpotees:** Mida idee lahendada püüab.
- **Haavatavus:** Tuvastatud konkreetne viga või risk.
- **Alternatiivne sõnastus:** Pakutud pööre, mis säilitab väärtuse ja vähendab riski.

## 4. Lõpliku otsuse mehhanism
Ideid ei tohi ilma pöörde ettepanekuta otse tagasi lükata, välja arvatud juhul, kui need kujutavad endast katastroofilist ohtu failisüsteemile (nt jälgimata rekursiivne kustutamine).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
