# Protocollo di comunicazione per la valutazione delle idee

Tutti gli agenti successivi che lavorano nell'ecosistema di personalizzazione del menu contestuale di Windows devono aderire a questo protocollo strutturato quando contestano, valutano o perfezionano le idee proposte.

## 1. La dottrina dello "Steel-Man"
Prima di contestare un'idea, l'agente valutatore deve costruire la versione più forte possibile della proposta originale.
- Articolare la proposta di valore centrale più chiaramente dell'autore originale.
- Identificare almeno un vantaggio non dichiarato dell'approccio.

## 2. Red-Teaming (valutazione delle vulnerabilità)
Una volta rafforzata l'idea, gli agenti devono sfidarla lungo i seguenti vettori:
- **Adattamento all'ecosistema:** Sembra uno strumento nativo del menu contestuale o sta cercando di essere un'applicazione completa?
- **Prestazioni:** Cosa succede se viene eseguito accidentalmente su una directory con 100.000 file?
- **Distruttività:** Esiste il rischio di perdita irreversibile di dati?
- **Costo delle dipendenze:** Richiede dipendenze esterne eccessive (ad esempio, enormi librerie Python o binari non installati)?

## 3. Il formato della confutazione
Qualsiasi critica deve essere strutturata come segue:
- **Ipotesi:** Cosa mira a risolvere l'idea.
- **Vulnerabilità:** Il difetto o il rischio specifico identificato.
- **Formulazione alternativa:** Una svolta proposta che mantiene il valore mitigando il rischio.

## 4. Meccanismo del verdetto finale
Le idee non devono essere respinte senza proporre una svolta, a meno che non comportino un rischio catastrofico per il file system (ad esempio, eliminazione ricorsiva non tracciata).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
