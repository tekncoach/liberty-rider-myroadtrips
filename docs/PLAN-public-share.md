# Plan — Lien public de partage d'un trajet

> **Statut : implémenté sur `feat/public-share`.** Ce document était le livrable
> de la phase 1 (le plan soumis au propriétaire avant tout code) ; il est
> conservé tel quel comme trace de la décision. Ce qui a changé à
> l'implémentation est noté en fin de document (§9). L'état de référence de la
> fonctionnalité, lui, est `docs/ARCHITECTURE.md`.

**Besoin** — pouvoir récupérer une URL publique pour un trajet déjà effectué et
l'envoyer par WhatsApp. La page montre la trace sur une carte + les infos de
base, consultable sans compte. Modèle mental : Strava / Komoot.

---

## 1. État de l'existant : qu'est-ce qui est déjà là ?

**Réponse courte : rien. La fonctionnalité n'existe à aucun degré.**

Recherche exhaustive sur `public`, `share`, `partag`, `token`, `slug`, `unauth`,
`noindex` dans `app.py`, `db.py`, `static/*`, `migrations/*.sql` : aucun résultat
lié au partage. Les seules occurrences de « token » concernent les jetons
Firebase / Liberty Rider (`bearer_token`, `refresh_token`) et l'id de session.

### Ce qui est public aujourd'hui (3 routes, aucune donnée)

| Route | Contenu |
| --- | --- |
| `GET /` (`app.py:1408`) | `static/landing.html` — page marketing |
| `GET /app` (`app.py:1413`) | `static/index.html` — la SPA, qui n'affiche que `#authScreen` tant que `/api/auth/status` n'a pas confirmé une session |
| `/static/*` (`app.py:1418`) | fichiers statiques |

**Absolument tous** les autres endpoints portent `user=Depends(get_session_user)`
(`app.py:70`), à trois exceptions près qui ne renvoient aucune donnée de trajet :
`POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/status`.

Il n'existe donc **aucun chemin de lecture non authentifié** vers un trajet. Tout
est à construire : table, endpoints, page, UI. En contrepartie, la surface
actuelle est propre — on part d'une base où l'invariant multi-tenant n'a pas de
trou à colmater.

### Ce qui est déjà réutilisable (et c'est beaucoup)

- **Décodage de polyline côté client** — `decodePolyline` / `decodePolylines`
  (`static/app.js:87-108`). Fonctions **pures**, zéro dépendance à `state` ou à
  la session. Le serveur envoie les chaînes Google-encodées telles quelles
  (`_encoded_polylines`, `app.py:340`) ; c'est le navigateur qui décode.
- **Formateurs** — `fmtKm`, `fmtDuration`, `fmtDate`, `fmtDay`, `fmtTime`,
  `fmtAlt`, `fmtAvgSpeed`, `escapeHtml` (`static/app.js:53-85`). Pures.
- **Rendu carte Leaflet** — le bloc carte de `openRideModal`
  (`static/app.js:690-738`) : tuiles OSM, `L.polyline`, marqueurs de pause,
  contrôle « recentrer », `fitBounds`. Ne lit que la variable locale `ride`.
- **Chronologie trajet/pause** — `renderRideTimeline` (`static/app.js:1252`) :
  ne lit que `ride.timeline` (déjà calculé serveur par `_merged_ride_timeline`)
  et écrit dans un id DOM fixe. Pure à un renommage d'id près.
- **Agrégation des rides fusionnés** — `_merged_ride_dict`,
  `_merged_polyline_and_pauses`, `_merged_ride_timeline` (`app.py:204-318`) :
  prennent `(conn, row)`, jamais un `user`. Directement appelables depuis un
  chemin public.
- **Le thème CSS** — les variables `:root` de `index.html:9-31` (clair/sombre)
  se copient telles quelles.

### Ce qui est couplé à la session et **ne doit pas** suivre

Dans `openRideModal` (`static/app.js:659-738`) :

