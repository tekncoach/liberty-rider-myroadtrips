# Plan — Lien public de partage d'un roadtrip

> **Statut : PLAN, non implémenté.** Aucune ligne de code n'a été écrite.
> Rien ne démarre sans un GO explicite du propriétaire.
>
> Suite directe de `docs/PLAN-public-share.md`, dont la mise en œuvre est
> mergée et déployée (`9d776bc`). Ce plan part de `origin/main` à jour
> (`fe3621c`) et ne réexplique pas ce qui y est déjà décidé — modèle de
> jeton, discipline du 404, en-têtes, liste blanche.

**Besoin** — partager un **roadtrip entier** via une URL publique, comme on
partage déjà une trace : la carte des étapes, les agrégats, le jour par jour,
consultable sans compte.

---

## 1. Le modèle de données

### Ce qui se réutilise tel quel, sans y toucher

- **La génération du jeton** — `secrets.token_urlsafe(16)`, 128 bits
  (`db.SHARE_TOKEN_BYTES`). Rien à changer : un roadtrip n'est pas plus
  sensible qu'une trace, et l'espace de jetons doit rester le même.
- **Le cycle de vie** — une ligne par jeton **jamais émis deux fois, jamais
  supprimée** ; révoquer pose `revoked_at` ; régénérer révoque puis insère ;
  index unique partiel `WHERE revoked_at IS NULL` pour « au plus un lien
  actif ». Ces règles sont indépendantes de ce qui est partagé.
- **La discipline du chemin public** — résolution **par jeton uniquement**,
  404 identique pour inconnu / révoqué / expiré, liste blanche construite
  champ par champ, en-têtes `Referrer-Policy: no-referrer` +
  `X-Robots-Tag`, `Disallow: /t/`.
- **La troncature** — `_truncate_track` et `_truncation_bounds` (`app.py`)
  s'appliquent à une trace ; un roadtrip est un paquet de traces, donc elles
  se réutilisent telles quelles, une fois par étape (voir §3).
- **Le socle front** — `static/shared.js`, `static/shared.css`, le squelette
  de `share.html`, l'injection Open Graph par `_render_share_page`.

### Ce qui doit changer : trois options

Le point dur est que `ride_shares.ride_id` est un `TEXT NOT NULL REFERENCES
rides(id)`, alors qu'un roadtrip a un `id` **entier** (`roadtrips.id SERIAL`).

| Option | Coût | Verdict |
| --- | --- | --- |
| **A. Colonne polymorphe** — `target_kind TEXT` + `target_id TEXT` sur `ride_shares` | Réécriture de la table | ❌ **Rejeté.** Une colonne `TEXT` unique ne peut pas porter de clé étrangère vers deux tables : on troque une contrainte vérifiée par la base contre une convention tenue à la main. Et il faudrait convertir les `roadtrips.id` entiers en texte, donc perdre le typage des deux côtés. |
| **B. Table dédiée `trip_shares`** en miroir de `ride_shares` | +1 table, +7 helpers | 🟡 **Recevable, c'est l'option prudente.** Aucune migration de données vivantes, chaque table garde sa vraie FK. Mais le chemin public doit interroger **deux** tables pour résoudre un jeton — deux endroits où l'invariant « par jeton seulement » peut se fissurer — et un troisième type (les tags, dont l'endpoint a exactement la même forme, cf. `_tag_summary`) imposerait une troisième table. |
| **C. Table unique `shares`, deux colonnes typées nullables** — `ride_id TEXT NULL REFERENCES rides(id)`, `roadtrip_id INTEGER NULL REFERENCES roadtrips(id)`, `CHECK` qu'exactement une des deux est renseignée | Renommage + migration de la table vivante | ✅ **Recommandé.** |

### Pourquoi C

- **Un seul espace de jetons, une seule résolution.** `get_share_by_token`
  reste l'unique porte d'entrée du chemin public. C'est l'invariant qui tient
  toute la fonctionnalité ; le dupliquer sur deux tables, c'est doubler la
  surface où une erreur future peut fuiter.
- **Les clés étrangères survivent**, contrairement à A : la base continue de
  refuser un partage qui pointe vers un trajet ou un roadtrip inexistant.
- **Le `CHECK` porte la règle** plutôt qu'un commentaire :
  `CHECK ((ride_id IS NOT NULL) <> (roadtrip_id IS NOT NULL))`.
- **Un troisième type coûte une colonne**, pas une table ni un chemin de
  lecture supplémentaire.
- **`purge_user_data` reste un seul `DELETE`** au lieu de deux — un endroit
  de moins à oublier.

