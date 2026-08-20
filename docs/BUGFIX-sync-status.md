# Bugfix — le statut de sync affichait « à jour » à tort

Branche : `fix/sync-status`.

## Symptôme

Le menu en bas à gauche (`#userbar`) affiche en permanence
« *N* trajets Liberty Rider · à jour », y compris quand des traces récentes
attendent côté Liberty Rider. L'utilisateur doit appuyer sur le bouton orange
`⟳` pour découvrir qu'il y avait effectivement du nouveau.

## Cause racine

Le libellé était calculé **uniquement à partir de deux compteurs**, dont un
qui ne compte pas ce qu'on croit :

- `static/app.js:184-191` (`profileSubText`, avant correctif) :
  ```js
  const remote = profile.manual_ride_count;
  const local = state.ungrouped.length;
  const diff = remote - local;
  if (diff > 0) return `${remote} trajets Liberty Rider · ≈${diff} à synchroniser`;
  return `${remote} trajets Liberty Rider · à jour`;
  ```
- `app.py:729` (`GET /api/auth/profile`) renvoie
  `manual_ride_count = currentUser.manualRideCount`, le champ Liberty Rider
  dont `docs/API.md` disait déjà qu'il *« ne correspond pas nécessairement »*
  au nombre de trajets de l'app.

Trois problèmes qui se cumulent, du plus décisif au plus secondaire :

1. **`manualRideCount` n'est pas le nombre de trajets distants.** Son nom
   pointe vers les trajets *créés manuellement*, pas vers `stoppedRides`.
   C'est donc structurellement un petit nombre (0 pour un utilisateur qui
   n'a jamais saisi de trajet à la main), très inférieur au compteur local.
   `diff` est donc **toujours ≤ 0** → la branche « à jour » est la seule
   jamais atteinte. Le libellé n'est pas « faux de temps en temps » : il est
   constant.
2. **Le compteur local n'est pas comparable non plus.**
   `state.ungrouped.length` vient de `GET /api/rides`, qui exclut les traces
   absorbées par une fusion (`merged_into IS NULL`, `app.py:831`). Sur la
   base de dev : 201 traces en base, 27 fusionnées → 174 côté UI. Même avec
   un vrai compteur distant, la soustraction dériverait de 27 à chaque
   fusion.
3. **Rien n'interroge Liberty Rider sur la fraîcheur.** L'hypothèse du
   brief est confirmée dans son principe : l'état affiché ne pouvait
   dire, au mieux, que « rien de neuf depuis le dernier sync que *j'ai*
   lancé » — et `sync_state.last_sync_max_start_time` n'était même pas
   consulté par ce chemin.

**Mécanisme attendu** : « à jour » est une affirmation *sur Liberty Rider*,
elle doit donc venir de Liberty Rider — comparer le trajet distant le plus
récent à ce qu'on a déjà importé.

## Options envisagées

| # | Option | Coût par ouverture | Verdict |
|---|--------|--------------------|---------|
| 1 | Ne plus rien affirmer : afficher « *N* trajets synchronisés », sans « à jour » | 0 appel | Supprime le mensonge, pas le besoin. L'utilisateur continue de cliquer à l'aveugle. Rejetée (mais gardée comme mode dégradé). |
| 2 | Corriger le compteur distant (`stoppedRides` complet + `COUNT`) | 1 requête lourde (page de 50 trajets avec `detailedPolyline`, ~10 Ko/trajet) | Le comptage exact n'existe pas dans l'API (pas de champ `totalCount`), il faudrait paginer tout l'historique. Disproportionné. Rejetée. |
| 3 | **Sonde légère : comparer le `startTime` du trajet distant le plus récent au curseur local** | 1 petite requête (`id` + `startTime`, `first: 1`) | Répond exactement à la question posée, payload négligeable. **Retenue.** |
| 4 | Auto-sync à l'ouverture | 1 sync complet incrémental (potentiellement plusieurs pages lourdes) | Écrit en base sans que l'utilisateur l'ait demandé, latence non bornée, et frappe fort une API non officielle à chaque ouverture d'onglet. Rejetée — mais l'option 3 rend l'auto-sync trivial à ajouter plus tard si on le souhaite (le flag `pending` suffit à le déclencher). |
| 5 | Polling périodique du statut | N appels/heure/onglet | L'API Liberty Rider est non documentée et rate-limitée : on ne va pas la sonder en boucle pour un confort marginal. Rejetée. |

## Solution retenue

Une sonde légère, appelée **uniquement** à l'ouverture de l'app et après
chaque sync — jamais en boucle.