| Élément | Couplage |
| --- | --- |
| `renderRideModalTags` + `#rideModalTagInput` | `POST`/`DELETE /api/rides/{id}/tags`, `state.rideModalId`, `onTagsChanged` → `state.tags` |
| Notes (`#rideModalNotesText`, contenteditable) | `PATCH /api/rides/{id}/notes` |
| Bandeau fusion + « Dissocier » | `DELETE /api/rides/{id}/merge` puis `refresh()` (recharge **toute** la liste du compte) |
| Lien GPX | `/api/rides/{id}/export.gpx` — endpoint authentifié |
| `renderElevationChart` | `fetch /api/rides/{id}/elevation` et `/cols` — authentifiés, et `/cols` **écrit** en base (`DELETE`+`INSERT` sur `ride_cols`) |
| `state.rideModalMap`, `state.rideModalId` | état global de la SPA |

**Verdict sur l'effort de factorisation : modéré, et sans réécriture du modal.**
La partie réutilisable est celle qui est déjà pure ; la partie couplée est
justement celle qu'on ne veut pas sur une page publique (éditer les tags de
quelqu'un d'autre n'a aucun sens). On ne « transforme » donc pas le modal en
page : on **extrait ~90 lignes de fonctions pures** dans un `static/shared.js`
chargé par les deux pages, et la page publique réimplémente son propre assemblage
(~120 lignes) à partir de ces briques. Voir §4, option retenue.

Le point d'attention sur le dénivelé : `/cols` a un effet de bord en base et fait
un appel réseau Overpass par pic candidat. **Le profil altimétrique et les cols
sont hors périmètre de la page publique** (v1) — ni utiles au destinataire
WhatsApp, ni gratuits.

---

## 2. Options d'architecture

### Le modèle de données

| Option | Coût | Verdict |
| --- | --- | --- |
| **A. Colonnes sur `rides`** (`share_token`, `shared_at`) | 2 `ALTER TABLE` | ❌ **Rejeté.** Régénérer un lien écrase l'ancien token : aucune trace, et rien ne garantit qu'un token réémis ne collisionne pas avec un ancien. Ne survit pas à la spec « révoquer puis régénérer ». |
| **B. Table dédiée `ride_shares`, une ligne par token émis** | 1 table + 2 index | ✅ **Retenu.** Un token = une ligne, jamais mise à jour sauf pour poser `revoked_at`. La révocation et la régénération tombent naturellement, l'historique des liens émis est conservé, et la `PRIMARY KEY` sur `token` interdit structurellement qu'un token réémis réutilise un ancien. |

### Le rendu de la page publique

| Option | Coût | Verdict |
| --- | --- | --- |
| **1. SPA publique** — `share.html` statique + `GET /api/public/rides/{token}` en JSON | ~1 fichier HTML + 1 endpoint | Cohérent avec le repo (pas de build step, exactement le schéma de `/app`). Mais : l'aperçu WhatsApp est **générique** — le HTML servi est identique pour tous les tokens, donc pas de titre ni de description du trajet dans la carte de prévisualisation. |
| **2. Rendu serveur** — HTML généré avec les données injectées | +1 dépendance (Jinja2) ou du HTML en f-string | Bon aperçu WhatsApp, pas de flash de chargement. Mais introduit un moteur de template dans un projet qui n'en a jamais eu, et duplique tout le rendu carte en Python alors qu'il existe déjà en JS. |
| **3. Hybride** — `share.html` statique, servi par une route qui **injecte uniquement les balises `<meta property="og:*">`** par un `str.replace` sur un placeholder ; les données du trajet arrivent par le fetch JSON | option 1 + ~15 lignes | ✅ **Retenu.** Zéro dépendance nouvelle, zéro moteur de template, et l'aperçu WhatsApp affiche « Col du Galibier — 187 km · 4h20 · 12 juillet 2025 ». C'est **le** cas d'usage énoncé par le propriétaire. |
| **4. Réutiliser `/api/rides/{id}` avec un token en paramètre** | ~10 lignes | ❌ **Rejeté fermement.** Fait cohabiter le chemin authentifié et le chemin public dans une même fonction : la moindre évolution future de la réponse fuite par défaut. Voir §3, invariant n°2. |

### Décision recommandée

> **Table `ride_shares` (une ligne par token émis) + endpoint JSON public dédié
> avec liste blanche explicite de champs + page `share.html` statique servie sur
> `/t/{token}` avec injection des seules balises Open Graph.**

Pas d'ORM, pas de build step, pas de dépendance ajoutée : on reste dans l'idiome
du projet.

---

## 3. Sécurité et vie privée — le cœur du sujet

Une URL publique non authentifiée expose une trace GPS. Une trace de moto part
presque toujours du domicile. C'est la partie du plan qui ne se négocie pas.

