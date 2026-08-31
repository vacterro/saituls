# Protocole de communication pour l'évaluation d'idées

Tous les agents ultérieurs travaillant dans l'écosystème de personnalisation du menu contextuel Windows doivent adhérer à ce protocole structuré lorsqu'ils contestent, évaluent ou affinent des idées proposées.

## 1. La doctrine de l'"homme de paille inversé" (Steel-Man)
Avant de contester une idée, l'agent évaluateur doit construire la version la plus forte possible de la proposition originale.
- Articuler la proposition de valeur centrale plus clairement que l'auteur original.
- Identifier au moins un avantage non énoncé de l'approche.

## 2. Red-Teaming (évaluation des vulnérabilités)
Une fois l'idée renforcée, les agents doivent la contester selon les vecteurs suivants :
- **Adéquation à l'écosystème :** Cela ressemble-t-il à un outil natif de menu contextuel ou tente-t-il d'être une application complète ?
- **Performances :** Que se passe-t-il si cela est exécuté par accident sur un répertoire de 100 000 fichiers ?
- **Destructivité :** Existe-t-il un risque de perte de données irrécupérables ?
- **Surcharge de dépendances :** Cela nécessite-t-il des dépendances externes excessives (par exemple, d'énormes bibliothèques Python ou des binaires non installés) ?

## 3. Le format de réfutation
Toute critique doit être structurée comme suit :
- **Hypothèse :** Ce que l'idée vise à résoudre.
- **Vulnérabilité :** Le défaut ou le risque spécifique identifié.
- **Formulation alternative :** Une proposition de pivot qui conserve la valeur tout en atténuant le risque.

## 4. Mécanisme de verdict final
Les idées ne doivent pas être rejetées d'emblée sans proposer un pivot, sauf si elles présentent un risque catastrophique pour le système de fichiers (par exemple, une suppression récursive non suivie).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