- `liberty_client.py` — nouvelle requête `LATEST_RIDE_QUERY` et méthode
  `get_latest_ride()` : `stoppedRides(first: 1) { id startTime }`. Aucun
  `detailedPolyline`, aucune pause. Le serveur ayant déjà été observé en
  train d'ignorer le `first` demandé (cf. docstring de `sync.py`), on prend
  le `max` par `startTime` de ce qui revient plutôt que de supposer une
  liste d'un seul élément.
- `sync.py` — `pending_status(client, user_id)` compare ce `startTime`
  distant à ce qu'on a localement : le curseur
  `sync_state.last_sync_max_start_time`, **ou** `MAX(rides.start_time)` si
  le curseur manque (données récupérées par `claim_orphaned_data`, premier
  sync interrompu). Sans ce repli, un compte sans curseur crierait au loup
  en permanence. Les deux dates sont des chaînes ISO-8601 UTC issues de la
  même API : comparaison lexicographique, comme le fait déjà `sync()`.
- `app.py` — `GET /api/sync/status` : renvoie
  `{pending, remote_latest_start_time, last_sync_start_time, local_rides}`,
  avec la même gestion d'erreur que `POST /api/sync` (401 si le token est
  irrécupérable, 502 sinon).
- `static/app.js` — `refreshSyncStatus()` remplace `profileSubText()`.
  `refreshProfile()` ne gère plus que le prénom/l'avatar ; `#userSub`
  appartient désormais au statut de sync. En cas d'erreur réseau/API, on
  retombe sur « *N* trajet(s) synchronisé(s) » — **statut inconnu, pas
  « à jour »**.
- `static/index.html` — pastille verte sur le bouton `⟳`
  (`#syncBtn.has-pending::after`) quand il y a quelque chose à importer, plus
  un `title` explicite. Le bouton étant déjà orange, un badge est plus lisible
  qu'un énième changement de couleur.

Coût réel : 2 petits appels GraphQL par ouverture (la sonde de token de
`_live_client_for` + la sonde de trajet), payload de l'ordre de quelques
centaines d'octets. Tout ce que la base locale sait répondre seule (nombre de
traces) reste local.

`manual_ride_count` reste exposé par `GET /api/auth/profile` (compatibilité,
il est documenté), mais `docs/API.md` dit maintenant explicitement que ce
n'est **pas** un signal de fraîcheur.

## Fichiers modifiés

| Fichier | Changement |
|---------|-----------|
| `liberty_client.py` | `LATEST_RIDE_QUERY` + `get_latest_ride()` |
| `sync.py` | `pending_status()` |
| `app.py` | `GET /api/sync/status` |
| `static/app.js` | `refreshSyncStatus()`/`syncSubText()` à la place de `profileSubText()`, 3 points d'appel |
| `static/index.html` | pastille `#syncBtn.has-pending` |
| `tests/conftest.py` | `FakeLRClient.latest_ride` + `get_latest_ride()` |
| `tests/test_sync_status.py` | *(nouveau)* 8 tests bout-en-bout du statut |
| `tests/test_liberty_client.py` | 4 tests unitaires de `get_latest_ride()` |
| `docs/API.md`, `docs/ARCHITECTURE.md` | documentation du nouvel endpoint et du raisonnement |

## Comment tester

```bash
make test          # 94 tests, dont 12 nouveaux
```

Cas couverts : jamais synchronisé → `pending: true` ; compte distant vide →
`pending: false` ; trajet le plus récent déjà importé → `pending: false` ;
nouveau trajet distant → `pending: true` ; un sync remet le flag à zéro ;
curseur absent mais traces en base → repli sur `MAX(start_time)` ; isolation
entre comptes ; 401 sans session. Côté client GraphQL : la requête ne demande
qu'un trajet et rien de lourd, le plus récent est retenu même si le serveur
renvoie une page entière, `None` sur compte vide, `RuntimeError` sur erreur
GraphQL.

Vérification manuelle (`make run`, puis se connecter) :

1. Ouvrir l'app avec tout synchronisé → « *N* trajet(s) synchronisé(s) ·
   à jour », pas de pastille.
2. Simuler un nouveau trajet distant : dans la base locale,
   `DELETE FROM sync_state WHERE key='last_sync_max_start_time';` puis
   `DELETE FROM rides WHERE start_time = (SELECT MAX(start_time) FROM rides);`
   Recharger → « des trajets à importer » + pastille verte sur `⟳`.
3. Cliquer sur `⟳` → la trace revient, le libellé repasse à « à jour » et la
   pastille disparaît, sans rechargement de page.
4. Couper le réseau (ou invalider le token) → le libellé retombe sur
   « *N* trajet(s) synchronisé(s) », sans affirmer « à jour ».