### Les cinq invariants

**Invariant 1 — Opt-in strict, par trajet.**
Rien n'est public par défaut. Aucun trajet n'a de ligne dans `ride_shares` tant
que l'utilisateur n'a pas cliqué. Aucune action en masse (« tout partager »),
aucun partage de roadtrip ou de tag en v1 : un lien = un trajet.

**Invariant 2 — La résolution se fait *par token*, jamais par id.**
Le chemin public ne prend **jamais** de `ride_id` en entrée. La signature de la
fonction de lookup est :

```python
def _get_shared_ride(conn, token: str):
    """Le pendant public de _get_owned_ride : résout un trajet par token de
    partage actif, jamais par id. Un id de trajet passé ici ne peut pas
    matcher — c'est l'invariant qui rend la fuite entre comptes impossible
    par construction."""
```

Un `user_id` n'apparaît nulle part dans le chemin public : le token *est*
l'autorisation. Conséquence directe : `GET /api/public/rides/{un_id_de_trajet}`
renvoie 404, y compris pour un trajet effectivement partagé — c'est testé
(§6).

**Invariant 3 — Token non devinable.**

```python
token = secrets.token_urlsafe(16)   # 128 bits, 22 caractères URL-safe
```

- **128 bits d'entropie**, tirés du CSPRNG de l'OS — même primitive que
  `db.create_session` (`db.py:559`, qui utilise 32 octets pour un cookie de
  session à durée illimitée). 16 octets suffisent très largement ici et donnent
  une URL courte, présentable dans WhatsApp :
  `https://…/t/kJ3nQ7xR2mVpL8sT4wYzAg`.
- **Surtout pas** : l'id du trajet (c'est l'id Liberty Rider, traçable vers le
  compte), un compteur, un hash de l'id, un UUIDv1 (horodaté), ni
  `random.random()`.
- **Stockage** : en clair dans `ride_shares.token`, avec un index unique. Ce
  n'est pas un secret partagé réutilisable comme un mot de passe ; le hasher
  empêcherait juste le lookup direct sans bénéfice réel, et l'app est déjà
  dépositaire de données autrement plus sensibles (les `refresh_token`
  Firebase, en clair dans `users`).

**Invariant 4 — Révocation et régénération.** *(spec explicite du propriétaire)*

Le modèle doit permettre : couper un lien déjà envoyé, **puis** en émettre un
nouveau avec un token **différent**, l'ancien restant définitivement mort.

- **Révoquer** = `UPDATE ride_shares SET revoked_at = ? WHERE ride_id = ? AND
  user_id = ? AND revoked_at IS NULL`. On ne **supprime jamais** la ligne :
  garder les tokens révoqués conserve l'historique et garantit qu'un token
  régénéré ne peut pas ressusciter un ancien lien (la `PRIMARY KEY` sur `token`
  couvre toutes les lignes, actives et révoquées).
- **Régénérer** = révoquer l'actif **puis** insérer une nouvelle ligne, dans la
  même transaction. Nouveau token, nouvelle URL.
- **Au plus un lien actif par trajet**, garanti en base par un index unique
  partiel :
  `CREATE UNIQUE INDEX … ON ride_shares(ride_id) WHERE revoked_at IS NULL`
  (supporté par SQLite comme par Postgres). L'UI n'a donc jamais à gérer une
  liste de liens.
- **Un token révoqué → `404`**, avec exactement le même corps de réponse qu'un
  token inconnu. **Pourquoi pas `410 Gone`** : un `410` confirmerait au visiteur
  que ce token a existé, ce qui est un oracle gratuit. Le `404` ne distingue pas
  « n'a jamais existé » de « a été coupé ». Côté humain, la *page* `/t/{token}`
  répond quand même en HTTP 200 avec un état neutre et en français — « Ce lien
  n'est plus actif, ou n'a jamais existé. » — pour que le destinataire d'un vieux
  message WhatsApp ne tombe pas sur une erreur brute. La distinction est donc
  purement cosmétique et ne révèle rien.
- **`expires_at TEXT NULL`** est présent dans la table dès le départ et vérifié
  dans la requête de lookup, mais **jamais renseigné en v1**. Ça ne coûte
  qu'un mot dans un `CREATE TABLE` aujourd'hui, contre un fichier de migration
  Postgres complet plus tard.

**Invariant 5 — Liste blanche, jamais liste noire.**