**Le prix, et il est réel** : la table est en production avec de vrais liens
déjà envoyés. La migration doit garantir que **tout jeton déjà distribué
continue de fonctionner**. Sur Postgres — le backend déployé — c'est direct :

```sql
ALTER TABLE ride_shares RENAME TO shares;
ALTER TABLE shares ADD COLUMN roadtrip_id INTEGER REFERENCES roadtrips(id);
ALTER TABLE shares ALTER COLUMN ride_id DROP NOT NULL;
ALTER TABLE shares ADD CONSTRAINT shares_one_target
  CHECK ((ride_id IS NOT NULL) <> (roadtrip_id IS NOT NULL));
-- l'index unique partiel se dédouble, un par type de cible
DROP INDEX IF EXISTS idx_ride_shares_active;
CREATE UNIQUE INDEX idx_shares_active_ride ON shares(ride_id) WHERE revoked_at IS NULL AND ride_id IS NOT NULL;
CREATE UNIQUE INDEX idx_shares_active_trip ON shares(roadtrip_id) WHERE revoked_at IS NULL AND roadtrip_id IS NOT NULL;
```

Les lignes sont conservées, `token` reste la clé primaire : **aucune URL déjà
partagée ne change**. Côté SQLite (dev/tests), `ALTER COLUMN ... DROP NOT
NULL` n'existe pas → reconstruction de table, exactement ce que `db.py` fait
déjà dans `_migrate_tags_table` et `_migrate_users_table`, en préservant les
`token`. Ce chemin-là mérite son propre test.

**Si le propriétaire juge le renommage trop risqué sur une table vivante**,
l'option B est un repli honnête : même fonctionnalité, code un peu plus
bavard, zéro migration. C'est le seul arbitrage à trancher avant l'étape 1.

---

## 2. Ce que la page publique d'un roadtrip montre

Le principe reste celui de la trace : **la page publique montre ce que le
propriétaire voit dans sa propre vue**, moins ce qui est privé. Aujourd'hui
`showTripDetail` (`static/app.js:938`) affiche : nom, jours / étapes /
distance / durée / pauses, l'histogramme des km par jour, la carte multi-jours
(une couleur par jour), la liste des étapes jour par jour.

### Montre

- **Nom du roadtrip**, `start_date` → `end_date`, et les agrégats déjà
  calculés par `_roadtrip_summary` : `day_count`, `ride_count`,
  `total_distance`, `total_duration`, `total_pause_count`.
- **La carte** — une polyligne par étape, **une couleur par jour**, comme la
  vue privée (`renderMap`). Les pauses restent derrière la même case à cocher
  « Afficher les pauses », décochée par défaut.
- **L'histogramme des km par jour** (`renderKmChart`, avec le comblement des
  jours vides).
- **Le jour par jour** — par journée : date, distance, durée totale, temps à
  moto, nombre de pauses, nombre d'étapes ; et par étape : nom, heure de
  départ → heure d'arrivée, distance, durée.

### Ne montre pas

Mêmes règles que pour une trace, plus ce que la vue roadtrip ajoute :

| Écarté | Motif |
| --- | --- |
| **`notes` par étape** | La vue privée les affiche **en ligne dans la liste des jours** (`day-ride-note`, éditable). C'est le piège n°1 de cette page : la note est déjà dans le payload de `/api/roadtrips/{id}`. |
| `tags` | Taxonomie privée |
| `id` du roadtrip, `id` des trajets, `merge_ride_ids`, `merged_into`, `created_roadbook_id` | Identifiants internes et Liberty Rider |
| `start_lat`/`start_lon`/`stop_lat`/`stop_lon` de chaque étape | Le domicile et les hôtels, en clair |
| `preview_picture_url` | URL du CDN Liberty Rider |
| `vehicle_brand` / `vehicle_model` | Décision déjà prise sur la trace (retirés) |
| `hidden`, `is_favorite`, `state`, `roadtrip_id` | État interne |
| e-mail, prénom, id Liberty Rider | La page est **anonyme**, comme celle d'une trace |
| Les commandes : « Supprimer », « Export GPX », 🗑 « Retirer du roadtrip », le nom éditable | Ce sont des actions de propriétaire |
| 🔍 « Voir le détail » par étape | v1 : un lien de roadtrip donne le roadtrip, pas une page par étape — voir §4 |

**Le nom du roadtrip est écrit par l'utilisateur** : il part dans le HTML et
dans les balises Open Graph. Il doit passer par `html.escape` côté serveur
(`_render_share_page` le fait déjà) et `escapeHtml` côté client.

