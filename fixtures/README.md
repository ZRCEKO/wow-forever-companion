# Fixtures uniquement fictives

Ces CSV sont écrits à la main pour les tests. Aucun prix, nom, volume ou taux ne
décrit l'économie réelle. Les identifiants numériques servent seulement à tester
le contrat `itemId` ; aucune méthode d'obtention ni correspondance Forever n'est affirmée.

Les quatre marchés de la démo reçoivent volontairement les **mêmes valeurs**. Ils
démontrent l'isolation région/règles/faction, pas des différences économiques.
Les slugs `fixture-*` ne sont pas des identifiants TSM valides vérifiés.

- `realm_items.csv` : zéros, champs absents et premier instant.
- `realm_items_later.csv` : nouvel instant, disparition d'un item (pas une vente),
  colonne fournisseur inconnue conservée.
- `region_items.csv` : statistiques régionales distinctes des prix du marché.

Les dates anciennes sont intentionnelles : l'API doit signaler les données périmées.