La réponse publique est construite par une fonction **dédiée**, qui énumère les
champs autorisés — et non par un filtrage de `_merged_ride_dict`. Raison : le
jour où quelqu'un ajoute un champ à `ride_row_to_dict` (`app.py:117`), une liste
noire le laisse fuiter en silence ; une liste blanche l'ignore, et le test
d'égalité stricte des clés (§6) échoue bruyamment si l'oubli est de l'autre côté.

Voici l'audit champ par champ de ce que renvoie aujourd'hui
`GET /api/rides/{ride_id}` (`app.py:867`) :

| Champ actuel | Public ? | Motif |
| --- | --- | --- |
| `name`, `start_time`, `distance`, `duration`, `duration_without_pauses`, `total_pauses_duration`, `pause_count`, `maximum_altitude` | ✅ | Le contenu même du partage |
| `polyline` (chaînes encodées), `pauses`, `timeline` | ✅ | La trace et sa chronologie — voir la troncature ci-dessous |
| `vehicle_brand`, `vehicle_model` | ❌ | Point laissé au propriétaire (§8), **tranché : retirés**. Une moto se reconnaît, et laquelle on roule ne fait pas partie de « voilà où je suis allé ». |
| `id` | ❌ | C'est l'id Liberty Rider, traçable vers le compte |
| `merge_ride_ids`, `merged_into`, `created_roadbook_id`, `roadtrip_id` | ❌ | Ids internes et Liberty Rider ; `roadtrip_id` révèle en plus l'organisation du compte |
| `notes` | ❌ | Note privée, souvent personnelle |
| `tags` | ❌ | Taxonomie privée de l'utilisateur |
| `hidden`, `is_favorite`, `state` | ❌ | État interne, sans intérêt public |
| `preview_picture_url` | ❌ | URL du CDN Liberty Rider : hotlink depuis une page publique = requête sortante vers un tiers **et** fuite de l'id LR dans l'URL |
| `start_lat`/`start_lon`, `stop_lat`/`stop_lon` | ❌ | **Ce sont littéralement les coordonnées du domicile.** Redondant avec la polyline, et incompatible avec la troncature |
| e-mail, prénom, id Liberty Rider du propriétaire | ❌ | N'apparaissent nulle part. Le partage est **anonyme** en v1 : aucune attribution. |

À cela s'ajoutent deux champs propres au partage : `shared_at` et
`track_truncated` (booléen, pour afficher la mention adéquate sur la page).

### Troncature départ / arrivée — **recommandée, activée par défaut**

C'est la protection la plus efficace du lot, pour ~25 lignes de serveur. On
supprime les points de la polyline situés dans un rayon de **250 m** du premier
et du dernier point, et les pauses tombant dans ces zones. `haversine_km` existe
déjà dans `utils.py`.

- Se fait **côté serveur**, dans la construction de la réponse publique : les
  points retirés ne doivent jamais atteindre le navigateur du visiteur. Une
  troncature en JS serait de la décoration.
- Les statistiques (distance, durée, dénivelé max) restent celles enregistrées
  sur le trajet, **non recalculées** depuis la trace tronquée — sinon la page
  publique afficherait une distance qui ne correspond pas à ce que le
  propriétaire voit chez lui.
- La page affiche une mention discrète : « Départ et arrivée approximatifs. »
- Pas de réglage en v1. Une constante `SHARE_TRUNCATION_M = 250` en tête de
  module suffit.

### Le reste

- **`noindex`** — trois niveaux : `<meta name="robots" content="noindex,
  nofollow">` dans `share.html`, en-tête `X-Robots-Tag: noindex` sur la réponse
  de `/t/{token}` **et** sur le JSON public (l'en-tête couvre les crawlers qui
  ne parsent pas le HTML), et une route `GET /robots.txt` avec `Disallow: /t/`.
  Le `Disallow` ne divulgue aucun token, juste le préfixe.
- **`Referrer-Policy: no-referrer`** sur la page publique : sinon le token part
  dans l'en-tête `Referer` de chaque requête vers les tuiles OpenStreetMap.
  **C'est une vraie fuite** — un clic sur un lien externe depuis la page, ou un
  simple chargement de tuile, transmettrait l'URL complète à un tiers. Ligne
  unique, à ne pas oublier.
