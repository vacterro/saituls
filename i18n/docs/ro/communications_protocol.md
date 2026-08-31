# Protocol de comunicare pentru evaluarea ideilor

Toți agenții ulteriori care lucrează în ecosistemul de personalizare a meniului contextual Windows trebuie să respecte acest protocol structurat atunci când contestă, evaluează sau perfecționează idei propuse.

## 1. Doctrina "Steel-Man"
Înainte de a contesta o idee, agentul de evaluare trebuie să construiască cea mai puternică versiune posibilă a propunerii originale.
- Articularea propunerii de valoare centrală mai clar decât autorul original.
- Identificarea a cel puțin unui beneficiu nedeclarat al abordării.

## 2. Red-Teaming (evaluarea vulnerabilităților)
Odată ce ideea este întărită, agenții trebuie să o conteste de-a lungul următorilor vectori:
- **Potrivirea cu ecosistemul:** Se simte ca un instrument nativ de meniu contextual sau încearcă să fie o aplicație completă?
- **Performanță:** Ce se întâmplă dacă este rulat accidental pe un director cu 100.000 de fișiere?
- **Distructivitate:** Există riscul pierderii ireversibile de date?
- **Sarcina dependențelor:** Necesită dependențe externe excesive (de exemplu, biblioteci Python uriașe sau binare neinstalate)?

## 3. Formatul respingerii
Orice critică trebuie structurată astfel:
- **Ipoteză:** Ce își propune ideea să rezolve.
- **Vulnerabilitate:** Defectul sau riscul specific identificat.
- **Formulare alternativă:** O pivotare propusă care păstrează valoarea reducând în același timp riscul.

## 4. Mecanismul verdictului final
Ideile nu trebuie respinse complet fără a propune o pivotare, cu excepția cazului în care prezintă un risc catastrofal pentru sistemul de fișiere (de exemplu, ștergerea recursivă netrasată).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
