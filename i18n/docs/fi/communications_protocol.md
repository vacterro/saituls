# Viestintäprotokolla ideoiden arviointiin

Kaikkien Windowsin pikavalikon mukauttamisen ekosysteemissä työskentelevien myöhempien agenttien on noudatettava tätä strukturoitua protokollaa haastaessaan, arvioidessaan tai jalostaessaan ehdotettuja ideoita.

## 1. "Steel-Man"-oppi
Ennen idean haastamista arvioivan agentin on rakennettava vahvin mahdollinen versio alkuperäisestä ehdotuksesta.
- Muotoile keskeinen arvolupaus selkeämmin kuin alkuperäinen kirjoittaja.
- Tunnista vähintään yksi lähestymistavan mainitsematon hyöty.

## 2. Red-Teaming (haavoittuvuuksien arviointi)
Kun idea on tehty vahvaksi, agenttien on haastettava se seuraavia vektoreita pitkin:
- **Ekologinen sopivuus:** Tuntuuko tämä natiivilta pikavalikkotyökalulta vai yrittääkö se olla täysimittainen sovellus?
- **Suorituskyky:** Mitä tapahtuu, jos tämä ajetaan vahingossa hakemistossa, jossa on 100 000 tiedostoa?
- **Tuhoisuus:** Onko olemassa peruuttamattoman tietojen menetyksen riskiä?
- **Riippuvuuksien taakka:** Edellyttääkö tämä liiallisia ulkoisia riippuvuuksia (esim. valtavia Python-kirjastoja tai asentamattomia binääritiedostoja)?

## 3. Vastaväitteen muoto
Kaiken kritiikin on oltava rakenteeltaan seuraava:
- **Hypoteesi:** Mitä idea pyrkii ratkaisemaan.
- **Haavoittuvuus:** Tunnistettu erityinen vika tai riski.
- **Vaihtoehtoinen muotoilu:** Ehdotettu käännös, joka säilyttää arvon ja lieventää riskiä.

## 4. Lopullisen tuomion mekanismi
Ideoita ei saa hylätä suoralta kädeltä ilman käännösehdotusta, elleivät ne aiheuta katastrofaalista riskiä tiedostojärjestelmälle (esim. jäljittämätön rekursiivinen poisto).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