- **Rate limiting** — non nécessaire pour le brute-force de token : 128 bits
  rendent la recherche infaisable, et le projet n'a aucun middleware de ce type
  aujourd'hui. En ajouter un serait de la sur-ingénierie. Point de vigilance
  réel en revanche : la réponse 404 doit être **identique** (statut, corps,
  en-têtes) pour un token inconnu et un token révoqué, et rien de sensible ne
  doit finir dans les logs applicatifs.
- **Cohérence multi-tenant** (`docs/ARCHITECTURE.md`, § *Multi-tenancy and
  auth*) — `ride_shares` porte un `user_id` en plus du `ride_id`, comme toutes
  les tables du modèle. Toutes les mutations (créer / révoquer / régénérer)
  passent par `_get_owned_ride(conn, ride_id, user["id"])` (`app.py:860`), qui
  renvoie déjà 404 sur le trajet d'un autre compte : partager le trajet d'un
  tiers est donc impossible **sans une ligne de code supplémentaire**. La table
  reçoit `ENABLE ROW LEVEL SECURITY` sans policy, comme les douze autres
  (`migrations/0001_initial_schema.sql:122-133`).

### Trois interactions avec l'existant à ne pas rater

1. **`purge_user_data`** (`db.py:510`) supprime les rides mais ignorerait
   `ride_shares` → violation de FK et lignes orphelines. Ajouter le `DELETE` **avant**
   celui sur `rides`. Testé.
2. **Fusion de trajets** (`api_merge_rides`, `app.py:1005`) — si un trajet
   partagé est ensuite absorbé dans une fusion (`merged_into` renseigné), son
   lien public pointerait sur une ligne qui n'est plus le représentant du groupe.
   Comportement retenu, le plus simple et le plus sûr : **révoquer les partages
   des trajets absorbés** au moment de la fusion. L'utilisateur repartage le
   trajet fusionné s'il le souhaite.
3. **Re-sync** (`db.upsert_ride`) — aucun impact : les partages vivent dans une
   table séparée, indexée par un `ride_id` que l'upsert préserve par
   construction. Rien à faire, mais c'est à vérifier une fois.

---

## 4. Le schéma et les endpoints

### Table `ride_shares`

```sql
-- Un lien public de partage. Une ligne par token EMIS, jamais supprimée :
-- révoquer pose `revoked_at`, régénérer révoque puis insère une nouvelle
-- ligne. Garder les lignes révoquées conserve l'historique et garantit
-- qu'un token régénéré ne peut jamais réutiliser un token déjà émis.
CREATE TABLE IF NOT EXISTS ride_shares (
  token TEXT PRIMARY KEY,
  ride_id TEXT NOT NULL REFERENCES rides(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  revoked_at TEXT,            -- NULL = lien actif
  expires_at TEXT             -- toujours NULL en v1 ; la requête le vérifie déjà
);

-- Au plus un lien ACTIF par trajet (les révoqués s'accumulent librement).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ride_shares_active
  ON ride_shares(ride_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ride_shares_user ON ride_shares(user_id);
```

Les deux backends, comme le veut `db.py` :

- **SQLite** — ajouter le bloc à la constante `SCHEMA` (`db.py:59`). Le
  `CREATE TABLE IF NOT EXISTS` est rejoué à chaque `init_db()` : les bases
  existantes récupèrent la table sans migration `ALTER`.
- **Postgres** — **nouveau fichier** `migrations/0002_ride_shares.sql` (ne
  jamais modifier `0001`, déjà appliqué), avec `token TEXT PRIMARY KEY`,
  `SERIAL` hors sujet ici, et `ALTER TABLE ride_shares ENABLE ROW LEVEL
  SECURITY;` en fin de fichier.

Helpers dans `db.py`, dans le style du module (SQL en `?`, `conn.commit()`
explicite) : `create_ride_share`, `get_active_share_for_ride`,
`get_share_by_token`, `revoke_ride_share`, `regenerate_ride_share`.

### Endpoints

