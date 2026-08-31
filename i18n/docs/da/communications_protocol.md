# Kommunikationsprotokol til idévurdering

Alle efterfølgende agenter, der arbejder i økosystemet for tilpasning af Windows-kontekstmenuen, skal følge denne strukturerede protokol, når de udfordrer, evaluerer eller forfiner foreslåede idéer.

## 1. "Steel-Man"-doktrinen
Før en idé udfordres, skal den evaluerende agent konstruere den stærkest mulige version af det oprindelige forslag.
- Formulere det centrale værditilbud tydeligere end den oprindelige forfatter.
- Identificere mindst én uudtalt fordel ved tilgangen.

## 2. Red-Teaming (sårbarhedsvurdering)
Når idéen er gjort stærk, skal agenter udfordre den langs følgende vektorer:
- **Økosystempasform:** Føles dette som et indbygget kontekstmenu-værktøj, eller forsøger det at være en fuld applikation?
- **Ydeevne:** Hvad sker der, hvis dette ved et uheld køres på en mappe med 100.000 filer?
- **Destruktivitet:** Er der risiko for uopretteligt datatab?
- **Afhængighedsbyrde:** Kræver dette overdrevne eksterne afhængigheder (f.eks. enorme Python-biblioteker eller ikke-installerede binærfiler)?

## 3. Modbevisningsformatet
Enhver kritik skal struktureres som følger:
- **Hypotese:** Hvad idéen har til formål at løse.
- **Sårbarhed:** Den specifikke fejl eller risiko, der er identificeret.
- **Alternativ formulering:** Et foreslået drej, der bevarer værdien og mindsker risikoen.

## 4. Mekanisme for endelig afgørelse
Idéer må ikke afvises uden at foreslå et drej, medmindre de udgør en katastrofal risiko for filsystemet (f.eks. usporet rekursiv sletning).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
