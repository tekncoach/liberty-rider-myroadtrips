# Politique de sécurité

Merci de signaler tout problème de sécurité — ce projet manipule des
identifiants Liberty Rider et des traces GPS, deux catégories de données où
un bug a des conséquences réelles.

## Signaler une vulnérabilité

**N'ouvrez pas d'issue publique.** Utilisez le *private vulnerability
reporting* de GitHub : onglet **Security → Report a vulnerability** du dépôt.
À défaut, écrivez à l'adresse du mainteneur figurant dans les commits git.

Merci d'inclure : ce que vous avez observé, comment le reproduire, et
l'impact que vous estimez. Un correctif proposé est bienvenu mais pas exigé.

Délais indicatifs sur ce projet personnel : accusé de réception sous 7 jours,
premier diagnostic sous 30 jours. Les vulnérabilités confirmées sont
corrigées sur `main` puis déployées ; vous serez crédité·e si vous le
souhaitez.

## Périmètre

**Dans le périmètre** — le code de ce dépôt et le déploiement de démonstration :
authentification et gestion de session, cloisonnement entre comptes,
injection (SQL, XSS, SSRF), exposition de données personnelles (traces GPS,
emails, jetons), configuration CI/CD et chaîne d'approvisionnement.

**Hors périmètre** — l'API Liberty Rider elle-même et l'infrastructure de
Liberty Rider (ce projet en est un client indépendant, non affilié :
signalez-leur directement) ; le déni de service par volume brut ; les
rapports issus d'un scanner automatique sans démonstration d'impact.

## Ce que l'application stocke

- Les **jetons Firebase** (bearer + refresh) du compte Liberty Rider
  connecté, nécessaires pour synchroniser sans redemander le mot de passe.
  Le mot de passe lui-même n'est **jamais** stocké : il est transmis une
  seule fois à Firebase. Les jetons sont effacés à la déconnexion.
- Les **traces GPS** synchronisées, l'email et le prénom du compte.
- Aucune donnée n'est transmise à un tiers en dehors de Liberty Rider
  (synchronisation), OpenStreetMap/Overpass (noms de cols) et
  open-elevation (profils d'altitude), qui reçoivent des coordonnées.

Pour tout supprimer : déconnectez-vous et demandez la purge au mainteneur
(`DELETE /api/account/data` côté serveur).
