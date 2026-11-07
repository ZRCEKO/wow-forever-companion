# Spécimen de session — entièrement fictif

`addon/` et `profile.synthetic.json` sont des créations entièrement fictives pour
la qualification CLI/Docker des archives de catalogues. Aucun contenu de guide
tiers ni profil réel : zone, objet 999999 et trajet inventés, aucun rendement.
La CI les importe explicitement avec `--dataset synthetic`, vérifie le rejeu,
l'isolation API et la sauvegarde/restauration. Aucun import automatique sur le NAS.

`session.synthetic.json` illustre le **contrat proposé**, non encore implémenté,
de [la première tranche de phase 3](../../docs/PHASE3_FARMING_SESSIONS.md).
Il ne provient d'aucun joueur ou export réel et n'atteste aucun lieu ou rendement.
L'objet 2447 réutilise uniquement l'identifiant de base des fixtures existantes.

Durée murale 2 100 s, active 1 800 s, pause 300 s, quantité inventée 12 : le taux
attendu pour cette session est 24 objets/h. Le cuivre direct zéro est explicitement
renseigné ; les coûts inconnus restent `null`. Aucun GPH monétaire n'est fourni.

Le fichier n'est chargé ni par `goldmaking demo`, ni par l'API, ni par le collecteur.
Il n'est pas aujourd'hui accepté par une commande d'import de farming. Sa présence
prépare les prochains contrats et tests ; elle ne crée aucune donnée sur le NAS.