**Pas de profil d'altitude ni de cols** sur cette page en v1 : la vue privée
du roadtrip n'en a pas non plus (ils vivent dans le modal d'une trace). La
parité privée/publique est le critère, et elle est respectée.

### La liste blanche

Comme pour la trace : une fonction dédiée `_public_roadtrip_dict`, qui
**énumère** ses champs au lieu de filtrer `_roadtrip_summary` — un champ
ajouté plus tard à la vue privée reste invisible par défaut. Le test d'égalité
stricte du jeu de clés s'applique **à deux niveaux** ici : l'objet roadtrip,
et chaque étape dans `days[].rides[]`.

Forme proposée :

```
{ name, start_date, end_date, day_count, ride_count, total_distance,
  total_duration, total_pause_count, track_truncated,
  days: [{ date, total_distance, total_duration,
           total_duration_without_pauses, total_pause_count,
           stages: [{ name, start_time, duration, distance,
                      polyline, pauses }] }] }
```

Les étapes sont **imbriquées dans les jours** plutôt que servies à plat avec
des `ride_ids` : la vue privée a besoin des ids pour ses boutons, la page
publique non — et ne pas les émettre du tout vaut mieux que les émettre puis
se rappeler de ne pas s'en servir.

---

## 3. Troncature du domicile sur un multi-jours

La question posée : les 250 m aux deux bouts de **chaque** trace, ou seulement
du tout premier départ et de la toute dernière arrivée ?

**Recommandation : chaque étape, aux deux bouts, même règle, même constante.**

Les arguments, dans l'ordre où ils pèsent :

1. **Sinon la page publique du roadtrip contredit celle de la trace.** Une
   étape peut être partagée individuellement en parallèle (§4). Sa page
   tronque ses deux bouts. Si la page du roadtrip ne le fait pas, les deux
   URL montrent la même trace différemment — et c'est la version roadtrip qui
   fuite. Une règle qui dépend du contexte d'affichage n'est pas une règle.
