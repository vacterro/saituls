# Kommunikasjonsprotokoll for idévurdering

Alle etterfølgende agenter som jobber i økosystemet for tilpasning av Windows-kontekstmenyen, må følge denne strukturerte protokollen når de utfordrer, evaluerer eller foredler foreslåtte ideer.

## 1. "Steel-Man"-doktrinen
Før en idé utfordres, må den evaluerende agenten konstruere den sterkest mulige versjonen av det opprinnelige forslaget.
- Artikulere det sentrale verditilbudet tydeligere enn den opprinnelige forfatteren.
- Identifisere minst én uuttalt fordel ved tilnærmingen.

## 2. Red-Teaming (sårbarhetsvurdering)
Når ideen er gjort sterk, må agenter utfordre den langs følgende vektorer:
- **Økosystemtilpasning:** Føles dette som et innebygd kontekstmenyverktøy, eller prøver det å være en fullverdig applikasjon?
- **Ytelse:** Hva skjer hvis dette ved et uhell kjøres på en katalog med 100 000 filer?
- **Destruktivitet:** Er det risiko for uopprettelig datatap?
- **Avhengighetskostnad:** Krever dette overdrevne eksterne avhengigheter (f.eks. enorme Python-biblioteker eller ikke-installerte binærfiler)?

## 3. Motbevisingsformatet
All kritikk må struktureres som følger:
- **Hypotese:** Hva ideen har til hensikt å løse.
- **Sårbarhet:** Den spesifikke feilen eller risikoen som er identifisert.
- **Alternativ formulering:** Et foreslått vendepunkt som beholder verdien samtidig som risikoen reduseres.

## 4. Mekanisme for endelig avgjørelse
Ideer må ikke avvises uten å foreslå et vendepunkt, med mindre de utgjør en katastrofal risiko for filsystemet (f.eks. usporet rekursiv sletting).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