| Route | Auth | Rôle |
| --- | --- | --- |
| `POST /api/rides/{ride_id}/share` | ✅ session | Corps `{"regenerate": false}` (pydantic `ShareRequest`, comme le reste du repo). `false` (défaut) = get-or-create idempotent. `true` = révoque l'actif et émet un nouveau token. Renvoie `{token, url, created_at}`. 404 si le trajet n'appartient pas au compte. |
| `DELETE /api/rides/{ride_id}/share` | ✅ session | Révoque le lien actif. Idempotent (204 même s'il n'y en avait pas). |
| `GET /api/public/rides/{token}` | ❌ **aucune** | La charge utile publique en liste blanche. 404 si token inconnu, révoqué ou expiré. |
| `GET /t/{token}` | ❌ **aucune** | Sert `share.html` + injecte les balises OG + `X-Robots-Tag`. |
| `GET /robots.txt` | ❌ | `Disallow: /t/` |

Pas d'endpoint `GET /api/rides/{id}/share` : `GET /api/rides/{ride_id}` gagne un
champ `"share": {"token", "url", "created_at"} | null`, ce qui évite un
aller-retour supplémentaire à l'ouverture du modal.

### Front

- **`static/shared.js`** (nouveau, ~90 lignes) — `decodePolyline`,
  `decodePolylines`, les formateurs, `escapeHtml`, **déplacés** depuis `app.js`.
  Chargé par `index.html` **avant** `app.js` et par `share.html`. Pas de module
  ES : de simples globals via deux balises `<script>`, exactement l'idiome
  existant (c'est déjà comme ça que `L` de Leaflet est disponible).
  *Alternative écartée : dupliquer les 90 lignes dans `share.js`. Zéro risque
  pour la SPA, mais deux décodeurs de polyline qui divergent à terme.*
- **`static/share.html`** (~180 lignes) — page autonome : les variables CSS
  `:root` copiées d'`index.html`, un en-tête (nom du trajet + date), une bande
  de statistiques, la carte plein écran, un pied de page « Créé avec Carnet de
  Route » cliquable vers `/`. Responsive : le lien s'ouvre sur mobile, dans
  WhatsApp.
- **`static/share.js`** (~120 lignes) — lit le token dans `location.pathname`,
  `fetch` du JSON public, rend la carte à partir des briques de `shared.js`,
  gère les états « chargement », « lien inactif » et « erreur ».

### UX d'obtention du lien

Dans `#rideModalHead`, à côté de « 📄 Télécharger en GPX », un bouton
**« 🔗 Lien public »**. Il déplie un panneau **dans le modal** (pas un second
modal en surépaisseur — le repo n'en empile jamais deux) :

*État non partagé*
```
Ce trajet est privé.
[ Créer un lien public ]
Toute personne ayant le lien pourra voir la trace, sans compte.
```

*État partagé*
```
🔗 https://carnet-de-route…/t/kJ3nQ7xR2mVpL8sT4wYzAg   [ Copier ]
Partagé le 12 juillet 2025 · départ et arrivée approximatifs
[ Désactiver le lien ]   [ Régénérer ]
```

- « Copier » → `navigator.clipboard.writeText`, le bouton passe à « Copié ✓ »
  une seconde. Repli sur la sélection du champ si l'API n'est pas disponible
  (contexte non sécurisé).
- « Désactiver » → `confirm("Désactiver ce lien ? Les personnes à qui tu l'as
  envoyé ne pourront plus voir le trajet.")`.
- « Régénérer » → `confirm("Créer un nouveau lien ? L'ancien cessera
  immédiatement de fonctionner, y compris pour les personnes à qui tu l'as déjà
  envoyé.")` — la formulation dit explicitement que les liens WhatsApp déjà
  partis meurent, c'est le point qui doit être compris avant le clic.
- *Bonus* : une pastille « 🔗 » sur la ligne du trajet dans la liste, pour
  repérer d'un coup d'œil ce qui est partagé. Demande un champ `shared` dans
  `GET /api/rides`, en une requête groupée sur le modèle de `_attach_tags_bulk`
  (`app.py:510`) — surtout pas une requête par trajet.

Tout en français, ton et style de l'existant (phrases courtes, tutoiement,
emoji parcimonieux).

---

## 5. Découpage en étapes

Une **seule branche `feat/public-share`**, en commits ordonnés. C'est un écart
assumé à la règle « une branche par phase » du CLAUDE.md global : aucune de ces
étapes ne fonctionne ni ne se relit isolément (un endpoint public sans page à
servir, une page sans endpoint), et elles se mergent forcément ensemble.

| # | Contenu | Fichiers | Effort |
| --- | --- | --- | --- |
| 1 | Schéma + helpers `db.py` + `DELETE` dans `purge_user_data` | `db.py`, `migrations/0002_ride_shares.sql` | ~90 l. · **1 h** |
| 2 | Endpoints authentifiés (`POST`/`DELETE .../share`, champ `share` dans le détail) + révocation à la fusion | `app.py` | ~70 l. · **1 h** |
| 3 | Endpoint public en liste blanche + troncature + `/t/{token}` + `noindex` + `Referrer-Policy` + `robots.txt` | `app.py` | ~90 l. · **1 h 15** |
| 4 | Extraction de `shared.js`, page `share.html` + `share.js` | `static/` | ~300 l. dont ~90 déplacées · **2 h** |
| 5 | UI du modal : bouton, panneau, copie, désactivation, régénération | `static/app.js`, `static/index.html` | ~150 l. · **1 h 30** |
| 6 | Balises Open Graph, pastille « partagé » dans la liste, docs (`API.md` + section dans `ARCHITECTURE.md`) | divers | ~120 l. · **1 h 30** |

**MVP livrable (1→5) : ~6 h. Complet (1→6) : ~8-9 h**, soit une bonne journée en
comptant relecture et tests. Les tests sont écrits **dans** chaque étape, pas à
la fin.

---

## 6. Tests — `tests/test_public_share.py`

Style du repo : fonctions `test_*` plates, fixtures `client` / `make_client` /
`login_as` de `conftest.py`, seed direct en base via `db_module.upsert_ride` +
`make_ride` (cf. `tests/test_app_multitenant.py`). Un `TestClient` frais et
**sans cookie** matérialise le visiteur non authentifié.

**Le socle exigé par le brief**

```
test_revoked_token_is_404
test_unknown_token_is_404
test_revoked_and_unknown_tokens_are_indistinguishable   # même statut ET même corps
test_public_payload_never_leaks_another_account          # seed 2 comptes, 1 partage
```

**Révocation / régénération** *(spec ajoutée par le propriétaire)*

```
test_regenerate_issues_a_different_token
test_regenerated_link_works                              # le nouveau token rend 200
test_old_token_is_dead_after_regenerate                  # l'ancien rend 404
test_regenerate_leaves_exactly_one_active_share          # l'index unique tient
test_revoke_then_create_also_issues_a_new_token          # l'autre ordre d'opérations
test_revoke_is_idempotent
```

**L'invariant « jamais par id »**

```
test_ride_id_is_not_a_valid_token        # GET /api/public/rides/{ride_id} → 404
                                          # même quand ce trajet EST partagé
```

**Surface exposée**

```
test_public_payload_exposes_exactly_the_allowlisted_keys   # égalité STRICTE de set()
test_public_payload_has_no_notes_no_tags_no_ids
test_public_payload_has_no_start_or_stop_coordinates
test_public_payload_has_no_owner_email_or_user_id          # scan de la réponse brute
test_track_is_truncated_near_start_and_end
```

Le test d'égalité stricte des clés est le garde-fou structurel : ajouter un champ
au dictionnaire de trajet sans décider explicitement de son statut public fait
échouer la suite.

**Propriété et cycle de vie**

```
test_cannot_share_another_users_ride                     # 404
test_cannot_revoke_another_users_share                   # 404, et le lien reste actif
test_create_share_is_idempotent                          # deux POST → même token
test_ride_detail_exposes_the_active_share_to_its_owner
test_merging_a_shared_ride_revokes_its_share
test_purge_account_data_removes_shares
```

**Page et en-têtes**

```
test_share_page_is_served_without_a_session
test_share_page_is_noindex                               # meta ET en-tête X-Robots-Tag
test_share_page_sets_no_referrer_policy
test_robots_txt_disallows_the_share_prefix
```

Environ **25 tests, ~250 lignes**. La suite existante (`make -q test` /
`pytest`) doit rester verte : les seules modifications de comportement existant
sont l'ajout du champ `share` au détail d'un trajet et la révocation à la fusion.

---

## 7. Phase 2 — comment voir le résultat tourner avant tout merge

À exécuter **après le GO**, pas maintenant.

### Option A — worktree git local *(recommandée)*

```bash
git worktree add ../lr-share -b feat/public-share
cp data/rides.db ../lr-share/data/            # non versionné : sinon base vide
../.venv/bin/uvicorn app:app --reload --port 8010   # le venv du repo principal se réutilise tel quel
```

- **Mise en place : ~10 min.** Coût nul.
- Isole proprement le working tree — ce qui compte ici, puisque `secaudit` et
  `teamlead` travaillent dans le même dossier.
- **Le point qui décide** : la page publique n'a d'intérêt visuel qu'avec une
  vraie trace. La base `data/rides.db` est non versionnée ; la copier dans le
  worktree prend une seconde et reste **entièrement sur la machine du
  propriétaire**.
- Limite : pas d'URL partageable, donc pas de test réel de l'aperçu WhatsApp.

### Option B — sandbox exe.dev

- **Mise en place : ~30-45 min** (VM, dépendances Python, uvicorn, exposition).
- Donne une URL publique — séduisant puisque la fonctionnalité *est* une URL
  publique.
- **Mais** : pour que la démo montre quelque chose, il faut copier des traces GPS
  réelles sur une VM tierce, et les rendre accessibles depuis une URL publique.
  C'est très exactement le risque que la fonctionnalité cherche à encadrer. Le
  faire pour une simple revue visuelle est disproportionné.

### Recommandation

> **Worktree local pour la totalité de l'implémentation et de la revue.**
>
> Puis, **si et seulement si** le propriétaire veut valider l'aperçu WhatsApp
> pour de vrai (étape 6, balises Open Graph — c'est la seule chose qu'un
> `127.0.0.1` ne peut pas montrer), un passage exe.dev **court et cadré** :
> base de données jetable contenant **une** trace choisie exprès (ou fabriquée),
> jamais la copie de l'historique réel, et VM détruite juste après.

Coût réel : ~10 min contre ~40 min, pour une seule chose en plus, qui n'est
utile qu'à la toute fin.

---

## 8. Ce qui reste à trancher par le propriétaire

1. ~~**`vehicle_brand` / `vehicle_model` sur la page publique**~~ — **tranché
   par le propriétaire : retirés.** La référence de la moto n'a pas sa place
   sur la page publique.
2. **Troncature à 250 m activée par défaut** — recommandation : oui. La valeur
   se discute (100 m ? 500 m ?).
3. **Attribution** — v1 anonyme, aucun prénom sur la page. Afficher « Trajet
   d'Alex » supposerait de lire `users.first_name` depuis un chemin public :
   possible, mais c'est un choix, pas un défaut.
4. **Étape 6 (Open Graph, pastille, docs)** dans le premier lot ou en suivi ?

---

## 9. Écarts entre ce plan et l'implémentation

Le plan a été suivi tel quel, à quatre détails près :

1. **Troncature** — le plan disait « supprimer les points dans un rayon de 250 m
   du premier et du dernier point ». L'implémentation retire une **plage
   contiguë** à chaque bout plutôt que tout point dans le rayon : filtrer
   perce un trou au milieu d'une trace en boucle et trace une droite au
   travers, ce qui se lit comme un bug et **désigne** le trou. Limite assumée et
   écrite dans le code : une boucle qui repasse près de son départ en cours de
   route montre ce passage.
2. **Régénération** — exposée comme `POST …/share {"regenerate": true}` plutôt
   que comme une route dédiée, pour rester sur le modèle pydantic du repo.
3. **Marque/modèle de moto** — d'abord conservés, puis **retirés** sur
   décision du propriétaire (§8, point 1).
4. **Étape 6** — faite dans le même lot : balises Open Graph, pastille 🔗 dans
   la liste, docs. Le plan la donnait comme optionnelle.

5. **Contenu de la page publique** — le plan mettait le profil altimétrique
   « hors périmètre v1 ». Le propriétaire a demandé l'inverse : la page
   publique doit reprendre la présentation du modal (chronologie + profil
   d'altitude + les deux blocs de statistiques). Fait, avec un endpoint
   public dédié calculé sur la trace **tronquée** ; seuls les cols restent
   dehors (Overpass + écriture en base depuis une URL non authentifiée).
   Les notes et les tags restent privés, comme prévu.
6. **Emplacement du bouton de partage** — d'abord dans l'en-tête du modal,
   déplacé sur demande du propriétaire dans un menu « ⋯ » qui contient aussi
   le GPX : rien n'est visible tant qu'on ne l'ouvre pas.

Ce que le plan avait sous-estimé : rien de bloquant. Le modal n'a pas eu à être
refactorisé, comme prévu — seules les fonctions déjà pures ont bougé dans
`static/shared.js`.