2. **Les étapes intermédiaires révèlent les hôtels.** Le domicile n'est pas la
   seule adresse en jeu : la fin de l'étape du jour 3, c'est l'endroit où le
   rider a dormi — souvent une adresse privée (chambre d'hôte, ami), et une
   information sur quelqu'un d'autre que lui.
3. **Le coût visuel est nul.** Un roadtrip s'affiche à l'échelle de la région :
   3 188 km sur une carte de 1 000 px, c'est ~3 km par pixel. 250 m est
   largement sous le pixel. Les 44 bouts coupés d'un roadtrip de 22 étapes
   totalisent au pire 11 km sur 3 188, soit **0,3 %** — invisible tant qu'on
   ne zoome pas jusqu'à la rue, ce qui est précisément le seul cas où ça
   compte.
4. **La règle simple est celle qu'on peut tester.** « Le premier et le
   dernier » suppose de savoir qui est premier — et ça change dès qu'on retire
   une étape du roadtrip (§4). Un lien déjà envoyé se mettrait alors à
   dévoiler un bout qu'il masquait la veille.

Conséquences à assumer, comme pour la trace : les statistiques restent celles
enregistrées (non recalculées sur la trace coupée), et une étape plus courte
que deux rayons de troncature ne publie **aucun** tracé — sur un roadtrip elle
disparaît donc de la carte tout en restant dans les chiffres du jour. Le
`track_truncated` global de la page porte la mention « Départs et arrivées
approximatifs », au pluriel.

---

## 4. Cohérence avec le reste

**Une étape déjà partagée individuellement.** Les deux liens sont
indépendants : deux jetons, deux pages, deux révocations. Révoquer le lien du
roadtrip ne touche pas ceux des étapes, et l'inverse non plus. C'est la
lecture la moins surprenante : le propriétaire a fait deux gestes distincts.
La page publique du roadtrip **ne renvoie pas** vers la page d'une étape même
partagée — il faudrait publier le jeton de l'étape dans le payload du
roadtrip, donc élargir la surface pour un confort mineur. Écarté en v1.

**Retirer une étape d'un roadtrip partagé** (`DELETE /api/rides/{id}/roadtrip`).
Rien à révoquer : la page est calculée à la lecture, l'étape disparaît, les
agrégats se recalculent. Un lien déjà envoyé montre donc moins qu'avant — ce
qui va dans le bon sens, et découle d'une action volontaire du propriétaire.
À écrire dans un test plutôt que dans une note : c'est le genre de
comportement qu'on croit évident jusqu'à ce qu'il change.

**Supprimer le roadtrip** (`api_delete_roadtrip`). Le lien doit mourir avec
lui, et la clé étrangère l'impose : on ne peut pas laisser une ligne `shares`
pointer vers un `roadtrips.id` supprimé. Deux façons de faire :

- **Supprimer les lignes de partage** du roadtrip. C'est déjà le précédent
  établi par `purge_user_data`, qui supprime les lignes plutôt que de les
  révoquer. Retenu.
- Révoquer puis mettre `roadtrip_id` à NULL violerait le `CHECK`.

À noter honnêtement : supprimer une ligne fait reposer la garantie « un jeton
régénéré ne peut pas ressusciter un ancien lien » sur les 128 bits d'entropie
au lieu de la clé primaire. C'est déjà le cas depuis `purge_user_data`, et
c'est suffisant — mais c'est un affaiblissement, pas une équivalence, et ça
mérite d'être dit.

**Fusionner des étapes dans un roadtrip partagé.** `api_merge_rides` révoque
déjà les liens des trajets absorbés ; la page du roadtrip recalcule et affiche
l'étape fusionnée. Rien à faire, un test le fige.

**`purge_user_data`.** Oui, et c'est un argument de plus pour l'option C : un
`DELETE FROM shares WHERE user_id = ?` couvre les deux types. Avec l'option B
il faudrait penser à ajouter la seconde ligne — exactement le genre d'oubli
qui crée une fuite.

**`claim_orphaned_data`** — sans objet (inerte dès le second compte).

---

## 5. Poids de la page

Mesuré sur la base réelle du propriétaire, roadtrip le plus lourd
(« Trip #4 • Alpes du Sud ») :

| | |
| --- | --- |
| Étapes | 22 |
| Points GPS | **81 260** |
| Polylignes encodées | **276 Ko** |
| Équivalent JSON décodé | ~1,7 Mo (ce que l'encodage évite déjà) |

**C'est déjà ce que la vue privée envoie aujourd'hui.** La question n'est donc
pas « est-ce que ça marche » mais « est-ce qu'on envoie ça à quelqu'un qui
ouvre un lien WhatsApp sur son téléphone, en 4G ». Deux coûts distincts : les
276 Ko sur le réseau, et surtout le **décodage plus le rendu Leaflet de 81 000
points** sur un mobile.

Simplification Douglas-Peucker mesurée sur ce même roadtrip :

| Tolérance | Points | Encodé | Gain |
| --- | --- | --- | --- |
| — | 81 260 | 276 Ko | — |
| **10 m** | **15 700** | **59 Ko** | **×4,6** |
| 25 m | 8 992 | 36 Ko | ×7,7 |

**Recommandation : simplifier à 10 m, sur la page publique du roadtrip
uniquement.** À l'échelle d'un roadtrip (~3 km par pixel), 10 m est deux
ordres de grandeur sous le pixel : le tracé est visuellement identique, y
compris en zoomant sur une vallée. La page d'une trace seule garde le tracé
complet — c'est là qu'on zoome sur un lacet.

Points d'attention pour l'implémentation :

- **Le coût CPU du RDP** (~81 000 points en Python pur) est à mesurer à
  l'étape 3, pas à supposer. S'il dépasse quelques centaines de millisecondes,
  deux replis dans l'ordre : une décimation par distance — `_resample_indices`
  existe déjà dans `app.py` pour le profil d'altitude, mais elle coupe les
  virages, ce que le RDP préserve — ou la mise en cache du tracé simplifié,
  sur le modèle d'`elevation_cache`. Ne pas cacher avant d'avoir mesuré.
- **La récursion** : écrire le RDP en itératif, une trace de 20 000 points
  fait exploser la pile par défaut.
- **La compression** est complémentaire, pas alternative : l'app n'a
  aujourd'hui **aucun `GZipMiddleware`** (vérifié). Du texte de polyligne se
  comprime très bien ; l'ajouter profiterait à toutes les réponses. À
  proposer séparément, hors périmètre de ce plan — et ça ne réduit ni le
  décodage ni le rendu côté client, contrairement à la simplification.
- **Ordre des opérations** : tronquer d'abord, simplifier ensuite. L'inverse
  laisserait la simplification déplacer les points d'extrémité avant qu'on
  mesure les 250 m.

---

## 6. Découpage, effort, tests

Une seule branche `feat/public-share-roadtrip`, commits ordonnés — mêmes
raisons que la première fois : aucune étape ne fonctionne isolément.

| # | Contenu | Effort |
| --- | --- | --- |
| 1 | `ride_shares` → `shares` : migration Postgres + reconstruction SQLite, `CHECK`, index dédoublés, helpers `db.py` généralisés (`create_share(kind, id)`…) | ~120 l. · **2 h** |
| 2 | Endpoints propriétaire : `POST`/`DELETE /api/roadtrips/{id}/share`, champ `share` sur le détail, suppression des partages à la suppression du roadtrip, `purge_user_data` | ~70 l. · **1 h** |
| 3 | `GET /api/public/roadtrips/{token}` : liste blanche imbriquée, troncature par étape, simplification (+ mesure du coût) | ~120 l. · **2 h** |
| 4 | Aiguillage de `/t/{token}` selon le type de cible + Open Graph par type | ~50 l. · **1 h** |
| 5 | Page `share-trip.html` + `share-trip.js` : factoriser `renderMap` / `renderDayList` / `renderKmChart` en variantes lecture seule dans `shared.js` — **c'est le gros morceau**, ces trois-là sont couplées à `state` et aux contrôles d'édition | ~350 l. · **3 h** |
| 6 | UI propriétaire : menu « ⋯ » dans l'en-tête du roadtrip (aujourd'hui « Supprimer » + « Export GPX » à nu), panneau de partage réutilisé tel quel | ~80 l. · **1 h 30** |
| 7 | Docs (`API.md`, `ARCHITECTURE.md`) + revue | **1 h** |

**Total ≈ 11-12 h**, une bonne journée et demie. Plus lourd que le partage
d'une trace (~8 h) presque entièrement à cause de l'étape 5.

### Tests — `tests/test_public_share_roadtrip.py`

**La migration, d'abord** — c'est ce qui touche des données vivantes :

```
test_an_existing_ride_token_still_resolves_after_the_migration
test_the_sqlite_rebuild_preserves_every_token_and_its_revoked_state
test_a_share_row_cannot_target_both_a_ride_and_a_roadtrip   # le CHECK
test_a_share_row_cannot_target_neither
```

**Le socle, en miroir de la suite existante** : jeton révoqué → 404, jeton
inconnu → 404 indiscernable, régénération (ancien mort / nouveau vivant),
`id` de roadtrip refusé comme jeton, partage d'un roadtrip d'un autre compte
→ 404, en-têtes `noindex` et `no-referrer`.

**Surface exposée** :

```
test_the_public_roadtrip_payload_is_exactly_the_allow_list
test_no_stage_carries_a_note            # le piège de la liste des jours
test_no_stage_carries_tags_or_ids
test_no_start_or_stop_coordinates_anywhere_in_the_payload
test_the_payload_never_identifies_the_owner
```

**Troncature et poids** :

```
test_every_stage_is_trimmed_at_both_ends          # pas seulement la 1re et la dernière
test_a_pause_near_a_stage_end_is_dropped
test_a_stage_shorter_than_two_radii_publishes_no_track
test_the_published_track_is_simplified            # moins de points que le privé
test_simplification_keeps_the_shape               # écart max < tolérance
```

**Cohérence** :

```
test_sharing_a_roadtrip_does_not_share_its_stages_individually
test_revoking_the_roadtrip_link_leaves_a_stage_link_alive
test_removing_a_ride_removes_it_from_the_public_page
test_deleting_the_roadtrip_kills_its_link
test_purging_the_account_kills_both_kinds_of_link
test_merging_stages_inside_a_shared_roadtrip_keeps_the_link_working
```

Environ **35 tests**. Comme la première fois, la suite complète doit rester
verte **sur les deux backends** (SQLite et Postgres) — la migration de
l'étape 1 est exactement le genre de code qui passe sur l'un et casse sur
l'autre.

---

## 7. Ce qui reste à trancher par le propriétaire

1. **Option C (table `shares` unifiée, renommage d'une table en production) ou
   option B (table `trip_shares` séparée, zéro migration)** — recommandation :
   C, mais c'est le seul choix qui engage des données vivantes, donc il te
   revient.
2. **Tolérance de simplification** : 10 m recommandé. 25 m divise encore le
   poids par 1,7 et reste invisible à l'échelle d'un roadtrip.
3. **Lien vers la page d'une étape** depuis la page du roadtrip quand cette
   étape est elle aussi partagée — écarté en v1, à rouvrir si l'usage le
   demande.
4. **`GZipMiddleware`** : utile bien au-delà de cette fonctionnalité, à
   traiter comme un chantier séparé.

---

**Prochaine action : attendre le GO.** Aucune ligne de code avant.
